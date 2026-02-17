"""MCP server setup for fetchaller."""

import sys
import time
from datetime import UTC, datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .botfighter import ChallengeSolver, CookieCache
from .cache.response_cache import ResponseCache
from .config import Config, load_config
from .content.fetcher import ContentFetcher, RetryConfig
from .queue.reddit_queue import QueueConfig, RedditRequestQueue
from .tools.browse_reddit import browse_reddit
from .tools.fetch import fetch_url
from .tools.get_alibaba_product import get_alibaba_product
from .tools.get_aliexpress_product import get_aliexpress_product
from .tools.search import search_web
from .tools.search_alibaba import search_alibaba_tool
from .tools.search_aliexpress import search_aliexpress_tool
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
    elif tool_name == "search":
        return f"query={args.get('query', '?')} page={args.get('page', 1)}"
    elif tool_name == "get_aliexpress_product":
        return f"product_id={args.get('product_id', '?')}"
    elif tool_name == "search_aliexpress":
        return f"query={args.get('query', '?')} page={args.get('page', 1)} sort={args.get('sort', 'default')}"
    elif tool_name == "get_alibaba_product":
        return f"product_id={args.get('product_id', '?')}"
    elif tool_name == "search_alibaba":
        return f"query={args.get('query', '?')} page={args.get('page', 1)} sort={args.get('sort', 'default')}"
    return str(args)


