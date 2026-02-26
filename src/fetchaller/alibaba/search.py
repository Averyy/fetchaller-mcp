"""Alibaba.com search via SSR HTML scraping.

Alibaba.com serves search data as server-rendered HTML with embedded JSON
in ``window.__page__data_sse10._offer_list``. No MTop API or browser needed.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from ..content.alibaba import extract_search_data
from ..ratelimit import alibaba_limiter
from ..tools.fetch import fetch_url


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] alibaba search: {msg}", file=sys.stderr)

# Sort option mapping
_SORT_MAP = {
    "default": "",
    "price_asc": "PRICE_ASC",
    "price_desc": "PRICE_DESC",
}


def _build_search_url(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
) -> str:
    """Build Alibaba.com search URL from parameters."""
    from urllib.parse import quote

    params = [f"SearchText={quote(query)}"]
    if page > 1:
        params.append(f"page={page}")
    sort_type = _SORT_MAP.get(sort, "")
    if sort_type:
        params.append(f"sortType={sort_type}")
    if min_price is not None:
        params.append(f"minPrice={min_price}")
    if max_price is not None:
        params.append(f"maxPrice={max_price}")
    return "https://www.alibaba.com/trade/search?" + "&".join(params)


def _format_offer(idx: int, offer: dict) -> str:
    """Format a single search result offer."""
    lines = []

    title = offer.get("enPureTitle", "")
    lines.append(f"{idx}. {title}")

    # Price — top-level field (string like "US$0.50-1.20")
    price = offer.get("price", "")
    if price:
        lines.append(f"   Price: {price}")

    # MOQ — moqV2 is the formatted string, moq is sometimes raw
    moq = offer.get("moqV2", "") or offer.get("moq", "")
    if moq:
        lines.append(f"   MOQ: {moq}")

    # Product rating — combine score + count so "★4.8 (234 reviews)" is visible
    review_score = offer.get("reviewScore") or offer.get("productScore")
    review_count = offer.get("reviewCount")
    if review_score and review_count:
        lines.append(f"   ★{review_score} ({review_count} reviews)")
    elif review_count:
        lines.append(f"   {review_count} reviews")
    elif review_score:
        lines.append(f"   ★{review_score}")

    # Supplier info — separate line for clarity
    supplier = offer.get("companyName", "")
    country = offer.get("countryCode", "")
    supplier_parts = []
    if supplier:
        supplier_parts.append(supplier)
    if country:
        supplier_parts.append(country)
    years = offer.get("goldSupplierYears")
    if years:
        supplier_parts.append(f"{years}")
    svc_rating = offer.get("supplierService")
    if svc_rating:
        supplier_parts.append(f"service ★{svc_rating}")
    if supplier_parts:
        lines.append(f"   {' | '.join(supplier_parts)}")

    # Shipping time
    shipping = offer.get("shippingTime", "")
    if shipping:
        lines.append(f"   Ships: {shipping}")

    # Customizable flag
    if offer.get("customizable"):
        lines.append("   Customizable")

    # Product URL — use productUrl if available, otherwise build from productId/id
    product_url = offer.get("productUrl", "")
    product_id = offer.get("productId") or offer.get("id", "")
    if product_url:
        if not product_url.startswith("http"):
            product_url = f"https:{product_url}"
        lines.append(f"   {product_url}")
    elif product_id:
        lines.append(f"   https://www.alibaba.com/product-detail/_{product_id}.html")

    return "\n".join(lines)


def _format_search_results(
    offers: list[dict], query: str, page: int, total: int
) -> str:
    """Format search results into numbered list."""
    header = f'Search: "{query}" | page {page} | {total} results'
    if not offers:
        return f"{header}\n\nNo products found."

    # 48 products per page on Alibaba
    formatted = [
        _format_offer(i + 1 + (page - 1) * 48, offer)
        for i, offer in enumerate(offers)
    ]
    return f"{header}\n\n" + "\n\n".join(formatted)


def _parse_search_html(html: str, query: str, page: int) -> dict | None:
    """Extract search results from HTML containing __page__data_sse10.

    Returns formatted content dict or None if extraction fails.
    """
    offer_list = extract_search_data(html)
    if not offer_list:
        return None

    try:
        offer_data = offer_list.get("offerResultData", {})
        offers = offer_data.get("offers", [])
        total_count = offer_data.get("totalCount", len(offers))
    except (TypeError, AttributeError) as e:
        _log(f"unexpected _offer_list structure: {e}")
        return None

    if not offers:
        return None

    content = _format_search_results(offers, query, page, total_count)
    return {"content": content}


async def search_alibaba(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Search Alibaba.com products.

    Fetches SSR HTML and extracts embedded JSON. No browser needed.

    Args:
        query: Search query string.
        page: Page number (1-indexed).
        sort: Sort order (default, price_asc, price_desc).
        min_price: Minimum price filter (USD).
        max_price: Maximum price filter (USD).
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: BrowserSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted results) or "error".
    """
    # Domain-level rate limiting (shared with alibaba product).
    # extra_delay=2.0 → 4.0 base + 2.0 = 6.0s minimum between search requests.
    await alibaba_limiter.wait(extra_delay=2.0)

    url = _build_search_url(query, page, sort, min_price, max_price)

    result = await fetch_url(
        url=url,
        max_tokens=500000,  # Full page for JSON extraction
        timeout=30,
        raw=True,  # Get raw HTML, not markdown
        cache=cache,
        config=config,
        browser_solver=browser_solver,
        _skip_alibaba_intercept=True,
    )

    if "error" in result:
        return result

    html = result.get("content", "")
    parsed = _parse_search_html(html, query, page)
    if parsed:
        return parsed

    _log(f"no search data in response for query={query}")
    return {"error": "Alibaba.com search failed. Could not extract product data from response."}
