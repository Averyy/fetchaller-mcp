"""Alibaba.com product detail via SSR HTML scraping.

Alibaba.com serves product data as server-rendered HTML with embedded JSON in
``window.detailData``. This does not use MTop, but wafer may still invoke its
browser solver when the HTML transport is challenged.
"""

from __future__ import annotations

import asyncio
import math
import re
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from ..content._numeric import bounded_number_text
from ..content._price import has_positive_price
from ..content.alibaba import extract_product_data
from ..ratelimit import alibaba_limiter
from ..security.xss import safe_log_text
from ..tools.fetch import fetch_url

_MAX_TITLE_CHARS = 500
_MAX_SCALAR_CHARS = 512
_MAX_PRICE_TIERS = 20
_MAX_LEAD_TIMES = 20
_MAX_PROPERTIES = 40
_MAX_VARIANTS = 20
_MAX_VARIANT_VALUES = 20
_MAX_PRODUCT_OUTPUT_CHARS = 100_000


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] alibaba product: {safe_log_text(msg)}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Product ID extraction
# ---------------------------------------------------------------------------


def extract_product_id(input_str: str) -> str | None:
    """Extract product ID from URL or bare numeric string.

    Accepts:
    - Full URL: https://www.alibaba.com/product-detail/slug_1234567890.html
    - Bare numeric ID: 1234567890 (5-20 digits)
    """
    if not isinstance(input_str, str):
        return None
    if re.fullmatch(r"\d{5,20}", input_str):
        return input_str

    try:
        parsed = urlparse(input_str)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not (host == "alibaba.com" or host.endswith(".alibaba.com"))
    ):
        return None
    match = re.fullmatch(
        r"/product-detail/[^/]*_(\d{5,20})\.html",
        parsed.path,
    )
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Data extraction from globalData
# ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict:
    """Return a mapping or an empty mapping for malformed embedded data."""

    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    """Return a list or an empty list for malformed embedded data."""

    return value if isinstance(value, list) else []


def _bounded_scalar(value: object, maximum: int = _MAX_SCALAR_CHARS) -> str:
    """Return compact scalar text, rejecting complex and oversized fields."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    try:
        text = " ".join(str(value).split())
    except (OverflowError, ValueError):
        return ""
    if not text or len(text) > maximum:
        return ""
    return text


def _bounded_integer(value: object, maximum_digits: int = 20) -> int | None:
    """Return an integral quantity with a bounded decimal representation."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        text = format(value, ".0f")
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if re.fullmatch(rf"[+-]?\d{{1,{maximum_digits}}}", text) is None:
        return None
    return int(text)


def _tier_maximum(value: object) -> tuple[bool, int | None]:
    """Parse a tier maximum, distinguishing open sentinels from bad data."""

    if value is None or value == "":
        return True, None
    if isinstance(value, bool):
        return False, None
    maximum = _bounded_integer(value)
    if maximum in (0, -1):
        return True, None
    if maximum is None:
        return False, None
    return True, maximum


def _normalized_price_tiers(value: object) -> list[dict]:
    """Return sorted, non-overlapping, positive price tiers."""

    if not isinstance(value, list):
        return []
    candidates: list[dict] = []
    for tier in value[:_MAX_PRICE_TIERS]:
        if not isinstance(tier, dict):
            continue
        minimum = _bounded_integer(tier.get("min"))
        valid_maximum, maximum = _tier_maximum(tier.get("max"))
        price = _bounded_scalar(tier.get("price"), 128)
        if (
            minimum is None
            or minimum <= 0
            or not valid_maximum
            or (maximum is not None and maximum < minimum)
            or not has_positive_price(price, require_currency=True)
        ):
            continue
        candidates.append(
            {
                "qty_str": (
                    f"{minimum}-{maximum}" if maximum is not None else f"{minimum}+"
                ),
                "min": minimum,
                "max": maximum,
                "price": price,
            }
        )

    normalized: list[dict] = []
    previous_max = 0
    for tier in sorted(candidates, key=lambda item: item["min"]):
        if previous_max is None or tier["min"] <= previous_max:
            continue
        normalized.append(tier)
        previous_max = tier["max"]
    return normalized


