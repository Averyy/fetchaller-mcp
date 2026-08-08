"""aartech.ca search API client.

Every listing on aartech.ca — category, manufacturer, and keyword search — is a
React app mounted on an empty ``<div id="search">``. The SSR HTML contains the
page furniture and nothing else: no product rows, no part numbers, and no
prices. Reading the markup gets you a category blurb and a footer.

The app calls ``{base_index_url}api/search/{searchId}[/{systemSearchFilterId}]``
with a static ``api-key`` header, and it pushes its own query string onto the
address bar as it filters. That last detail is the useful one: the query string
of the page URL a caller pastes *is* the API's query string, so ``?limit=24``
and ``?page=2`` need no translation.

A keyword search is the exception, and a dangerous one. The term is **not**
``?q=``; the API ignores every obvious spelling of that and answers with the
entire catalogue, so ``?q=cat6`` returns 3,432 unrelated products that render
perfectly happily under a "Search Results" heading. The site's own autocomplete
shows the real form::

    window.location.href = base_index_url + `search?controls[${searchControlId}]=${term}`

where ``searchControlId`` is another SSR global. ``controls[1]=cat6`` returns
126 cable products and ``controls[1]=zzzznope`` returns 0. So a caller-supplied
``q``/``query``/``search`` is rewritten into that form, and a term that cannot
be conveyed raises rather than quietly answering with the whole catalogue.

Which listing to ask for comes from two globals the SSR page does emit:

    var searchId = '3';
    var systemSearchFilterId = '198';

``searchId`` selects the search configuration (3 = category, 4 = manufacturer,
2 = keyword search) and ``systemSearchFilterId`` is the category/brand it is
scoped to.

Product pages hide their prices too, but differently: the markup ships empty
``<template>`` elements (``product-pricing-template`` and friends) that a script
fills in from one embedded blob, ``new window.catalog.ProductPreview({...})``.
That blob holds the price, the unit-of-measure table, volume breaks and stock,
so the price is read from there rather than from the rendered markup.

URL shape cannot tell the two apart — ``/network-cable`` is a category and
``/intermatic-dt200lt`` is a product, both a single segment with no suffix. So
the page is fetched once and dispatched on what it actually contains, which is
also what lets a page that is neither fall back to ordinary HTML cleanly.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from urllib.parse import unquote_plus, urlparse, urlunparse

import wafer

from ..config import get_wafer_cache_dir
from ..content._json_extract import extract_json_object
from ..security.xss import safe_log_text

# Static routing key the search bundle sends on every call. Not a credential and
# not tied to a session: it is a literal in the site's own http-client chunk.
_API_KEY = "zs-search"

# Rows per request when the caller's URL does not say. Matches the site's own
# default page size; without it the API is happy to return the whole catalogue.
DEFAULT_LIMIT = 24

# Ceiling on rows per request. A listing of 3,400 hits would otherwise answer
# with megabytes of JSON that no caller asked for.
MAX_LIMIT = 100

_HOST_RE = re.compile(r"^(?:www\.)?aartech\.ca$")

_SEARCH_ID_RE = re.compile(r"\bvar\s+searchId\s*=\s*['\"]([0-9]+)['\"]")
_FILTER_ID_RE = re.compile(r"\bvar\s+systemSearchFilterId\s*=\s*['\"]([0-9]+)['\"]")
_BASE_URL_RE = re.compile(r"\bbase_index_url\s*=\s*['\"](https?://[^'\"]+)['\"]")
_CONTROL_ID_RE = re.compile(r"\bvar\s+searchControlId\s*=\s*['\"]?([0-9]+)")

# Query keys a caller (or a model) naturally reaches for when they mean "search
# for this". None of them do anything at the API; they are rewritten.
_TERM_KEYS = frozenset({"q", "query", "search", "keyword", "keywords", "term"})

# The constructor call whose argument carries a product page's real pricing.
_PRODUCT_BLOB_MARKER = "window.catalog.ProductPreview("

# Paths that are neither a listing nor a product, so no request is worth
# spending. Everything else is decided by reading the page, since URL shape
# cannot separate a category from a product here.
#
# Matched on whole path segments, never as a bare string prefix: `/cart` must
# not swallow the `/cartridge-heaters` category, whose only symptom would be a
# priceless HTML fallback — the exact failure this module exists to prevent.
_NON_CATALOGUE_ROOTS = frozenset({"page", "checkout", "account", "accounts", "cart", "blog", "login"})


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] aartech: {safe_log_text(msg)}",
        file=sys.stderr,
    )


_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> wafer.AsyncSession:
    """Shared session: reuses cookies/clearance and caps unbounded bodies.

    A product page is ~500KB of HTML and a 100-row listing is a few hundred KB
    of JSON, so an explicit ceiling matters — wafer's default is no limit.
    """
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


def is_aartech(url: str) -> bool:
    """Whether ``url`` points at aartech.ca."""
    return bool(_HOST_RE.match((urlparse(url).hostname or "").lower()))


def is_aartech_catalogue_url(url: str) -> bool:
    """Whether ``url`` is worth reading as a listing or a product page.

    Deliberately permissive: it only skips paths that are certainly neither.
    What a page actually is gets decided by reading it, because ``/network-cable``
    (a category) and ``/intermatic-dt200lt`` (a product) are the same shape.
    """
    if not is_aartech(url):
        return False
    segments = [s for s in urlparse(url).path.lower().split("/") if s]
    if not segments:
        return False  # the home page renders its own promo grid
    return segments[0] not in _NON_CATALOGUE_ROOTS


def extract_product(html: str) -> dict | None:
    """Pull the embedded product blob out of a product page, if it has one."""
    marker = html.find(_PRODUCT_BLOB_MARKER)
    if marker == -1:
        return None
    brace = html.find("{", marker + len(_PRODUCT_BLOB_MARKER) - 1)
    if brace == -1:
        return None
    blob = extract_json_object(html, brace)
    # `exists` is the site's own word for "this part number resolved".
    if not isinstance(blob, dict) or not blob.get("part_number"):
        return None
    return blob


def extract_listing_ids(html: str) -> tuple[str, str | None] | None:
    """Pull ``(searchId, systemSearchFilterId)`` out of a listing page's HTML.

    Returns None when the page mounts no search app, which is how a product or
    content page is told apart from a listing.
    """
    search_id = _SEARCH_ID_RE.search(html)
    if not search_id:
        return None
    filter_id = _FILTER_ID_RE.search(html)
    return search_id.group(1), filter_id.group(1) if filter_id else None


def _api_base(html: str, url: str) -> str:
    """The API origin the page itself names, falling back to the page's own."""
    declared = _BASE_URL_RE.search(html)
    if declared:
        base = declared.group(1)
        # Only honour a same-host value; a listing page has no business
        # redirecting our API calls to another origin.
        if is_aartech(base):
            return base.rstrip("/") + "/"
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def resolve_window(query: str) -> tuple[int, int]:
    """The ``(page, limit)`` a query string actually resolves to.

    Shared by the request builder and the renderer so the "further products not
    shown" count is computed against the same window that was fetched.
    """
    page, limit = 1, DEFAULT_LIMIT
    for segment in query.split("&"):
        if not segment:
            continue
        key, _, value = segment.partition("=")
        lowered = unquote_plus(key).lower()
        if lowered == "limit":
            try:
                limit = max(1, min(MAX_LIMIT, int(value)))
            except ValueError:
                limit = DEFAULT_LIMIT
        elif lowered == "page":
            try:
                page = max(1, int(value))
            except ValueError:
                page = 1
    return page, limit


