"""Classify Home Depot URLs into the reads this package knows how to make.

Both storefronts are recognised by URL shape only as a pre-filter. What a page
actually *is* is decided by the data that comes back — the US gateway's search
report, the Canadian page's transfer state — because both sites rewrite and
redirect: a US keyword can land on a brand page, and a Canadian search for
"drill" is answered with the Drills category.

US paths mirror the site's own router (``search-desktop``'s URL parser):

``/p/<slug>/<itemId>``                product (``itemId`` is the Internet #)
``/p/reviews/<slug>/<itemId>/<n>``     page ``n`` of that product's reviews
``/s/<keyword>``                       keyword search
``/b/<slug>/N-<navParam>``             category / brand / refinement listing,
``.../Ntk-<x>/Ntt-<keyword>``          optionally narrowed by a keyword
``/l/<name>/<ST>/<city>/<zip>/<id>``   store page

and every listing honours the same query parameters the site writes:
``Nao`` (result offset), ``sortby``/``sortorder``, ``lowerbound``/``upperbound``.

Canadian paths: ``/product/<slug>/<code>`` (``/produit/`` in French), category
pages under ``/categories/`` ending ``.html`` (refinements are appended as
``/f/<label>/<key>`` path segments, paging and sort as ``?page=`` and
``?sort=``), and keyword search at ``/search`` (``/rechercher``).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from ..urlparams import with_param  # noqa: F401 - re-exported for this package

COM_HOSTS = frozenset({"www.homedepot.com", "homedepot.com"})
CA_HOSTS = frozenset({"www.homedepot.ca", "homedepot.ca"})

COM_ORIGIN = "https://www.homedepot.com"
CA_ORIGIN = "https://www.homedepot.ca"

# The site's own sortby vocabulary (search-desktop's sort menu) mapped onto the
# gateway's ProductSortField enum. Only values the enum accepts are listed: an
# unknown enum value fails validation outright rather than falling back.
COM_SORT_FIELDS = {
    "bestmatch": "BEST_MATCH",
    "topsellers": "TOP_SELLERS",
    "toprated": "TOP_RATED",
    "price": "PRICE",
    "mostpopular": "MOST_POPULAR",
    "newitems": "NEW_ITEMS",
    "productname": "PRODUCT_NAME",
    "brandname": "BRAND_NAME",
}

COM_PAGE_SIZE = 24
CA_PAGE_SIZE = 40


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_homedepot(url: str) -> bool:
    """Whether ``url`` is on either Home Depot storefront."""
    host = _host(url)
    return host in COM_HOSTS or host in CA_HOSTS


def _segments(url: str) -> list[str]:
    return [unquote(s) for s in urlparse(url).path.split("/") if s]


def _query(url: str) -> dict[str, str]:
    """First value of each query parameter, keys lower-cased.

    The site writes both ``lowerBound`` and ``lowerbound`` and its own parser
    accepts either, so matching is case-insensitive.
    """
    out: dict[str, str] = {}
    for key, values in parse_qs(urlparse(url).query, keep_blank_values=False).items():
        out.setdefault(key.lower(), values[0])
    return out


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, OverflowError):  # "inf" and "1e999" overflow int()
        return None


@dataclass(frozen=True)
class ComTarget:
    """One read against the US federation gateway."""

    kind: str  # "product" | "listing" | "reviews" | "store"
    item_id: str | None = None
    keyword: str | None = None
    nav_param: str | None = None
    start_index: int = 0
    sort_by: str | None = None  # the URL's own word, e.g. "price"
    sort_order: str | None = None  # "asc" | "desc"
    lower_bound: int | None = None
    upper_bound: int | None = None
    review_page: int = 1
    store_id: str | None = None
    store_zip: str | None = None

    @property
    def sort_field(self) -> str | None:
        """The gateway enum for ``sort_by``, or None if the site has no such sort."""
        if not self.sort_by:
            return None
        return COM_SORT_FIELDS.get(self.sort_by.lower())


def com_target(url: str) -> ComTarget | None:
    """Classify a homedepot.com URL, or None when it is not one this package reads."""
    if _host(url) not in COM_HOSTS:
        return None
    seg = _segments(url)
    if not seg:
        return None
    head = seg[0].lower()

    if head == "p" and len(seg) >= 2:
        if seg[1].lower() == "reviews":
            digits = [i for i, s in enumerate(seg[2:], start=2) if s.isdigit()]
            if not digits:
                return None
            item_id = seg[digits[0]]
            page = 1
            if len(digits) > 1:
                page = max(1, int(seg[digits[1]]))
            return ComTarget(kind="reviews", item_id=item_id, review_page=page)
        if seg[1].lower() in {"questions", "answers"}:
            return None
        if seg[-1].isdigit():
            return ComTarget(kind="product", item_id=seg[-1])
        return None

    if head == "l" and len(seg) >= 6 and seg[-1].isdigit():
        return ComTarget(kind="store", store_id=seg[-1], store_zip=seg[-2])

    if head not in {"s", "b"}:
        return None

    keyword: str | None = None
    nav: str | None = None
    if head == "s" and len(seg) >= 2:
        keyword = seg[1]
    # On /s/ the first segment is the keyword itself ("N-95 mask"), never a nav.
    for part in seg[2:] if head == "s" else seg[1:]:
        if part.startswith("N-") and len(part) > 2:
            nav = part[2:]
        elif part.startswith("Ntt-") and len(part) > 4:
            keyword = part[4:]
    if keyword is not None:
        keyword = keyword.strip() or None
    if not keyword and not nav:
        return None

    q = _query(url)
    order = (q.get("sortorder") or "").lower() or None
    if order not in {None, "asc", "desc"}:
        order = None
    return ComTarget(
        kind="listing",
        keyword=keyword,
        nav_param=nav,
        start_index=max(0, _int(q.get("nao")) or 0),
        sort_by=q.get("sortby") or None,
        sort_order=order,
        lower_bound=_int(q.get("lowerbound")),
        upper_bound=_int(q.get("upperbound")),
    )


@dataclass(frozen=True)
class CaTarget:
    """One read against homedepot.ca."""

    kind: str  # "product" | "category" | "search"
    lang: str = "en"
    code: str | None = None
    query: str | None = None
    page: int = 1
    sort: str | None = None
    filter: str | None = None


def ca_target(url: str) -> CaTarget | None:
    """Classify a homedepot.ca URL, or None when it is not one this package reads."""
    if _host(url) not in CA_HOSTS:
        return None
    seg = _segments(url)
    if not seg:
        return None
    head = seg[0].lower()
    if head in {"product", "produit"} and len(seg) >= 2 and seg[-1].isdigit():
        return CaTarget(kind="product", lang="fr" if head == "produit" else "en", code=seg[-1])
    q = _query(url)
    if head in {"search", "rechercher"}:
        query = (q.get("q") or "").strip()
        if not query:
            return None
        return CaTarget(
            kind="search",
            lang="fr" if head == "rechercher" else "en",
            query=query,
            page=max(1, _int(q.get("page")) or 1),
            sort=q.get("sort") or None,
            filter=q.get("filter") or None,
        )
    if head in {"en", "fr"} and "categories" in [s.lower() for s in seg]:
        if any(s.lower().endswith(".html") for s in seg):
            return CaTarget(kind="category", lang=head)
    return None