def _extract_details(global_data: dict) -> dict:
    """Extract structured product details from globalData object."""
    global_data = _as_dict(global_data)
    product = _as_dict(global_data.get("product"))
    seller = _as_dict(global_data.get("seller"))
    trade = _as_dict(global_data.get("trade"))
    review = _as_dict(global_data.get("review"))
    price_info = _as_dict(product.get("price"))
    trade_info = _as_dict(trade.get("tradeInfo"))
    raw_moq = bounded_number_text(
        product.get("moq"),
        minimum=0,
        maximum=1_000_000_000_000_000_000_000,
        minimum_exclusive=True,
        max_chars=64,
    )

    data = {
        # Basic info
        "title": _bounded_scalar(product.get("subject"), _MAX_TITLE_CHARS),
        "product_id": _bounded_scalar(product.get("productId"), 20),
        # Pricing
        "price_range": "",
        "price_tiers": [],
        "unit": _bounded_scalar(price_info.get("unitEven"), 64),
        "moq": raw_moq,
        # Supplier
        "company_name": _bounded_scalar(seller.get("companyName"), 500),
        "company_id": _bounded_scalar(seller.get("companyId"), 64),
        "company_url": _bounded_scalar(seller.get("companyProfileUrl"), 4096),
        # Trade
        "sales_volume": _bounded_scalar(trade.get("salesVolume"), 256),
        "trade_type": _bounded_scalar(trade_info.get("tradePriceType"), 128),
        "lead_times": [],
        # Specs
        "key_properties": [],
        "variants": [],
        # Reviews
        "avg_star": "",
        "review_count": 0,
    }

    # Price range — try formatLadderPrice first, then productRangePrices
    format_price = _bounded_scalar(price_info.get("formatLadderPrice"), 256)
    if format_price and has_positive_price(format_price, require_currency=True):
        data["price_range"] = format_price
    else:
        range_prices = _as_dict(price_info.get("productRangePrices"))
        range_text = _bounded_scalar(range_prices.get("priceRangeText"), 256)
        if range_text and has_positive_price(range_text, require_currency=True):
            data["price_range"] = range_text

    # Tiered pricing — keys vary: minQuantity/maxQuantity or min/max,
    # formatPrice (with currency symbol) or price (numeric)
    ladder_prices = _as_list(price_info.get("productLadderPrices"))
    for tier in ladder_prices[:_MAX_PRICE_TIERS]:
        if not isinstance(tier, dict):
            continue
        # Keys vary across pages: minQuantity/maxQuantity vs min/max
        min_qty = tier.get("minQuantity") if "minQuantity" in tier else tier.get("min")
        max_qty = tier.get("maxQuantity") if "maxQuantity" in tier else tier.get("max")
        min_qty = _bounded_integer(min_qty)
        if min_qty is None:
            continue
        valid_max_qty, max_qty = _tier_maximum(max_qty)
        if not valid_max_qty:
            continue
        fmt_price = _bounded_scalar(tier.get("formatPrice"), 128)
        raw_price = _bounded_scalar(tier.get("price"), 128)
        price_str = fmt_price if fmt_price else (f"${raw_price}" if raw_price else "")
        data["price_tiers"].append(
            {"min": min_qty, "max": max_qty, "price": price_str}
        )
    data["price_tiers"] = _normalized_price_tiers(data["price_tiers"])

    # Lead times — field is "day" or "processPeriod" depending on page
    lead_time_info = _as_dict(trade.get("leadTimeInfo"))
    ladder_periods = _as_list(lead_time_info.get("ladderPeriodList"))
    for period in ladder_periods[:_MAX_LEAD_TIMES]:
        if not isinstance(period, dict):
            continue
        min_qty = _bounded_integer(period.get("minQuantity"))
        valid_max_qty, max_qty = _tier_maximum(period.get("maxQuantity"))
        days = _bounded_integer(
            period.get("day") or period.get("processPeriod"),
        )
        if (
            min_qty is not None
            and min_qty > 0
            and valid_max_qty
            and (max_qty is None or max_qty >= min_qty)
            and days is not None
            and days > 0
        ):
            qty_str = (
                f"{min_qty}-{max_qty}" if max_qty is not None else f"{min_qty}+"
            )
            data["lead_times"].append(f"{qty_str} units: {days} days")

    # Key properties (specs) — fields are attrName/attrValue on real pages
    # Merge key + basic + other properties, dedup by name+value pair
    seen_props = set()
    for source_name in (
        "productKeyIndustryProperties",
        "productBasicProperties",
        "productOtherProperties",
    ):
        for prop in _as_list(product.get(source_name)):
            if len(data["key_properties"]) >= _MAX_PROPERTIES:
                break
            if not isinstance(prop, dict):
                continue
            name = _bounded_scalar(
                prop.get("attrName") or prop.get("name"),
                150,
            )
            value = _bounded_scalar(
                prop.get("attrValue") or prop.get("value"),
                500,
            )
            if name and value:
                key = (name, value)
                if key in seen_props:
                    continue
                seen_props.add(key)
                data["key_properties"].append(f"{name}: {value}")

    # Variants (SKU attributes)
    sku = _as_dict(product.get("sku"))
    sku_attrs = _as_list(sku.get("skuAttrs"))
    for attr in sku_attrs[:_MAX_VARIANTS]:
        if not isinstance(attr, dict):
            continue
        attr_name = _bounded_scalar(attr.get("name"), 100)
        values = []
        for value in _as_list(attr.get("values"))[:_MAX_VARIANT_VALUES]:
            if not isinstance(value, dict):
                continue
            value_name = _bounded_scalar(value.get("name"), 100)
            if value_name:
                values.append(value_name)
        if attr_name and values:
            variant = f"{attr_name}: {', '.join(values)}"
            if len(variant) <= 2_500:
                data["variants"].append(variant)

    # Reviews
    product_review = _as_dict(review.get("productReview"))
    data["avg_star"] = bounded_number_text(
        product_review.get("averageStar"),
        minimum=0,
        maximum=5,
    )
    review_count = bounded_number_text(
        product_review.get("totalReviewCount"),
        minimum=0,
        maximum=1_000_000_000,
        integral=True,
        allow_grouping=True,
    )
    data["review_count"] = (
        int(review_count.replace(",", "")) if review_count else 0
    )

    return data


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _canonical_company_url(value: object) -> str:
    """Return a strict HTTPS Alibaba supplier URL or an empty string."""

    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    candidate = f"https:{value}" if value.startswith("//") else value
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not (host == "alibaba.com" or host.endswith(".alibaba.com"))
    ):
        return ""
    return parsed.geturl()


