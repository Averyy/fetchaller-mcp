"""homedepot.com through its own federation gateway.

Every www.homedepot.com page except the home page answers a session that has
not run Akamai's sensor script with a 2.5 KB behavioural-challenge interstitial
(``sec-if-cpt-container``) — a 200 on a warm session, a 403 on a cold one — so
without a browser the HTML path cannot read a product, a search or a store.
The data those pages render does not come from the HTML anyway: the site's
React experiences ask ``/federation-gateway/graphql`` for it, and the gateway
answers a plain request, cold, with no challenge. That is the read made here.

The site builds its queries at runtime from component data models, so no query
string exists in its bundles to copy. The ones below were assembled from the
fields those data models declare (and the SSR ``__APOLLO_STATE__`` records),
and validated against the gateway, which reports every unknown field by name.
Introspection is refused (401).

Pricing, pickup stock and badges are per store. With no store selected the site
assigns store #121 (Cumberland, GA) — observed in its own SSR state — and the
same store is used here, named in the output so a price is never presented as
national.
"""

from __future__ import annotations

import asyncio
import math
import re
import sys
import time
from datetime import UTC, datetime

import wafer

from ..config import get_wafer_cache_dir
from ..security.xss import safe_log_text
from .urls import COM_ORIGIN, COM_PAGE_SIZE, ComTarget

GATEWAY = f"{COM_ORIGIN}/federation-gateway/graphql"
DEFAULT_STORE_ID = "121"


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] homedepot: {safe_log_text(msg)}",
        file=sys.stderr,
    )


class GatewayError(LookupError):
    """The gateway answered, but not with the data asked for."""


# The gateway is cookieless, but it is behind Akamai's edge, and the edge can
# stop passing it. On 2026-10-07, after a burst of testing (about forty gateway
# reads and eight cold browser solves of homedepot.com pages in half an hour),
# every gateway read was answered 206 ``{"error":[{"message":"Generic
# errors"}]}`` by ``AkamaiGHost`` -- on macOS and Linux alike, warm or cold --
# while the home page still loaded. It lifted on its own within ~20 minutes.
# Sending more reads into that refusal earns nothing, so a refusal stops the
# gateway for a while and every read in that window fails at once, saying when
# it will be tried again. The first read after the window is the probe: refused
# again, the hold doubles (to a cap); answered, it resets.
_REFUSAL_HOLD = 600.0
_REFUSAL_HOLD_MAX = 1800.0
_hold_until = 0.0
_hold_length = 0.0
_hold_started = 0.0


def _clock() -> float:
    return time.monotonic()


# Pages (the HTML path, which needs a browser solve) get a hold of their own.
# A failed solve is a bot signal Akamai acts on: twice on 2026-10-07 a run of
# failed solves in the Linux image (2, then 8) was followed within minutes by
# the edge refusing the gateway as well, for ~20 minutes, while a successful
# macOS solve followed by gateway reads was not. After one failed solve,
# homedepot.com pages are not tried again for a while; nothing else is held.
_PAGE_HOLD = 600.0
_page_hold_until = 0.0


def page_hold_remaining() -> float:
    return max(0.0, _page_hold_until - _clock())


def hold_pages() -> float:
    """Start (or restart) the page hold after a failed browser solve."""
    global _page_hold_until
    _page_hold_until = _clock() + _PAGE_HOLD
    return _PAGE_HOLD


def page_hold_message(remaining: float) -> str:
    return (
        "homedepot.com's bot challenge was not passed on a recent attempt; fetchaller is not "
        f"trying homedepot.com pages again for {_minutes(remaining)}, because repeated failed "
        "attempts have made Akamai refuse the site's data gateway (products, search, stores) too"
    )


def _edge_refused(response) -> bool:
    """Akamai's edge declining to pass the request, as opposed to a gateway error."""
    edge = (response.headers.get("server") or "").lower().startswith("akamaighost")
    return response.status_code in (403, 429) or (response.status_code == 206 and edge)


def _hold() -> float:
    """Start a hold, or double it when the probe after one is refused again.

    Reads already in flight when the edge starts refusing all come back
    refused; they join the hold that is running rather than each doubling it.
    """
    global _hold_until, _hold_length, _hold_started
    now = _clock()
    if now < _hold_until:
        return _hold_until - now
    _hold_length = min(_REFUSAL_HOLD_MAX, _hold_length * 2) if _hold_length else _REFUSAL_HOLD
    _hold_until = now + _hold_length
    _hold_started = now
    return _hold_length


def _release(sent_at: float | None = None) -> None:
    """End the hold, unless the answering read was sent before it began."""
    global _hold_until, _hold_length
    if sent_at is not None and sent_at < _hold_started:
        return
    _hold_until = 0.0
    _hold_length = 0.0


