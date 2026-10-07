"""Render Home Depot products, listings, reviews and stores as compact markdown.

Three things are kept honest throughout:

* **Whose price.** Both storefronts price per store. Every price is printed
  next to the store it belongs to (homedepot.com: the store the site assigns
  with none selected; homedepot.ca: its "Online" store), never as a national
  figure.
* **Was-prices only when the site gives one.** A struck-through price is
  printed only when the site returns a higher original; ``original == value``
  is the normal case on homedepot.com and is not a sale.
* **Sponsored rows are labelled.** homedepot.com ranks paid placements inline
  with organic results; a row it marks ``isSponsored`` says so.
"""

from __future__ import annotations

import html
import math
import re
from urllib.parse import urljoin, urlparse

from .urls import CA_ORIGIN, COM_ORIGIN, COM_PAGE_SIZE, ComTarget, with_param


def _m(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: object) -> list:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _money(value: object, currency: str = "") -> str | None:
    amount = _num(value)
    if amount is None:
        return None
    text = f"${amount:,.2f}"
    return f"{text} {currency}" if currency else text


_SAFE_HREF = re.compile(r"(?:https?://|/(?!/))[^\s<>\"']*\Z", re.I)
# Line starts that markdown reads as structure. Review and description text is
# written by shoppers and merchants; "# Great drill" must stay a sentence.
_STRUCTURE_START = re.compile(r"^(#{1,6}\s|>|\d+[.)]\s)")


def _markdown_link(label: str, href: str) -> str:
    """``[label](href)`` for a safe href, else the label alone."""
    href = html.unescape(href).strip()
    if not _SAFE_HREF.match(href):
        return label
    href = href.replace("(", "%28").replace(")", "%29")
    label = label.replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({href})"


