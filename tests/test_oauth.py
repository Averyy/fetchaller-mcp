"""Tests for OAuth functionality."""

import hashlib
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from fetchaller.config import Config
from fetchaller.http.app import create_app
from fetchaller.http.middleware import verify_bearer_auth
from fetchaller.http.oauth import OAuthStore
from fetchaller.http.routes import create_router
from fetchaller.security.crypto import (
    create_access_token,
    generate_id,
    hash_api_key,
    timing_safe_compare,
    verify_access_token,
    verify_pkce,
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
        token = create_access_token("client123", "api_key_hash", secret, 3600)

        payload = verify_access_token(token, secret)
        assert payload is not None
        assert payload["sub"] == "client123"  # JWT uses "sub" for subject/client_id

    def test_invalid_token(self):
        """Invalid token returns None."""
        secret = b"test-secret-key-exactly-32-bytes"  # 32 bytes
        assert verify_access_token("invalid.token.here", secret) is None

    def test_wrong_secret(self):
        """A token signed under secret A fails verification under secret B."""
        secret1 = b"secret-one-exactly-32-bytes-key!"  # 32 bytes
        secret2 = b"secret-two-exactly-32-bytes-key!"  # 32 bytes

        token = create_access_token("client", "hash", secret1, 3600)
        assert verify_access_token(token, secret2) is None

    def test_fixed_secret_survives_independent_app_construction(self, tmp_path):
        """A fixed JWT secret lets independently constructed apps verify tokens."""
        config = Config(
            api_key="test-api-key",
            jwt_secret="stable-test-secret",
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
        assert isinstance(client.client_secret, str) and len(client.client_secret) > 10
        assert client.redirect_uris == ["https://example.com/cb"]

        # Retrieve by ID
        retrieved = store.get_client(client.client_id)
        assert retrieved.client_id == client.client_id
        assert retrieved.client_secret == client.client_secret

        # Unknown ID
        assert store.get_client("nonexistent-id") is None

    def test_create_auth_code_is_consumable(self):
        """Created auth code stores client_id and can be consumed with correct PKCE verifier."""
        store = OAuthStore()
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code("client1", challenge, "https://x.com/cb", "api_key")
        assert isinstance(auth_code.code, str) and len(auth_code.code) > 10
        assert auth_code.client_id == "client1"

        # Code is consumable with correct verifier
        result = store.consume_auth_code(auth_code.code, "client1", "https://x.com/cb", verifier)
        assert result is not None

    def test_consume_auth_code_once(self):
        """Auth code can only be consumed once."""
        store = OAuthStore()
        # Use a known verifier/challenge pair
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code("client1", challenge, "https://x.com/cb", "api_key")

        # First consume succeeds
        result = store.consume_auth_code(auth_code.code, "client1", "https://x.com/cb", verifier)
        assert result is not None

        # Second consume fails (already consumed)
        result2 = store.consume_auth_code(auth_code.code, "client1", "https://x.com/cb", verifier)
        assert result2 is None

    def test_consume_wrong_client_id(self):
        """Wrong client_id fails."""
        store = OAuthStore()
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code("client1", challenge, "https://x.com/cb", "api_key")
        result = store.consume_auth_code(auth_code.code, "wrong_client", "https://x.com/cb", verifier)

        assert result is None

    def test_consume_wrong_redirect_uri(self):
        """Wrong redirect_uri fails."""
        store = OAuthStore()
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

        auth_code = store.create_auth_code("client1", challenge, "https://x.com/cb", "api_key")
        result = store.consume_auth_code(auth_code.code, "client1", "https://wrong.com/cb", verifier)

        assert result is None

    def test_max_clients_limit(self):
        """Registration evicts the least-recently-used client at capacity."""
        store = OAuthStore(max_clients=2)

        oldest = store.register_client(["https://one.com/cb"])
        newest = store.register_client(["https://two.com/cb"])
        oldest.last_used_at = 1
        newest.last_used_at = 2
        result = store.register_client(["https://three.com/cb"])

        assert result is not None
        assert len(store.clients) == 2
        assert oldest.client_id not in store.clients
        assert newest.client_id in store.clients
        assert result.client_id in store.clients

    def test_client_registry_round_trips_with_secure_permissions(self, tmp_path):
        """Registered clients survive store reconstruction on disk."""
        store = OAuthStore(data_dir=str(tmp_path))
        client = store.register_client(["https://example.com/callback"], "Persistent")

        restored = OAuthStore(data_dir=str(tmp_path))
        loaded = restored.get_client(client.client_id)

        assert loaded is not None
        assert loaded.client_secret == client.client_secret
        assert loaded.redirect_uris == client.redirect_uris
        assert os.stat(tmp_path).st_mode & 0o777 == 0o700
        assert os.stat(tmp_path / "oauth_clients.json").st_mode & 0o777 == 0o600

    def test_expired_clients_are_dropped_on_load(self, tmp_path):
        """Loading persisted state cannot resurrect expired clients."""
        store = OAuthStore(data_dir=str(tmp_path), client_ttl=60)
        client = store.register_client(["https://example.com/callback"])
        client.created_at = time.time() - 61
        store._persist()

        restored = OAuthStore(data_dir=str(tmp_path), client_ttl=60)

        assert restored.clients == {}

    def test_corrupt_registry_starts_empty(self, tmp_path, caplog):
        """A corrupt state file is logged and treated as empty."""
        state_path = tmp_path / "oauth_clients.json"
        state_path.write_text("{not-json")

        store = OAuthStore(data_dir=str(tmp_path))

        assert store.clients == {}
        assert store.refresh_tokens == {}
        assert "starting empty" in caplog.text

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

        new_raw_token, restored_api_key_hash = rotated
        assert restored_api_key_hash == api_key_hash
        assert restored.rotate_refresh_token(raw_token, client.client_id, {api_key_hash}) is None
        assert restored.rotate_refresh_token(new_raw_token, client.client_id, {api_key_hash})

    def test_refresh_token_endpoint_rotates_and_issues_working_access_token(self, tmp_path):
        """The refresh grant rejects reuse and returns a valid access token."""
        config = Config(
            api_key="test-api-key",
            jwt_secret="test-jwt-secret",
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
        health = client.get("/health").json()
        assert "refresh_token" in metadata["grant_types_supported"]
        assert health["jwt_secret_ephemeral"] is True
        assert set(health) == {
            "status",
            "service",
            "timestamp",
            "jwt_secret_ephemeral",
        }
