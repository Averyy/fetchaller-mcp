"""FastAPI application setup for fetchaller HTTP server."""

import hashlib
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .. import __version__
from ..config import Config, load_config
from ..security.crypto import hash_api_key
from ..server import create_server
from .middleware import RateLimiter, RateLimitMiddleware
from .oauth import OAuthStore
from .routes import create_router


def create_app(config: Config | None = None) -> FastAPI:
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

    # Create rate limiter
    rate_limiter = RateLimiter(
        requests_per_minute=config.rate_limit_requests,
        max_entries=config.max_rate_limit_entries,
    )

    # Create MCP server and session manager
    mcp_server = create_server(config)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=True,
        json_response=True,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Application lifespan handler."""
        # Startup
        oauth_store.start_cleanup()
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

        # Start the session manager
        async with session_manager.run():
            # Store session manager in app state for access by routes
            app.state.session_manager = session_manager
            yield

        # Shutdown
        print(f"[{datetime.now(UTC).isoformat()}] Shutting down...", file=sys.stderr)
        await oauth_store.stop_cleanup()
        from ..server import cleanup_server
        await cleanup_server(mcp_server)

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

    # Add request logging middleware (runs BEFORE rate limiting)
    @app.middleware("http")
    async def log_all_requests(request, call_next):
        """Log all incoming requests for debugging."""
        print(
            f"[{datetime.now(UTC).isoformat()}] REQUEST: {request.method} {request.url.path} from {request.client.host if request.client else 'unknown'}",
            file=sys.stderr,
        )
        response = await call_next(request)
        return response

    # Add rate limiting middleware
    app.add_middleware(RateLimitMiddleware, rate_limiter=rate_limiter)

    # Add routes
    router = create_router(
        config,
        api_key_hashes,
        oauth_store,
        jwt_secret,
        jwt_secret_ephemeral,
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

    app = create_app(config)

    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.http_port,
        log_level="warning",  # Reduce uvicorn noise
    )
    server = uvicorn.Server(server_config)

    print(
        f"[{datetime.now(UTC).isoformat()}] fetchaller MCP HTTP server v{__version__} listening on port {config.http_port}",
        file=sys.stderr,
    )

    await server.serve()
