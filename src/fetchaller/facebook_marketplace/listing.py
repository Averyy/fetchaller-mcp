"""Facebook Marketplace listing detail via GraphQL.

Fetches full listing details using the ``MarketplacePDPContainerQuery``
and ``MarketplacePDPC2CMediaViewerWithImagesQuery`` GraphQL queries.
No authentication required.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from .graphql import (
    DOC_ID_LISTING,
    DOC_ID_LISTING_IMAGES,
    build_listing_variables,
    graphql_request,
    parse_listing_images,
    parse_listing_response,
)


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] fb marketplace listing: {msg}", file=sys.stderr)


def _format_listing(listing: dict, images: list[dict]) -> str:
    """Format a listing dict as readable markdown."""
    lines: list[str] = []

    title = listing.get("title", "Untitled")
    lines.append(f"# {title}")
    lines.append("")

    # Price line
    price = listing.get("price", "")
    original = listing.get("original_price", "")
    if price and original:
        lines.append(f"**Price:** {price} ~~{original}~~")
    elif price:
        lines.append(f"**Price:** {price}")

    # Status
    status = listing.get("status", "")
    if status and status != "Available":
        lines.append(f"**Status:** {status}")

    # Condition
    condition = listing.get("condition", "")
    if condition:
        lines.append(f"**Condition:** {condition}")

    # Category
    category = listing.get("category", "")
    if category:
        lines.append(f"**Category:** {category}")

    # Location
    location = listing.get("location", "")
    if location:
        lines.append(f"**Location:** {location}")

    # Delivery
    delivery = listing.get("delivery", [])
    if delivery:
        delivery_str = ", ".join(d.replace("_", " ").title() for d in delivery)
        lines.append(f"**Delivery:** {delivery_str}")

    # Creation time
    creation_time = listing.get("creation_time")
    if creation_time:
        try:
            dt = datetime.fromtimestamp(creation_time, tz=UTC)
            lines.append(f"**Listed:** {dt.strftime('%B %d, %Y')}")
        except (ValueError, OSError):
            pass

    # Attributes
    attributes = listing.get("attributes", [])
    for attr in attributes:
        name = attr.get("name", "")
        value = attr.get("value", "")
        if name and value:
            lines.append(f"**{name}:** {value}")

    # Description
    description = listing.get("description", "")
    if description:
        lines.append("")
        lines.append("## Description")
        lines.append("")
        lines.append(description)

    # Images
    if images:
        lines.append("")
        lines.append(f"## Photos ({len(images)})")
        lines.append("")
        for i, img in enumerate(images, 1):
            alt = img.get("alt", "")
            alt_text = alt if alt and alt != "No photo description available." else f"Photo {i}"
            lines.append(f"- [{alt_text}]({img['uri']})")

    # URL
    url = listing.get("url", "")
    if url:
        lines.append("")
        lines.append(f"**Link:** {url}")

    return "\n".join(lines)


async def get_listing(listing_id: str) -> dict:
    """Fetch a single Marketplace listing via GraphQL.

    Makes two API calls: one for listing details, one for images.

    Args:
        listing_id: Numeric listing ID.

    Returns:
        Dict with ``content`` (formatted markdown) or ``error``.
    """
    # Fetch listing details
    try:
        variables = build_listing_variables(listing_id)
        data = await graphql_request(DOC_ID_LISTING, variables)
    except Exception as e:
        error_str = str(e)
        if "1675004" in error_str or "rate limit" in error_str.lower():
            return {
                "error": "Facebook Marketplace blocked this request (IP reputation). "
                "Try again later or use a different network."
            }
        _log(f"GraphQL request failed: {e}")
        return {"error": f"Facebook Marketplace listing request failed: {e}"}

    # Check for GraphQL errors
    errors = data.get("errors", [])
    if errors:
        err_msg = errors[0].get("message", "Unknown error")
        _log(f"GraphQL error: {err_msg}")
        return {"error": f"Facebook Marketplace error: {err_msg}"}

    # Parse listing
    listing = parse_listing_response(data)
    if not listing:
        return {"error": f"Could not parse listing {listing_id}. It may have been removed or is unavailable."}

    # Fetch images (separate query, non-fatal if it fails)
    images: list[dict] = []
    try:
        img_data = await graphql_request(
            DOC_ID_LISTING_IMAGES,
            {"targetId": listing_id},
        )
        images = parse_listing_images(img_data)
    except Exception as e:
        _log(f"Image fetch failed for {listing_id}: {e}")

    content = _format_listing(listing, images)
    return {"content": content}
