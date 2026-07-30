"""AliExpress search via wafer HTTP client.

AliExpress search pages embed product data in ``_init_data_`` JSON within the
HTML. wafer's AsyncSession handles TMD/WAF challenges transparently via
BrowserSolver, so we fetch the search page directly and extract the data.
"""

from __future__ import annotations

import asyncio
import math
import sys
import threading
import time
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import wafer

from ..config import get_wafer_cache_dir
from ..content.aliexpress import (
    extract_init_data,
    format_search_results,
    search_product_snapshot,
    valid_search_products,
)
from ..ratelimit import aliexpress_limiter
from ..security.xss import safe_log_text


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] aliexpress search: {safe_log_text(msg)}",
        file=sys.stderr,
    )


# Module-level session — reuses TLS identity and cookies across searches.
# browser_solver is set on first call (passed from server singleton).
_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()
_snapshot_lock = threading.Lock()
_product_snapshots: dict[str, tuple[float, dict]] = {}
_PRODUCT_SNAPSHOT_TTL = 15 * 60.0
_MAX_PRODUCT_SNAPSHOTS = 120


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=10 * 1024 * 1024,
                )
    return _session


async def close_session() -> None:
    """Release the shared session (for shutdown cleanup)."""
    global _session
    _session = None
    _clear_product_snapshots()


def _remember_search_products(products: list[dict]) -> None:
    """Retain bounded real listing data for an immediately-following detail."""

    now = time.monotonic()
    snapshots = [
        snapshot
        for product in products
        if (snapshot := search_product_snapshot(product)) is not None
    ]
    with _snapshot_lock:
        expired = [
            product_id
            for product_id, (created, _) in _product_snapshots.items()
            if now - created > _PRODUCT_SNAPSHOT_TTL
        ]
        for product_id in expired:
            _product_snapshots.pop(product_id, None)
        for snapshot in snapshots:
            _product_snapshots[snapshot["product_id"]] = (now, snapshot)
        if len(_product_snapshots) > _MAX_PRODUCT_SNAPSHOTS:
            oldest = sorted(
                _product_snapshots,
                key=lambda product_id: _product_snapshots[product_id][0],
            )
            excess = len(_product_snapshots) - _MAX_PRODUCT_SNAPSHOTS
            for product_id in oldest[:excess]:
                _product_snapshots.pop(product_id, None)


def get_recent_product_snapshot(product_id: str) -> dict | None:
    """Return an isolated, unexpired snapshot for the exact product ID."""

    now = time.monotonic()
    with _snapshot_lock:
        entry = _product_snapshots.get(product_id)
        if entry is None:
            return None
        created, snapshot = entry
        if now - created > _PRODUCT_SNAPSHOT_TTL:
            _product_snapshots.pop(product_id, None)
            return None
        return dict(snapshot)


def _clear_product_snapshots() -> None:
    """Clear process snapshots for deterministic lifecycle tests."""

    with _snapshot_lock:
        _product_snapshots.clear()


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
    params: list[tuple[str, object]] = [("page", page)]
    if sort_type != "default":
        params.append(("sortType", sort_type))
    if min_price is not None:
        params.append(("minPrice", min_price))
    if max_price is not None:
        params.append(("maxPrice", max_price))
    if params:
        url += "?" + urlencode(params, quote_via=quote)
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
        products = valid_search_products(item_list.get("content", []))
        page_info = root_fields.get("pageInfo", {})
        total = page_info.get("totalResults", len(products))
        page = page_info.get("page", 1)
    except (AttributeError, KeyError, TypeError) as e:
        _log(f"unexpected _init_data_ structure: {type(e).__name__}")
        return None

    if not products:
        _log("embedded item list contained no substantive priced products")
        return None

    _remember_search_products(products)
    content = format_search_results(products, query, page, total)
    return {"content": content}


async def search_aliexpress(
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
        timeout: End-to-end request and browser-challenge timeout in seconds.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: BrowserSolver for challenge solving.

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
    if not browser_solver:
        return {"error": "AliExpress search requires a browser solver (not available). Install wafer-py[browser]."}

    deadline = time.monotonic() + timeout
    try:
        async with asyncio.timeout(timeout):
            # Domain-level rate limiting (shared with aliexpress product).
            # Lock contention, spacing, and Retry-After deferral consume the
            # same advertised end-to-end budget as fetch and challenge solve.
            await aliexpress_limiter.wait(extra_delay=2.0)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            url = _build_search_url(query, page, sort, min_price, max_price)
            session = await _get_session(browser_solver)
            resp = await session.get(
                url,
                headers={"Referer": "https://www.aliexpress.com/"},
                timeout=remaining,
            )

            if resp.status_code >= 400:
                _log(f"HTTP {resp.status_code} for search request")
                return {"error": f"AliExpress search returned HTTP {resp.status_code}"}

            html = await asyncio.to_thread(lambda: resp.text)
            result = await asyncio.to_thread(_parse_search_html, html, query)
            if result:
                return result

            # _init_data_ not found — page might be a challenge page or empty
            if "_____tmd_____" in html:
                _log("TMD punish page in response (challenge not solved)")
                return {"error": "AliExpress search blocked by TMD bot protection."}
    except wafer.ChallengeDetected as e:
        _log(f"challenge not solved: {e.challenge_type}")
        return {"error": f"AliExpress search blocked by {e.challenge_type} bot protection."}
    except wafer.WaferError as e:
        _log(f"wafer error: {type(e).__name__}")
        return {"error": f"AliExpress search failed: {e}"}
    except TimeoutError:
        return {
            "error": (f"Request timed out after {timeout}s. Try increasing the timeout parameter for slow servers.")
        }

    _log(f"could not extract substantive product data ({len(html)} chars)")
    return {"error": "AliExpress search failed. Could not extract product data from response."}
