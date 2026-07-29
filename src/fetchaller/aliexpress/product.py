"""AliExpress product detail — MTop API with Chrome-based fallback."""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import time
from datetime import UTC, datetime

from ..content._numeric import bounded_number_text
from ..content._price import has_positive_price
from ..ratelimit import aliexpress_limiter
from ..security.xss import safe_log_text
from .mtop import MTopClient
from .reviews import fetch_reviews

_PRODUCT_ID_RE = re.compile(r"(?:aliexpress\.com/item/|(?<!\d))(\d{8,20})(?!\d)(?:\.html)?")
_EMBEDDED_PRODUCT_ID_RE = re.compile(r"\d{8,20}\Z")
_MAX_TITLE_CHARS = 500
_MAX_STORE_CHARS = 500
_MAX_FIELD_CHARS = 256
_MAX_DETAIL_LINE_CHARS = 1_000
_MAX_VARIANTS = 20
_MAX_VARIANT_VALUES = 20
_MAX_SPECS = 40
_MAX_SKU_PRICES = 20
_MAX_OUTPUT_CHARS = 100_000
_OUTPUT_OMISSION_MARKER = (
    "[Additional AliExpress product fields omitted to enforce the output limit.]"
)

# MTop API methods to try, in order of preference
_MTOP_APIS = [
    ("mtop.aliexpress.pdp.pc.query", "1.0"),
    ("mtop.aliexpress.itemdetail.pc.asyncPCDetail", "1.0"),
    ("mtop.aliexpress.itemdetail.msite", "1.0"),
]


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] aliexpress product: "
        f"{safe_log_text(msg)}",
        file=sys.stderr,
    )


def extract_product_id(input_str: str) -> str | None:
    """Extract numeric product ID from a URL or bare ID string."""
    m = _PRODUCT_ID_RE.search(input_str)
    return m.group(1) if m else None


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _first_dict(*values: object) -> dict:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _first_list(*values: object) -> list:
    for value in values:
        if isinstance(value, list) and value:
            return value
    return []


