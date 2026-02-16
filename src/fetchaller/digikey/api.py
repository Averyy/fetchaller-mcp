"""DigiKey API client with OAuth2 token management.

Uses the official DigiKey API (free, OAuth2 client_credentials) for structured
product data. Falls through to HTML pipeline (botfighter) when no credentials
are configured.

API docs: https://developer.digikey.com/products/product-information/partsearch
Rate limit: 120 requests/minute burst, 1000/day.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from ..ratelimit import digikey_limiter

_API_BASE = "https://api.digikey.com"
_TOKEN_URL = f"{_API_BASE}/v1/oauth2/token"

# Token refresh safety margin (seconds before actual expiry)
_TOKEN_REFRESH_MARGIN = 60


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] digikey api: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------


class TokenManager:
    """Manages OAuth2 client_credentials tokens for DigiKey API."""

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str = ""
        self._token_expires: float = 0.0

    @property
    def is_expired(self) -> bool:
        return not self._token or time.time() >= self._token_expires

    async def ensure_token(self) -> str:
        """Get a valid token, refreshing if needed."""
        if not self.is_expired:
            return self._token

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        self._token = data["access_token"]
        expires_in = data.get("expires_in", 600)  # Default 10 min
        self._token_expires = time.time() + expires_in - _TOKEN_REFRESH_MARGIN
        _log(f"Token refreshed, expires in {expires_in}s")
        return self._token


# Module-level token manager (lazily initialized per credentials)
_token_managers: dict[str, TokenManager] = {}


def _get_token_manager(client_id: str, client_secret: str) -> TokenManager:
    """Get or create a token manager for the given credentials."""
    key = client_id
    if key not in _token_managers:
        _token_managers[key] = TokenManager(client_id, client_secret)
    return _token_managers[key]


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# /en/products/detail/texas-instruments/LM358P/277042
_PRODUCT_PATH_RE = re.compile(
    r"/(?:[a-z]{2}/)?products/detail/([^/]+)/([^/]+)/(\d+)"
)

# /en/products/result?keywords=LM358
_SEARCH_PATH_RE = re.compile(r"/(?:[a-z]{2}/)?products/result", re.I)


def extract_part_from_url(url: str) -> tuple[str, str, str] | None:
    """Extract (manufacturer, mpn, dk_part_number) from a DigiKey product URL."""
    m = _PRODUCT_PATH_RE.search(urlparse(url).path)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def extract_search_keyword(url: str) -> str | None:
    """Extract keyword from a DigiKey search URL."""
    parsed = urlparse(url)
    if _SEARCH_PATH_RE.search(parsed.path):
        qs = parse_qs(parsed.query)
        kw = qs.get("keywords") or qs.get("Keywords")
        if kw:
            return kw[0]
    return None


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------


def _api_headers(token: str, client_id: str) -> dict[str, str]:
    """Build standard DigiKey API headers."""
    return {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": client_id,
        "X-DIGIKEY-Locale-Site": "CA",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "CAD",
    }


async def search_keyword(
    keyword: str,
    token_manager: TokenManager,
    client_id: str,
    limit: int = 10,
) -> dict:
    """Search DigiKey by keyword. Returns raw API response dict."""
    await digikey_limiter.wait()
    token = await token_manager.ensure_token()
    url = f"{_API_BASE}/products/v4/search/keyword"
    payload = {
        "Keywords": keyword,
        "Limit": limit,
        "Offset": 0,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            headers=_api_headers(token, client_id),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def get_product_details(
    mpn: str,
    token_manager: TokenManager,
    client_id: str,
) -> dict:
    """Get product details by MPN. Returns raw API response dict."""
    await digikey_limiter.wait()
    token = await token_manager.ensure_token()
    url = f"{_API_BASE}/products/v4/search/{mpn}/productdetails"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            url,
            headers=_api_headers(token, client_id),
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _format_product(product: dict) -> str:
    """Format a single DigiKey product into markdown.

    Handles the v4 API response structure where:
    - MPN is in ManufacturerProductNumber (not ManufacturerPartNumber)
    - DK part #, pricing, MOQ are inside ProductVariations[0]
    - QuantityAvailable and ManufacturerLeadWeeks are at top level
    """
    lines: list[str] = []

    desc = product.get("Description", {})
    detail_desc = desc.get("DetailedDescription", "") if isinstance(desc, dict) else str(desc)
    short_desc = desc.get("ProductDescription", "") if isinstance(desc, dict) else ""
    # v4 uses ManufacturerProductNumber
    mpn = product.get("ManufacturerProductNumber", "") or product.get("ManufacturerPartNumber", "")
    mfr = product.get("Manufacturer", {})
    mfr_name = mfr.get("Name", "") if isinstance(mfr, dict) else str(mfr)

    heading_desc = short_desc or detail_desc
    lines.append(f"# {mpn}" + (f" \u2014 {heading_desc}" if heading_desc else ""))
    lines.append("")

    if mfr_name:
        lines.append(f"**Manufacturer:** {mfr_name}")
    if mpn:
        lines.append(f"**Manufacturer Part #:** {mpn}")

    # DK part # is inside ProductVariations[0] in v4, or at top level in older format
    variations = product.get("ProductVariations", [])
    primary_variation = variations[0] if variations else {}
    dk_pn = primary_variation.get("DigiKeyProductNumber", "") or product.get("DigiKeyPartNumber", "")
    if dk_pn:
        lines.append(f"**DigiKey Part #:** {dk_pn}")

    category = product.get("Category", {})
    cat_name = category.get("Name", "") if isinstance(category, dict) else str(category)
    if cat_name:
        lines.append(f"**Category:** {cat_name}")
    if detail_desc:
        lines.append(f"**Description:** {detail_desc}")

    status = product.get("ProductStatus", {})
    status_name = status.get("Status", "") if isinstance(status, dict) else str(status)
    if status_name:
        lines.append(f"**Status:** {status_name}")

    datasheet = product.get("DatasheetUrl", "") or product.get("PrimaryDatasheet", "")
    if datasheet:
        lines.append(f"**Datasheet:** {datasheet}")

    # Stock & Pricing
    qty_avail = product.get("QuantityAvailable", "")
    lead_time = product.get("ManufacturerLeadWeeks", "")
    # MOQ/Mult from primary variation or top level
    moq = primary_variation.get("MinimumOrderQuantity", "") or product.get("MinimumOrderQuantity", "")
    mult = primary_variation.get("StandardPackage", "") or product.get("StandardPackage", "")

    lines.append("")
    lines.append("## Stock & Pricing")
    lines.append("")
    if qty_avail is not None and qty_avail != "":
        lines.append(f"**In Stock:** {qty_avail:,}" if isinstance(qty_avail, int) else f"**In Stock:** {qty_avail}")
    if lead_time:
        lines.append(f"**Lead Time:** {lead_time} weeks")
    moq_parts = []
    if moq:
        moq_parts.append(f"**MOQ:** {moq}")
    if mult:
        moq_parts.append(f"**Mult:** {mult}")
    if moq_parts:
        lines.append(" | ".join(moq_parts))

    # Pricing from primary variation or top level
    price_breaks = primary_variation.get("StandardPricing", []) or product.get("StandardPricing", [])
    if price_breaks:
        lines.append("")
        lines.append("| Qty | Unit Price (CAD) |")
        lines.append("|-----|-----------------|")
        for pb in price_breaks:
            qty = pb.get("BreakQuantity", "")
            price = pb.get("UnitPrice", "")
            lines.append(f"| {qty} | ${price} |")

    # Package type from variation
    pkg_type = primary_variation.get("PackageType", {})
    pkg_name = pkg_type.get("Name", "") if isinstance(pkg_type, dict) else ""

    # Specifications (Parameters)
    params = product.get("Parameters", [])
    if params or pkg_name:
        lines.append("")
        lines.append("## Specifications")
        lines.append("")
        if pkg_name:
            lines.append(f"- **Package Type:** {pkg_name}")
        for param in params:
            name = param.get("ParameterText", "")
            value = param.get("ValueText", "")
            if name and value:
                lines.append(f"- **{name}:** {value}")

    # Product URL
    product_url = product.get("ProductUrl", "")
    if product_url:
        lines.append("")
        if product_url.startswith("/"):
            product_url = f"https://www.digikey.ca{product_url}"
        lines.append(product_url)

    return "\n".join(lines)


def _format_search_results(products: list[dict], total: int) -> str:
    """Format multiple search results into markdown."""
    if not products:
        return "No results found."

    sections: list[str] = []
    sections.append(f"**{total} results found** (showing {len(products)})\n")

    for product in products:
        sections.append(_format_product(product))

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def get_product(
    url: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict:
    """Fetch DigiKey product/search data via API.

    Args:
        url: DigiKey URL (product page or search page)
        client_id: DigiKey client ID. Falls back to DIGIKEY_CLIENT_ID env var.
        client_secret: DigiKey client secret. Falls back to DIGIKEY_CLIENT_SECRET env var.

    Returns:
        {"content": markdown_string} on success, {"error": message} on failure.
    """
    client_id = client_id or os.environ.get("DIGIKEY_CLIENT_ID", "")
    client_secret = client_secret or os.environ.get("DIGIKEY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return {"error": "DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET not configured."}

    token_manager = _get_token_manager(client_id, client_secret)

    try:
        # Try product URL first
        part_info = extract_part_from_url(url)
        if part_info:
            _mfr, mpn, _dk_id = part_info
            _log(f"Looking up product: {mpn}")
            # Product details endpoint takes MPN (not the numeric URL ID)
            try:
                data = await get_product_details(mpn, token_manager, client_id)
                product = data.get("Product", data)
                if product.get("ManufacturerProductNumber") or product.get("ManufacturerPartNumber"):
                    return {"content": _format_product(product)}
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                _log(f"Product details 404 for {mpn}, trying keyword search")

            # Fall back to keyword search by MPN
            data = await search_keyword(mpn, token_manager, client_id)
            products = data.get("Products", [])
            total = data.get("ProductsCount", len(products))
            if products:
                return {"content": _format_search_results(products, total)}
            return {"error": f"No results found for part: {mpn}"}

        # Try search URL
        keyword = extract_search_keyword(url)
        if keyword:
            _log(f"Searching: {keyword}")
            data = await search_keyword(keyword, token_manager, client_id)
            products = data.get("Products", [])
            total = data.get("ProductsCount", len(products))
            return {"content": _format_search_results(products, total)}

        return {"error": f"Could not extract part number or search keyword from URL: {url}"}

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            # Token may be stale — clear it so next call re-authenticates
            token_manager._token = ""
            token_manager._token_expires = 0.0
            return {"error": "DigiKey API authentication failed. Check DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET."}
        if e.response.status_code == 429:
            return {"error": "DigiKey API rate limit exceeded. Try again in a moment."}
        return {"error": f"DigiKey API error: HTTP {e.response.status_code}"}
    except httpx.TimeoutException:
        return {"error": "DigiKey API request timed out."}
    except Exception as e:
        _log(f"Unexpected error: {e}")
        return {"error": f"DigiKey API error: {e}"}
