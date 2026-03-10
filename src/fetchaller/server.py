"""MCP server setup for fetchaller."""

import sys
import time
import traceback
from datetime import UTC, datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .cache.response_cache import ResponseCache
from .config import Config, load_config, set_wafer_cache_dir
from .marketplace.search import search_marketplace
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


async def cleanup_server(server) -> None:
    """Clean up server resources (shared between stdio and HTTP shutdown).

    Each cleanup step is wrapped in try/except so one failure doesn't
    prevent the remaining resources from being released.
    """
    try:
        if hasattr(server, '_reddit_queue'):
            await server._reddit_queue.stop()
    except Exception:
        _log("warning: reddit queue cleanup failed")

    try:
        if hasattr(server, '_browser_solver') and server._browser_solver is not None:
            server._browser_solver.close()
    except Exception:
        _log("warning: browser solver cleanup failed")

    # Release module-level shared sessions (set to None, GC handles the rest).
    cleanup_fns = []
    try:
        from .search import close_session as close_search_session
        cleanup_fns.append(close_search_session)
    except ImportError:
        pass
    try:
        from .facebook_marketplace.graphql import close_session as close_fb_session
        cleanup_fns.append(close_fb_session)
    except ImportError:
        pass
    try:
        from .kijiji.api import close_session as close_kijiji_session
        cleanup_fns.append(close_kijiji_session)
    except ImportError:
        pass
    try:
        from .aliexpress.product import close_client as close_mtop_client
        cleanup_fns.append(close_mtop_client)
    except ImportError:
        pass
    try:
        from .aliexpress.reviews import close_session as close_reviews_session
        cleanup_fns.append(close_reviews_session)
    except ImportError:
        pass
    try:
        from .aliexpress.search import close_session as close_ae_search_session
        cleanup_fns.append(close_ae_search_session)
    except ImportError:
        pass
    try:
        from .tools.browse_reddit import close_session as close_reddit_session
        cleanup_fns.append(close_reddit_session)
    except ImportError:
        pass
    try:
        from .craigslist.sapi import close_session as close_cl_session
        cleanup_fns.append(close_cl_session)
    except ImportError:
        pass
    try:
        from .costco.api import close_session as close_costco_session
        cleanup_fns.append(close_costco_session)
    except ImportError:
        pass

    for fn in cleanup_fns:
        try:
            await fn()
        except Exception:
            _log(f"warning: {fn.__module__} cleanup failed")


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
    elif tool_name == "search_marketplace":
        return f"query={args.get('query', '?')} location={args.get('location', '?')} platforms={args.get('platforms', 'all')}"
    return str(args)


