"""Facebook Marketplace GraphQL client.

Uses Facebook's public GraphQL endpoint (``/api/graphql/``) with known
stable doc_ids. No authentication required.

Known doc_ids:
- Location geocode: ``5585904654783609``
- Marketplace search: ``7111939778879383``
- Listing detail: ``34344688261796183``
- Listing images: ``10059604367394414``
"""

from __future__ import annotations

import json
import random
import sys
from datetime import UTC, datetime

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from ..config import BROWSER_FINGERPRINTS
from ..ratelimit import facebook_limiter

_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

# curl_cffi sets User-Agent, Sec-Ch-Ua, and TLS fingerprint natively
# via the impersonate parameter — do not set these manually.
_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.facebook.com",
    "Referer": "https://www.facebook.com/marketplace/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Module-level session for cookie persistence across requests.
# Facebook uses IP reputation + cookies — reusing a session with
# seeded cookies from visiting the marketplace page helps avoid blocks.
_session: AsyncSession | None = None
_session_seeded: bool = False


async def _get_session() -> AsyncSession:
    """Get or create the shared AsyncSession with browser impersonation.

    On first use, seeds the session by visiting the marketplace page
    to pick up anonymous session cookies (matches real browser behavior).
    """
    global _session, _session_seeded
    if _session is None:
        _session = AsyncSession(impersonate=random.choice(BROWSER_FINGERPRINTS))
        _session_seeded = False

    if not _session_seeded:
        try:
            _log("Seeding session cookies from marketplace page...")
            await _session.get(
                "https://www.facebook.com/marketplace/",
                timeout=15.0,
            )
            _session_seeded = True
            _log("Session cookies seeded.")
        except Exception as e:
            _log(f"Cookie seeding failed (non-fatal): {e}")
            _session_seeded = True  # Don't retry on every request

    return _session

# Known stable doc_ids
DOC_ID_GEOCODE = "5585904654783609"
DOC_ID_SEARCH = "7111939778879383"
DOC_ID_LISTING = "34344688261796183"
DOC_ID_LISTING_IMAGES = "10059604367394414"


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] fb marketplace: {msg}", file=sys.stderr)


