"""Tests for configuration."""

import os

import pytest

from fetchaller.config import (
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
    Config,
    load_config,
)


class TestConfig:
    """Test configuration loading and validation."""

    def test_effective_server_url_default(self):
        """Server URL defaults to localhost with port."""
        config = Config(http_port=8080)
        assert config.effective_server_url == "http://localhost:8080"

    def test_effective_server_url_override(self):
        """Server URL can be overridden."""
        config = Config(server_url="https://example.com")
        assert config.effective_server_url == "https://example.com"

    @pytest.mark.parametrize(
        ("configured", "canonical"),
        [
            ("https://Example.COM:443", "https://example.com"),
            ("http://LOCALHOST:80", "http://localhost"),
            ("https://Example.COM:8443", "https://example.com:8443"),
        ],
    )
    def test_effective_server_url_is_a_canonical_origin(
        self, configured, canonical
    ):
        assert Config(server_url=configured).effective_server_url == canonical

    def test_port_validation_valid(self):
        """Valid ports are accepted."""
        os.environ["HTTP_PORT"] = "8080"
        os.environ.pop("MCP_API_KEY", None)
        os.environ.pop("MCP_SERVER_URL", None)
        os.environ.pop("JWT_SECRET", None)
        os.environ.pop("RATE_LIMIT_REQUESTS", None)

        try:
            config = load_config()
            assert config.http_port == 8080
        finally:
            os.environ.pop("HTTP_PORT", None)

    def test_port_validation_too_high(self):
        """Port > 65535 raises error."""
        os.environ["HTTP_PORT"] = "70000"

        try:
            with pytest.raises(ValueError, match="between 1 and 65535"):
                load_config()
        finally:
            os.environ.pop("HTTP_PORT", None)

    def test_port_validation_zero(self):
        """Port 0 raises error."""
        os.environ["HTTP_PORT"] = "0"

        try:
            with pytest.raises(ValueError, match="between 1 and 65535"):
                load_config()
        finally:
            os.environ.pop("HTTP_PORT", None)

    def test_rate_limit_validation(self):
        """Negative rate limit raises error."""
        os.environ["HTTP_PORT"] = "6000"
        os.environ["RATE_LIMIT_REQUESTS"] = "-1"

        try:
            with pytest.raises(ValueError, match="positive"):
                load_config()
        finally:
            os.environ.pop("HTTP_PORT", None)
            os.environ.pop("RATE_LIMIT_REQUESTS", None)

    def test_invalid_int_env_var(self, monkeypatch):
        """Non-integer env var raises clear error."""
        monkeypatch.setenv("CACHE_MAX_ENTRIES", "abc")
        with pytest.raises(ValueError, match="CACHE_MAX_ENTRIES must be an integer"):
            load_config()

    def test_invalid_float_env_var(self, monkeypatch):
        """Non-numeric env var raises clear error."""
        monkeypatch.setenv("RETRY_INITIAL_DELAY", "not-a-number")
        with pytest.raises(ValueError, match="RETRY_INITIAL_DELAY must be a number"):
            load_config()

    def test_invalid_port_env_var(self, monkeypatch):
        """Non-integer port raises clear error."""
        monkeypatch.setenv("HTTP_PORT", "banana")
        with pytest.raises(ValueError, match="HTTP_PORT must be an integer"):
            load_config()

    def test_invalid_rate_limit_env_var(self, monkeypatch):
        """Non-integer rate limit raises clear error."""
        monkeypatch.setenv("RATE_LIMIT_REQUESTS", "xyz")
        with pytest.raises(ValueError, match="RATE_LIMIT_REQUESTS must be an integer"):
            load_config()

    def test_access_token_ttl_defaults_to_30_days(self):
        """Access and refresh tokens use the conservative requested defaults."""
        assert ACCESS_TOKEN_TTL == 30 * 24 * 60 * 60
        assert REFRESH_TOKEN_TTL == 180 * 24 * 60 * 60
        assert Config().access_token_ttl == ACCESS_TOKEN_TTL
        assert Config().refresh_token_ttl == REFRESH_TOKEN_TTL

    def test_access_token_ttl_from_environment(self, monkeypatch):
        """ACCESS_TOKEN_TTL overrides the access-token lifetime in seconds."""
        monkeypatch.setenv("ACCESS_TOKEN_TTL", "12345")
        assert load_config().access_token_ttl == 12345

    def test_access_token_ttl_must_be_positive(self, monkeypatch):
        """Non-positive access-token lifetimes are rejected."""
        monkeypatch.setenv("ACCESS_TOKEN_TTL", "0")
        with pytest.raises(ValueError, match="ACCESS_TOKEN_TTL must be positive"):
            load_config()

    def test_data_dir_from_environment(self, monkeypatch, tmp_path):
        """DATA_DIR configures the persistent OAuth state location."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert load_config().data_dir == str(tmp_path)

    def test_browser_executable_path_from_environment(self, monkeypatch, tmp_path):
        browser = tmp_path / "Chrome for Testing"
        monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", str(browser))

        assert load_config().browser_executable_path == str(browser)

    @pytest.mark.parametrize("value", ["", "bad\x00path", "bad\npath"])
    def test_browser_executable_path_rejects_invalid_values(self, value):
        with pytest.raises(ValueError, match="BROWSER_EXECUTABLE_PATH"):
            Config(browser_executable_path=value)

    def test_local_default_data_dir_is_user_writable_not_container_path(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.delenv("DATA_DIR", raising=False)
        monkeypatch.delenv("WAFER_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

        config = load_config()

        assert config.data_dir == str(tmp_path / "fetchaller")
        assert config.wafer_cache_dir == str(tmp_path / "fetchaller" / "wafer")
        assert not config.data_dir.startswith("/app/")

    @pytest.mark.parametrize("value", ["not-a-cidr", "10.0.0.1/999"])
    def test_trusted_proxy_values_are_validated_at_startup(self, monkeypatch, value):
        monkeypatch.setenv("TRUSTED_PROXY_IPS", value)
        with pytest.raises(ValueError, match="TRUSTED_PROXY_IPS"):
            load_config()

    @pytest.mark.parametrize("value", ["nan", "inf", "-0.1"])
    def test_retry_delays_must_be_finite_and_nonnegative(self, monkeypatch, value):
        monkeypatch.setenv("RETRY_INITIAL_DELAY", value)
        with pytest.raises(ValueError, match="RETRY_INITIAL_DELAY"):
            load_config()

    @pytest.mark.parametrize(
        "value",
        [
            "ftp://example.com",
            "https://user:pass@example.com",
            "https://example.com/path?query=1",
            "https://example.com/",
            "https://example.com/oauth",
            "https://example.com?",
            "https://example.com#",
            "http://example.com",
            "http://127.0.0.2:6000",
            "https://example.com:",
            "https://example.com:0",
            "https://bad_host.example",
            "https://éxample.com",
            "not a url",
        ],
    )
    def test_server_url_is_strict_absolute_origin(self, monkeypatch, value):
        monkeypatch.setenv("MCP_SERVER_URL", value)
        with pytest.raises(ValueError, match="MCP_SERVER_URL"):
            load_config()

    @pytest.mark.parametrize(
        "value",
        [
            "http://example.com",
            "https://example.com/",
            "https://example.com/oauth",
            "https://example.com?",
            "https://example.com#",
        ],
    )
    def test_direct_config_cannot_bypass_server_origin_validation(self, value):
        """Programmatic Config consumers enforce the same OAuth origin boundary."""
        with pytest.raises(ValueError, match="MCP_SERVER_URL"):
            Config(server_url=value)

    @pytest.mark.parametrize(
        "value",
        [
            "https://example.com",
            "https://example.com:8443",
            "http://localhost:6000",
            "http://127.0.0.1:6000",
            "http://[::1]:6000",
        ],
    )
    def test_server_url_accepts_https_or_exact_loopback_origin(
        self,
        monkeypatch,
        value,
    ):
        monkeypatch.setenv("MCP_SERVER_URL", value)

        assert load_config().effective_server_url == value

    def test_authenticated_config_rejects_weak_jwt_secret(self, monkeypatch):
        monkeypatch.setenv("MCP_API_KEY", "api-key")
        monkeypatch.setenv("JWT_SECRET", "short")
        with pytest.raises(ValueError, match="JWT_SECRET"):
            load_config()

    def test_programmatic_authenticated_config_rejects_weak_jwt_secret(self):
        with pytest.raises(ValueError, match="JWT_SECRET"):
            Config(api_key="test-api-key", jwt_secret="short")

    def test_allow_ephemeral_jwt_requires_explicit_one(self, monkeypatch):
        """Only ALLOW_EPHEMERAL_JWT=1 enables the unsafe local-dev escape hatch."""
        monkeypatch.setenv("ALLOW_EPHEMERAL_JWT", "true")
        assert load_config().allow_ephemeral_jwt is False

        monkeypatch.setenv("ALLOW_EPHEMERAL_JWT", "1")
        assert load_config().allow_ephemeral_jwt is True

