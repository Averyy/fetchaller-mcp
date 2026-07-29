"""Tests for OAuth functionality."""

import hashlib
import json
import os
import re
import time
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from fetchaller.config import Config
from fetchaller.http.app import create_app
from fetchaller.http.middleware import verify_bearer_auth
from fetchaller.http.oauth import OAUTH_SCOPE, PENDING_CLIENT_TTL, OAuthStore
from fetchaller.http.routes import create_router
from fetchaller.security.crypto import (
    create_access_token,
    generate_id,
    hash_api_key,
    timing_safe_compare,
    verify_access_token,
    verify_pkce,
)

_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_SERVER_URL = "https://fetchaller.example"
_REDIRECT_URI = "https://connector.example/oauth/callback"


@pytest.fixture(autouse=True)
def clear_oauth_route_state():
    """Keep global abuse-protection state isolated between route tests."""
    from fetchaller.http import routes

    routes._csrf_tokens.clear()
    routes._register_timestamps.clear()
    yield
    routes._csrf_tokens.clear()
    routes._register_timestamps.clear()


def _route_client(
    *,
    data_dir: str | None = None,
    api_key: str = "test-api-key",
) -> tuple[TestClient, OAuthStore, Config]:
    config = Config(
        api_key=api_key,
        data_dir=data_dir,
        server_url=_SERVER_URL,
    )
    store = OAuthStore.from_config(config)
    app = FastAPI()
    app.state.config = config
    app.include_router(
        create_router(
            config,
            {hash_api_key(api_key)} if api_key else set(),
            store,
            b"0123456789abcdef0123456789abcdef",
        )
    )
    return TestClient(app), store, config


def _begin_authorization(
    client_http: TestClient,
    client_id: str,
    *,
    state: str = "opaque-state",
    scope: str = OAUTH_SCOPE,
    resource: str = _SERVER_URL,
) -> tuple[dict[str, str], object]:
    response = client_http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "state": state,
            "code_challenge": _CHALLENGE,
            "code_challenge_method": "S256",
            "scope": scope,
            "resource": resource,
        },
    )
    assert response.status_code == 200
    csrf_match = re.search(
        r'name="csrf_token" value="([^"]+)"',
        response.text,
    )
    assert csrf_match
    return (
        {
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "state": state,
            "code_challenge": _CHALLENGE,
            "api_key": "test-api-key",
            "csrf_token": csrf_match.group(1),
            "scope": scope,
            "resource": resource,
        },
        response,
    )


class TestPKCE:
    """Test PKCE verification."""

    def test_valid_pkce(self):
        """Valid code_verifier matches challenge."""
        # code_verifier -> SHA256 -> base64url = code_challenge
        # Test vector: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        assert verify_pkce(verifier, challenge) is True

    def test_invalid_pkce(self):
        """Invalid code_verifier fails."""
        assert verify_pkce("wrong", "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM") is False

    def test_empty_verifier(self):
        """Empty verifier fails."""
        assert verify_pkce("", "some_challenge") is False


class TestCrypto:
    """Test cryptographic helpers."""

    def test_hash_api_key_consistent(self):
        """Same key produces same hash."""
        key = "test-api-key"
        assert hash_api_key(key) == hash_api_key(key)

    def test_hash_api_key_different(self):
        """Different keys produce different hashes."""
        assert hash_api_key("key1") != hash_api_key("key2")

    def test_timing_safe_compare_equal(self):
        """Equal strings return True."""
        assert timing_safe_compare("secret", "secret") is True

    def test_timing_safe_compare_different(self):
        """Different strings return False."""
        assert timing_safe_compare("secret", "wrong") is False

    def test_timing_safe_compare_different_length(self):
        """Different length strings return False."""
        assert timing_safe_compare("short", "longer") is False

    def test_generate_id_length(self):
        """Generated ID length scales with byte count (base64url encoding)."""
        id16 = generate_id(16)
        id32 = generate_id(32)
        # token_urlsafe(n) produces ceil(n * 4/3) chars (base64url, no padding)
        assert 21 <= len(id16) <= 22  # 16 bytes → 22 base64url chars
        assert 42 <= len(id32) <= 43  # 32 bytes → 43 base64url chars

    def test_generate_id_unique(self):
        """Generated IDs are unique."""
        ids = [generate_id(16) for _ in range(100)]
        assert len(set(ids)) == 100


