"""Alibaba.com product detail MCP tool wrapper."""

from ..alibaba.product import get_product


async def get_alibaba_product(
    product_id: str,
    fetcher=None,
    cache=None,
    config=None,
    cookie_cache=None,
    challenge_solver=None,
) -> dict:
    """Get Alibaba.com product details including tiered pricing, MOQ, and specs.

    Args:
        product_id: Numeric product ID or full Alibaba.com URL.
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