async def graphql_request(doc_id: str, variables: dict) -> dict:
    """Make a GraphQL request to Facebook's API.

    Args:
        doc_id: The stable document ID for the query.
        variables: Query variables dict.

    Returns:
        Parsed JSON response dict.

    Raises:
        RequestsError: On HTTP/connection errors.
    """
    await facebook_limiter.wait()

    form_data = {
        "doc_id": doc_id,
        "variables": json.dumps(variables),
    }

    session = await _get_session()
    resp = await session.post(
        _GRAPHQL_URL,
        headers=_HEADERS,
        data=form_data,
        timeout=20.0,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except (ValueError, UnicodeDecodeError) as e:
        raise RequestsError(f"Invalid JSON response: {e}") from e


def build_search_variables(
    query: str,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: int = 50,
    count: int = 24,
    min_price: int | None = None,
    max_price: int | None = None,
    sort: str | None = None,
    condition: str | None = None,
    category_id: str | None = None,
) -> dict:
    """Build variables dict for marketplace search GraphQL query.

    Args:
        query: Search query string.
        latitude: Location latitude (required for results).
        longitude: Location longitude (required for results).
        radius_km: Search radius in kilometers.
        count: Number of results to return.
        min_price: Minimum price filter (cents).
        max_price: Maximum price filter (cents).
        sort: Sort order (e.g. "CREATION_TIME_DESCEND", "PRICE_ASCEND").
        condition: Condition filter (e.g. "new", "used_like_new").
        category_id: Category slug (e.g. "vehicles", "electronics").
    """
    browse_params: dict = {
        "bqf": {
            "callsite": "COMMERCE_MKTPLACE_WWW",
            "query": query,
        },
        "browse_request_params": {
            "commerce_enable_local_pickup": True,
            "commerce_enable_shipping": True,
            "commerce_search_and_rp_available": True,
            "commerce_search_and_rp_ctime_days": None,
            "filter_location_latitude": latitude,
            "filter_location_longitude": longitude,
            "filter_radius_km": radius_km,
            "filter_price_lower_bound": min_price if min_price is not None else 0,
            "filter_price_upper_bound": max_price if max_price is not None else 214748364700,
        },
        "custom_request_params": {
            "surface": "SEARCH",
            "search_vertical": "C2C",
        },
    }

    if condition:
        browse_params["browse_request_params"]["commerce_search_and_rp_condition"] = condition
    if sort:
        browse_params["browse_request_params"]["commerce_search_sort_by"] = sort

    variables: dict = {
        "count": count,
        "params": browse_params,
    }

    if category_id:
        variables["category_id"] = category_id

    return variables


def build_listing_variables(listing_id: str) -> dict:
    """Build variables dict for listing detail GraphQL query.

    Args:
        listing_id: Numeric listing ID.
    """
    return {
        "feedbackSource": 56,
        "feedLocation": "MARKETPLACE_MEGAMALL",
        "scale": 2,
        "targetId": listing_id,
        "useDefaultActor": False,
        # Relay provider flags required by the query schema
        "__relay_internal__pv__ShouldUpdateMarketplaceBoostListingBoostedStatusrelayprovider": False,
        "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": False,
        "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
        "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
        "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
        "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": False,
        "__relay_internal__pv__IsWorkUserrelayprovider": False,
        "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": False,
        "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": False,
        "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": False,
    }


def parse_search_response(data: dict) -> list[dict]:
    """Parse a marketplace search GraphQL response into listing dicts.

    Returns a list of listing dicts with standardized fields.
    """
    listings: list[dict] = []

    # Navigate the response structure — Facebook's GraphQL responses
    # are deeply nested with inconsistent shapes
    try:
        # Try the primary response path
        marketplace = data.get("data", {}).get("marketplace_search", {})
        edges = marketplace.get("feed_units", {}).get("edges", [])

        for edge in edges:
            node = edge.get("node", {})
            listing = node.get("listing", {})
            if not listing:
                continue

            # Extract fields
            item: dict = {
                "id": listing.get("id", ""),
                "title": listing.get("marketplace_listing_title", ""),
            }

            # Price — use pre-formatted amount from GraphQL
            price_info = listing.get("listing_price", {})
            if price_info:
                formatted = price_info.get("formatted_amount", "")
                if formatted:
                    item["price"] = formatted

            # Strikethrough (original) price
            strikethrough = listing.get("strikethrough_price")
            if strikethrough:
                original = strikethrough.get("formatted_amount", "")
                if original:
                    item["original_price"] = original

            # Location
            location = listing.get("location", {})
            if location:
                reverse = location.get("reverse_geocode", {})
                city = reverse.get("city", "")
                # Try display_name from city_page for more detail
                city_page = reverse.get("city_page", {})
                if city_page and city_page.get("display_name"):
                    city = city_page["display_name"]
                item["location"] = city

            # Condition
            condition = listing.get("condition_text")
            if condition:
                item["condition"] = condition

            # Status
            if listing.get("is_pending"):
                item["status"] = "Pending"

            # Seller info
            seller = listing.get("marketplace_listing_seller", {})
            if seller:
                seller_name = seller.get("name", "")
                if seller_name:
                    item["seller"] = seller_name

            # URL
            listing_id = listing.get("id", "")
            if listing_id:
                item["url"] = f"https://www.facebook.com/marketplace/item/{listing_id}/"

            # Image
            primary_photo = listing.get("primary_listing_photo", {})
            if primary_photo:
                item["image_url"] = primary_photo.get("image", {}).get("uri", "")

            listings.append(item)
    except (TypeError, AttributeError, KeyError) as e:
        _log(f"Failed to parse search response: {e}")

    return listings


def parse_listing_response(data: dict) -> dict | None:
    """Parse a listing detail GraphQL response into a structured dict.

    Returns a dict with listing fields, or None if parsing fails.
    """
    try:
        page = (
            data.get("data", {})
            .get("viewer", {})
            .get("marketplace_product_details_page", {})
        )
        if not page:
            return None

        target = page.get("target", {})
        if not target:
            return None

        listing_id = target.get("id", "")

        # Title
        title = target.get("marketplace_listing_title", "") or target.get("base_marketplace_listing_title", "")

        # Price
        price_info = target.get("listing_price", {})
        price = price_info.get("formatted_amount_zeros_stripped", "") if price_info else ""
        price_amount = price_info.get("amount", "") if price_info else ""
        price_currency = price_info.get("currency", "") if price_info else ""

        # Strikethrough price
        strikethrough = target.get("strikethrough_price")
        original_price = ""
        if strikethrough:
            original_price = strikethrough.get("formatted_amount_zeros_stripped", "")

        # Description
        desc_obj = target.get("redacted_description", {})
        description = desc_obj.get("text", "") if desc_obj else ""

        # Location
        location_obj = target.get("location_text", {})
        location = location_obj.get("text", "") if location_obj else ""

        # Condition + other attributes
        condition = ""
        attributes: list[dict] = []
        for attr in target.get("attribute_data", []):
            label = attr.get("label", "") or attr.get("value", "")
            name = attr.get("attribute_name", "")
            if name.lower() == "condition":
                condition = label
            else:
                attributes.append({"name": name, "value": label})

        # Category
        category = ""
        renderable = target.get("marketplaceListingRenderableIfLoggedOut", {})
        if renderable:
            category = renderable.get("marketplace_listing_category_name", "")

        # Taxonomy breadcrumb
        taxonomy_path = []
        seo_cat = (
            page.get("marketplace_listing_renderable_target", {})
            .get("seo_virtual_category", {})
        )
        if seo_cat:
            for path_item in seo_cat.get("taxonomy_path", []):
                seo_info = path_item.get("seo_info", {})
                if seo_info.get("seo_url"):
                    taxonomy_path.append(seo_info["seo_url"])

        # Status
        is_pending = target.get("is_pending", False)
        is_sold = target.get("is_sold", False)
        is_live = target.get("is_live", False)
        status = "Available"
        if is_sold:
            status = "Sold"
        elif is_pending:
            status = "Pending"
        elif not is_live:
            status = "Unavailable"

        # Delivery
        delivery_types = target.get("delivery_types", [])

        # Creation time
        creation_time = target.get("creation_time")

        # Comparable price (retail value)
        comparable_price = target.get("comparable_price")
        comparable_price_type = target.get("comparable_price_type")

        result: dict = {
            "id": listing_id,
            "title": title,
            "price": price,
            "description": description,
            "location": location,
            "status": status,
            "url": f"https://www.facebook.com/marketplace/item/{listing_id}/",
        }

        if original_price:
            result["original_price"] = original_price
        if condition:
            result["condition"] = condition
        if category:
            result["category"] = category
        if taxonomy_path:
            result["taxonomy"] = " > ".join(taxonomy_path)
        if attributes:
            result["attributes"] = attributes
        if delivery_types:
            result["delivery"] = delivery_types
        if creation_time:
            result["creation_time"] = creation_time
        if price_amount:
            result["price_amount"] = price_amount
        if price_currency:
            result["price_currency"] = price_currency
        if comparable_price:
            result["comparable_price"] = comparable_price
            result["comparable_price_type"] = comparable_price_type

        return result

    except (TypeError, AttributeError, KeyError) as e:
        _log(f"Failed to parse listing response: {e}")
        return None


def parse_listing_images(data: dict) -> list[dict]:
    """Parse listing images from the images query response.

    Returns a list of image dicts with ``uri``, ``width``, ``height``.
    """
    images: list[dict] = []
    try:
        target = (
            data.get("data", {})
            .get("viewer", {})
            .get("marketplace_product_details_page", {})
            .get("target", {})
        )
        for photo in target.get("listing_photos", []):
            img = photo.get("image", {})
            uri = img.get("uri", "")
            if uri:
                images.append({
                    "uri": uri,
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "alt": photo.get("accessibility_caption", ""),
                })
    except (TypeError, AttributeError, KeyError) as e:
        _log(f"Failed to parse listing images: {e}")
    return images


async def geocode_location(location_str: str) -> dict | None:
    """Geocode a location string using Facebook's geocode API.

    Returns dict with ``latitude``, ``longitude``, ``name`` or None on failure.
    """
    try:
        data = await graphql_request(
            DOC_ID_GEOCODE,
            {
                "params": {
                    "caller": "MARKETPLACE",
                    "page_category": [
                        "CITY",
                        "SUBCITY",
                        "NEIGHBORHOOD",
                        "POSTAL_CODE",
                    ],
                    "query": location_str,
                }
            },
        )
        results = (
            data.get("data", {})
            .get("city_street_search", {})
            .get("street_results", {})
            .get("edges", [])
        )
        if not results:
            return None

        node = results[0].get("node", {})
        return {
            "latitude": node.get("location", {}).get("latitude"),
            "longitude": node.get("location", {}).get("longitude"),
            "name": node.get("single_line_address", location_str),
        }
    except Exception as e:
        _log(f"Geocode failed for '{location_str}': {e}")
        return None
