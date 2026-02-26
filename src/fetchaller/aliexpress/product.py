"""AliExpress product detail — MTop API with Chrome-based fallback."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import UTC, datetime

from ..ratelimit import aliexpress_limiter
from .mtop import MTopClient
from .reviews import fetch_reviews

_PRODUCT_ID_RE = re.compile(r"(?:aliexpress\.com/item/|(?<!\d))(\d{8,20})(?!\d)(?:\.html)?")

# MTop API methods to try, in order of preference
_MTOP_APIS = [
    ("mtop.aliexpress.pdp.pc.query", "1.0"),
    ("mtop.aliexpress.itemdetail.pc.asyncPCDetail", "1.0"),
    ("mtop.aliexpress.itemdetail.msite", "1.0"),
]


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] aliexpress product: {msg}", file=sys.stderr)


def extract_product_id(input_str: str) -> str | None:
    """Extract numeric product ID from a URL or bare ID string."""
    m = _PRODUCT_ID_RE.search(input_str)
    return m.group(1) if m else None


def _format_rating_breakdown(stats: dict) -> str:
    """Format rating breakdown from productEvaluationStatistic."""
    parts = []
    star_names = {5: "five", 4: "four", 3: "three", 2: "two", 1: "one"}
    for stars, name in star_names.items():
        rate = stats.get(f"{name}StarRate", 0)
        parts.append(f"★{stars}: {rate}%")
    return " | ".join(parts)


def _format_reviews(review_list: list[dict]) -> str:
    """Format review list into readable text."""
    lines = []
    for review in review_list[:5]:  # Show up to 5 recent reviews
        rating = review.get("buyerEval", 0) // 20
        stars = f"★{rating}" if rating else ""
        country = review.get("buyerCountry", "")
        date = review.get("evalDate", "")
        sku = review.get("skuInfo", "")

        # Prefer translated feedback, fall back to original
        text = review.get("buyerTranslationFeedback") or review.get("buyerFeedback", "")
        text = text.strip()
        if len(text) > 200:
            text = text[:197] + "..."

        images = review.get("images", [])
        photo_note = f" [{len(images)} photo{'s' if len(images) != 1 else ''}]" if images else ""

        header_parts = [s for s in [stars, country, date, sku] if s]
        header = " | ".join(header_parts)

        if text:
            lines.append(f"  {header}\n  {text}{photo_note}")
        else:
            lines.append(f"  {header}{photo_note}")
    return "\n\n".join(lines)


def _extract_product_data(result: dict) -> dict:
    """Extract structured product info from MTop response.

    Handles multiple response formats (pdp.pc.query, asyncPCDetail, msite).
    """
    data = result.get("data", {})
    # pdp.pc.query wraps in data.result
    if "result" in data:
        data = data["result"]

    info: dict = {}

    # Title
    title_mod = data.get("PRODUCT_TITLE") or data.get("titleModule") or {}
    info["title"] = title_mod.get("text") or title_mod.get("subject", "")

    # Price
    price_mod = data.get("PRICE") or data.get("priceModule") or {}
    target = price_mod.get("targetSkuPriceInfo") or price_mod.get("formattedActivityPrice") or {}
    sale = (
        target.get("salePriceString")
        or target.get("salePriceLocal")
        or target.get("salePrice")
        or price_mod.get("formattedPrice", "")
    )
    if isinstance(sale, dict):
        sale = sale.get("formatedAmount") or sale.get("formattedAmount", "")
    info["sale_price"] = sale
    orig = (
        target.get("originalPriceString")
        or target.get("originalPrice")
        or price_mod.get("formattedOriginalPrice", "")
    )
    # originalPrice can be a dict like {"currency": "USD", "formatedAmount": "$6.67", "value": 6.67}
    if isinstance(orig, dict):
        orig = orig.get("formatedAmount") or orig.get("formattedAmount", "")
    info["original_price"] = orig
    info["discount"] = target.get("discount") or price_mod.get("discount", "")

    # SKU pricing map
    sku_map = price_mod.get("skuPriceInfoMap", {})
    if sku_map:
        sku_prices = []
        for sku_id, sku_info in list(sku_map.items())[:10]:
            sp = sku_info.get("salePriceString") or sku_info.get("salePrice", "")
            if sp:
                sku_prices.append(f"  SKU {sku_id}: {sp}")
        info["sku_prices"] = "\n".join(sku_prices)

    # Rating
    rating_mod = data.get("PC_RATING") or data.get("titleModule") or {}
    info["rating"] = rating_mod.get("rating") or rating_mod.get("averageStar", "")
    info["review_count"] = rating_mod.get("totalValidNum") or rating_mod.get("feedbackCount", "")

    # Store
    store_mod = data.get("SHOP_CARD_PC") or data.get("storeModule") or {}
    info["store_name"] = store_mod.get("storeName", "")
    info["positive_rate"] = (
        store_mod.get("sellerPositiveRate")
        or store_mod.get("positiveRate", "")
    )

    # Quantity
    qty_mod = data.get("QUANTITY_PC") or data.get("quantityModule") or {}
    info["stock"] = qty_mod.get("totalAvailableInventory", "")

    # Trade/orders
    trade_mod = data.get("TRADE") or data.get("tradeModule") or {}
    info["orders"] = trade_mod.get("tradeCount") or trade_mod.get("formatTradeCount", "")

    # Shipping
    ship_mod = data.get("SHIPPING") or data.get("shippingModule") or {}
    ship_list = ship_mod.get("originalLayoutResultList") or ship_mod.get("generalFreightInfo", {}).get("originalLayoutResultList", [])
    if ship_list and isinstance(ship_list, list):
        first = ship_list[0] if ship_list else {}
        biz = first.get("bizData", {})
        info["shipping"] = biz.get("displayAmount", "") or biz.get("shippingFee", "")
        info["delivery_days"] = biz.get("deliveryDayMax", "")

    # Variants (SKU properties)
    sku_mod = data.get("SKU") or data.get("skuModule") or {}
    props = sku_mod.get("skuProperties") or sku_mod.get("productSKUPropertyList", [])
    if props:
        variants = []
        for prop in props:
            name = prop.get("skuPropertyName", "")
            values = prop.get("skuPropertyValues", [])
            val_names = [v.get("propertyValueDisplayName") or v.get("propertyValueName", "") for v in values]
            if name and val_names:
                variants.append(f"  {name}: {', '.join(val_names[:15])}")
        info["variants"] = "\n".join(variants)

    # Specifications
    spec_mod = data.get("PRODUCT_PROP_PC") or data.get("productPropModule") or {}
    shown = spec_mod.get("showedProps") or spec_mod.get("outerProps") or spec_mod.get("props", [])
    if shown:
        specs = []
        for s in shown[:15]:
            name = s.get("name") or s.get("attrName", "")
            value = s.get("value") or s.get("attrValue", "")
            if name and value:
                specs.append(f"  {name}: {value}")
        info["specs"] = "\n".join(specs)

    return info


def _extract_from_chrome_html(html: str) -> dict:
    """Extract product data from Chrome-rendered HTML.

    Falls back to JSON-LD structured data and DOM scraping when MTop API fails.
    AliExpress product pages are SPA-rendered, so this only works with
    Chrome-rendered HTML (not raw HTTP responses).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    info: dict = {}

    # 1. JSON-LD structured data (most reliable source for core fields)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
            if isinstance(ld, list):
                ld = ld[0] if ld else {}
            if ld.get("@type") == "Product":
                info["title"] = (ld.get("name") or "").replace(" - AliExpress", "").strip()
                offers = ld.get("offers", {})
                currency = offers.get("priceCurrency", "")
                price = offers.get("price", "")
                if price and currency:
                    info["sale_price"] = f"{currency} {price}"
                elif price:
                    info["sale_price"] = str(price)
                rating_data = ld.get("aggregateRating", {})
                info["rating"] = str(rating_data.get("ratingValue", ""))
                info["review_count"] = str(rating_data.get("reviewCount", ""))
                brand = ld.get("brand", {})
                if isinstance(brand, dict):
                    info["brand"] = brand.get("name", "")
                break
        except (json.JSONDecodeError, TypeError):
            continue

    # 2. og:title fallback
    if not info.get("title"):
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            info["title"] = og["content"].replace(" - AliExpress", "").strip()

    # 3. DOM scraping for fields not in JSON-LD
    # Variants (SKU properties)
    sku_titles = soup.find_all(class_=lambda c: c and "sku-item--title" in c)
    if sku_titles:
        variants = []
        for el in sku_titles[:5]:
            text = el.get_text(strip=True).replace("\xa0", " ")
            if text:
                variants.append(f"  {text}")
        if variants:
            info["variants"] = "\n".join(variants)

    # Specifications — structure: ul > li > div.prop > (div.title + div.desc)
    # Each li contains 2 props (two-column layout).
    spec_el = soup.find(class_=lambda c: c and "specification--list" in c)
    if spec_el:
        props = spec_el.find_all(class_=lambda c: c and "specification--prop" in c)
        specs = []
        for prop in props[:15]:
            title_el = prop.find(class_=lambda c: c and "specification--title" in c)
            desc_el = prop.find(class_=lambda c: c and "specification--desc" in c)
            if title_el and desc_el:
                name = title_el.get_text(strip=True)
                value = desc_el.get_text(strip=True)
                if name and value and value != "None":
                    specs.append(f"  {name}: {value}")
        if specs:
            info["specs"] = "\n".join(specs)

    # Orders sold (from body text)
    body_text = soup.get_text()
    sold_match = re.search(r"([\d,]+\+?)\s+sold", body_text)
    if sold_match:
        info["orders"] = sold_match.group(1)

    # Store name
    store_el = soup.find(class_=lambda c: c and "store-name" in c)
    if store_el:
        info["store_name"] = store_el.get_text(strip=True)

    return info


