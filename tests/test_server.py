"""Tests for MCP server call_tool handler and _format_result."""

import asyncio
import socket
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ListToolsRequest,
    TextContent,
)

from fetchaller import __version__
from fetchaller.config import Config
from fetchaller.http.app import create_app, run_http_server
from fetchaller.server import create_server, run_stdio_server


@pytest.fixture
def server():
    """Create a server with default config for testing."""
    return create_server(Config(data_dir=None), browser_solver=False)


async def _call_tool(server, name, arguments):
    """Invoke the server's call_tool handler directly."""
    handler = server.request_handlers[CallToolRequest]
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(request)
    return result.root


async def _list_tools(server):
    handler = server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest(method="tools/list"))
    return result.root.tools


def test_server_advertises_package_version(server):
    assert server.name == "fetchaller"
    assert server.version == __version__


@pytest.mark.asyncio
async def test_exact_tool_surface_has_strict_schemas(server):
    tools = await _list_tools(server)

    assert [tool.name for tool in tools] == [
        "fetch",
        "browse_reddit",
        "search_reddit",
        "search",
        "get_aliexpress_product",
        "search_aliexpress",
        "get_alibaba_product",
        "search_alibaba",
        "search_marketplace",
        "search_linkedin_jobs",
        "get_linkedin_job",
        "search_realtor",
    ]
    assert all(tool.inputSchema["additionalProperties"] is False for tool in tools)
    assert all(tool.inputSchema["required"] for tool in tools)
    product = next(tool for tool in tools if tool.name == "get_aliexpress_product")
    assert product.inputSchema["properties"]["timeout"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 180,
        "default": 180,
        "description": "End-to-end timeout in seconds (default: 180)",
    }
    alibaba_search = next(tool for tool in tools if tool.name == "search_alibaba")
    assert alibaba_search.inputSchema["properties"]["timeout"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 300,
        "default": 180,
        "description": "End-to-end timeout in seconds (default: 180)",
    }
    aliexpress_search = next(
        tool for tool in tools if tool.name == "search_aliexpress"
    )
    assert aliexpress_search.inputSchema["properties"]["timeout"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 300,
        "default": 180,
        "description": "End-to-end timeout in seconds (default: 180)",
    }
    alibaba_product = next(
        tool for tool in tools if tool.name == "get_alibaba_product"
    )
    assert alibaba_product.inputSchema["properties"]["timeout"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 300,
        "default": 180,
        "description": "End-to-end timeout in seconds (default: 180)",
    }


