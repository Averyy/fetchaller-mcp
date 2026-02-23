"""Kijiji GraphQL API client.

Uses Kijiji's unauthenticated Apollo GraphQL endpoint at
https://www.kijiji.ca/anvil/api for structured search results and
listing details. CSR pages have no useful SSR HTML — this bypasses
the HTML pipeline entirely for search/listing URLs.
"""

from __future__ import annotations

import random
import re
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from ..config import BROWSER_FINGERPRINTS
from ..ratelimit import kijiji_limiter

_API_URL = "https://www.kijiji.ca/anvil/api"

# curl_cffi sets User-Agent, Sec-Ch-Ua, and TLS fingerprint natively
# via the impersonate parameter — do not set these manually.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Language": "en_CA",
    "Origin": "https://www.kijiji.ca",
    "Referer": "https://www.kijiji.ca/",
    "Apollo-Require-Preflight": "true",
}

# Module-level session for cookie persistence across requests.
_session: AsyncSession | None = None


def _get_session() -> AsyncSession:
    """Get or create the shared AsyncSession with browser impersonation."""
    global _session
    if _session is None:
        _session = AsyncSession(impersonate=random.choice(BROWSER_FINGERPRINTS))
    return _session


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] kijiji api: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

# /b-category/location/query/k0c123l456 or /b-category/location/c123l456
_SEARCH_PATH_RE = re.compile(r"^/b-[^/]+/")

# /v-category/city/slug/1234567890
_LISTING_PATH_RE = re.compile(r"^/v-[^/]+/")

# Numeric ID at the end of listing URLs
_LISTING_ID_RE = re.compile(r"/(\d{8,})(?:[/?#]|$)")


def is_kijiji(url: str) -> bool:
    """Check if URL is any Kijiji page."""
    host = (urlparse(url).hostname or "").lower()
    return host in ("kijiji.ca", "www.kijiji.ca")


def is_kijiji_search(url: str) -> bool:
    """Check if URL is a Kijiji search/category page (/b-...)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("kijiji.ca", "www.kijiji.ca"):
        return False
    return bool(_SEARCH_PATH_RE.match(parsed.path))


def is_kijiji_listing(url: str) -> bool:
    """Check if URL is a Kijiji individual listing page (/v-...)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("kijiji.ca", "www.kijiji.ca"):
        return False
    return bool(_LISTING_PATH_RE.match(parsed.path))


def extract_listing_id(url: str) -> str | None:
    """Extract numeric listing ID from a Kijiji listing URL."""
    m = _LISTING_ID_RE.search(urlparse(url).path)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

# Search query: only use canonicalName/canonicalValues for attributes.
# Requesting `name` or `values` causes 500 errors on some attributes
# (pricerating, carmodel, srpLogoUrl) where the backend lacks translations.
_SEARCH_QUERY = """
query GetSearchResultsPageByUrl(
  $searchResultsByUrlInput: SearchResultsByUrlInput!,
  $pagination: PaginationInputV2!
) {
  searchResultsPageByUrl(input: $searchResultsByUrlInput) {
    pagination {
      totalCount
    }
    results {
      mainListings(pagination: $pagination) {
        id
        title
        imageCount
        url
        activationDate
        sortingDate
        location {
          name
          distance
        }
        price {
          type
          ... on AmountPrice {
            amount
            originalAmount
          }
        }
        posterInfo {
          posterId
          verified
        }
        attributes {
          all {
            canonicalName
            canonicalValues
          }
        }
      }
    }
  }
}
"""

_LISTING_QUERY = """
query GetListing($listingId: ID!) {
  listing(id: $listingId, trackView: false) {
    id
    title
    description
    activationDate
    imageUrls
    url
    categoryId
    price {
      type
      ... on StandardAmountPrice {
        amount
        currency
        originalAmount
      }
      ... on AutosDealerAmountPrice {
        amount
        currency
        originalAmount
        msrp
      }
    }
    location {
      name
      address
    }
    posterInfo {
      posterId
      sellerType
      verified
    }
    attributes {
      all {
        canonicalName
        canonicalValues
        name
        values
      }
    }
    metrics {
      views
    }
  }
}
"""