def _text(value: object) -> str:
    """Plain text from the sites' small HTML/entity-laden strings."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<br\s*/?>|</p>|</li>", "\n", value, flags=re.I)
    # A bullet that is a link ("Shop All Dewalt Tools") is a link on the page;
    # dropping the href would leave a call to action pointing nowhere.
    value = re.sub(
        r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        lambda m: _markdown_link(html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(), m.group(1)),
        value,
        flags=re.I | re.S,
    )
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(
        ("\\" + line) if _STRUCTURE_START.match(line) else line for line in lines if line
    )


def _rating(average: object, count: object) -> str | None:
    avg = _num(average)
    total = _num(count)
    if avg is None or not total:
        return None
    return f"★{avg:.1f}/5 from {int(total):,} review{'s' if total != 1 else ''}"


def _date(value: object) -> str:
    return str(value)[:10] if value else ""


# ---------------------------------------------------------------------------
# homedepot.com
# ---------------------------------------------------------------------------

_COM_SERVICE_LABELS = {
    "bopis": "Pickup in store",
    "boss": "Ship to store",
    "sth": "Ship to home",
    "express delivery": "Express delivery from store",
    "direct delivery": "Direct delivery",
}

_COM_SORT_LABELS = {
    "bestmatch": "Best Match",
    "topsellers": "Top Sellers",
    "toprated": "Top Rated",
    "price": "Price",
    "mostpopular": "Most Popular",
    "newitems": "New Items",
    "productname": "Product Name",
    "brandname": "Brand Name",
}


def com_price_line(pricing: object, info: object = None) -> str:
    """The price as homedepot.com shows it, with any sale it reports."""
    pricing = _m(pricing)
    value = _num(pricing.get("value"))
    if _m(info).get("hidePrice") or value is None:
        message = str(pricing.get("message") or "").strip()
        return f"not shown on the page{f' ({message})' if message else ''}"
    message = str(pricing.get("message") or "").strip()
    parts = [_money(value) or ""]
    uom = str(pricing.get("unitOfMeasure") or "").strip()
    if uom and uom.lower() != "each":
        parts[0] += f" /{uom}"
    # "Starting at" qualifies the number (a configurable product's lowest
    # option); printed after it, it reads as a separate note.
    if message.lower().startswith(("starting at", "from")):
        parts[0] = f"{message} {parts[0]}"
        message = ""
    original = _num(pricing.get("original"))
    promotion = _m(pricing.get("promotion"))
    if original is not None and original > value:
        saving = original - value
        pct = _num(promotion.get("percentageOff"))
        detail = f"was {_money(original)}, save {_money(saving)}"
        if pct:
            detail += f" ({pct:.0f}%)"
        end = _date(_m(promotion.get("dates")).get("end"))
        if end:
            detail += f", ends {end}"
        parts.append(f"({detail})")
    description = _m(promotion.get("description")).get("shortDesc")
    if description:
        parts.append(f"— {_text(description)}")
    if pricing.get("specialBuy"):
        parts.append("· Special Buy")
    unit = _m(_m(pricing.get("alternate")).get("unit"))
    unit_price = _money(unit.get("value"))
    if unit_price and unit.get("caseUnitOfMeasure"):
        parts.append(f"· {unit_price} per {unit['caseUnitOfMeasure']}")
    bulk = _m(_m(pricing.get("alternate")).get("bulk"))
    bulk_price = _money(bulk.get("value"))
    if bulk_price and bulk.get("thresholdQuantity"):
        parts.append(f"· {bulk_price} each for {bulk['thresholdQuantity']}+")
    if message:
        parts.append(f"· {message}")
    return " ".join(p for p in parts if p)


def _inventory(inv: dict) -> str:
    qty = inv.get("quantity")
    if inv.get("isOutOfStock"):
        return "out of stock"
    if inv.get("isUnavailable"):
        return "unavailable"
    if inv.get("isLimitedQuantity"):
        return f"limited stock ({qty})" if qty else "limited stock"
    if inv.get("isInStock"):
        return f"{qty:,} in stock" if isinstance(qty, int) and qty > 0 else "in stock"
    return ""


def com_fulfillment_lines(fulfillment: object) -> list[str]:
    """One line per way the product can reach the customer."""
    fulfillment = _m(fulfillment)
    lines: list[str] = []
    for option in _l(fulfillment.get("fulfillmentOptions")):
        services = _l(option.get("services"))
        if not option.get("fulfillable") and not services:
            lines.append(f"- {str(option.get('type') or 'option').title()}: not available")
            continue
        for service in services:
            kind = str(service.get("type") or "")
            label = _COM_SERVICE_LABELS.get(kind.lower(), kind or "Service")
            bits: list[str] = []
            anchors = [loc for loc in _l(service.get("locations")) if loc.get("isAnchor")]
            if kind.lower() in {"bopis", "boss"} and anchors:
                loc = anchors[0]
                where = ", ".join(x for x in (loc.get("storeName"), loc.get("state")) if x)
                if where:
                    label += f" ({where} #{loc.get('locationId')})"
                if kind.lower() == "bopis":
                    stock = _inventory(_m(loc.get("inventory")))
                    if stock:
                        bits.append(stock)
            if not option.get("fulfillable"):
                bits.append("not available")
            dates = _m(service.get("deliveryDates"))
            start, end = _date(dates.get("startDate")), _date(dates.get("endDate"))
            if start:
                bits.append(f"arrives {start}" + (f" – {end}" if end and end != start else ""))
            charge = _num(service.get("totalCharge"))
            if service.get("hasFreeShipping") or charge == 0:
                if kind.lower() != "bopis":
                    bits.append("free")
            elif charge:
                bits.append(f"{_money(charge)} delivery")
            threshold = _money(service.get("freeDeliveryThreshold"))
            if threshold:
                bits.append(f"free over {threshold}")
            lines.append(f"- {label}" + (f": {', '.join(bits)}" if bits else ""))
    if fulfillment.get("backordered"):
        ship = _date(fulfillment.get("backorderedShipDate"))
        lines.append("- Backordered" + (f", ships {ship}" if ship else ""))
    excluded = str(fulfillment.get("excludedShipStates") or "").strip()
    if excluded:
        lines.append(f"- Does not ship to: {excluded.replace(',', ', ')}")
    return lines


def com_store_label(fulfillment: object) -> str:
    """The store a product's price and pickup stock belong to."""
    from .com import DEFAULT_STORE_ID

    for option in _l(_m(fulfillment).get("fulfillmentOptions")):
        for service in _l(option.get("services")):
            for loc in _l(service.get("locations")):
                if loc.get("isAnchor") and str(loc.get("locationId")) == DEFAULT_STORE_ID and loc.get("storeName"):
                    where = ", ".join(x for x in (loc.get("storeName"), loc.get("state")) if x)
                    return f"#{DEFAULT_STORE_ID} {where} (no store selected)"
    return f"#{DEFAULT_STORE_ID} (no store selected)"


def _com_image(image: dict) -> str | None:
    url = str(image.get("url") or "")
    if not url:
        return None
    sizes = [s for s in image.get("sizes") or [] if str(s).isdigit()]
    size = "1000" if "1000" in sizes else (max(sizes, key=int) if sizes else "600")
    return url.replace("<SIZE>", size)


