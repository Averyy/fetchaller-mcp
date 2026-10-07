"""homedepot.ca through its server-rendered pages and the services they call.

homedepot.ca is not blocked: every page answers plain wafer. It still loses
most of a page on the HTML path, because it is an Angular app whose data
arrives as transfer state in ``<script id="hdca-state">`` and is drawn
client-side. Converting the markup gives a product page with an empty
Specifications heading and a category page with no products on it.

What the state carries, keyed by entry name:

``product-<code>``
    The whole product: description, bullet features, ``classifications`` (the
    specification groups), variant options, documents, warranty, dimensions,
    and the first page of BazaarVoice reviews as SEO HTML (``bvReviews``).
    Its ``price`` is the plain sell price only — a sale's was-price and saving
    are not in it. The product page fetches those from
    ``products-localized-basic`` (with online stock), and promotion messages
    from ``promosvc``; both are read here too, or a sale item would be quoted
    without its sale.

``aemContent-<slug>`` → ``plpData``
    A category listing: forty products, facets, sorts and a search report. The
    page honours ``?page=``, ``?sort=`` and ``/f/...`` refinements in its SSR,
    so the URL as given is the read.

Keyword search (``/search?q=``) is the exception: its page carries no results
at all and the browser calls ``/api/search/v1/search``. That API reports the
site's own keyword redirect (``keywordRedirectUrl``: "drill" goes to the Drills
category) and the read follows it, as the site does.

The JSON services are only answered after a page. Called cold, the same
``products-localized-basic`` request that succeeds after its product page is
met with an Akamai challenge (measured 2026-10-04: cold → 403, page first →
200, one minute apart, fresh sessions both). That is our request sequence, not
the site refusing the data, so every service call here follows a page in the
same session and carries it as Referer — the order a browser makes them in.

Prices and stock are the online store's. With no store selected the site prices
against store 7274, which its own store service names "Online" ("CANADA
ECOMMERCE"); that store's ``storeStock`` is always out of stock and means
nothing, so only the online ``stock`` is reported.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote, urlencode, urljoin, urlparse

import wafer

from .com import _log, get_session
from .urls import CA_HOSTS, CA_ORIGIN, CA_PAGE_SIZE, CaTarget

ONLINE_STORE_ID = "7274"

_STATE_RE = re.compile(r'<script id="hdca-state" type="application/json">(.*?)</script>', re.DOTALL)


class CaReadError(LookupError):
    """homedepot.ca answered, but not with the data asked for."""


# When this session last loaded a homedepot.ca page. Akamai's session cookies
# (bm_sz) live four hours; re-warming well inside that keeps a long-running
# server from drifting back into cold service calls.
_last_page_at: float = 0.0
_WARM_FOR = 30 * 60


_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def on_site(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in CA_HOSTS


async def _get(url: str, *, timeout: float, referer: str | None = None):
    from ..ratelimit import homedepot_ca_limiter

    session = await get_session()
    await homedepot_ca_limiter.wait()
    if referer is None:
        return await session.get(url, timeout=timeout)
    return await session.get(url, headers={"Accept": "application/json", "Referer": referer}, timeout=timeout)


async def _get_page(url: str, *, timeout: float):
    """Load a page, following redirects only while they stay on homedepot.ca.

    Returns ``(response, final_url)``.
    """
    global _last_page_at
    current = url
    for _ in range(6):
        response = await _get(current, timeout=timeout)
        if response.status_code not in _REDIRECTS:
            if response.status_code == 200:
                _last_page_at = time.monotonic()
            return response, current
        location = response.headers.get("location")
        if not location:
            return response, current
        following = urljoin(current, location)
        if not on_site(following):
            raise CaReadError(f"homedepot.ca redirected off-site to {following}; not followed")
        current = following
    raise CaReadError("homedepot.ca redirected too many times")


async def _service(url: str, *, page_url: str, timeout: float):
    """A JSON service call made the way the page makes it.

    Loads ``page_url`` first unless this session has loaded a page recently,
    and repeats that once if the service still answers with a challenge.
    """
    if time.monotonic() - _last_page_at > _WARM_FOR:
        await _get_page(page_url, timeout=timeout)
    try:
        return await _get(url, timeout=timeout, referer=page_url)
    except wafer.ChallengeDetected:
        _log(f"challenge on {url.split('?')[0]}; reloading {page_url} and retrying once")
        await _get_page(page_url, timeout=timeout)
        return await _get(url, timeout=timeout, referer=page_url)


def parse_state(html: str) -> dict | None:
    """The page's Angular transfer state, or None if it has none."""
    match = _STATE_RE.search(html)
    if not match:
        return None
    try:
        state = json.loads(match.group(1))
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def state_product(state: dict, code: str | None = None) -> dict | None:
    """The ``product-<code>`` entry, preferring the code the URL named.

    An unknown code still gets an entry: the "Product Not Found" page answers
    200 and carries ``product-<code>`` with every field null. Without the
    ``code`` check that skeleton renders as a product with no name and no
    price.
    """
    if code and f"product-{code}" in state:
        # The page answered for this code: either it is the product or it is the
        # not-found skeleton. Another product-* entry on the page (a sibling, a
        # recommendation) is never a stand-in for it.
        wanted = state[f"product-{code}"]
        return wanted if isinstance(wanted, dict) and wanted.get("code") else None
    found = [
        value
        for key, value in state.items()
        if key.startswith("product-") and isinstance(value, dict) and value.get("code")
    ]
    # With no entry for the code, take the page's product only if it is unambiguous.
    return found[0] if len(found) == 1 else None


