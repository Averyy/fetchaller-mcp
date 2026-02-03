"""Tests for OAuth functionality."""

from fetchaller.http.oauth import OAuthStore
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
        """Generated ID has correct length."""
        id16 = generate_id(16)
        id32 = generate_id(32)
        assert len(id16) > 0
        assert len(id32) > len(id16)

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
        """Token with wrong secret fails."""
        secret1 = b"secret-one-exactly-32-bytes-key!"  # 32 bytes
        secret2 = b"secret-two-exactly-32-bytes-key!"  # 32 bytes

        token = create_access_token("client", "hash", secret1, 3600)
        assert verify_access_token(token, secret2) is None


class TestOAuthStore:
    """Test OAuth store operations."""

    def test_register_client(self):
        """Client registration creates client."""
        store = OAuthStore()
        client = store.register_client(["https://example.com/cb"], "Test")

        assert client is not None
        assert client.client_id is not None
        assert client.client_secret is not None
        assert client.redirect_uris == ["https://example.com/cb"]

    def test_get_client(self):
        """Can retrieve registered client."""
        store = OAuthStore()
        client = store.register_client(["https://example.com/cb"])

        retrieved = store.get_client(client.client_id)
        assert retrieved is not None
        assert retrieved.client_id == client.client_id

    def test_get_unknown_client(self):
        """Unknown client returns None."""
        store = OAuthStore()
        assert store.get_client("unknown") is None

    def test_create_auth_code(self):
        """Auth code creation works."""
        store = OAuthStore()
        code = store.create_auth_code("client1", "challenge", "https://x.com/cb", "api_key")

        assert code is not None
        assert code.code is not None
        assert code.client_id == "client1"

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
        """Registration fails at max clients."""
        store = OAuthStore(max_clients=2)

        store.register_client(["https://one.com/cb"])
        store.register_client(["https://two.com/cb"])
        result = store.register_client(["https://three.com/cb"])

        assert result is None
