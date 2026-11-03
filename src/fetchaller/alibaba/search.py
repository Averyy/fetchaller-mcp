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
from ..content.alibaba import extract_search_data, is_pro_search_shell, is_zero_result_page
from ..ratelimit import alibaba_limiter
from ..security.xss import safe_log_text
from ..tools.fetch import fetch_url


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] alibaba search: {safe_log_text(msg)}",
        file=sys.stderr,
    )


# Sort option -> Alibaba ``sortType``. The search page offers only relevance
# (empty) and "most sold" (``prodSold180``). It has no price order:
# ``sortType=PRICE_ASC`` and ``PRICE_DESC`` both answered the same unsorted
# relevance list (2026-11-02), so the price sorts read the relevance page and
# order that page here, and say so (``_LOCAL_PRICE_SORTS``).
_SORT_MAP = {
    "default": "",
    "orders": "prodSold180",
    "price_asc": "",
    "price_desc": "",
}
_LOCAL_PRICE_SORTS = frozenset({"price_asc", "price_desc"})
# ``/trade/search`` redirects about half of its requests, at random and per
# request, to the "pro search" app shell, which carries no results. It is the
# fallback only (``_trade_search_url``), and gets this many tries.
_TRADE_SEARCH_ATTEMPTS = 3
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


def _price_param(value: float) -> str:
    """A price bound as plain decimal text (never exponent notation)."""

    text = format(float(value), "f").rstrip("0").rstrip(".")
    return text or "0"


def _filter_params(
    page: int,
    sort: str,
    min_price: float | None,
    max_price: float | None,
) -> list[tuple[str, object]]:
    """Paging, sort and price-band parameters, under the names the site uses.

    The price band is ``pricef``/``pricet`` (the page's own filter form);
    ``minPrice``/``maxPrice``, sent until 3.8.8, were ignored without a word:
    9 of 48 offers inside a 1-2 band, against 48 of 48 with ``pricef``.
    """

    params: list[tuple[str, object]] = []
    if page > 1:
        params.append(("page", page))
    sort_type = _SORT_MAP.get(sort, "")
    if sort_type:
        params.append(("sortType", sort_type))
    if min_price is not None:
        params.append(("pricef", _price_param(min_price)))
    if max_price is not None:
        params.append(("pricet", _price_param(max_price)))
    return params


def _normalized_query(query: str) -> str:
    return " ".join(query.split())


def _products_url(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
) -> str | None:
    """Alibaba's keyword listing page (``/products/usb_c_cable.html``).

    It serves the same server-rendered ``_offer_list`` as ``/trade/search`` in
    one request, with no redirect chain, and it never answered with the "pro
    search" shell (20+ reads, 2026-11-02), where ``/trade/search`` did on about
    half of them. The slug cannot carry every query: a ``/`` cut "50/50 mix"
    down to the keyword "50" even percent-encoded, and ``_`` reads as a space.
    None for those; the page's own ``keyword`` is checked afterwards as well.
    """

    text = _normalized_query(query)
    if not text or "/" in text or "_" in text:
        return None
    slug = quote(text.replace(" ", "_"), safe="_")
    params = _filter_params(page, sort, min_price, max_price)
    url = f"https://www.alibaba.com/products/{slug}.html"
    return f"{url}?{urlencode(params, quote_via=quote)}" if params else url


def _trade_search_url(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
) -> str:
    """The site's own ``/trade/search`` form (its paging links use it)."""

    text = _normalized_query(query)
    params: list[tuple[str, object]] = [
        ("fsb", "y"),
        ("IndexArea", "product_en"),
        ("keywords", text),
        ("originKeywords", text),
        *_filter_params(page, sort, min_price, max_price),
    ]
    return "https://www.alibaba.com/trade/search?" + urlencode(params, quote_via=quote)


def _searched_keyword(html: str) -> str | None:
    """The keyword Alibaba says it searched, when the page states one."""

    data = extract_search_data(html)
    keyword = data.get("keyword") if isinstance(data, dict) else None
    if isinstance(keyword, str) and len(keyword) <= 1024:
        return keyword
    return None


def _lowest_price(offer: dict) -> float | None:
    """The lowest amount in an offer's price text ("$2.40-3.39" -> 2.40)."""

    text = _bounded_offer_text(offer.get("price"), 128)
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if match is None:
        return None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _order_by_price(offers: list[dict], *, descending: bool) -> list[dict]:
    """Order one page's offers by lowest listed price; unreadable prices last."""

    priced = [offer for offer in offers if _lowest_price(offer) is not None]
    unpriced = [offer for offer in offers if _lowest_price(offer) is None]
    priced.sort(key=lambda offer: _lowest_price(offer), reverse=descending)
    return priced + unpriced


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


