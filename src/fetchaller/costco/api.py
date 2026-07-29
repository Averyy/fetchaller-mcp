"""Costco search API client.

Direct interface to ``search.costco.com`` / ``search.costco.ca`` for structured
product search results. Returns up to 24 items per request with total result
count and full product metadata.

Auth is via a static ``x-api-key`` header (UUID) embedded in the SSR HTML of
any Costco search page. Keys are cached in memory and refreshed on 401.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import costco_limiter
from ..security.xss import safe_log_text

# Default API keys (extracted from Costco search pages)
_DEFAULT_KEYS: dict[str, str] = {
    "com": "273db6be-f015-4de7-b0d6-dd4746ccd5c3",
    "ca": "134a4023-68d5-4138-8e03-8353667d5fb3",
}

# In-memory cache: domain → API key
_api_keys: dict[str, str] = {}

# Domains where the hardcoded default has been invalidated (got 401)
_default_keys_exhausted: set[str] = set()


def _key_cache_path() -> Path | None:
    """Return path to the on-disk API key cache file, or None if no cache dir."""
    cache_dir = get_wafer_cache_dir()
    if not cache_dir:
        return None
    return Path(cache_dir) / "costco_api_keys.json"


def _load_cached_keys() -> None:
    """Load API keys from disk into memory cache on startup."""
    path = _key_cache_path()
    if not path or not path.exists():
        return
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            for domain, key in data.items():
                if isinstance(key, str) and len(key) == 36:
                    _api_keys[domain] = key
            if _api_keys:
                _log(f"Loaded {len(_api_keys)} cached API key(s) from disk")
                return
        # Valid JSON but wrong shape or no usable keys — delete it
        _log("Cache file has no usable keys, deleting")
        path.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError):
        _log("Corrupt cache file, deleting")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _save_cached_keys() -> None:
    """Persist current API keys to disk."""
    path = _key_cache_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_api_keys))
    except OSError:
        pass


_keys_loaded = False

# Regex to extract API key from Costco page HTML
# Matches both escaped JSON (\"apikey\",\"value\":\"UUID\") and plain JSON ("apikey","value":"UUID")
_API_KEY_RE = re.compile(r'[\\"]apikey[\\"][\s,]*[\\"]value[\\"][\s:]*[\\"]([0-9a-f\-]{36})[\\"]')

_LOCALE_MAP: dict[str, str] = {
    "com": "en-US",
    "ca": "en-CA",
}


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] costco api: "
        f"{safe_log_text(msg)}",
        file=sys.stderr,
    )


_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    max_rotations=0,
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=10 * 1024 * 1024,
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------


def _extract_api_key(html: str) -> str | None:
    """Extract the API key UUID from Costco page HTML."""
    m = _API_KEY_RE.search(html)
    return m.group(1) if m else None


async def _resolve_api_key(domain: str, session: wafer.AsyncSession) -> str:
    """Return the API key for the given domain.

    Uses cached key if available. On 401, fetches a Costco search page
    to extract a fresh key.
    """
    global _keys_loaded
    if not _keys_loaded:
        _load_cached_keys()
        _keys_loaded = True

    # Check cache first (includes keys loaded from disk)
    if domain in _api_keys:
        return _api_keys[domain]

    # Use hardcoded default (only if it hasn't already been invalidated by a 401)
    if domain not in _default_keys_exhausted:
        key = _DEFAULT_KEYS.get(domain)
        if key:
            _api_keys[domain] = key
            return key

    # Fallback: fetch a search page and extract the key
    return await _refresh_api_key(domain, session)


async def _refresh_api_key(domain: str, session: wafer.AsyncSession) -> str:
    """Fetch a Costco search page and extract a fresh API key."""
    # Evict the stale key and mark default as exhausted so we don't loop
    _api_keys.pop(domain, None)
    _default_keys_exhausted.add(domain)

    url = f"https://www.costco.{domain}/s?keyword=test"
    _log(f"Fetching API key for .{domain}")

    try:
        resp = await session.get(url, timeout=15)
        if resp.status_code == 200:
            key = _extract_api_key(resp.text)
            if key:
                _api_keys[domain] = key
                _default_keys_exhausted.discard(domain)
                _save_cached_keys()
                _log(f"Extracted API key for .{domain}")
                return key
            else:
                _log(f"Could not extract API key from .{domain} HTML ({len(resp.text)} chars)")
        else:
            _log(f"Failed to fetch .{domain} search page: HTTP {resp.status_code}")
    except (wafer.WaferError, wafer.WaferTimeout) as e:
        _log(
            f"Failed to fetch .{domain} search page: "
            f"{type(e).__name__}"
        )

    # Refresh failed — return empty so caller knows to bail out.
    # Don't return the hardcoded default here; it likely just got a 401.
    return ""


# ---------------------------------------------------------------------------
# Search API
# ---------------------------------------------------------------------------


async def search(
    query: str,
    domain: str = "com",
    start: int = 0,
    rows: int = 24,
) -> dict | None:
    """Search Costco via the search API.

    Args:
        query: Search terms.
        domain: ``"com"`` or ``"ca"``.
        start: Result offset for pagination.
        rows: Number of results per page (max 24).

    Returns:
        Parsed JSON response dict, or None on failure.
    """
    await costco_limiter.wait()

    session = await _get_session()
    api_key = await _resolve_api_key(domain, session)
    if not api_key:
        _log("No API key available")
        return None

    domain_code = domain  # "com" or "ca"
    locale = _LOCALE_MAP.get(domain, "en-US")

    endpoint = (
        f"https://search.costco.{domain}/api/apps/www_costco_{domain_code}"
        f"/query/www_costco_{domain_code}_search"
    )

    params = {
        "expoption": "def",
        "q": query,
        "locale": locale,
        "start": str(start),
        "expand": "false",
        "loc": "*",
        "whloc": "1-wh",
        "rows": str(rows),
        "chdcategory": "true",
        "chdheader": "true",
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": f"https://www.costco.{domain}/",
        "Origin": f"https://www.costco.{domain}",
        "x-api-key": api_key,
    }

    url = f"{endpoint}?{urlencode(params)}"
    _log(f"Search request for .{domain}")

    try:
        resp = await session.get(url, headers=headers, timeout=15)
    except wafer.WaferTimeout:
        _log("Search request timed out")
        return None
    except wafer.WaferError as e:
        _log(f"Search request failed: {type(e).__name__}")
        return None

    # On 401, refresh the API key and retry once
    if resp.status_code == 401:
        _log(f"Got 401, refreshing API key for .{domain}")
        api_key = await _refresh_api_key(domain, session)
        if not api_key:
            _log("Could not refresh API key")
            return None

        headers["x-api-key"] = api_key
        url = f"{endpoint}?{urlencode(params)}"

        try:
            await costco_limiter.wait()
            resp = await session.get(url, headers=headers, timeout=15)
        except wafer.WaferTimeout:
            _log("Retry request timed out")
            return None
        except wafer.WaferError as e:
            _log(f"Retry request failed: {type(e).__name__}")
            return None

    if resp.status_code == 401:
        # Refreshed key also got 401 — evict it so next call tries fresh
        _api_keys.pop(domain, None)
        _log(f"Search HTTP 401 after key refresh for .{domain}")
        return None

    if resp.status_code != 200:
        _log(
            f"Search HTTP {resp.status_code} "
            f"(response_chars={len(resp.text)})"
        )
        return None

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        _log(f"Failed to parse JSON response: {type(e).__name__}")
        return None

    if not isinstance(data, dict) or "response" not in data:
        _log(f"Unexpected response shape: {type(data).__name__}")
        return None

    return data


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_search_items(data: dict, domain: str = "com") -> list[dict]:
    """Parse Costco search API response into a list of product dicts.

    Each item has: ``title``, ``item_number``, ``price``, ``sale_price``,
    ``rating``, ``reviews``, ``brand``, ``stock``, ``url``, ``image``,
    ``description``, ``features``.
    """
    docs = data.get("response", {}).get("docs", [])
    results: list[dict] = []

    for doc in docs:
        if not isinstance(doc, dict):
            continue

        title = doc.get("item_product_name", "")
        if not title:
            continue

        # Price
        price = doc.get("item_location_pricing_listPrice")
        sale_price = doc.get("item_location_pricing_salePrice")

        # Rating / reviews
        rating = doc.get("item_review_ratings")
        reviews = doc.get("item_product_review_count")

        # Brand (array field, take first)
        brand_list = doc.get("Brand_attr", [])
        brand = brand_list[0] if isinstance(brand_list, list) and brand_list else ""

        # Stock status
        stock = doc.get("item_location_stockStatus", "")

        # Item number
        item_number = doc.get("item_number", "")

        # URL — construct from slug or item number
        slug = doc.get("slug", "")
        url = ""
        if slug:
            url = f"https://www.costco.{domain}/{slug}.html"
        elif item_number:
            url = f"https://www.costco.{domain}/p/-/{item_number}"

        # Image
        image = doc.get("item_product_primary_image", "")

        # Description and features
        description = doc.get("item_short_description", "")
        raw_features = doc.get("item_product_marketing_features", [])
        if not isinstance(raw_features, list):
            raw_features = []
        # Features may contain semicolon-separated values — split them
        features: list[str] = []
        for feat in raw_features:
            if isinstance(feat, str):
                features.extend(f.strip() for f in feat.split(";") if f.strip())
            else:
                features.append(str(feat))

        results.append({
            "title": title,
            "item_number": item_number,
            "price": price,
            "sale_price": sale_price,
            "rating": rating,
            "reviews": reviews,
            "brand": brand,
            "stock": stock,
            "url": url,
            "image": image,
            "description": description,
            "features": features,
        })

    return results


def get_total_count(data: dict) -> int:
    """Extract total result count from Costco search API response."""
    return data.get("response", {}).get("numFound", 0)
