"""Costco search via search API.

Flow:
1. Detect Costco URL type (search, category, or product)
2. Extract query/domain/pagination from URL
3. Call Costco search API -> parse items -> format as numbered markdown
4. Return content dict or error dict
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from . import api


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] costco search: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_COSTCO_HOST_RE = re.compile(r"^(?:www\.)?costco\.(com|ca)$")


def is_costco_url(url: str) -> bool:
    """Check if URL is a Costco domain."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return bool(_COSTCO_HOST_RE.match(hostname))


def is_costco_search_url(url: str) -> bool:
    """Check if URL is a Costco search page (``/s?keyword=...``)."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not _COSTCO_HOST_RE.match(hostname):
        return False
    return parsed.path.rstrip("/") == "/s"


def is_costco_category_url(url: str) -> bool:
    """Check if URL is a Costco category page (e.g., ``/dog-food.html``).

    Category pages end in ``.html`` but are NOT product pages (which contain
    ``.product.`` or ``/p/`` in their path).
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not _COSTCO_HOST_RE.match(hostname):
        return False
    path = parsed.path.lower()
    if ".product." in path or "/p/" in path:
        return False
    return path.endswith(".html") and "/" not in path.strip("/")


# ---------------------------------------------------------------------------
# URL param extraction
# ---------------------------------------------------------------------------


def extract_search_params(url: str) -> dict | None:
    """Extract search parameters from a Costco search URL.

    Returns dict with ``query``, ``domain``, ``start``, or None if not
    a Costco search URL.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    m = _COSTCO_HOST_RE.match(hostname)
    if not m:
        return None

    domain = m.group(1)  # "com" or "ca"
    qs = parse_qs(parsed.query)

    query = qs.get("keyword", [""])[0]
    if not query:
        return None

    # Pagination: Costco uses "currentPage" or "offset"
    start = 0
    if "offset" in qs:
        try:
            start = int(qs["offset"][0])
        except (ValueError, IndexError):
            pass
    elif "currentPage" in qs:
        try:
            page = int(qs["currentPage"][0])
            start = (page - 1) * 24
        except (ValueError, IndexError):
            pass

    return {
        "query": query,
        "domain": domain,
        "start": start,
    }


def extract_category_params(url: str) -> dict | None:
    """Extract category name from a Costco category URL.

    Returns dict with ``query`` (category name) and ``domain``, or None.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    m = _COSTCO_HOST_RE.match(hostname)
    if not m:
        return None

    domain = m.group(1)
    path = parsed.path.strip("/")

    # Remove .html suffix and convert hyphens to spaces
    if path.endswith(".html"):
        path = path[:-5]

    if not path or ".product." in path.lower() or "/" in path:
        return None

    query = path.replace("-", " ")

    return {
        "query": query,
        "domain": domain,
        "start": 0,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_item(n: int, item: dict) -> str:
    """Format a single Costco product as a numbered markdown entry."""
    title = item.get("title", "Untitled")
    lines = [f"{n}. **{title}**"]

    # Metadata line: item#, brand, price, rating, stock
    meta_parts: list[str] = []

    item_number = item.get("item_number", "")
    if item_number:
        meta_parts.append(f"Item# {item_number}")

    brand = item.get("brand", "")
    if brand:
        meta_parts.append(brand)

    # Price display
    price = item.get("price")
    sale_price = item.get("sale_price")
    if sale_price and price and sale_price != price:
        meta_parts.append(f"~~${price}~~ **${sale_price}**")
    elif sale_price:
        meta_parts.append(f"${sale_price}")
    elif price:
        meta_parts.append(f"${price}")

    # Rating
    rating = item.get("rating")
    reviews = item.get("reviews")
    if rating:
        try:
            rating_str = f"Rating: {float(rating):.1f}"
        except (ValueError, TypeError):
            rating_str = f"Rating: {rating}"
        if reviews:
            rating_str += f" ({reviews} reviews)"
        meta_parts.append(rating_str)

    stock = item.get("stock", "")
    if stock:
        meta_parts.append(stock)

    if meta_parts:
        lines.append(f"   {' | '.join(meta_parts)}")

    # Description
    description = item.get("description", "")
    if description:
        lines.append(f"   {description}")

    # Features as bullet list
    features = item.get("features", [])
    if features:
        for feat in features:
            if feat:
                lines.append(f"   - {feat}")

    # URL
    url = item.get("url", "")
    if url:
        lines.append(f"   {url}")

    return "\n".join(lines)


def format_search_results(
    items: list[dict],
    query: str,
    total: int,
    domain: str,
) -> str:
    """Format Costco search results into numbered markdown."""
    site = f"Costco.{domain}"
    header_parts = [site]
    if query:
        header_parts.append(f'"{query}"')

    if total > 0 and len(items) < total:
        header_parts.append(f"showing {len(items)} of {total:,}")
    else:
        header_parts.append(f"{len(items)} results")

    header = " | ".join(header_parts)

    if not items:
        return f"{header}\n\nNo products found."

    formatted = [_format_item(i + 1, item) for i, item in enumerate(items)]
    return f"{header}\n\n" + "\n\n".join(formatted)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def search_costco(
    url: str,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Search Costco via the search API.

    Args:
        url: Costco search or category URL.
        cache: ResponseCache instance (unused, reserved for interface compat).
        config: Config instance.
        browser_solver: BrowserSolver (unused, reserved for interface compat).

    Returns:
        Dict with ``content`` (formatted results) or ``error``.
    """
    # Try search URL first, then category URL
    params = extract_search_params(url)
    if not params:
        params = extract_category_params(url)
    if not params:
        return {"error": f"Could not extract search parameters from URL: {url}"}

    query = params["query"]
    domain = params["domain"]
    start = params.get("start", 0)

    _log(f"Searching Costco.{domain} for '{query}' (start={start})")

    data = await api.search(query=query, domain=domain, start=start)
    if not data:
        return {"error": "Costco search API request failed."}

    items = api.parse_search_items(data, domain=domain)
    total = api.get_total_count(data)

    content = format_search_results(items, query, total, domain)
    return {"content": content}