def render_com_product(product: dict, url: str) -> str:
    ident = _m(product.get("identifiers"))
    details = _m(product.get("details"))
    info = _m(product.get("info"))
    brand = str(ident.get("brandName") or "").strip()
    label = str(ident.get("productLabel") or "").strip()
    item_id = str(product.get("itemId") or "")
    canonical = urljoin(COM_ORIGIN, str(ident.get("canonicalUrl") or "")) if ident.get("canonicalUrl") else url

    out = [f"# {brand} {label}".strip(), ""]
    ids = [f"**Internet #** {item_id}"]
    for key, name in (("modelNumber", "Model #"), ("storeSkuNumber", "Store SKU #"), ("upc", "UPC")):
        if ident.get(key):
            ids.append(f"**{name}** {ident[key]}")
    out.append(" · ".join(ids))
    out.append(f"**Price:** {com_price_line(product.get('pricing'), info)}")
    out.append(f"**Store:** {com_store_label(product.get('fulfillment'))}")
    rating = _rating(
        _m(_m(product.get("reviews")).get("ratingsReviews")).get("averageRating"),
        _m(_m(product.get("reviews")).get("ratingsReviews")).get("totalReviews"),
    )
    if rating:
        out.append(f"**Rating:** {rating}")
    availability = _m(product.get("availabilityType"))
    if availability.get("discontinued"):
        out.append("**Status:** discontinued")
    crumbs = [c.get("label") for c in _l(_m(product.get("taxonomy")).get("breadCrumbs")) if c.get("label")]
    if crumbs:
        out.append(f"**Category:** {' > '.join(crumbs)}")
    terms = []
    if info.get("returnable"):
        terms.append(f"Returns: {info['returnable']}")
    if info.get("quantityLimit"):
        terms.append(f"Limit {info['quantityLimit']} per order")
    if terms:
        out.append(" · ".join(terms))
    collection = _m(details.get("collection"))
    if collection.get("name"):
        out.append(f"**Collection:** {collection['name']}")

    lines = com_fulfillment_lines(product.get("fulfillment"))
    out.append("")
    out.append("## Availability")
    out.extend(lines or ["- No fulfillment options were reported for this store."])

    description = _text(details.get("description"))
    bullets = [
        _text(a.get("value"))
        for a in _l(details.get("descriptiveAttributes"))
        if a.get("bulleted") and _text(a.get("value"))
    ]
    highlights = [_text(h) for h in details.get("highlights") or [] if _text(h)]
    if description or bullets or highlights:
        out += ["", "## Description"]
        if description:
            out.append(description)
        if highlights:
            out.append("")
            out.extend(f"- {h}" for h in highlights)
        if bullets:
            out.append("")
            out.extend(f"- {b}" for b in bullets)

    groups = _l(product.get("specificationGroup"))
    if groups:
        out += ["", "## Specifications"]
        for group in groups:
            specs = [s for s in _l(group.get("specifications")) if s.get("specName")]
            if not specs:
                continue
            out.append(f"### {group.get('specTitle') or 'Specifications'}")
            out.extend(f"- {s['specName']}: {s.get('specValue') if s.get('specValue') not in (None, '') else '-'}" for s in specs)

    images = [i for i in (_com_image(img) for img in _l(_m(product.get("media")).get("images"))) if i]
    if images:
        out += ["", "## Images"]
        out.extend(f"- {i}" for i in images[:8])

    slug = urlparse(canonical).path.rstrip("/").split("/")
    review_slug = slug[2] if len(slug) > 3 and slug[1] == "p" else "product"
    out += [
        "",
        f"Reviews: {COM_ORIGIN}/p/reviews/{review_slug}/{item_id}/1",
        f"Product page: {canonical}",
        "",
        "_Read from homedepot.com's data gateway. Price and pickup stock are for the store named above, "
        "the one homedepot.com assigns when no store is selected; both vary by store._",
    ]
    return "\n".join(out).strip() + "\n"


def _com_row_availability(product: dict) -> str:
    bits: list[str] = []
    for option in _l(_m(product.get("fulfillment")).get("fulfillmentOptions")):
        if not option.get("fulfillable"):
            continue
        for service in _l(option.get("services")):
            kind = str(service.get("type") or "").lower()
            if kind == "bopis":
                anchors = [loc for loc in _l(service.get("locations")) if loc.get("isAnchor")]
                inventory = _m(anchors[0].get("inventory")) if anchors else {}
                qty = inventory.get("quantity")
                if isinstance(qty, int) and qty > 0:
                    bits.append(f"pickup ({qty:,} at store)")
                elif inventory.get("isInStock") is False or qty == 0:
                    # Fulfillable as a service, but not from this store's shelf.
                    bits.append("pickup (out of stock at store)")
                else:
                    bits.append("pickup")
            elif kind == "sth":
                bits.append("ship to home")
            elif kind == "boss":
                bits.append("ship to store")
            elif kind:
                bits.append(_COM_SERVICE_LABELS.get(kind, kind).lower())
    return ", ".join(dict.fromkeys(bits))


def _com_sort_label(report: dict) -> str:
    by = str(report.get("sortBy") or "").lower()
    order = str(report.get("sortOrder") or "").lower()
    label = _COM_SORT_LABELS.get(by, by or "default order")
    if by == "price" and order in {"asc", "desc"}:
        label += " (low to high)" if order == "asc" else " (high to low)"
    return label


