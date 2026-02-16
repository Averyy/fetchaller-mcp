"""AliExpress product detail MCP tool wrapper."""

from ..aliexpress.product import get_product


async def get_aliexpress_product(
    product_id: str,
    fetcher=None,
    cache=None,
    config=None,
    cookie_cache=None,
    challenge_solver=None,
) -> dict:
    """Get AliExpress product details including price, specs, and reviews.

    Args:
        product_id: Numeric product ID or full AliExpress URL.
        fetcher: ContentFetcher for HTTP requests.
        cache: ResponseCache instance.
        config: Config instance.
        cookie_cache: CookieCache for bot challenge cookies.
        challenge_solver: ChallengeSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted text) or "error".
    """
    return await get_product(
        product_id,
        fetcher=fetcher,
        cache=cache,
        config=config,
        cookie_cache=cookie_cache,
        challenge_solver=challenge_solver,
    )
