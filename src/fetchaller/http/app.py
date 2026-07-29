"""FastAPI application setup for fetchaller HTTP server."""

import hashlib
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .. import __version__
from ..config import Config, load_config
from ..security.crypto import hash_api_key
from ..server import close_browser_runtime, create_server
from .middleware import (
    RateLimiter,
    RateLimitMiddleware,
    RequestBodyLimitMiddleware,
)
from .oauth import OAuthStore
from .routes import create_router


def _transport_security_settings(config: Config) -> TransportSecuritySettings:
    """Bind MCP Host and Origin checks to the configured public origin."""

    parsed = urlparse(config.effective_server_url)
    hostname = (parsed.hostname or "").casefold()
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = (
        f"{authority_host}:{parsed.port}"
        if parsed.port is not None
        else authority_host
    )
    origin = f"{parsed.scheme.casefold()}://{authority}"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority],
        allowed_origins=[origin],
    )


def create_app(config: Config | None = None, *, mcp_server=None) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: Optional configuration (loads from env if not provided)

    Returns:
        Configured FastAPI application
    """
    if config is None:
        config = load_config()

    # Parse API keys (comma-separated for multiple keys)
    api_key_hashes: set[str] = set()
    api_key_count = 0
    if config.api_key:
        for key in config.api_key.split(","):
            key = key.strip()
            if key:
                api_key_hashes.add(hash_api_key(key))
                api_key_count += 1

    # Create JWT secret.
    #
    # When JWT_SECRET is unset we generate a random per-process secret rather
    # than deriving one from the API key hash. The old derivation was recoverable
    # from any issued token: every token payload carries `api_key_hash` in
    # cleartext, and in single-key deployments that value equals the derivation
    # seed — so a captured token let an attacker recompute the secret and forge
    # non-expiring tokens. A random secret closes that; the only cost is that
    # tokens don't survive a restart, which is the correct trade-off. Set
    # JWT_SECRET to persist tokens across restarts.
    jwt_secret_ephemeral = not bool(config.jwt_secret)
    if config.jwt_secret:
        jwt_secret = hashlib.sha256(config.jwt_secret.encode()).digest()
    else:
        if api_key_hashes and not config.allow_ephemeral_jwt:
            raise RuntimeError(
                "JWT_SECRET must be set when MCP_API_KEY is configured in HTTP mode. "
                "Set a stable random JWT_SECRET, or set ALLOW_EPHEMERAL_JWT=1 for "
                "local development only."
            )

        import secrets

        jwt_secret = secrets.token_bytes(32)
        if api_key_hashes:
            print(
                f"[{datetime.now(UTC).isoformat()}] WARNING: JWT_SECRET not set — using a random "
                "per-process secret. OAuth tokens will not validate across a restart or across "
                "multiple workers/replicas. Set JWT_SECRET (a fixed random value) for any "
                "multi-worker or persistent deployment.",
                file=sys.stderr,
            )

    # Create OAuth store
    oauth_store = OAuthStore.from_config(config)
    runtime_readiness: dict[str, object] = {}
    if config.data_dir:
        runtime_readiness["oauth_persistence"] = oauth_store.persistence_ready

    if config.wafer_cache_dir:
        cache_path = Path(config.wafer_cache_dir)
        probe_path: str | None = None
        try:
            cache_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            probe_fd, probe_path = tempfile.mkstemp(
                prefix=".wafer-readiness-",
                dir=cache_path,
            )
            with os.fdopen(probe_fd, "wb") as probe:
                probe.write(b"fetchaller-readiness")
                probe.flush()
                os.fsync(probe.fileno())
            if Path(probe_path).read_bytes() != b"fetchaller-readiness":
                raise OSError("wafer cache read-back mismatch")
            os.unlink(probe_path)
            probe_path = None
            runtime_readiness["wafer_cache"] = True
        except OSError as exc:
            runtime_readiness["wafer_cache"] = False
            print(
                f"[{datetime.now(UTC).isoformat()}] Wafer cache is not durable: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
        finally:
            if probe_path:
                try:
                    os.unlink(probe_path)
                except OSError:
                    pass

    # Create rate limiter
    rate_limiter = RateLimiter(
        requests_per_minute=config.rate_limit_requests,
        max_entries=config.max_rate_limit_entries,
    )

    # Create MCP server and session manager
    if mcp_server is None:
        mcp_server = create_server(config)
    if config.browser_preflight:
        runtime_readiness["browser_solver"] = lambda: bool(
            getattr(
                getattr(mcp_server, "_browser_solver", None),
                "runtime_ready",
                False,
            )
        )
        runtime_readiness["browser_proxy"] = lambda: bool(
            getattr(
                getattr(mcp_server, "_browser_proxy", None),
                "ready",
                False,
            )
        )
    startup_readiness: dict[str, bool] = {}
    for name, probe in runtime_readiness.items():
        try:
            startup_readiness[name] = bool(
                probe() if callable(probe) else probe
            )
        except Exception:
            startup_readiness[name] = False
    if api_key_hashes and not all(startup_readiness.values()):
        failed = ", ".join(
            name for name, ready in startup_readiness.items() if not ready
        )
        # create_server() may already have launched Chrome and its loopback
        # egress proxy. This exception happens before FastAPI's lifespan exists,
        # so lifespan cleanup cannot run.
        close_browser_runtime(mcp_server)
        raise RuntimeError(
            f"Authenticated HTTP startup requires ready dependencies; failed: {failed}"
        )
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=True,
        json_response=True,
        security_settings=_transport_security_settings(config),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan handler."""
        cleanup_started = False
        try:
            oauth_store.start_cleanup()
            cleanup_started = True
            print(f"[{datetime.now(UTC).isoformat()}] fetchaller MCP HTTP server v{__version__} starting", file=sys.stderr)

            if api_key_hashes:
                print(f"[{datetime.now(UTC).isoformat()}] Bearer token authentication enabled ({api_key_count} key(s))", file=sys.stderr)
                print(f"[{datetime.now(UTC).isoformat()}] OAuth 2.1 endpoints enabled (for Claude.ai connectors)", file=sys.stderr)
                print(f"[{datetime.now(UTC).isoformat()}]   - Authorization: {config.effective_server_url}/authorize", file=sys.stderr)
                print(f"[{datetime.now(UTC).isoformat()}]   - Token: {config.effective_server_url}/token", file=sys.stderr)
                print(f"[{datetime.now(UTC).isoformat()}]   - Register: {config.effective_server_url}/register", file=sys.stderr)
            else:
                print(
                    f"[{datetime.now(UTC).isoformat()}] WARNING: No MCP_API_KEY set - all MCP requests will be denied",
                    file=sys.stderr,
                )

            print(f"[{datetime.now(UTC).isoformat()}] Rate limit: {config.rate_limit_requests} requests/minute per IP", file=sys.stderr)

            async with session_manager.run():
                app.state.session_manager = session_manager
                yield
        finally:
            print(
                f"[{datetime.now(UTC).isoformat()}] Shutting down...",
                file=sys.stderr,
            )
            if cleanup_started:
                try:
                    await oauth_store.stop_cleanup()
                except Exception as exc:
                    print(
                        f"[{datetime.now(UTC).isoformat()}] OAuth cleanup "
                        f"failed: {type(exc).__name__}",
                        file=sys.stderr,
                    )
            try:
                from ..server import cleanup_server

                await cleanup_server(mcp_server)
            except Exception as exc:
                print(
                    f"[{datetime.now(UTC).isoformat()}] Server cleanup "
                    f"failed: {type(exc).__name__}",
                    file=sys.stderr,
                )

    app = FastAPI(
        title="fetchaller",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,  # Disable Swagger UI
        redoc_url=None,  # Disable ReDoc
        openapi_url=None,  # Disable OpenAPI schema
    )

    # Store config items in app state for access by routes
    app.state.config = config
    app.state.api_key_hashes = api_key_hashes
    app.state.oauth_store = oauth_store
    app.state.jwt_secret = jwt_secret
    app.state.jwt_secret_ephemeral = jwt_secret_ephemeral

    # Body limiting is innermost. Rate limiting runs before it, so a client
    # that is already over quota cannot make the server consume a chunked
    # request body. Request logging is outermost so rejected requests remain
    # observable without logging query strings or credentials.
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=1024 * 1024)
    app.add_middleware(RateLimitMiddleware, rate_limiter=rate_limiter)

    @app.middleware("http")
    async def log_all_requests(request, call_next):
        """Log all incoming requests for debugging."""
        print(
            f"[{datetime.now(UTC).isoformat()}] REQUEST: {request.method} {request.url.path} from {request.client.host if request.client else 'unknown'}",
            file=sys.stderr,
        )
        response = await call_next(request)
        return response

    # Add routes
    router = create_router(
        config,
        api_key_hashes,
        oauth_store,
        jwt_secret,
        jwt_secret_ephemeral,
        runtime_readiness,
    )
    app.include_router(router)

    # Catch-all 404 handler
    @app.exception_handler(404)
    async def not_found_handler(request, exc):
        return JSONResponse(
            status_code=404,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Not found"},
                "id": None,
            },
        )

    return app


async def run_http_server(config: Config | None = None) -> None:
    """Run the HTTP server."""
    import uvicorn

    if config is None:
        config = load_config()

    # BrowserSolver uses Patchright's synchronous API. Constructing and
    # preflighting it on this asyncio thread raises before authenticated HTTP
    # can boot, so keep the synchronous dependency startup in a worker.
    import asyncio

    mcp_server = await asyncio.to_thread(create_server, config)
    app = create_app(config, mcp_server=mcp_server)

    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.http_port,
        log_level="warning",  # Reduce uvicorn noise
        # Tool handlers can have caller-selected end-to-end budgets up to
        # 180s. On termination, do not let one such request outlive Docker's
        # bounded shutdown contract; lifespan browser cleanup is separately
        # bounded to five seconds.
        timeout_graceful_shutdown=30,
    )
    server = uvicorn.Server(server_config)

    print(
        f"[{datetime.now(UTC).isoformat()}] fetchaller MCP HTTP server v{__version__} listening on port {config.http_port}",
        file=sys.stderr,
    )

    await server.serve()
