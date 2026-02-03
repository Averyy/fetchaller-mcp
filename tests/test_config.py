"""Tests for configuration."""

import os

import pytest

from fetchaller.config import Config, load_config


class TestConfig:
    """Test configuration loading and validation."""

    def test_default_values(self):
        """Config has sensible defaults."""
        config = Config()

        assert config.http_port == 6000
        assert config.rate_limit_requests == 100
        assert config.default_max_tokens == 25000
        assert config.default_timeout_seconds == 10

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
