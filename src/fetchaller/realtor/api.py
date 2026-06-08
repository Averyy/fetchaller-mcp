"""realtor.ca client — api2 search + SSR listing detail.

All search data comes from the Imperva-protected ``api2.realtor.ca`` XHR
endpoints (the public map page is a CSR shell). wafer 0.2.4 handles Imperva
transparently — a fresh session free-passes via native-TLS at light load (no
browser), and under escalation the ``browser_solver`` solves on the origin page
(www.realtor.ca) with same-site XHR passthrough — so this module just constructs
the requests and parses the JSON.

Endpoints used:
- ``Location.svc/SubAreaSearch``        — geocode a place to a viewport + GEOId
- ``Listing.svc/PropertySearch_Post``   — the search (list view = Results,
                                          map view = Pins), POST form-encoded
Individual listing pages (``www.realtor.ca/real-estate/{id}/{slug}``) are
server-rendered HTML and are parsed directly; "similar listings" are lazy-loaded
on the site, so we reconstruct them from a geo-bounded PropertySearch around the
listing's coordinates and price.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

import wafer
from bs4 import BeautifulSoup

from ..config import get_wafer_cache_dir

API = "https://api2.realtor.ca"
SITE = "https://www.realtor.ca"
_API_HEADERS = {"Origin": SITE, "Referer": SITE + "/"}
_COMMON = {"ApplicationId": "1", "CultureId": "1", "Version": "7.0"}

# api2 caps a search at 600 records (50 pages of 12) regardless of TotalRecords.
MAX_API_RECORDS = 600

# ---------------------------------------------------------------------------
# Filter vocabularies (human-friendly -> realtor.ca codes). All verified live.
# ---------------------------------------------------------------------------

TRANSACTION = {"sale": "2", "rent": "3"}

# PropertySearchTypeId
PROPERTY_TYPE = {
    "any": "0", "residential": "1", "recreational": "2", "condo": "3",
    "agriculture": "4", "parking": "5", "vacant-land": "6", "multi-family": "8",
}

# BuildingTypeId
BUILDING_TYPE = {
    "house": "1", "duplex": "2", "triplex": "3",
    "townhouse": "16", "apartment": "17", "other": "19",
}

# Sort codes
SORT = {"newest": "6-D", "oldest": "6-A", "price-asc": "1-A", "price-desc": "1-D"}

# OwnershipTypeGroupId
OWNERSHIP = {"freehold": "1", "condo": "2"}

# Used to label property-group; 1 = residential (home search).
RESIDENTIAL_GROUP = "1"


# ---------------------------------------------------------------------------
# Shared session
# ---------------------------------------------------------------------------

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    """Shared session for all realtor traffic.

    rate_limit spaces out api2 calls so we don't trip Imperva's rate-based
    reese84 challenge; browser_solver lets wafer mint that token once (and
    reuse it) when heavy load demands it. www.realtor.ca and api2.realtor.ca
    are rate-limited independently (per-hostname), and both share the cookie
    jar (Imperva cookies are Domain=.realtor.ca).
    """
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    timeout=timedelta(seconds=60),
                    rate_limit=1.5,
                    rate_jitter=0.5,
                    cache_dir=get_wafer_cache_dir(),
                )
    return _session


async def close_session() -> None:
    """Release the shared session (shutdown cleanup)."""
    global _session
    _session = None


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_HOSTS = ("realtor.ca", "www.realtor.ca")
# /real-estate/{id}/{slug} (en) or /immobilier/{id}/{slug} (fr)
_LISTING_RE = re.compile(r"^/(?:real-estate|immobilier)/(\d+)(?:/|$)")
# SEO search pages end in /real-estate (with a location prefix), plus /map.
_SEARCH_TAIL_RE = re.compile(r"/(?:real-estate|immobilier)/?$")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_realtor(url: str) -> bool:
    return _host(url) in _HOSTS


def is_realtor_listing(url: str) -> bool:
    if _host(url) not in _HOSTS:
        return False
    return bool(_LISTING_RE.match(urlparse(url).path))


def is_realtor_search(url: str) -> bool:
    """SEO location pages (``/on/ottawa/real-estate``) and the map app."""
    if _host(url) not in _HOSTS:
        return False
    path = urlparse(url).path
    if _LISTING_RE.match(path):
        return False
    if path.rstrip("/") == "/map":
        return True
    return bool(_SEARCH_TAIL_RE.search(path))


def extract_listing_id(url: str) -> str | None:
    m = _LISTING_RE.match(urlparse(url).path)
    return m.group(1) if m else None


def _full(url: str) -> str:
    if url.startswith("http"):
        return url
    return SITE + (url if url.startswith("/") else "/" + url)


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


async def geocode(area: str, browser_solver=None) -> dict | None:
    """Resolve a place name to a viewport bbox + GEOId via SubAreaSearch."""
    s = await _get_session(browser_solver)
    r = await s.get(
        f"{API}/Location.svc/SubAreaSearch",
        params={"Area": area, "CurrentPage": "1", **_COMMON},
        headers=_API_HEADERS,
    )
    r.raise_for_status()
    subs = r.json().get("SubArea") or []
    if not subs:
        return None
    sa = subs[0]
    vp = sa.get("Viewport") or {}
    ne, sw = vp.get("NorthEast") or {}, vp.get("SouthWest") or {}
    bbox = None
    if ne.get("Latitude") and sw.get("Latitude"):
        bbox = {
            "LatitudeMax": ne["Latitude"], "LongitudeMax": ne["Longitude"],
            "LatitudeMin": sw["Latitude"], "LongitudeMin": sw["Longitude"],
        }
    return {"location": sa.get("Location") or area, "geoid": sa.get("GEOId") or "", "bbox": bbox}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _range(minimum: int | None) -> str | None:
    """A 'N or more' range in realtor.ca's ``min-max`` form (0 = unbounded)."""
    if not minimum or minimum < 1:
        return None
    return f"{minimum}-0"


