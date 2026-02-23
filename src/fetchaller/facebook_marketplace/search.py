"""Facebook Marketplace search via GraphQL.

Facebook Marketplace is 100% CSR with obfuscated CSS — HTML scraping is
not viable. The GraphQL API at ``/api/graphql/`` with known stable doc_ids
is the only option.

Flow:
1. Extract query, location, and price filters from the Marketplace URL
2. Geocode the location to lat/lng (if a city slug is present)
3. Call the search GraphQL endpoint
4. Format results as markdown
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
)


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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def search_marketplace(url: str) -> dict:
    """Search Facebook Marketplace via GraphQL.

    Args:
        url: Facebook Marketplace search URL.

    Returns:
        Dict with ``content`` (formatted results) or ``error``.
    """
    query = extract_search_query(url)
    location_slug = extract_location_from_url(url)
    min_price, max_price = extract_price_filters(url)

    # Geocode location
    lat = _DEFAULT_LAT
    lng = _DEFAULT_LNG
    location_name = _DEFAULT_LOCATION_NAME

    if location_slug:
        # Numeric location IDs don't need geocoding — but we don't have
        # a way to convert FB location IDs to coordinates without the
        # geocode API, so we geocode the slug as text.
        geo_query = location_slug.replace("-", " ")
        geo = await geocode_location(geo_query)
        if geo and geo.get("latitude") is not None and geo.get("longitude") is not None:
            lat = geo["latitude"]
            lng = geo["longitude"]
            location_name = geo.get("name", location_slug)
        else:
            location_name = location_slug.replace("-", " ").title()

    # Build and execute search query
    variables = build_search_variables(
        query=query or "",
        latitude=lat,
        longitude=lng,
        radius_km=50,
        count=24,
        min_price=min_price,
        max_price=max_price,
    )

    try:
        data = await graphql_request(DOC_ID_SEARCH, variables)
    except Exception as e:
        error_str = str(e)
        # IP reputation block (error 1675004)
        if "1675004" in error_str or "rate limit" in error_str.lower():
            return {
                "error": "Facebook Marketplace blocked this request (IP reputation). "
                "Try again later or use a different network."
            }
        _log(f"GraphQL request failed: {e}")
        return {"error": f"Facebook Marketplace search failed: {e}"}

    # Check for GraphQL errors
    errors = data.get("errors", [])
    if errors:
        err_msg = errors[0].get("message", "Unknown error")
        _log(f"GraphQL error: {err_msg}")
        return {"error": f"Facebook Marketplace error: {err_msg}"}

    # Parse and format
    listings = parse_search_response(data)
    content = format_search_results(listings, query, location_name)
    return {"content": content}
