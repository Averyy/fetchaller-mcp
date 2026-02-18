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
async def test_error_returns_is_error_true(server):
    """Error results return CallToolResult with isError=True."""
    with patch("fetchaller.server.fetch_url", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = {"error": "Connection timeout"}
        result = await _call_tool(server, "fetch", {"url": "https://example.com"})
        assert isinstance(result, CallToolResult)
        assert result.isError is True
        assert "Connection timeout" in result.content[0].text


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
