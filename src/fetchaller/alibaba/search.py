"""Alibaba.com search via SSR HTML scraping.

Alibaba.com serves search data as server-rendered HTML with embedded JSON in
``window.__page__data_sse10._offer_list``. This does not use MTop, but wafer
may still invoke its browser solver when the HTML transport is challenged.
"""

from __future__ import annotations

import asyncio
import math
import re
import sys
import time
from datetime import UTC, datetime
from urllib.parse import quote, urlencode, urlparse

from bs4 import BeautifulSoup

from ..content._numeric import bounded_number_text
from ..content._price import has_positive_price
from ..content.alibaba import extract_search_data
from ..ratelimit import alibaba_limiter
from ..security.xss import safe_log_text
from ..tools.fetch import fetch_url


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] alibaba search: {safe_log_text(msg)}",
        file=sys.stderr,
    )


# Sort option mapping
_SORT_MAP = {
    "default": "",
    "price_asc": "PRICE_ASC",
    "price_desc": "PRICE_DESC",
}
_MAX_PAGE_OFFERS = 48
_MAX_TITLE_SOURCE_CHARS = 4096
_MAX_TITLE_CHARS = 500
_MAX_PRODUCT_URL_CHARS = 4096
_MAX_SEARCH_OUTPUT_CHARS = 100_000