def _bounded_scalar(value: object, maximum: int = _MAX_FIELD_CHARS) -> str:
    """Return compact finite scalar text, rejecting nested/oversized fields."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    try:
        text = " ".join(str(value).split())
    except (OverflowError, ValueError):
        return ""
    return text if text and len(text) <= maximum else ""


def _bounded_percent(value: object) -> str:
    text = _bounded_scalar(value, 32).removesuffix("%")
    return bounded_number_text(
        text,
        minimum=0,
        maximum=100,
        minimum_exclusive=True,
    )


def _normalized_mtop_price(
    *formatted_values: object,
    raw_value: object = None,
) -> str:
    """Normalize a formatted or USD-bound raw MTop price for display."""

    for value in formatted_values:
        if isinstance(value, dict):
            value = value.get("formatedAmount") or value.get("formattedAmount")
        text = _bounded_scalar(value, _MAX_FIELD_CHARS)
        if has_positive_price(text, require_currency=True):
            return text

    if isinstance(raw_value, dict):
        formatted = (
            raw_value.get("formatedAmount")
            or raw_value.get("formattedAmount")
        )
        formatted_text = _bounded_scalar(formatted, _MAX_FIELD_CHARS)
        if has_positive_price(formatted_text, require_currency=True):
            return formatted_text
        raw_value = raw_value.get("value")
    raw_text = _bounded_scalar(raw_value, _MAX_FIELD_CHARS)
    if has_positive_price(raw_text, require_currency=True):
        return raw_text
    if has_positive_price(raw_value, require_currency=False):
        return f"USD {raw_text}"
    return ""


def _bounded_count(value: object, *, allow_plus: bool = False) -> str:
    text = _bounded_scalar(value, 32)
    suffix = "+" if allow_plus and text.endswith("+") else ""
    numeric = text.removesuffix("+")
    validated = bounded_number_text(
        numeric,
        minimum=0,
        maximum=1_000_000_000,
        integral=True,
        allow_grouping=True,
    )
    return f"{validated}{suffix}" if validated else ""


def _has_letters(value: object) -> bool:
    return isinstance(value, str) and any(character.isalpha() for character in value)


def _bounded_output(lines: list[str]) -> str:
    """Join complete lines under the MCP output contract."""

    output: list[str] = []
    length = 0
    for line in lines:
        if not isinstance(line, str):
            continue
        separator = 1 if output else 0
        if length + separator + len(line) > _MAX_OUTPUT_CHARS:
            while output:
                removed = output.pop()
                length -= len(removed) + (1 if output else 0)
                marker_separator = 1 if output else 0
                if (
                    length + marker_separator + len(_OUTPUT_OMISSION_MARKER)
                    <= _MAX_OUTPUT_CHARS
                ):
                    break
            output.append(_OUTPUT_OMISSION_MARKER)
            break
        output.append(line)
        length += separator + len(line)
    return "\n".join(output).strip()


def _bounded_detail_block(
    value: object,
    *,
    maximum_lines: int,
) -> str:
    if not isinstance(value, str):
        return ""
    lines: list[str] = []
    for line in value.splitlines()[:maximum_lines]:
        if (
            line
            and len(line) <= _MAX_DETAIL_LINE_CHARS
            and any(character.isalpha() for character in line)
        ):
            lines.append(line)
    return "\n".join(lines)


def _bounded_sku_price_block(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lines: list[str] = []
    for line in value.splitlines()[:_MAX_SKU_PRICES]:
        _, separator, price = line.partition(":")
        if (
            separator
            and len(line) <= _MAX_DETAIL_LINE_CHARS
            and has_positive_price(price.strip(), require_currency=True)
        ):
            lines.append(line)
    return "\n".join(lines)


def _format_rating_breakdown(stats: dict) -> str:
    """Format rating breakdown from productEvaluationStatistic."""
    if not isinstance(stats, dict):
        return ""
    parts = []
    star_names = {5: "five", 4: "four", 3: "three", 2: "two", 1: "one"}
    for stars, name in star_names.items():
        rate = bounded_number_text(
            stats.get(f"{name}StarRate"),
            minimum=0,
            maximum=100,
        )
        if rate:
            parts.append(f"★{stars}: {rate}%")
    return " | ".join(parts)


def _format_reviews(review_list: list[dict]) -> str:
    """Format review list into readable text."""
    lines = []
    for review in _as_list(review_list)[:5]:
        if not isinstance(review, dict):
            continue
        buyer_eval = bounded_number_text(
            review.get("buyerEval"),
            minimum=0,
            maximum=100,
            integral=True,
        )
        rating = int(buyer_eval) // 20 if buyer_eval else 0
        stars = f"★{rating}" if rating else ""
        country = _bounded_scalar(review.get("buyerCountry"), 32)
        date = _bounded_scalar(review.get("evalDate"), 64)
        sku = _bounded_scalar(review.get("skuInfo"), 256)

        # Prefer translated feedback, fall back to original
        text = _bounded_scalar(
            review.get("buyerTranslationFeedback")
            or review.get("buyerFeedback"),
            1_000,
        )
        if len(text) > 200:
            text = text[:197] + "..."

        images = _as_list(review.get("images"))
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
    if not isinstance(result, dict):
        return {}
    data = result.get("data", {})
    # pdp.pc.query wraps in data.result
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("result"), dict):
        data = data["result"]

    info: dict = {
        "product_id": "",
        "title": "",
        "sale_price": "",
        "original_price": "",
        "discount": "",
        "sku_prices": "",
        "rating": "",
        "review_count": "",
        "store_name": "",
        "positive_rate": "",
        "stock": "",
        "orders": "",
        "shipping": "",
        "delivery_days": "",
        "variants": "",
        "specs": "",
    }

    # A successful MTop response must bind its detail to the requested item.
    # Different supported endpoints place that identifier either alongside the
    # modules or in GLOBAL_DATA.globalData.  Keep every known source explicit;
    # accepting an unbound title/price shell is worse than rejecting an
    # unfamiliar response shape.
    global_module = data.get("GLOBAL_DATA")
    global_data = (
        global_module.get("globalData", {})
        if isinstance(global_module, dict)
        else {}
    )
    id_candidates = [
        data.get("itemId"),
        data.get("item_id"),
        data.get("productId"),
        data.get("product_id"),
        global_data.get("itemId") if isinstance(global_data, dict) else None,
        global_data.get("item_id") if isinstance(global_data, dict) else None,
        global_data.get("productId") if isinstance(global_data, dict) else None,
        global_data.get("product_id") if isinstance(global_data, dict) else None,
    ]
    normalized_ids = {
        str(candidate)
        for candidate in id_candidates
        if not isinstance(candidate, bool)
        and _EMBEDDED_PRODUCT_ID_RE.fullmatch(str(candidate))
    }
    # Multiple conflicting IDs are not a trustworthy binding.  Preserve an
    # empty value so the semantic gate fails closed rather than guessing.
    info["product_id"] = normalized_ids.pop() if len(normalized_ids) == 1 else ""

    # Title
    title_mod = _first_dict(data.get("PRODUCT_TITLE"), data.get("titleModule"))
    info["title"] = _bounded_scalar(
        title_mod.get("text") or title_mod.get("subject"),
        _MAX_TITLE_CHARS,
    )

    # Price
    price_mod = _first_dict(data.get("PRICE"), data.get("priceModule"))
    target = _first_dict(
        price_mod.get("targetSkuPriceInfo"),
        price_mod.get("formattedActivityPrice"),
    )
    sale = _normalized_mtop_price(
        target.get("salePriceString"),
        target.get("salePriceLocal"),
        price_mod.get("formattedPrice"),
        raw_value=target.get("salePrice"),
    )
    info["sale_price"] = sale
    orig = _normalized_mtop_price(
        target.get("originalPriceString"),
        price_mod.get("formattedOriginalPrice"),
        raw_value=target.get("originalPrice"),
    )
    info["original_price"] = orig
    info["discount"] = _bounded_percent(
        target.get("discount") or price_mod.get("discount")
    )

    # SKU pricing map
    sku_map = price_mod.get("skuPriceInfoMap", {})
    if isinstance(sku_map, dict):
        sku_prices = []
        for sku_id, sku_info in list(sku_map.items())[:_MAX_SKU_PRICES]:
            if not isinstance(sku_info, dict):
                continue
            sku_id_text = _bounded_scalar(sku_id, 64)
            sp = _normalized_mtop_price(
                sku_info.get("salePriceString"),
                raw_value=sku_info.get("salePrice"),
            )
            if (
                sku_id_text
                and has_positive_price(sp, require_currency=True)
            ):
                sku_prices.append(f"  SKU {sku_id_text}: {sp}")
        info["sku_prices"] = "\n".join(sku_prices)

    # Rating
    rating_mod = _first_dict(data.get("PC_RATING"), data.get("titleModule"))
    info["rating"] = bounded_number_text(
        rating_mod.get("rating") or rating_mod.get("averageStar"),
        minimum=0,
        maximum=5,
    )
    info["review_count"] = _bounded_count(
        rating_mod.get("totalValidNum") or rating_mod.get("feedbackCount")
    )

    # Store
    store_mod = _first_dict(data.get("SHOP_CARD_PC"), data.get("storeModule"))
    info["store_name"] = _bounded_scalar(
        store_mod.get("storeName"),
        _MAX_STORE_CHARS,
    )
    info["positive_rate"] = _bounded_percent(
        store_mod.get("sellerPositiveRate")
        or store_mod.get("positiveRate")
    )

    # Quantity
    qty_mod = _first_dict(data.get("QUANTITY_PC"), data.get("quantityModule"))
    info["stock"] = _bounded_count(qty_mod.get("totalAvailableInventory"))

    # Trade/orders
    trade_mod = _first_dict(data.get("TRADE"), data.get("tradeModule"))
    info["orders"] = _bounded_count(
        trade_mod.get("tradeCount") or trade_mod.get("formatTradeCount"),
        allow_plus=True,
    )

    # Shipping
    ship_mod = _first_dict(data.get("SHIPPING"), data.get("shippingModule"))
    freight = _as_dict(ship_mod.get("generalFreightInfo"))
    ship_list = _first_list(
        ship_mod.get("originalLayoutResultList"),
        freight.get("originalLayoutResultList"),
    )
    if ship_list:
        first = _as_dict(ship_list[0])
        biz = _as_dict(first.get("bizData"))
        display_amount = _bounded_scalar(
            biz.get("displayAmount"),
            _MAX_FIELD_CHARS,
        )
        if display_amount.lower() == "free shipping":
            shipping = display_amount
        else:
            shipping = _normalized_mtop_price(
                display_amount,
                raw_value=biz.get("shippingFee"),
            )
        if shipping:
            info["shipping"] = shipping
        info["delivery_days"] = bounded_number_text(
            biz.get("deliveryDayMax"),
            minimum=0,
            maximum=3650,
            integral=True,
            minimum_exclusive=True,
        )

    # Variants (SKU properties)
    sku_mod = _first_dict(data.get("SKU"), data.get("skuModule"))
    props = _first_list(
        sku_mod.get("skuProperties"),
        sku_mod.get("productSKUPropertyList"),
    )
    if props:
        variants = []
        for prop in props[:_MAX_VARIANTS]:
            if not isinstance(prop, dict):
                continue
            name = _bounded_scalar(prop.get("skuPropertyName"), 128)
            values = _as_list(prop.get("skuPropertyValues"))
            val_names = [
                value_name
                for value in values[:_MAX_VARIANT_VALUES]
                if isinstance(value, dict)
                and (
                    value_name := _bounded_scalar(
                        value.get("propertyValueDisplayName")
                        or value.get("propertyValueName"),
                        256,
                    )
                )
            ]
            if name and val_names:
                line = f"  {name}: {', '.join(val_names)}"
                if len(line) <= _MAX_DETAIL_LINE_CHARS and _has_letters(line):
                    variants.append(line)
        info["variants"] = "\n".join(variants)

    # Specifications
    spec_mod = _first_dict(
        data.get("PRODUCT_PROP_PC"),
        data.get("productPropModule"),
    )
    shown = _first_list(
        spec_mod.get("showedProps"),
        spec_mod.get("outerProps"),
        spec_mod.get("props"),
    )
    if shown:
        specs = []
        for spec in shown[:_MAX_SPECS]:
            if not isinstance(spec, dict):
                continue
            name = _bounded_scalar(
                spec.get("name") or spec.get("attrName"),
                256,
            )
            value = _bounded_scalar(
                spec.get("value") or spec.get("attrValue"),
                512,
            )
            line = f"  {name}: {value}"
            if (
                name
                and value
                and len(line) <= _MAX_DETAIL_LINE_CHARS
                and _has_letters(line)
            ):
                specs.append(line)
        info["specs"] = "\n".join(specs)

    return info


def _has_useful_product_data(
    product_data: dict | None,
    *,
    expected_product_id: str | None = None,
) -> bool:
    """Whether data is a bound, substantive, purchasable product record.

    MTop may return a nominal SUCCESS response for a challenge or page shell.
    The product tool therefore requires the item ID echoed by the supported
    payload, a positive displayed price, and one real product-detail module:
    seller/store identity, SKU variants, or specifications.  This is an OR
    gate, not an arbitrary field count, so sparse legitimate listings from any
    supported endpoint remain usable.
    """

    if not isinstance(product_data, dict):
        return False
    title = product_data.get("title")
    if not isinstance(title, str) or not title.strip():
        return False
    if not any(character.isalpha() for character in title):
        return False
    embedded_id = product_data.get("product_id")
    if not isinstance(embedded_id, str) or not _EMBEDDED_PRODUCT_ID_RE.fullmatch(
        embedded_id
    ):
        return False
    if expected_product_id is not None and embedded_id != expected_product_id:
        return False
    # A title from Open Graph alone is a page shell, not product detail.  This
    # tool's contract requires an actual displayed numeric price or range;
    # stock only describes availability and cannot substitute for it.
    if not has_positive_price(
        product_data.get("sale_price"),
        require_currency=True,
    ):
        return False

    # Each value below is extracted from a product-specific MTop module.  Do
    # not let ratings, stock, or reviews alone substitute for product detail:
    # those fields can be present on challenge shells as well.
    for field in ("store_name", "variants", "specs"):
        value = product_data.get(field)
        if isinstance(value, str) and any(
            character.isalpha() for character in value
        ):
            return True
    return False


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
            if isinstance(ld, dict) and ld.get("@type") == "Product":
                title = _bounded_scalar(ld.get("name"), _MAX_TITLE_CHARS)
                info["title"] = title.removesuffix(" - AliExpress").strip()
                offers = _as_dict(ld.get("offers"))
                currency = _bounded_scalar(offers.get("priceCurrency"), 16)
                price = _bounded_scalar(offers.get("price"), 128)
                if price and currency:
                    candidate_price = _bounded_scalar(f"{currency} {price}")
                elif price:
                    candidate_price = _bounded_scalar(price)
                else:
                    candidate_price = ""
                if has_positive_price(candidate_price, require_currency=True):
                    info["sale_price"] = candidate_price
                rating_data = _as_dict(ld.get("aggregateRating"))
                info["rating"] = bounded_number_text(
                    rating_data.get("ratingValue"),
                    minimum=0,
                    maximum=5,
                )
                info["review_count"] = _bounded_count(
                    rating_data.get("reviewCount")
                )
                brand = ld.get("brand", {})
                if isinstance(brand, dict):
                    info["brand"] = _bounded_scalar(brand.get("name"), 256)
                break
        except (AttributeError, json.JSONDecodeError, TypeError):
            continue

    # 2. og:title fallback
    if not info.get("title"):
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = _bounded_scalar(og["content"], _MAX_TITLE_CHARS)
            info["title"] = title.removesuffix(" - AliExpress").strip()

    # 3. DOM scraping for fields not in JSON-LD
    # Variants (SKU properties)
    sku_titles = soup.find_all(class_=lambda c: c and "sku-item--title" in c)
    if sku_titles:
        variants = []
        for el in sku_titles[:5]:
            text = _bounded_scalar(
                el.get_text(strip=True).replace("\xa0", " "),
                _MAX_DETAIL_LINE_CHARS,
            )
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
        for prop in props[:_MAX_SPECS]:
            title_el = prop.find(class_=lambda c: c and "specification--title" in c)
            desc_el = prop.find(class_=lambda c: c and "specification--desc" in c)
            if title_el and desc_el:
                name = _bounded_scalar(title_el.get_text(strip=True), 256)
                value = _bounded_scalar(desc_el.get_text(strip=True), 512)
                if name and value and value != "None" and _has_letters(
                    f"{name}: {value}"
                ):
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
        info["store_name"] = _bounded_scalar(
            store_el.get_text(strip=True),
            _MAX_STORE_CHARS,
        )

    return info


def _format_output(
    product_id: str,
    product_data: dict | None,
    reviews_data: dict | None,
) -> str:
    """Format merged product + reviews into structured text."""
    lines = []

    if isinstance(product_data, dict):
        title = _bounded_scalar(product_data.get("title"), _MAX_TITLE_CHARS)
        if _has_letters(title):
            lines.append(title)
        lines.append(f"https://www.aliexpress.com/item/{product_id}.html")
        lines.append("")

        # Price line
        price_parts = []
        sale_price = _bounded_scalar(product_data.get("sale_price"))
        original_price = _bounded_scalar(product_data.get("original_price"))
        discount = _bounded_percent(product_data.get("discount"))
        if has_positive_price(sale_price, require_currency=True):
            price_parts.append(sale_price)
        if has_positive_price(original_price, require_currency=True):
            price_parts.append(f"(was {original_price})")
        if discount:
            price_parts.append(f"-{discount}%")
        if price_parts:
            lines.append(f"Price: {' '.join(price_parts)}")

        # Rating line
        rating_parts = []
        rating = bounded_number_text(
            product_data.get("rating"),
            minimum=0,
            maximum=5,
        )
        review_count = _bounded_count(product_data.get("review_count"))
        orders = _bounded_count(product_data.get("orders"), allow_plus=True)
        stock = _bounded_count(product_data.get("stock"))
        if rating:
            r = f"★{rating}"
            if review_count:
                r += f" ({review_count} reviews)"
            rating_parts.append(r)
        if orders:
            rating_parts.append(f"{orders} sold")
        if stock:
            rating_parts.append(f"{stock} in stock")
        if rating_parts:
            lines.append(" | ".join(rating_parts))

        # Store
        store_name = _bounded_scalar(
            product_data.get("store_name"),
            _MAX_STORE_CHARS,
        )
        positive_rate = _bounded_percent(product_data.get("positive_rate"))
        if _has_letters(store_name):
            store = f"Store: {store_name}"
            if positive_rate:
                store += f" ({positive_rate}% positive)"
            lines.append(store)

        # Shipping
        shipping = _bounded_scalar(product_data.get("shipping"))
        delivery_days = bounded_number_text(
            product_data.get("delivery_days"),
            minimum=0,
            maximum=3650,
            integral=True,
            minimum_exclusive=True,
        )
        if shipping.lower() == "free shipping" or has_positive_price(
            shipping,
            require_currency=True,
        ):
            ship = f"Shipping: {shipping}"
            if delivery_days:
                ship += f" (est. {delivery_days} days)"
            lines.append(ship)

        lines.append("")

        # Variants
        variants = _bounded_detail_block(
            product_data.get("variants"),
            maximum_lines=_MAX_VARIANTS,
        )
        if variants:
            lines.append("Variants:")
            lines.extend(variants.splitlines())
            lines.append("")

        # SKU pricing
        sku_prices = _bounded_sku_price_block(product_data.get("sku_prices"))
        if sku_prices:
            lines.append("SKU Pricing:")
            lines.extend(sku_prices.splitlines())
            lines.append("")

        # Specs
        specs = _bounded_detail_block(
            product_data.get("specs"),
            maximum_lines=_MAX_SPECS,
        )
        if specs:
            lines.append("Specifications:")
            lines.extend(specs.splitlines())
            lines.append("")

    # Reviews section (appended to product data, or standalone if we only have reviews)
    if isinstance(reviews_data, dict) and "error" not in reviews_data:
        stats = _as_dict(reviews_data.get("productEvaluationStatistic"))
        if stats:
            avg = bounded_number_text(
                stats.get("evarageStar"),
                minimum=0,
                maximum=5,
            )
            total = _bounded_count(stats.get("totalNum"))
            if avg:
                review_suffix = f" ({total} reviews)" if total else ""
                lines.append(f"Rating: ★{avg}{review_suffix}")
            breakdown = _format_rating_breakdown(stats)
            if breakdown:
                lines.append(f"Rating breakdown: {breakdown}")
            if avg or breakdown:
                lines.append("")

        review_list = _as_list(reviews_data.get("evaViewList"))
        if review_list:
            formatted_reviews = _format_reviews(review_list)
        else:
            formatted_reviews = ""
        if formatted_reviews:
            lines.append("Recent reviews:")
            lines.extend(formatted_reviews.splitlines())

    if not lines:
        return ""

    return _bounded_output(lines)


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


async def _fetch_mtop(
    product_id: str,
    browser_solver=None,
    deadline: float | None = None,
) -> dict | None:
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
            if deadline is not None and deadline <= time.monotonic():
                raise TimeoutError("AliExpress product deadline exhausted")
            result = await client.request(
                api_name, version, data, deadline=deadline
            )
            if not isinstance(result, dict):
                _log(f"MTop {api_name} returned a malformed response")
                continue

            # Check for errors
            ret = result.get("ret", [])
            if isinstance(ret, list):
                ret_str = " ".join(ret)
            else:
                ret_str = str(ret)

            # Success
            if "SUCCESS" in ret_str:
                # Check for product-level errors (API returns SUCCESS but item is gone)
                inner = _as_dict(_as_dict(result.get("data")).get("result"))
                global_module = _as_dict(inner.get("GLOBAL_DATA"))
                global_data = _as_dict(global_module.get("globalData"))
                error_code = global_data.get("errorCode", "")
                if error_code == "SITEM_NOT_EXIST":
                    _log(f"product {product_id} does not exist (delisted)")
                    return {"_error": "product_not_found"}

                extracted = _extract_product_data(result)
                if _has_useful_product_data(
                    extracted, expected_product_id=product_id
                ):
                    return extracted
                _log(f"MTop {api_name} returned SUCCESS but no useful product data")
                continue

            # Anti-bot triggered — client already attempted TMD solve internally,
            # so don't retry with other APIs
            if "FAIL_SYS_USER_VALIDATE" in ret_str or "RGV587_ERROR" in ret_str:
                _log(f"MTop blocked ({api_name})")
                return None

            _log(f"MTop {api_name} failed")
        except TimeoutError:
            raise
        except Exception as e:
            _log(f"MTop {api_name} exception: {type(e).__name__}")

    return None


async def get_product(
    product_id: str,
    cache=None,
    config=None,
    browser_solver=None,
    timeout: int = 180,
) -> dict:
    """Get AliExpress product details with reviews.

    Tries MTop API first. Reviews are always fetched in parallel.

    Args:
        product_id: Numeric product ID or full AliExpress URL.
        cache: ResponseCache instance.
        config: Config instance.
        browser_solver: Optional BrowserSolver for browser-based challenges.
        timeout: End-to-end operation deadline in seconds (1 through 180).

    Returns:
        Dict with ``content`` (formatted text) or ``error``.
    """
    if type(timeout) is not int or not 1 <= timeout <= 180:
        return {"error": "timeout must be an integer from 1 to 180."}
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("AliExpress product retrieval timed out")
        return value

    def consume_detached_task(task: asyncio.Task) -> None:
        """Retrieve a late task outcome without extending the tool deadline."""

        try:
            task.exception()
        except BaseException:
            pass

    def cancel_and_detach(*tasks: asyncio.Task) -> None:
        for task in tasks:
            if task.done():
                consume_detached_task(task)
                continue
            task.cancel()
            task.add_done_callback(consume_detached_task)

    # Extract product ID from URL if needed
    pid = extract_product_id(product_id)
    if not pid:
        return {"error": f"Could not extract product ID from: {product_id}"}

    # Domain-level rate limiting (shared with aliexpress search).
    # No extra_delay → 3.0s base interval between product fetches.
    # Reviews (feedback.aliexpress.com) are exempt — different service.
    try:
        await asyncio.wait_for(aliexpress_limiter.wait(), timeout=remaining())
    except TimeoutError:
        return {"error": "AliExpress product retrieval timed out."}

    # Fetch MTop and reviews in parallel
    mtop_task = asyncio.create_task(
        _fetch_mtop(pid, browser_solver=browser_solver, deadline=deadline)
    )
    try:
        reviews_timeout = remaining()
    except TimeoutError:
        cancel_and_detach(mtop_task)
        return {"error": "AliExpress product retrieval timed out."}
    reviews_task = asyncio.create_task(fetch_reviews(pid, timeout=reviews_timeout))

    try:
        done, pending = await asyncio.wait(
            {mtop_task, reviews_task},
            timeout=remaining(),
        )
    except asyncio.CancelledError:
        cancel_and_detach(mtop_task, reviews_task)
        raise

    if pending:
        cancel_and_detach(mtop_task, reviews_task)
        return {"error": "AliExpress product retrieval timed out."}

    try:
        product_data = mtop_task.result()
        reviews_data = reviews_task.result()
    except asyncio.CancelledError:
        # A provider may cancel itself. It is an ordinary retrieval failure,
        # not cancellation of the MCP caller (handled around asyncio.wait).
        return {"error": "Could not retrieve product details. MTop API may be blocked."}
    except Exception:
        return {"error": "Could not retrieve product details. MTop API may be blocked."}

    # Product delisted — return clear error
    if product_data and product_data.get("_error") == "product_not_found":
        return {"error": f"Product {pid} not found (delisted or unavailable)."}

    # MTop succeeded — format structured data + reviews
    if _has_useful_product_data(product_data, expected_product_id=pid):
        content = _format_output(pid, product_data, reviews_data)
        return {"content": content}

    # Reviews do not identify a purchasable product or provide its price,
    # variants, or specifications.  Returning them as a successful product
    # detail response masks an MTop/TMD failure and violates this tool's
    # contract, so preserve the failure explicitly instead.
    if reviews_data and "error" not in reviews_data:
        _log("MTop product detail unavailable; refusing reviews-only success")
    return {"error": "Could not retrieve product details. MTop API may be blocked."}