def render_com_listing(
    model: dict,
    target: ComTarget,
    url: str,
    *,
    redirected_to: str | None = None,
) -> str:
    report = _m(model.get("searchReport"))
    metadata = _m(model.get("metadata"))
    products = _l(model.get("products"))
    total = int(_num(report.get("totalProducts")) or 0)
    start = int(_num(report.get("startIndex")) or target.start_index)

    # Applied refinements ride in the breadcrumb trail too ("Drills > $50 -
    # $150"); they are listed under "Filters applied", so only the category
    # path is kept here.
    crumbs = [
        c
        for c in _l(_m(model.get("taxonomy")).get("breadCrumbs"))
        if c.get("label") and str(c.get("dimensionName") or "Category") == "Category"
    ]
    if target.keyword and not target.nav_param and not redirected_to:
        title = f'Home Depot search: "{report.get("keyword") or target.keyword}"'
    else:
        title = str(metadata.get("h1Tag") or "").strip() or (crumbs[-1]["label"] if crumbs else "Home Depot listing")
        if target.keyword and target.nav_param:
            title += f' — "{target.keyword}"'
    out = [f"# {title}", ""]
    if redirected_to:
        out.append(
            f'_homedepot.com sends the search "{target.keyword}" to {redirected_to} '
            f"— these are that page's results._"
        )
        out.append("")
    corrected = report.get("correctedKeyword") or report.get("didYouMean")
    if corrected:
        out.append(f'_Results are for "{corrected}" (the site corrected the keyword)._')
        out.append("")
    if crumbs and not (target.keyword and not target.nav_param):
        out.append(f"**Category:** {' > '.join(c['label'] for c in crumbs)}")

    store = _m(metadata.get("stores"))
    store_name = f"#{store.get('storeId')} {store.get('storeName')}" if store.get("storeId") else ""
    shown = f"showing {start + 1}–{start + len(products)}" if products else "none shown"
    summary = f"**{total:,} products** · {shown} · sorted by {_com_sort_label(report)}"
    if store_name:
        summary += f" · prices for store {store_name}"
    out.append(summary)
    if target.sort_by and target.sort_field is None:
        out.append(f"_`sortby={target.sort_by}` is not a sort homedepot.com offers; this is its default order._")
    applied = []
    for dim in _l(model.get("appliedDimensions")):
        labels = [r.get("label") for r in _l(dim.get("refinements")) if r.get("label")]
        if labels:
            applied.append(f"{dim.get('label')}: {', '.join(labels)}")
    if applied:
        out.append(f"**Filters applied:** {'; '.join(applied)}")
    out.append("")

    if not products:
        out.append("No products matched." if total == 0 else "No products on this page — it is past the last result.")
    for index, product in enumerate(products, start=start + 1):
        ident = _m(product.get("identifiers"))
        name = f"**{ident.get('brandName') or ''}** {ident.get('productLabel') or ''}".replace("**** ", "").strip()
        bits = [com_price_line(product.get("pricing"), product.get("info"))]
        ratings = _m(_m(product.get("reviews")).get("ratingsReviews"))
        avg, count = _num(ratings.get("averageRating")), _num(ratings.get("totalReviews"))
        if avg is not None and count:
            bits.append(f"★{avg:.1f} ({int(count):,})")
        if ident.get("modelNumber"):
            bits.append(f"Model {ident['modelNumber']}")
        badges = [b.get("label") for b in _l(product.get("badges")) if b.get("label")]
        if badges:
            bits.append(", ".join(badges))
        if _m(product.get("info")).get("isSponsored"):
            bits.append("Sponsored")
        if _m(product.get("availabilityType")).get("discontinued"):
            bits.append("discontinued")
        out.append(f"{index}. {name} — {' · '.join(b for b in bits if b)}")
        availability = _com_row_availability(product)
        link = urljoin(COM_ORIGIN, str(ident.get("canonicalUrl") or f"/p/{product.get('itemId')}"))
        out.append(f"   {availability + ' · ' if availability else ''}{link}")

    if products and start + len(products) < total:
        out += ["", f"Next page: {with_param(url, 'Nao', start + COM_PAGE_SIZE)}"]

    dims = [d for d in _l(model.get("dimensions")) if _l(d.get("refinements"))]
    if dims:
        out += ["", "## Refine"]
        for dim in dims:
            refinements = _l(dim.get("refinements"))
            shown_refs = [
                f"{r.get('label')} ({r.get('recordCount')}) {r.get('url')}" for r in refinements[:8] if r.get("label")
            ]
            more = f" … +{len(refinements) - 8} more" if len(refinements) > 8 else ""
            out.append(f"- **{dim.get('label')}:** " + "; ".join(shown_refs) + more)
        out.append(f"_Refinement links are relative to {COM_ORIGIN}._")
    return "\n".join(out).strip() + "\n"


_REVIEW_BADGES = {
    "verifiedPurchaser": "Verified purchaser",
    "earlyReviewerIncentive": "Early Reviewer Incentive",
    "incentivizedReview": "Incentivized review",
    "top250Contributor": "Top 250 contributor",
    "DIY": "DIY",
}