def _normalize_query(query: str, control_id: str | None) -> str:
    """Carry the caller's query string through, bounding size and fixing the term.

    The search app round-trips its parameters through the address bar, so most of
    the query string is already in the API's own vocabulary and passes as-is.
    Two keys are not:

    - ``limit``, because absent it returns everything and unbounded it returns
      far too much.
    - a search term under any of ``_TERM_KEYS``, which the API ignores outright.
      It is rewritten to ``controls[{control_id}]``. Raises ``LookupError`` if a
      term was supplied and no control id was found, because answering a keyword
      search with an unfiltered catalogue is worse than not answering.
    """
    _page, limit = resolve_window(query)
    kept: list[str] = []
    term: str | None = None
    for segment in query.split("&"):
        if not segment:
            continue
        key, _, value = segment.partition("=")
        lowered = unquote_plus(key).lower()
        if lowered == "limit":
            continue  # re-emitted below, clamped
        if lowered in _TERM_KEYS:
            term = value
            continue
        kept.append(segment)

    if term:
        if not control_id:
            raise LookupError(
                "this page exposes no search control, so the term could not be "
                "applied; searching would have returned the whole catalogue"
            )
        # Already-correct `controls[...]` params passed through above; only add
        # ours when the caller used a plain term key.
        if not any(segment.lower().startswith(("controls[", "controls%5b")) for segment in kept):
            kept.append(f"controls[{control_id}]={term}")
    kept.append(f"limit={limit}")
    return "&".join(kept)


async def fetch_catalogue_page(url: str, *, timeout: float = 30.0) -> tuple[str, dict] | None:
    """Read one aartech page and return the priced data behind it.

    Returns ``("listing", payload)`` for a category/manufacturer/search page,
    ``("product", blob)`` for a product page, or None when the page is neither
    and belongs on the ordinary HTML path. Raises ``LookupError`` when the page
    is a listing but the search API would not answer for it.
    """
    session = await _get_session()
    page = await session.get(url, timeout=timeout)
    html = page.text

    # Listing first: only listing pages define searchId, while a listing's
    # rows can themselves mention product-shaped JSON.
    ids = extract_listing_ids(html)
    if ids is None:
        product = extract_product(html)
        if product is not None:
            _log(f"product {product.get('part_number')}")
            return "product", product
        _log(f"neither listing nor product: {url} — leaving it to the HTML path")
        return None

    search_id, filter_id = ids
    control = _CONTROL_ID_RE.search(html)
    path = f"search/{search_id}/{filter_id}" if filter_id else f"search/{search_id}"
    query = _normalize_query(urlparse(url).query, control.group(1) if control else None)
    endpoint = f"{_api_base(html, url)}api/{path}?{query}"

    _log(f"listing searchId={search_id} filterId={filter_id or '-'}")
    response = await session.get(
        endpoint,
        headers={"api-key": _API_KEY},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise LookupError(f"search API returned HTTP {response.status_code}")
    try:
        payload = json.loads(response.text)
    except ValueError as exc:
        raise LookupError("search API did not return JSON") from exc
    if not isinstance(payload, dict) or "items" not in payload:
        raise LookupError("search API returned an unexpected shape")
    return "listing", payload
