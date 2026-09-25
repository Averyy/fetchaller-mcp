"""Read the search a Marketplace page runs, from the page itself.

A search URL names a Facebook page, and the logged-out page answers it
completely before any script runs. Its ``expectedPreloaders`` carry the query
Facebook ran for it — doc_id and variables, with the URL's location slug
already resolved to coordinates and every URL filter (price, radius, condition,
sort) applied — and it streams that query's first page of results into
``RelayPrefetchedStreamCache`` as the same JSON a GraphQL call returns. Reading
it is one request and matches facebook.com exactly.

Rebuilding the search instead got two things wrong on the same URL
(2026-09-25): the slug was geocoded as free text, which put
``/marketplace/vancouver/`` in Vancouver, Washington (the page says BC, 49.28
-123.12), and the URL's ``minPrice``/``maxPrice`` were sent as cents although
they are whole units (the page turns ``minPrice=100`` into a 10000 bound).

A city *browse* page (``/marketplace/toronto/``) runs a different query, a
feed, and streams it an edge at a time: a "top picks" unit carrying 20
listings, then single listings (26 in all on 2026-09-25). That first screen is
all the logged-out feed serves — the same query over GraphQL with ``count=24``
answers with the same 1 + 6 edges — so it is read from the page. An
empty-query *search* for the same city returns nothing, which is how the city
page used to render as "No listings found".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from ..ratelimit import facebook_limiter
from .graphql import _get_session, mark_session_seeded

SEARCH_QUERY_NAME = "CometMarketplaceSearchContentContainerQuery"
BROWSE_QUERY_PREFIX = "MarketplaceCometBrowseFeed"

_PRELOADERS_KEY = '"expectedPreloaders":'
_STREAM_RE = re.compile(r'\["RelayPrefetchedStreamCache","next",\[\],\["(adp_[A-Za-z0-9_]+)",')
_DECODER = json.JSONDecoder()
_SPACE_RE = re.compile(r"\s*")


def _decode_at(text: str, index: int):
    """``raw_decode`` from ``index``, past any whitespace (which it rejects)."""
    return _DECODER.raw_decode(text, _SPACE_RE.match(text, index).end())


@dataclass(frozen=True)
class SearchPage:
    latitude: float | None = None
    longitude: float | None = None
    location_id: str = ""
    city: str = ""
    radius_km: int | None = None
    doc_id: str = ""
    variables: dict | None = None
    # The search query's streamed answer, shaped like a GraphQL response.
    result: dict | None = None
    # A browse page's streamed feed, as listing dicts (None: not a browse page).
    browse: list[dict] | None = None


def _expected_preloaders(html: str) -> list[dict]:
    found: list[dict] = []
    start = 0
    while (i := html.find(_PRELOADERS_KEY, start)) >= 0:
        start = i + len(_PRELOADERS_KEY)
        try:
            value, _ = _decode_at(html, start)
        except ValueError:
            continue
        if isinstance(value, list):
            found.extend(p for p in value if isinstance(p, dict))
    return found


def _stream_chunks(html: str) -> dict[str, list[dict]]:
    """``preloaderID`` -> every streamed result carrying data, in page order.

    A query answers in one chunk; a feed with ``@stream`` answers in one chunk
    per edge after the first.
    """
    chunks: dict[str, list[dict]] = {}
    for m in _STREAM_RE.finditer(html):
        try:
            payload, _ = _decode_at(html, m.end())
        except ValueError:
            continue
        bbox = payload.get("__bbox") if isinstance(payload, dict) else None
        result = bbox.get("result") if isinstance(bbox, dict) else None
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            chunks.setdefault(m.group(1), []).append(result)
    return chunks


def _dig(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _city(data) -> str:
    """The location the page searched from, named the way the page names it."""
    for path in (
        ("viewer", "buy_location", "buy_location", "location", "reverse_geocode", "city"),
        ("viewer", "marketplace_feed_stories", "buy_location", "display_name"),
    ):
        city = _dig(data, *path)
        if isinstance(city, str) and city.strip():
            return city.strip()
    return ""


def _place(location) -> str:
    geo = _dig(location, "reverse_geocode")
    name = _dig(geo, "city_page", "display_name") or _dig(geo, "city")
    return name.strip() if isinstance(name, str) else ""


# Currencies whose `amount_with_offset` scale has been checked against a live
# listing (2500 on a CAD item titled "$25"). Anything else prints unscaled and
# says so: the JP store lesson in CLAUDE.md is that a flat /100 is a silent 100x
# error on a zero-decimal currency.
_OFFSET_100 = frozenset({"CAD", "USD", "EUR", "GBP", "AUD", "NZD", "MXN"})


def _offset_price(price) -> str:
    currency = _dig(price, "currency")
    raw = _dig(price, "amount_with_offset")
    if not isinstance(currency, str) or not isinstance(raw, (str, int)) or isinstance(raw, bool):
        return ""
    try:
        minor = int(raw)
    except ValueError:
        return ""
    if currency in _OFFSET_100:
        whole, cents = divmod(minor, 100)
        return f"{currency} {whole:,}" + (f".{cents:02d}" if cents else "")
    return f"{currency} {minor} (amount_with_offset, unscaled)"


def _listing_url(listing_id: str) -> str:
    return f"https://www.facebook.com/marketplace/item/{listing_id}/"


def _top_pick(item: dict) -> dict | None:
    listing_id = str(item.get("id") or "")
    title = item.get("marketplace_listing_title") or item.get("custom_title") or ""
    if not listing_id or not title:
        return None
    out = {"id": listing_id, "title": title, "url": _listing_url(listing_id)}
    price = _dig(item, "formatted_price", "text")
    if isinstance(price, str) and price:
        out["price"] = price
    place = _place(item.get("location"))
    if place:
        out["location"] = place
    if item.get("is_sold") is True:
        out["status"] = "Sold"
    elif item.get("is_pending") is True:
        out["status"] = "Pending"
    return out


def _general(node: dict) -> dict | None:
    listing_id = str(_dig(node, "listing", "id") or node.get("entity_id") or "")
    title = _dig(node, "data", "title")
    if not listing_id or not isinstance(title, str) or not title:
        return None
    out = {"id": listing_id, "title": title, "url": _listing_url(listing_id)}
    price = _offset_price(_dig(node, "data", "price"))
    if price:
        out["price"] = price
    original = _offset_price(node.get("strikethrough_price"))
    if original:
        out["original_price"] = original
    place = _place(_dig(node, "entity", "location"))
    if place:
        out["location"] = place
    return out


def parse_browse_feed(chunks: list[dict]) -> list[dict]:
    """Listings from a browse feed's streamed chunks, in feed order, once each."""
    nodes: list[dict] = []
    for chunk in chunks:
        data = chunk.get("data") or {}
        edges = _dig(data, "marketplace_home_feed", "edges")
        if isinstance(edges, list):
            nodes.extend(e["node"] for e in edges if isinstance(e, dict) and isinstance(e.get("node"), dict))
        if isinstance(data.get("node"), dict):
            nodes.append(data["node"])
    listings: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        kind = node.get("__typename")
        if kind == "MarketplaceFeedTopPicksUnit":
            found = [_top_pick(i) for i in node.get("marketplace_listings") or [] if isinstance(i, dict)]
        elif kind == "MarketplaceFeedGeneralListingObject":
            found = [_general(node)]
        else:
            found = []
        for item in found:
            if item and item["id"] not in seen:
                seen.add(item["id"])
                listings.append(item)
    return listings


