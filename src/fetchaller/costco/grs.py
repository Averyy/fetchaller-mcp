"""Costco's catalogue search: Google Retail Search behind ``gdx-api.costco.com``.

This is what costco.com and costco.ca themselves call for every keyword search
and every category page (``POST /catalog/search/api/v1/search``, observed
2026-10-05; the page config says ``isGRSFeatureFlagEnabled: true``,
``searchResultProvider: GRS``). The ``search.costco.*`` Fusion service this
package was first built on is no longer called by the site. It still answers
keyword searches, but it cannot browse a category, and for a keyword the site
redirects ("tv" → ``/televisions.html``) it returns zero documents plus the
redirect. Read naively, that is "Costco sells no TVs".

**Nothing here is hardcoded that the site publishes.** The search endpoint,
its required headers and request template, the warehouse-locator endpoint and
its ``client-identifier``, and the site's default location for a visitor who
has set none are all read from the page config Costco ships in every search
and category page. The values below are only the fallback for a page whose
config cannot be read, and their use is logged. When Costco moves a service,
the config moves with it.

**Prices and stock are per location.** With no location set the site uses a
default delivery postal code (``M4V 2H7``, ON for costco.ca; ``98101``, WA for
costco.com) and default coordinates. It resolves the nearest warehouse from
those coordinates (``salesLocations.json``) and the delivery centres from the
postal code (``distributioncenters``). The same chain is followed here, and
the output names the warehouse a price belongs to.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

import wafer

from ..content._json_extract import extract_json_object
from ..ratelimit import costco_limiter
from .api import _get_session, _log

ORIGIN = {"com": "https://www.costco.com", "ca": "https://www.costco.ca"}
SITE_CODE = {"com": "USBC", "ca": "CABC"}
LOCALE = {"com": "en-US", "ca": "en-CA"}
PAGE_SIZE = 24  # the site's own resultsPerPage

# Observed in the page config on 2026-10-05; used only when it cannot be read.
_FALLBACK_SEARCH_ENDPOINT = "https://gdx-api.costco.com/catalog/search/api/v1/search"
_FALLBACK_SEARCH_CLIENT = "168287ea-1201-45f6-9b45-5bbea49f8ee7"
_FALLBACK_LOCATOR_ENDPOINT = "https://ecom-api.costco.com/core/warehouse-locator/v1/salesLocations.json"
_FALLBACK_LOCATOR_CLIENT = "7c71124c-7bf1-44db-bc9d-498584cd66e5"
_FALLBACK_DC_ENDPOINT = "https://ecom-api.costco.com/ebusiness/inventory/v1/location/distributioncenters"
_FALLBACK_DEFAULTS = {
    "ca": {"latitude": 43.681, "longitude": -79.399, "postal": "M4V 2H7", "state": "ON"},
    "com": {"latitude": 47.564661, "longitude": -122.32941, "postal": "98101", "state": "WA"},
}

_CACHE_SECONDS = 6 * 60 * 60

# The site's sortBy vocabulary (left over from Fusion and still what its own
# sort menu writes into the URL) mapped onto GRS ``orderBy``, as its bundle
# does. Relevance and popularity sorts are GRS's default order (null).
SORT_MAP = {
    "item_location_pricing_salePrice+desc": "price desc",
    "item_location_pricing_salePrice+asc": "price",
    "item_review_ratings+desc": "rating desc",
    "item_ratings+desc": "rating desc",
    "item_startDate+desc": "attributes.start_date_epoch desc",
    "Brand_attr+asc": "attributes.brand",
}
SORT_LABELS = {
    "price desc": "price, high to low",
    "price": "price, low to high",
    "rating desc": "rating",
    "attributes.start_date_epoch desc": "newest",
    "attributes.brand": "brand",
}

# URL ``refine=||<field>-<value>`` segments the site writes, mapped onto the
# GRS attribute they filter. Others are reported as not applied.
REFINE_FIELDS = {"Brand_attr": "attributes.brand"}


class GrsError(LookupError):
    """The catalogue service did not answer with a search result."""


@dataclass
class SiteConfig:
    domain: str
    search_endpoint: str
    search_headers: dict[str, str]
    search_template: dict
    locator_endpoint: str
    locator_headers: dict[str, str]
    dc_endpoint: str
    latitude: float
    longitude: float
    postal: str
    state: str
    from_page: bool


@dataclass
class Location:
    warehouse_id: str
    warehouse_name: str
    warehouse_city: str
    postal: str
    state: str
    delivery_locations: list[str] = field(default_factory=list)


_configs: dict[str, tuple[float, SiteConfig]] = {}
_locations: dict[str, tuple[float, Location]] = {}
_visitor_id = uuid.uuid4().hex


def _object_after(text: str, marker: str) -> dict | None:
    """The JSON object that follows ``marker`` in the de-escaped page text."""
    index = text.find(marker)
    if index < 0:
        return None
    brace = text.find("{", index + len(marker) - 1)
    if brace < 0 or brace - (index + len(marker)) > 2:
        return None
    return extract_json_object(text, brace, max_scan=200_000)


def parse_site_config(html: str, domain: str) -> SiteConfig | None:
    """Read the services and defaults a Costco page publishes.

    The config sits inside React Server Component strings, so quotes arrive
    escaped (``\\"endpoint\\":``); they are unescaped once before reading.
    Returns None when the search service's entry is missing.
    """
    text = html.replace('\\"', '"')
    search_at = text.find(f'"endpoint":"{_FALLBACK_SEARCH_ENDPOINT}","method":"POST"')
    if search_at < 0:
        match = re.search(r'"endpoint":"(https://[^"]+/catalog/search/api/v[0-9]+/search)","method":"POST"', text)
        if match is None:
            return None
        search_at = match.start()
        endpoint = match.group(1)
    else:
        endpoint = _FALLBACK_SEARCH_ENDPOINT
    tail = text[search_at : search_at + 20_000]
    raw_headers = _object_after(tail, '"required_request_headers":{') or {}
    template = _object_after(tail, '"required_request_parameters":{') or {}

    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if isinstance(value, str):
            headers[key] = value
        elif isinstance(value, dict):
            # Per-site maps: client_id {"CABC": "CABC"}, locale {"en-ca": "en-CA"}.
            picked = value.get(SITE_CODE[domain]) or value.get(LOCALE[domain].lower())
            if isinstance(picked, str):
                headers[key] = picked
    if "client-identifier" not in headers:
        return None

    locator = _object_after(text, '"warehouseLocatorSalesLocationApi":{') or {}
    locator_headers = {
        k: v for k, v in (locator.get("required_request_headers") or {}).items() if isinstance(v, str)
    }
    dc = _object_after(text, '"locationCatalogAPIService":{') or {}

    defaults = dict(_FALLBACK_DEFAULTS[domain])
    coords = re.search(r'"defaultLocation":\{"latitude":(-?[0-9.]+),"longitude":(-?[0-9.]+)\}', text)
    if coords:
        defaults["latitude"], defaults["longitude"] = float(coords.group(1)), float(coords.group(2))
    delivery = re.search(r'"defaultLocation":"([^"]{3,12})","defaultState":"([A-Z]{2})"', text)
    if delivery:
        defaults["postal"], defaults["state"] = delivery.group(1), delivery.group(2)

    return SiteConfig(
        domain=domain,
        search_endpoint=endpoint,
        search_headers=headers,
        search_template=template if isinstance(template, dict) else {},
        locator_endpoint=str(locator.get("endpoint") or _FALLBACK_LOCATOR_ENDPOINT).strip(),
        locator_headers=locator_headers or {"client-identifier": _FALLBACK_LOCATOR_CLIENT},
        dc_endpoint=str(dc.get("endpoint") or _FALLBACK_DC_ENDPOINT).strip(),
        latitude=defaults["latitude"],
        longitude=defaults["longitude"],
        postal=defaults["postal"],
        state=defaults["state"],
        from_page=True,
    )


def _fallback_config(domain: str) -> SiteConfig:
    defaults = _FALLBACK_DEFAULTS[domain]
    return SiteConfig(
        domain=domain,
        search_endpoint=_FALLBACK_SEARCH_ENDPOINT,
        search_headers={
            "client-identifier": _FALLBACK_SEARCH_CLIENT,
            "client_id": SITE_CODE[domain],
            "locale": LOCALE[domain],
            "searchResultProvider": "GRS",
        },
        search_template={},
        locator_endpoint=_FALLBACK_LOCATOR_ENDPOINT,
        locator_headers={"client-identifier": _FALLBACK_LOCATOR_CLIENT},
        dc_endpoint=_FALLBACK_DC_ENDPOINT,
        latitude=defaults["latitude"],
        longitude=defaults["longitude"],
        postal=defaults["postal"],
        state=defaults["state"],
        from_page=False,
    )


async def site_config(domain: str, page_url: str, *, timeout: float) -> SiteConfig:
    """The page-published config for ``domain``, read from ``page_url`` once per window."""
    cached = _configs.get(domain)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    session = await _get_session()
    config: SiteConfig | None = None
    try:
        await costco_limiter.wait()
        response = await session.get(page_url, timeout=timeout)
        if response.status_code == 200:
            config = parse_site_config(response.text, domain)
    except (wafer.WaferError, OSError, TimeoutError) as exc:
        _log(f"config page read failed: {type(exc).__name__}")
    if config is None:
        _log(f"catalogue config not found on {page_url}; using the values observed 2026-10-05")
        # Not cached: one failed read must not pin the dated fallback for the
        # whole window; the next request tries the page again.
        return _fallback_config(domain)
    _configs[domain] = (time.monotonic(), config)
    return config


async def _get_json(url: str, headers: dict[str, str], *, timeout: float) -> object:
    """A location-service read (ecom-api.costco.com).

    Not spaced by ``costco_limiter``: these are two small reads on a separate
    service, made once per cache window, and the site makes them in parallel
    with its own page load. Spacing them 2 s apart pushed a cold search past
    the fetch tool's 10 s default.
    """
    session = await _get_session()
    response = await session.get(url, headers={"Accept": "application/json", **headers}, timeout=timeout)
    if response.status_code != 200:
        raise GrsError(f"{url.split('?')[0]} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise GrsError(f"{url.split('?')[0]} did not return JSON") from exc


def nearest_warehouse(payload: object) -> dict | None:
    """The closest location the locator lists as a ``Warehouse`` (not a business centre)."""
    rows = payload.get("salesLocations") if isinstance(payload, dict) else None
    warehouses = [
        row
        for row in rows or []
        if isinstance(row, dict)
        and row.get("salesLocationId")
        and ((row.get("subType") or {}).get("code") == "Warehouse")
    ]
    if not warehouses:
        return None
    return min(warehouses, key=lambda row: float(row.get("distance") or 0))


async def default_location(config: SiteConfig, *, timeout: float) -> Location:
    """The site's default location, resolved the way its own pages resolve it."""
    cached = _locations.get(config.domain)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    origin = ORIGIN[config.domain]
    locator = await _get_json(
        f"{config.locator_endpoint}?latitude={config.latitude}&longitude={config.longitude}&limit=50",
        {**config.locator_headers, "Origin": origin, "Referer": origin + "/"},
        timeout=timeout,
    )
    warehouse = nearest_warehouse(locator)
    if warehouse is None:
        raise GrsError("the warehouse locator listed no warehouse near Costco's default location")
    warehouse_id = f"{warehouse['salesLocationId']}-wh"
    name = next(
        (n.get("value") for n in warehouse.get("name") or [] if isinstance(n, dict) and n.get("value")),
        warehouse["salesLocationId"],
    )
    city = str((warehouse.get("address") or {}).get("city") or "").title()

    delivery: list[str] = [warehouse_id]
    try:
        centres = await _get_json(
            f"{config.dc_endpoint}?destinationPostalCode={quote(config.postal)}&stateCode={config.state}",
            {"Origin": origin, "Referer": origin + "/"},
            timeout=timeout,
        )
        if isinstance(centres, dict):
            for key in ("groceryCenters", "distributionCenters", "pickUpCenters"):
                delivery.extend(str(c) for c in centres.get(key) or [] if c)
    except GrsError as exc:
        # Delivery centres refine availability; the search answers without them.
        _log(f"delivery centres unavailable: {exc}")

    location = Location(
        warehouse_id=warehouse_id,
        warehouse_name=str(name),
        warehouse_city=city,
        postal=config.postal,
        state=config.state,
        delivery_locations=list(dict.fromkeys(delivery)),
    )
    _locations[config.domain] = (time.monotonic(), location)
    return location


