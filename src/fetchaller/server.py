"""MCP server setup for fetchaller."""

import sys
import time
from datetime import UTC, datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .cache.response_cache import ResponseCache
from .config import Config, load_config
from .content.fetcher import ContentFetcher, RetryConfig
from .queue.reddit_queue import QueueConfig, RedditRequestQueue
from .tools.browse_reddit import browse_reddit
from .tools.fetch import fetch_url
from .tools.search_reddit import search_reddit


def _log(msg: str) -> None:
    """Log with timestamp."""
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


def _summarize_args(tool_name: str, args: dict) -> str:
    """Summarize tool arguments for logging."""
    if tool_name == "fetch":
        url = args.get("url", "?")
        timeout = args.get("timeout", 10)
        return f"url={url} timeout={timeout}s"
    elif tool_name == "browse_reddit":
        return f"r/{args.get('subreddit', '?')} sort={args.get('sort', 'hot')}"
    elif tool_name == "search_reddit":
        return f"query={args.get('query', '?')} r/{args.get('subreddit', 'all')}"
    return str(args)


def create_server(
    config: Config | None = None,
    fetcher: ContentFetcher | None = None,
    cache: ResponseCache | None = None,
    reddit_queue: RedditRequestQueue | None = None,
) -> Server:
    """
    Create and configure the MCP server.

    Args:
        config: Optional configuration (loads from env if not provided)
        fetcher: Optional ContentFetcher instance
        cache: Optional ResponseCache instance
        reddit_queue: Optional RedditRequestQueue instance

    Returns:
        Configured MCP Server instance
    """
    if config is None:
        config = load_config()

    # Create shared instances
    if fetcher is None:
        fetcher = ContentFetcher(retry_config=RetryConfig.from_config(config))

    if cache is None:
        cache = ResponseCache.from_config(config)

    if reddit_queue is None:
        reddit_queue = RedditRequestQueue(QueueConfig.from_config(config))
        # Note: Queue auto-starts on first enqueue() call when event loop is running
        # Don't call start() here as there may not be a running event loop yet

    server = Server("fetchaller")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available tools."""
        return [
            Tool(
                name="fetch",
                description=(
                    "Fetch any URL and return the page content as clean markdown. "
                    "Handles HTML, JSON, XML, CSV, and PDF files. "
                    "Use this tool for reading/fetching web pages - it has no domain restrictions. "
                    "For discovering URLs via search, use WebSearch. For reading URL content, use this tool."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to fetch",
                        },
                        "maxTokens": {
                            "type": "integer",
                            "description": "Maximum tokens to return (default: 25000)",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Request timeout in seconds (default: 10)",
                        },
                        "raw": {
                            "type": "boolean",
                            "description": "Return raw HTML instead of markdown (default: false)",
                        },
                    },
                    "required": ["url"],
                },
            ),
            Tool(
                name="browse_reddit",
                description=(
                    "Browse a subreddit's posts. Returns metadata and URLs. "
                    "Use mcp__fetchaller__fetch to read full post content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "subreddit": {
                            "type": "string",
                            "description": "Subreddit name without r/ prefix",
                            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["hot", "new", "top", "rising"],
                            "default": "hot",
                            "description": "Sort order",
                        },
                        "time": {
                            "type": "string",
                            "enum": ["hour", "day", "week", "month", "year", "all"],
                            "default": "day",
                            "description": "Time filter (only applies to 'top' sort)",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 25,
                            "default": 10,
                            "description": "Number of posts (1-25)",
                        },
                        "after": {
                            "type": "string",
                            "description": "Pagination cursor from previous response",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Request timeout in seconds (default: 10)",
                        },
                    },
                    "required": ["subreddit"],
                },
            ),
            Tool(
                name="search_reddit",
                description=(
                    "Search Reddit posts. Returns metadata and URLs. "
                    "Use mcp__fetchaller__fetch to read full post content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "subreddit": {
                            "type": "string",
                            "description": "Limit to subreddit (without r/)",
                            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["relevance", "hot", "top", "new", "comments"],
                            "default": "relevance",
                            "description": "Sort order",
                        },
                        "time": {
                            "type": "string",
                            "enum": ["hour", "day", "week", "month", "year", "all"],
                            "default": "all",
                            "description": "Time filter",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 25,
                            "default": 10,
                            "description": "Number of results (1-25)",
                        },
                        "after": {
                            "type": "string",
                            "description": "Pagination cursor from previous response",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Request timeout in seconds (default: 10)",
                        },
                    },
                    "required": ["query"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Handle tool calls."""
        import traceback

        start_time = time.time()
        tool_args_summary = _summarize_args(name, arguments)
        _log(f"TOOL START: {name} {tool_args_summary}")

        try:
            if name == "fetch":
                result = await fetch_url(
                    url=arguments["url"],
                    max_tokens=arguments.get("maxTokens", config.default_max_tokens),
                    timeout=arguments.get("timeout", config.default_timeout_seconds),
                    raw=arguments.get("raw", False),
                    fetcher=fetcher,
                    cache=cache,
                    config=config,
                )

                elapsed = (time.time() - start_time) * 1000
                if "error" in result:
                    _log(f"TOOL END: {name} ERROR={result['error']} time={elapsed:.1f}ms")
                    text = f"Error: {result['error']}"
                    if "body" in result:
                        text += f"\n\nPartial content:\n{result['body']}"
                    return [TextContent(type="text", text=text)]

                content_len = len(result.get("content", ""))
                _log(f"TOOL END: {name} OK content_len={content_len} time={elapsed:.1f}ms")
                return [TextContent(type="text", text=result["content"])]

            elif name == "browse_reddit":
                result = await browse_reddit(
                    subreddit=arguments["subreddit"],
                    sort=arguments.get("sort", "hot"),
                    time=arguments.get("time", "day"),
                    limit=arguments.get("limit", 10),
                    after=arguments.get("after"),
                    timeout=arguments.get("timeout", 10),
                    fetcher=fetcher,
                    queue=reddit_queue,
                )

                elapsed = (time.time() - start_time) * 1000
                if "error" in result:
                    _log(f"TOOL END: {name} ERROR={result['error']} time={elapsed:.1f}ms")
                    return [TextContent(type="text", text=f"Error: {result['error']}")]

                content_len = len(result.get("content", ""))
                _log(f"TOOL END: {name} OK content_len={content_len} time={elapsed:.1f}ms")
                return [TextContent(type="text", text=result["content"])]

            elif name == "search_reddit":
                result = await search_reddit(
                    query=arguments["query"],
                    subreddit=arguments.get("subreddit"),
                    sort=arguments.get("sort", "relevance"),
                    time=arguments.get("time", "all"),
                    limit=arguments.get("limit", 10),
                    after=arguments.get("after"),
                    timeout=arguments.get("timeout", 10),
                    fetcher=fetcher,
                    queue=reddit_queue,
                )

                elapsed = (time.time() - start_time) * 1000
                if "error" in result:
                    _log(f"TOOL END: {name} ERROR={result['error']} time={elapsed:.1f}ms")
                    return [TextContent(type="text", text=f"Error: {result['error']}")]

                content_len = len(result.get("content", ""))
                _log(f"TOOL END: {name} OK content_len={content_len} time={elapsed:.1f}ms")
                return [TextContent(type="text", text=result["content"])]

            else:
                elapsed = (time.time() - start_time) * 1000
                _log(f"TOOL END: {name} UNKNOWN time={elapsed:.1f}ms")
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            _log(f"TOOL END: {name} EXCEPTION={type(e).__name__}: {e} time={elapsed:.1f}ms")
            traceback.print_exc(file=sys.stderr)
            return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]

    return server


async def run_stdio_server(config: Config | None = None) -> None:
    """Run the server in stdio mode."""
    import sys

    if config is None:
        config = load_config()

    server = create_server(config)

    print("fetchaller MCP server running on stdio", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