def _format_search_results(
    offers: list[dict],
    query: str,
    page: int,
    total: int,
    note: str = "",
) -> str:
    """Format search results into numbered list."""
    header = f'Search: "{query}" | page {page} | {total} results'
    if note:
        header = f"{header}\n{note}"
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


def _parse_search_html(html: str, query: str, page: int, sort: str = "default") -> dict | None:
    """Extract search results from HTML containing __page__data_sse10.

    Returns formatted content dict or None if extraction fails. A price sort
    orders this page's offers here (``_SORT_MAP``) and says so in the header.
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

    note = ""
    if sort in _LOCAL_PRICE_SORTS:
        descending = sort == "price_desc"
        offers = _order_by_price(offers, descending=descending)
        note = (
            f"Alibaba.com has no price sort: these are its page {page} offers in "
            f"relevance order, re-ordered here by lowest listed price "
            f"({'highest' if descending else 'lowest'} first). Other pages can hold "
            "cheaper or dearer offers; min_price/max_price filter on Alibaba's side."
        )

    content = _format_search_results(offers, query, page, total_count, note)
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
        sort: Sort order (default, orders, price_asc, price_desc). The price
            sorts order the returned page here; Alibaba has none.
        min_price: Minimum price filter, in the results' currency.
        max_price: Maximum price filter, in the results' currency.
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

            async def read(url: str) -> dict:
                # Domain-level rate limiting (shared with alibaba product).
                # extra_delay=2.0 → 4.0 base + 2.0 = 6.0s minimum between
                # requests. Lock contention, spacing, and Retry-After deferral
                # consume the same advertised end-to-end budget as the fetch.
                await alibaba_limiter.wait(extra_delay=2.0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                return await fetch_url(
                    url=url,
                    max_tokens=500000,  # Full page for JSON extraction
                    timeout=remaining,
                    raw=True,  # Get raw HTML, not markdown
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                    _skip_alibaba_intercept=True,
                )

            html: str | None = None
            listing_url = _products_url(query, page, sort, min_price, max_price)
            if listing_url is not None:
                result = await read(listing_url)
                if result.get("error") == "HTTP 404":
                    _log("listing page answered 404; using /trade/search")
                elif "error" in result:
                    return result
                else:
                    html = result.get("content", "")
                    keyword = await asyncio.to_thread(_searched_keyword, html)
                    if keyword is not None and (
                        _normalized_query(keyword).casefold()
                        != _normalized_query(query).casefold()
                    ):
                        # The slug carried a different query than the one asked.
                        _log("listing page searched a different keyword; using /trade/search")
                        html = None

            if html is None:
                trade_url = _trade_search_url(query, page, sort, min_price, max_price)
                for attempt in range(_TRADE_SEARCH_ATTEMPTS):
                    result = await read(trade_url)
                    if "error" in result:
                        return result
                    html = result.get("content", "")
                    if not is_pro_search_shell(html):
                        break
                    _log(f"/trade/search answered with the pro-search shell ({attempt + 1}/{_TRADE_SEARCH_ATTEMPTS})")
                else:
                    return {
                        "error": (
                            f'Alibaba.com answered "{query}" with its "pro search" app page, '
                            f"which carries no results, on all {_TRADE_SEARCH_ATTEMPTS} tries. "
                            "It does this to about half of its searches at random, so "
                            "retrying may return results."
                        )
                    }

            parsed = await asyncio.to_thread(
                _parse_search_html,
                html,
                query,
                page,
                sort,
            )
            zero_result = parsed is None and await asyncio.to_thread(
                is_zero_result_page,
                html,
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

    if zero_result:
        # Alibaba's own answer, so report it as that rather than as a page we
        # failed to read. It is not rendered as an empty result: on 2026-10-08
        # the same "led strip" URL drew this page on some requests and 100,000+
        # results on others, a minute apart, on one session.
        _log("zero-result page in response")
        return {
            "error": (
                f'Alibaba.com answered that no products match "{query}" '
                '(its "did not match any products" page). Alibaba also serves '
                "that page intermittently for searches that do have results, "
                "so retrying may return them."
            )
        }

    _log("no search data in response")
    return {"error": "Alibaba.com search failed. Could not extract product data from response."}
