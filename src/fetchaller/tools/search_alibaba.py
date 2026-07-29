"""Alibaba.com search MCP tool wrapper."""

from ..alibaba.search import search_alibaba


async def search_alibaba_tool(
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
    """Search Alibaba.com products.

    Args:
        query: Search query string.
        page: Page number (1-indexed).
        sort: Sort order (default, price_asc, price_desc).
        min_price: Minimum price filter (USD).
        max_price: Maximum price filter (USD).
        timeout: End-to-end timeout in seconds.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: BrowserSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted results) or "error".
    """
    return await search_alibaba(
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
