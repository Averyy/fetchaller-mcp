"""Render an aartech.ca listing as compact markdown.

The point of this module is the price. aartech's listing pages ship no prices at
all in their HTML, so anything read from the markup is a product list with the
one field a shopper came for missing. Everything below is rendered from the
search API's ``price``/``quantityBreaks``/``stock`` fields instead.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import wafer

from . import api


def _mapping(value: object) -> dict:
    """A dict, or an empty one. Guards against a field arriving as a scalar."""
    return value if isinstance(value, dict) else {}


def _price_line(item: dict) -> str:
    """Price for one row: sale, list, volume breaks, or an explicit absence."""
    if item.get("is_quote_only"):
        return "Price on request"
    if not item.get("isPriceVisible", True):
        return "Price not shown"

    price = _mapping(item.get("price"))
    amount = price.get("amount")
    amount_text = price.get("amountText")
    list_price = price.get("listPrice")
    list_text = price.get("listPriceText")

    if amount_text is None:
        return "Price unavailable"
    # Only call it a sale when the site itself prices it below list.
    if list_price is not None and amount is not None and list_price > amount:
        return f"~~{list_text}~~ **{amount_text}**"
    return str(amount_text)


def _volume_line(item: dict) -> str | None:
    """Quantity-break pricing, flattened across units of measure."""
    breaks = _mapping(item.get("quantityBreaks"))
    rendered: list[str] = []
    for uom, tiers in breaks.items():
        if not isinstance(tiers, list):
            continue
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            quantity = tier.get("quantityBreak")
            formatted = tier.get("formattedPrice")
            if quantity is None or not formatted:
                continue
            label = f"{quantity}+ {formatted}"
            # Only name the unit when the item is sold in more than one.
            if len(breaks) > 1:
                label += f" ({uom})"
            rendered.append(label)
    return "Volume: " + ", ".join(rendered) if rendered else None


def _stock_text(item: dict) -> str | None:
    """The site's own availability wording ("In Stock", "Out of Stock", ...)."""
    display = _mapping(_mapping(item.get("stock")).get("display"))
    message = display.get("message")
    return str(message) if message else None


def _format_item(position: int, item: dict) -> str:
    title = item.get("shortDescription") or item.get("partNumber") or "Untitled"
    lines = [f"{position}. **{title}**"]

    meta: list[str] = []
    part_number = item.get("partNumber")
    if part_number:
        meta.append(str(part_number))
    manufacturer = _mapping(item.get("manufacturer")).get("title")
    if manufacturer:
        meta.append(str(manufacturer))
    meta.append(_price_line(item))
    stock = _stock_text(item)
    if stock:
        meta.append(stock)
    lines.append(f"   {' | '.join(meta)}")

    volume = _volume_line(item)
    if volume:
        lines.append(f"   {volume}")

    url = item.get("url")
    if url:
        lines.append(f"   {url}")
    return "\n".join(lines)


def render_listing(payload: dict, url: str) -> str:
    """Format an API payload as numbered markdown.

    The header always states the shown count against the listing's own total, so
    a bounded page is never mistaken for the whole catalogue.
    """
    items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
    details = _mapping(payload.get("resultDetails"))
    title = details.get("title")

    # The window this page represents. Without the offset, page 3 of 3 would
    # report the rows *behind* it as still to come.
    page, limit = api.resolve_window(urlparse(url).query)
    offset = (page - 1) * limit

    header = ["Aartech Canada"]
    if title:
        header.append(str(title))

    total = payload.get("totalHits")
    seen = offset + len(items)
    if isinstance(total, int) and total > len(items):
        header.append(f"showing {offset + 1}-{seen} of {total:,}" if offset else f"showing {len(items)} of {total:,}")
    else:
        header.append(f"{len(items)} product{'s' if len(items) != 1 else ''}")

    text = " | ".join(header)
    if not items:
        return f"{text}\n\nNo products found.\n\nSource: {url}"

    body = "\n\n".join(_format_item(i + 1, item) for i, item in enumerate(items))
    footer = f"Source: {url}"
    if isinstance(total, int) and total > seen:
        remaining = total - seen
        # `?` when the URL carries no query yet, else `&` — otherwise the hint
        # tells the caller to build a URL the site cannot parse.
        joiner = "&" if urlparse(url).query else "?"
        footer = (
            f"{remaining:,} further product{'s' if remaining != 1 else ''} not shown; "
            f"add {joiner}page={page + 1} to this URL to reach them.\n{footer}"
        )
    return f"{text}\n\n{body}\n\n{footer}"


# Custom fields that are noise on every product: zero-valued internal counters,
# and copies of text already rendered from a dedicated field.
_SKIPPED_SPEC_VALUES = {"", "0.00", "0", "0.0", "none", "n/a"}
_SKIPPED_SPEC_NAMES = {
    "product html description",
    "product short description",
    "product long description",
    "upc",
}