@pytest.mark.parametrize(
    ("tool_name", "patch_target", "arguments"),
    [
        ("fetch", "fetch_url", {"url": "https://example.com"}),
        ("browse_reddit", "browse_reddit", {"subreddit": "Python"}),
        ("search_reddit", "search_reddit", {"query": "asyncio"}),
        ("search", "search_web", {"query": "asyncio"}),
        (
            "get_aliexpress_product",
            "get_aliexpress_product",
            {"product_id": "1005006727707575"},
        ),
        (
            "search_aliexpress",
            "search_aliexpress_tool",
            {"query": "usb cable"},
        ),
        (
            "get_alibaba_product",
            "get_alibaba_product",
            {"product_id": "1600486391522"},
        ),
        (
            "search_alibaba",
            "search_alibaba_tool",
            {"query": "usb cable"},
        ),
        (
            "search_marketplace",
            "search_marketplace",
            {"query": "bicycle", "location": "Toronto, ON"},
        ),
        (
            "search_realtor",
            "search_realtor",
            {"location": "Toronto"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_tool_dispatches_through_the_server(
    server,
    tool_name,
    patch_target,
    arguments,
):
    with patch(
        f"fetchaller.server.{patch_target}",
        new_callable=AsyncMock,
        return_value={"content": "verified"},
    ) as implementation:
        result = await _call_tool(server, tool_name, arguments)

    assert result.isError is False
    assert result.content[0].text == "verified"
    implementation.assert_awaited_once()


@pytest.mark.asyncio
async def test_aliexpress_product_timeout_dispatch_and_range(server):
    with patch(
        "fetchaller.server.get_aliexpress_product",
        new_callable=AsyncMock,
        return_value={"content": "verified"},
    ) as implementation:
        result = await _call_tool(
            server,
            "get_aliexpress_product",
            {"product_id": "1005006727707575", "timeout": 17},
        )

    assert result.isError is False
    assert implementation.await_args.kwargs["timeout"] == 17
    invalid = await _call_tool(
        server,
        "get_aliexpress_product",
        {"product_id": "1005006727707575", "timeout": 181},
    )
    assert invalid.isError is True
    assert "maximum of 180" in invalid.content[0].text


@pytest.mark.asyncio
async def test_alibaba_search_timeout_dispatch_and_range(server):
    with patch(
        "fetchaller.server.search_alibaba_tool",
        new_callable=AsyncMock,
        return_value={"content": "verified"},
    ) as implementation:
        result = await _call_tool(
            server,
            "search_alibaba",
            {"query": "linear rail", "timeout": 240},
        )

    assert result.isError is False
    assert implementation.await_args.kwargs["timeout"] == 240
    invalid = await _call_tool(
        server,
        "search_alibaba",
        {"query": "linear rail", "timeout": 301},
    )
    assert invalid.isError is True
    assert "maximum of 300" in invalid.content[0].text


@pytest.mark.parametrize(
    ("tool_name", "patch_target", "arguments"),
    [
        (
            "search_aliexpress",
            "search_aliexpress_tool",
            {"query": "usb cable", "timeout": 240},
        ),
        (
            "get_alibaba_product",
            "get_alibaba_product",
            {"product_id": "1600486391522", "timeout": 240},
        ),
    ],
)
@pytest.mark.asyncio
async def test_other_protected_commerce_timeout_dispatch_and_range(
    server,
    tool_name,
    patch_target,
    arguments,
):
    with patch(
        f"fetchaller.server.{patch_target}",
        new_callable=AsyncMock,
        return_value={"content": "verified"},
    ) as implementation:
        result = await _call_tool(server, tool_name, arguments)

    assert result.isError is False
    assert implementation.await_args.kwargs["timeout"] == 240
    invalid_arguments = dict(arguments, timeout=301)
    invalid = await _call_tool(server, tool_name, invalid_arguments)
    assert invalid.isError is True
    assert "maximum of 300" in invalid.content[0].text


@pytest.mark.parametrize(
    ("tool_name", "arguments", "error_fragment"),
    [
        ("fetch", {"url": "https://example.com", "unknown": True}, "not allowed"),
        ("search", {"query": ""}, "non-empty"),
        ("browse_reddit", {"subreddit": "python/evil"}, "match"),
        (
            "search_reddit",
            {"query": "asyncio", "subreddit": "_python"},
            "match",
        ),
        ("search_aliexpress", {"query": "x", "min_price": -1}, "minimum"),
        (
            "search_marketplace",
            {
                "query": "x",
                "location": "Toronto",
                "platforms": ["kijiji", "kijiji"],
            },
            "unique",
        ),
        (
            "search_realtor",
            {"location": "Toronto", "min_price": 2, "max_price": 1},
            "must not exceed",
        ),
        ("search_realtor", {"location": "Toronto", "page": 31}, "maximum"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_arguments_are_machine_detectable(
    server,
    tool_name,
    arguments,
    error_fragment,
):
    result = await _call_tool(server, tool_name, arguments)

    assert result.isError is True
    assert error_fragment in result.content[0].text


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


@pytest.mark.parametrize("result_value", [None, {}, {"content": ""}, {"content": " \n "}])
@pytest.mark.asyncio
async def test_invalid_or_empty_tool_result_is_an_mcp_error(server, result_value):
    with patch(
        "fetchaller.server.fetch_url",
        new_callable=AsyncMock,
        return_value=result_value,
    ):
        result = await _call_tool(
            server,
            "fetch",
            {"url": "https://example.com"},
        )

    assert result.isError is True
    assert "Error:" in result.content[0].text


@pytest.mark.parametrize(
    "message",
    [
        "This is a private Reddit community.",
        "This community is quarantined.",
        "This Reddit community has been banned.",
        "Reddit content not found.",
    ],
)
@pytest.mark.asyncio
async def test_reddit_content_states_are_not_mcp_errors(server, message):
    with patch(
        "fetchaller.server.fetch_url",
        new_callable=AsyncMock,
        return_value={"content": f"# Reddit\n\n{message}"},
    ):
        result = await _call_tool(
            server,
            "fetch",
            {"url": "https://www.reddit.com/r/example/about/"},
        )

    assert result.isError is False
    assert message in result.content[0].text


@pytest.mark.asyncio
async def test_content_error_returns_is_error_true(server):
    """Content errors are machine-detectable MCP tool failures."""
    with patch("fetchaller.server.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {"error": "Connection timeout"}
        result = await _call_tool(server, "fetch", {"url": "https://example.com"})
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert "Error: Connection timeout" in result.content[0].text


@pytest.mark.asyncio
async def test_account_gated_reddit_moderator_roster_is_an_mcp_error(server):
    with patch(
        "fetchaller.server.fetch_url",
        new_callable=AsyncMock,
        return_value={
            "error": (
                "Reddit requires a logged-in account for exact moderator "
                "rosters, and fetchaller reads Reddit anonymously only. No "
                "moderator names were guessed or reconstructed."
            )
        },
    ):
        result = await _call_tool(
            server,
            "fetch",
            {
                "url": (
                    "https://www.reddit.com/r/Python/about/moderators/"
                )
            },
        )

    assert result.isError is True
    assert "requires a logged-in account" in result.content[0].text
    assert "No moderator names were guessed" in result.content[0].text


@pytest.mark.asyncio
async def test_reddit_morechildren_declared_failure_is_an_mcp_error(server):
    url = (
        "https://www.reddit.com/api/morechildren?"
        "link_id=t3_abc123&children=def456"
    )
    with patch(
        "fetchaller.server.fetch_url",
        new_callable=AsyncMock,
        return_value={
            "error": "Reddit reported that comment expansion failed."
        },
    ) as fetch:
        result = await _call_tool(server, "fetch", {"url": url})

    assert result.isError is True
    assert result.content[0].text == (
        "Error: Reddit reported that comment expansion failed."
    )
    assert fetch.await_args.kwargs["url"] == url


@pytest.mark.asyncio
async def test_error_with_partial_body(server):
    """Error results with partial body include both error and body text."""
    with patch("fetchaller.server.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {"error": "Truncated", "body": "partial content here"}
        result = await _call_tool(server, "fetch", {"url": "https://example.com"})
        assert isinstance(result, CallToolResult)
        assert result.isError is True
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

    def test_windows_finds_per_user_chrome(self, monkeypatch):
        from fetchaller import server as srv

        monkeypatch.setattr(srv.sys, "platform", "win32")
        monkeypatch.setenv("PROGRAMFILES", r"C:\Program Files")
        monkeypatch.setenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
        expected = (
            r"C:\Users\me\AppData\Local/Google/Chrome/Application/chrome.exe"
        )
        monkeypatch.setattr(
            srv.os,
            "access",
            lambda path, _mode: path == expected,
        )

        assert srv._system_chrome_path() == expected

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
        with tempfile.TemporaryDirectory(
            prefix="fetchaller-x11-", dir="/tmp"
        ) as sockets_dir:
            sockets = Path(sockets_dir)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(sockets / "X99"))
            listener.listen()

            monkeypatch.setattr(srv, "_system_chrome_path", lambda: str(chrome))
            monkeypatch.setattr(srv, "_X11_SOCKET_DIR", sockets)
            monkeypatch.setattr(srv.sys, "platform", "linux")
            monkeypatch.setenv("DISPLAY", ":99")

            try:
                ready, detail = srv._browser_solver_ready()
                assert ready is True
                assert detail == str(chrome)
            finally:
                listener.close()

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
        assert "no X server is accepting connections" in detail

    def test_stale_x_socket_path_is_not_ready(self, monkeypatch, tmp_path):
        from fetchaller import server as srv

        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        with tempfile.TemporaryDirectory(
            prefix="fetchaller-x11-", dir="/tmp"
        ) as sockets_dir:
            sockets = Path(sockets_dir)
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(sockets / "X99"))
            stale.close()

            monkeypatch.setattr(srv, "_system_chrome_path", lambda: str(chrome))
            monkeypatch.setattr(srv, "_X11_SOCKET_DIR", sockets)
            monkeypatch.setattr(srv.sys, "platform", "linux")
            monkeypatch.setenv("DISPLAY", ":99")

            ready, detail = srv._browser_solver_ready()
            assert ready is False
            assert "no X server is accepting connections" in detail

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


class TestGuardedBrowserLifecycle:
    class FakeProxy:
        def __init__(self):
            self.url = "socks5://127.0.0.1:43210"
            self.ready = False
            self.closed = False

        def start(self):
            self.ready = True

        def close(self):
            self.ready = False
            self.closed = True

    class FakeSolver:
        def __init__(
            self,
            *,
            egress_guard_proxy=None,
            executable_path=None,
        ):
            self.egress_guard_proxy = egress_guard_proxy
            self.executable_path = executable_path
            self.closed = False
            self.preflighted = False
            self.runtime_ready = False

        def configure_egress_guard(self, proxy):
            self.egress_guard_proxy = proxy

        def preflight(self):
            self.preflighted = True
            self.runtime_ready = True

        def close(self):
            self.closed = True
            self.runtime_ready = False

    def test_auto_solver_is_always_constructed_with_guard_proxy(self):
        from fetchaller import server as srv

        proxy = self.FakeProxy()
        created: list[TestGuardedBrowserLifecycle.FakeSolver] = []

        def solver_factory(**kwargs):
            solver = self.FakeSolver(**kwargs)
            created.append(solver)
            return solver

        with (
            patch.object(srv, "BrowserEgressProxy", return_value=proxy),
            patch("wafer.browser.BrowserSolver", side_effect=solver_factory),
            patch.object(
                srv,
                "_browser_solver_ready",
                return_value=(True, "/chrome"),
            ),
        ):
            server = create_server(Config(data_dir=None))

        assert created[0].egress_guard_proxy == proxy.url
        assert server._browser_solver is created[0]
        assert server._browser_proxy is proxy
        assert proxy.ready
        created[0].close()
        proxy.close()

    def test_auto_solver_uses_caller_pinned_browser_executable(self):
        from fetchaller import server as srv

        proxy = self.FakeProxy()
        created: list[TestGuardedBrowserLifecycle.FakeSolver] = []

        def solver_factory(**kwargs):
            solver = self.FakeSolver(**kwargs)
            created.append(solver)
            return solver

        with (
            patch.object(srv, "BrowserEgressProxy", return_value=proxy),
            patch("wafer.browser.BrowserSolver", side_effect=solver_factory),
            patch.object(
                srv,
                "_browser_solver_ready",
                return_value=(True, "/pinned/chrome"),
            ) as ready,
        ):
            server = create_server(
                Config(
                    data_dir=None,
                    browser_executable_path="/pinned/chrome",
                )
            )

        assert created[0].executable_path == "/pinned/chrome"
        ready.assert_called_once_with("/pinned/chrome")
        assert server._browser_solver is created[0]
        created[0].close()
        proxy.close()

    def test_injected_solver_is_configured_before_use(self):
        from fetchaller import server as srv

        proxy = self.FakeProxy()
        solver = self.FakeSolver()
        with (
            patch.object(srv, "BrowserEgressProxy", return_value=proxy),
            patch.object(
                srv,
                "_browser_solver_ready",
                return_value=(True, "/chrome"),
            ),
        ):
            server = create_server(
                Config(data_dir=None),
                browser_solver=solver,
            )

        assert solver.egress_guard_proxy == proxy.url
        assert server._browser_solver is solver
        assert server._browser_proxy is proxy
        solver.close()
        proxy.close()

    def test_browser_preflight_requires_pinned_recaptcha_models(self):
        from fetchaller import server as srv

        proxy = self.FakeProxy()
        solver = self.FakeSolver()
        with (
            patch.object(srv, "BrowserEgressProxy", return_value=proxy),
            patch.object(
                srv,
                "_browser_solver_ready",
                return_value=(True, "/chrome"),
            ),
            patch.object(srv, "_preflight_recaptcha_models") as preflight,
        ):
            create_server(
                Config(data_dir=None, browser_preflight=True),
                browser_solver=solver,
            )

        preflight.assert_called_once_with()
        assert solver.preflighted
        solver.close()
        proxy.close()

    def test_recaptcha_preflight_wrapper_waits_for_native_loader_completion(self):
        from fetchaller import server as srv

        with patch(
            "wafer.browser.preflight_recaptcha_models", create=True
        ) as preflight:
            srv._preflight_recaptcha_models()

        preflight.assert_called_once_with(timeout=None)

    def test_unconfigurable_injected_solver_fails_closed(self):
        from fetchaller import server as srv

        proxy = self.FakeProxy()
        solver = object()
        with patch.object(srv, "BrowserEgressProxy", return_value=proxy):
            server = create_server(
                Config(data_dir=None),
                browser_solver=solver,
            )

        assert server._browser_solver is None
        assert server._browser_proxy is None
        assert proxy.closed

    @pytest.mark.asyncio
    async def test_cleanup_closes_proxy_even_when_solver_close_fails(self):
        from fetchaller.server import cleanup_server

        class BrokenSolver:
            def close(self):
                raise RuntimeError("close failed")

        proxy = self.FakeProxy()
        proxy.start()
        holder = type(
            "Holder",
            (),
            {
                "_browser_solver": BrokenSolver(),
                "_browser_proxy": proxy,
            },
        )()

        await cleanup_server(holder)
        assert proxy.closed

    @pytest.mark.asyncio
    async def test_browser_cleanup_is_bounded_while_a_solver_is_busy(self):
        from fetchaller.server import close_browser_runtime_bounded

        started = threading.Event()
        release = threading.Event()

        class BusySolver:
            def close(self):
                started.set()
                release.wait(timeout=2)

        proxy = self.FakeProxy()
        proxy.start()
        holder = type(
            "Holder",
            (),
            {"_browser_solver": BusySolver(), "_browser_proxy": proxy},
        )()

        started_at = time.monotonic()
        await close_browser_runtime_bounded(holder, timeout=0.01)
        elapsed = time.monotonic() - started_at
        release.set()

        assert elapsed < 0.5
        assert proxy.closed
        assert holder._browser_solver is None
        assert holder._browser_proxy is None

    def test_failed_authenticated_readiness_closes_partial_browser_runtime(self):
        class BrokenSolver:
            def __init__(self):
                self.close_attempted = False

            def close(self):
                self.close_attempted = True
                raise RuntimeError("close failed")

        solver = BrokenSolver()
        proxy = self.FakeProxy()
        proxy.start()
        proxy.ready = False
        holder = type(
            "Holder",
            (),
            {
                "_browser_solver": solver,
                "_browser_proxy": proxy,
            },
        )()

        with pytest.raises(RuntimeError, match="browser_proxy"):
            create_app(
                Config(
                    api_key="test-api-key",
                    jwt_secret="0123456789abcdef0123456789abcdef",
                    data_dir=None,
                    wafer_cache_dir=None,
                    browser_preflight=True,
                ),
                mcp_server=holder,
            )

        assert solver.close_attempted
        assert proxy.closed
        assert holder._browser_solver is None
        assert holder._browser_proxy is None

    def test_health_dynamically_detects_solver_and_proxy_failure(self):
        config = Config(
            api_key="test-api-key",
            jwt_secret="0123456789abcdef0123456789abcdef",
            server_url="https://fetchaller.example",
            data_dir=None,
            wafer_cache_dir=None,
            browser_preflight=True,
        )
        solver = self.FakeSolver()
        solver.preflight()
        proxy = self.FakeProxy()
        proxy.start()
        mcp_server = create_server(config, browser_solver=False)
        mcp_server._browser_solver = solver
        mcp_server._browser_proxy = proxy
        app = create_app(config, mcp_server=mcp_server)

        with TestClient(
            app,
            base_url=config.effective_server_url,
        ) as client:
            healthy = client.get("/health")
            proxy.ready = False
            proxy_failed = client.get("/health")
            proxy.ready = True
            solver.runtime_ready = False
            solver_failed = client.get("/health")

        assert healthy.status_code == 200
        assert healthy.json()["readiness"]["browser_solver"] is True
        assert healthy.json()["readiness"]["browser_proxy"] is True
        assert proxy_failed.status_code == 503
        assert proxy_failed.json()["readiness"]["browser_proxy"] is False
        assert solver_failed.status_code == 503
        assert solver_failed.json()["readiness"]["browser_solver"] is False


@pytest.mark.asyncio
async def test_stdio_constructs_and_preflights_server_off_the_asyncio_thread():
    constructed_off_loop = False

    class _FakeServer:
        def create_initialization_options(self):
            return object()

        async def run(self, _read, _write, _options):
            return None

    def construct(_config):
        nonlocal constructed_off_loop
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            constructed_off_loop = True
        return _FakeServer()

    @asynccontextmanager
    async def fake_stdio():
        yield object(), object()

    with (
        patch("fetchaller.server.create_server", side_effect=construct),
        patch("fetchaller.server.stdio_server", fake_stdio),
        patch("fetchaller.server.cleanup_server", new_callable=AsyncMock),
    ):
        await run_stdio_server(Config(data_dir=None))

    assert constructed_off_loop is True


@pytest.mark.asyncio
async def test_stdio_identity_captures_source_before_server_construction(
    tmp_path,
):
    from scripts.reddit_parity import _audit_process_identity

    wafer_package = tmp_path / "wafer"
    wafer_package.mkdir()
    wafer_init = wafer_package / "__init__.py"
    wafer_module = wafer_package / "transport.py"
    wafer_init.write_text("from .transport import value\n")
    wafer_module.write_text("value = 1\n")
    messages: list[str] = []

    class _FakeServer:
        def create_initialization_options(self):
            return object()

        async def run(self, _read, _write, _options):
            return None

    def construct(_config):
        # Mutation during server/model/browser construction must occur after
        # START was captured and therefore make END differ.
        wafer_module.write_text("value = 2\n")
        return _FakeServer()

    @asynccontextmanager
    async def fake_stdio():
        yield object(), object()

    with (
        patch("fetchaller.server.create_server", side_effect=construct),
        patch("fetchaller.server.stdio_server", fake_stdio),
        patch("fetchaller.server.cleanup_server", new_callable=AsyncMock),
        patch(
            "fetchaller.server.importlib.util.find_spec",
            return_value=SimpleNamespace(origin=str(wafer_init)),
        ),
        patch("fetchaller.server._log", side_effect=messages.append),
    ):
        await run_stdio_server(Config(data_dir=None))

    log = tmp_path / "server.stderr.log"
    log.write_text("\n".join(messages))
    evidence = _audit_process_identity(log, "test")
    assert evidence.status == "failed"
    assert evidence.detail == (
        "process source identity changed during run: wafer_sha256"
    )


@pytest.mark.asyncio
async def test_http_constructs_and_preflights_server_off_the_asyncio_thread():
    constructed_off_loop = False
    sentinel_server = object()
    sentinel_app = object()

    def construct(_config):
        nonlocal constructed_off_loop
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            constructed_off_loop = True
        return sentinel_server

    fake_uvicorn_server = AsyncMock()
    fake_uvicorn_server.serve = AsyncMock()
    with (
        patch("fetchaller.http.app.create_server", side_effect=construct),
        patch(
            "fetchaller.http.app.create_app",
            return_value=sentinel_app,
        ) as app_factory,
        patch("uvicorn.Config", return_value=object()) as config_factory,
        patch("uvicorn.Server", return_value=fake_uvicorn_server),
    ):
        await run_http_server(Config(data_dir=None))

    assert constructed_off_loop is True
    app_factory.assert_called_once()
    assert app_factory.call_args.args[0].data_dir is None
    assert app_factory.call_args.kwargs == {"mcp_server": sentinel_server}
    assert config_factory.call_args.kwargs["timeout_graceful_shutdown"] == 30
    fake_uvicorn_server.serve.assert_awaited_once()


class TestAuditedBrowserSolverCountsEveryNavigation:
    """The dispatch audit must observe every browser navigation it claims to.

    ``BROWSER_DISPATCH_SUMMARY ... reddit=0`` is used as evidence that no
    browser ever drove a Reddit page. That only holds if the wrapper intercepts
    every entry point that navigates. It wrapped ``solve``/``asolve`` only,
    while ``__getattr__`` forwarded ``intercept_iframe``/``aintercept_iframe``
    -- which navigate to ``embedder_url`` -- straight to the real solver,
    uncounted. The summary then asserted more than it had observed.
    """

    @staticmethod
    def _wrap():
        from fetchaller.server import _AuditedBrowserSolver

        class FakeSolver:
            def __init__(self):
                self.calls = []

            def solve(self, url, *a, **k):
                self.calls.append(("solve", url))

            async def asolve(self, url, *a, **k):
                self.calls.append(("asolve", url))

            def intercept_iframe(self, embedder_url, *a, **k):
                self.calls.append(("intercept_iframe", embedder_url))

            async def aintercept_iframe(self, embedder_url, *a, **k):
                self.calls.append(("aintercept_iframe", embedder_url))

            def preflight(self):
                self.calls.append(("preflight", None))

        inner = FakeSolver()
        audit = {"total": 0, "reddit": 0}
        return inner, audit, _AuditedBrowserSolver(inner, audit)

    def test_sync_iframe_interception_is_counted(self):
        inner, audit, wrapped = self._wrap()
        wrapped.intercept_iframe("https://www.reddit.com/r/python/", "reddit.com")
        assert audit == {"total": 1, "reddit": 1}
        assert inner.calls == [("intercept_iframe", "https://www.reddit.com/r/python/")]

    async def test_async_iframe_interception_is_counted(self):
        inner, audit, wrapped = self._wrap()
        await wrapped.aintercept_iframe("https://old.reddit.com/r/python/", "reddit.com")
        assert audit == {"total": 1, "reddit": 1}

    def test_non_reddit_navigation_counts_only_toward_total(self):
        _, audit, wrapped = self._wrap()
        wrapped.intercept_iframe("https://example.com/careers", "ashby.com")
        assert audit == {"total": 1, "reddit": 0}

    async def test_solve_paths_still_counted(self):
        _, audit, wrapped = self._wrap()
        wrapped.solve("https://www.reddit.com/r/x/")
        await wrapped.asolve("https://example.com/")
        assert audit == {"total": 2, "reddit": 1}

    def test_every_url_taking_solver_method_is_audited(self):
        """Guards against a future wafer navigation method slipping through.

        ``preflight()`` takes no URL and performs no target navigation, so it is
        excluded; anything else whose first parameter is a URL must be wrapped.
        """

        import inspect

        from wafer.browser import BrowserSolver

        from fetchaller.server import _AuditedBrowserSolver

        url_methods = set()
        for name, func in inspect.getmembers(BrowserSolver, inspect.isfunction):
            if name.startswith("_"):
                continue
            params = list(inspect.signature(func).parameters)
            if len(params) > 1 and params[1] in {"url", "embedder_url"}:
                url_methods.add(name)

        assert url_methods, "no URL-taking solver methods found"
        unaudited = {n for n in url_methods if n not in vars(_AuditedBrowserSolver)}
        assert not unaudited, (
            f"BrowserSolver method(s) navigate uncounted: {sorted(unaudited)}"
        )
