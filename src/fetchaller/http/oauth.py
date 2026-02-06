"""OAuth 2.1 implementation for Claude.ai custom connectors."""

import asyncio
import time
from dataclasses import dataclass, field

from ..config import ACCESS_TOKEN_TTL, AUTH_CODE_TTL, CLIENT_TTL, Config
from ..security.crypto import (
    create_access_token,
    generate_id,
    hash_api_key,
    timing_safe_compare,
    verify_access_token,
    verify_pkce,
)


@dataclass
class OAuthClient:
    """Registered OAuth client."""

    client_id: str
    client_secret: str
    redirect_uris: list[str]
    client_name: str
    created_at: float


@dataclass
class AuthCode:
    """OAuth authorization code."""

    code: str
    client_id: str
    code_challenge: str
    redirect_uri: str
    api_key_hash: str
    expires_at: float


@dataclass
class OAuthStore:
    """
    In-memory OAuth store with bounded memory.

    Limits:
    - Max 1000 clients
    - Max 5000 auth codes
    """

    clients: dict[str, OAuthClient] = field(default_factory=dict)
    auth_codes: dict[str, AuthCode] = field(default_factory=dict)

    max_clients: int = 1000
    max_auth_codes: int = 5000

    # TTLs (defaults from config constants)
    auth_code_ttl: int = AUTH_CODE_TTL
    access_token_ttl: int = ACCESS_TOKEN_TTL
    client_ttl: int = CLIENT_TTL

    _cleanup_task: asyncio.Task | None = field(default=None, repr=False)

    @classmethod
    def from_config(cls, config: Config) -> "OAuthStore":
        return cls(
            max_clients=config.max_oauth_clients,
            max_auth_codes=config.max_auth_codes,
            auth_code_ttl=config.auth_code_ttl,
            access_token_ttl=config.access_token_ttl,
            client_ttl=config.client_ttl,
        )

    def start_cleanup(self) -> None:
        """Start periodic cleanup task."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup(self) -> None:
        """Stop periodic cleanup task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of expired entries."""
        while True:
            try:
                await asyncio.sleep(60)  # Run every minute
                self._cleanup()
            except asyncio.CancelledError:
                break

    def _cleanup(self) -> None:
        """Remove expired entries."""
        now = time.time()

        # Clean auth codes
        expired_codes = [code for code, data in self.auth_codes.items() if data.expires_at < now]
        for code in expired_codes:
            del self.auth_codes[code]

        # Clean old clients
        client_cutoff = now - self.client_ttl
        expired_clients = [cid for cid, data in self.clients.items() if data.created_at < client_cutoff]
        for cid in expired_clients:
            del self.clients[cid]

    def register_client(
        self,
        redirect_uris: list[str],
        client_name: str = "Claude",
    ) -> OAuthClient | None:
        """
        Register a new OAuth client (Dynamic Client Registration).

        Returns None if at max capacity.
        """
        if len(self.clients) >= self.max_clients:
            self._cleanup()
            if len(self.clients) >= self.max_clients:
                return None

        client_id = generate_id(16)
        client_secret = generate_id(32)

        client = OAuthClient(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=redirect_uris,
            client_name=client_name,
            created_at=time.time(),
        )
        self.clients[client_id] = client
        return client

    def get_client(self, client_id: str) -> OAuthClient | None:
        """Get a registered client by ID."""
        return self.clients.get(client_id)

    def create_auth_code(
        self,
        client_id: str,
        code_challenge: str,
        redirect_uri: str,
        api_key: str,
    ) -> AuthCode | None:
        """
        Create an authorization code.

        Returns None if at max capacity.
        """
        if len(self.auth_codes) >= self.max_auth_codes:
            self._cleanup()
            if len(self.auth_codes) >= self.max_auth_codes:
                return None

        code = generate_id(32)
        auth_code = AuthCode(
            code=code,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            api_key_hash=hash_api_key(api_key),
            expires_at=time.time() + self.auth_code_ttl,
        )
        self.auth_codes[code] = auth_code
        return auth_code

    def consume_auth_code(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> AuthCode | None:
        """
        Consume an authorization code (one-time use).

        Validates client_id, redirect_uri, and PKCE code_verifier.
        Returns the auth code data if valid, None otherwise.

        Note: Always consumes the code on any access attempt to prevent
        timing attacks that could reveal code existence.
        """
        # Always consume the code first to prevent timing oracle attacks
        auth_code = self.auth_codes.pop(code, None)
        if not auth_code:
            # Use timing_safe_compare to prevent timing oracle on code existence
            # Compare against a dummy value to maintain constant time
            timing_safe_compare(code, "x" * len(code) if code else "dummy")
            return None

        # Check expiration
        if auth_code.expires_at < time.time():
            return None

        # Verify client_id using timing-safe comparison
        if not timing_safe_compare(auth_code.client_id, client_id):
            return None

        # Verify redirect_uri using timing-safe comparison
        if not timing_safe_compare(auth_code.redirect_uri, redirect_uri):
            return None

        # Verify PKCE (already uses timing-safe operations internally)
        if not verify_pkce(code_verifier, auth_code.code_challenge):
            return None

        return auth_code

    def create_access_token_entry(
        self,
        client_id: str,
        api_key_hash: str,
        jwt_secret: bytes,
    ) -> str:
        """
        Create a JWT access token (stateless).

        Tokens are verified statelessly via JWT signature, so no
        in-memory tracking is needed.
        """
        return create_access_token(client_id, api_key_hash, jwt_secret, self.access_token_ttl)

    def verify_token(self, token: str, jwt_secret: bytes, valid_api_key_hashes: set[str]) -> bool:
        """
        Verify an access token (stateless).

        Checks:
        1. JWT signature is valid (proves we issued it)
        2. JWT is not expired (exp claim)
        3. API key hash in JWT matches one of the valid hashes

        This is stateless - tokens survive server restarts.
        Trade-off: Can't revoke individual tokens (but can rotate JWT_SECRET
        to invalidate all, or remove API keys to invalidate tokens for that key).

        Args:
            token: The JWT token
            jwt_secret: Secret used for JWT signing
            valid_api_key_hashes: Set of valid API key hashes
        """
        # Verify JWT signature and expiration (both checked by verify_access_token)
        payload = verify_access_token(token, jwt_secret)
        if not payload:
            return False

        # Verify API key hash from JWT payload matches a valid key
        token_api_key_hash = payload.get("api_key_hash")
        if not token_api_key_hash:
            return False

        return any(timing_safe_compare(token_api_key_hash, h) for h in valid_api_key_hashes)
