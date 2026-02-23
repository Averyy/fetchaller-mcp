"""Unified marketplace search across Craigslist, Kijiji, and Facebook Marketplace.

Searches all three platforms concurrently with human-readable parameters
(city name, category alias, sort alias) and returns grouped markdown results.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from .aliases import (
    CATEGORY_MAP,
    CONDITION_MAP,
    SORT_MAP,
    resolve_alias,
)


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] marketplace: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Per-platform search functions
# ---------------------------------------------------------------------------


async def _search_kijiji(
    query: str,
    location: str,
    category: str | None,
    sort: str | None,
    condition: str | None,
    min_price: int | None,
    max_price: int | None,
) -> dict:
    """Search Kijiji via GraphQL.

    Returns {"platform": "kijiji", "items": [...], "total": N, "header": str}
    or {"platform": "kijiji", "error": str}.
    """
    from ..kijiji.api import get_listing
    from ..kijiji.locations import build_search_url, resolve_location

    loc = resolve_location(location)
    if loc is None:
        return {"platform": "kijiji", "error": f"Could not resolve location: {location}"}

    location_id, display_name = loc

    # Resolve aliases
    kj_category = resolve_alias(category, CATEGORY_MAP, "kijiji") or "c10"
    kj_sort = resolve_alias(sort, SORT_MAP, "kijiji")
    kj_condition = resolve_alias(condition, CONDITION_MAP, "kijiji")

    url = build_search_url(
        query=query,
        location_id=location_id,
        category_code=kj_category,
        sort=kj_sort,
        min_price=min_price,
        max_price=max_price,
        condition=kj_condition,
    )

    _log(f"Kijiji search: {url}")
    result = await get_listing(url)

    if "error" in result:
        return {"platform": "kijiji", "error": result["error"]}

    return {
        "platform": "kijiji",
        "content": result["content"],
    }


async def _search_craigslist(
    query: str,
    location: str,
    category: str | None,
    sort: str | None,
    condition: str | None,
    min_price: int | None,
    max_price: int | None,
    fetcher=None,
    config=None,
    cookie_cache=None,
    challenge_solver=None,
) -> dict:
    """Search Craigslist via SAPI.

    Returns {"platform": "craigslist", "content": str}
    or {"platform": "craigslist", "error": str}.
    """
    from ..craigslist.locations import resolve_hostname
    from ..craigslist.sapi import (
        fetch_sapi,
        get_area_name,
        get_total_count,
        parse_sapi_items,
    )
    from ..craigslist.search import _resolve_area_id

    hostname = resolve_hostname(location)
    if hostname is None:
        return {"platform": "craigslist", "error": f"Could not resolve location: {location}"}

    # Resolve aliases
    cl_category = resolve_alias(category, CATEGORY_MAP, "craigslist") or "sss"
    cl_sort = resolve_alias(sort, SORT_MAP, "craigslist")
    cl_condition = resolve_alias(condition, CONDITION_MAP, "craigslist")

    # Build a dummy URL for area_id resolution
    dummy_url = f"https://{hostname}/search/{cl_category}"

    area_id = await _resolve_area_id(
        hostname,
        dummy_url,
        fetcher=fetcher,
        config=config,
        cookie_cache=cookie_cache,
        challenge_solver=challenge_solver,
    )
    if not area_id:
        return {"platform": "craigslist", "error": "Could not resolve Craigslist area ID."}

    # Build SAPI params
    extra_params: dict[str, str | int] = {}
    if query:
        extra_params["query"] = query
    if cl_sort:
        extra_params["sort"] = cl_sort
    if cl_condition:
        extra_params["condition"] = cl_condition
    if min_price is not None:
        extra_params["min_price"] = min_price
    if max_price is not None:
        extra_params["max_price"] = max_price

    sapi_data = await fetch_sapi(
        area_id=area_id,
        category=cl_category,
        extra_params=extra_params,
    )
    if not sapi_data:
        return {"platform": "craigslist", "error": "Craigslist SAPI returned no data."}

    items = parse_sapi_items(sapi_data)
    total = get_total_count(sapi_data)
    area_name = get_area_name(sapi_data)

    from ..craigslist.search import format_search_results

    content = format_search_results(items, query, area_name, total)
    return {"platform": "craigslist", "content": content}


async def _search_facebook(
    query: str,
    location: str,
    category: str | None,
    sort: str | None,
    condition: str | None,
    min_price: int | None,
    max_price: int | None,
) -> dict:
    """Search Facebook Marketplace via GraphQL.

    Returns {"platform": "facebook", "content": str}
    or {"platform": "facebook", "error": str}.
    """
    from ..facebook_marketplace.graphql import (
        DOC_ID_SEARCH,
        build_search_variables,
        geocode_location,
        graphql_request,
        parse_search_response,
    )
    from ..facebook_marketplace.search import format_search_results

    # Geocode location
    geo = await geocode_location(location)
    if not geo or geo.get("latitude") is None or geo.get("longitude") is None:
        return {"platform": "facebook", "error": f"Could not geocode location: {location}"}

    lat = geo["latitude"]
    lng = geo["longitude"]
    location_name = geo.get("name", location)

    # Resolve aliases
    fb_sort = resolve_alias(sort, SORT_MAP, "facebook")
    fb_condition = resolve_alias(condition, CONDITION_MAP, "facebook")
    fb_category = resolve_alias(category, CATEGORY_MAP, "facebook")

    # FB prices are in cents
    fb_min_price = min_price * 100 if min_price is not None else None
    fb_max_price = max_price * 100 if max_price is not None else None

    variables = build_search_variables(
        query=query,
        latitude=lat,
        longitude=lng,
        radius_km=50,
        count=24,
        min_price=fb_min_price,
        max_price=fb_max_price,
        sort=fb_sort,
        condition=fb_condition,
        category_id=fb_category,
    )

    try:
        data = await graphql_request(DOC_ID_SEARCH, variables)
    except Exception as e:
        error_str = str(e)
        if "1675004" in error_str or "rate limit" in error_str.lower():
            return {
                "platform": "facebook",
                "error": "Facebook Marketplace blocked this request (IP reputation).",
            }
        _log(f"FB GraphQL error: {e}")
        return {"platform": "facebook", "error": f"Facebook Marketplace search failed: {e}"}

    errors = data.get("errors", [])
    if errors:
        err_msg = errors[0].get("message", "Unknown error")
        return {"platform": "facebook", "error": f"Facebook Marketplace error: {err_msg}"}

    listings = parse_search_response(data)
    content = format_search_results(listings, query, location_name)
    return {"platform": "facebook", "content": content}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_ALL_PLATFORMS = ("kijiji", "craigslist", "facebook")


async def search_marketplace(
    query: str,
    location: str,
    platforms: list[str] | None = None,
    category: str | None = None,
    sort: str = "date",
    condition: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    fetcher=None,
    config=None,
    cookie_cache=None,
    challenge_solver=None,
) -> dict:
    """Search multiple marketplace platforms concurrently.

    Args:
        query: Search keywords.
        location: City name (e.g. "st catharines", "toronto, ON").
        platforms: Platforms to search (default: all three).
        category: Category alias (e.g. "cars", "electronics", "all").
        sort: Sort alias (e.g. "date", "price_asc", "price_desc", "relevance").
        condition: Condition alias (e.g. "new", "like_new", "good", "fair").
        min_price: Minimum price in dollars.
        max_price: Maximum price in dollars.
        fetcher: ContentFetcher for HTTP requests.
        config: Config instance.
        cookie_cache: CookieCache for bot challenge cookies.
        challenge_solver: ChallengeSolver for browser-based challenges.

    Returns:
        Dict with "content" (grouped markdown) or "error".
    """
    # Determine which platforms to search
    if platforms:
        active = [p.lower() for p in platforms if p.lower() in _ALL_PLATFORMS]
    else:
        active = list(_ALL_PLATFORMS)

    if not active:
        return {"error": "No valid platforms specified. Use: kijiji, craigslist, facebook"}

    # Detect Canadian location — used for Kijiji skip and FB geocode disambiguation
    from ..craigslist.locations import is_canadian_location
    is_canadian = is_canadian_location(location)

    # Skip Kijiji for non-Canadian locations
    if "kijiji" in active and not is_canadian:
        active.remove("kijiji")
        _log(f"Skipping Kijiji for non-Canadian location: {location}")

    # Disambiguate location for Facebook geocoding (e.g. "vancouver" → Vancouver, WA without this)
    fb_location = location
    if is_canadian and "," not in location:
        fb_location = f"{location}, Canada"

    # Normalize category default
    if category is None:
        category = "all"

    # Launch platform searches concurrently
    tasks: list[asyncio.Task] = []
    platform_order: list[str] = []

    for platform in active:
        if platform == "kijiji":
            tasks.append(asyncio.ensure_future(
                _search_kijiji(query, location, category, sort, condition, min_price, max_price)
            ))
            platform_order.append("kijiji")
        elif platform == "craigslist":
            tasks.append(asyncio.ensure_future(
                _search_craigslist(
                    query, location, category, sort, condition, min_price, max_price,
                    fetcher=fetcher, config=config,
                    cookie_cache=cookie_cache, challenge_solver=challenge_solver,
                )
            ))
            platform_order.append("craigslist")
        elif platform == "facebook":
            tasks.append(asyncio.ensure_future(
                _search_facebook(query, fb_location, category, sort, condition, min_price, max_price)
            ))
            platform_order.append("facebook")

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=45,
        )
    except TimeoutError:
        _log("Marketplace search timed out after 45s")
        # Cancel any still-running tasks
        for t in tasks:
            if not t.done():
                t.cancel()
        # Collect whatever finished
        results = []
        for t in tasks:
            if t.done() and not t.cancelled():
                results.append(t.result())
            else:
                results.append(TimeoutError("Platform search timed out"))

    # Collect results, skip errors
    sections: list[str] = []
    errors: list[str] = []

    for i, result in enumerate(results):
        platform_name = platform_order[i]
        display_name = {
            "kijiji": "Kijiji",
            "craigslist": "Craigslist",
            "facebook": "Facebook Marketplace",
        }.get(platform_name, platform_name)

        if isinstance(result, Exception):
            _log(f"{platform_name} exception: {result}")
            errors.append(f"{display_name}: {result}")
            continue

        if "error" in result:
            _log(f"{platform_name} error: {result['error']}")
            errors.append(f"{display_name}: {result['error']}")
            continue

        content = result.get("content", "")
        if content:
            sections.append(f"## {display_name}\n\n{content}")

    if not sections:
        error_detail = "\n".join(f"- {e}" for e in errors) if errors else "No results found."
        return {"error": f"All platforms failed:\n{error_detail}"}

    # Build final output
    parts: list[str] = []

    # Header
    header_parts = [f'"{query}"']
    header_parts.append(location)
    if category and category != "all":
        header_parts.append(category)
    if min_price is not None or max_price is not None:
        price_range = ""
        if min_price is not None:
            price_range += f"${min_price:,}"
        price_range += "–"
        if max_price is not None:
            price_range += f"${max_price:,}"
        header_parts.append(price_range)

    parts.append(f"# Marketplace Search: {' | '.join(header_parts)}\n")
    parts.extend(sections)

    # Append errors as footnotes
    if errors:
        parts.append("\n---\n**Errors:**")
        for e in errors:
            parts.append(f"- {e}")

    return {"content": "\n\n".join(parts)}
