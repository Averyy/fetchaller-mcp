"""Tests for MCP server call_tool handler and _format_result."""

from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from fetchaller.server import create_server


@pytest.fixture
def server():
    """Create a server with default config for testing."""
    return create_server()


async def _call_tool(server, name, arguments):
    """Invoke the server's call_tool handler directly."""
    handler = server.request_handlers[CallToolRequest]
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(request)
    return result.root


@pytest.mark.asyncio
async def test_success_returns_is_error_false(server):
    """Success results return CallToolResult with isError=False."""
    with patch("fetchaller.server.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {"content": "# Hello World"}
        result = await _call_tool(server, "fetch", {"url": "https://example.com"})
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)
        assert "Hello World" in result.content[0].text


@pytest.mark.asyncio
async def test_content_error_returns_is_error_false(server):
    """Content errors return isError=False so parallel sibling calls aren't cancelled."""
    with patch("fetchaller.server.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {"error": "Connection timeout"}
        result = await _call_tool(server, "fetch", {"url": "https://example.com"})
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert "Error: Connection timeout" in result.content[0].text


@pytest.mark.asyncio
async def test_error_with_partial_body(server):
    """Error results with partial body include both error and body text."""
    with patch("fetchaller.server.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {"error": "Truncated", "body": "partial content here"}
        result = await _call_tool(server, "fetch", {"url": "https://example.com"})
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        text = result.content[0].text
        assert "Truncated" in text
        assert "partial content here" in text


@pytest.mark.asyncio
async def test_unknown_tool_returns_is_error_true(server):
    """Unknown tool name returns CallToolResult with isError=True."""
    result = await _call_tool(server, "nonexistent_tool", {})
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert "Unknown tool" in result.content[0].text


@pytest.mark.asyncio
async def test_exception_returns_sanitized_error(server):
    """Unhandled exceptions return sanitized message, not raw exception details."""
    with patch("fetchaller.server.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = RuntimeError("https://api.mouser.com/search?apiKey=SECRET123")
        result = await _call_tool(server, "fetch", {"url": "https://example.com"})
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        text = result.content[0].text
        # Must NOT leak the exception message (could contain API keys)
        assert "SECRET123" not in text
        assert "unexpected error" in text
        # Exception type is still included for debugging
        assert "RuntimeError" in text


class TestBrowserSolverReadiness:
    """The startup guard must check the browser wafer actually launches.

    Regression: the old check looked for patchright's bundled *chromium* and
    logged "BrowserSolver available". wafer launches *system Chrome*
    (channel="chrome"), so the check passed on an image where every solve died
    at launch. A green check over a broken dependency is worse than no check.
    """

    def test_reports_missing_system_chrome(self, monkeypatch):
        from fetchaller import server as srv

        monkeypatch.setattr(srv, "_system_chrome_path", lambda: "/nonexistent/chrome")
        ready, detail = srv._browser_solver_ready()
        assert ready is False
        assert "/nonexistent/chrome" in detail

    def test_bundled_chromium_alone_does_not_satisfy_the_check(self, monkeypatch, tmp_path):
        """A populated PLAYWRIGHT_BROWSERS_PATH must not make the check pass."""
        from fetchaller import server as srv

        browsers = tmp_path / "browsers"
        (browsers / "chromium-1217").mkdir(parents=True)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
        monkeypatch.setattr(srv, "_system_chrome_path", lambda: "/nonexistent/chrome")

        ready, _ = srv._browser_solver_ready()
        assert ready is False, "bundled chromium is not what wafer launches"

    def test_requires_display_on_linux(self, monkeypatch, tmp_path):
        from fetchaller import server as srv

        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        monkeypatch.setattr(srv, "_system_chrome_path", lambda: str(chrome))
        monkeypatch.setattr(srv.sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)

        ready, detail = srv._browser_solver_ready()
        assert ready is False
        assert "DISPLAY" in detail

    def test_ready_when_chrome_and_live_display_present(self, monkeypatch, tmp_path):
        from fetchaller import server as srv

        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        sockets = tmp_path / "x11"
        sockets.mkdir()
        (sockets / "X99").touch()  # Xvfb is actually serving :99

        monkeypatch.setattr(srv, "_system_chrome_path", lambda: str(chrome))
        monkeypatch.setattr(srv, "_X11_SOCKET_DIR", sockets)
        monkeypatch.setattr(srv.sys, "platform", "linux")
        monkeypatch.setenv("DISPLAY", ":99")

        ready, detail = srv._browser_solver_ready()
        assert ready is True
        assert detail == str(chrome)

    def test_display_set_but_no_x_server_is_not_ready(self, monkeypatch, tmp_path):
        """The image sets DISPLAY unconditionally — the env var proves nothing.

        If Xvfb dies or never starts, an env-var-only check reports the solver
        as available over a browser that cannot launch. That is the same
        false-green the old bundled-Chromium check produced.
        """
        from fetchaller import server as srv

        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        sockets = tmp_path / "x11"
        sockets.mkdir()  # exists but empty: no X server listening

        monkeypatch.setattr(srv, "_system_chrome_path", lambda: str(chrome))
        monkeypatch.setattr(srv, "_X11_SOCKET_DIR", sockets)
        monkeypatch.setattr(srv.sys, "platform", "linux")
        monkeypatch.setenv("DISPLAY", ":99")

        ready, detail = srv._browser_solver_ready()
        assert ready is False
        assert "no X server is listening" in detail

    def test_remote_display_accepted_unverified(self, monkeypatch, tmp_path):
        """A TCP DISPLAY (host:0) has no local socket — don't fail it wrongly."""
        from fetchaller import server as srv

        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        monkeypatch.setattr(srv, "_system_chrome_path", lambda: str(chrome))
        monkeypatch.setattr(srv, "_X11_SOCKET_DIR", tmp_path / "nope")
        monkeypatch.setattr(srv.sys, "platform", "linux")
        monkeypatch.setenv("DISPLAY", "somehost:0")

        ready, _ = srv._browser_solver_ready()
        assert ready is True

    def test_macos_does_not_require_display(self, monkeypatch, tmp_path):
        from fetchaller import server as srv

        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        monkeypatch.setattr(srv, "_system_chrome_path", lambda: str(chrome))
        monkeypatch.setattr(srv.sys, "platform", "darwin")
        monkeypatch.delenv("DISPLAY", raising=False)

        ready, _ = srv._browser_solver_ready()
        assert ready is True