def _minutes(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def get_session() -> wafer.AsyncSession:
    """Shared session for both storefronts. A Canadian listing page is ~1 MB."""
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                # Redirects are followed by hand (homedepot.ca page loads) so
                # that a hop can never leave Home Depot's own hosts; the fetch
                # tool's SSRF pinning does not cover this session.
                _session = wafer.AsyncSession(
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=10 * 1024 * 1024,
                    follow_redirects=False,
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


PRODUCT_FIELDS = """
    itemId
    identifiers { brandName productLabel modelNumber storeSkuNumber upc canonicalUrl productType isSuperSku parentId }
    details { description descriptiveAttributes { name value bulleted sequence } highlights collection { name url } }
    specificationGroup { specTitle specifications { specName specValue } }
    pricing(storeId: $storeId) { value original message mapAboveOriginalPrice unitOfMeasure specialBuy
      promotion { type description { shortDesc longDesc } dollarOff percentageOff promotionTag dates { start end } }
      alternate { unit { value unitsPerCase caseUnitOfMeasure unitsOriginalPrice } bulk { value thresholdQuantity } } }
    reviews { ratingsReviews { averageRating totalReviews } }
    media { images { url sizes subType type } }
    availabilityType { type discontinued buyable status }
    fulfillment(storeId: $storeId) { backordered backorderedShipDate excludedShipStates
      fulfillmentOptions { type fulfillable services { type hasFreeShipping freeDeliveryThreshold totalCharge
        deliveryDates { startDate endDate } locations { isAnchor locationId storeName state type
          inventory { quantity isInStock isOutOfStock isLimitedQuantity isUnavailable } } } } }
    taxonomy { breadCrumbs { label url } }
    info { returnable quantityLimit hidePrice ecoRebate }
"""

PRODUCT_QUERY = (
    "query productClientOnlyProduct($itemId: String!, $storeId: String) {\n"
    "  product(itemId: $itemId) {" + PRODUCT_FIELDS + "  }\n}\n"
)

SEARCH_QUERY = """
query searchModel($keyword: String, $navParam: String, $storeId: String, $startIndex: Int, $pageSize: Int,
                  $orderBy: ProductSort, $filter: ProductFilter, $channel: Channel, $storefilter: StoreFilter,
                  $additionalSearchParams: AdditionalParams) {
  searchModel(keyword: $keyword, navParam: $navParam, storeId: $storeId, channel: $channel,
              storefilter: $storefilter, additionalSearchParams: $additionalSearchParams) {
    searchReport { keyword totalProducts didYouMean correctedKeyword sortBy sortOrder pageSize startIndex }
    metadata { canonicalUrl searchRedirect h1Tag stores { storeId storeName } }
    taxonomy { breadCrumbs { label url dimensionName } }
    appliedDimensions { label refinements { label refinementKey url } }
    dimensions { label refinements { label url recordCount } }
    products(startIndex: $startIndex, pageSize: $pageSize, orderBy: $orderBy, filter: $filter) {
      itemId
      identifiers { brandName productLabel modelNumber canonicalUrl }
      pricing(storeId: $storeId) { value original message unitOfMeasure
        promotion { dollarOff percentageOff }
        alternate { unit { value caseUnitOfMeasure } } }
      reviews { ratingsReviews { averageRating totalReviews } }
      availabilityType { discontinued buyable }
      badges(storeId: $storeId) { label }
      info { isSponsored hidePrice }
      fulfillment(storeId: $storeId) { fulfillmentOptions { type fulfillable
        services { type locations { isAnchor inventory { quantity isInStock } } } } }
    }
  }
}
"""

REVIEWS_QUERY = """
query reviews($itemId: String!, $startIndex: Int, $pagesize: String, $recfirstpage: String) {
  product(itemId: $itemId) { itemId identifiers { brandName productLabel canonicalUrl } }
  reviews(itemId: $itemId, startIndex: $startIndex, pagesize: $pagesize, recfirstpage: $recfirstpage) {
    TotalResults
    Includes { Products {
      store { Id FilteredReviewStatistics { AverageOverallRating TotalReviewCount
        RecommendedCount NotRecommendedCount RatingDistribution { RatingValue Count } } }
      items { Id FilteredReviewStatistics { AverageOverallRating TotalReviewCount
        RecommendedCount NotRecommendedCount RatingDistribution { RatingValue Count } } } } }
    Results { Id ProductId Rating Title ReviewText SubmissionTime UserNickname IsRecommended
      TotalPositiveFeedbackCount TotalNegativeFeedbackCount BadgesOrder
      Photos { Id } ClientResponses { Department Response Date } }
  }
}
"""

STORE_QUERY = """
query storeSearch($storeSearchInput: String!, $pagesize: String, $radius: String) {
  storeSearch(storeSearchInput: $storeSearchInput, pagesize: $pagesize, radius: $radius) {
    stores { storeId name phone proDeskPhone toolRentalPhone storeType storeDetailsPageLink
      address { street city state postalCode country } coordinates { lat lng }
      flags { bopisFlag bodfsFlag curbsidePickupFlag }
      services { hdMoving loadNGo propane toolRental penske keyCutting wiFi applianceShowroom
        expandedFlooringShowroom largeEquipment kitchenShowroom }
      storeHours { monday { open close } tuesday { open close } wednesday { open close } thursday { open close }
        friday { open close } saturday { open close } sunday { open close } } }
  }
}
"""

REVIEWS_PAGE_SIZE = 10


def _refusal(response) -> str:
    """Say who answered a non-200, not just its status.

    Akamai's edge answers a request it will not pass with HTTP 206 and
    ``{"error":[{"message":"Generic errors"}]}`` under ``Server: AkamaiGHost``
    (seen 2026-10-07). That is the edge refusing, not the gateway answering, and
    a bare "HTTP 206" reads like a partial result.
    """
    edge = (response.headers.get("server") or "").lower().startswith("akamaighost")
    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        errors = body.get("error") or body.get("errors") or []
        if isinstance(errors, list):
            message = "; ".join(
                str(e.get("message"))[:200] for e in errors if isinstance(e, dict) and e.get("message")
            )
    where = " from its Akamai edge" if edge else ""
    return f"homedepot.com's data gateway returned HTTP {response.status_code}{where}" + (
        f": {message}" if message else ""
    )


async def _post(operation: str, query: str, variables: dict, *, experience: str, timeout: float) -> dict:
    """Run one gateway operation and return its ``data``.

    A GraphQL error is raised, never returned as empty data: the gateway answers
    an unknown item with HTTP 200 and ``product: null``, which would otherwise
    render as a product with nothing on it.
    """
    from ..ratelimit import homedepot_com_limiter

    def held_error(remaining: float) -> GatewayError:
        return GatewayError(
            "homedepot.com's Akamai edge refused the data gateway recently; fetchaller is not "
            f"calling it again for {_minutes(remaining)} rather than keep sending requests into a refusal"
        )

    if (remaining := _hold_until - _clock()) > 0:
        raise held_error(remaining)
    session = await get_session()
    await homedepot_com_limiter.wait()
    # A hold can begin while this read waits its turn at the limiter.
    if (remaining := _hold_until - _clock()) > 0:
        raise held_error(remaining)
    sent_at = _clock()
    response = await session.post(
        f"{GATEWAY}?opname={operation}",
        json={"operationName": operation, "variables": variables, "query": query},
        headers={
            "x-experience-name": experience,
            "x-hd-dc": "origin",
            "x-debug": "false",
            "Origin": COM_ORIGIN,
            "Referer": f"{COM_ORIGIN}/",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        if _edge_refused(response):
            held = _hold()
            _log(f"{operation}: edge refused (HTTP {response.status_code}); holding the gateway for {held:.0f}s")
            raise GatewayError(f"{_refusal(response)}. Not calling it again for {_minutes(held)}")
        raise GatewayError(_refusal(response))
    _release(sent_at)
    try:
        payload = response.json()
    except ValueError as exc:
        raise GatewayError("homedepot.com's data gateway did not return JSON") from exc
    if not isinstance(payload, dict):
        raise GatewayError("homedepot.com's data gateway returned an unexpected payload")
    errors = [e for e in payload.get("errors") or [] if isinstance(e, dict)]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if errors:
        messages = "; ".join(str(e.get("message") or "")[:200] for e in errors)
        _log(f"{operation}: {messages}")
        # Partial data is still an answer when the field asked about came back;
        # the caller decides. With nothing usable, the message is the answer.
        if not any(v is not None for v in data.values()):
            raise GatewayError(messages or "unknown gateway error")
    return data


async def fetch_product(item_id: str, *, timeout: float) -> dict:
    data = await _post(
        "productClientOnlyProduct",
        PRODUCT_QUERY,
        {"itemId": item_id, "storeId": DEFAULT_STORE_ID},
        experience="general-merchandise",
        timeout=timeout,
    )
    product = data.get("product")
    if not isinstance(product, dict):
        raise GatewayError(f"no product with Internet # {item_id}")
    return product


def _search_variables(target: ComTarget, nav_param: str | None, keyword: str | None) -> dict:
    field = target.sort_field
    if field is None:
        # The site's own defaults: relevance for a keyword, best sellers for a
        # bare category. Its browse SSR sends TOP_SELLERS with order ASC.
        field = "BEST_MATCH" if keyword else "TOP_SELLERS"
    order = (target.sort_order or "asc").upper()
    product_filter: dict = {}
    if target.lower_bound is not None or target.upper_bound is not None:
        # A missing upper bound is left out: sent as 0 it means "at most $0"
        # and the gateway answers no listing at all (measured 2026-10-07:
        # lowerbound=100 on Drills gave nothing with upperBound 0, and the 808
        # matching drills with it omitted). A missing lower bound is 0.
        product_filter = {"rangefilter": "price", "lowerBound": target.lower_bound or 0}
        if target.upper_bound is not None:
            product_filter["upperBound"] = target.upper_bound
    return {
        "keyword": keyword,
        "navParam": nav_param,
        "storeId": DEFAULT_STORE_ID,
        "startIndex": target.start_index,
        "pageSize": COM_PAGE_SIZE,
        "orderBy": {"field": field, "order": order},
        "filter": product_filter,
        "channel": "DESKTOP",
        "storefilter": "ALL",
        "additionalSearchParams": {"multiStoreIds": []},
    }


async def fetch_listing(
    target: ComTarget, *, timeout: float, nav_param: str | None = None, keyword: str | None = None
) -> dict:
    """One page of a search or category listing (the ``searchModel``)."""
    nav = nav_param if nav_param is not None else target.nav_param
    kw = keyword if keyword is not None else target.keyword
    data = await _post(
        "searchModel",
        SEARCH_QUERY,
        _search_variables(target, nav, kw),
        experience="search-desktop" if kw else "browse-desktop",
        timeout=timeout,
    )
    model = data.get("searchModel")
    if not isinstance(model, dict):
        raise GatewayError("the search returned no result model")
    return model


async def fetch_reviews(target: ComTarget, *, timeout: float) -> tuple[dict | None, dict]:
    """Product identity plus one page of its reviews, in one request.

    Home Depot pools reviews across a product's variant family: a page for
    204279858 also returns reviews written about 203164241, 203164237 and
    308959619, each tagged with its own ``ProductId``. ``Includes.Products``
    carries statistics for every family member, and its ``store`` slot holds
    whichever member leads *this page* — so it is not the product's figure
    after page one. The renderer picks the entry whose ``Id`` is the URL's.
    """
    page = max(1, target.review_page)
    data = await _post(
        "reviews",
        REVIEWS_QUERY,
        {
            "itemId": target.item_id,
            # BazaarVoice offsets: startIndex is 1-based.
            "startIndex": (page - 1) * REVIEWS_PAGE_SIZE + 1,
            "pagesize": str(REVIEWS_PAGE_SIZE),
            "recfirstpage": str(REVIEWS_PAGE_SIZE),
        },
        experience="general-merchandise",
        timeout=timeout,
    )
    reviews = data.get("reviews")
    if not isinstance(reviews, dict):
        raise GatewayError(f"no reviews returned for Internet # {target.item_id}")
    product = data.get("product") if isinstance(data.get("product"), dict) else None
    return product, reviews


async def fetch_store(store_id: str, store_zip: str, *, timeout: float) -> tuple[dict, str | None]:
    """One store's record, plus a note when the URL's store has moved.

    ``storeDetails(storeId)`` exists but carries almost nothing (no name, hours,
    services or flags). ``storeSearch`` keyed on the ZIP that every store URL
    carries returns full records for the stores near it, and the one named by
    the URL is picked out of those by id.

    A relocated store keeps its old URL in circulation while the store search
    lists only its replacement, which Home Depot names "<name> (Relo <old id>)"
    — measured on #6177, listed as #6140 "Midtown Manhattan (Relo 6177)". That
    label is the site's own statement of the move, so it is followed, and said.
    """
    data = await _post(
        "storeSearch",
        STORE_QUERY,
        {"storeSearchInput": store_zip, "pagesize": "40", "radius": "50"},
        experience="general-merchandise",
        timeout=timeout,
    )
    stores = [s for s in ((data.get("storeSearch") or {}).get("stores")) or [] if isinstance(s, dict)]
    # The search zero-pads ids ("0121") while store URLs do not ("/30339/121").
    wanted = store_id.lstrip("0") or "0"
    for store in stores:
        if str(store.get("storeId") or "").lstrip("0") == wanted:
            return store, None
    relocation = re.compile(rf"\bRelo 0*{re.escape(wanted)}\b", re.IGNORECASE)
    for store in stores:
        if relocation.search(str(store.get("name") or "")):
            return store, (
                f"Store #{store_id} has relocated; Home Depot lists its replacement as "
                f"#{store.get('storeId')} {store.get('name')}."
            )
    raise GatewayError(f"store #{store_id} is not among the stores near ZIP {store_zip}")
