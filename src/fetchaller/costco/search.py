"""Costco search and category pages.

Flow:
1. Detect Costco URL type (search, category, or product)
2. Extract the keyword or category slug, page, sort and refinements
3. Ask Costco's catalogue search (``grs.py``) exactly as the site does,
   following its own redirect when a keyword is sent to a category
4. Fall back to the older Fusion service (``api.py``) only for a keyword
   search the catalogue could not answer, and say so
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import wafer

from ..security.xss import safe_log_text
from . import api


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] costco search: "
        f"{safe_log_text(msg)}",
        file=sys.stderr,
    )


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


def _money(value: object) -> str | None:
    try:
        amount = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f"${amount:,.2f}"


def _price_range(price: dict) -> str | None:
    low, high = _money(price.get("minPrice")), _money(price.get("maxPrice"))
    if low and high and low != high:
        return f"{low}–{high}"
    return low or high


_AVAILABILITY = {
    "IN_STOCK": "in stock",
    "LOW_STOCK": "low stock",
    "OUT_OF_STOCK": "out of stock",
    "NOT_AVAILABLE": "not available",
}


def _format_grs_item(n: int, item: dict) -> list[str]:
    bits: list[str] = []
    if item["price_in_cart"]:
        bits.append("price shown in cart")
    else:
        delivery = _price_range(item["delivery_price"])
        warehouse = _price_range(item["warehouse_price"])
        original = _price_range(item["original_price"])
        if delivery and warehouse and delivery != warehouse:
            bits.append(f"{delivery} online, {warehouse} in warehouse")
        elif delivery or warehouse:
            bits.append(delivery or warehouse)
        else:
            bits.append("price not returned")
        shown = delivery or warehouse
        try:
            if original and shown and float(item["original_price"].get("minPrice")) > float(
                (item["delivery_price"] or item["warehouse_price"]).get("minPrice")
            ):
                bits[-1] += f" (was {original})"
        except (TypeError, ValueError):
            pass
    if item["member_only"]:
        bits.append("members only")
    try:
        if item["rating"] is not None and item["reviews"]:
            bits.append(f"★{float(item['rating']):.1f} ({int(item['reviews']):,})")
    except (TypeError, ValueError):
        pass
    if item["item_number"]:
        bits.append(f"Item {item['item_number']}")
    if item["model"]:
        bits.append(f"Model {item['model']}")
    bits.extend(item["promotions"])
    bits.extend(s for s in item["statements"] if s not in item["promotions"])
    lines = [f"{n}. **{item['title']}** — {' · '.join(b for b in bits if b)}"]

    stock = []
    if item["delivery_availability"]:
        stock.append(f"delivery: {_AVAILABILITY.get(item['delivery_availability'], item['delivery_availability'].lower())}")
    if item["warehouse_availability"]:
        stock.append(f"warehouse: {_AVAILABILITY.get(item['warehouse_availability'], item['warehouse_availability'].lower())}")
    lines.append(f"   {' · '.join(stock + [item['url']]) if stock else item['url']}")
    return lines


def _facet_lines(data: dict) -> list[str]:
    """Text facets with their counts — the site's own filter sidebar."""
    lines: list[str] = []
    skip = {"attributes.category_uri", "attributes.category_info", "attributes.program_types", "categories"}
    for facet in (data.get("searchResult") or {}).get("facets") or []:
        if not isinstance(facet, dict) or facet.get("key") in skip:
            continue
        values = [
            f"{v.get('value')} ({v.get('count')})"
            for v in facet.get("values") or []
            if isinstance(v, dict) and v.get("value") not in (None, "")
        ]
        if values:
            more = f" … +{len(values) - 10} more" if len(values) > 10 else ""
            lines.append(f"- **{facet.get('key')}:** {'; '.join(values[:10])}{more}")
    return lines


def format_grs_results(
    data: dict,
    *,
    domain: str,
    url: str,
    query: str,
    category: str | None,
    page: int,
    location,
    sort_label: str,
    redirected_to: str | None = None,
    notes: list[str] | None = None,
) -> str:
    """Render a catalogue answer: header, location, rows, paging, facets."""
    from . import grs

    result = data.get("searchResult") or {}
    items = grs.parse_items(data)
    total = int(result.get("totalSize") or 0)
    crumb = data.get("breadcrumb") if isinstance(data.get("breadcrumb"), dict) else {}
    trail = " > ".join(p for p in str(crumb.get("name") or "").split("|") if p)

    if category:
        title = f"Costco.{domain}: {trail or category}"
    else:
        title = f'Costco.{domain} search: "{query}"'
    out = [f"# {title}", ""]
    if redirected_to:
        out += [f'_Costco sends the search "{query}" to {redirected_to} — these are that page\'s results._', ""]
    for note in notes or []:
        out += [f"_{note}_", ""]
    corrected = result.get("correctedQuery")
    if corrected and corrected != query:
        out += [f'_Results are for "{corrected}" (Costco corrected the query)._', ""]

    first = _offset(page)
    shown = f"showing {first + 1}–{first + len(items)}" if items else "none shown"
    out.append(f"**{total:,} products** · {shown} · sorted by {sort_label} · out-of-stock items hidden, as on the site")
    out.append(
        f"Prices and stock for warehouse #{location.warehouse_id.split('-')[0]} {location.warehouse_name}"
        f"{', ' + location.warehouse_city if location.warehouse_city and location.warehouse_city.lower() != location.warehouse_name.lower() else ''}"
        f" and delivery to {location.postal} "
        f"{location.state} — Costco's default when no location is set."
    )
    out.append("")
    if not items:
        if total == 0 and category and not trail:
            out.append(f"Costco's catalogue returned no products for the category '{category}' — check the URL.")
        else:
            out.append("No products matched." if total == 0 else "No products on this page — it is past the last result.")
    for index, item in enumerate(items, start=first + 1):
        out.extend(_format_grs_item(index, item))
    if items and first + len(items) < total:
        from ..urlparams import with_param

        out += ["", f"Next page: {with_param(url, 'currentPage', page + 1)}"]
    facets = _facet_lines(data)
    if facets:
        out += ["", "## Filters (as the site offers them)", *facets]
    return "\n".join(out).strip() + "\n"