def render_com_reviews(product: dict | None, reviews: dict, target: ComTarget, url: str) -> str:
    ident = _m(_m(product).get("identifiers"))
    name = f"{ident.get('brandName') or ''} {ident.get('productLabel') or ''}".strip() or f"Internet # {target.item_id}"
    products = _m(_m(reviews.get("Includes")).get("Products"))
    family = [_m(products.get("store"))] + _l(products.get("items"))
    own = next((p for p in family if str(p.get("Id")) == str(target.item_id)), {})
    stats = _m(own.get("FilteredReviewStatistics"))
    total = int(_num(reviews.get("TotalResults")) or 0)
    results = _l(reviews.get("Results"))
    page = max(1, target.review_page)

    out = [f"# Reviews: {name}", ""]
    rating = _rating(stats.get("AverageOverallRating"), stats.get("TotalReviewCount"))
    if rating:
        recommend = []
        if stats.get("RecommendedCount") is not None:
            recommend.append(f"{int(stats['RecommendedCount']):,} recommend")
        if stats.get("NotRecommendedCount") is not None:
            recommend.append(f"{int(stats['NotRecommendedCount']):,} don't")
        out.append(f"**{rating}**" + (f" · {', '.join(recommend)}" if recommend else ""))
    distribution = sorted(_l(stats.get("RatingDistribution")), key=lambda d: -(_num(d.get("RatingValue")) or 0))
    if distribution:
        out.append(" | ".join(f"{int(_num(d.get('RatingValue')) or 0)}★ {int(_num(d.get('Count')) or 0):,}" for d in distribution))
    first = (page - 1) * 10 + 1
    if results:
        out.append(f"Showing reviews {first}–{first + len(results) - 1} of {total:,} (page {page})")
    siblings = sorted({str(r.get("ProductId")) for r in results if r.get("ProductId")} - {str(target.item_id)})
    if siblings:
        out.append(
            "_Home Depot pools reviews across this product's variants; reviews written about another "
            "variant are marked with its Internet #._"
        )
    out.append("")

    if not results:
        out.append("No reviews on this page." if total else "This product has no reviews.")
    for review in results:
        stars = int(_num(review.get("Rating")) or 0)
        title = _text(review.get("Title")) or "(no title)"
        out.append(f"### {'★' * stars}{'☆' * (5 - stars)} {title}")
        meta = [str(review.get("UserNickname") or "Anonymous"), _date(review.get("SubmissionTime"))]
        reviewed = str(review.get("ProductId") or "")
        if reviewed and reviewed != str(target.item_id):
            meta.append(f"review of variant Internet # {reviewed}")
        badges = [_REVIEW_BADGES.get(b, b) for b in review.get("BadgesOrder") or [] if isinstance(b, str)]
        meta += badges
        if review.get("IsRecommended") is True:
            meta.append("Recommends")
        elif review.get("IsRecommended") is False:
            meta.append("Does not recommend")
        out.append(" · ".join(m for m in meta if m))
        body = _text(review.get("ReviewText"))
        out.append(body or "_(rating only, no text)_")
        photos = len(_l(review.get("Photos")))
        helpful = (_num(review.get("TotalPositiveFeedbackCount")) or 0, _num(review.get("TotalNegativeFeedbackCount")) or 0)
        extras = []
        if any(helpful):
            extras.append(f"Helpful: {int(helpful[0])} yes / {int(helpful[1])} no")
        if photos:
            extras.append(f"{photos} photo{'s' if photos != 1 else ''}")
        if extras:
            out.append(" · ".join(extras))
        for response in _l(review.get("ClientResponses")):
            who = response.get("Department") or "Response"
            out.append(f"> **{who}** ({_date(response.get('Date'))}): {_text(response.get('Response'))}")
        out.append("")

    if results and first + len(results) - 1 < total:
        base = url.rstrip("/")
        parts = base.split("/")
        if parts[-1].isdigit() and parts[-1] != str(target.item_id):
            parts[-1] = str(page + 1)
        else:
            parts.append(str(page + 1))
        out.append(f"Next page: {'/'.join(parts)}")
    if ident.get("canonicalUrl"):
        out.append(f"Product page: {urljoin(COM_ORIGIN, ident['canonicalUrl'])}")
    return "\n".join(out).strip() + "\n"


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_STORE_SERVICES = {
    "toolRental": "Tool rental",
    "loadNGo": "Load 'N Go truck rental",
    "penske": "Penske truck rental",
    "hdMoving": "Moving supplies",
    "propane": "Propane exchange",
    "keyCutting": "Key cutting",
    "wiFi": "Wi-Fi",
    "applianceShowroom": "Appliance showroom",
    "kitchenShowroom": "Kitchen showroom",
    "expandedFlooringShowroom": "Expanded flooring showroom",
    "largeEquipment": "Large equipment rental",
}

_STORE_FLAGS = {
    "bopisFlag": "Buy online, pick up in store",
    "curbsidePickupFlag": "Curbside pickup",
    "bodfsFlag": "Delivery from store",
}


