"""Alibaba.com product detail via SSR HTML scraping.

Alibaba.com serves all product data as server-rendered HTML with embedded JSON
in ``window.detailData``. No MTop API or browser needed — curl_cffi with
Chrome impersonation works directly.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime

from ..content.alibaba import extract_product_data, extract_product_id_from_url
from ..ratelimit import alibaba_limiter
from ..tools.fetch import fetch_url


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] alibaba product: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Product ID extraction
# ---------------------------------------------------------------------------


def extract_product_id(input_str: str) -> str | None:
    """Extract product ID from URL or bare numeric string.

    Accepts:
    - Full URL: https://www.alibaba.com/product-detail/slug_1234567890.html
    - Bare numeric ID: 1234567890 (5-20 digits)
    """
    # Try as URL first
    url_id = extract_product_id_from_url(input_str)
    if url_id:
        return url_id

    # Try as bare numeric ID
    if re.match(r"^\d{5,20}$", input_str):
        return input_str

    return None


# ---------------------------------------------------------------------------
# Data extraction from globalData
# ---------------------------------------------------------------------------


def _extract_details(global_data: dict) -> dict:
    """Extract structured product details from globalData object."""
    product = global_data.get("product", {})
    seller = global_data.get("seller", {})
    trade = global_data.get("trade", {})
    review = global_data.get("review", {})

    data = {
        # Basic info
        "title": product.get("subject", ""),
        "product_id": product.get("productId", ""),

        # Pricing
        "price_range": "",
        "price_tiers": [],
        "unit": product.get("price", {}).get("unitEven", ""),
        "moq": product.get("moq", ""),

        # Supplier
        "company_name": seller.get("companyName", ""),
        "company_id": seller.get("companyId", ""),
        "company_url": seller.get("companyProfileUrl", ""),

        # Trade
        "sales_volume": trade.get("salesVolume", ""),
        "trade_type": trade.get("tradeInfo", {}).get("tradePriceType", ""),
        "lead_times": [],

        # Specs
        "key_properties": [],
        "variants": [],

        # Reviews
        "avg_star": "",
        "review_count": 0,
    }

    # Price range — try formatLadderPrice first, then productRangePrices
    price_info = product.get("price", {})
    format_price = price_info.get("formatLadderPrice", "")
    if format_price:
        data["price_range"] = format_price
    else:
        range_prices = price_info.get("productRangePrices", {})
        range_text = range_prices.get("priceRangeText", "")
        if range_text:
            data["price_range"] = range_text

    # Tiered pricing — keys vary: minQuantity/maxQuantity or min/max,
    # formatPrice (with currency symbol) or price (numeric)
    ladder_prices = price_info.get("productLadderPrices", [])
    for tier in ladder_prices:
        # Keys vary across pages: minQuantity/maxQuantity vs min/max
        min_qty = tier.get("minQuantity") if "minQuantity" in tier else tier.get("min")
        max_qty = tier.get("maxQuantity") if "maxQuantity" in tier else tier.get("max")
        # Coerce to int — API may return strings
        try:
            min_qty = int(min_qty) if min_qty is not None else None
        except (ValueError, TypeError):
            continue
        try:
            max_qty = int(max_qty) if max_qty is not None else None
        except (ValueError, TypeError):
            max_qty = None
        # 0 or negative means open-ended (no max) — API uses both 0 and -1
        if max_qty is not None and max_qty <= 0:
            max_qty = None
        fmt_price = tier.get("formatPrice", "")
        raw_price = tier.get("price", "")
        price_str = fmt_price if fmt_price else (f"${raw_price}" if raw_price else "")
        if min_qty is not None and price_str:
            qty_str = f"{min_qty}-{max_qty}" if max_qty else f"{min_qty}+"
            data["price_tiers"].append({"qty_str": qty_str, "min": min_qty, "max": max_qty, "price": price_str})

    # Lead times — field is "day" or "processPeriod" depending on page
    lead_time_info = trade.get("leadTimeInfo", {})
    ladder_periods = lead_time_info.get("ladderPeriodList", [])
    for period in ladder_periods:
        min_qty = period.get("minQuantity", "")
        max_qty = period.get("maxQuantity", "")
        days = period.get("day") or period.get("processPeriod", "")
        if min_qty and days:
            qty_str = f"{min_qty}-{max_qty}" if max_qty else f"{min_qty}+"
            data["lead_times"].append(f"{qty_str} units: {days} days")

    # Key properties (specs) — fields are attrName/attrValue on real pages
    # Merge key + basic + other properties, dedup by name+value pair
    seen_props = set()
    all_props = (
        product.get("productKeyIndustryProperties", [])
        + product.get("productBasicProperties", [])
        + product.get("productOtherProperties", [])
    )
    for prop in all_props:
        name = prop.get("attrName") or prop.get("name", "")
        value = prop.get("attrValue") or prop.get("value", "")
        if name and value:
            key = (name, value)
            if key in seen_props:
                continue
            seen_props.add(key)
            data["key_properties"].append(f"{name}: {value}")

    # Variants (SKU attributes)
    sku = product.get("sku", {})
    sku_attrs = sku.get("skuAttrs", [])
    for attr in sku_attrs:
        attr_name = attr.get("name", "")
        values = [v.get("name", "") for v in attr.get("values", []) if v.get("name")]
        if attr_name and values:
            data["variants"].append(f"{attr_name}: {', '.join(values)}")

    # Reviews
    product_review = review.get("productReview", {})
    avg = product_review.get("averageStar")
    if avg is not None:
        data["avg_star"] = str(avg)
    data["review_count"] = product_review.get("totalReviewCount", 0)

    return data


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


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
    tiers = data.get("price_tiers", [])
    if tiers and moq:
        try:
            moq_int = int(moq)
            for tier in tiers:
                tier_min = tier["min"]
                tier_max = tier["max"]
                if tier_min <= moq_int and (tier_max is None or moq_int <= tier_max):
                    lines.append(f"**MOQ Price:** {tier['price']}/{unit_singular}")
                    break
            else:
                # MOQ might be at or above the highest tier
                if moq_int >= tiers[-1]["min"]:
                    lines.append(f"**MOQ Price:** {tiers[-1]['price']}/{unit_singular}")
        except (ValueError, KeyError):
            pass

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
        if not company_url.startswith("http"):
            company_url = f"https:{company_url}"
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

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def get_product(
    product_id: str,
    fetcher=None,
    cache=None,
    config=None,
    cookie_cache=None,
    challenge_solver=None,
) -> dict:
    """Get Alibaba.com product details.

    Fetches the product page HTML and extracts embedded JSON data.

    Args:
        product_id: Numeric product ID or full Alibaba.com URL.
        fetcher: ContentFetcher for HTTP requests.
        cache: ResponseCache instance.
        config: Config instance.
        cookie_cache: CookieCache for bot challenge cookies.
        challenge_solver: ChallengeSolver for browser-based challenges.

    Returns:
        Dict with "content" (formatted text) or "error".
    """
    pid = extract_product_id(product_id)
    if not pid:
        return {"error": f"Invalid Alibaba.com product ID or URL: {product_id}"}

    # Domain-level rate limiting (shared with alibaba search).
    # No extra_delay → 4.0s base interval between product fetches.
    await alibaba_limiter.wait()

    url = f"https://www.alibaba.com/product-detail/_{pid}.html"
    _log(f"fetching product {pid}")

    result = await fetch_url(
        url=url,
        max_tokens=500000,  # Full page for JSON extraction
        timeout=30,
        raw=True,
        fetcher=fetcher,
        cache=cache,
        config=config,
        cookie_cache=cookie_cache,
        challenge_solver=challenge_solver,
        _skip_alibaba_intercept=True,
    )

    if "error" in result:
        return result

    html = result.get("content", "")
    global_data = extract_product_data(html)

    if not global_data:
        _log(f"no detailData found in HTML for product {pid}")
        return {"error": "Could not extract product data. The page may be blocked or unavailable."}

    data = _extract_details(global_data)
    content = _format_output(pid, data)

    if not content:
        return {"error": "Product data was empty after extraction."}

    return {"content": content}
