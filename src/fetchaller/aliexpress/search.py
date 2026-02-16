"""AliExpress search via Chrome with session warming.

AliExpress search pages are always TMD-blocked for curl_cffi — even with
cached cookies. The only reliable path is Chrome: visit the homepage first
(establishes a valid session), then navigate to the search page to extract
``_init_data_`` JSON from the rendered DOM.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from urllib.parse import quote

from ..content.aliexpress import extract_init_data, format_search_results
from ..ratelimit import aliexpress_limiter


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] aliexpress search: {msg}", file=sys.stderr)

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


async def _poll_and_extract(tab, url: str, query: str) -> dict | str | None:
    """Poll for _init_data_ on the current page and extract results.

    Returns:
        dict with "content" on success, "tmd" string if TMD-blocked, None on failure.
    """
    poll_interval = 0.5
    max_polls = 20  # 10 seconds total
    for i in range(max_polls):
        await asyncio.sleep(poll_interval)

        # Check for TMD punish (appears as a redirect)
        try:
            url_resp = await tab.execute_script(
                "return window.location.href", return_by_value=True,
            )
            current_url = url_resp.get("result", {}).get("result", {}).get("value", "")
            if "_____tmd_____" in current_url:
                _log("Chrome: TMD punish detected")
                return "tmd"
        except Exception:
            continue

        # Check if _init_data_ has been written to the page
        try:
            has_data = await tab.execute_script(
                "return !!(window._dida_config_ && window._dida_config_._init_data_)",
                return_by_value=True,
            )
            if has_data.get("result", {}).get("result", {}).get("value"):
                _log(f"Chrome: _init_data_ found after {(i + 1) * poll_interval:.1f}s")
                break
        except Exception:
            continue
    else:
        _log("Chrome: _init_data_ not found after polling")

    # Extract full HTML and parse
    try:
        html_resp = await tab.execute_script(
            "return document.documentElement.outerHTML",
            return_by_value=True,
        )
        html = html_resp.get("result", {}).get("result", {}).get("value", "")
        if html:
            result = _parse_search_html(html, query)
            if result:
                _log("Chrome: extracted products from rendered HTML")
                return result
    except Exception as e:
        _log(f"Chrome HTML extraction failed: {e}")

    return None


async def _search_via_chrome(url: str, query: str, challenge_solver) -> dict | None:
    """Search AliExpress via Chrome, reusing existing session when possible.

    Navigates directly to the search URL — if the Chrome instance already has
    session cookies from a previous request, this works immediately (like a
    real user searching again). If TMD blocks us (no valid session), warms up
    by visiting the homepage first, then retries the search.
    """
    async def _on_search_page(tab):
        """with_page already loaded the search URL. Poll and extract."""
        result = await _poll_and_extract(tab, url, query)

        if result == "tmd":
            # No valid session yet — warm up from homepage and retry
            _log("Chrome: no session, warming up from homepage")
            await tab.go_to("https://www.aliexpress.com/")
            await asyncio.sleep(3)

            _log("Chrome: retrying search after session warming")
            await tab.go_to(url)
            result = await _poll_and_extract(tab, url, query)
            if result == "tmd":
                _log("Chrome: TMD punish even after session warming")
                return None

        return result if isinstance(result, dict) else None

    # with_page loads the search URL, waits briefly for SPA init, then
    # calls our callback. No homepage detour if session already exists.
    return await challenge_solver.with_page(
        url, _on_search_page, wait=1, timeout=30,
    )


async def search_aliexpress(
    query: str,
    page: int = 1,
    sort: str = "default",
    min_price: float | None = None,
    max_price: float | None = None,
    fetcher=None,
    cache=None,
    config=None,
    cookie_cache=None,
    challenge_solver=None,
) -> dict:
    """Search AliExpress products via Chrome with session warming.

    Always uses Chrome — curl_cffi is guaranteed to get TMD-blocked on
    AliExpress search pages. Chrome visits the homepage first (establishes
    session cookies), then navigates to the search page.

    Args:
        query: Search query string.
        page: Page number (1-indexed).
        sort: Sort order (default, orders, price_asc, price_desc).
        min_price: Minimum price filter.
        max_price: Maximum price filter.
        fetcher: Unused (kept for interface compatibility).
        cache: Unused (kept for interface compatibility).
        config: Unused (kept for interface compatibility).
        cookie_cache: Unused (kept for interface compatibility).
        challenge_solver: ChallengeSolver for browser-based session warming.

    Returns:
        Dict with "content" (formatted results) or "error".
    """
    if not challenge_solver:
        return {"error": "AliExpress search requires Chrome (challenge_solver not available)."}

    # Domain-level rate limiting (shared with aliexpress product).
    # extra_delay=2.0 → 3.0 base + 2.0 = 5.0s minimum between search requests.
    await aliexpress_limiter.wait(extra_delay=2.0)

    url = _build_search_url(query, page, sort, min_price, max_price)

    result = await _search_via_chrome(url, query, challenge_solver)
    if result:
        return result

    return {"error": "AliExpress search failed. Anti-bot protection blocked the request."}