def render_com_store(store: dict, url: str, *, note: str | None = None) -> str:
    address = _m(store.get("address"))
    out = [f"# The Home Depot — {store.get('name')} (#{store.get('storeId')})", ""]
    if note:
        out += [f"_{note}_", ""]
    line = ", ".join(x for x in (address.get("street"), address.get("city")) if x)
    region = " ".join(x for x in (address.get("state"), address.get("postalCode")) if x)
    out.append(", ".join(x for x in (line, region) if x))
    phones = []
    for key, label in (("phone", "Phone"), ("proDeskPhone", "Pro Desk"), ("toolRentalPhone", "Tool Rental")):
        if store.get(key):
            phones.append(f"{label}: {store[key]}")
    if phones:
        out.append(" · ".join(phones))
    coords = _m(store.get("coordinates"))
    if coords.get("lat") is not None and coords.get("lng") is not None:
        out.append(f"Coordinates: {coords['lat']}, {coords['lng']}")

    hours = _m(store.get("storeHours"))
    if hours:
        out += ["", "## Store hours"]
        for day in _DAYS:
            slot = _m(hours.get(day))
            if slot.get("open") and slot.get("close"):
                out.append(f"- {day.title()}: {slot['open']}–{slot['close']}")
            elif day in hours:
                out.append(f"- {day.title()}: closed")
    services = _m(store.get("services"))
    if services:
        out += ["", "## Services"]
        for key, label in _STORE_SERVICES.items():
            if key in services:
                out.append(f"- {label}: {'✓' if services[key] else '—'}")
    flags = _m(store.get("flags"))
    if flags:
        out += ["", "## Pickup and delivery"]
        for key, label in _STORE_FLAGS.items():
            if key in flags:
                out.append(f"- {label}: {'✓' if flags[key] else '—'}")
    link = store.get("storeDetailsPageLink")
    if link:
        out += ["", f"Store page: {urljoin(COM_ORIGIN, str(link))}"]
    return "\n".join(out).strip() + "\n"


# ---------------------------------------------------------------------------
# homedepot.ca
# ---------------------------------------------------------------------------

_CA_STOCK = {
    "instock": "in stock online",
    "lowstock": "low stock online",
    "outofstock": "out of stock online",
    "backorder": "backordered",
}


def ca_stock(stock: object) -> str:
    status = str(_m(stock).get("stockLevelStatus") or "")
    return _CA_STOCK.get(status.lower(), status)


def ca_price_line(pricing: object, fallback_price: object = None) -> str:
    """A homedepot.ca price block (``displayPrice`` / ``wasprice`` / savings)."""
    pricing = _m(pricing)
    display = _m(pricing.get("displayPrice")) or _m(fallback_price)
    value = _num(display.get("value"))
    if value is None:
        # Not "unpriced": these rows (productStatus "OU") get no online price
        # from the listing or from the price service, and the page shows none.
        return "no online price"
    currency = str(display.get("currencyIso") or "CAD")
    text = _money(value, currency) or ""
    # Key off the code, not the word: the French site spells "each" "chaque".
    uom = str(display.get("unitOfMeasure") or "").strip()
    if uom and str(display.get("unitOfMeasureCode") or "").upper() != "EA":
        text += f" /{uom}"
    was = _num(_m(pricing.get("wasprice")).get("value"))
    if was is not None and was > value:
        detail = f"was {_money(was)}"
        saving = _money(_m(pricing.get("savingsAmount")).get("value"))
        if saving:
            detail += f", save {saving}"
        if pricing.get("percentSaving"):
            detail += f" ({pricing['percentSaving']})"
        text += f" ({detail})"
    return text


def _ca_specs(product: dict) -> list[str]:
    """``classifications`` flattened: the groups are all named "Categories"."""
    rows: list[str] = []
    seen: set[str] = set()
    for group in _l(product.get("classifications")):
        for feature in _l(group.get("features")):
            name = str(feature.get("name") or "").strip()
            values = [str(v.get("value")) for v in _l(feature.get("featureValues")) if v.get("value") not in (None, "")]
            if not name or name in seen:
                continue
            seen.add(name)
            value = ", ".join(values) if values else "-"
            unit = _m(feature.get("featureUnit")).get("symbol")
            # "Assembled Weight (in lbs)" already states its unit; appending the
            # unit's own symbol ("Pounds") would say it twice.
            if values and unit and "(" not in name and str(unit).lower() not in value.lower():
                value += f" {unit}"
            rows.append(f"- {name}: {_text(value) or value}")
    return rows


def ca_reviews_from_seo(markup: object, limit: int = 10) -> list[dict]:
    """The first page of reviews out of BazaarVoice's SEO block."""
    if not isinstance(markup, str) or "itemprop" not in markup:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")

    def value(el) -> str:
        if el is None:
            return ""
        return (el.get("content") or el.get_text(" ", strip=True) or "").strip()

    reviews = []
    for node in soup.select('[itemprop="review"]')[:limit]:
        # The review's own ``name`` (its title) is a direct child; the author's
        # ``name`` sits inside the Person. A descendant search returns the
        # author first and the title is lost.
        author = node.find(attrs={"itemprop": "author"})
        reviews.append(
            {
                "rating": value(node.select_one('[itemprop="ratingValue"]')),
                "title": value(node.find(attrs={"itemprop": "name"}, recursive=False)),
                "body": value(node.find(attrs={"itemprop": "reviewBody"}, recursive=False))
                or value(node.find(attrs={"itemprop": "description"}, recursive=False)),
                "author": value(author.find(attrs={"itemprop": "name"}) if author else None) or value(author),
                "date": value(node.find(attrs={"itemprop": "datePublished"})),
            }
        )
    return reviews