class TestJWT:
    """Test JWT token creation and verification."""

    def test_create_and_verify_token(self):
        """Token can be created and verified."""
        secret = b"test-secret-key-exactly-32-bytes"  # 32 bytes
        token = create_access_token(
            "client123",
            "api_key_hash",
            secret,
            3600,
            audience="https://fetchaller.example",
            scope="fetchaller:read",
        )

        payload = verify_access_token(
            token,
            secret,
            audience="https://fetchaller.example",
            required_scope="fetchaller:read",
        )
        assert payload is not None
        assert payload["sub"] == "client123"  # JWT uses "sub" for subject/client_id
        assert payload["aud"] == "https://fetchaller.example"
        assert payload["scope"] == "fetchaller:read"

    def test_invalid_token(self):
        """Invalid token returns None."""
        secret = b"test-secret-key-exactly-32-bytes"  # 32 bytes
        assert (
            verify_access_token(
                "invalid.token.here",
                secret,
                audience="https://fetchaller.example",
                required_scope="fetchaller:read",
            )
            is None
        )

    def test_wrong_secret(self):
        """A token signed under secret A fails verification under secret B."""
        secret1 = b"secret-one-exactly-32-bytes-key!"  # 32 bytes
        secret2 = b"secret-two-exactly-32-bytes-key!"  # 32 bytes

        token = create_access_token(
            "client",
            "hash",
            secret1,
            3600,
            audience="https://fetchaller.example",
            scope="fetchaller:read",
        )
        assert (
            verify_access_token(
                token,
                secret2,
                audience="https://fetchaller.example",
                required_scope="fetchaller:read",
            )
            is None
        )

    def test_wrong_audience_or_scope_is_rejected(self):
        """Bearer JWTs are bound to the protected resource and read scope."""
        secret = b"test-secret-key-exactly-32-bytes"
        token = create_access_token(
            "client",
            "hash",
            secret,
            3600,
            audience=_SERVER_URL,
            scope=OAUTH_SCOPE,
        )

        assert (
            verify_access_token(
                token,
                secret,
                audience="https://other-resource.example",
                required_scope=OAUTH_SCOPE,
            )
            is None
        )
        assert (
            verify_access_token(
                token,
                secret,
                audience=_SERVER_URL,
                required_scope="fetchaller:write",
            )
            is None
        )

    def test_fixed_secret_survives_independent_app_construction(self, tmp_path):
        """A fixed JWT secret lets independently constructed apps verify tokens."""
        config = Config(
            api_key="test-api-key",
            jwt_secret="0123456789abcdef0123456789abcdef",
            data_dir=str(tmp_path),
        )
        app_one = create_app(config)
        token = app_one.state.oauth_store.create_access_token_entry(
            "client",
            hash_api_key("test-api-key"),
            app_one.state.jwt_secret,
        )

        app_two = create_app(config)
        assert app_two.state.oauth_store.verify_token(
            token,
            app_two.state.jwt_secret,
            app_two.state.api_key_hashes,
        )

    def test_http_app_rejects_ephemeral_secret_with_api_key(self):
        """HTTP mode fails closed unless ephemeral JWTs are explicitly allowed."""
        with pytest.raises(RuntimeError, match="JWT_SECRET must be set"):
            create_app(Config(api_key="test-api-key", data_dir=None))

        app = create_app(
            Config(
                api_key="test-api-key",
                allow_ephemeral_jwt=True,
                data_dir=None,
            )
        )
        assert app.state.jwt_secret_ephemeral is True


