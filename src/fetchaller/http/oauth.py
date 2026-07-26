"""OAuth 2.1 implementation for Claude.ai custom connectors."""

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import ACCESS_TOKEN_TTL, AUTH_CODE_TTL, CLIENT_TTL, REFRESH_TOKEN_TTL, Config
from ..security.crypto import (
    create_access_token,
    generate_id,
    hash_api_key,
    timing_safe_compare,
    verify_access_token,
    verify_pkce,
)

logger = logging.getLogger(__name__)


@dataclass
class OAuthClient:
    """Registered OAuth client."""

    client_id: str
    client_secret: str
    redirect_uris: list[str]
    client_name: str
    created_at: float
    last_used_at: float


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
class RefreshToken:
    """Persisted metadata for a refresh token. The raw token is never stored."""

    token_hash: str
    client_id: str
    api_key_hash: str
    expires_at: float


@dataclass
class OAuthStore:
    """
    OAuth store with persistent clients and refresh tokens.

    Limits:
    - Max 1000 clients
    - Max 5000 auth codes

    The JSON persistence layer assumes a single worker and a single replica.
    Multi-worker or multi-replica deployments must replace it with a
    concurrency-safe shared store such as a database.
    """

    clients: dict[str, OAuthClient] = field(default_factory=dict)
    auth_codes: dict[str, AuthCode] = field(default_factory=dict)
    refresh_tokens: dict[str, RefreshToken] = field(default_factory=dict)

    max_clients: int = 1000
    max_auth_codes: int = 5000

    # TTLs (defaults from config constants)
    auth_code_ttl: int = AUTH_CODE_TTL
    access_token_ttl: int = ACCESS_TOKEN_TTL
    refresh_token_ttl: int = REFRESH_TOKEN_TTL
    client_ttl: int = CLIENT_TTL
    data_dir: str | None = None

    _cleanup_task: asyncio.Task | None = field(default=None, repr=False)
    _state_path: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Set up persistence and restore durable OAuth state."""
        if not self.data_dir:
            return

        data_dir = Path(self.data_dir)
        try:
            data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(data_dir, 0o700)
            probe_fd, probe_path = tempfile.mkstemp(prefix=".oauth-write-test-", dir=data_dir)
            os.close(probe_fd)
            os.unlink(probe_path)
        except OSError as exc:
            logger.warning("OAuth persistence disabled: cannot use data directory %s: %s", data_dir, exc)
            self.data_dir = None
            return

        self._state_path = data_dir / "oauth_clients.json"
        self._load()

    @classmethod
    def from_config(cls, config: Config) -> "OAuthStore":
        return cls(
            max_clients=config.max_oauth_clients,
            max_auth_codes=config.max_auth_codes,
            auth_code_ttl=config.auth_code_ttl,
            access_token_ttl=config.access_token_ttl,
            refresh_token_ttl=config.refresh_token_ttl,
            client_ttl=config.client_ttl,
            data_dir=config.data_dir,
        )

    def _load(self) -> None:
        """Load clients and refresh-token hashes, tolerating all file errors."""
        if self._state_path is None:
            return
        if not self._state_path.exists():
            logger.info("OAuth state file %s does not exist; starting empty", self._state_path)
            return

        try:
            os.chmod(self._state_path, 0o600)
            state = json.loads(self._state_path.read_text())
            raw_clients = state.get("clients", {})
            raw_refresh_tokens = state.get("refresh_tokens", {})
            if not isinstance(raw_clients, dict) or not isinstance(raw_refresh_tokens, dict):
                raise ValueError("clients and refresh_tokens must be objects")

            clients: dict[str, OAuthClient] = {}
            for client_id, raw_client in raw_clients.items():
                client_data = dict(raw_client)
                client_data.setdefault("last_used_at", client_data["created_at"])
                client = OAuthClient(**client_data)
                if client.client_id != client_id:
                    raise ValueError("client ID does not match its registry key")
                if (
                    not isinstance(client.client_id, str)
                    or not isinstance(client.client_secret, str)
                    or not isinstance(client.redirect_uris, list)
                    or not all(isinstance(uri, str) for uri in client.redirect_uris)
                    or not isinstance(client.client_name, str)
                    or not isinstance(client.created_at, (int, float))
                    or not isinstance(client.last_used_at, (int, float))
                ):
                    raise ValueError("client entry has invalid field types")
                clients[client_id] = client

            refresh_tokens: dict[str, RefreshToken] = {}
            for token_hash, raw_token in raw_refresh_tokens.items():
                token = RefreshToken(**raw_token)
                if token.token_hash != token_hash:
                    raise ValueError("refresh token hash does not match its registry key")
                if (
                    not isinstance(token.token_hash, str)
                    or not isinstance(token.client_id, str)
                    or not isinstance(token.api_key_hash, str)
                    or not isinstance(token.expires_at, (int, float))
                ):
                    raise ValueError("refresh token entry has invalid field types")
                refresh_tokens[token_hash] = token

            self.clients = clients
            self.refresh_tokens = refresh_tokens
            if self._cleanup(persist=False):
                self._persist()
            logger.info(
                "Loaded %d OAuth clients and %d refresh tokens from %s",
                len(self.clients),
                len(self.refresh_tokens),
                self._state_path,
            )
        except (AttributeError, OSError, TypeError, ValueError, KeyError) as exc:
            self.clients = {}
            self.refresh_tokens = {}
            logger.warning("Could not load OAuth state from %s; starting empty: %s", self._state_path, exc)

    def _persist(self) -> None:
        """Atomically persist clients and refresh-token hashes."""
        if self._state_path is None:
            return

        state = {
            "clients": {client_id: asdict(client) for client_id, client in self.clients.items()},
            "refresh_tokens": {
                token_hash: asdict(token) for token_hash, token in self.refresh_tokens.items()
            },
        }
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{self._state_path.name}.",
                dir=self._state_path.parent,
            )
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as temp_file:
                json.dump(state, temp_file, separators=(",", ":"), sort_keys=True)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.rename(temp_path, self._state_path)
            os.chmod(self._state_path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not persist OAuth state to %s: %s", self._state_path, exc)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

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

    def _cleanup(self, *, persist: bool = True) -> bool:
        """Remove expired entries and return whether persistent state changed."""
        now = time.time()
        persistent_state_changed = False

        # Clean auth codes
        expired_codes = [code for code, data in self.auth_codes.items() if data.expires_at < now]
        for code in expired_codes:
            del self.auth_codes[code]

        # Clean old clients
        client_cutoff = now - self.client_ttl
        expired_clients = [cid for cid, data in self.clients.items() if data.created_at < client_cutoff]
        for cid in expired_clients:
            self._remove_client(cid)
            persistent_state_changed = True

        # Enforce a lowered capacity when loading an older or hand-edited file.
        excess_clients = len(self.clients) - max(self.max_clients, 0)
        if excess_clients > 0:
            by_last_use = sorted(
                self.clients,
                key=lambda client_id: self.clients[client_id].last_used_at,
            )
            for client_id in by_last_use[:excess_clients]:
                self._remove_client(client_id)
                persistent_state_changed = True

        # Clean expired or orphaned refresh tokens
        expired_refresh_tokens = [
            token_hash
            for token_hash, data in self.refresh_tokens.items()
            if data.expires_at < now or data.client_id not in self.clients
        ]
        for token_hash in expired_refresh_tokens:
            del self.refresh_tokens[token_hash]
            persistent_state_changed = True

        if persist and persistent_state_changed:
            self._persist()
        return persistent_state_changed

    def _remove_client(self, client_id: str) -> None:
        """Remove a client and all refresh tokens bound to it."""
        self.clients.pop(client_id, None)
        for token_hash in [
            token_hash
            for token_hash, token in self.refresh_tokens.items()
            if token.client_id == client_id
        ]:
            del self.refresh_tokens[token_hash]

    def register_client(
        self,
        redirect_uris: list[str],
        client_name: str = "Claude",
    ) -> OAuthClient | None:
        """
        Register a new OAuth client (Dynamic Client Registration).

        At capacity, evicts the least-recently-used client.
        """
        if self.max_clients <= 0:
            return None

        self._cleanup(persist=False)
        if len(self.clients) >= self.max_clients:
            oldest_client_id = min(
                self.clients,
                key=lambda client_id: self.clients[client_id].last_used_at,
            )
            self._remove_client(oldest_client_id)

        client_id = generate_id(16)
        client_secret = generate_id(32)
        now = time.time()

        client = OAuthClient(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=redirect_uris,
            client_name=client_name,
            created_at=now,
            last_used_at=now,
        )
        self.clients[client_id] = client
        self._persist()
        return client

    def get_client(self, client_id: str) -> OAuthClient | None:
        """Get a registered client by ID."""
        client = self.clients.get(client_id)
        if client is not None:
            client.last_used_at = time.time()
            self._persist()
        return client

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

    def create_refresh_token(self, client_id: str, api_key_hash: str) -> str:
        """Create and persist a refresh token, storing only its SHA-256 hash."""
        raw_token, refresh_token = self._new_refresh_token(client_id, api_key_hash)
        self.refresh_tokens[refresh_token.token_hash] = refresh_token
        self._persist()
        return raw_token

    def rotate_refresh_token(
        self,
        raw_token: str,
        client_id: str,
        valid_api_key_hashes: set[str] | None = None,
    ) -> tuple[str, str] | None:
        """
        Redeem and rotate a refresh token.

        Returns the new raw refresh token and API key hash. The old token is
        invalidated in the same persisted update that stores its replacement.
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        refresh_token = self.refresh_tokens.get(token_hash)
        if refresh_token is None:
            timing_safe_compare(token_hash, "0" * len(token_hash))
            return None
        if refresh_token.expires_at < time.time():
            del self.refresh_tokens[token_hash]
            self._persist()
            return None
        if not timing_safe_compare(refresh_token.client_id, client_id):
            return None
        if client_id not in self.clients:
            del self.refresh_tokens[token_hash]
            self._persist()
            return None
        if valid_api_key_hashes is not None and not any(
            timing_safe_compare(refresh_token.api_key_hash, valid_hash)
            for valid_hash in valid_api_key_hashes
        ):
            del self.refresh_tokens[token_hash]
            self._persist()
            return None

        new_raw_token, new_refresh_token = self._new_refresh_token(
            client_id,
            refresh_token.api_key_hash,
        )
        del self.refresh_tokens[token_hash]
        self.refresh_tokens[new_refresh_token.token_hash] = new_refresh_token
        self.clients[client_id].last_used_at = time.time()
        self._persist()
        return new_raw_token, refresh_token.api_key_hash

    def _new_refresh_token(self, client_id: str, api_key_hash: str) -> tuple[str, RefreshToken]:
        """Build a refresh token and its hash-only persisted representation."""
        raw_token = generate_id(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        return raw_token, RefreshToken(
            token_hash=token_hash,
            client_id=client_id,
            api_key_hash=api_key_hash,
            expires_at=time.time() + self.refresh_token_ttl,
        )

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