def create_server(
    config: Config | None = None,
    fetcher: ContentFetcher | None = None,
    cache: ResponseCache | None = None,
    reddit_queue: RedditRequestQueue | None = None,
    cookie_cache: CookieCache | None = None,
    challenge_solver: ChallengeSolver | None = None,
) -> Server:
    """
    Create and configure the MCP server.

    Args:
        config: Optional configuration (loads from env if not provided)
        fetcher: Optional ContentFetcher instance
        cache: Optional ResponseCache instance
        reddit_queue: Optional RedditRequestQueue instance
        cookie_cache: Optional CookieCache for bot challenge cookie persistence
        challenge_solver: Optional ChallengeSolver for browser-based challenge solving

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

    if cookie_cache is None:
        cookie_cache = CookieCache(persist_path=config.cookie_cache_path)

    if challenge_solver is None:
        challenge_solver = ChallengeSolver(config)

    server = Server("fetchaller")
    # Store reddit_queue for external cleanup access (e.g., HTTP app lifespan)
    server._reddit_queue = reddit_queue  # type: ignore[attr-defined]

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
                    "For discovering URLs via search, use the search tool. For reading URL content, use this tool."
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
            Tool(
                name="search",
                description=(
                    "Search the web and return results with titles, URLs, and snippets. "
                    "Use this to discover URLs, then use fetch to read full page content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                            "description": "Result page (1-indexed, default: 1)",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_aliexpress_product",
                description=(
                    "Get AliExpress product details including price, specifications, "
                    "ratings, and recent reviews. Accepts a numeric product ID or full URL."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "product_id": {
                            "type": "string",
                            "description": "Numeric product ID or full AliExpress URL",
                        },
                    },
                    "required": ["product_id"],
                },
            ),
            Tool(
                name="search_aliexpress",
                description=(
                    "Search AliExpress products. Returns product listings with prices, "
                    "ratings, and links. Best-effort — may fail if anti-bot protection triggers."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                            "description": "Page number (1-indexed, default: 1)",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["default", "orders", "price_asc", "price_desc"],
                            "default": "default",
                            "description": "Sort order",
                        },
                        "min_price": {
                            "type": "number",
                            "description": "Minimum price filter",
                        },
                        "max_price": {
                            "type": "number",
                            "description": "Maximum price filter",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_alibaba_product",
                description=(
                    "Get Alibaba.com B2B product details including tiered pricing, "
                    "MOQ, lead times, supplier info, and specifications. "
                    "Accepts a numeric product ID or full URL."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "product_id": {
                            "type": "string",
                            "description": "Numeric product ID or full Alibaba.com URL",
                        },
                    },
                    "required": ["product_id"],
                },
            ),
            Tool(
                name="search_alibaba",
                description=(
                    "Search Alibaba.com B2B products. Returns supplier listings with "
                    "tiered pricing, MOQ, and supplier info."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                            "description": "Page number (1-indexed, default: 1)",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["default", "price_asc", "price_desc"],
                            "default": "default",
                            "description": "Sort order",
                        },
                        "min_price": {
                            "type": "number",
                            "description": "Minimum price filter (USD)",
                        },
                        "max_price": {
                            "type": "number",
                            "description": "Maximum price filter (USD)",
                        },
                    },
                    "required": ["query"],
                },
            ),
        ]

    def _format_result(name: str, result: dict, start_time: float) -> CallToolResult:
        """Format a tool result into CallToolResult, with logging."""
        elapsed = (time.time() - start_time) * 1000
        if "error" in result:
            _log(f"TOOL END: {name} ERROR={result['error']} time={elapsed:.1f}ms")
            text = f"Error: {result['error']}"
            if "body" in result:
                text += f"\n\nPartial content:\n{result['body']}"
            return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)

        content = result.get("content", "")
        _log(f"TOOL END: {name} OK content_len={len(content)} time={elapsed:.1f}ms")
        return CallToolResult(content=[TextContent(type="text", text=content)], isError=False)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        """Handle tool calls."""
        import traceback

        start_time = time.time()
        tool_args_summary = _summarize_args(name, arguments)
        _log(f"TOOL START: {name} {tool_args_summary}")

        try:
            if name == "fetch":
                result = await fetch_url(
                    url=arguments["url"],
                    max_tokens=max(1, min(250000, arguments.get("maxTokens", config.default_max_tokens))),
                    timeout=max(1, min(300, arguments.get("timeout", config.default_timeout_seconds))),
                    raw=arguments.get("raw", False),
                    fetcher=fetcher,
                    cache=cache,
                    config=config,
                    cookie_cache=cookie_cache,
                    challenge_solver=challenge_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "browse_reddit":
                result = await browse_reddit(
                    subreddit=arguments["subreddit"],
                    sort=arguments.get("sort", "hot"),
                    time=arguments.get("time", "day"),
                    limit=arguments.get("limit", 10),
                    after=arguments.get("after"),
                    timeout=max(1, min(300, arguments.get("timeout", 10))),
                    fetcher=fetcher,
                    queue=reddit_queue,
                )
                return _format_result(name, result, start_time)

            elif name == "search_reddit":
                result = await search_reddit(
                    query=arguments["query"],
                    subreddit=arguments.get("subreddit"),
                    sort=arguments.get("sort", "relevance"),
                    time=arguments.get("time", "all"),
                    limit=arguments.get("limit", 10),
                    after=arguments.get("after"),
                    timeout=max(1, min(300, arguments.get("timeout", 10))),
                    fetcher=fetcher,
                    queue=reddit_queue,
                )
                return _format_result(name, result, start_time)

            elif name == "search":
                result = await search_web(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                )
                return _format_result(name, result, start_time)

            elif name == "get_aliexpress_product":
                result = await get_aliexpress_product(
                    product_id=arguments["product_id"],
                    fetcher=fetcher,
                    cache=cache,
                    config=config,
                    cookie_cache=cookie_cache,
                    challenge_solver=challenge_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_aliexpress":
                result = await search_aliexpress_tool(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                    sort=arguments.get("sort", "default"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    fetcher=fetcher,
                    cache=cache,
                    config=config,
                    cookie_cache=cookie_cache,
                    challenge_solver=challenge_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "get_alibaba_product":
                result = await get_alibaba_product(
                    product_id=arguments["product_id"],
                    fetcher=fetcher,
                    cache=cache,
                    config=config,
                    cookie_cache=cookie_cache,
                    challenge_solver=challenge_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_alibaba":
                result = await search_alibaba_tool(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                    sort=arguments.get("sort", "default"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    fetcher=fetcher,
                    cache=cache,
                    config=config,
                    cookie_cache=cookie_cache,
                    challenge_solver=challenge_solver,
                )
                return _format_result(name, result, start_time)

            else:
                elapsed = (time.time() - start_time) * 1000
                _log(f"TOOL END: {name} UNKNOWN time={elapsed:.1f}ms")
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Unknown tool: {name}")], isError=True,
                )

        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            _log(f"TOOL END: {name} EXCEPTION={type(e).__name__}: {e} time={elapsed:.1f}ms")
            traceback.print_exc(file=sys.stderr)
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")], isError=True,
            )

    return server


async def run_stdio_server(config: Config | None = None) -> None:
    """Run the server in stdio mode."""

    if config is None:
        config = load_config()

    fetcher = ContentFetcher(retry_config=RetryConfig.from_config(config))
    challenge_solver = ChallengeSolver(config)
    cookie_cache = CookieCache(persist_path=config.cookie_cache_path)
    server = create_server(
        config, fetcher=fetcher, cookie_cache=cookie_cache, challenge_solver=challenge_solver,
    )

    from . import __version__

    print(f"[{datetime.now(UTC).isoformat()}] fetchaller MCP stdio server v{__version__} started", file=sys.stderr)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await fetcher.close()
        await challenge_solver.close()
        if hasattr(server, '_reddit_queue'):
            await server._reddit_queue.stop()
        from .search import close_session as close_search_session
        await close_search_session()
