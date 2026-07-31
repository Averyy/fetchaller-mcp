"""HTTP routes for fetchaller MCP server."""

import ipaddress
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import Config, _canonical_server_origin
from ..security.crypto import hash_api_key, timing_safe_compare
from ..security.xss import sanitize_for_log
from .oauth import OAUTH_SCOPE, OAuthStore
from .templates import get_authorize_page, get_authorize_success_page

# Per-IP registration rate limiter (5 registrations per IP per hour)
_register_timestamps: dict[str, list[float]] = {}
_REGISTER_LIMIT = 5
_REGISTER_WINDOW = 3600  # 1 hour
_REGISTER_MAX_IPS = 10000  # Max tracked IPs

@dataclass(frozen=True)
class _CsrfRecord:
    expires_at: float
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    scope: str
    resource: str


# CSRF tokens are one-time and bound to the complete authorization request.
_csrf_tokens: dict[str, _CsrfRecord] = {}
_CSRF_TTL = 600  # 10 minutes
_CSRF_MAX_TOKENS = 10000  # Max outstanding tokens
_MAX_REDIRECT_URIS = 10
_MAX_REDIRECT_URI_LENGTH = 2048
# Path spellings that name this resource server: the bare origin our
# protected-resource metadata advertises, and the MCP endpoint connectors are
# configured with. Compared after stripping trailing slashes.
_MCP_RESOURCE_PATHS = ("", "/mcp")
_PKCE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_OAUTH_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
_OAUTH_TOKEN_PARAMETERS = (
    "grant_type",
    "code",
    "refresh_token",
    "redirect_uri",
    "client_id",
    "code_verifier",
    "scope",
    "resource",
)


class _DuplicateJsonParameterError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonParameterError(key)
        result[key] = value
    return result


def _valid_redirect_uri(uri: str) -> bool:
    """Validate an OAuth redirect URI without accepting executable URL syntax."""

    if (
        not uri
        or len(uri) > _MAX_REDIRECT_URI_LENGTH
        or not uri.isascii()
        or "#" in uri
    ):
        return False
    if any(ord(char) <= 0x20 or ord(char) == 0x7F or char in '<>"\'\\' for char in uri):
        return False
    for index, char in enumerate(uri):
        if char == "%" and (
            index + 2 >= len(uri)
            or not all(
                digit in "0123456789abcdefABCDEF"
                for digit in uri[index + 1 : index + 3]
            )
        ):
            return False
    try:
        parsed = urlparse(uri)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if (
        not parsed.hostname
        or not parsed.netloc
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.scheme not in {"http", "https"}
        or port == 0
        or parsed.netloc.rsplit("@", 1)[-1].endswith(":")
    ):
        return False
    hostname = parsed.hostname
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname != "localhost":
            try:
                ascii_hostname = hostname.encode("idna").decode("ascii")
            except UnicodeError:
                return False
            if (
                len(ascii_hostname) > 253
                or ascii_hostname.endswith(".")
                or not all(
                    _DNS_LABEL_RE.fullmatch(label)
                    for label in ascii_hostname.split(".")
                )
            ):
                return False
        loopback = hostname == "localhost"
    else:
        loopback = str(address) in {"127.0.0.1", "::1"}
    return parsed.scheme == "https" or loopback