def _specs(blob: dict) -> list[str]:
    """Named specifications worth showing, from the product's custom fields."""
    rendered: list[str] = []
    for field in blob.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("display_name") or field.get("name") or "").strip()
        value = str(field.get("value") or "").strip()
        if not name or name.lower() in _SKIPPED_SPEC_NAMES:
            continue
        if value.lower() in _SKIPPED_SPEC_VALUES or "<" in value:
            continue
        rendered.append(f"{name}: {value}")
    return rendered


def _product_price_lines(blob: dict) -> list[str]:
    """Price, per unit of measure, plus any volume breaks."""
    if blob.get("is_quote_only"):
        return ["**Price on request**"]
    if not blob.get("isPriceVisible", True):
        return ["**Price not shown**"]

    lines: list[str] = []
    units = [u for u in blob.get("uoms") or [] if isinstance(u, dict)]
    for unit in units:
        text = unit.get("text")
        if not text:
            continue
        label = f"**{text}**"
        list_text = unit.get("listPrice")
        # listPrice here is the pre-discount figure, formatted like `text`.
        if list_text and list_text != text:
            label = f"~~{list_text}~~ {label}"
        code = unit.get("code")
        pack = unit.get("packSize")
        suffix = []
        if code and len(units) > 1:
            suffix.append(str(code))
        if isinstance(pack, int) and pack > 1:
            suffix.append(f"pack of {pack}")
        if suffix:
            label += f" ({', '.join(suffix)})"
        lines.append(label)

    if not lines:
        price = blob.get("price")
        lines.append(f"**${price} CAD**" if price else "Price unavailable")

    breaks = blob.get("quantity_breaks") or []
    tiers = [
        f"{t.get('quantityBreak')}+ {t.get('formattedPrice')}"
        for t in breaks
        if isinstance(t, dict) and t.get("quantityBreak") and t.get("formattedPrice")
    ]
    if tiers:
        lines.append("Volume: " + ", ".join(tiers))
    return lines


def render_product(blob: dict, url: str) -> str:
    """Format a product page, price first."""
    title = blob.get("title") or blob.get("short_description") or blob.get("part_number") or "Product"
    lines = [f"# {title}", ""]

    lines.extend(_product_price_lines(blob))

    stock = _mapping(_mapping(blob.get("stock")).get("display")).get("message")
    if stock:
        lines.append(f"Availability: {stock}")

    facts: list[str] = []
    part_number = blob.get("part_number")
    if part_number:
        facts.append(f"Part #: {part_number}")
    manufacturer = _mapping(blob.get("manufacturer")).get("name")
    if manufacturer:
        facts.append(f"Brand: {manufacturer}")
    upc = blob.get("upc")
    if isinstance(upc, list) and upc:
        facts.append(f"UPC: {upc[0]}")
    category = _mapping(blob.get("default_category")).get("name")
    if category:
        facts.append(f"Category: {category}")
    minimum = blob.get("minimumOrderQty")
    if isinstance(minimum, int) and minimum > 1:
        facts.append(f"Minimum order: {minimum}")
    if facts:
        lines.extend(["", *facts])

    description = _plain_text(blob.get("HTML_description") or "") or (blob.get("long_description") or "").strip()
    if description:
        lines.extend(["", description])

    specs = _specs(blob)
    if specs:
        lines.extend(["", "## Specifications", *[f"- {s}" for s in specs]])

    lines.extend(["", f"Source: {blob.get('url') or url}"])
    return "\n".join(lines)


def _plain_text(html: str) -> str:
    """Flatten a short HTML description to text.

    These descriptions are full of ``&nbsp;``, which unescapes to U+00A0. Left
    in, it reads as a space but does not match ``\\s``, so it silently breaks
    any downstream matching on the rendered text. Fold it here, at the seam.
    """
    if not html.strip():
        return ""
    from bs4 import BeautifulSoup

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return re.sub(r"[\s\xa0]+", " ", text).strip()


async def search_aartech(url: str, *, timeout: float = 30.0) -> dict | None:
    """Fetch and render one aartech.ca listing or product page.

    Returns ``{"content": ...}``, ``{"error": ...}``, or None when the URL is
    neither and belongs on the ordinary HTML path.
    """
    try:
        found = await api.fetch_catalogue_page(url, timeout=timeout)
    except LookupError as exc:
        return {"error": f"Aartech page could not be read: {exc}"}
    except (TimeoutError, OSError, ValueError, wafer.WaferError) as exc:
        # A transport failure has to come back as an error dict; letting it
        # escape crashes the whole tool call rather than reporting the URL.
        return {"error": f"Aartech request failed: {type(exc).__name__}: {exc}"}
    if found is None:
        return None
    kind, data = found
    if kind == "product":
        return {"content": render_product(data, url)}
    return {"content": render_listing(data, url)}
