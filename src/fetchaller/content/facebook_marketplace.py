"""Facebook Marketplace URL detection.

Only matches ``/marketplace/*`` paths — all other facebook.com URLs are ignored.
This module handles URL detection only; the GraphQL client lives in
``fetchaller.facebook_marketplace.graphql``.
"""

import re
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_FB_HOST_RE = re.compile(r"^(?:www\.|m\.|web\.)?facebook\.com$")

# Paths that are NOT search/browse — these are navigation/account pages
_NON_SEARCH_SLUGS = frozenset({
    "item", "search", "create", "you", "categories", "category",
    "directory", "groups", "saved",
})


def is_facebook_marketplace(url: str) -> bool:
    """Check if URL is a Facebook Marketplace page."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not _FB_HOST_RE.match(hostname):
        return False
    return parsed.path.startswith("/marketplace")


def is_facebook_marketplace_search(url: str) -> bool:
    """Check if URL is a Facebook Marketplace search/browse page.

    Matches:
    - /marketplace/ (root browse page)
    - /marketplace/search/?query=...
    - /marketplace/<city>/search/?query=...
    - /marketplace/<city>/ (city browse page)
    - /marketplace/<location_id>/ (numeric location browse page)
    - /marketplace/<location_id>/search/?query=...&category_id=...
    """
    if not is_facebook_marketplace(url):
        return False
    path = urlparse(url).path.rstrip("/")
    parts = path.split("/")

    # /marketplace (root browse)
    if len(parts) == 2 and parts[1] == "marketplace":
        return True
    # /marketplace/search or /marketplace/<city>/search
    if path.endswith("/search"):
        return True
    # /marketplace/<slug> where slug is a city name or location ID, not a
    # reserved path like "item", "create", "you", "categories", etc.
    if len(parts) == 3 and parts[1] == "marketplace":
        slug = parts[2]
        if slug not in _NON_SEARCH_SLUGS:
            return True
    return False


def is_facebook_marketplace_listing(url: str) -> bool:
    """Check if URL is a Facebook Marketplace listing detail page.

    Matches: /marketplace/item/<listing_id>/
    """
    if not is_facebook_marketplace(url):
        return False
    path = urlparse(url).path
    return "/marketplace/item/" in path


_LISTING_ID_RE = re.compile(r"/marketplace/item/(\d+)")


def extract_listing_id(url: str) -> str | None:
    """Extract numeric listing ID from a Marketplace listing URL."""
    m = _LISTING_ID_RE.search(urlparse(url).path)
    return m.group(1) if m else None


def extract_search_query(url: str) -> str:
    """Extract search query from a Marketplace search URL."""
    qs = parse_qs(urlparse(url).query)
    return qs.get("query", [""])[0]


def extract_location_from_url(url: str) -> str:
    """Extract location slug from a Marketplace URL.

    ``/marketplace/vancouver/search/`` → ``vancouver``
    ``/marketplace/108565719168398/search/`` → ``108565719168398``
    ``/marketplace/search/`` → ``""``
    """
    path = urlparse(url).path.rstrip("/")
    parts = path.split("/")
    # /marketplace/<slug>/search or /marketplace/<slug>
    if len(parts) >= 3 and parts[2] not in _NON_SEARCH_SLUGS:
        return parts[2]
    return ""


def extract_price_filters(url: str) -> tuple[int | None, int | None]:
    """Extract price filters from URL query params.

    Facebook uses ``minPrice`` and ``maxPrice`` in cents.

    Returns:
        Tuple of (min_price_cents, max_price_cents), None for unset.
    """
    qs = parse_qs(urlparse(url).query)
    min_price = None
    max_price = None
    try:
        if "minPrice" in qs:
            min_price = int(qs["minPrice"][0])
    except (ValueError, IndexError):
        pass
    try:
        if "maxPrice" in qs:
            max_price = int(qs["maxPrice"][0])
    except (ValueError, IndexError):
        pass
    return min_price, max_price