class TestOAuthStore:
    """Test OAuth store operations."""

    def test_register_and_retrieve_client(self):
        """Registered client is stored and retrievable by ID; unknown IDs return None."""
        store = OAuthStore()
        client = store.register_client(["https://example.com/cb"], "Test")

        assert isinstance(client.client_id, str) and len(client.client_id) > 10
        assert not hasattr(client, "client_secret")
        assert client.redirect_uris == ["https://example.com/cb"]

        # Retrieve by ID
        retrieved = store.get_client(client.client_id)
        assert retrieved.client_id == client.client_id

        # Unknown ID
        assert store.get_client("nonexistent-id") is None

    def test_create_auth_code_is_consumable(self):
        """Created auth code stores client_id and can be consumed with correct PKCE verifier."""
        store = OAuthStore()
        client = store.register_client(["https://x.com/cb"])
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code(
            client.client_id,
            challenge,
            "https://x.com/cb",
            "api_key",
        )
        assert isinstance(auth_code.code, str) and len(auth_code.code) > 10
        assert auth_code.client_id == client.client_id

        # Code is consumable with correct verifier
        result = store.consume_auth_code(
            auth_code.code,
            client.client_id,
            "https://x.com/cb",
            verifier,
        )
        assert result is not None

    def test_consume_auth_code_once(self):
        """Auth code can only be consumed once."""
        store = OAuthStore()
        client = store.register_client(["https://x.com/cb"])
        # Use a known verifier/challenge pair
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code(
            client.client_id,
            challenge,
            "https://x.com/cb",
            "api_key",
        )

        # First consume succeeds
        result = store.consume_auth_code(
            auth_code.code,
            client.client_id,
            "https://x.com/cb",
            verifier,
        )
        assert result is not None

        # Second consume fails (already consumed)
        result2 = store.consume_auth_code(
            auth_code.code,
            client.client_id,
            "https://x.com/cb",
            verifier,
        )
        assert result2 is None

    def test_consume_wrong_client_id(self):
        """Wrong client_id fails."""
        store = OAuthStore()
        client = store.register_client(["https://x.com/cb"])
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code(
            client.client_id,
            challenge,
            "https://x.com/cb",
            "api_key",
        )
        result = store.consume_auth_code(auth_code.code, "wrong_client", "https://x.com/cb", verifier)

        assert result is None

    def test_consume_wrong_redirect_uri(self):
        """Wrong redirect_uri fails."""
        store = OAuthStore()
        client = store.register_client(["https://x.com/cb"])
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code(
            client.client_id,
            challenge,
            "https://x.com/cb",
            "api_key",
        )
        result = store.consume_auth_code(
            auth_code.code,
            client.client_id,
            "https://wrong.com/cb",
            verifier,
        )

        assert result is None

    def test_max_clients_limit(self):
        """Pending registrations can be bounded without evicting pairings."""
        store = OAuthStore(max_clients=2)

        oldest = store.register_client(["https://one.com/cb"])
        newest = store.register_client(["https://two.com/cb"])
        result = store.register_client(["https://three.com/cb"])

        assert result is not None
        assert len(store.clients) == 2
        assert oldest.client_id not in store.clients
        assert newest.client_id in store.clients
        assert result.client_id in store.clients

    def test_paired_clients_are_never_evicted_by_registration(self):
        """An unauthenticated DCR flood cannot destroy a durable pairing."""
        store = OAuthStore(max_clients=2)
        paired = store.register_client(["https://paired.example/cb"])
        assert store.create_auth_code(
            paired.client_id,
            _CHALLENGE,
            "https://paired.example/cb",
            "test-api-key",
        )
        pending = store.register_client(["https://pending.example/cb"])

        replacement = store.register_client(["https://replacement.example/cb"])

        assert replacement is not None
        assert paired.client_id in store.clients
        assert pending.client_id not in store.clients
        assert replacement.client_id in store.clients

    def test_capacity_reduction_evicts_only_enough_pending_clients(self):
        """Lowering max_clients bounds old pending state without touching pairings."""
        store = OAuthStore(max_clients=5)
        clients = [
            store.register_client([f"https://pending{index}.example/cb"])
            for index in range(5)
        ]
        store.max_clients = 2

        replacement = store.register_client(["https://replacement.example/cb"])

        assert replacement is not None
        assert len(store.clients) == 2
        assert clients[-1].client_id in store.clients
        assert replacement.client_id in store.clients

    def test_capacity_reduction_rejects_when_pairings_cannot_be_preserved(self):
        """A lowered cap never authorizes eviction of existing pairings."""
        store = OAuthStore(max_clients=3)
        clients = [
            store.register_client([f"https://paired{index}.example/cb"])
            for index in range(3)
        ]
        for client in clients:
            assert store.create_auth_code(
                client.client_id,
                _CHALLENGE,
                client.redirect_uris[0],
                "test-api-key",
            )
        store.max_clients = 1

        assert store.register_client(["https://replacement.example/cb"]) is None
        assert set(store.clients) == {client.client_id for client in clients}

    def test_pending_and_paired_clients_use_distinct_ttls(self, monkeypatch):
        """Pending clients expire quickly while used pairings honor client_ttl."""
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now)
        store = OAuthStore(client_ttl=24 * 60 * 60)
        pending = store.register_client(["https://pending.example/cb"])
        paired = store.register_client(["https://paired.example/cb"])
        assert store.create_auth_code(
            paired.client_id,
            _CHALLENGE,
            "https://paired.example/cb",
            "test-api-key",
        )

        monkeypatch.setattr(
            time,
            "time",
            lambda: now + PENDING_CLIENT_TTL + 1,
        )
        store._cleanup()

        assert pending.client_id not in store.clients
        assert paired.client_id in store.clients

    def test_failed_pending_eviction_transaction_restores_original_state(
        self,
        monkeypatch,
    ):
        """A failed commit restores every pending client removed for capacity."""
        store = OAuthStore(max_clients=3)
        original = [
            store.register_client([f"https://pending{index}.example/cb"])
            for index in range(3)
        ]
        store.max_clients = 1
        monkeypatch.setattr(store, "_persist", lambda: False)

        assert store.register_client(["https://replacement.example/cb"]) is None
        assert set(store.clients) == {client.client_id for client in original}

    def test_background_cleanup_failure_restores_pairing_and_refresh_token(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Cleanup never applies an uncommitted durable-state deletion in memory."""
        from fetchaller.http import oauth

        store = OAuthStore(data_dir=str(tmp_path), client_ttl=60)
        client = store.register_client([_REDIRECT_URI])
        assert store.create_auth_code(
            client.client_id,
            _CHALLENGE,
            _REDIRECT_URI,
            "test-api-key",
        )
        raw_refresh = store.create_refresh_token(
            client.client_id,
            hash_api_key("test-api-key"),
        )
        token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        client.last_used_at = time.time() - 61
        assert store._persist()

        def fail_replace(source, destination):
            raise OSError("injected cleanup replace failure")

        monkeypatch.setattr(oauth.os, "replace", fail_replace)
        changed = store._cleanup()

        assert changed is False
        assert client.client_id in store.clients
        assert token_hash in store.refresh_tokens
        assert store.persistence_ready is False

    def test_registration_commit_failure_rolls_back_cleanup_deletions(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A combined cleanup/registration failure restores the full snapshot."""
        from fetchaller.http import oauth

        store = OAuthStore(data_dir=str(tmp_path), max_clients=2)
        paired = store.register_client([_REDIRECT_URI])
        assert store.create_auth_code(
            paired.client_id,
            _CHALLENGE,
            _REDIRECT_URI,
            "test-api-key",
        )
        raw_refresh = store.create_refresh_token(
            paired.client_id,
            hash_api_key("test-api-key"),
        )
        pending = store.register_client(["https://pending.example/callback"])
        pending.created_at = time.time() - PENDING_CLIENT_TTL - 1
        assert store._persist()
        original_clients = set(store.clients)
        original_tokens = set(store.refresh_tokens)

        def fail_replace(source, destination):
            raise OSError("injected registration replace failure")

        monkeypatch.setattr(oauth.os, "replace", fail_replace)
        result = store.register_client(["https://replacement.example/callback"])

        assert result is None
        assert set(store.clients) == original_clients
        assert set(store.refresh_tokens) == original_tokens
        assert hashlib.sha256(raw_refresh.encode()).hexdigest() in store.refresh_tokens
        assert pending.client_id in store.clients
        assert store.persistence_ready is False

    def test_failed_pairing_commit_rolls_back_client_and_auth_code(
        self,
        monkeypatch,
    ):
        """Authorization is not issued unless paired-client state commits."""
        store = OAuthStore()
        client = store.register_client([_REDIRECT_URI])
        monkeypatch.setattr(store, "_persist", lambda: False)

        auth_code = store.create_auth_code(
            client.client_id,
            _CHALLENGE,
            _REDIRECT_URI,
            "test-api-key",
        )

        assert auth_code is None
        assert client.paired_at is None
        assert store.auth_codes == {}

    def test_unpaired_client_cannot_receive_refresh_token(self):
        """Refresh credentials are only minted after successful authorization."""
        store = OAuthStore()
        client = store.register_client([_REDIRECT_URI])

        assert (
            store.create_refresh_token(
                client.client_id,
                hash_api_key("test-api-key"),
            )
            is None
        )

    def test_client_registry_round_trips_with_secure_permissions(self, tmp_path):
        """Registered clients survive store reconstruction on disk."""
        store = OAuthStore(data_dir=str(tmp_path))
        client = store.register_client(["https://example.com/callback"], "Persistent")

        restored = OAuthStore(data_dir=str(tmp_path))
        loaded = restored.get_client(client.client_id)

        assert loaded is not None
        assert loaded.redirect_uris == client.redirect_uris
        assert "client_secret" not in (tmp_path / "oauth_clients.json").read_text()
        assert os.stat(tmp_path).st_mode & 0o777 == 0o700
        assert os.stat(tmp_path / "oauth_clients.json").st_mode & 0o777 == 0o600

    def test_legacy_client_secret_is_removed_during_migration(self, tmp_path):
        """Old confidential-client fields are neither loaded nor left on disk."""
        store = OAuthStore(data_dir=str(tmp_path))
        client = store.register_client([_REDIRECT_URI], "Legacy")
        state_path = tmp_path / "oauth_clients.json"
        state = json.loads(state_path.read_text())
        state["clients"][client.client_id]["client_secret"] = "legacy-secret"
        state_path.write_text(json.dumps(state))

        restored = OAuthStore(data_dir=str(tmp_path))

        assert restored.get_client(client.client_id) is not None
        assert not hasattr(restored.get_client(client.client_id), "client_secret")
        assert "legacy-secret" not in state_path.read_text()
        assert "client_secret" not in state_path.read_text()

    def test_registration_replace_failure_preserves_last_commit(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A pre-commit filesystem error changes neither memory nor durable state."""
        from fetchaller.http import oauth

        store = OAuthStore(data_dir=str(tmp_path))
        first = store.register_client(["https://first.example/callback"])
        before = (tmp_path / "oauth_clients.json").read_bytes()

        def fail_replace(source, destination):
            raise OSError("injected replace failure")

        monkeypatch.setattr(oauth.os, "replace", fail_replace)
        second = store.register_client(["https://second.example/callback"])

        assert second is None
        assert set(store.clients) == {first.client_id}
        assert (tmp_path / "oauth_clients.json").read_bytes() == before
        assert store.persistence_ready is False

    def test_directory_fsync_failure_keeps_visible_commit_consistent(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A post-replace durability error never rolls memory behind the file."""
        from fetchaller.http import oauth

        store = OAuthStore(data_dir=str(tmp_path))
        original_fsync = oauth.os.fsync
        calls = 0

        def fail_directory_fsync(file_descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected directory fsync failure")
            return original_fsync(file_descriptor)

        monkeypatch.setattr(oauth.os, "fsync", fail_directory_fsync)
        client = store.register_client(["https://committed.example/callback"])

        assert client is not None
        assert client.client_id in store.clients
        assert store.persistence_ready is False
        state = json.loads((tmp_path / "oauth_clients.json").read_text())
        assert client.client_id in state["clients"]

    def test_refresh_rotation_failure_restores_old_credential(
        self,
        monkeypatch,
    ):
        """Rotation is atomic: a failed commit leaves the old token redeemable."""
        api_key_hash = hash_api_key("test-api-key")
        store = OAuthStore()
        client = store.register_client([_REDIRECT_URI])
        assert store.create_auth_code(
            client.client_id,
            _CHALLENGE,
            _REDIRECT_URI,
            "test-api-key",
        )
        raw_token = store.create_refresh_token(client.client_id, api_key_hash)
        original_persist = store._persist
        monkeypatch.setattr(store, "_persist", lambda: False)

        assert (
            store.rotate_refresh_token(
                raw_token,
                client.client_id,
                {api_key_hash},
            )
            is None
        )
        assert hashlib.sha256(raw_token.encode()).hexdigest() in store.refresh_tokens

        monkeypatch.setattr(store, "_persist", original_persist)
        assert store.rotate_refresh_token(
            raw_token,
            client.client_id,
            {api_key_hash},
        )

    def test_expired_clients_are_dropped_on_load(self, tmp_path):
        """Loading persisted state cannot resurrect expired clients."""
        store = OAuthStore(data_dir=str(tmp_path), client_ttl=60)
        client = store.register_client(["https://example.com/callback"])
        client.created_at = time.time() - 3601
        client.last_used_at = time.time() - 3601
        store._persist()

        restored = OAuthStore(data_dir=str(tmp_path), client_ttl=60)

        assert restored.clients == {}

    def test_corrupt_registry_starts_empty(self, tmp_path, caplog):
        """A corrupt state file fails closed instead of being overwritten."""
        state_path = tmp_path / "oauth_clients.json"
        state_path.write_text("{not-json")

        store = OAuthStore(data_dir=str(tmp_path))

        assert store.clients == {}
        assert store.refresh_tokens == {}
        assert store.persistence_ready is False
        assert store.register_client(["https://example.com/callback"]) is None
        assert state_path.read_text() == "{not-json"
        assert "refusing persistent mutations" in caplog.text

    def test_one_invalid_persisted_record_does_not_erase_valid_oauth_state(
        self,
        tmp_path,
        caplog,
    ):
        """A malformed record is discarded without destroying other pairings."""

        api_key_hash = hash_api_key("test-api-key")
        store = OAuthStore(data_dir=str(tmp_path))
        valid_client = store.register_client(
            ["https://example.com/callback"],
            "Valid client",
        )
        assert store.create_auth_code(
            valid_client.client_id,
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "https://example.com/callback",
            "test-api-key",
        )
        raw_refresh_token = store.create_refresh_token(
            valid_client.client_id,
            api_key_hash,
        )

        state_path = tmp_path / "oauth_clients.json"
        state = json.loads(state_path.read_text())
        state["clients"]["poison"] = {
            "client_id": "poison",
            "client_secret": "secret",
            "redirect_uris": ["https://example.com/callback"],
            "client_name": {"not": "a string"},
            "created_at": time.time(),
            "last_used_at": time.time(),
        }
        state["refresh_tokens"]["bad-token"] = {
            "token_hash": "bad-token",
            "client_id": "poison",
            "api_key_hash": api_key_hash,
            "expires_at": time.time() + 3600,
        }
        state_path.write_text(json.dumps(state))

        restored = OAuthStore(data_dir=str(tmp_path))

        assert set(restored.clients) == {valid_client.client_id}
        assert "poison" not in restored.clients
        assert "bad-token" not in restored.refresh_tokens
        assert (
            restored.rotate_refresh_token(
                raw_refresh_token,
                valid_client.client_id,
                {api_key_hash},
            )
            is not None
        )
        assert "Discarding invalid persisted OAuth client" in caplog.text

    def test_registration_rejects_metadata_that_could_poison_persistence(
        self,
        tmp_path,
    ):
        """DCR accepts only durable metadata types and supported auth mode."""

        from fetchaller.http import routes

        routes._register_timestamps.clear()
        config = Config(data_dir=str(tmp_path))
        app = FastAPI()
        app.include_router(
            create_router(
                config,
                set(),
                OAuthStore.from_config(config),
                b"test-secret",
            )
        )
        client = TestClient(app)

        not_object = client.post("/register", json=["not", "an", "object"])
        bad_name = client.post(
            "/register",
            json={
                "redirect_uris": ["https://example.com/callback"],
                "client_name": {"poison": True},
            },
        )
        bad_auth_method = client.post(
            "/register",
            json={
                "redirect_uris": ["https://example.com/callback"],
                "token_endpoint_auth_method": "client_secret_basic",
            },
        )
        valid = client.post(
            "/register",
            json={
                "redirect_uris": ["https://example.com/callback"],
                "client_name": "  Durable client  ",
                "token_endpoint_auth_method": "none",
            },
        )

        assert not_object.status_code == 400
        assert bad_name.status_code == 400
        assert bad_name.json()["error"] == "invalid_client_metadata"
        assert bad_auth_method.status_code == 400
        assert valid.status_code == 201
        assert valid.json()["client_name"] == "Durable client"
        restored = OAuthStore(data_dir=str(tmp_path))
        assert len(restored.clients) == 1
        assert next(iter(restored.clients.values())).client_name == "Durable client"
        routes._register_timestamps.clear()

    def test_unusable_data_dir_disables_persistence(self, tmp_path, caplog):
        """An unusable default-style path never prevents store construction."""
        not_a_directory = tmp_path / "not-a-directory"
        not_a_directory.write_text("occupied")

        store = OAuthStore(data_dir=str(not_a_directory))

        assert store.data_dir is None
        assert store.register_client(["https://example.com/callback"]) is not None
        assert "OAuth persistence disabled" in caplog.text

    def test_refresh_tokens_survive_restart_as_hashes(self, tmp_path):
        """Only refresh-token hashes persist, and the token works after reload."""
        api_key_hash = hash_api_key("test-api-key")
        store = OAuthStore(data_dir=str(tmp_path))
        client = store.register_client(["https://example.com/callback"])
        assert store.create_auth_code(
            client.client_id,
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "https://example.com/callback",
            "test-api-key",
        )
        raw_token = store.create_refresh_token(client.client_id, api_key_hash)

        persisted = (tmp_path / "oauth_clients.json").read_text()
        assert raw_token not in persisted
        assert hashlib.sha256(raw_token.encode()).hexdigest() in persisted

        restored = OAuthStore(data_dir=str(tmp_path))
        rotated = restored.rotate_refresh_token(
            raw_token,
            client.client_id,
            {api_key_hash},
        )
        assert rotated is not None

        new_raw_token, restored_api_key_hash, scope, resource = rotated
        assert restored_api_key_hash == api_key_hash
        assert scope == "fetchaller:read"
        assert resource == "http://localhost:6000"
        assert restored.rotate_refresh_token(raw_token, client.client_id, {api_key_hash}) is None
        assert restored.rotate_refresh_token(new_raw_token, client.client_id, {api_key_hash})

    def test_refresh_token_endpoint_rotates_and_issues_working_access_token(self, tmp_path):
        """The refresh grant rejects reuse and returns a valid access token."""
        config = Config(
            api_key="test-api-key",
            jwt_secret="0123456789abcdef0123456789abcdef",
            data_dir=str(tmp_path),
        )
        api_key_hash = hash_api_key("test-api-key")
        api_key_hashes = {api_key_hash}
        jwt_secret = b"0123456789abcdef0123456789abcdef"
        store = OAuthStore.from_config(config)
        client = store.register_client(["https://example.com/callback"])
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        auth_code = store.create_auth_code(
            client.client_id,
            challenge,
            "https://example.com/callback",
            "test-api-key",
        )

        app = FastAPI()
        app.include_router(create_router(config, api_key_hashes, store, jwt_secret))
        client_http = TestClient(app)
        initial_response = client_http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code.code,
                "client_id": client.client_id,
                "redirect_uri": "https://example.com/callback",
                "code_verifier": verifier,
            },
        )
        assert initial_response.status_code == 200
        old_refresh_token = initial_response.json()["refresh_token"]

        refresh_response = client_http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh_token,
                "client_id": client.client_id,
            },
        )
        assert refresh_response.status_code == 200
        refreshed = refresh_response.json()
        assert refreshed["refresh_token"] != old_refresh_token
        assert store.verify_token(refreshed["access_token"], jwt_secret, api_key_hashes)

        reused_response = client_http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh_token,
                "client_id": client.client_id,
            },
        )
        assert reused_response.status_code == 400
        assert reused_response.json()["error"] == "invalid_grant"

    def test_raw_api_key_auth_is_unchanged(self, tmp_path):
        """Raw API-key bearer auth remains independent of OAuth state."""
        api_key = "raw-api-key"
        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", f"Bearer {api_key}".encode())],
            }
        )
        store = OAuthStore(data_dir=str(tmp_path))
        store.register_client(["https://example.com/callback"])

        assert verify_bearer_auth(
            request,
            {hash_api_key(api_key)},
            store,
            b"unrelated-jwt-secret",
        ) is None

    def test_discovery_and_health_report_refresh_and_ephemeral_state(self):
        """Discovery advertises refresh, while health exposes only a boolean."""
        config = Config(data_dir=None)
        app = FastAPI()
        app.include_router(
            create_router(
                config,
                set(),
                OAuthStore(),
                b"ephemeral-secret",
                jwt_secret_ephemeral=True,
            )
        )
        client = TestClient(app)

        metadata = client.get("/.well-known/oauth-authorization-server").json()
        health_response = client.get("/health")
        health = health_response.json()
        assert "refresh_token" in metadata["grant_types_supported"]
        assert health_response.status_code == 503
        assert health["status"] == "unhealthy"
        assert health["readiness"]["authentication"] is False
        assert health["jwt_secret_ephemeral"] is True
        assert set(health) == {
            "status",
            "service",
            "timestamp",
            "jwt_secret_ephemeral",
            "readiness",
        }