def render_ca_product(
    product: dict,
    url: str,
    *,
    localized: dict | None = None,
    promotions: list[dict] | None = None,
) -> str:
    price_unknown = localized is None
    localized = _m(localized)
    brand = str(product.get("manufacturer") or "").strip()
    name = _text(product.get("name"))
    out = [f"# {brand} {name}".strip(), ""]
    ids = [f"**Store SKU #** {product.get('code')}"]
    if product.get("modelNumber"):
        ids.append(f"**Model #** {product['modelNumber']}")
    out.append(" · ".join(ids))
    optimized = _m(localized.get("optimizedPrice"))
    catalogue = _money(_m(product.get("price")).get("value"), str(_m(product.get("price")).get("currencyIso") or "CAD"))
    if price_unknown:
        price = ca_price_line({}, product.get("price"))
        out.append(f"**Price:** {price} — online price (no store selected)")
        out.append(
            "_The price service did not answer, so this is the page's sell price alone; "
            "any sale or was-price is not shown._"
        )
    elif optimized.get("displayPrice"):
        out.append(f"**Price:** {ca_price_line(optimized)} — online price (no store selected)")
    else:
        # The visible price block is drawn from the price service; for some
        # items (productStatus "OU") it returns none and the page shows no
        # price, while the catalogue price still sits in the page's product
        # data and schema.org Offer. Quoting it bare would be a price the
        # site does not display.
        status = optimized.get("productStatus")
        line = "**Price:** no online price — homedepot.ca's price service returns none for this item"
        line += f" (status {status})" if status else ""
        if catalogue:
            line += f"; its product data lists {catalogue} as the catalogue price"
        out.append(line)
    for promo in promotions or []:
        message = _text(promo.get("pipMessage") or promo.get("accordionMessage") or promo.get("stripeMessage"))
        if message:
            out.append(f"**Promotion:** {message}")
    rating = _rating(product.get("averageRating"), product.get("numberOfReviews"))
    if rating:
        questions = product.get("numberOfQuestions")
        out.append(f"**Rating:** {rating}" + (f" · {questions} questions" if questions else ""))
    stock = ca_stock(localized.get("stock") or product.get("stock"))
    availability = [stock] if stock else []
    if product.get("deliveryTime"):
        availability.append(f"delivery {_text(product['deliveryTime'])} {product.get('deliveryUnit') or ''}".strip())
    if product.get("shipToStoreTime"):
        availability.append(f"ship to store {_text(product['shipToStoreTime'])} {product.get('shipToStoreUnit') or ''}".strip())
    if availability:
        out.append(f"**Availability:** {' · '.join(availability)}")
    # The trail ends with the product itself (titled with its code), which
    # is not a category; only crumbs that name a category id are kept.
    crumbs = [c.get("title") for c in _l(product.get("breadCrumbs")) if c.get("title") and c.get("categoryId")]
    if crumbs:
        out.append(f"**Category:** {' > '.join(crumbs)}")
    badges = [b.get("name") for b in _l(product.get("badges")) if b.get("name")]
    if badges:
        out.append(f"**Badges:** {', '.join(badges)}")

    variants = _l(product.get("variantOptionsSorted"))
    if variants:
        out += ["", "## Options"]
        for variant in variants:
            choices = []
            for choice in _l(variant.get("variantOptionQualifiers")):
                label = _text(choice.get("value"))
                if not label:
                    continue
                if choice.get("selected"):
                    choices.append(f"**{label}** (this item)")
                else:
                    unavailable = "" if choice.get("available", True) else " (unavailable)"
                    choices.append(f"{label}{unavailable} {urljoin(CA_ORIGIN, str(choice.get('url') or ''))}")
            if choices:
                out.append(f"- {variant.get('name')}: " + "; ".join(choices))

    description = _text(product.get("description"))
    features = [_text(v.get("value")) for f in _l(product.get("productFeatures")) for v in _l(f.get("featureValues"))]
    features = [f for f in features if f]
    if description or features:
        out += ["", "## Description"]
        if description:
            out.append(description)
        if features:
            out.append("")
            out.extend(f"- {f}" for f in features)

    specs = _ca_specs(product)
    extra = []
    if product.get("warranty"):
        extra.append(f"- Warranty: {_text(product['warranty'])}")
    if product.get("countryOfOrigin"):
        extra.append(f"- Country of origin: {product['countryOfOrigin']}")
    if specs or extra:
        out += ["", "## Specifications"]
        out.extend(specs + extra)

    documents = [d for d in _l(product.get("documents")) if d.get("URL") or d.get("url")]
    if documents:
        out += ["", "## Documents"]
        out.extend(f"- {d.get('name')}: {d.get('URL') or d.get('url')}" for d in documents)

    reviews = ca_reviews_from_seo(product.get("bvReviews"))
    if reviews:
        out += ["", f"## Reviews (first {len(reviews)})"]
        for review in reviews:
            stars = int(_num(review["rating"]) or 0)
            out.append(f"### {'★' * stars}{'☆' * (5 - stars)} {review['title'] or '(no title)'}")
            meta = " · ".join(x for x in (review["author"], _date(review["date"])) if x)
            if meta:
                out.append(meta)
            out.append(review["body"] or "_(rating only, no text)_")
            out.append("")

    images = [_m(i).get("url") for i in _l(product.get("images")) + _l(product.get("alternateImages"))]
    images = [i for i in dict.fromkeys(images) if i]
    if images:
        out += ["", "## Images"]
        out.extend(f"- {i}" for i in images[:8])

    urls = _m(product.get("urls"))
    out.append("")
    out.append(f"Product page: {product.get('url') or url}")
    other = urls.get("fr") if "/product/" in url else urls.get("en")
    if other:
        out.append(f"{'Français' if '/product/' in url else 'English'}: {other}")
    out.append("")
    out.append(
        "_Read from homedepot.ca's page data. Price and stock are for its online store; "
        "in-store price and stock vary by store and are not shown._"
    )
    return "\n".join(out).strip() + "\n"


