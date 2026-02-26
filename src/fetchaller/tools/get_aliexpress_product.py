"""AliExpress product detail MCP tool wrapper."""

from ..aliexpress.product import get_product


async def get_aliexpress_product(
    product_id: str,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Get AliExpress product details including price, specs, and reviews.

    Args:
        product_id: Numeric product ID or full AliExpress URL.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: Optional BrowserSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted text) or "error".
    """
    return await get_product(
        product_id,
        cache=cache,
        config=config,
        browser_solver=browser_solver,
    )
