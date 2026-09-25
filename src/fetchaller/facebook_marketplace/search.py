"""Facebook Marketplace search for a Marketplace URL.

The visible markup is CSR with obfuscated CSS and is never scraped. What is
read is the structured data the page ships with (see ``page.py``).

Flow:
1. GET the page the URL names. A search page carries the query Facebook ran
   for it (location resolved, URL filters applied) and streams its first page
   of results; those are rendered as they are.
2. If the page ran the search but did not stream it, replay its own query.
3. Otherwise (a city browse page, a login wall, a failed GET) build the search:
   coordinates from the page when it gave them, else geocode the slug, and the
   URL's prices, which are whole units, converted to the cents GraphQL takes.
4. Format results as markdown.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from ..content.facebook_marketplace import (
    extract_location_from_url,
    extract_price_filters,
    extract_search_query,
)
from .graphql import (
    DOC_ID_SEARCH,
    build_search_variables,
    geocode_location,
    graphql_request,
    parse_search_response,
    withheld_listings_error,
)
from .page import fetch_search_page


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] fb marketplace search: {msg}", file=sys.stderr)


# Default location (Vancouver, BC) used when no location can be determined
_DEFAULT_LAT = 49.2827
_DEFAULT_LNG = -123.1207
_DEFAULT_LOCATION_NAME = "Vancouver, BC"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_listing(idx: int, item: dict) -> str:
    """Format a single search result listing."""
    title = item.get("title", "Untitled")
    parts: list[str] = []

    price = item.get("price", "")
    original_price = item.get("original_price", "")
    if price and original_price:
        parts.append(f"{price} (was {original_price})")
    elif price:
        parts.append(price)

    location = item.get("location", "")
    if location:
        parts.append(location)

    condition = item.get("condition", "")
    if condition:
        parts.append(condition)

    status = item.get("status", "")
    if status:
        parts.append(status)

    line = f"{idx}. **{title}**"
    if parts:
        line += f"\n   {' | '.join(parts)}"

    url = item.get("url", "")
    if url:
        line += f"\n   {url}"

    return line


def format_search_results(
    listings: list[dict],
    query: str,
    location_name: str = "",
    radius_km: int = 50,
) -> str:
    """Format search results as numbered markdown list."""
    header_parts = []
    if query:
        header_parts.append(f'"{query}"')
    if location_name:
        header_parts.append(location_name)
    header_parts.append(f"{radius_km}km")

    header = f"Facebook Marketplace: {' | '.join(header_parts)}"

    if not listings:
        return f"{header}\n\nNo listings found."

    formatted = [_format_listing(i + 1, item) for i, item in enumerate(listings)]
    return f"{header}\n\n" + "\n\n".join(formatted)


def format_browse_results(listings: list[dict], location_name: str, radius_km: int) -> str:
    """A city browse page's feed: what facebook.com shows before any scroll."""
    header = (
        f"Facebook Marketplace: {location_name} | {radius_km}km | browse feed, first screen "
        f"({len(listings)} listing{'s' if len(listings) != 1 else ''}, no search term)"
    )
    formatted = [_format_listing(i + 1, item) for i, item in enumerate(listings)]
    return f"{header}\n\n" + "\n\n".join(formatted)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _run_search(doc_id: str, variables: dict) -> tuple[dict | None, str | None]:
    try:
        data = await graphql_request(doc_id, variables)
    except Exception as e:
        error_str = str(e)
        # IP reputation block (error 1675004)
        if "1675004" in error_str or "rate limit" in error_str.lower():
            return None, (
                "Facebook Marketplace blocked this request (IP reputation). "
                "Try again later or use a different network."
            )
        _log(f"GraphQL request failed: {e}")
        return None, f"Facebook Marketplace search failed: {e}"
    errors = data.get("errors", [])
    if errors:
        err_msg = errors[0].get("message", "Unknown error")
        _log(f"GraphQL error: {err_msg}")
        return None, f"Facebook Marketplace error: {err_msg}"
    return data, None


async def search_marketplace(url: str) -> dict:
    """Search Facebook Marketplace for a Marketplace URL.

    Args:
        url: Facebook Marketplace search or city-browse URL.

    Returns:
        Dict with ``content`` (formatted results) or ``error``.
    """
    query = extract_search_query(url)
    location_slug = extract_location_from_url(url)
    min_price, max_price = extract_price_filters(url)

    page = await fetch_search_page(url)
    location_name = (page.city if page else "") or ""
    radius_km = 50

    if page is not None and page.browse is not None and not query:
        radius_km = page.radius_km or radius_km
        if not page.browse:
            return {
                "error": (
                    f"Facebook's browse page for {location_name or location_slug or 'this location'} "
                    "carried no listings. Add a search term (/marketplace/<city>/search?query=...) "
                    "to search instead."
                )
            }
        return {"content": format_browse_results(page.browse, location_name or location_slug, radius_km)}

    if page is not None and page.result is not None:
        data = page.result
        radius_km = page.radius_km or radius_km
    elif page is not None and page.doc_id and page.variables is not None:
        data, error = await _run_search(page.doc_id, page.variables)
        if error:
            return {"error": error}
        radius_km = page.radius_km or radius_km
    else:
        lat, lng = _DEFAULT_LAT, _DEFAULT_LNG
        if page is not None and page.latitude is not None and page.longitude is not None:
            lat, lng = page.latitude, page.longitude
        elif location_slug:
            # Free text is the last resort: it is how "vancouver" became
            # Vancouver, Washington. The page's own resolution is preferred.
            geo = await geocode_location(location_slug.replace("-", " "))
            if geo and geo.get("latitude") is not None and geo.get("longitude") is not None:
                lat, lng = geo["latitude"], geo["longitude"]
                location_name = location_name or geo.get("name", location_slug)
        if not location_name:
            location_name = location_slug.replace("-", " ").title() if location_slug else _DEFAULT_LOCATION_NAME

        variables = build_search_variables(
            query=query or "",
            latitude=lat,
            longitude=lng,
            radius_km=radius_km,
            count=24,
            min_price=min_price * 100 if min_price is not None else None,
            max_price=max_price * 100 if max_price is not None else None,
        )
        data, error = await _run_search(DOC_ID_SEARCH, variables)
        if error:
            return {"error": error}

    if not location_name:
        location_name = location_slug.replace("-", " ").title() if location_slug else ""

    listings = parse_search_response(data)
    withheld = withheld_listings_error(data, listings)
    if withheld:
        _log(withheld)
        return {"error": withheld}
    content = format_search_results(listings, query, location_name, radius_km)
    return {"content": content}