def _bounded_offer_text(
    value: object,
    maximum: int,
) -> str:
    """Return a compact scalar offer field or omit an oversized value."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    try:
        text = " ".join(str(value).split())
    except (OverflowError, ValueError):
        return ""
    if not text or len(text) > maximum:
        return ""
    return text


def _offer_title(offer: dict) -> str:
    """Return the current or legacy Alibaba offer title as plain text."""

    value = offer.get("enPureTitle") or offer.get("title")
    if isinstance(value, dict):
        value = value.get("displayTitle") or value.get("seoTitle") or value.get("title")
    if (
        not isinstance(value, str)
        or len(value) > _MAX_TITLE_SOURCE_CHARS
    ):
        return ""
    # Current live SSR embeds promotional image/span markup in ``title``.
    # Search output is Markdown, so strip the markup instead of leaking HTML or
    # dropping every title when the legacy ``enPureTitle`` key is absent.
    title = " ".join(BeautifulSoup(value, "lxml").get_text(" ", strip=True).split())
    return title if len(title) <= _MAX_TITLE_CHARS else ""


def _offer_product_url(offer: dict) -> str | None:
    """Return a canonical Alibaba detail URL for a strictly valid offer."""

    product_url = offer.get("productUrl")
    product_id = offer.get("productId") or offer.get("id")
    product_id = str(product_id) if product_id is not None else ""
    if product_url:
        if (
            not isinstance(product_url, str)
            or len(product_url) > _MAX_PRODUCT_URL_CHARS
        ):
            return None
        candidate = f"https:{product_url}" if product_url.startswith("//") else product_url
        try:
            parsed = urlparse(candidate)
            port = parsed.port
        except (TypeError, ValueError):
            return None
        host = (parsed.hostname or "").lower()
        match = re.fullmatch(
            r"/product-detail/[^/]*_(\d{5,20})\.html",
            parsed.path,
        )
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or not (host == "alibaba.com" or host.endswith(".alibaba.com"))
            or match is None
            or (product_id and product_id != match.group(1))
        ):
            return None
        return parsed.geturl()
    if re.fullmatch(r"\d{5,20}", product_id):
        return f"https://www.alibaba.com/product-detail/_{product_id}.html"
    return None


def _has_positive_offer_price(offer: dict) -> bool:
    """Whether the current offer price contains a positive numeric amount."""

    return has_positive_price(
        offer.get("price"),
        require_currency=True,
    )


def _has_usable_offer(offer: object) -> bool:
    """Reject blank/ID-only/challenge-shell offers before formatting."""

    if not isinstance(offer, dict):
        return False
    title = _offer_title(offer)
    if not title or not any(character.isalpha() for character in title):
        return False
    return _has_positive_offer_price(offer) and _offer_product_url(offer) is not None


def _build_search_url(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
) -> str:
    """Build Alibaba.com search URL from parameters."""
    params: list[tuple[str, object]] = [("SearchText", query)]
    if page > 1:
        params.append(("page", page))
    sort_type = _SORT_MAP.get(sort, "")
    if sort_type:
        params.append(("sortType", sort_type))
    if min_price is not None:
        params.append(("minPrice", min_price))
    if max_price is not None:
        params.append(("maxPrice", max_price))
    return (
        "https://www.alibaba.com/trade/search?"
        + urlencode(params, quote_via=quote)
    )


def _format_offer(idx: int, offer: dict) -> str:
    """Format a single search result offer."""
    lines = []

    title = _offer_title(offer)
    lines.append(f"{idx}. {title}")

    # Price — top-level field (string like "US$0.50-1.20")
    price = _bounded_offer_text(offer.get("price", ""), 128)
    if price:
        lines.append(f"   Price: {price}")

    # MOQ — moqV2 is the formatted string, moq is sometimes raw
    moq = _bounded_offer_text(
        offer.get("moqV2", "") or offer.get("moq", ""),
        256,
    )
    if moq:
        lines.append(f"   MOQ: {moq}")

    # Product rating — combine score + count so "★4.8 (234 reviews)" is visible
    review_score = bounded_number_text(
        offer.get("reviewScore") or offer.get("productScore"),
        minimum=0,
        maximum=5,
    )
    review_count = bounded_number_text(
        offer.get("reviewCount"),
        minimum=0,
        maximum=1_000_000_000,
        integral=True,
        allow_grouping=True,
    )
    if review_score and review_count:
        lines.append(f"   ★{review_score} ({review_count} reviews)")
    elif review_count:
        lines.append(f"   {review_count} reviews")
    elif review_score:
        lines.append(f"   ★{review_score}")

    # Supplier info — separate line for clarity
    supplier = _bounded_offer_text(offer.get("companyName", ""), 512)
    country = _bounded_offer_text(offer.get("countryCode", ""), 16)
    supplier_parts = []
    if supplier:
        supplier_parts.append(supplier)
    if country:
        supplier_parts.append(country)
    years = _bounded_offer_text(offer.get("goldSupplierYears"), 64)
    if years:
        supplier_parts.append(f"{years}")
    svc_rating = bounded_number_text(
        offer.get("supplierService"),
        minimum=0,
        maximum=5,
    )
    if svc_rating:
        supplier_parts.append(f"service ★{svc_rating}")
    if supplier_parts:
        lines.append(f"   {' | '.join(supplier_parts)}")

    # Shipping time
    shipping = _bounded_offer_text(offer.get("shippingTime", ""), 256)
    if shipping:
        lines.append(f"   Ships: {shipping}")

    # Customizable flag
    if offer.get("customizable") is True:
        lines.append("   Customizable")

    # Product URL — use productUrl if available, otherwise build from productId/id
    product_url = _offer_product_url(offer)
    if product_url:
        lines.append(f"   {product_url}")

    return "\n".join(lines)


def _format_search_results(offers: list[dict], query: str, page: int, total: int) -> str:
    """Format search results into numbered list."""
    header = f'Search: "{query}" | page {page} | {total} results'
    offers = [offer for offer in offers if _has_usable_offer(offer)]
    if not offers:
        return f"{header}\n\nNo products found."

    # 48 products per page on Alibaba
    prefix = f"{header}\n\n"
    formatted: list[str] = []
    length = len(prefix)
    for index, offer in enumerate(offers[:_MAX_PAGE_OFFERS]):
        item = _format_offer(index + 1 + (page - 1) * _MAX_PAGE_OFFERS, offer)
        separator = 2 if formatted else 0
        if length + separator + len(item) > _MAX_SEARCH_OUTPUT_CHARS:
            marker = "[Additional products omitted to enforce the search output limit.]"
            while formatted:
                removed = formatted.pop()
                length -= len(removed) + (2 if formatted else 0)
                if length + 2 + len(marker) <= _MAX_SEARCH_OUTPUT_CHARS:
                    break
            formatted.append(marker)
            break
        formatted.append(item)
        length += separator + len(item)
    return prefix + "\n\n".join(formatted)


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
        _log(f"unexpected _offer_list structure: {type(e).__name__}")
        return None

    if not isinstance(offers, list):
        return None
    # Alibaba currently returns at most 48 offers per page. Cap the raw list
    # before BeautifulSoup/title validation so a compact hostile payload
    # cannot amplify CPU or MCP output with thousands of nominal offers.
    offers = [
        offer
        for offer in offers[:_MAX_PAGE_OFFERS]
        if _has_usable_offer(offer)
    ]
    if not offers:
        return None
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
    ):
        total_count = len(offers)
    total_count = min(total_count, 1_000_000_000)

    content = _format_search_results(offers, query, page, total_count)
    return {"content": content}


async def search_alibaba(
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

    Fetches SSR HTML and extracts embedded JSON. Wafer handles any transport
    challenge with the supplied browser solver.

    Args:
        query: Search query string.
        page: Page number (1-indexed).
        sort: Sort order (default, price_asc, price_desc).
        min_price: Minimum price filter (USD).
        max_price: Maximum price filter (USD).
        timeout: End-to-end request and browser-challenge timeout in seconds.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: BrowserSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted results) or "error".
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        return {"error": "timeout must be greater than zero and at most 300 seconds."}
    deadline = time.monotonic() + timeout
    try:
        async with asyncio.timeout(timeout):
            # Domain-level rate limiting (shared with alibaba product).
            # extra_delay=2.0 → 4.0 base + 2.0 = 6.0s minimum between
            # requests. Lock contention, spacing, and Retry-After deferral
            # consume the same advertised end-to-end budget as the fetch.
            await alibaba_limiter.wait(extra_delay=2.0)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            url = _build_search_url(query, page, sort, min_price, max_price)

            result = await fetch_url(
                url=url,
                max_tokens=500000,  # Full page for JSON extraction
                timeout=remaining,
                raw=True,  # Get raw HTML, not markdown
                cache=cache,
                config=config,
                browser_solver=browser_solver,
                _skip_alibaba_intercept=True,
            )
            if "error" in result:
                return result

            html = result.get("content", "")
            parsed = await asyncio.to_thread(
                _parse_search_html,
                html,
                query,
                page,
            )
    except TimeoutError:
        return {
            "error": (
                f"Request timed out after {timeout}s. "
                "Try increasing the timeout parameter for slow servers."
            )
        }

    if parsed:
        return parsed

    _log("no search data in response")
    return {"error": "Alibaba.com search failed. Could not extract product data from response."}