async def property_search(
    *,
    bbox: dict | None = None,
    geoids: str = "",
    transaction: str = "sale",
    property_type: str = "any",
    building_type: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_beds: int | None = None,
    min_baths: int | None = None,
    ownership: str | None = None,
    sort: str = "newest",
    page: int = 1,
    records_per_page: int = 20,
    browser_solver=None,
) -> dict:
    """Run a PropertySearch_Post. Needs ``bbox`` and/or ``geoids`` for scope."""
    body: dict[str, str] = {
        "PropertyTypeGroupID": RESIDENTIAL_GROUP,
        "TransactionTypeId": TRANSACTION.get(transaction, "2"),
        "PropertySearchTypeId": PROPERTY_TYPE.get(property_type, "0"),
        "Sort": SORT.get(sort, "6-D"),
        "Currency": "CAD",
        "IncludeHiddenListings": "false",
        "RecordsPerPage": str(records_per_page),
        "CurrentPage": str(max(1, page)),
        **_COMMON,
    }
    if bbox:
        body.update({k: str(v) for k, v in bbox.items()})
    if geoids:
        body["GeoIds"] = geoids
    if building_type and building_type in BUILDING_TYPE:
        body["BuildingTypeId"] = BUILDING_TYPE[building_type]
    # Rent uses Rent{Min,Max}; sale uses Price{Min,Max}.
    if transaction == "rent":
        if min_price:
            body["RentMin"] = str(min_price)
        if max_price:
            body["RentMax"] = str(max_price)
    else:
        if min_price:
            body["PriceMin"] = str(min_price)
        if max_price:
            body["PriceMax"] = str(max_price)
    bed = _range(min_beds)
    if bed:
        body["BedRange"] = bed
    bath = _range(min_baths)
    if bath:
        body["BathRange"] = bath
    if ownership and ownership in OWNERSHIP:
        body["OwnershipTypeGroupId"] = OWNERSHIP[ownership]

    s = await _get_session(browser_solver)
    r = await s.post(f"{API}/Listing.svc/PropertySearch_Post", form=body, headers=_API_HEADERS)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Listing detail (SSR HTML)