# ---------------------------------------------------------------------------
# GraphQL transport
# ---------------------------------------------------------------------------


async def _graphql(query: str, variables: dict) -> dict:
    """POST a GraphQL query to Kijiji's Apollo endpoint."""
    await kijiji_limiter.wait()
    session = _get_session()
    resp = await session.post(
        _API_URL,
        headers=_HEADERS,
        json={"query": query, "variables": variables},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

# Kijiji API returns prices in cents (e.g. 2349500 = $23,495.00).
_CENTS_DIVISOR = 100


def _format_price(price: dict | None) -> str:
    """Format a Kijiji price union type to a display string.

    Price types observed:
    - FIXED: standard priced listing (amount in cents)
    - FREE: free item
    - PLEASE_CONTACT: seller wants contact for price
    - SWAP_TRADE: swap/trade offer
    """
    if not price:
        return ""
    ptype = price.get("type", "")
    if ptype in ("FIXED", "SPECIFIED_AMOUNT"):
        amount = price.get("amount")
        if amount is not None:
            dollars = amount / _CENTS_DIVISOR
            currency = price.get("currency", "")
            formatted = f"${dollars:,.2f}"
            if currency and currency != "CAD":
                formatted += f" {currency}"
            original = price.get("originalAmount")
            if original and original != amount:
                formatted += f" (was ${original / _CENTS_DIVISOR:,.2f})"
            return formatted
    if ptype in ("FREE", "GIVE_AWAY"):
        return "Free"
    if ptype in ("PLEASE_CONTACT", "CONTACT"):
        return "Please Contact"
    if ptype == "SWAP_TRADE":
        return "Swap / Trade"
    return ""


def _format_date(iso_str: str | None) -> str:
    """Format ISO date to a compact display string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return iso_str


# Canonical attribute names used in search results for key info display.
# These use short codes (e.g. "awd" not "All-wheel drive (AWD)").
_KM_ATTRS = frozenset({"carmileageinkms", "mileage"})
_YEAR_ATTR = "caryear"
_DRIVETRAIN_ATTR = "drivetrain"

# Map canonical drivetrain codes to display names.
_DRIVETRAIN_DISPLAY = {
    "awd": "AWD",
    "fwd": "FWD",
    "rwd": "RWD",
    "4x4": "4x4",
}

# Rental attributes.
_BEDROOMS_ATTR = "numberbedrooms"
_BATHROOMS_ATTR = "numberbathrooms"  # stored as tenths (10=1, 15=1.5)
_UNITTYPE_ATTR = "unittype"
_AREA_ATTR = "areainfeet"

# General attributes shown in summaries.
_CONDITION_ATTR = "condition"

# Map canonical condition codes to display names.
_CONDITION_DISPLAY = {
    "new": "New",
    "usedlikenew": "Like New",
    "usedgood": "Used - Good",
    "usedfair": "Used - Fair",
    "usedpoor": "Used - Poor",
}


def _format_listing_summary(idx: int, listing: dict) -> str:
    """Format a single search result listing as a compact numbered item."""
    title = listing.get("title", "Untitled")
    price_str = _format_price(listing.get("price"))
    location = listing.get("location") or {}
    loc_name = location.get("name", "")
    distance = location.get("distance")

    # Extract key attributes for compact display
    attrs = (listing.get("attributes") or {}).get("all") or []
    attr_parts: list[str] = []
    for attr in attrs:
        canon = attr.get("canonicalName", "")
        vals = attr.get("canonicalValues") or []
        if not vals:
            continue
        if canon in _KM_ATTRS:
            try:
                km_val = int(vals[0])
                attr_parts.append(f"{km_val:,} km")
            except (ValueError, TypeError):
                attr_parts.append(f"{vals[0]} km")
        elif canon == _DRIVETRAIN_ATTR:
            display = _DRIVETRAIN_DISPLAY.get(str(vals[0]).lower(), vals[0])
            attr_parts.append(display)
        elif canon == _YEAR_ATTR:
            attr_parts.append(vals[0])
        elif canon == _BEDROOMS_ATTR:
            bd = vals[0]
            attr_parts.append("Bachelor" if bd == "0" else f"{bd} bd")
        elif canon == _BATHROOMS_ATTR:
            # Stored as tenths: 10=1, 15=1.5, 20=2
            try:
                raw = int(vals[0])
                ba = raw / 10
                ba_str = f"{ba:g}"  # e.g. "1" not "1.0", "1.5" stays "1.5"
                attr_parts.append(f"{ba_str} ba")
            except (ValueError, TypeError):
                attr_parts.append(f"{vals[0]} ba")
        elif canon == _UNITTYPE_ATTR:
            attr_parts.append(vals[0].replace("_", " ").title())
        elif canon == _AREA_ATTR:
            try:
                sqft = int(vals[0])
                if sqft > 0:
                    attr_parts.append(f"{sqft:,} sqft")
            except (ValueError, TypeError):
                pass
        elif canon == _CONDITION_ATTR:
            display = _CONDITION_DISPLAY.get(vals[0], vals[0])
            attr_parts.append(display)

    date_str = _format_date(listing.get("activationDate") or listing.get("sortingDate"))
    url = listing.get("url", "")

    parts: list[str] = []
    if price_str:
        parts.append(price_str)
    if loc_name:
        loc_display = loc_name
        if distance:
            loc_display += f" ({distance})"
        parts.append(loc_display)
    if attr_parts:
        parts.append(" | ".join(attr_parts))
    if date_str:
        parts.append(date_str)

    line = f"{idx}. **{title}**"
    if parts:
        line += f"\n   {' -- '.join(parts)}"
    if url:
        full_url = f"https://www.kijiji.ca{url}" if url.startswith("/") else url
        line += f"\n   {full_url}"

    return line


def _format_listing_detail(listing: dict) -> str:
    """Format a full listing detail as markdown."""
    lines: list[str] = []

    title = listing.get("title", "Untitled")
    lines.append(f"# {title}")
    lines.append("")

    price_str = _format_price(listing.get("price"))
    if price_str:
        lines.append(f"**Price:** {price_str}")

    location = listing.get("location") or {}
    loc_name = location.get("name", "")
    address = location.get("address", "")
    if address and address != loc_name:
        lines.append(f"**Location:** {address}")
    elif loc_name:
        lines.append(f"**Location:** {loc_name}")

    date_str = _format_date(listing.get("activationDate"))
    if date_str:
        lines.append(f"**Posted:** {date_str}")

    views = (listing.get("metrics") or {}).get("views")
    if views is not None:
        lines.append(f"**Views:** {views:,}")

    poster = listing.get("posterInfo") or {}
    seller_type = poster.get("sellerType", "")
    verified = poster.get("verified", False)
    seller_parts: list[str] = []
    if seller_type:
        seller_parts.append(seller_type.replace("_", " ").title())
    if verified:
        seller_parts.append("Verified")
    if seller_parts:
        lines.append(f"**Seller:** {' | '.join(seller_parts)}")

    # Description — strip HTML tags that Kijiji embeds (e.g. <br> in dealer listings)
    desc = listing.get("description", "")
    if desc:
        desc = re.sub(r"<br\s*/?>", "\n", desc, flags=re.IGNORECASE)
        desc = re.sub(r"<[^>]+>", "", desc)
        lines.append("")
        lines.append("## Description")
        lines.append("")
        lines.append(desc.strip())

    # Attributes — use human-readable `name` if available, fall back to canonicalName
    attrs = (listing.get("attributes") or {}).get("all") or []
    if attrs:
        lines.append("")
        lines.append("## Details")
        lines.append("")
        for attr in attrs:
            name = attr.get("name") or attr.get("canonicalName", "")
            vals = attr.get("values") or attr.get("canonicalValues") or []
            if name and vals:
                lines.append(f"- **{name}:** {', '.join(str(v) for v in vals)}")

    # Images
    images = listing.get("imageUrls") or []
    if images:
        lines.append("")
        lines.append(f"**Images:** {len(images)}")

    # URL
    url = listing.get("url", "")
    if url:
        full_url = f"https://www.kijiji.ca{url}" if url.startswith("/") else url
        lines.append("")
        lines.append(full_url)

    return "\n".join(lines)


def _format_search_results(data: dict) -> str:
    """Format GraphQL search response as markdown."""
    page = data.get("searchResultsPageByUrl") or {}
    pagination = page.get("pagination") or {}
    total = pagination.get("totalCount", 0)
    results = page.get("results") or {}
    listings = results.get("mainListings") or []

    if not listings:
        return "No results found."

    lines: list[str] = []
    lines.append(f"**{total:,} results** (showing {len(listings)})\n")

    for i, listing in enumerate(listings, 1):
        lines.append(_format_listing_summary(i, listing))

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def get_listing(url: str) -> dict:
    """Fetch Kijiji search/listing data via GraphQL.

    Returns:
        {"content": markdown_string} on success, {"error": message} on failure.
    """
    try:
        # Individual listing page
        if is_kijiji_listing(url):
            listing_id = extract_listing_id(url)
            if not listing_id:
                return {"error": f"Could not extract listing ID from URL: {url}"}

            _log(f"Fetching listing: {listing_id}")
            data = await _graphql(_LISTING_QUERY, {"listingId": listing_id})

            # GraphQL can return partial data alongside errors — only fail if
            # there's no usable data at all.
            listing = (data.get("data") or {}).get("listing")
            if not listing:
                if "errors" in data and data["errors"]:
                    err_msg = data["errors"][0].get("message", "Unknown error")
                    return {"error": f"Kijiji GraphQL error: {err_msg}"}
                return {"error": f"Listing not found: {listing_id}"}

            return {"content": _format_listing_detail(listing)}

        # Search/category page
        if is_kijiji_search(url):
            # Ensure full URL for the API
            full_url = url
            if not full_url.startswith("http"):
                full_url = f"https://www.kijiji.ca{url}"

            _log(f"Searching: {full_url}")
            # Pagination must be nested inside searchResultsByUrlInput AND
            # passed as a separate variable for the mainListings field arg.
            data = await _graphql(_SEARCH_QUERY, {
                "searchResultsByUrlInput": {
                    "url": full_url,
                    "pagination": {"offset": 0, "limit": 40},
                },
                "pagination": {"offset": 0, "limit": 40},
            })

            # Partial errors are expected (some attribute fields fail) —
            # only fail if there's no data at all.
            search_data = (data.get("data") or {}).get("searchResultsPageByUrl")
            if not search_data:
                if "errors" in data and data["errors"]:
                    err_msg = data["errors"][0].get("message", "Unknown error")
                    return {"error": f"Kijiji GraphQL error: {err_msg}"}
                return {"error": "Kijiji returned empty search results."}

            return {"content": _format_search_results(data.get("data") or {})}

        return {"error": f"Could not extract search or listing path from URL: {url}"}

    except RequestsError as e:
        err_str = str(e)
        if "timeout" in err_str.lower():
            return {"error": "Kijiji API request timed out."}
        return {"error": f"Kijiji API error: {e}"}
    except Exception as e:
        _log(f"Unexpected error: {e}")
        return {"error": f"Kijiji API error: {e}"}
