"""Ubiquiti store and tech-specs client.

Three ui.com properties answer questions about a UniFi device, and all three are
Next.js apps whose useful content never reaches the rendered markup:

``*.store.ui.com``
    Product and category pages. The markup carries the marketing copy and the
    price, so the generic HTML path returns something that *looks* complete —
    which is exactly the trap. Every technical specification (WiFi standard,
    spatial streams, MIMO, radio frequencies, PoE draw, certifications) lives
    behind a "Technical" tab that renders client-side from ``__NEXT_DATA__``.
    Read the markup and you get a product page with no specifications on it, and
    nothing says so.

``techspecs.ui.com``
    The same specification tree without the storefront. Useful when a device is
    not sold in the caller's region, so the store has no page for it.

``ui.com/qig/<slug>`` / ``dl.ui.com/qig/<slug>``
    Installation guides. See ``guide.py``.

Dispatch keys off ``__NEXT_DATA__["page"]`` — the Next.js route template — not
off the URL's shape. The store serves the same product under
``/us/en/category/...`` and ``/us/en/pro/category/...``, and rewrites both onto
one route, so the route is the thing that actually says what a page is. URL
matching is kept only as a cheap pre-filter deciding whether to spend a request.

Prices arrive as integer minor units under two keys, and the difference between
them matters: ``minDisplayPrice`` is the base and
``minDisplayPriceWithSurcharges`` is what the store actually charges and
displays ("$290.00, Surcharge incl."). Rendering the base as the price would
undercount every product carrying a memory surcharge.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

import wafer

from ..config import get_wafer_cache_dir
from ..security.xss import safe_log_text

# Storefronts are per-region subdomains of one app: store.ui.com plus ca., eu.,
# uk., and friends. They differ only in currency and catalogue.
_STORE_HOST_RE = re.compile(r"^(?:[a-z]{2}\.)?store\.ui\.com$")
_TECHSPECS_HOSTS = frozenset({"techspecs.ui.com"})
# ui.com/qig/<slug> redirects to dl.ui.com/qig/<slug>/; both are accepted so a
# link copied from a product page's `documents` array works as pasted.
_GUIDE_HOSTS = frozenset({"ui.com", "www.ui.com", "dl.ui.com"})

# Next.js route templates. These are the authoritative page kinds.
ROUTE_STORE_PRODUCT = "/[store]/[language]/category/[category]/products/[product]"
ROUTE_STORE_CATEGORY = "/[store]/[language]/category/[category]"
# Some ranges (door access, cameras) file their products under a *collection*
# and the store rewrites onto these two routes. Both carry a product payload —
# the bare collection URL resolves to the collection's current product, with
# `currentProductId` naming which — so both are read as product pages. Without
# them a door-access product silently loses all five of its spec sections.
ROUTE_STORE_COLLECTION_PRODUCT = (
    "/[store]/[language]/category/[category]/collections/[collection]/products/[product]"
)
ROUTE_STORE_COLLECTION = "/[store]/[language]/category/[category]/collections/[collection]"
# The storefront landing route. A URL that names a category but lands here is a
# soft 404: the store answers 200 and renders its home page.
ROUTE_STORE_HOME = "/[store]/[language]"
ROUTE_SPECS_PRODUCT = "/[productLine]/[category]/[product]"
ROUTE_SPECS_CATEGORY = "/[productLine]/[category]"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] ubiquiti: {safe_log_text(msg)}",
        file=sys.stderr,
    )


_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def get_session() -> wafer.AsyncSession:
    """Shared session. A store product page is ~450KB, so cap the body."""
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=10 * 1024 * 1024,
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_ui_store(url: str) -> bool:
    """Whether ``url`` points at a Ubiquiti regional storefront."""
    return bool(_STORE_HOST_RE.match(_host(url)))


def is_ui_techspecs(url: str) -> bool:
    """Whether ``url`` points at techspecs.ui.com."""
    return _host(url) in _TECHSPECS_HOSTS


def is_ui_guide(url: str) -> bool:
    """Whether ``url`` is an installation-guide permalink."""
    if _host(url) not in _GUIDE_HOSTS:
        return False
    segments = [s for s in urlparse(url).path.lower().split("/") if s]
    return len(segments) >= 2 and segments[0] == "qig"


def guide_slug(url: str) -> str | None:
    """The product slug an installation-guide URL names."""
    if not is_ui_guide(url):
        return None
    segments = [s for s in urlparse(url).path.split("/") if s]
    return segments[1]


def is_ubiquiti(url: str) -> bool:
    """Whether ``url`` is a ui.com page this module renders."""
    return is_ui_store(url) or is_ui_techspecs(url) or is_ui_guide(url)


def parse_next_data(html: str) -> dict | None:
    """Pull the ``__NEXT_DATA__`` blob out of a Next.js page."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def page_props(next_data: dict) -> dict:
    props = next_data.get("props")
    if not isinstance(props, dict):
        return {}
    page = props.get("pageProps")
    return page if isinstance(page, dict) else {}


