"""HTTP middleware for rate limiting and authentication."""

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..security.crypto import timing_safe_compare
from .oauth import OAuthStore


@dataclass
class RateLimitEntry:
    """Rate limit tracking for a single IP."""

    requests: deque = field(default_factory=deque)


class RateLimiter:
    """
    In-memory rate limiter with bounded memory.

    Note: Rate limits are per-container. For horizontal scaling,
    use external rate limiting (Redis, Caddy plugin, etc.)
    """

    def __init__(self, requests_per_minute: int = 100, max_entries: int = 10000):
        self.requests_per_minute = requests_per_minute
        self.max_entries = max_entries
        self._entries: dict[str, RateLimitEntry] = {}
        self._last_cleanup = time.time()

    def _cleanup(self) -> None:
        """Remove stale entries."""
        now = time.time()

        # Only cleanup every 30 seconds
        if now - self._last_cleanup < 30:
            return
        self._last_cleanup = now

        cutoff = now - 60
        stale = []
        for ip, entry in self._entries.items():
            if not entry.requests or entry.requests[-1] < cutoff:
                stale.append(ip)
        for ip in stale:
            del self._entries[ip]

    def check(self, client_ip: str) -> tuple[bool, int | None]:
        """
        Check if request is allowed.

        Returns:
            (allowed, retry_after_seconds)
        """
        now = time.time()
        cutoff = now - 60

        entry = self._entries.get(client_ip)
        if entry is None:
            # Enforce max entries
            if len(self._entries) >= self.max_entries:
                self._cleanup()
                if len(self._entries) >= self.max_entries:
                    return False, 60

            entry = RateLimitEntry()
            self._entries[client_ip] = entry

        # Filter old requests
        while entry.requests and entry.requests[0] < cutoff:
            entry.requests.popleft()

        # Check limit
        if len(entry.requests) >= self.requests_per_minute:
            return False, 60

        # Record request
        entry.requests.append(now)

        # Trigger periodic cleanup
        self._cleanup()

        return True, None


def get_client_ip(request: Request) -> str:
    """
    Get client IP from request.

    Uses rightmost X-Forwarded-For IP, which is typically set by the closest
    reverse proxy. This approach is reasonable for single-proxy deployments
    (e.g., Caddy, nginx) but has limitations:

    Security Note:
    - In multi-proxy environments, clients could potentially spoof IPs by
      prepending fake values to X-Forwarded-For
    - For production deployments with multiple proxies, consider configuring
      trusted proxy IPs at the reverse proxy level (e.g., Caddy's trusted_proxies)
    - Rate limiting is primarily a DoS mitigation, not a security boundary,
      so the risk is limited to rate limit bypass
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Use rightmost IP (set by our trusted reverse proxy)
        ips = forwarded.split(",")
        return ips[-1].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware."""

    def __init__(self, app, rate_limiter: RateLimiter):
        super().__init__(app)
        self.rate_limiter = rate_limiter

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks
        if request.url.path == "/health":
            return await call_next(request)

        client_ip = get_client_ip(request)
        allowed, retry_after = self.rate_limiter.check(client_ip)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32000,
                        "message": f"Rate limit exceeded ({self.rate_limiter.requests_per_minute} requests/minute). Try again in {retry_after or 60} seconds.",
                    },
                    "id": None,
                },
                headers={"Retry-After": str(retry_after or 60)},
            )

        return await call_next(request)


def verify_bearer_auth(
    request: Request,
    api_key_hashes: set[str],
    oauth_store: OAuthStore | None,
    jwt_secret: bytes | None,
) -> str | None:
    """
    Verify Bearer token authentication.

    Accepts EITHER:
    1. Raw API key (for direct Bearer token auth)
    2. OAuth JWT token (issued by /token endpoint)

    Returns error message if auth fails, None if successful.
    """
    auth_header = request.headers.get("authorization")
    if not auth_header:
        return "Missing Authorization header"

    # Parse auth header
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return "Invalid Authorization header format"

    token = parts[1]

    # First, check if it's a raw API key (timing-safe)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if any(timing_safe_compare(token_hash, h) for h in api_key_hashes):
        return None

    # Second, check if it's an OAuth JWT token
    if oauth_store and jwt_secret:
        if oauth_store.verify_token(token, jwt_secret, api_key_hashes):
            return None

    return "Invalid Bearer token"