def _format_output(
    product_id: str,
    product_data: dict | None,
    reviews_data: dict | None,
) -> str:
    """Format merged product + reviews into structured text."""
    lines = []

    if product_data:
        title = product_data.get("title", "Unknown Product")
        lines.append(title)
        lines.append(f"https://www.aliexpress.com/item/{product_id}.html")
        lines.append("")

        # Price line
        price_parts = []
        if product_data.get("sale_price"):
            price_parts.append(product_data["sale_price"])
        if product_data.get("original_price"):
            price_parts.append(f"(was {product_data['original_price']})")
        if product_data.get("discount"):
            price_parts.append(f"-{product_data['discount']}%")
        if price_parts:
            lines.append(f"Price: {' '.join(price_parts)}")

        # Rating line
        rating_parts = []
        if product_data.get("rating"):
            r = f"★{product_data['rating']}"
            if product_data.get("review_count"):
                r += f" ({product_data['review_count']} reviews)"
            rating_parts.append(r)
        if product_data.get("orders"):
            rating_parts.append(f"{product_data['orders']} sold")
        if product_data.get("stock"):
            rating_parts.append(f"{product_data['stock']} in stock")
        if rating_parts:
            lines.append(" | ".join(rating_parts))

        # Store
        if product_data.get("store_name"):
            store = f"Store: {product_data['store_name']}"
            if product_data.get("positive_rate"):
                store += f" ({product_data['positive_rate']} positive)"
            lines.append(store)

        # Shipping
        if product_data.get("shipping"):
            ship = f"Shipping: {product_data['shipping']}"
            if product_data.get("delivery_days"):
                ship += f" (est. {product_data['delivery_days']} days)"
            lines.append(ship)

        lines.append("")

        # Variants
        if product_data.get("variants"):
            lines.append("Variants:")
            lines.append(product_data["variants"])
            lines.append("")

        # SKU pricing
        if product_data.get("sku_prices"):
            lines.append("SKU Pricing:")
            lines.append(product_data["sku_prices"])
            lines.append("")

        # Specs
        if product_data.get("specs"):
            lines.append("Specifications:")
            lines.append(product_data["specs"])
            lines.append("")

    # Reviews section (appended to product data, or standalone if we only have reviews)
    if reviews_data and "error" not in reviews_data:
        stats = reviews_data.get("productEvaluationStatistic", {})
        if stats:
            avg = stats.get("evarageStar", "")
            total = stats.get("totalNum", 0)
            if avg:
                lines.append(f"Rating: ★{avg} ({total} reviews)")
            breakdown = _format_rating_breakdown(stats)
            if breakdown:
                lines.append(f"Rating breakdown: {breakdown}")
            lines.append("")

        review_list = reviews_data.get("evaViewList", [])
        if review_list:
            lines.append("Recent reviews:")
            lines.append(_format_reviews(review_list))

    if not lines:
        return ""

    return "\n".join(lines).strip()


