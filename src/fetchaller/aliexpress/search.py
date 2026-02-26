"""AliExpress search via wafer HTTP client.

AliExpress search pages embed product data in ``_init_data_`` JSON within the
HTML. wafer's AsyncSession handles TMD/WAF challenges transparently via
BrowserSolver, so we fetch the search page directly and extract the data.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from urllib.parse import quote

import wafer

from ..config import get_wafer_cache_dir
from ..content.aliexpress import extract_init_data, format_search_results
from ..ratelimit import aliexpress_limiter


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] aliexpress search: {msg}", file=sys.stderr)


# Module-level session — reuses TLS identity and cookies across searches.
# browser_solver is set on first call (passed from server singleton).
_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    cache_dir=get_wafer_cache_dir(),
                )
    return _session


async def close_session() -> None:
    """Release the shared session (for shutdown cleanup)."""
    global _session
    _session = None

# Sort option mapping
_SORT_MAP = {
    "default": "default",
    "orders": "total_tranpro_desc",
    "price_asc": "price_asc",
    "price_desc": "price_desc",
}


def _build_search_url(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
) -> str:
    """Build AliExpress search URL from parameters."""
    query_slug = quote(query.replace(" ", "-"), safe="-")
    sort_type = _SORT_MAP.get(sort, "default")
    url = f"https://www.aliexpress.com/w/wholesale-{query_slug}.html"
    params = [f"page={page}"]
    if sort_type != "default":
        params.append(f"sortType={sort_type}")
    if min_price is not None:
        params.append(f"minPrice={min_price}")
    if max_price is not None:
        params.append(f"maxPrice={max_price}")
    if params:
        url += "?" + "&".join(params)
    return url


def _parse_search_html(html: str, query: str) -> dict | None:
    """Extract search results from HTML containing _init_data_.

    Returns formatted content dict or None if extraction fails.
    """
    init_data = extract_init_data(html)
    if not init_data:
        return None

    try:
        root_fields = init_data["data"]["root"]["fields"]
        mods = root_fields.get("mods", {})
        item_list = mods.get("itemList", {})
        products = item_list.get("content", [])
        page_info = root_fields.get("pageInfo", {})
        total = page_info.get("totalResults", len(products))
        page = page_info.get("page", 1)
    except (KeyError, TypeError) as e:
        _log(f"unexpected _init_data_ structure: {e}")
        return None

    if not products:
        return None

    content = format_search_results(products, query, page, total)
    return {"content": content}


async def search_aliexpress(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Search AliExpress products.

    Fetches the search page via wafer (which handles TMD/WAF challenges
    transparently via BrowserSolver) and extracts product data from the
    embedded ``_init_data_`` JSON.

    Args:
        query: Search query string.
        page: Page number (1-indexed).
        sort: Sort order (default, orders, price_asc, price_desc).
        min_price: Minimum price filter.
        max_price: Maximum price filter.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: BrowserSolver for challenge solving.

    Returns:
        Dict with "content" (formatted results) or "error".
    """
    if not browser_solver:
        return {"error": "AliExpress search requires a browser solver (not available). Install wafer-py[browser]."}

    # Domain-level rate limiting (shared with aliexpress product).
    # extra_delay=2.0 → 3.0 base + 2.0 = 5.0s minimum between search requests.
    await aliexpress_limiter.wait(extra_delay=2.0)

    url = _build_search_url(query, page, sort, min_price, max_price)

    session = await _get_session(browser_solver)
    try:
        resp = await session.get(
            url,
            headers={"Referer": "https://www.aliexpress.com/"},
            timeout=30,
        )
    except wafer.ChallengeDetected as e:
        _log(f"challenge not solved: {e.challenge_type}")
        return {"error": f"AliExpress search blocked by {e.challenge_type} bot protection."}
    except wafer.WaferError as e:
        _log(f"wafer error: {e}")
        return {"error": f"AliExpress search failed: {e}"}

    if resp.status_code >= 400:
        _log(f"HTTP {resp.status_code} for search query '{query}'")
        return {"error": f"AliExpress search returned HTTP {resp.status_code}"}

    html = resp.text
    result = _parse_search_html(html, query)
    if result:
        return result

    # _init_data_ not found — page might be a challenge page or empty
    if "_____tmd_____" in html:
        _log("TMD punish page in response (challenge not solved)")
        return {"error": "AliExpress search blocked by TMD bot protection."}

    _log(f"no _init_data_ found in response ({len(html)} chars)")
    return {"error": "AliExpress search failed. Could not extract product data from response."}
