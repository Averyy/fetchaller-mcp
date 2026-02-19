"""Tests for configuration."""

import os
from enum import Enum
from unittest.mock import patch

import pytest

from fetchaller.config import (
    _FALLBACK_FINGERPRINTS,
    Config,
    _chrome_version,
    _discover_chrome_fingerprints,
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


class TestChromeVersion:
    """Test _chrome_version sort key."""

    def test_base_version(self):
        assert _chrome_version("chrome133") == (133, "")

    def test_sub_version(self):
        assert _chrome_version("chrome133a") == (133, "a")

    def test_sub_version_sorts_after_base(self):
        assert _chrome_version("chrome133a") > _chrome_version("chrome133")

    def test_ordering(self):
        fps = ["chrome99", "chrome133a", "chrome133", "chrome136"]
        assert sorted(fps, key=_chrome_version) == [
            "chrome99", "chrome133", "chrome133a", "chrome136",
        ]

    def test_non_chrome_returns_zero(self):
        assert _chrome_version("firefox120") == (0, "firefox120")


class TestDiscoverChromeFingerprints:
    """Test _discover_chrome_fingerprints auto-discovery and fallback."""

    def test_live_discovery_returns_up_to_3_sorted(self):
        """Live discovery from curl_cffi returns 1-3 sorted Chrome desktop fps."""
        result = _discover_chrome_fingerprints()
        assert 1 <= len(result) <= 3
        # All must be chrome desktop (no android)
        for fp in result:
            assert fp.startswith("chrome")
            assert "android" not in fp
        # Must be sorted ascending by version
        versions = [_chrome_version(fp) for fp in result]
        assert versions == sorted(versions)

    def test_fallback_on_import_error(self):
        """Falls back to _FALLBACK_FINGERPRINTS when curl_cffi unavailable."""
        # Setting sys.modules entry to None makes `from X import Y` raise ImportError
        with patch.dict(
            "sys.modules",
            {"curl_cffi": None, "curl_cffi.requests": None},
        ):
            result = _discover_chrome_fingerprints()
        expected = sorted(_FALLBACK_FINGERPRINTS, key=_chrome_version)[-3:]
        assert result == expected

    def test_fallback_when_no_chrome_entries(self):
        """Falls back when BrowserType has no chrome desktop entries."""

        class NoChromeBrowserType(Enum):
            safari17 = "safari17"
            firefox120 = "firefox120"

        # Replace the real module with a fake that has no chrome entries
        import types
        fake_mod = types.ModuleType("curl_cffi.requests")
        fake_mod.BrowserType = NoChromeBrowserType
        with patch.dict("sys.modules", {"curl_cffi.requests": fake_mod}):
            result = _discover_chrome_fingerprints()
        expected = sorted(_FALLBACK_FINGERPRINTS, key=_chrome_version)[-3:]
        assert result == expected

    def test_selects_newest_3_from_many(self):
        """With many versions, returns only the newest 3 (excluding android)."""

        class ManyBrowserType(Enum):
            c99 = "chrome99"
            c110 = "chrome110"
            c120 = "chrome120"
            c133 = "chrome133"
            c133a = "chrome133a"
            c136 = "chrome136"
            android = "chrome_android"

        import types
        fake_mod = types.ModuleType("curl_cffi.requests")
        fake_mod.BrowserType = ManyBrowserType
        with patch.dict("sys.modules", {"curl_cffi.requests": fake_mod}):
            result = _discover_chrome_fingerprints()
        assert result == ["chrome133", "chrome133a", "chrome136"]