def _bounded_product_output(lines: list[str]) -> str:
    """Join whole lines under the MCP output cap, naming any omission."""

    output_lines: list[str] = []
    length = 0
    for line in lines:
        separator = 1 if output_lines else 0
        if length + separator + len(line) > _MAX_PRODUCT_OUTPUT_CHARS:
            marker = "[Additional product fields omitted to enforce the output limit.]"
            while output_lines:
                removed = output_lines.pop()
                length -= len(removed) + (1 if output_lines else 0)
                marker_separator = 1 if output_lines else 0
                if (
                    length + marker_separator + len(marker)
                    <= _MAX_PRODUCT_OUTPUT_CHARS
                ):
                    break
            output_lines.append(marker)
            break
        output_lines.append(line)
        length += separator + len(line)
    return "\n".join(output_lines).strip()


def _format_output(product_id: str, data: dict) -> str:
    """Format product data into readable text."""
    lines = []

    # Title
    title = data.get("title", "")
    if title:
        lines.append(f"# {title}")
        lines.append("")

    # Price
    price_range = data.get("price_range", "")
    unit_plural = data.get("unit", "piece")
    unit_singular = unit_plural[:-1] if unit_plural.endswith("s") else unit_plural
    if price_range:
        unit_str = f" per {unit_plural}" if unit_plural else ""
        lines.append(f"**Price:** {price_range}{unit_str}")

    # MOQ
    moq = data.get("moq", "")
    if moq:
        lines.append(f"**MOQ:** {moq} {unit_plural}")

    # MOQ Price — find the tier that contains the MOQ quantity
    tiers = _normalized_price_tiers(data.get("price_tiers", []))
    if tiers and moq:
        moq_int = _bounded_integer(moq)
        if moq_int is not None and moq_int > 0:
            matching = [
                tier
                for tier in tiers
                if tier["min"] <= moq_int
                and (tier["max"] is None or moq_int <= tier["max"])
            ]
            if len(matching) == 1:
                lines.append(
                    f"**MOQ Price:** {matching[0]['price']}/{unit_singular}"
                )

    # Tiered pricing
    if tiers:
        lines.append("**Tiered pricing:**")
        for tier in tiers:
            lines.append(f"  - {tier['qty_str']}: {tier['price']}")

    # Trade type
    trade_type = data.get("trade_type", "")
    if trade_type:
        lines.append(f"**Price type:** {trade_type}")

    lines.append("")

    # Supplier
    company = data.get("company_name", "")
    if company:
        lines.append(f"**Supplier:** {company}")
    company_url = data.get("company_url", "")
    if company_url:
        company_url = _canonical_company_url(company_url)
        if company_url:
            lines.append(f"**Supplier profile:** {company_url}")

    # Sales
    sales = data.get("sales_volume", "")
    if sales:
        lines.append(f"**Sales:** {sales}")

    # Rating
    avg_star = data.get("avg_star", "")
    review_count = data.get("review_count", 0)
    if avg_star:
        lines.append(f"**Rating:** ★{avg_star} ({review_count} reviews)")
    elif review_count:
        lines.append(f"**Reviews:** {review_count}")

    lines.append("")

    # Lead times
    lead_times = data.get("lead_times", [])
    if lead_times:
        lines.append("**Lead times:**")
        for lt in lead_times:
            lines.append(f"  - {lt}")
        lines.append("")

    # Variants
    variants = data.get("variants", [])
    if variants:
        lines.append("**Variants:**")
        for v in variants:
            lines.append(f"  - {v}")
        lines.append("")

    # Specs
    props = data.get("key_properties", [])
    if props:
        lines.append("**Specifications:**")
        for p in props:
            lines.append(f"  - {p}")
        lines.append("")

    # URL
    pid = product_id or data.get("product_id", "")
    if pid:
        lines.append(f"https://www.alibaba.com/product-detail/_{pid}.html")

    return _bounded_product_output(lines)


