"""OAuth 2.1 implementation for Claude.ai custom connectors."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
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
from ..security.xss import sanitize_for_log

logger = logging.getLogger(__name__)
PENDING_CLIENT_TTL = 60 * 60
OAUTH_SCOPE = "fetchaller:read"
_PKCE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass
class OAuthClient:
    """Registered OAuth client."""

    client_id: str
    redirect_uris: list[str]
    client_name: str
    created_at: float
    last_used_at: float
    paired_at: float | None = None


@dataclass
class AuthCode:
    """OAuth authorization code."""

    code: str
    client_id: str
    code_challenge: str
    redirect_uri: str
    api_key_hash: str
    scope: str
    resource: str
    expires_at: float


@dataclass
class RefreshToken:
    """Persisted metadata for a refresh token. The raw token is never stored."""

    token_hash: str
    client_id: str
    api_key_hash: str
    scope: str
    resource: str
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
    resource_server: str = "http://localhost:6000"

    _cleanup_task: asyncio.Task | None = field(default=None, repr=False)
    _state_path: Path | None = field(default=None, init=False, repr=False)
    _persistence_ready: bool = field(default=False, init=False, repr=False)
    _persistence_error: str | None = field(default=None, init=False, repr=False)
    _persistence_locked: bool = field(default=False, init=False, repr=False)

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
            self._persistence_error = str(exc)
            self.data_dir = None
            return

        self._state_path = data_dir / "oauth_clients.json"
        self._persistence_ready = True
        if self._state_path.exists():
            self._load()
        else:
            # Exercise the actual temp-write/fsync/replace/directory-fsync path
            # at startup, not merely mkstemp, so readiness reflects the commit
            # primitive OAuth mutations will use.
            self._persist()

    @property
    def persistence_ready(self) -> bool:
        """Whether configured durable state passed its latest write check."""

        return self._persistence_ready

    @property
    def persistence_error(self) -> str | None:
        return self._persistence_error

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
            resource_server=config.effective_server_url,
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
            discarded_entries = False
            for client_id, raw_client in raw_clients.items():
                try:
                    client_data = dict(raw_client)
                    client_data.setdefault("last_used_at", client_data["created_at"])
                    client_data.setdefault("paired_at", client_data["created_at"])
                    # Versions before public-client registration persisted a
                    # meaningless client_secret. Drop it during migration.
                    if "client_secret" in client_data:
                        discarded_entries = True
                    client_data.pop("client_secret", None)
                    client = OAuthClient(**client_data)
                    if client.client_id != client_id:
                        raise ValueError("client ID does not match its registry key")
                    if (
                        not isinstance(client.client_id, str)
                        or not client.client_id
                        or len(client.client_id) > 512
                        or not isinstance(client.redirect_uris, list)
                        or not client.redirect_uris
                        or not all(isinstance(uri, str) for uri in client.redirect_uris)
                        or not isinstance(client.client_name, str)
                        or not client.client_name
                        or len(client.client_name) > 200
                        or type(client.created_at) not in (int, float)
                        or not math.isfinite(client.created_at)
                        or type(client.last_used_at) not in (int, float)
                        or not math.isfinite(client.last_used_at)
                        or (
                            client.paired_at is not None
                            and (
                                type(client.paired_at) not in (int, float)
                                or not math.isfinite(client.paired_at)
                            )
                        )
                    ):
                        raise ValueError("client entry has invalid field types")
                except (TypeError, ValueError, KeyError) as exc:
                    discarded_entries = True
                    logger.warning(
                        "Discarding invalid persisted OAuth client %r (%s)",
                        sanitize_for_log(str(client_id), max_length=64),
                        type(exc).__name__,
                    )
                    continue
                clients[client_id] = client

            refresh_tokens: dict[str, RefreshToken] = {}
            for token_hash, raw_token in raw_refresh_tokens.items():
                try:
                    token_data = dict(raw_token)
                    if "scope" not in token_data or "resource" not in token_data:
                        discarded_entries = True
                    token_data.setdefault("scope", OAUTH_SCOPE)
                    token_data.setdefault("resource", self.resource_server)
                    token = RefreshToken(**token_data)
                    if token.token_hash != token_hash:
                        raise ValueError("refresh token hash does not match its registry key")
                    if (
                        not isinstance(token.token_hash, str)
                        or not _SHA256_HEX_RE.fullmatch(token.token_hash)
                        or not isinstance(token.client_id, str)
                        or not isinstance(token.api_key_hash, str)
                        or not _SHA256_HEX_RE.fullmatch(token.api_key_hash)
                        or token.scope != OAUTH_SCOPE
                        or token.resource != self.resource_server
                        or type(token.expires_at) not in (int, float)
                        or not math.isfinite(token.expires_at)
                        or token.client_id not in clients
                    ):
                        raise ValueError("refresh token entry has invalid field types or client")
                except (TypeError, ValueError, KeyError) as exc:
                    discarded_entries = True
                    logger.warning(
                        "Discarding invalid persisted OAuth refresh token %r (%s)",
                        sanitize_for_log(str(token_hash), max_length=64),
                        type(exc).__name__,
                    )
                    continue
                refresh_tokens[token_hash] = token

            self.clients = clients
            self.refresh_tokens = refresh_tokens
            if discarded_entries or self._cleanup(persist=False):
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
            self._persistence_ready = False
            self._persistence_locked = True
            self._persistence_error = f"state load failed ({type(exc).__name__})"
            logger.warning(
                "Could not load OAuth state from %s; refusing persistent mutations: %s",
                self._state_path,
                type(exc).__name__,
            )

    def _persist(self) -> bool:
        """Atomically persist clients and refresh-token hashes."""
        if self._state_path is None:
            return True
        if self._persistence_locked:
            return False

        state = {
            "clients": {client_id: asdict(client) for client_id, client in self.clients.items()},
            "refresh_tokens": {
                token_hash: asdict(token) for token_hash, token in self.refresh_tokens.items()
            },
        }
        temp_path: str | None = None
        replaced = False
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
            os.replace(temp_path, self._state_path)
            replaced = True
            temp_path = None
            directory_fd = os.open(self._state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._persistence_ready = True
            self._persistence_error = None
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not persist OAuth state to %s: %s", self._state_path, exc)
            self._persistence_ready = False
            self._persistence_error = str(exc)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            # Once replace succeeded, memory and the visible state file contain
            # the same commit. A directory-fsync error weakens crash durability
            # and readiness, but rolling memory back would create an immediate
            # split brain and hand the client an unusable state after restart.
            return replaced

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
        clients_before = dict(self.clients)
        refresh_tokens_before = dict(self.refresh_tokens)

        # Clean auth codes
        expired_codes = [code for code, data in self.auth_codes.items() if data.expires_at < now]
        for code in expired_codes:
            del self.auth_codes[code]

        # Clean old clients
        client_cutoff = now - self.client_ttl
        pending_cutoff = now - PENDING_CLIENT_TTL
        expired_clients = [
            cid for cid, data in self.clients.items()
            if (
                data.paired_at is None
                and data.created_at < pending_cutoff
            )
            or (
                data.paired_at is not None
                and data.last_used_at < client_cutoff
            )
        ]
        for cid in expired_clients:
            self._remove_client(cid)
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

        if persist and persistent_state_changed and not self._persist():
            # Cleanup is a durable-state transaction. If its commit fails,
            # preserve the last committed registry in memory so a transient
            # disk error cannot silently unpair clients until restart.
            self.clients = clients_before
            self.refresh_tokens = refresh_tokens_before
            return False
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
        client_name: str = "OAuth client",
    ) -> OAuthClient | None:
        """
        Register a new OAuth client (Dynamic Client Registration).

        At capacity, evict only the oldest still-pending registrations. Never
        evict a durable pairing through an unauthenticated endpoint.
        """
        if self.max_clients <= 0:
            return None

        clients_before = dict(self.clients)
        refresh_tokens_before = dict(self.refresh_tokens)
        self._cleanup(persist=False)
        if len(self.clients) >= self.max_clients:
            pending = sorted(
                [
                    client
                    for client in self.clients.values()
                    if client.paired_at is None
                ],
                key=lambda client: client.created_at,
            )
            eviction_count = len(self.clients) - self.max_clients + 1
            if len(pending) < eviction_count:
                self.clients = clients_before
                self.refresh_tokens = refresh_tokens_before
                return None
            for pending_client in pending[:eviction_count]:
                self._remove_client(pending_client.client_id)

        client_id = generate_id(16)
        while client_id in self.clients:
            client_id = generate_id(16)
        now = time.time()

        client = OAuthClient(
            client_id=client_id,
            redirect_uris=redirect_uris,
            client_name=client_name,
            created_at=now,
            last_used_at=now,
        )
        self.clients[client_id] = client
        if not self._persist():
            # Roll back the entire combined cleanup/eviction/registration
            # transaction, not only the new client.
            self.clients = clients_before
            self.refresh_tokens = refresh_tokens_before
            return None
        return client

    def get_client(self, client_id: str) -> OAuthClient | None:
        """Get a registered client without turning lookup traffic into disk writes."""
        return self.clients.get(client_id)

    def create_auth_code(
        self,
        client_id: str,
        code_challenge: str,
        redirect_uri: str,
        api_key: str,
        scope: str = OAUTH_SCOPE,
        resource: str | None = None,
    ) -> AuthCode | None:
        """
        Create an authorization code.

        Returns None if at max capacity.
        """
        if len(self.auth_codes) >= self.max_auth_codes:
            self._cleanup()
            if len(self.auth_codes) >= self.max_auth_codes:
                return None

        client = self.clients.get(client_id)
        if (
            client is None
            or redirect_uri not in client.redirect_uris
            or not _PKCE_CHALLENGE_RE.fullmatch(code_challenge)
            or scope != OAUTH_SCOPE
        ):
            return None
        resource = resource or self.resource_server
        if resource != self.resource_server:
            return None

        code = generate_id(32)
        while code in self.auth_codes:
            code = generate_id(32)
        auth_code = AuthCode(
            code=code,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            api_key_hash=hash_api_key(api_key),
            scope=scope,
            resource=resource,
            expires_at=time.time() + self.auth_code_ttl,
        )
        previous_paired_at = client.paired_at
        previous_last_used_at = client.last_used_at
        now = time.time()
        client.paired_at = client.paired_at or now
        client.last_used_at = now
        if not self._persist():
            client.paired_at = previous_paired_at
            client.last_used_at = previous_last_used_at
            return None
        self.auth_codes[code] = auth_code
        return auth_code

    def restore_auth_code(self, auth_code: AuthCode) -> None:
        """Restore a validated code after a pre-commit token persistence failure."""

        if auth_code.expires_at >= time.time():
            self.auth_codes.setdefault(auth_code.code, auth_code)

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
        scope: str = OAUTH_SCOPE,
        resource: str | None = None,
    ) -> str:
        """
        Create a JWT access token (stateless).

        Tokens are verified statelessly via JWT signature, so no
        in-memory tracking is needed.
        """
        resource = resource or self.resource_server
        if scope != OAUTH_SCOPE or resource != self.resource_server:
            raise ValueError("access token scope or resource is invalid")
        return create_access_token(
            client_id,
            api_key_hash,
            jwt_secret,
            self.access_token_ttl,
            audience=resource,
            scope=scope,
        )

    def create_refresh_token(
        self,
        client_id: str,
        api_key_hash: str,
        scope: str = OAUTH_SCOPE,
        resource: str | None = None,
    ) -> str | None:
        """Create and persist a refresh token, storing only its SHA-256 hash."""
        resource = resource or self.resource_server
        client = self.clients.get(client_id)
        if (
            client is None
            or client.paired_at is None
            or scope != OAUTH_SCOPE
            or resource != self.resource_server
        ):
            return None
        raw_token, refresh_token = self._new_refresh_token(
            client_id,
            api_key_hash,
            scope,
            resource,
        )
        self.refresh_tokens[refresh_token.token_hash] = refresh_token
        if not self._persist():
            self.refresh_tokens.pop(refresh_token.token_hash, None)
            return None
        return raw_token

    def rotate_refresh_token(
        self,
        raw_token: str,
        client_id: str,
        valid_api_key_hashes: set[str] | None = None,
        scope: str = OAUTH_SCOPE,
        resource: str | None = None,
    ) -> tuple[str, str, str, str] | None:
        """
        Redeem and rotate a refresh token.

        Returns the new raw refresh token, API key hash, scope, and resource.
        The old token is invalidated in the same persisted update that stores
        its replacement.
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        refresh_token = self.refresh_tokens.get(token_hash)
        if refresh_token is None:
            timing_safe_compare(token_hash, "0" * len(token_hash))
            return None
        if refresh_token.expires_at < time.time():
            del self.refresh_tokens[token_hash]
            if not self._persist():
                self.refresh_tokens[token_hash] = refresh_token
            return None
        if not timing_safe_compare(refresh_token.client_id, client_id):
            return None
        resource = resource or self.resource_server
        if (
            not timing_safe_compare(refresh_token.scope, scope)
            or not timing_safe_compare(refresh_token.resource, resource)
        ):
            return None
        if client_id not in self.clients:
            del self.refresh_tokens[token_hash]
            if not self._persist():
                self.refresh_tokens[token_hash] = refresh_token
            return None
        if valid_api_key_hashes is not None and not any(
            timing_safe_compare(refresh_token.api_key_hash, valid_hash)
            for valid_hash in valid_api_key_hashes
        ):
            del self.refresh_tokens[token_hash]
            if not self._persist():
                self.refresh_tokens[token_hash] = refresh_token
            return None

        new_raw_token, new_refresh_token = self._new_refresh_token(
            client_id,
            refresh_token.api_key_hash,
            refresh_token.scope,
            refresh_token.resource,
        )
        del self.refresh_tokens[token_hash]
        self.refresh_tokens[new_refresh_token.token_hash] = new_refresh_token
        previous_last_used = self.clients[client_id].last_used_at
        self.clients[client_id].last_used_at = time.time()
        if not self._persist():
            self.refresh_tokens.pop(new_refresh_token.token_hash, None)
            self.refresh_tokens[token_hash] = refresh_token
            self.clients[client_id].last_used_at = previous_last_used
            return None
        return (
            new_raw_token,
            refresh_token.api_key_hash,
            refresh_token.scope,
            refresh_token.resource,
        )

    def _new_refresh_token(
        self,
        client_id: str,
        api_key_hash: str,
        scope: str,
        resource: str,
    ) -> tuple[str, RefreshToken]:
        """Build a refresh token and its hash-only persisted representation."""
        raw_token = generate_id(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        while token_hash in self.refresh_tokens:
            raw_token = generate_id(48)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        return raw_token, RefreshToken(
            token_hash=token_hash,
            client_id=client_id,
            api_key_hash=api_key_hash,
            scope=scope,
            resource=resource,
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
        payload = verify_access_token(
            token,
            jwt_secret,
            audience=self.resource_server,
            required_scope=OAUTH_SCOPE,
        )
        if not payload:
            return False

        # Verify API key hash from JWT payload matches a valid key
        token_api_key_hash = payload.get("api_key_hash")
        if not token_api_key_hash:
            return False

        return any(timing_safe_compare(token_api_key_hash, h) for h in valid_api_key_hashes)
