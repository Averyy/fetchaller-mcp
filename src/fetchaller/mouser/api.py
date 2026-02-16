"""Mouser Search API client.

Uses the official Mouser API (free, key-based) for structured product data.
Falls through to HTML pipeline (botfighter) when no API key is configured.

API docs: https://api.mouser.com/api/docs/ui/index
Rate limit: 30 requests/minute.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from ..ratelimit import mouser_limiter

_API_BASE = "https://api.mouser.com/api/v2"


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] mouser api: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# /ProductDetail/Texas-Instruments/LM358P?qs=...
_PRODUCT_PATH_RE = re.compile(r"/ProductDetail/([^/]+)/([^/?]+)")

# /Search/Refine?Keyword=LM358
_SEARCH_PATH_RE = re.compile(r"/Search/Refine", re.I)


def extract_mpn_from_url(url: str) -> str | None:
    """Extract manufacturer part number from a Mouser product URL."""
    m = _PRODUCT_PATH_RE.search(urlparse(url).path)
    if m:
        return m.group(2)
    return None


def extract_search_keyword(url: str) -> str | None:
    """Extract keyword from a Mouser search URL."""
    parsed = urlparse(url)
    if _SEARCH_PATH_RE.search(parsed.path):
        qs = parse_qs(parsed.query)
        kw = qs.get("Keyword") or qs.get("keyword")
        if kw:
            return kw[0]
    return None


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------


async def search_by_keyword(
    keyword: str,
    api_key: str,
    records: int = 10,
    page: int = 1,
) -> dict:
    """Search Mouser by keyword. Returns raw API response dict."""
    await mouser_limiter.wait()
    url = f"{_API_BASE}/search/keyword"
    payload = {
        "SearchByKeywordRequest": {
            "keyword": keyword,
            "records": records,
            "startingRecord": (page - 1) * records,
            "searchOptions": "",
            "searchWithYourSignUpLanguage": "",
        }
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            params={"apiKey": api_key},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def search_by_part_number(
    part_numbers: list[str],
    api_key: str,
) -> dict:
    """Search Mouser by part number(s). Max 10, pipe-delimited."""
    await mouser_limiter.wait()
    url = f"{_API_BASE}/search/partnumber"
    payload = {
        "SearchByPartRequest": {
            "mouserPartNumber": "|".join(part_numbers[:10]),
        }
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            params={"apiKey": api_key},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_part(part: dict) -> str:
    """Format a single Mouser part into markdown."""
    lines: list[str] = []

    desc = part.get("Description", "")
    mpn = part.get("ManufacturerPartNumber", "")
    mfr = part.get("Manufacturer", "")

    lines.append(f"# {mpn}" + (f" \u2014 {desc}" if desc else ""))
    lines.append("")

    if mfr:
        lines.append(f"**Manufacturer:** {mfr}")
    if mpn:
        lines.append(f"**Manufacturer Part #:** {mpn}")
    mouser_pn = part.get("MouserPartNumber", "")
    if mouser_pn:
        lines.append(f"**Mouser Part #:** {mouser_pn}")

    category = part.get("Category", "")
    if category:
        lines.append(f"**Category:** {category}")
    if desc:
        lines.append(f"**Description:** {desc}")

    lifecycle = part.get("LifecycleStatus", "")
    if lifecycle:
        lines.append(f"**Status:** {lifecycle}")

    rohs = part.get("ROHSStatus", "")
    if rohs:
        lines.append(f"**RoHS:** {rohs}")

    datasheet = part.get("DataSheetUrl", "")
    if datasheet:
        lines.append(f"**Datasheet:** {datasheet}")

    # Stock & Pricing
    # Prefer numeric AvailabilityInStock, fall back to Availability string
    avail = part.get("AvailabilityInStock") or part.get("Availability", "")
    lead_time = part.get("LeadTime", "")
    moq = part.get("Min", "")
    mult = part.get("Mult", "")

    lines.append("")
    lines.append("## Stock & Pricing")
    lines.append("")
    if avail:
        # Format number with commas if numeric
        try:
            avail_num = int(str(avail).replace(",", ""))
            lines.append(f"**In Stock:** {avail_num:,}")
        except (ValueError, TypeError):
            lines.append(f"**In Stock:** {avail}")
    if lead_time:
        lines.append(f"**Lead Time:** {lead_time}")
    if moq or mult:
        parts = []
        if moq:
            parts.append(f"**MOQ:** {moq}")
        if mult:
            parts.append(f"**Mult:** {mult}")
        lines.append(" | ".join(parts))

    price_breaks = part.get("PriceBreaks", [])
    if price_breaks:
        lines.append("")
        lines.append("| Qty | Unit Price |")
        lines.append("|-----|-----------|")
        for pb in price_breaks:
            qty = pb.get("Quantity", "")
            price = pb.get("Price", "")
            currency = pb.get("Currency", "")
            price_str = f"{currency} {price}" if currency else price
            lines.append(f"| {qty} | {price_str} |")

    # Specifications
    attrs = part.get("ProductAttributes", [])
    if attrs:
        lines.append("")
        lines.append("## Specifications")
        lines.append("")
        for attr in attrs:
            name = attr.get("AttributeName", "")
            value = attr.get("AttributeValue", "")
            if name and value:
                lines.append(f"- **{name}:** {value}")

    # Product URL
    product_url = part.get("ProductDetailUrl", "")
    if product_url:
        lines.append("")
        lines.append(product_url)

    return "\n".join(lines)


def _format_search_results(parts: list[dict], total: int) -> str:
    """Format multiple search results into markdown."""
    if not parts:
        return "No results found."

    sections: list[str] = []
    sections.append(f"**{total} results found** (showing {len(parts)})\n")

    for part in parts:
        sections.append(_format_part(part))

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def get_product(url: str, api_key: str | None = None) -> dict:
    """Fetch Mouser product/search data via API.

    Args:
        url: Mouser URL (product page or search page)
        api_key: Mouser API key. Falls back to MOUSER_API_KEY env var.

    Returns:
        {"content": markdown_string} on success, {"error": message} on failure.
    """
    api_key = api_key or os.environ.get("MOUSER_API_KEY", "")
    if not api_key:
        return {"error": "MOUSER_API_KEY not configured. Set the environment variable to enable Mouser API access."}

    try:
        # Try product URL first
        mpn = extract_mpn_from_url(url)
        if mpn:
            _log(f"Looking up MPN: {mpn}")
            data = await search_by_part_number([mpn], api_key)
            parts = _extract_parts(data)
            if parts:
                content = _format_part(parts[0])
                return {"content": content}
            # If exact match fails, try keyword search
            _log(f"No exact match for {mpn}, trying keyword search")
            data = await search_by_keyword(mpn, api_key)
            parts = _extract_parts(data)
            total = _extract_total(data)
            if parts:
                return {"content": _format_search_results(parts, total)}
            return {"error": f"No results found for part: {mpn}"}

        # Try search URL
        keyword = extract_search_keyword(url)
        if keyword:
            _log(f"Searching: {keyword}")
            data = await search_by_keyword(keyword, api_key)
            parts = _extract_parts(data)
            total = _extract_total(data)
            return {"content": _format_search_results(parts, total)}

        return {"error": f"Could not extract part number or search keyword from URL: {url}"}

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return {"error": "Invalid Mouser API key. Check MOUSER_API_KEY."}
        if e.response.status_code == 429:
            return {"error": "Mouser API rate limit exceeded (30 req/min). Try again in a moment."}
        return {"error": f"Mouser API error: HTTP {e.response.status_code}"}
    except httpx.TimeoutException:
        return {"error": "Mouser API request timed out."}
    except Exception as e:
        _log(f"Unexpected error: {e}")
        return {"error": f"Mouser API error: {e}"}


def _extract_parts(data: dict) -> list[dict]:
    """Extract parts list from API response."""
    sr = data.get("SearchResults") or {}
    return sr.get("Parts") or []


def _extract_total(data: dict) -> int:
    """Extract total result count from API response."""
    sr = data.get("SearchResults") or {}
    return sr.get("NumberOfResult", 0)
