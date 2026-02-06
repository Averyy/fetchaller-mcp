"""fetchaller-mcp: MCP server that fetches any URL with TLS fingerprint impersonation."""

from importlib.metadata import version

try:
    __version__ = version("fetchaller-mcp")
except Exception:
    __version__ = "2.1.0"  # Fallback for development