def state_listing(state: dict) -> tuple[dict, dict] | None:
    """``(plpData, aemContent)`` from a category page, or None if it has no listing."""
    for key, value in state.items():
        if not key.startswith("aemContent") or not isinstance(value, dict):
            continue
        plp = value.get("plpData")
        if isinstance(plp, dict) and isinstance(plp.get("searchReport"), dict):
            aem = value.get("aemContent") if isinstance(value.get("aemContent"), dict) else {}
            return plp, aem
    return None


async def fetch_page_state(url: str, *, timeout: float) -> tuple[str, dict]:
    """Read one page and return ``(final_url, state)``."""
    response, final_url = await _get_page(url, timeout=timeout)
    if response.status_code == 404:
        raise CaReadError("homedepot.ca has no such page (HTTP 404)")
    if response.status_code != 200:
        raise CaReadError(f"homedepot.ca returned HTTP {response.status_code}")
    state = parse_state(response.text)
    if state is None:
        raise CaReadError("the page carried no data state")
    return final_url, state


async def fetch_localized(
    codes: list[str], *, lang: str, page_url: str, timeout: float
) -> dict[str, dict] | None:
    """Displayed price (with any was-price) and online stock, keyed by product code.

    None means the service could not be read — distinct from ``{}``, an answer
    with no rows — so the caller can say the sale price is unknown rather than
    implying there is no sale.
    """
    if not codes:
        return {}
    url = (
        f"{CA_ORIGIN}/api/productsvc/v1/products-localized-basic?"
        f"products={','.join(codes)}&store={ONLINE_STORE_ID}&lang={lang}"
    )
    try:
        response = await _service(url, page_url=page_url, timeout=timeout)
    except wafer.WaferError as exc:
        _log(f"products-localized-basic failed: {type(exc).__name__}")
        return None
    if response.status_code != 200:
        _log(f"products-localized-basic returned HTTP {response.status_code}")
        return None
    try:
        rows = response.json()
    except ValueError:
        return None
    out: dict[str, dict] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("productId"):
            out[str(row["productId"])] = row
    return out


async def fetch_promotions(code: str, *, lang: str, page_url: str, timeout: float) -> list[dict]:
    url = f"{CA_ORIGIN}/api/promosvc/v1/promotions?products={code}&store={ONLINE_STORE_ID}&lang={lang}"
    try:
        response = await _service(url, page_url=page_url, timeout=timeout)
    except wafer.WaferError as exc:
        _log(f"promosvc failed: {type(exc).__name__}")
        return []
    if response.status_code != 200:
        _log(f"promosvc returned HTTP {response.status_code}")
        return []
    try:
        rows = response.json()
    except ValueError:
        return []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("productCode")) == code:
            return [p for p in row.get("promotions") or [] if isinstance(p, dict)]
    return []


def search_api_url(target: CaTarget) -> str:
    params = {"q": target.query or "", "pageSize": str(CA_PAGE_SIZE), "lang": target.lang}
    if target.page > 1:
        params["page"] = str(target.page)
    if target.sort:
        params["sort"] = target.sort
    if target.filter:
        params["filter"] = target.filter
    return f"{CA_ORIGIN}/api/search/v1/search?{urlencode(params, quote_via=quote)}"


async def fetch_search(target: CaTarget, *, page_url: str, timeout: float) -> dict:
    """The search API, called after the search page it serves.

    The page carries no results of its own, but it is the request the API
    follows in a browser; see the module docstring.
    """
    response = await _service(search_api_url(target), page_url=page_url, timeout=timeout)
    if response.status_code != 200:
        raise CaReadError(f"homedepot.ca search returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CaReadError("homedepot.ca search did not return JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("searchReport"), dict):
        raise CaReadError("homedepot.ca search returned no search report")
    return data


def redirect_target(data: dict) -> str | None:
    """The page the site sends this keyword to, as an absolute URL."""
    report = data.get("searchReport") or {}
    redirect = report.get("keywordRedirectUrl")
    if not redirect or not isinstance(redirect, str):
        return None
    destination = urljoin(CA_ORIGIN + "/", redirect)
    return destination if on_site(destination) else None