async def search(
    config: SiteConfig,
    location: Location,
    *,
    query: str = "",
    category: str | None = None,
    page: int = 1,
    order_by: str | None = None,
    filters: list[str] | None = None,
    timeout: float,
) -> dict:
    """One page of results, exactly as the site's own request builds it."""
    filter_by = list(filters or [])
    if category:
        filter_by.insert(0, f"attributes.category_uri: ANY({json.dumps(category)})")
    # The site hides out-of-stock items unless the shopper asks otherwise.
    filter_by.append("HIDE_OUT_OF_STOCK")
    body = {
        **config.search_template,
        "visitorId": _visitor_id,
        "query": query,
        "pageSize": PAGE_SIZE,
        "offset": PAGE_SIZE * (max(1, page) - 1),
        "orderBy": order_by,
        "searchMode": "page",
        "personalizationEnabled": False,
        "warehouseId": location.warehouse_id,
        "shipToPostal": location.postal,
        "shipToState": location.state,
        "deliveryLocations": location.delivery_locations,
        "filterBy": filter_by,
        "pageCategories": [category] if category else [],
    }
    origin = ORIGIN[config.domain]
    session = await _get_session()
    await costco_limiter.wait()
    response = await session.post(
        config.search_endpoint,
        json=body,
        headers={
            **config.search_headers,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin + "/",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise GrsError(f"Costco's catalogue search returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise GrsError("Costco's catalogue search did not return JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("searchResult"), dict):
        raise GrsError("Costco's catalogue search returned no search result")
    return data


def _text(attrs: dict, key: str) -> list[str]:
    value = attrs.get(key)
    if isinstance(value, dict):
        return [str(t) for t in value.get("text") or [] if t not in (None, "")]
    return []


def _flag(attrs: dict, key: str) -> bool:
    value = attrs.get(key)
    numbers = value.get("numbers") if isinstance(value, dict) else None
    return bool(numbers) and numbers[0] == 1


def parse_items(data: dict) -> list[dict]:
    """Results joined to their inventory rows (prices and availability)."""
    inventory = {
        str(row.get("productId")): row
        for row in data.get("inventoryResponse") or []
        if isinstance(row, dict) and row.get("productId")
    }
    items = []
    for result in (data.get("searchResult") or {}).get("results") or []:
        if not isinstance(result, dict):
            continue
        product = result.get("product") if isinstance(result.get("product"), dict) else {}
        title = str(product.get("title") or "").strip()
        if not title:
            continue
        attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
        rollup = result.get("variantRollupValues") if isinstance(result.get("variantRollupValues"), dict) else {}
        product_id = str(result.get("id") or product.get("id") or "")
        stock = inventory.get(product_id, {})
        rating = product.get("rating") if isinstance(product.get("rating"), dict) else {}
        items.append(
            {
                "title": title,
                "url": str(product.get("uri") or ""),
                "product_id": product_id,
                "item_number": str((rollup.get("variantId") or [""])[0] or ""),
                "brand": str((product.get("brands") or [""])[0] or ""),
                "model": (_text(attrs, "model") or [""])[0],
                "rating": rating.get("averageRating"),
                "reviews": rating.get("ratingCount"),
                "delivery_price": (stock.get("deliveryPrice") or {}),
                "warehouse_price": (stock.get("warehousePrice") or {}),
                "original_price": (stock.get("originalPrice") or {}),
                "delivery_availability": stock.get("deliveryAvailability") or "",
                "warehouse_availability": stock.get("warehouseAvailability") or "",
                "promotions": [
                    str(p.get("short_text"))
                    for p in stock.get("promotions") or []
                    if isinstance(p, dict) and p.get("short_text")
                ],
                "statements": _text(attrs, "promotional_statement") + _text(attrs, "marketing_statement"),
                "member_only": _flag(attrs, "member_only"),
                "price_in_cart": _flag(attrs, "disp_price_in_cart_only"),
            }
        )
    return items


def refine_filters(refine: str | None) -> tuple[list[str], list[str]]:
    """GRS filters for a URL ``refine`` value, and the segments not applied."""
    applied: list[str] = []
    skipped: list[str] = []
    for segment in (refine or "").split("||"):
        segment = segment.strip()
        if not segment:
            continue
        name, _, value = segment.partition("-")
        attribute = REFINE_FIELDS.get(name)
        if attribute and value:
            applied.append(f'{attribute}: ANY({json.dumps(value)})')
        else:
            skipped.append(segment)
    return applied, skipped