def store_product(next_data: dict) -> dict | None:
    """The product a store product page is about.

    ``collection.products`` can hold several entries when a category shares one
    payload, so ``currentProductId`` picks the one the URL actually named rather
    than trusting position.
    """
    props = page_props(next_data)
    collection = props.get("collection")
    if not isinstance(collection, dict):
        return None
    products = [p for p in collection.get("products") or [] if isinstance(p, dict)]
    if not products:
        return None
    wanted = props.get("currentProductId")
    for product in products:
        if wanted and product.get("id") == wanted:
            return product
    if wanted and len(products) > 1:
        # Falling back silently here would render a sibling product under the
        # URL the caller asked for, which is worse than saying so.
        _log(f"currentProductId {wanted} matched none of {len(products)} products; using the first")
    return products[0]


def store_category_products(next_data: dict) -> list[tuple[str, list[dict]]]:
    """Every product a store category page carries, grouped by sub-category.

    A category page's ``__NEXT_DATA__`` holds the *whole* sibling set — every
    sub-category of the parent, not just the one the URL named — because the
    filter chips switch between them without a navigation. The caller decides
    which group the URL asked for; returning all of them keeps that decision
    out of here.
    """
    props = page_props(next_data)
    groups: list[tuple[str, list[dict]]] = []
    for sub in props.get("subCategories") or []:
        if not isinstance(sub, dict):
            continue
        products = [p for p in sub.get("products") or [] if isinstance(p, dict)]
        if products:
            groups.append((str(sub.get("id") or ""), products))
    return groups


def techspecs_product(next_data: dict) -> dict | None:
    product = page_props(next_data).get("product")
    return product if isinstance(product, dict) else None


def techspecs_groups(next_data: dict) -> list[tuple[str, list[dict]]]:
    """Sub-categories and their products from a techspecs listing page."""
    groups: list[tuple[str, list[dict]]] = []
    for sub in page_props(next_data).get("subCategoriesWithProducts") or []:
        if not isinstance(sub, dict):
            continue
        products = [p for p in sub.get("products") or [] if isinstance(p, dict)]
        if products:
            label = sub.get("name") or sub.get("title") or sub.get("id") or ""
            groups.append((str(label), products))
    return groups


async def fetch_page(url: str, *, timeout: float = 30.0) -> tuple[str, dict, dict] | None:
    """Read one ui.com page and return ``(route, next_data, page_props)``.

    Returns None when the page carries no ``__NEXT_DATA__`` or sits on a route
    this module does not render, so the caller can fall through to the ordinary
    HTML path instead of erroring on a page that HTML handles fine.
    """
    session = await get_session()
    response = await session.get(url, timeout=timeout)
    if response.status_code != 200:
        raise LookupError(f"ui.com returned HTTP {response.status_code}")

    next_data = parse_next_data(response.text)
    if next_data is None:
        _log(f"no __NEXT_DATA__ on {url} — leaving it to the HTML path")
        return None

    route = str(next_data.get("page") or "")

    # An unknown category is not a 404 here: the store answers 200 and rewrites
    # the URL onto its home route, so the payload is the storefront's own
    # (recommendedProducts, whatsNewCards) and carries nothing about the
    # category asked for. Falling through would render the home page under a
    # heading the caller supplied, which reads as an answer.
    if route == ROUTE_STORE_HOME and "/category/" in urlparse(url).path.lower():
        raise LookupError(
            "no such category in this store — ui.com answered with its home page. "
            "Check the slug against the store's own navigation."
        )

    if route not in {
        ROUTE_STORE_PRODUCT,
        ROUTE_STORE_COLLECTION_PRODUCT,
        ROUTE_STORE_COLLECTION,
        ROUTE_STORE_CATEGORY,
        ROUTE_SPECS_PRODUCT,
        ROUTE_SPECS_CATEGORY,
    }:
        _log(f"unhandled route {route!r} on {url} — leaving it to the HTML path")
        return None

    _log(f"route {route}")
    return route, next_data, page_props(next_data)