def _append_redirect_params(
    redirect_uri: str,
    code: str,
    state: str | None,
) -> str:
    parsed = urlparse(redirect_uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("code", code))
    if state:
        query.append(("state", state))
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def _new_csrf_token(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str,
    resource: str,
) -> str:
    """Generate and store a new CSRF token, evicting expired entries if needed."""
    now = time.time()
    # Evict expired tokens if at capacity
    if len(_csrf_tokens) >= _CSRF_MAX_TOKENS:
        expired = [
            token
            for token, record in _csrf_tokens.items()
            if record.expires_at < now
        ]
        for t in expired:
            del _csrf_tokens[t]
    # If still at capacity after cleanup, evict oldest entries
    if len(_csrf_tokens) >= _CSRF_MAX_TOKENS:
        by_expiry = sorted(
            _csrf_tokens.items(),
            key=lambda item: item[1].expires_at,
        )
        for t, _ in by_expiry[: len(by_expiry) // 2]:
            del _csrf_tokens[t]
    token = secrets.token_urlsafe(32)
    _csrf_tokens[token] = _CsrfRecord(
        expires_at=now + _CSRF_TTL,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        scope=scope,
        resource=resource,
    )
    return token


def _oauth_json(
    status_code: int,
    content: dict,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response_headers = dict(_OAUTH_NO_STORE_HEADERS)
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=response_headers,
    )


def _authorization_headers(csp_nonce: str) -> dict[str, str]:
    return {
        **_OAUTH_NO_STORE_HEADERS,
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"style-src 'nonce-{csp_nonce}' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            f"script-src 'nonce-{csp_nonce}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    }


def _authorization_html(
    content: str,
    csp_nonce: str,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return HTMLResponse(
        content=content,
        status_code=status_code,
        headers=_authorization_headers(csp_nonce),
    )


def _bounded_string(
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and (allow_empty or bool(value))
        and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in value)
    )


def _canonical_resource_indicator(value: object, server_url: str) -> str | None:
    """Fold an RFC 8707 resource indicator naming this server to its canonical origin.

    Clients derive the indicator from the URL they were configured with, so a
    connector pointed at ``https://host/mcp`` sends that spelling, while our
    protected-resource metadata advertises the bare origin. Both name this
    resource server, so accept either (trailing slash optional) and return the
    canonical origin that access tokens carry as ``aud``. Anything naming a
    different origin returns ``None``, so audience binding still rejects tokens
    minted for someone else's resource server.
    """

    if not _bounded_string(value, maximum=_MAX_REDIRECT_URI_LENGTH):
        return None
    if value == server_url:
        return server_url
    if not value.isascii():
        return None
    try:
        parsed = urlparse(value)
        origin = _canonical_server_origin(value)
    except ValueError:
        return None
    if (
        parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") not in _MCP_RESOURCE_PATHS
        or origin != server_url
    ):
        return None
    return server_url


def _safe_log(value: object, maximum: int = 64) -> str:
    return sanitize_for_log(str(value), max_length=maximum)


def create_router(
    config: Config,
    api_key_hashes: set[str],
    oauth_store: OAuthStore,
    jwt_secret: bytes,
    jwt_secret_ephemeral: bool = False,
    runtime_readiness: dict[str, object] | None = None,
) -> APIRouter:
    """Create the API router with all endpoints."""
    router = APIRouter()
    server_url = config.effective_server_url
    readiness = runtime_readiness or {}

    # =========================================================================
    # Health Check (no auth)
    # =========================================================================
    @router.get("/health")
    async def health():
        """Health check endpoint."""
        current_readiness: dict[str, bool] = {}
        for name, probe in readiness.items():
            try:
                current_readiness[name] = bool(
                    probe() if callable(probe) else probe
                )
            except Exception:
                current_readiness[name] = False
        current_readiness["authentication"] = bool(api_key_hashes)
        if config.data_dir:
            # Persistence performs a complete temp-write/fsync/replace/
            # directory-fsync check at startup and updates this status after
            # every mutation. Health requests stay read-only and cheap.
            current_readiness["oauth_persistence"] = oauth_store.persistence_ready
        healthy = all(current_readiness.values()) if current_readiness else True
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "healthy" if healthy else "unhealthy",
                "service": "fetchaller-mcp",
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "jwt_secret_ephemeral": jwt_secret_ephemeral,
                "readiness": current_readiness,
            },
        )

    # =========================================================================
    # OAuth 2.1 Discovery Endpoints (no auth)
    # =========================================================================
    @router.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource():
        """OAuth Protected Resource Metadata (RFC 9728)."""
        return {
            "resource": server_url,
            "authorization_servers": [server_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["fetchaller:read"],
        }

    # Claude.ai sometimes appends /mcp to the protected resource path
    @router.get("/.well-known/oauth-protected-resource/mcp")
    async def oauth_protected_resource_mcp():
        """OAuth Protected Resource Metadata - alternate path."""
        return {
            "resource": server_url,
            "authorization_servers": [server_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["fetchaller:read"],
        }

    def _oauth_server_metadata() -> dict:
        """OAuth Authorization Server Metadata (shared by discovery endpoints)."""
        return {
            "issuer": server_url,
            "authorization_endpoint": f"{server_url}/authorize",
            "token_endpoint": f"{server_url}/token",
            "registration_endpoint": f"{server_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["fetchaller:read"],
        }

    @router.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server():
        """OAuth Authorization Server Metadata (RFC 8414)."""
        return _oauth_server_metadata()

    # OpenID Connect discovery - return OAuth metadata for compatibility
    @router.get("/.well-known/openid-configuration")
    async def openid_configuration():
        """OpenID Connect discovery - returns OAuth metadata for compatibility."""
        return _oauth_server_metadata()

    # =========================================================================
    # Dynamic Client Registration (RFC 7591)
    # =========================================================================
    @router.post("/register")
    async def register_client(request: Request):
        """Dynamic Client Registration endpoint."""
        from .middleware import get_client_ip

        # Per-IP rate limiting for registration
        client_ip = get_client_ip(request)
        now = time.time()
        cutoff = now - _REGISTER_WINDOW
        timestamps = _register_timestamps.get(client_ip, [])
        timestamps = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= _REGISTER_LIMIT:
            return _oauth_json(
                status_code=429,
                content={
                    "error": "too_many_requests",
                    "error_description": f"Registration limit ({_REGISTER_LIMIT} per hour) exceeded. Try again later.",
                },
                headers={"Retry-After": "3600"},
            )
        timestamps.append(now)
        _register_timestamps[client_ip] = timestamps

        # Evict stale IPs if dict is too large
        if len(_register_timestamps) > _REGISTER_MAX_IPS:
            stale = [ip for ip, ts in _register_timestamps.items() if not ts or ts[-1] < cutoff]
            for ip in stale:
                del _register_timestamps[ip]
            # If still over limit after stale cleanup, evict oldest entries
            if len(_register_timestamps) > _REGISTER_MAX_IPS:
                by_latest = sorted(_register_timestamps.items(), key=lambda x: x[1][-1] if x[1] else 0)
                for ip, _ in by_latest[: len(by_latest) // 2]:
                    del _register_timestamps[ip]

        try:
            body = await request.json()
        except Exception:
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid JSON body",
                },
            )
        if not isinstance(body, dict):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "JSON body must be an object",
                },
            )

        redirect_uris = body.get("redirect_uris")
        client_name = body.get("client_name", "OAuth client")
        token_endpoint_auth_method = body.get("token_endpoint_auth_method", "none")

        # Validate redirect_uris
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or len(redirect_uris) > _MAX_REDIRECT_URIS
            or not all(isinstance(uri, str) and uri for uri in redirect_uris)
            or len(set(redirect_uris)) != len(redirect_uris)
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_client_metadata",
                    "error_description": (
                        f"redirect_uris must contain 1-{_MAX_REDIRECT_URIS} strings"
                    ),
                },
            )
        if not _bounded_string(client_name, maximum=200) or not client_name.strip():
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_client_metadata",
                    "error_description": "client_name must be a non-empty string up to 200 characters",
                },
            )
        if token_endpoint_auth_method != "none":
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_client_metadata",
                    "error_description": "Only token_endpoint_auth_method=none is supported",
                },
            )

        # Validate each redirect URI (HTTPS, or HTTP on an exact loopback host).
        for uri in redirect_uris:
            if not _valid_redirect_uri(uri):
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_redirect_uri",
                        "error_description": (
                            "Redirect URIs must be HTTPS (or HTTP on localhost, "
                            "127.0.0.1, or ::1), contain no credentials or fragment, "
                            f"and be at most {_MAX_REDIRECT_URI_LENGTH} characters"
                        ),
                    },
                )

        # Register client
        client = oauth_store.register_client(redirect_uris, client_name.strip())
        if client is None:
            print(f"[{datetime.now(UTC).isoformat()}] OAuth: Max clients limit reached", file=sys.stderr)
            if config.data_dir and not oauth_store.persistence_ready:
                return _oauth_json(
                    status_code=503,
                    content={
                        "error": "server_error",
                        "error_description": "Registration persistence is unavailable",
                    },
                )
            return _oauth_json(
                status_code=503,
                content={
                    "error": "server_error",
                    "error_description": "Maximum number of clients reached. Try again later.",
                },
            )

        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: Registered client "
            f"{_safe_log(client.client_id)} ({_safe_log(client_name)})",
            file=sys.stderr,
        )

        return _oauth_json(
            status_code=201,
            content={
                "client_id": client.client_id,
                "client_name": client.client_name,
                "redirect_uris": client.redirect_uris,
                "token_endpoint_auth_method": token_endpoint_auth_method,
            },
        )

    # =========================================================================
    # Authorization Endpoint
    # =========================================================================
    def render_authorize_form(
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        scope: str,
        resource: str,
        client_name: str,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        csrf_token = _new_csrf_token(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            scope=scope,
            resource=resource,
        )
        csp_nonce = secrets.token_urlsafe(18)
        html = get_authorize_page(
            client_id,
            redirect_uri,
            state,
            code_challenge,
            error=error,
            csrf_token=csrf_token,
            client_name=client_name,
            scope=scope,
            resource=resource,
            csp_nonce=csp_nonce,
        )
        return _authorization_html(html, csp_nonce, status_code=status_code)

    @router.head("/authorize")
    async def authorize_head():
        """HEAD request for authorize - some clients check this first."""
        csp_nonce = secrets.token_urlsafe(18)
        return Response(
            status_code=200,
            headers={
                **_authorization_headers(csp_nonce),
                "Content-Type": "text/html; charset=utf-8",
            },
        )

    @router.get("/authorize")
    async def authorize_get(
        request: Request,
        client_id: str = Query(None),
        redirect_uri: str = Query(None),
        response_type: str = Query(None),
        state: str = Query(None),
        code_challenge: str = Query(None),
        code_challenge_method: str = Query(None),
        scope: str = Query(None),
        resource: str = Query(None),
    ):
        """Authorization endpoint - GET shows the login form."""
        authorize_parameter_names = (
            "client_id",
            "redirect_uri",
            "response_type",
            "state",
            "code_challenge",
            "code_challenge_method",
            "scope",
            "resource",
        )
        if any(
            len(request.query_params.getlist(name)) > 1
            for name in authorize_parameter_names
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": (
                        "Authorization parameters must not be repeated"
                    ),
                },
            )
        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: /authorize GET - "
            f"client_id={_safe_log(client_id)}, "
            f"redirect_uri={'present' if redirect_uri else 'default'}, "
            f"response_type={_safe_log(response_type, 16)}, "
            f"code_challenge={'present' if code_challenge else 'MISSING'}, "
            f"code_challenge_method={_safe_log(code_challenge_method, 16)}, "
            f"scope={'present' if scope else 'default'}, "
            f"resource={'present' if resource else 'default'}",
            file=sys.stderr,
        )

        # Manual validation for OAuth-compliant error responses (not FastAPI's 422)
        if not _bounded_string(client_id, maximum=512):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "client_id must be a bounded non-empty string",
                },
            )

        if not _bounded_string(response_type, maximum=32) or response_type != "code":
            return _oauth_json(
                status_code=400,
                content={
                    "error": "unsupported_response_type",
                    "error_description": "Only response_type=code is supported",
                },
            )

        if (
            not isinstance(code_challenge, str)
            or not _PKCE_CHALLENGE_RE.fullmatch(code_challenge)
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid PKCE S256 code_challenge",
                },
            )

        if code_challenge_method != "S256":
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Only S256 code_challenge_method is supported",
                },
            )

        if state is not None and not _bounded_string(
            state,
            maximum=2048,
            allow_empty=True,
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "state must be a bounded string",
                },
            )
        requested_scope = scope or OAUTH_SCOPE
        if (
            not _bounded_string(requested_scope, maximum=256)
            or requested_scope != OAUTH_SCOPE
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_scope",
                    "error_description": f"Only scope={OAUTH_SCOPE} is supported",
                },
            )
        requested_resource = _canonical_resource_indicator(
            resource or server_url,
            server_url,
        )
        if requested_resource is None:
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_target",
                    "error_description": "The requested resource is not supported",
                },
            )

        client = oauth_store.get_client(client_id)
        if client is None:
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_client",
                    "error_description": "Unknown client_id",
                },
            )

        # Validate redirect_uri
        actual_redirect_uri = redirect_uri or client.redirect_uris[0]
        if (
            not _bounded_string(
                actual_redirect_uri,
                maximum=_MAX_REDIRECT_URI_LENGTH,
            )
            or not _valid_redirect_uri(actual_redirect_uri)
            or actual_redirect_uri not in client.redirect_uris
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid redirect_uri",
                },
            )

        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: /authorize accepted "
            f"for client {_safe_log(client_id)}",
            file=sys.stderr,
        )
        return render_authorize_form(
            client_id=client_id,
            redirect_uri=actual_redirect_uri,
            state=state or "",
            code_challenge=code_challenge,
            scope=requested_scope,
            resource=requested_resource,
            client_name=client.client_name,
        )

    @router.post("/authorize")
    async def authorize_post(request: Request):
        """Authorization endpoint - POST handles form submission."""
        try:
            form = await request.form()
        except Exception:
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid authorization form",
                },
            )
        parameter_names = (
            "client_id",
            "redirect_uri",
            "state",
            "code_challenge",
            "api_key",
            "csrf_token",
            "scope",
            "resource",
        )
        if any(len(form.getlist(name)) > 1 for name in parameter_names):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Authorization parameters must not be repeated",
                },
            )
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        state = form.get("state", "")
        code_challenge = form.get("code_challenge", "")
        api_key = form.get("api_key", "")
        csrf_token = form.get("csrf_token", "")
        scope = form.get("scope", "")
        resource = form.get("resource", "")

        start_time = time.time()
        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: /authorize POST "
            f"for client {_safe_log(client_id)}",
            file=sys.stderr,
        )

        try:
            if not (
                _bounded_string(client_id, maximum=512)
                and _bounded_string(
                    redirect_uri,
                    maximum=_MAX_REDIRECT_URI_LENGTH,
                )
                and _bounded_string(state, maximum=2048, allow_empty=True)
                and isinstance(code_challenge, str)
                and _PKCE_CHALLENGE_RE.fullmatch(code_challenge)
                and _bounded_string(api_key, maximum=8192)
                and _bounded_string(csrf_token, maximum=128)
                and _bounded_string(scope, maximum=256)
                and _bounded_string(
                    resource,
                    maximum=_MAX_REDIRECT_URI_LENGTH,
                )
            ):
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "error_description": "Authorization form parameters are invalid",
                    },
                )

            now = time.time()
            csrf_record = _csrf_tokens.pop(csrf_token, None)
            expected_csrf = _CsrfRecord(
                expires_at=csrf_record.expires_at if csrf_record else 0,
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                scope=scope,
                resource=resource,
            )
            if (
                csrf_record is None
                or csrf_record.expires_at < now
                or csrf_record != expected_csrf
            ):
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "error_description": "Authorization session is invalid or expired",
                    },
                )

            if scope != OAUTH_SCOPE:
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_scope",
                        "error_description": f"Only scope={OAUTH_SCOPE} is supported",
                    },
                )
            canonical_resource = _canonical_resource_indicator(resource, server_url)
            if canonical_resource is None:
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_target",
                        "error_description": "The requested resource is not supported",
                    },
                )
            resource = canonical_resource

            client = oauth_store.get_client(client_id)
            if client is None:
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_client",
                        "error_description": "Unknown client_id",
                    },
                )

            # Validate redirect_uri
            if (
                not _valid_redirect_uri(redirect_uri)
                or redirect_uri not in client.redirect_uris
            ):
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "error_description": "Invalid redirect_uri",
                    },
                )

            # Validate API key
            if not api_key_hashes:
                return render_authorize_form(
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    state=state,
                    code_challenge=code_challenge,
                    scope=scope,
                    resource=resource,
                    client_name=client.client_name,
                    error="Server not configured. MCP_API_KEY environment variable is not set.",
                    status_code=503,
                )

            # Check if API key matches any valid key (timing-safe)
            provided_hash = hash_api_key(api_key)
            if not any(timing_safe_compare(provided_hash, h) for h in api_key_hashes):
                print(
                    f"[{datetime.now(UTC).isoformat()}] OAuth: Invalid API key "
                    f"for client {_safe_log(client_id)}",
                    file=sys.stderr,
                )
                return render_authorize_form(
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    state=state,
                    code_challenge=code_challenge,
                    scope=scope,
                    resource=resource,
                    client_name=client.client_name,
                    error="Invalid API key. Please check your MCP_API_KEY and try again.",
                )

            # Create auth code
            auth_code = oauth_store.create_auth_code(
                client_id,
                code_challenge,
                redirect_uri,
                api_key,
                scope,
                resource,
            )
            if auth_code is None:
                return render_authorize_form(
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    state=state,
                    code_challenge=code_challenge,
                    scope=scope,
                    resource=resource,
                    client_name=client.client_name,
                    error="Server busy. Please try again in a few minutes.",
                    status_code=503,
                )

            redirect_url = _append_redirect_params(
                redirect_uri,
                auth_code.code,
                state,
            )

            elapsed = time.time() - start_time
            print(
                f"[{datetime.now(UTC).isoformat()}] OAuth: Issued auth code for "
                f"client {_safe_log(client_id)} in {elapsed:.3f}s",
                file=sys.stderr,
            )

            csp_nonce = secrets.token_urlsafe(18)
            return _authorization_html(
                get_authorize_success_page(
                    redirect_url,
                    client.client_name,
                    csp_nonce,
                ),
                csp_nonce,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            print(
                f"[{datetime.now(UTC).isoformat()}] OAuth: /authorize POST failed "
                f"after {elapsed:.3f}s ({type(e).__name__})",
                file=sys.stderr,
            )
            return _oauth_json(
                status_code=500,
                content={
                    "error": "server_error",
                    "error_description": "Authorization failed",
                },
            )

    # =========================================================================
    # Token Endpoint
    # =========================================================================
    @router.post("/token")
    async def token(request: Request):
        """Token endpoint - exchange code for access token."""
        print(f"[{datetime.now(UTC).isoformat()}] OAuth: /token POST received", file=sys.stderr)

        # Parse form or JSON body
        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: /token "
            f"media_type={_safe_log(media_type, 64)}",
            file=sys.stderr,
        )

        try:
            if media_type == "application/x-www-form-urlencoded":
                form = await request.form()
                if any(
                    len(form.getlist(name)) > 1
                    for name in _OAUTH_TOKEN_PARAMETERS
                ):
                    raise _DuplicateJsonParameterError()
                body = dict(form)
            elif media_type == "application/json":
                body = json.loads(
                    await request.body(),
                    object_pairs_hook=_unique_json_object,
                )
            else:
                form = await request.form()
                if any(
                    len(form.getlist(name)) > 1
                    for name in _OAUTH_TOKEN_PARAMETERS
                ):
                    raise _DuplicateJsonParameterError()
                body = dict(form)
        except _DuplicateJsonParameterError:
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": (
                        "Token parameters must not be repeated"
                    ),
                },
            )
        except Exception:
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid request body",
                },
            )
        if not isinstance(body, dict):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Request body must be an object",
                },
            )

        grant_type = body.get("grant_type")
        code = body.get("code")
        refresh_token = body.get("refresh_token")
        redirect_uri = body.get("redirect_uri")
        client_id = body.get("client_id")
        code_verifier = body.get("code_verifier")
        requested_scope = body.get("scope")
        requested_resource = body.get("resource")

        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: /token params - "
            f"grant_type={_safe_log(grant_type, 32)}, "
            f"client_id={_safe_log(client_id)}, "
            f"code={'present' if code else 'MISSING'}, "
            f"refresh_token={'present' if refresh_token else 'MISSING'}, "
            f"redirect_uri={'present' if redirect_uri else 'MISSING'}, "
            f"code_verifier={'present' if code_verifier else 'MISSING'}, "
            f"scope={'present' if requested_scope is not None else 'default'}, "
            f"resource={'present' if requested_resource is not None else 'default'}",
            file=sys.stderr,
        )

        # Validate grant type
        if (
            not _bounded_string(grant_type, maximum=32)
            or grant_type not in ("authorization_code", "refresh_token")
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "unsupported_grant_type",
                    "error_description": "Only authorization_code and refresh_token grants are supported",
                },
            )

        scope = requested_scope if requested_scope is not None else OAUTH_SCOPE
        if (
            not _bounded_string(scope, maximum=256)
            or scope != OAUTH_SCOPE
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_scope",
                    "error_description": f"Only scope={OAUTH_SCOPE} is supported",
                },
            )
        resource = _canonical_resource_indicator(
            requested_resource if requested_resource is not None else server_url,
            server_url,
        )
        if resource is None:
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_target",
                    "error_description": "The requested resource is not supported",
                },
            )

        if grant_type == "refresh_token":
            if (
                not _bounded_string(refresh_token, maximum=1024)
                or not _bounded_string(client_id, maximum=512)
            ):
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "error_description": (
                            "Missing required parameter: refresh_token and client_id are required"
                        ),
                    },
                )

            rotated = oauth_store.rotate_refresh_token(
                refresh_token,
                client_id,
                api_key_hashes,
                scope,
                resource,
            )
            if rotated is None:
                if config.data_dir and not oauth_store.persistence_ready:
                    return _oauth_json(
                        status_code=503,
                        content={
                            "error": "server_error",
                            "error_description": "Token persistence is unavailable",
                        },
                    )
                print(
                    f"[{datetime.now(UTC).isoformat()}] OAuth: /token FAILED - "
                    "invalid refresh token",
                    file=sys.stderr,
                )
                return _oauth_json(
                    status_code=400,
                    content={
                        "error": "invalid_grant",
                        "error_description": "Invalid or expired refresh token",
                    },
                )

            new_refresh_token, api_key_hash, token_scope, token_resource = rotated
            access_token = oauth_store.create_access_token_entry(
                client_id,
                api_key_hash,
                jwt_secret,
                token_scope,
                token_resource,
            )
            print(
                f"[{datetime.now(UTC).isoformat()}] OAuth: Rotated refresh token "
                f"for client {_safe_log(client_id)}",
                file=sys.stderr,
            )
            return _oauth_json(
                status_code=200,
                content={
                    "access_token": access_token,
                    "refresh_token": new_refresh_token,
                    "token_type": "Bearer",
                    "expires_in": config.access_token_ttl,
                    "scope": token_scope,
                },
            )

        # Validate required parameters
        if (
            not _bounded_string(code, maximum=1024)
            or not _bounded_string(client_id, maximum=512)
            or not _bounded_string(
                redirect_uri,
                maximum=_MAX_REDIRECT_URI_LENGTH,
            )
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": (
                        "code, client_id, and redirect_uri must be bounded non-empty strings"
                    ),
                },
            )

        # PKCE code_verifier is required (RFC 7636)
        if (
            not isinstance(code_verifier, str)
            or not 43 <= len(code_verifier) <= 128
            or not all(
                char.isascii() and (char.isalnum() or char in "-._~")
                for char in code_verifier
            )
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "code_verifier must satisfy RFC 7636 (43-128 characters)",
                },
            )

        # Consume auth code (validates client_id, redirect_uri, PKCE)
        auth_code = oauth_store.consume_auth_code(code, client_id, redirect_uri, code_verifier)
        if (
            auth_code is None
            or not timing_safe_compare(auth_code.scope, scope)
            or not timing_safe_compare(auth_code.resource, resource)
        ):
            return _oauth_json(
                status_code=400,
                content={
                    "error": "invalid_grant",
                    "error_description": "Invalid or expired authorization code",
                },
            )

        # Create access token
        access_token = oauth_store.create_access_token_entry(
            client_id,
            auth_code.api_key_hash,
            jwt_secret,
            auth_code.scope,
            auth_code.resource,
        )
        refresh_token = oauth_store.create_refresh_token(
            client_id,
            auth_code.api_key_hash,
            auth_code.scope,
            auth_code.resource,
        )
        if refresh_token is None:
            oauth_store.restore_auth_code(auth_code)
            return _oauth_json(
                status_code=503,
                content={
                    "error": "server_error",
                    "error_description": "Token persistence is unavailable",
                },
            )

        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: Issued access and refresh tokens "
            f"for client {_safe_log(client_id)}",
            file=sys.stderr,
        )

        return _oauth_json(
            status_code=200,
            content={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": config.access_token_ttl,
                "scope": auth_code.scope,
            },
        )

    # =========================================================================
    # MCP Endpoint
    # =========================================================================
    @router.post("/mcp")
    async def mcp_endpoint(request: Request):
        """MCP protocol endpoint (requires auth)."""
        import json as json_module

        from .middleware import get_client_ip, verify_bearer_auth

        client_ip = _safe_log(get_client_ip(request))
        protocol_version = request.headers.get("mcp-protocol-version")
        print(
            f"[{datetime.now(UTC).isoformat()}] MCP request from {client_ip} - "
            f"protocol={_safe_log(protocol_version or 'not-set', 32)} "
            f"accept={'present' if request.headers.get('accept') else 'missing'}",
            file=sys.stderr,
        )

        # Verify authentication
        auth_error = verify_bearer_auth(request, api_key_hashes, oauth_store, jwt_secret)
        if auth_error:
            print(f"[{datetime.now(UTC).isoformat()}] Auth failed: {auth_error} from {client_ip}", file=sys.stderr)
            # Per MCP spec: 401 MUST include WWW-Authenticate with resource_metadata
            return JSONResponse(
                status_code=401,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32001, "message": auth_error},
                    "id": None,
                },
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{server_url}/.well-known/oauth-protected-resource", scope="fetchaller:read"',
                },
            )

        method = "unknown"
        try:
            # Reject oversized request bodies (1MB max for MCP JSON-RPC)
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = -1
                if declared_length < 0:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32600,
                                "message": "Invalid Content-Length",
                            },
                            "id": None,
                        },
                    )
            else:
                declared_length = 0
            if declared_length > 1_048_576:
                return JSONResponse(
                    status_code=413,
                    content={
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": "Request body too large (max 1MB)"},
                        "id": None,
                    },
                )

            # Read and log request body for debugging
            body_bytes = await request.body()
            if len(body_bytes) > 1_048_576:
                return JSONResponse(
                    status_code=413,
                    content={
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": "Request body too large (max 1MB)"},
                        "id": None,
                    },
                )
            try:
                body_json = json_module.loads(body_bytes)
                if isinstance(body_json, dict):
                    method = _safe_log(body_json.get("method", "unknown"))
                print(
                    f"[{datetime.now(UTC).isoformat()}] MCP method={method} "
                    f"from {client_ip}",
                    file=sys.stderr,
                )
            except Exception:
                print(f"[{datetime.now(UTC).isoformat()}] MCP request body parse failed from {client_ip}", file=sys.stderr)

            # Get session manager from app state
            manager = request.app.state.session_manager

            # Create a response collector since handle_request uses ASGI send
            response_body = []
            response_status = [200]
            response_headers = [{}]

            async def collect_send(message):
                if message["type"] == "http.response.start":
                    response_status[0] = message.get("status", 200)
                    response_headers[0] = {
                        k.decode(): v.decode() for k, v in message.get("headers", [])
                    }
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    if body:
                        response_body.append(body)

            # We need to create a new receive that returns the body we already read
            body_consumed = False
            async def receive_with_body():
                nonlocal body_consumed
                if not body_consumed:
                    body_consumed = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return {"type": "http.disconnect"}

            await manager.handle_request(request.scope, receive_with_body, collect_send)

            content = b"".join(response_body)
            print(f"[{datetime.now(UTC).isoformat()}] MCP response status={response_status[0]} size={len(content)} for method={method}", file=sys.stderr)
            return Response(
                content=content,
                status_code=response_status[0],
                headers=response_headers[0],
                media_type="application/json",
            )

        except Exception as exc:
            print(
                f"[{datetime.now(UTC).isoformat()}] Error handling MCP request "
                f"({type(exc).__name__})",
                file=sys.stderr,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": "Internal server error"},
                    "id": None,
                },
            )

    @router.head("/mcp")
    async def mcp_head():
        """MCP protocol discovery (HEAD request)."""
        return Response(
            status_code=200,
            headers={
                "MCP-Protocol-Version": "2025-06-18",
                "Allow": "POST, HEAD",
            },
        )

    @router.get("/mcp")
    async def mcp_get_not_allowed():
        """Reject GET on /mcp."""
        return JSONResponse(
            status_code=405,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Method not allowed. Use POST or HEAD."},
                "id": None,
            },
            headers={"Allow": "POST, HEAD"},
        )

    @router.delete("/mcp")
    async def mcp_delete_not_allowed():
        """Reject DELETE on /mcp."""
        return JSONResponse(
            status_code=405,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Method not allowed. Use POST or HEAD."},
                "id": None,
            },
            headers={"Allow": "POST, HEAD"},
        )

    return router
