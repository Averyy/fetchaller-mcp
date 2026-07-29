"""Craigslist search via SAPI.

Primary path uses ``sapi.craigslist.org`` for structured JSON results (up to
120 items with total count). Falls back to standard fetchaller HTML pipeline
if SAPI fails.

Flow:
1. Extract query/category/params from the CL search URL
2. Resolve area ID (cached in memory, extracted from CL page HTML on miss)
3. Call SAPI → parse items → format as numbered markdown
4. If SAPI fails, return error (fetch.py falls through to HTML pipeline)
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from ..security.xss import safe_log_text
from .sapi import (
    cache_area_id,
    extract_area_id,
    fetch_sapi,
    get_area_name,
    get_cached_area_id,
    get_total_count,
    parse_sapi_items,
)


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] craigslist search: "
        f"{safe_log_text(msg)}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# /search/sss?query=bicycle or /search/cta?query=car
_SEARCH_PATH_RE = re.compile(r"^/search/([a-z]{2,4})")


def extract_search_params(url: str) -> dict | None:
    """Extract search parameters from a CL search URL.

    Returns dict with ``query``, ``category``, ``hostname``, and
    ``extra_params`` (forwarded to SAPI), or None if not a CL search URL.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not (hostname.endswith(".craigslist.org") or hostname == "craigslist.org"):
        return None

    m = _SEARCH_PATH_RE.match(parsed.path)
    if not m:
        return None

    qs = parse_qs(parsed.query)

    # Extract all single-value query params for SAPI forwarding
    extra_params: dict[str, str] = {}
    for key, values in qs.items():
        if key != "query" and values:
            extra_params[key] = values[0]

    return {
        "query": qs.get("query", [""])[0],
        "category": m.group(1),
        "hostname": hostname,
        "extra_params": extra_params,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_item(idx: int, item: dict) -> str:
    """Format a single search result item."""
    title = item.get("title", "Untitled")
    parts: list[str] = []

    price = item.get("price_str", "")
    if price:
        parts.append(price)

    location = item.get("location", "")
    if location:
        parts.append(location)

    posted = item.get("posted_str", "")
    if posted:
        parts.append(posted)

    line = f"{idx}. {title}"
    if parts:
        line += f"\n   {' | '.join(parts)}"

    item_url = item.get("url", "")
    if item_url:
        line += f"\n   {item_url}"

    return line


def format_search_results(
    items: list[dict],
    query: str,
    area: str = "",
    total: int | None = None,
) -> str:
    """Format extracted items into numbered markdown list."""
    header_parts = []
    if query:
        header_parts.append(f'"{query}"')
    if area:
        header_parts.append(area)

    # Use total from SAPI if available, otherwise count of returned items
    count = total if total is not None else len(items)
    if total is not None and len(items) < total:
        header_parts.append(f"showing {len(items)} of {count:,}")
    else:
        header_parts.append(f"{count:,} results")

    header = f"Craigslist: {' | '.join(header_parts)}"

    if not items:
        return f"{header}\n\nNo listings found."

    formatted = [_format_item(i + 1, item) for i, item in enumerate(items)]
    return f"{header}\n\n" + "\n\n".join(formatted)


# ---------------------------------------------------------------------------
# Area ID resolution
# ---------------------------------------------------------------------------


async def _resolve_area_id(
    hostname: str,
    url: str,
    config=None,
    browser_solver=None,
) -> int | None:
    """Resolve hostname to area ID, using cache or fetching HTML.

    Returns area_id or None if resolution fails.
    """
    # Check memory cache first
    cached = get_cached_area_id(hostname)
    if cached is not None:
        return cached

    # Fetch the CL search page HTML to extract area ID
    _log(f"Resolving area ID for {hostname}")

    from ..tools.fetch import fetch_url

    page_result = await fetch_url(
        url=url,
        max_tokens=500000,
        timeout=15,
        raw=True,
        cache=None,
        config=config,
        browser_solver=browser_solver,
        _skip_craigslist_intercept=True,
    )

    if "error" in page_result:
        _log("Failed to fetch CL page for area ID")
        return None

    html = page_result.get("content", "")
    area_id = extract_area_id(html)
    if area_id:
        cache_area_id(hostname, area_id)
        _log(f"Resolved {hostname} → area_id={area_id}")
    else:
        _log(f"Could not extract area ID from {hostname} page ({len(html)} chars)")

    return area_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def search_craigslist(
    url: str,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Search Craigslist via SAPI.

    Args:
        url: Craigslist search URL (e.g., ``https://chicago.craigslist.org/search/sss?query=bicycle``).
        cache: ResponseCache instance (unused, reserved for interface compat).
        config: Config instance.
        browser_solver: BrowserSolver for browser-based challenges.

    Returns:
        Dict with ``content`` (formatted results) or ``error``.
    """
    params = extract_search_params(url)
    if not params:
        return {"error": f"Could not extract search parameters from URL: {url}"}

    # Resolve area ID (cached after first call per hostname)
    area_id = await _resolve_area_id(
        params["hostname"],
        url,
        config=config,
        browser_solver=browser_solver,
    )
    if not area_id:
        return {"error": "Could not resolve Craigslist area ID. Falling back to HTML."}

    # Build SAPI extra params (query goes in extra_params too)
    extra_params = params.get("extra_params", {})
    if params["query"]:
        extra_params["query"] = params["query"]

    # Call SAPI
    sapi_data = await fetch_sapi(
        area_id=area_id,
        category=params["category"],
        extra_params=extra_params,
    )
    if not sapi_data:
        return {"error": "Could not extract search results from Craigslist SAPI."}

    # Parse items from SAPI response
    items = parse_sapi_items(sapi_data)
    total = get_total_count(sapi_data)
    area_name = get_area_name(sapi_data)

    content = format_search_results(items, params["query"], area_name, total)
    return {"content": content}