# Shared client instance (created on first use)
_client: MTopClient | None = None
_client_lock = asyncio.Lock()


async def close_client() -> None:
    """Release the shared MTopClient (for shutdown cleanup)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def _get_client(browser_solver=None) -> MTopClient:
    global _client

    needs_create = _client is None or (browser_solver and not _client.browser_solver)
    if needs_create:
        async with _client_lock:
            needs_create = _client is None or (browser_solver and not _client.browser_solver)
            if needs_create:
                # Release old client (sets session to None for GC)
                if _client is not None:
                    await _client.close()
                _client = MTopClient(browser_solver=browser_solver)
    return _client


async def _fetch_mtop(product_id: str, browser_solver=None) -> dict | None:
    """Try MTop APIs in fallback order. Returns product data dict or None."""
    client = await _get_client(browser_solver)

    # Include locale params that the browser sends — the API may return
    # empty data without them.
    data = {
        "productId": product_id,
        "_lang": "en_US",
        "_currency": "USD",
        "country": "US",
        "clientType": "pc",
    }

    for api_name, version in _MTOP_APIS:
        try:
            result = await client.request(api_name, version, data)

            # Check for errors
            ret = result.get("ret", [])
            if isinstance(ret, list):
                ret_str = " ".join(ret)
            else:
                ret_str = str(ret)

            # Success
            if "SUCCESS" in ret_str:
                # Check for product-level errors (API returns SUCCESS but item is gone)
                inner = result.get("data", {}).get("result", {})
                global_data = inner.get("GLOBAL_DATA", {}).get("globalData", {})
                error_code = global_data.get("errorCode", "")
                if error_code == "SITEM_NOT_EXIST":
                    _log(f"product {product_id} does not exist (delisted)")
                    return {"_error": "product_not_found"}

                extracted = _extract_product_data(result)
                if extracted.get("title"):
                    return extracted
                _log(f"MTop {api_name} returned SUCCESS but no product data")
                continue

            # Anti-bot triggered — client already attempted TMD solve internally,
            # so don't retry with other APIs
            if "FAIL_SYS_USER_VALIDATE" in ret_str or "RGV587_ERROR" in ret_str:
                _log(f"MTop blocked ({api_name}): {ret_str}")
                return None

            _log(f"MTop {api_name} failed: {ret_str}")
        except Exception as e:
            _log(f"MTop {api_name} exception: {e}")

    return None


async def get_product(
    product_id: str,
    cache=None,
    config=None,
    browser_solver=None,
) -> dict:
    """Get AliExpress product details with reviews.

    Tries MTop API first. Reviews are always fetched in parallel.

    Args:
        product_id: Numeric product ID or full AliExpress URL.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: Optional BrowserSolver for browser-based challenges.

    Returns:
        Dict with ``content`` (formatted text) or ``error``.
    """
    # Extract product ID from URL if needed
    pid = extract_product_id(product_id)
    if not pid:
        return {"error": f"Could not extract product ID from: {product_id}"}

    # Domain-level rate limiting (shared with aliexpress search).
    # No extra_delay → 3.0s base interval between product fetches.
    # Reviews (feedback.aliexpress.com) are exempt — different service.
    await aliexpress_limiter.wait()

    # Fetch MTop and reviews in parallel
    mtop_task = asyncio.create_task(
        _fetch_mtop(pid, browser_solver=browser_solver)
    )
    reviews_task = asyncio.create_task(fetch_reviews(pid))

    product_data, reviews_data = await asyncio.gather(mtop_task, reviews_task)

    # Product delisted — return clear error
    if product_data and product_data.get("_error") == "product_not_found":
        return {"error": f"Product {pid} not found (delisted or unavailable)."}

    # MTop succeeded — format structured data + reviews
    if product_data and product_data.get("title"):
        content = _format_output(pid, product_data, reviews_data)
        return {"content": content}

    # Nothing worked — return reviews-only if there's actual review content
    if reviews_data and "error" not in reviews_data:
        stats = reviews_data.get("productEvaluationStatistic", {})
        total = stats.get("totalNum", 0)
        review_list = reviews_data.get("evaViewList", [])
        # Only show reviews section if there are actual reviews (not all-zero stats)
        if total > 0 or review_list:
            content = _format_output(pid, None, reviews_data)
            if content:
                return {"content": content}

    return {"error": "Could not retrieve product details. MTop API may be blocked."}