def _has_usable_product_data(
    data: dict,
    *,
    expected_product_id: str | None = None,
) -> bool:
    """Whether extracted details are a product record rather than a shell.

    Alibaba challenge/placeholder pages can still expose a partial
    ``window.detailData`` object.  A title and canonical URL alone are not a
    useful product result and must not be reported as one.  The product tool's
    contract requires a titled offer with an actual price, supplier, and a
    substantive product property.
    """
    title = data.get("title")
    supplier = data.get("company_name")
    embedded_id = str(data.get("product_id", ""))
    if not re.fullmatch(r"\d{5,20}", embedded_id):
        return False
    if expected_product_id is not None and embedded_id != expected_product_id:
        return False
    if not isinstance(title, str) or not title.strip():
        return False
    # Challenge shells sometimes echo only the numeric offer ID as their
    # "title".  That is not product detail, even if other placeholder fields
    # happen to contain digits.
    if not any(character.isalpha() for character in title):
        return False
    if (
        not isinstance(supplier, str)
        or not any(character.isalpha() for character in supplier)
    ):
        return False

    prices = [data.get("price_range", "")]
    prices.extend(
        tier.get("price", "")
        for tier in data.get("price_tiers", [])
        if isinstance(tier, dict)
    )
    positive_price = any(
        has_positive_price(price, require_currency=True)
        for price in prices
    )
    if not positive_price:
        return False

    properties = data.get("key_properties", [])
    return any(
        isinstance(property_, str)
        and ":" in property_
        and any(character.isalpha() for character in property_)
        for property_ in properties
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _parse_product_html(html: str, product_id: str) -> dict:
    """Extract, validate, and format one Alibaba product response."""

    global_data = extract_product_data(html)
    if not global_data:
        _log(f"no detailData found in HTML for product {product_id}")
        return {
            "error": (
                "Could not extract product data. "
                "The page may be blocked or unavailable."
            )
        }

    data = _extract_details(global_data)
    if not _has_usable_product_data(data, expected_product_id=product_id):
        _log(f"incomplete product data for product {product_id}")
        return {
            "error": (
                "Could not extract complete product data. "
                "The product may be unavailable or the page may be blocked."
            )
        }
    content = _format_output(product_id, data)
    if not content:
        return {"error": "Product data was empty after extraction."}
    return {"content": content}


async def get_product(
    product_id: str,
    timeout: int = 180,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Get Alibaba.com product details.

    Fetches the product page HTML and extracts embedded JSON data.

    Args:
        product_id: Numeric product ID or full Alibaba.com URL.
        timeout: End-to-end request and browser-challenge timeout in seconds.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: Optional BrowserSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted text) or "error".
    """
    pid = extract_product_id(product_id)
    if not pid:
        return {"error": f"Invalid Alibaba.com product ID or URL: {product_id}"}
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        return {"error": "timeout must be greater than zero and at most 300 seconds."}

    deadline = time.monotonic() + timeout
    try:
        async with asyncio.timeout(timeout):
            # Domain-level rate limiting (shared with alibaba search).
            # Lock contention, spacing, and server deferral consume the same
            # advertised end-to-end budget as the protected fetch.
            await alibaba_limiter.wait()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            url = f"https://www.alibaba.com/product-detail/_{pid}.html"
            _log(f"fetching product {pid}")

            result = await fetch_url(
                url=url,
                max_tokens=500000,  # Full page for JSON extraction
                timeout=remaining,
                raw=True,
                cache=cache,
                config=config,
                browser_solver=browser_solver,
                _skip_alibaba_intercept=True,
            )
            if "error" in result:
                return result

            html = result.get("content", "")
            return await asyncio.to_thread(_parse_product_html, html, pid)
    except TimeoutError:
        return {
            "error": (
                f"Request timed out after {timeout}s. "
                "Try increasing the timeout parameter for slow servers."
            )
        }