def _ca_row(product: dict, index: int) -> list[str]:
    name = f"**{product.get('brand') or ''}** {_text(product.get('name'))}".replace("**** ", "").strip()
    bits = [ca_price_line(product.get("pricing"))]
    rating = _m(product.get("productRating"))
    avg, count = _num(rating.get("averageRating")), _num(rating.get("totalReviews"))
    if avg is not None and count:
        bits.append(f"★{avg:.1f} ({int(count):,})")
    if product.get("modelNumber"):
        bits.append(f"Model {product['modelNumber']}")
    badges = [b.get("name") for b in _l(product.get("badges")) if b.get("name")]
    if badges:
        bits.append(", ".join(badges))
    stock = ca_stock(product.get("stock"))
    if stock:
        bits.append(stock)
    if product.get("hasMoreOptions"):
        bits.append("more options")
    link = urljoin(CA_ORIGIN, str(product.get("url") or f"/product/{product.get('code')}"))
    return [f"{index}. {name} — {' · '.join(b for b in bits if b)}", f"   SKU {product.get('code')} · {link}"]


def render_ca_listing(
    plp: dict,
    url: str,
    *,
    title: str,
    is_search: bool,
    note: str | None = None,
) -> str:
    report = _m(plp.get("searchReport"))
    products = _l(plp.get("products"))
    total = int(_num(report.get("totalProducts")) or 0)
    page_size = int(_num(report.get("pageSize")) or 40) or 40
    page = max(1, int(_num(report.get("startIndex")) or 1))
    pages = max(1, math.ceil(total / page_size)) if total else 1

    out = [f"# {title}", ""]
    if note:
        out += [f"_{note}_", ""]
    sorts = _l(plp.get("sorts"))
    sort_label = next((s.get("name") for s in sorts if s.get("selected")), report.get("sortBy") or "default")
    out.append(
        f"**{total:,} products** · page {page} of {pages} ({page_size} per page) · sorted by {sort_label} "
        "· online prices"
    )
    selected = [
        f"{facet.get('name')}: {value.get('name')}"
        for facet in _l(plp.get("facets"))
        for value in _l(facet.get("facetValues"))
        if value.get("selected")
    ]
    if selected:
        out.append(f"**Filters applied:** {'; '.join(selected)}")
    out.append("")

    first = (page - 1) * page_size + 1
    if not products:
        out.append("No products matched." if total == 0 else "No products on this page — it is past the last result.")
    for offset, product in enumerate(products):
        out.extend(_ca_row(product, first + offset))
    if products and page < pages:
        out += ["", f"Next page: {with_param(url, 'page', page + 1)}"]
    other_sorts = [f"{s.get('name')} (`sort={s.get('code')}`)" for s in sorts if not s.get("selected")]
    if other_sorts:
        out.append(f"Other sorts: {', '.join(other_sorts)}")

    facets = [f for f in _l(plp.get("facets")) if any(not v.get("selected") for v in _l(f.get("facetValues")))]
    if facets:
        out += ["", "## Refine"]
        for facet in facets:
            values = [v for v in _l(facet.get("facetValues")) if not v.get("selected") and v.get("name")]
            listed = [f"{_text(v.get('name'))} ({v.get('count')}) [{v.get('filterKey')}]" for v in values[:10]]
            more = f" … +{len(values) - 10} more" if len(values) > 10 else ""
            out.append(f"- **{facet.get('name')}:** " + "; ".join(listed) + more)
        if is_search:
            out.append("_To refine, add `&filter=<key>` to the search URL._")
        else:
            out.append("_To refine, append `/f/<key>` to the category's `.html` path; keys already include the filters applied._")
    return "\n".join(out).strip() + "\n"