def _offset(page: int) -> int:
    from .grs import PAGE_SIZE

    return PAGE_SIZE * (max(1, page) - 1)


def _page_params(url: str) -> tuple[int, str | None, str | None]:
    from . import grs

    qs = parse_qs(urlparse(url).query)
    try:
        if "currentPage" not in qs and "offset" in qs:
            # The older offset= form still appears in links; it is a row index.
            page = max(0, int(qs["offset"][0])) // grs.PAGE_SIZE + 1
        else:
            page = max(1, int(qs.get("currentPage", ["1"])[0]))
    except ValueError:
        page = 1
    # The site writes ``sortBy=item_location_pricing_salePrice+asc``; query
    # parsing decodes that ``+`` to a space, and the sort then matched nothing.
    sort_by = (qs.get("sortBy") or [None])[0]
    if sort_by:
        sort_by = sort_by.replace(" ", "+")
    return page, sort_by, (qs.get("refine") or [None])[0]


async def search_costco(
    url: str,
    cache=None,
    config=None,
    browser_solver=None,
    timeout: float = 30.0,
) -> dict:
    """Read a Costco search or category page through Costco's catalogue search.

    A keyword the site redirects to a category is followed, with the caller's
    page and sort, and the output says so. If the catalogue service does not
    answer, a keyword search falls back to the older Fusion search service and
    says that too; a category has no such fallback.
    """
    from . import grs

    search_params = extract_search_params(url)
    category_params = None if search_params else extract_category_params(url)
    if not search_params and not category_params:
        return {"error": f"Could not extract search parameters from URL: {url}"}

    domain = (search_params or category_params)["domain"]
    page, sort_by, refine = _page_params(url)
    order_by = grs.SORT_MAP.get(sort_by or "")
    sort_label = grs.SORT_LABELS.get(order_by or "", "relevance")
    filters, skipped = grs.refine_filters(refine)
    notes = []
    if sort_by and order_by is None and sort_by not in ("score+desc", "item_page_views+desc", "item_orders+desc"):
        notes.append(f"sortBy={sort_by} is not a sort Costco's catalogue offers; this is its default order.")
    if skipped:
        notes.append(f"These URL refinements were not applied: {', '.join(skipped)}.")

    query = search_params["query"] if search_params else ""
    category = None
    if category_params:
        category = urlparse(url).path.strip("/").removesuffix(".html").lower()

    _log(f"Costco.{domain} catalogue read (query_chars={len(query)}, category={bool(category)}, page={page})")
    try:
        site = await grs.site_config(domain, url, timeout=timeout)
        location = await grs.default_location(site, timeout=timeout)
        data = await grs.search(
            site, location, query=query, category=category, page=page,
            order_by=order_by, filters=filters, timeout=timeout,
        )
        redirected_to = None
        redirect = str((data.get("searchResult") or {}).get("redirectUri") or "")
        if redirect and query:
            target = f"https://www.costco.{domain}{redirect if redirect.startswith('/') else '/' + redirect}"
            target_category = extract_category_params(target)
            if target_category is None:
                return {"content": (
                    f'# Costco.{domain} search: "{query}"\n\n'
                    f"Costco sends this search to {target}, which is not a product listing. "
                    "Fetch that URL for its content.\n"
                )}
            category = urlparse(target).path.strip("/").removesuffix(".html").lower()
            redirected_to = target
            data = await grs.search(
                site, location, query="", category=category, page=page,
                order_by=order_by, filters=filters, timeout=timeout,
            )
        content = format_grs_results(
            data, domain=domain, url=url, query=query, category=category, page=page,
            location=location, sort_label=sort_label, redirected_to=redirected_to, notes=notes,
        )
        return {"content": content}
    except (grs.GrsError, wafer.WaferError, OSError, TimeoutError) as exc:
        _log(f"catalogue search failed: {type(exc).__name__}: {exc}")
        if category_params:
            return {"error": f"Costco's catalogue search did not answer for this category: {exc}"}

    # Keyword search only: the older Fusion service still answers, but it is
    # not what the site shows, and it cannot follow a redirect to a category.
    start = search_params.get("start", 0)
    data = await api.search(query=query, domain=domain, start=start)
    if not data:
        return {"error": "Costco search failed: neither the catalogue search nor the older search service answered."}
    items = api.parse_search_items(data, domain=domain)
    total = api.get_total_count(data)
    content = format_search_results(items, query, total, domain)
    note = (
        "_Costco's catalogue search did not answer, so these results come from its older search "
        "service, which the site no longer uses and which can differ from what it shows._\n\n"
    )
    return {"content": note + content}