def create_server(
    config: Config | None = None,
    cache: ResponseCache | None = None,
    reddit_queue: RedditRequestQueue | None = None,
    browser_solver=None,
) -> Server:
    """
    Create and configure the MCP server.

    Args:
        config: Optional configuration (loads from env if not provided)
        cache: Optional ResponseCache instance
        reddit_queue: Optional RedditRequestQueue instance
        browser_solver: Optional BrowserSolver for browser-based challenges

    Returns:
        Configured MCP Server instance
    """
    if config is None:
        config = load_config()

    # Set global wafer cache dir so all modules creating sessions use it.
    set_wafer_cache_dir(config.wafer_cache_dir)

    if cache is None:
        cache = ResponseCache.from_config(config)

    if reddit_queue is None:
        reddit_queue = RedditRequestQueue(QueueConfig.from_config(config))
        # Note: Queue auto-starts on first enqueue() call when event loop is running
        # Don't call start() here as there may not be a running event loop yet

    # Auto-create BrowserSolver if not provided and patchright is installed.
    # Lazy browser launch: BrowserSolver only starts Chrome on first challenge.
    if browser_solver is None:
        try:
            from wafer.browser import BrowserSolver
            browser_solver = BrowserSolver()
            _log("BrowserSolver available (browser launched on first challenge)")
        except ImportError:
            _log("BrowserSolver not available (install wafer-py[browser] for bot challenge solving)")

    server = Server("fetchaller")
    # Store for external cleanup access (e.g., HTTP app lifespan)
    server._reddit_queue = reddit_queue  # type: ignore[attr-defined]
    server._browser_solver = browser_solver  # type: ignore[attr-defined]

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
            Tool(
                name="search_marketplace",
                description=(
                    "Search Kijiji, Craigslist, and Facebook Marketplace simultaneously. "
                    "Takes human-readable parameters (city name, category, price range) and "
                    "returns grouped results from all platforms. Use fetch(url) to get full "
                    "listing details for any result URL."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search keywords (e.g. \"golf r\", \"ikea couch\")",
                        },
                        "location": {
                            "type": "string",
                            "description": (
                                "City name, optionally with province/state "
                                "(e.g. \"toronto\", \"st catharines, ON\", \"seattle\")"
                            ),
                        },
                        "platforms": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["kijiji", "craigslist", "facebook"],
                            },
                            "description": (
                                "Platforms to search (default: all). "
                                "Kijiji is Canada-only and auto-skipped for US locations."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "all", "cars", "electronics", "furniture", "clothing",
                                "tools", "free", "bikes", "phones", "motorcycles",
                                "boats", "rvs", "auto_parts", "sporting", "toys", "baby",
                            ],
                            "default": "all",
                            "description": "Category filter",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["date", "price_asc", "price_desc", "relevance"],
                            "default": "date",
                            "description": "Sort order",
                        },
                        "condition": {
                            "type": "string",
                            "enum": ["new", "like_new", "good", "fair"],
                            "description": "Item condition filter",
                        },
                        "min_price": {
                            "type": "integer",
                            "description": "Minimum price in dollars",
                        },
                        "max_price": {
                            "type": "integer",
                            "description": "Maximum price in dollars",
                        },
                    },
                    "required": ["query", "location"],
                },
            ),
        ]

    def _format_result(name: str, result: dict, start_time: float) -> CallToolResult:
        """Format a tool result into CallToolResult, with logging.

        Content errors (bot detection, timeouts, 404s) return isError=False so
        that parallel sibling tool calls are not cancelled by the client.  The
        error message is still in the text content for the LLM to read.
        """
        elapsed = (time.time() - start_time) * 1000
        if "error" in result:
            _log(f"TOOL END: {name} ERROR={result['error']} time={elapsed:.1f}ms")
            text = f"Error: {result['error']}"
            if "body" in result:
                text += f"\n\nPartial content:\n{result['body']}"
            return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)

        content = result.get("content", "")
        _log(f"TOOL END: {name} OK content_len={len(content)} time={elapsed:.1f}ms")
        return CallToolResult(content=[TextContent(type="text", text=content)], isError=False)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        """Handle tool calls."""

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
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
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
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_aliexpress":
                result = await search_aliexpress_tool(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                    sort=arguments.get("sort", "default"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "get_alibaba_product":
                result = await get_alibaba_product(
                    product_id=arguments["product_id"],
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_alibaba":
                result = await search_alibaba_tool(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                    sort=arguments.get("sort", "default"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_marketplace":
                result = await search_marketplace(
                    query=arguments["query"],
                    location=arguments["location"],
                    platforms=arguments.get("platforms"),
                    category=arguments.get("category"),
                    sort=arguments.get("sort", "date"),
                    condition=arguments.get("condition"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    config=config,
                    browser_solver=browser_solver,
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
                content=[TextContent(type="text", text=f"Error: {type(e).__name__}: unexpected error (check server logs)")], isError=True,
            )

    return server


async def run_stdio_server(config: Config | None = None) -> None:
    """Run the server in stdio mode."""

    if config is None:
        config = load_config()

    server = create_server(config)

    from . import __version__

    print(f"[{datetime.now(UTC).isoformat()}] fetchaller MCP stdio server v{__version__} started", file=sys.stderr)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await cleanup_server(server)
