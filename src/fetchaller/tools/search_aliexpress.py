"""AliExpress search MCP tool wrapper."""

from ..aliexpress.search import search_aliexpress


async def search_aliexpress_tool(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
    timeout: int = 180,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Search AliExpress products.

    Args:
        query: Search query string.
        page: Page number (1-indexed).
        sort: Sort order (default, orders, price_asc, price_desc).
        min_price: Minimum price filter.
        max_price: Maximum price filter.
        timeout: End-to-end timeout in seconds.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: BrowserSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted results) or "error".
    """
    return await search_aliexpress(
        query=query,
        page=page,
        sort=sort,
        min_price=min_price,
        max_price=max_price,
        timeout=timeout,
        cache=cache,
        config=config,
        browser_solver=browser_solver,
    )