def _coordinate(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_search_page(html: str) -> SearchPage | None:
    """What the page resolved and ran, or ``None`` when it names no location
    (a login wall or checkpoint carries no Marketplace preloaders)."""
    preloaders = _expected_preloaders(html)
    chunks = _stream_chunks(html)
    streams = {pid: parts[0] for pid, parts in chunks.items()}

    search = next((p for p in preloaders if p.get("queryName") == SEARCH_QUERY_NAME), None)
    located = [search] if search else []
    located += [p for p in preloaders if p is not search]
    buy = None
    location_id = ""
    for p in located:
        variables = p.get("variables") if isinstance(p.get("variables"), dict) else {}
        if buy is None and isinstance(variables.get("buyLocation"), dict):
            buy = variables["buyLocation"]
        topic = variables.get("topicPageParams")
        if not location_id and isinstance(topic, dict) and isinstance(topic.get("location_id"), str):
            location_id = topic["location_id"]
    latitude = _coordinate((buy or {}).get("latitude"))
    longitude = _coordinate((buy or {}).get("longitude"))
    if search is None and (latitude is None or longitude is None):
        return None

    result = None
    variables = None
    doc_id = ""
    radius = None
    if search is not None:
        doc_id = str(search.get("queryID") or "")
        variables = search.get("variables") if isinstance(search.get("variables"), dict) else None
        stream = streams.get(str(search.get("preloaderID") or ""))
        if stream is not None and isinstance(stream["data"].get("marketplace_search"), dict):
            result = stream
        try:
            radius_value = variables["params"]["browse_request_params"]["filter_radius_km"]
        except (KeyError, TypeError):
            radius_value = None
        if isinstance(radius_value, int) and not isinstance(radius_value, bool):
            radius = radius_value

    browse = None
    feed = next((p for p in preloaders if str(p.get("queryName") or "").startswith(BROWSE_QUERY_PREFIX)), None)
    if search is None and feed is not None:
        feed_chunks = chunks.get(str(feed.get("preloaderID") or ""), [])
        browse = parse_browse_feed(feed_chunks)
        feed_radius = _dig(feed, "variables", "radius")
        if isinstance(feed_radius, int) and not isinstance(feed_radius, bool) and feed_radius > 0:
            radius = round(feed_radius / 1000)  # the feed takes metres

    city = _city(result.get("data")) if result else ""
    if not city:
        # A browse feed names its city on the top-picks node, not the root.
        candidates: list = []
        for parts in chunks.values():
            for part in parts:
                data = part.get("data") or {}
                candidates.append(data)
                candidates.append(data.get("node"))
                edges = _dig(data, "marketplace_home_feed", "edges") or []
                candidates.extend(e.get("node") for e in edges if isinstance(e, dict))
        city = next((c for c in map(_city, candidates) if c), "")

    return SearchPage(
        latitude=latitude,
        longitude=longitude,
        location_id=location_id,
        city=city,
        radius_km=radius,
        doc_id=doc_id,
        variables=variables,
        result=result,
        browse=browse,
    )


def _page_url(url: str) -> str:
    """The desktop page for any Marketplace host (m., web., www.)."""
    parsed = urlparse(url)
    return urlunparse(("https", "www.facebook.com", parsed.path or "/", "", parsed.query, ""))


async def fetch_search_page(url: str) -> SearchPage | None:
    """GET the page the URL names and read its search. ``None`` on any failure,
    so the caller can fall back to building the search itself."""
    await facebook_limiter.wait()
    session = await _get_session(seed=False)
    try:
        resp = await session.get(_page_url(url), timeout=20)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    # The page visit is exactly what seeding does, so it counts as one.
    mark_session_seeded()
    return parse_search_page(resp.text)
