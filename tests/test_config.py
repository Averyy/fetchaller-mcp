"""Tests for configuration."""

import os

import pytest

from fetchaller.config import Config, load_config


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
