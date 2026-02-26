"""Craigslist SAPI client.

Direct interface to ``sapi.craigslist.org`` for structured search results.
Returns up to 120 items per request with total result count and full metadata.

The SAPI is Craigslist's own frontend search API — no auth required.

Area IDs are resolved from CL page HTML on first request per hostname,
then cached in memory for subsequent calls.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import craigslist_limiter

_SAPI_BASE = "https://sapi.craigslist.org/web/v8/postings/search"

_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# In-memory cache: hostname → area_id (populated from HTML extraction)
_area_id_cache: dict[str, int] = {}

_AREA_ID_RE = re.compile(r'"areaId"\s*:\s*(\d+)')


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] craigslist sapi: {msg}", file=sys.stderr)


_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(max_rotations=0, cache_dir=get_wafer_cache_dir())
    return _session


async def close_session() -> None:
    global _session
    _session = None


# ---------------------------------------------------------------------------
# Area ID resolution
# ---------------------------------------------------------------------------


def extract_area_id(html: str) -> int | None:
    """Extract the numeric area ID from CL page HTML."""
    m = _AREA_ID_RE.search(html)
    return int(m.group(1)) if m else None


def get_cached_area_id(hostname: str) -> int | None:
    """Get cached area ID for hostname, or None if not cached."""
    return _area_id_cache.get(hostname)


def cache_area_id(hostname: str, area_id: int) -> None:
    """Store hostname → area_id mapping in memory cache."""
    _area_id_cache[hostname] = area_id


# ---------------------------------------------------------------------------
# SAPI request
# ---------------------------------------------------------------------------

# SAPI query params we forward from the CL search URL
_FORWARDED_PARAMS = {
    "query", "sort", "min_price", "max_price", "hasPic",
    "postedToday", "bundleDuplicates", "srchType", "purveyor",
    "condition", "auto_make_model", "min_auto_year", "max_auto_year",
    "min_auto_miles", "max_auto_miles", "auto_transmission",
    "auto_drivetrain", "auto_bodytype", "auto_fuel_type",
    "auto_title_status", "subarea", "free", "delivery_available",
}


async def fetch_sapi(
    area_id: int,
    category: str,
    extra_params: dict | None = None,
) -> dict | None:
    """Call the Craigslist SAPI and return parsed JSON response.

    Args:
        area_id: Numeric area ID (e.g., 11 for Chicago).
        category: Category abbreviation (e.g., "sss", "cta").
        extra_params: Additional query params (query, sort, min_price, etc.).

    Returns:
        Parsed JSON dict with ``data.items``, ``data.totalResultCount``, etc.
        None if the request fails.
    """
    await craigslist_limiter.wait()

    params: dict[str, str | int] = {
        "area_id": area_id,
        "lang": "en",
        "searchPath": category,
    }
    if extra_params:
        for key, val in extra_params.items():
            if key in _FORWARDED_PARAMS and val:
                params[key] = val

    url = f"{_SAPI_BASE}?{urlencode(params)}"
    _log(f"SAPI request: {url}")

    try:
        session = await _get_session()
        resp = await session.get(url, headers=_HEADERS, timeout=15)
        if resp.status_code != 200:
            _log(f"SAPI HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except wafer.WaferTimeout:
        _log("SAPI request timed out")
        return None
    except (wafer.WaferError, json.JSONDecodeError) as e:
        _log(f"SAPI request failed: {e}")
        return None

    # Validate response structure
    if not isinstance(data, dict) or "data" not in data:
        _log(f"SAPI unexpected response shape: {str(data)[:200]}")
        return None

    errors = data.get("errors", [])
    if errors:
        _log(f"SAPI errors: {errors}")
        return None

    return data


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------


def _format_relative_time(ts: int) -> str:
    """Format a Unix timestamp as a relative time string (e.g., '2h ago', '3d ago')."""
    now = int(time.time())
    delta = now - ts
    if delta < 0:
        return ""
    if delta < 3600:
        mins = max(1, delta // 60)
        return f"{mins}m ago"
    if delta < 86400:
        hours = delta // 3600
        return f"{hours}h ago"
    days = delta // 86400
    if days < 30:
        return f"{days}d ago"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%b %d")


def parse_sapi_items(data: dict) -> list[dict]:
    """Parse SAPI response into a list of formatted item dicts.

    Each item has: ``title``, ``url``, ``price_str``, ``location``,
    ``posted_str``.
    """
    items_raw = data.get("data", {}).get("items", [])
    results: list[dict] = []

    for item in items_raw:
        if not isinstance(item, dict):
            continue

        title = item.get("title", "")
        if not title:
            continue

        # Build listing URL from item fields
        loc = item.get("location", {})
        hostname = loc.get("hostname", "")
        subarea = loc.get("subareaAbbr", "")
        cat_abbr = item.get("categoryAbbr", "")
        seo = item.get("seo", "")
        posting_id = item.get("postingId", "")

        item_url = ""
        if hostname and posting_id:
            path_parts = [p for p in [subarea, cat_abbr, "d", seo, str(posting_id)] if p]
            item_url = f"https://{hostname}.craigslist.org/{'/'.join(path_parts)}.html"

        # Price
        price_str = item.get("priceString", "")

        # Location description
        location = loc.get("description", "")

        # Posted timestamp → human-readable
        posted_ts = item.get("postedDate")
        posted_str = _format_relative_time(posted_ts) if isinstance(posted_ts, int) else ""

        results.append({
            "title": title,
            "url": item_url,
            "price_str": price_str,
            "location": location,
            "posted_str": posted_str,
        })

    return results


def get_total_count(data: dict) -> int | None:
    """Extract total result count from SAPI response."""
    return data.get("data", {}).get("totalResultCount")


def get_area_name(data: dict) -> str:
    """Extract human-readable area name from SAPI response.

    Uses ``data.location.city`` which is partially capitalized
    (e.g., "new york city", "chicago", "vancouver, BC", "seattle-tacoma").
    Capitalizes lowercase words (including after hyphens) but preserves
    already-uppercase abbreviations like BC, NY, SF, etc.
    """
    loc = data.get("data", {}).get("location", {})
    city = loc.get("city", "")
    if not city:
        return ""

    def _cap_word(w: str) -> str:
        if w.islower():
            return "-".join(part.capitalize() for part in w.split("-"))
        return w

    return " ".join(_cap_word(w) for w in city.split())
