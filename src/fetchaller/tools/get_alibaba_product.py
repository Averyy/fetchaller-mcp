"""Alibaba.com product detail MCP tool wrapper."""

from ..alibaba.product import get_product


async def get_alibaba_product(
    product_id: str,
    timeout: int = 180,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Get Alibaba.com product details including tiered pricing, MOQ, and specs.

    Args:
        product_id: Numeric product ID or full Alibaba.com URL.
        timeout: End-to-end timeout in seconds.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: BrowserSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted text) or "error".
    """
    return await get_product(
        product_id,
        timeout=timeout,
        cache=cache,
        config=config,
        browser_solver=browser_solver,
    )