class TestOAuthRoutes:
    """End-to-end OAuth route validation and security properties."""

    def test_health_is_cheap_and_requires_usable_authentication(self, monkeypatch):
        """Health reads cached readiness and never performs a disk mutation."""
        client_http, store, _ = _route_client()

        def unexpected_persist():
            raise AssertionError("health must not touch persistence")

        monkeypatch.setattr(store, "_persist", unexpected_persist)
        response = client_http.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert response.json()["readiness"]["authentication"] is True

    def test_discovery_challenge_resource_and_audience_share_exact_origin(self):
        """Every externally visible OAuth identifier uses one canonical origin."""
        client_http, _, _ = _route_client()

        metadata = client_http.get(
            "/.well-known/oauth-authorization-server"
        ).json()
        protected = client_http.get(
            "/.well-known/oauth-protected-resource"
        ).json()
        unauthorized = client_http.post("/mcp", json={"jsonrpc": "2.0"})

        assert metadata["issuer"] == _SERVER_URL
        assert metadata["authorization_endpoint"] == f"{_SERVER_URL}/authorize"
        assert metadata["token_endpoint"] == f"{_SERVER_URL}/token"
        assert metadata["registration_endpoint"] == f"{_SERVER_URL}/register"
        assert protected["resource"] == _SERVER_URL
        assert protected["authorization_servers"] == [_SERVER_URL]
        assert unauthorized.status_code == 401
        assert (
            f'resource_metadata="{_SERVER_URL}/.well-known/'
            'oauth-protected-resource"'
            in unauthorized.headers["www-authenticate"]
        )

    @pytest.mark.parametrize("path", ["/register", "/authorize", "/token"])
    def test_body_limit_errors_on_oauth_endpoints_are_not_cacheable(
        self,
        path,
    ):
        """Middleware-generated OAuth errors carry the same no-store policy."""
        from fetchaller.http.middleware import RequestBodyLimitMiddleware

        config = Config(
            api_key="test-api-key",
            server_url=_SERVER_URL,
            data_dir=None,
        )
        store = OAuthStore.from_config(config)
        app = FastAPI()
        app.state.config = config
        app.add_middleware(RequestBodyLimitMiddleware, max_bytes=32)
        app.include_router(
            create_router(
                config,
                {hash_api_key("test-api-key")},
                store,
                b"0123456789abcdef0123456789abcdef",
            )
        )
        client_http = TestClient(app)

        response = client_http.post(path, content=b"x" * 33)

        assert response.status_code == 413
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "https://example.com/call back",
            "https://example.com\\@evil.example/callback",
            "https://example.com/callback\x7f",
            "https://example.com/callback#",
            "https://example.com/callback#fragment",
            "https://user@example.com/callback",
            "https://example.com:0/callback",
            "https://example.com:/callback",
            "https://example.com:65536/callback",
            "https://bad_host.example/callback",
            "https://éxample.com/callback",
            "https://example.com/%zz",
            "http://example.com/callback",
            "http://127.0.0.2/callback",
            "http://localhost.evil.example/callback",
            "javascript://example.com/callback",
        ],
    )
    def test_registration_rejects_malformed_or_unsafe_redirects(
        self,
        redirect_uri,
    ):
        """DCR accepts only strict HTTPS or exact-loopback HTTP URIs."""
        client_http, _, _ = _route_client()

        response = client_http.post(
            "/register",
            json={"redirect_uris": [redirect_uri]},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_redirect_uri"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "https://example.com/callback",
            "https://example.com:8443/callback?source=oauth",
            "https://xn--xample-9ua.example/callback",
            "http://localhost/callback",
            "http://localhost:49152/callback",
            "http://127.0.0.1/callback",
            "http://[::1]:49152/callback",
        ],
    )
    def test_registration_accepts_valid_redirects(self, redirect_uri):
        client_http, _, _ = _route_client()

        response = client_http.post(
            "/register",
            json={"redirect_uris": [redirect_uri]},
        )

        assert response.status_code == 201
        assert response.json()["redirect_uris"] == [redirect_uri]

    def test_registration_rejects_duplicates_and_control_characters(self):
        client_http, _, _ = _route_client()

        duplicates = client_http.post(
            "/register",
            json={"redirect_uris": [_REDIRECT_URI, _REDIRECT_URI]},
        )
        bad_name = client_http.post(
            "/register",
            json={
                "redirect_uris": [_REDIRECT_URI],
                "client_name": "Injected\nLog",
            },
        )

        assert duplicates.status_code == 400
        assert duplicates.json()["error"] == "invalid_client_metadata"
        assert bad_name.status_code == 400
        assert bad_name.json()["error"] == "invalid_client_metadata"

    def test_public_registration_has_no_secret_and_is_durable(self, tmp_path):
        """DCR registers a public PKCE client without creating fake secrets."""
        client_http, _, _ = _route_client(data_dir=str(tmp_path))

        response = client_http.post(
            "/register",
            json={
                "redirect_uris": [_REDIRECT_URI],
                "client_name": "Public Connector",
                "token_endpoint_auth_method": "none",
            },
        )

        assert response.status_code == 201
        assert "client_secret" not in response.json()
        assert response.json()["token_endpoint_auth_method"] == "none"
        assert response.headers["cache-control"] == "no-store"
        persisted = (tmp_path / "oauth_clients.json").read_text()
        assert "client_secret" not in persisted

    def test_authorization_page_is_client_specific_escaped_and_hardened(self):
        client_http, store, _ = _route_client()
        client = store.register_client(
            [_REDIRECT_URI],
            "Connector <script>alert(1)</script>",
        )

        _, response = _begin_authorization(client_http, client.client_id)

        assert "authorize Connector &lt;script&gt;alert(1)&lt;/script&gt;" in response.text
        assert "<script>alert(1)</script>" not in response.text
        assert "authorize Claude" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        nonce_match = re.search(r"style-src 'nonce-([^']+)'", csp)
        assert nonce_match
        nonce = nonce_match.group(1)
        assert f'<style nonce="{nonce}">' in response.text
        assert f'<script nonce="{nonce}">' in response.text
        assert f'name="scope" value="{OAUTH_SCOPE}"' in response.text
        assert f'name="resource" value="{_SERVER_URL}"' in response.text

    def test_complete_code_and_refresh_flow_binds_scope_and_resource(
        self,
        tmp_path,
    ):
        """Scope/resource survive code exchange, JWT issuance, and rotation."""
        client_http, store, config = _route_client(data_dir=str(tmp_path))
        oauth_client = store.register_client([_REDIRECT_URI], "Connector")
        form, _ = _begin_authorization(client_http, oauth_client.client_id)

        authorize_response = client_http.post("/authorize", data=form)

        assert authorize_response.status_code == 200
        assert authorize_response.headers["cache-control"] == "no-store"
        assert authorize_response.headers["x-frame-options"] == "DENY"
        assert "Redirecting to Connector" in authorize_response.text
        auth_code = next(iter(store.auth_codes.values()))
        assert auth_code.scope == OAUTH_SCOPE
        assert auth_code.resource == _SERVER_URL

        token_response = client_http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code.code,
                "client_id": oauth_client.client_id,
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": _VERIFIER,
                "scope": OAUTH_SCOPE,
                "resource": _SERVER_URL,
            },
        )

        assert token_response.status_code == 200
        assert token_response.headers["cache-control"] == "no-store"
        assert token_response.headers["pragma"] == "no-cache"
        tokens = token_response.json()
        payload = verify_access_token(
            tokens["access_token"],
            b"0123456789abcdef0123456789abcdef",
            audience=_SERVER_URL,
            required_scope=OAUTH_SCOPE,
        )
        assert payload is not None
        assert payload["aud"] == _SERVER_URL
        assert payload["scope"] == OAUTH_SCOPE
        assert (
            verify_access_token(
                tokens["access_token"],
                b"0123456789abcdef0123456789abcdef",
                audience="https://other.example",
                required_scope=OAUTH_SCOPE,
            )
            is None
        )

        refresh_response = client_http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": oauth_client.client_id,
                "scope": OAUTH_SCOPE,
                "resource": config.effective_server_url,
            },
        )

        assert refresh_response.status_code == 200
        assert refresh_response.json()["refresh_token"] != tokens["refresh_token"]
        assert store.verify_token(
            refresh_response.json()["access_token"],
            b"0123456789abcdef0123456789abcdef",
            {hash_api_key("test-api-key")},
        )
        restored = OAuthStore.from_config(config)
        assert len(restored.refresh_tokens) == 1
        persisted_token = next(iter(restored.refresh_tokens.values()))
        assert persisted_token.scope == OAUTH_SCOPE
        assert persisted_token.resource == _SERVER_URL

    @pytest.mark.parametrize(
        ("parameter", "value", "error"),
        [
            ("scope", "fetchaller:write", "invalid_scope"),
            ("resource", "https://other.example", "invalid_target"),
        ],
    )
    def test_authorize_rejects_unsupported_scope_or_resource(
        self,
        parameter,
        value,
        error,
    ):
        client_http, store, _ = _route_client()
        oauth_client = store.register_client([_REDIRECT_URI])
        params = {
            "client_id": oauth_client.client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "code_challenge": _CHALLENGE,
            "code_challenge_method": "S256",
            "scope": OAUTH_SCOPE,
            "resource": _SERVER_URL,
        }
        params[parameter] = value

        response = client_http.get("/authorize", params=params)

        assert response.status_code == 400
        assert response.json()["error"] == error
        assert response.headers["cache-control"] == "no-store"

    def test_token_rejects_wrong_resource_without_consuming_code(self):
        client_http, store, _ = _route_client()
        oauth_client = store.register_client([_REDIRECT_URI])
        auth_code = store.create_auth_code(
            oauth_client.client_id,
            _CHALLENGE,
            _REDIRECT_URI,
            "test-api-key",
            OAUTH_SCOPE,
            _SERVER_URL,
        )

        response = client_http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code.code,
                "client_id": oauth_client.client_id,
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": _VERIFIER,
                "resource": "https://other.example",
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_target"
        assert auth_code.code in store.auth_codes

    def test_csrf_token_is_one_time_and_bound_to_every_authorization_field(self):
        client_http, store, _ = _route_client()
        oauth_client = store.register_client([_REDIRECT_URI])
        form, _ = _begin_authorization(client_http, oauth_client.client_id)
        tampered = dict(form)
        tampered["state"] = "attacker-state"

        tampered_response = client_http.post("/authorize", data=tampered)
        replay_response = client_http.post("/authorize", data=form)

        assert tampered_response.status_code == 400
        assert tampered_response.json()["error"] == "invalid_request"
        assert replay_response.status_code == 400
        assert store.auth_codes == {}

    def test_missing_or_oversized_authorization_params_get_oauth_errors(self):
        client_http, store, _ = _route_client()
        oauth_client = store.register_client([_REDIRECT_URI])

        missing = client_http.post("/authorize", data={})
        oversized = client_http.get(
            "/authorize",
            params={
                "client_id": oauth_client.client_id,
                "redirect_uri": _REDIRECT_URI,
                "response_type": "code",
                "state": "x" * 2049,
                "code_challenge": _CHALLENGE,
                "code_challenge_method": "S256",
            },
        )

        assert missing.status_code == 400
        assert missing.json()["error"] == "invalid_request"
        assert missing.headers["cache-control"] == "no-store"
        assert oversized.status_code == 400
        assert oversized.json()["error"] == "invalid_request"

    @pytest.mark.parametrize(
        "repeated",
        [
            "client_id",
            "redirect_uri",
            "response_type",
            "state",
            "code_challenge",
            "code_challenge_method",
            "scope",
            "resource",
        ],
    )
    def test_authorize_get_rejects_repeated_parameters(self, repeated):
        client_http, store, _ = _route_client()
        oauth_client = store.register_client([_REDIRECT_URI])
        values = {
            "client_id": oauth_client.client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "state": "opaque-state",
            "code_challenge": _CHALLENGE,
            "code_challenge_method": "S256",
            "scope": OAUTH_SCOPE,
            "resource": _SERVER_URL,
        }
        params = list(values.items())
        params.append((repeated, values[repeated]))

        response = client_http.get("/authorize", params=params)

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    @pytest.mark.parametrize("encoding", ["form", "json"])
    @pytest.mark.parametrize(
        "repeated",
        [
            "grant_type",
            "code",
            "refresh_token",
            "redirect_uri",
            "client_id",
            "code_verifier",
            "scope",
            "resource",
        ],
    )
    def test_token_rejects_repeated_parameters(self, encoding, repeated):
        client_http, _, _ = _route_client()
        values = {
            "grant_type": "authorization_code",
            "code": "unused-code",
            "refresh_token": "unused-refresh-token",
            "redirect_uri": _REDIRECT_URI,
            "client_id": "unused-client",
            "code_verifier": _VERIFIER,
            "scope": OAUTH_SCOPE,
            "resource": _SERVER_URL,
        }
        pairs = list(values.items())
        pairs.append((repeated, values[repeated]))
        if encoding == "form":
            response = client_http.post(
                "/token",
                content=urlencode(pairs),
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                },
            )
        else:
            body = "{" + ",".join(
                f"{json.dumps(key)}:{json.dumps(value)}"
                for key, value in pairs
            ) + "}"
            response = client_http.post(
                "/token",
                content=body,
                headers={"content-type": "application/json"},
            )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    def test_pkce_method_is_mandatory_and_exact(self):
        client_http, store, _ = _route_client()
        oauth_client = store.register_client([_REDIRECT_URI])
        base = {
            "client_id": oauth_client.client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "code_challenge": _CHALLENGE,
        }

        missing = client_http.get("/authorize", params=base)
        plain = client_http.get(
            "/authorize",
            params={**base, "code_challenge_method": "plain"},
        )

        assert missing.status_code == 400
        assert plain.status_code == 400
        assert missing.json()["error"] == "invalid_request"
        assert plain.json()["error"] == "invalid_request"

    def test_refresh_persistence_failure_restores_consumed_auth_code(
        self,
        monkeypatch,
    ):
        """A retryable token commit failure does not burn the one-time code."""
        client_http, store, _ = _route_client()
        oauth_client = store.register_client([_REDIRECT_URI])
        auth_code = store.create_auth_code(
            oauth_client.client_id,
            _CHALLENGE,
            _REDIRECT_URI,
            "test-api-key",
        )
        original_create_refresh_token = store.create_refresh_token
        monkeypatch.setattr(store, "create_refresh_token", lambda *args: None)
        token_form = {
            "grant_type": "authorization_code",
            "code": auth_code.code,
            "client_id": oauth_client.client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_verifier": _VERIFIER,
        }

        failed = client_http.post("/token", data=token_form)

        assert failed.status_code == 503
        assert auth_code.code in store.auth_codes
        monkeypatch.setattr(
            store,
            "create_refresh_token",
            original_create_refresh_token,
        )
        succeeded = client_http.post("/token", data=token_form)
        assert succeeded.status_code == 200
        assert auth_code.code not in store.auth_codes

    def test_authorize_logs_are_control_safe_and_bounded(self, capsys):
        client_http, _, _ = _route_client()
        injected = "known-client\nFORGED-LINE-" + ("x" * 1000)

        response = client_http.get(
            "/authorize",
            params={
                "client_id": injected,
                "response_type": "code",
                "code_challenge": _CHALLENGE,
                "code_challenge_method": "S256",
            },
        )

        assert response.status_code == 400
        captured = capsys.readouterr().err
        assert "\nFORGED-LINE" not in captured
        assert "x" * 100 not in captured
