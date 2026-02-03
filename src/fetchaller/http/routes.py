"""HTTP routes for fetchaller MCP server."""

import sys
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import Config
from ..security.crypto import hash_api_key
from ..security.xss import sanitize_for_log
from .oauth import OAuthStore
from .templates import get_authorize_page


def create_router(
    config: Config,
    api_keys: list[str],
    api_key_hashes: set[str],
    oauth_store: OAuthStore,
    jwt_secret: bytes,
) -> APIRouter:
    """Create the API router with all endpoints."""
    router = APIRouter()
    server_url = config.effective_server_url

    # =========================================================================
    # Health Check (no auth)
    # =========================================================================
    @router.get("/health")
    async def health():
        """Health check endpoint."""
        return {
            "status": "healthy",
            "service": "fetchaller-mcp",
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }

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

    @router.get("/.well-known/oauth-authorization-server")
    async def oauth_authorization_server():
        """OAuth Authorization Server Metadata (RFC 8414)."""
        return {
            "issuer": server_url,
            "authorization_endpoint": f"{server_url}/authorize",
            "token_endpoint": f"{server_url}/token",
            "registration_endpoint": f"{server_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "scopes_supported": ["fetchaller:read"],
        }

    # =========================================================================
    # Dynamic Client Registration (RFC 7591)
    # =========================================================================
    @router.post("/register")
    async def register_client(request: Request):
        """Dynamic Client Registration endpoint."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid JSON body",
                },
            )

        redirect_uris = body.get("redirect_uris")
        client_name = body.get("client_name", "Claude")

        # Validate redirect_uris
        if not redirect_uris or not isinstance(redirect_uris, list) or len(redirect_uris) == 0:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_client_metadata",
                    "error_description": "redirect_uris is required",
                },
            )

        # Validate each redirect URI (must be HTTPS or localhost)
        for uri in redirect_uris:
            try:
                parsed = urlparse(uri)
                if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_redirect_uri",
                            "error_description": "Redirect URIs must be HTTPS or localhost",
                        },
                    )
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_redirect_uri",
                        "error_description": "Invalid redirect URI format",
                    },
                )

        # Register client
        client = oauth_store.register_client(redirect_uris, client_name)
        if client is None:
            print(f"[{datetime.now(UTC).isoformat()}] OAuth: Max clients limit reached", file=sys.stderr)
            return JSONResponse(
                status_code=503,
                content={
                    "error": "server_error",
                    "error_description": "Maximum number of clients reached. Try again later.",
                },
            )

        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: Registered client {sanitize_for_log(client.client_id)} ({sanitize_for_log(client_name)})",
            file=sys.stderr,
        )

        return JSONResponse(
            status_code=201,
            content={
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "client_name": client.client_name,
                "redirect_uris": client.redirect_uris,
                "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "none"),
            },
        )

    # =========================================================================
    # Authorization Endpoint
    # =========================================================================
    @router.get("/authorize")
    async def authorize_get(
        client_id: str = Query(...),
        redirect_uri: str = Query(None),
        response_type: str = Query(...),
        state: str = Query(None),
        code_challenge: str = Query(...),
        code_challenge_method: str = Query(None),
    ):
        """Authorization endpoint - GET shows the login form."""
        # Validate response_type
        if response_type != "code":
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unsupported_response_type",
                    "error_description": "Only response_type=code is supported",
                },
            )

        # Validate code_challenge_method
        if code_challenge_method and code_challenge_method != "S256":
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Only S256 code_challenge_method is supported",
                },
            )

        # Validate client
        client = oauth_store.get_client(client_id)
        if client is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_client",
                    "error_description": "Unknown client_id",
                },
            )

        # Validate redirect_uri
        actual_redirect_uri = redirect_uri or client.redirect_uris[0]
        if redirect_uri and redirect_uri not in client.redirect_uris:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid redirect_uri",
                },
            )

        # Show authorization form
        html = get_authorize_page(client_id, actual_redirect_uri, state, code_challenge)
        return HTMLResponse(content=html)

    @router.post("/authorize")
    async def authorize_post(
        client_id: str = Form(...),
        redirect_uri: str = Form(...),
        state: str = Form(None),
        code_challenge: str = Form(...),
        api_key: str = Form(...),
    ):
        """Authorization endpoint - POST handles form submission."""
        # Validate client
        client = oauth_store.get_client(client_id)
        if client is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_client",
                    "error_description": "Unknown client_id",
                },
            )

        # Validate redirect_uri
        if redirect_uri not in client.redirect_uris:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Invalid redirect_uri",
                },
            )

        # Validate API key
        if not api_keys:
            html = get_authorize_page(
                client_id,
                redirect_uri,
                state,
                code_challenge,
                error="Server not configured. MCP_API_KEY environment variable is not set.",
            )
            return HTMLResponse(content=html)

        # Check if API key matches any valid key
        provided_hash = hash_api_key(api_key)
        if provided_hash not in api_key_hashes:
            print(
                f"[{datetime.now(UTC).isoformat()}] OAuth: Invalid API key attempt for client {sanitize_for_log(client_id)}",
                file=sys.stderr,
            )
            html = get_authorize_page(
                client_id,
                redirect_uri,
                state,
                code_challenge,
                error="Invalid API key. Please check your MCP_API_KEY and try again.",
            )
            return HTMLResponse(content=html)

        # Create auth code
        auth_code = oauth_store.create_auth_code(client_id, code_challenge, redirect_uri, api_key)
        if auth_code is None:
            html = get_authorize_page(
                client_id,
                redirect_uri,
                state,
                code_challenge,
                error="Server busy. Please try again in a few minutes.",
            )
            return HTMLResponse(content=html)

        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: Issued auth code for client {sanitize_for_log(client_id)}",
            file=sys.stderr,
        )

        # Redirect back with code (URL-encode state to prevent open redirect)
        redirect_url = f"{redirect_uri}?code={auth_code.code}"
        if state:
            redirect_url += f"&state={quote(state, safe='')}"

        return RedirectResponse(url=redirect_url, status_code=302)

    # =========================================================================
    # Token Endpoint
    # =========================================================================
    @router.post("/token")
    async def token(request: Request):
        """Token endpoint - exchange code for access token."""
        # Parse form or JSON body
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            body = dict(form)
        elif "application/json" in content_type:
            body = await request.json()
        else:
            body = await request.form()
            body = dict(body)

        grant_type = body.get("grant_type")
        code = body.get("code")
        redirect_uri = body.get("redirect_uri")
        client_id = body.get("client_id")
        code_verifier = body.get("code_verifier")

        # Validate grant type
        if grant_type != "authorization_code":
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unsupported_grant_type",
                    "error_description": "Only authorization_code grant is supported",
                },
            )

        # Validate required parameters
        if not code or not client_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Missing required parameter: code and client_id are required",
                },
            )

        # PKCE code_verifier is required (RFC 7636)
        if not code_verifier:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "error_description": "Missing required parameter: code_verifier is required for PKCE",
                },
            )

        # Consume auth code (validates client_id, redirect_uri, PKCE)
        auth_code = oauth_store.consume_auth_code(code, client_id, redirect_uri or "", code_verifier)
        if auth_code is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_grant",
                    "error_description": "Invalid or expired authorization code",
                },
            )

        # Create access token
        access_token = oauth_store.create_access_token_entry(client_id, auth_code.api_key_hash, jwt_secret)
        if access_token is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "server_error",
                    "error_description": "Unable to create access token. Try again later.",
                },
            )

        print(
            f"[{datetime.now(UTC).isoformat()}] OAuth: Issued access token for client {sanitize_for_log(client_id)}",
            file=sys.stderr,
        )

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": config.access_token_ttl,
            "scope": "fetchaller:read",
        }

    # =========================================================================
    # MCP Endpoint
    # =========================================================================
    @router.post("/mcp")
    async def mcp_endpoint(request: Request):
        """MCP protocol endpoint (requires auth)."""
        from .middleware import get_client_ip, verify_bearer_auth

        # Verify authentication
        auth_error = verify_bearer_auth(request, api_keys, api_key_hashes, oauth_store, jwt_secret)
        if auth_error:
            client_ip = get_client_ip(request)
            print(f"[{datetime.now(UTC).isoformat()}] Auth failed: {auth_error} from {client_ip}", file=sys.stderr)
            return JSONResponse(
                status_code=401,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32001, "message": auth_error},
                    "id": None,
                },
            )

        try:
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

            await manager.handle_request(request.scope, request.receive, collect_send)

            content = b"".join(response_body)
            return Response(
                content=content,
                status_code=response_status[0],
                headers=response_headers[0],
                media_type="application/json",
            )

        except Exception as e:
            print(f"[{datetime.now(UTC).isoformat()}] Error handling MCP request: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
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