# ---------------------------------------------------------------------------

_COORD_RE = re.compile(r"destination=([\d.\-]+)(?:%2c|%2C|,)([\d.\-]+)")
_GEOID_RE = re.compile(r"\bg[0-9]0_[a-z0-9]+\b")
_PRICE_DIGITS_RE = re.compile(r"[\d,]+")
_MLS_RE = re.compile(r"MLS\W{0,5}Number\W{0,4}([A-Z0-9]{5,})")


def _extract_mls(soup: BeautifulSoup) -> str:
    """MLS number lives in an ``id*=MLS`` element as 'MLS ® Number: X12345678'."""
    for el in soup.select("[id*=MLS], [id*=Mls]"):
        m = _MLS_RE.search(el.get_text(" ", strip=True))
        if m:
            return m.group(1)
    return ""


def _extract_rooms(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Room-by-room breakdown (name/level + metric dimensions) from the Rooms section."""
    rooms: list[tuple[str, str]] = []
    for rc in soup.select(".listingDetailsRoomDetailsCon"):
        dims_con = rc.select_one(".listingDetailsRoomDetails_DimensionsCon")
        metric_el = rc.select_one(".listingDetailsRoomDetails_Dimensions.Metric")
        metric = ""
        if metric_el:
            metric = metric_el.get_text(" ", strip=True)
        elif dims_con:
            metric = dims_con.get_text(" ", strip=True)
        full = rc.get_text(" ", strip=True)
        label = full.replace(dims_con.get_text(" ", strip=True), "").strip() if dims_con else full
        if label:
            rooms.append((label, metric))
    return rooms


def _extract_location_desc(soup: BeautifulSoup) -> str:
    """The 'Location Description' block (cross-streets / directions), if present."""
    el = soup.select_one("#LocationDescription, [id*=LocationDescription]")
    if not el:
        return ""
    return re.sub(r"^\s*Location Description\s*", "", el.get_text(" ", strip=True)).strip()


_BROKERAGE_KW = ("Brokerage", "Bureau de courtage")  # EN, FR
# Office card text after the brokerage name is the street address: a "#unit" or a
# multi-digit civic number. Used to trim when no brokerage keyword is present.
_OFFICE_ADDR_RE = re.compile(r"\s+(?:#\d|\d{2,}[\s-])")


def _extract_agent(soup: BeautifulSoup) -> tuple[str, str]:
    """Listing agent name + brokerage (both SSR-present on the detail page).

    The office card is ``<name> <Brokerage|Bureau de courtage> <address> <phone>``;
    keep through the brokerage keyword (EN or FR), else trim at the street address.
    """
    name_el = soup.select_one(".realtorCardName")
    agent = name_el.get_text(" ", strip=True) if name_el else ""
    brokerage = ""
    office_el = soup.select_one("[id^=OfficeCard], [id*=Office]")
    if office_el:
        txt = office_el.get_text(" ", strip=True)
        for kw in _BROKERAGE_KW:
            if kw in txt:
                brokerage = txt.split(kw, 1)[0].strip() + " " + kw
                break
        else:
            brokerage = _OFFICE_ADDR_RE.split(txt, 1)[0].strip()[:80]
    return agent, brokerage


def _text(soup: BeautifulSoup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def parse_listing_html(html: str, url: str) -> dict:
    """Pull the listing fields out of the SSR detail page."""
    soup = BeautifulSoup(html, "lxml")

    price = _text(soup, "#listingPriceValue")
    address = _text(soup, "#listingAddress")
    beds = _text(soup, "#galleryBeds")
    baths = _text(soup, "#galleryBaths")
    description = _text(soup, "#propertyDescriptionCon")

    # Property detail label/value pairs (dedup, preserve order).
    labels = soup.select(".propertyDetailsSectionContentLabel")
    values = soup.select(".propertyDetailsSectionContentValue")
    details: list[tuple[str, str]] = []
    seen: set[str] = set()
    for lab, val in zip(labels, values):
        key = lab.get_text(" ", strip=True)
        value = val.get_text(" ", strip=True)
        if not key or not value:
            continue
        dedup = f"{key}={value}"
        if dedup in seen:
            continue
        seen.add(dedup)
        details.append((key, value))

    # Coordinates (from the "directions" link) for similar-listings lookup.
    coords = None
    cm = _COORD_RE.search(html)
    if cm:
        try:
            coords = (float(cm.group(1)), float(cm.group(2)))
        except ValueError:
            coords = None

    mls = _extract_mls(soup)
    rooms = _extract_rooms(soup)
    location_description = _extract_location_desc(soup)
    agent, brokerage = _extract_agent(soup)

    price_value = None
    if price:
        pm = _PRICE_DIGITS_RE.search(price)
        if pm:
            try:
                price_value = int(pm.group(0).replace(",", ""))
            except ValueError:
                price_value = None

    return {
        "url": _full(url),
        "id": extract_listing_id(url),
        "price": price,
        "price_value": price_value,
        "address": address,
        "beds": beds,
        "baths": baths,
        "description": description,
        "location_description": location_description,
        "details": details,
        "rooms": rooms,
        "coords": coords,
        "mls": mls,
        "agent": agent,
        "brokerage": brokerage,
    }


async def get_listing_detail(url: str, browser_solver=None) -> dict:
    """Fetch + parse an individual listing page."""
    s = await _get_session(browser_solver)
    r = await s.get(_full(url), headers={"Referer": SITE + "/"})
    r.raise_for_status()
    return parse_listing_html(r.text, url)


async def get_similar_listings(parsed: dict, browser_solver=None, limit: int = 6) -> list[dict]:
    """Reconstruct "similar listings": for-sale homes near this one, similar price."""
    coords = parsed.get("coords")
    if not coords:
        return []
    lat, lng = coords
    pad_lat, pad_lng = 0.05, 0.07  # ~5-8km box
    bbox = {
        "LatitudeMax": f"{lat + pad_lat:.6f}", "LongitudeMax": f"{lng + pad_lng:.6f}",
        "LatitudeMin": f"{lat - pad_lat:.6f}", "LongitudeMin": f"{lng - pad_lng:.6f}",
    }
    pv = parsed.get("price_value")
    min_price = int(pv * 0.6) if pv else None
    max_price = int(pv * 1.5) if pv else None
    try:
        data = await property_search(
            bbox=bbox, transaction="sale", sort="newest",
            min_price=min_price, max_price=max_price,
            records_per_page=limit + 4, browser_solver=browser_solver,
        )
    except wafer.WaferError:
        return []
    own_id = str(parsed.get("id") or "")
    out: list[dict] = []
    for res in data.get("Results") or []:
        if str(res.get("Id")) == own_id:
            continue
        out.append(res)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Search-URL handling (SEO pages + /map)
# ---------------------------------------------------------------------------

_BBOX_KEYS = ("LatitudeMax", "LongitudeMax", "LatitudeMin", "LongitudeMin")


def _map_kwargs(url: str) -> dict:
    """Parse a /map URL's query+hash params into ``property_search`` kwargs."""
    parsed = urlparse(url)
    raw: dict[str, str] = {}
    for blob in (parsed.query, parsed.fragment):
        if not blob:
            continue
        for k, vals in parse_qs(blob, keep_blank_values=False).items():
            if vals:
                raw[k] = unquote(vals[0])

    bbox = {k: raw[k] for k in _BBOX_KEYS if raw.get(k)}
    transaction = {"3": "rent"}.get(raw.get("TransactionTypeId", ""), "sale")
    kwargs: dict = {
        "bbox": bbox or None,
        "geoids": raw.get("GeoIds", ""),
        "transaction": transaction,
        "property_type": _invert(PROPERTY_TYPE, raw.get("PropertySearchTypeId"), "any"),
        "building_type": _invert(BUILDING_TYPE, raw.get("BuildingTypeId"), None),
        "ownership": _invert(OWNERSHIP, raw.get("OwnershipTypeGroupId"), None),
        "sort": _invert(SORT, raw.get("Sort"), "newest"),
        "page": int(raw.get("CurrentPage", "1") or "1"),
    }
    price_min = raw.get("RentMin") if transaction == "rent" else raw.get("PriceMin")
    price_max = raw.get("RentMax") if transaction == "rent" else raw.get("PriceMax")
    kwargs["min_price"] = int(price_min) if price_min and price_min.isdigit() else None
    kwargs["max_price"] = int(price_max) if price_max and price_max.isdigit() else None
    kwargs["min_beds"] = _range_min(raw.get("BedRange"))
    kwargs["min_baths"] = _range_min(raw.get("BathRange"))
    return {"kwargs": kwargs, "geoname": raw.get("GeoName", "")}


def _range_min(rng: str | None) -> int | None:
    """Inverse of ``_range``: ``'3-0'`` -> 3."""
    if not rng or "-" not in rng:
        return None
    head = rng.split("-", 1)[0]
    return int(head) if head.isdigit() and int(head) > 0 else None


async def search_from_url(url: str, browser_solver=None) -> dict:
    """Resolve a realtor.ca search/SEO/map URL to a PropertySearch result.

    Returns ``{"data":..., "location":..., "transaction":..., "page":...}``.
    """
    path = urlparse(url).path
    transaction = "rent" if re.search(r"for-rent|/rent\b|location-immobiliere", url, re.I) else "sale"

    # /map URL: bbox + GeoIds + filters live in the query/hash fragment.
    if path.rstrip("/") == "/map":
        mp = _map_kwargs(url)
        kwargs = mp["kwargs"]
        data = await property_search(browser_solver=browser_solver, **kwargs)
        return {
            "data": data,
            "location": mp["geoname"] or "map area",
            "transaction": kwargs["transaction"],
            "page": kwargs["page"],
        }

    # SEO page: fetch it to read the embedded GeoIds + place name, then search.
    s = await _get_session(browser_solver)
    r = await s.get(_full(url), headers={"Referer": SITE + "/"})
    r.raise_for_status()
    html = r.text
    # The page's own area GeoId is the dominant one; pick the most frequent.
    geoids = ""
    gmatches = _GEOID_RE.findall(html)
    if gmatches:
        geoids = max(set(gmatches), key=gmatches.count)
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    place = h1.get_text(" ", strip=True) if h1 else ""
    place = re.sub(r"\s*Real Estate.*$|\s*Homes.*$|\s*Properties.*$", "", place, flags=re.I).strip()

    bbox = None
    if not geoids:
        # No embedded GeoId — geocode the place name from the slug/H1.
        area = place or _place_from_slug(path)
        geo = await geocode(area, browser_solver)
        if geo:
            geoids = geo["geoid"]
            bbox = geo["bbox"]
            place = geo["location"]
    data = await property_search(bbox=bbox, geoids=geoids, transaction=transaction,
                                 sort="newest", browser_solver=browser_solver)
    return {"data": data, "location": place or "search area", "transaction": transaction, "page": 1}


def _invert(mapping: dict, code: str | None, default: str) -> str:
    if not code:
        return default
    for k, v in mapping.items():
        if v == code:
            return k
    return default


def _place_from_slug(path: str) -> str:
    """Turn ``/on/ottawa/orleans/real-estate`` into ``orleans, ottawa``."""
    parts = [p for p in path.split("/") if p and p not in ("real-estate", "immobilier")]
    # Drop a leading 2-letter province code.
    if parts and len(parts[0]) == 2:
        parts = parts[1:]
    parts = [p.replace("-", " ") for p in parts]
    parts.reverse()
    return ", ".join(parts[:2])
