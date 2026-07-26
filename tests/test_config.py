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

    def test_allow_ephemeral_jwt_requires_explicit_one(self, monkeypatch):
        """Only ALLOW_EPHEMERAL_JWT=1 enables the unsafe local-dev escape hatch."""
        monkeypatch.setenv("ALLOW_EPHEMERAL_JWT", "true")
        assert load_config().allow_ephemeral_jwt is False

        monkeypatch.setenv("ALLOW_EPHEMERAL_JWT", "1")
        assert load_config().allow_ephemeral_jwt is True
