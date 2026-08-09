"""Render Ubiquiti pages as compact markdown.

The specification tree is the reason this module exists, so it is worth being
precise about its shape. Each section holds a *flat* list of features, and three
``__typename`` variants share that list:

``...FeatureGroup``
    A heading with no value of its own ("MIMO"). The rows underneath it are not
    nested in the JSON — they are later entries whose ``feature.parentId``
    points back at the group, so nesting has to be rebuilt from that id.

``...FeatureEntryText``
    A labelled value. Multi-line values are common and meaningful: "Operating
    Frequency" carries a whole regional band plan in one string, so newlines are
    kept as indented continuation lines rather than flattened into one run-on.

``...FeatureEntryFlag``
    A capability that is present (``"True"``) or absent (``"Empty"``). Absent
    flags are rendered as ``—`` rather than dropped, because "this model has no
    6 GHz radio" and "nobody said" are different answers and silence reads as
    the second.

Sections of type ``Overview`` are the compare-grid badges. On a product page
they duplicate the visible Overview section and are skipped; on a category page
they are the *only* specs present, and they are exactly the badges the cards
show ("WiFi 7", "6 GHz Radio"), so there they are rendered.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

_FLAG_PRESENT = "True"


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# Minor units follow the currency's ISO 4217 exponent, not a flat 1/100. The JP
# store prices the U7 Pro Wall at `{"amount": 36391, "currency": "JPY"}` and
# displays ¥36,391 — dividing by 100 there reports ¥363.91, a silent 100x error
# on every product in that store. Only the exceptions to two decimals are
# listed; everything else is 2.
_CURRENCY_EXPONENTS = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0,
    "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}


def _money(value: object) -> str | None:
    """Format one of the store's minor-unit money objects."""
    money = _mapping(value)
    amount = money.get("amount")
    currency = money.get("currency")
    if not isinstance(amount, int | float) or not currency:
        return None
    code = str(currency).upper()
    exponent = _CURRENCY_EXPONENTS.get(code, 2)
    return f"{amount / (10**exponent):,.{exponent}f} {code}"


def _html_lines(html: str) -> list[str]:
    """Flatten the store's small HTML fragments into plain lines."""
    if not html or not html.strip():
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.find_all(["p", "li"]) or [soup]
    lines = []
    for block in blocks:
        text = re.sub(r"[\s\xa0]+", " ", block.get_text(" ", strip=True)).strip()
        if text:
            lines.append(text)
    return lines


# --------------------------------------------------------------------------
# Specifications
# --------------------------------------------------------------------------


def _entry_value(entry: dict) -> str | None:
    """The rendered value of one feature row, or None when it carries none."""
    typename = str(entry.get("__typename") or "")
    if typename.endswith("FeatureGroup"):
        return None
    if "flag" in entry:
        return "✓" if entry.get("flag") == _FLAG_PRESENT else "—"
    value = entry.get("value")
    return str(value) if value not in (None, "") else None


def _row(label: str, value: str, note: str, indent: str) -> list[str]:
    """One spec row, keeping a multi-line value readable."""
    suffix = f" ({note})" if note else ""
    if "\n" not in value:
        return [f"{indent}- {label}: {value}{suffix}"]
    lines = [f"{indent}- {label}:{suffix}"]
    lines.extend(f"{indent}  {part.strip()}" for part in value.split("\n") if part.strip())
    return lines


def spec_sections(spec: object, *, include_overview: bool = False) -> list[str]:
    """Render a ``technicalSpecification`` as markdown sections."""
    sections = _mapping(spec).get("sections") or []
    out: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        meta = _mapping(section.get("section"))
        if (meta.get("type") == "Overview") != include_overview:
            continue
        rows: list[str] = []
        group_ids: set[str] = set()
        for entry in section.get("features") or []:
            if not isinstance(entry, dict):
                continue
            feature = _mapping(entry.get("feature"))
            label = str(feature.get("label") or "").strip()
            if not label:
                continue
            note = str(entry.get("note") or feature.get("note") or "").strip()
            if str(entry.get("__typename") or "").endswith("FeatureGroup"):
                identifier = feature.get("id")
                if identifier:
                    group_ids.add(str(identifier))
                rows.append(f"- **{label}**{f' ({note})' if note else ''}")
                continue
            value = _entry_value(entry)
            if value is None:
                continue
            indent = "  " if str(feature.get("parentId") or "") in group_ids else ""
            rows.extend(_row(label, value, note, indent))
        if rows:
            out.append(f"### {meta.get('label') or 'Specifications'}")
            out.extend(rows)
            out.append("")
    return out


def spec_badges(spec: object) -> list[str]:
    """The compare-grid badges a category card shows ("WiFi 7", "6 GHz Radio")."""
    badges: list[str] = []
    for section in _mapping(spec).get("sections") or []:
        if not isinstance(section, dict):
            continue
        if _mapping(section.get("section")).get("type") != "Overview":
            continue
        for entry in section.get("features") or []:
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if value:
                badges.append(str(value))
            elif entry.get("flag") == _FLAG_PRESENT:
                label = _mapping(entry.get("feature")).get("label")
                if label:
                    badges.append(str(label))
    return badges


# --------------------------------------------------------------------------
# Availability and price
# --------------------------------------------------------------------------


def availability(product: dict) -> str:
    """The store's own availability wording, plus any restock date."""
    variants = [v for v in product.get("variants") or [] if isinstance(v, dict)]
    statuses: list[str] = []
    for variant in variants:
        status = str(variant.get("status") or "").strip()
        if not status:
            continue
        sku = variant.get("sku")
        text = status
        sold_out = variant.get("soldOutAt")
        if sold_out:
            text += f", since {str(sold_out)[:10]}"
        restock = variant.get("restockEtaAt")
        if restock:
            text += f", restock {str(restock)[:10]}"
        elif status.lower() not in {"available", "instock"}:
            text += ", no restock date given"
        statuses.append(f"{sku}: {text}" if len(variants) > 1 and sku else text)
    if statuses:
        return "; ".join(dict.fromkeys(statuses))
    return str(product.get("status") or "unknown")


def price_line(product: dict) -> str:
    """Price as the store charges it, naming the surcharge when there is one.

    ``minDisplayPriceWithSurcharges`` is what the page displays; the base price
    is only mentioned when the two differ, so the number quoted is always the
    number a buyer pays.
    """
    charged = _money(product.get("minDisplayPriceWithSurcharges")) or _money(product.get("minDisplayPrice"))
    if not charged:
        return "Price not published"

    # `minDisplayPrice` is the *minimum* across variants. On a product sold in
    # several configurations (USW Flex Mini: 39/119/189) printing it bare reads
    # as the price rather than the cheapest one, so say which it is.
    variant_prices = {
        _money(v.get("displayPriceWithSurcharges")) or _money(v.get("displayPrice"))
        for v in product.get("variants") or []
        if isinstance(v, dict)
    }
    variant_prices.discard(None)
    lead = f"from {charged}" if len(variant_prices) > 1 else charged

    parts = [lead]
    base = _money(product.get("minDisplayPrice"))
    if base and base != charged:
        surcharges = [str(s) for s in product.get("productSurchargeTypes") or [] if s]
        label = f"{'/'.join(surcharges)} surcharge" if surcharges else "surcharge"
        parts.append(f"base {base} + {label}")
    regular = _money(product.get("minDisplayRegularPrice"))
    if regular and regular != charged:
        parts.append(f"was {regular}")
    return parts[0] + (f" ({'; '.join(parts[1:])})" if len(parts) > 1 else "")


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------


def _documents(product: dict, base_url: str) -> list[str]:
    lines: list[str] = []
    for doc in product.get("documents") or []:
        if not isinstance(doc, dict):
            continue
        url = doc.get("url")
        if not url:
            continue
        kind = re.sub(r"(?<!^)(?=[A-Z])", " ", str(doc.get("type") or "Document"))
        lines.append(f"- {kind}: {urljoin(base_url, str(url))}")
    datasheet = product.get("datasheet")
    if isinstance(datasheet, dict) and datasheet.get("url"):
        lines.append(f"- Datasheet: {datasheet['url']}")
    elif isinstance(datasheet, str) and datasheet:
        lines.append(f"- Datasheet: {datasheet}")
    return lines


def _accessories(props: dict) -> list[str]:
    additions = _mapping(props.get("inlineAdditions"))
    lines: list[str] = []
    for item in additions.get("inlineRelatedProducts") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("shortTitle") or item.get("name") or item.get("title")
        price = _money(item.get("minDisplayPriceWithSurcharges")) or _money(item.get("minDisplayPrice"))
        if name:
            lines.append(f"- {name}" + (f" — {price}" if price else ""))
    return lines


def _ui_care(props: dict) -> list[str]:
    services = _mapping(_mapping(props.get("inlineAdditions")).get("serviceProducts"))
    lines: list[str] = []
    for offer in services.get("uiCare") or []:
        product = _mapping(_mapping(offer).get("upsellProduct"))
        price = _money(product.get("minDisplayPriceWithSurcharges")) or _money(product.get("minDisplayPrice"))
        name = product.get("shortTitle") or product.get("title") or "UI Care"
        if price:
            lines.append(f"- {name}: {price}")
    return lines


def render_product(product: dict, props: dict, url: str) -> str:
    """A store product page: price, availability, and the full spec tree."""
    title = product.get("title") or product.get("name") or "Ubiquiti product"
    lines = [f"# {title}", ""]

    identity = []
    sku = product.get("displaySku") or product.get("name")
    if sku:
        identity.append(f"**SKU:** {sku}")
    family = product.get("family")
    if family:
        identity.append(f"**Family:** {family}")
    store = props.get("storeId")
    if store:
        identity.append(f"**Store:** {str(store).upper()}")
    if identity:
        lines.append(" | ".join(identity))

    lines.append(f"**Price:** {price_line(product)}")
    lines.append(f"**Availability:** {availability(product)}")

    # `description` is an HTML fragment; `shortDescription` is already plain.
    description = _html_lines(str(product.get("description") or "")) or [
        str(product.get("shortDescription") or "").strip()
    ]
    description = [d for d in description if d]
    if description:
        lines.extend(["", *description])

    features = _html_lines(str(product.get("keyFeatures") or ""))
    if features:
        lines.extend(["", "## Key features", *[f"- {f}" for f in features]])

    notes: list[str] = []
    for note in product.get("notes") or []:
        raw = note.get("text") if isinstance(note, dict) else note
        if isinstance(raw, str):
            notes.extend(_html_lines(raw) if "<" in raw else [raw.strip()])
    notes = [n for n in notes if n]
    if notes:
        lines.extend(["", "## Notes", *[f"- {n}" for n in notes]])

    specs = spec_sections(product.get("technicalSpecification"))
    if specs:
        lines.extend(["", "## Technical specifications", *specs])
    else:
        badges = spec_badges(product.get("technicalSpecification"))
        if badges:
            lines.extend(["", "## Technical specifications", f"Highlights only: {', '.join(badges)}", ""])
        else:
            lines.extend(["", "## Technical specifications", "This page published none.", ""])

    variants = [v for v in product.get("variants") or [] if isinstance(v, dict)]
    if len(variants) > 1:
        lines.append("## Variants")
        for variant in variants:
            price = _money(variant.get("displayPriceWithSurcharges")) or _money(variant.get("displayPrice"))
            lines.append(
                f"- {variant.get('sku') or variant.get('slug')}"
                + (f" — {price}" if price else "")
                + f" ({variant.get('status') or 'unknown'})"
            )
        lines.append("")

    accessories = _accessories(props)
    if accessories:
        lines.extend(["## Accessories", *accessories, ""])

    care = _ui_care(props)
    if care:
        lines.extend(["## UI Care", *care, ""])

    documents = _documents(product, url)
    if documents:
        lines.extend(["## Documents", *documents, ""])

    box = _mapping(product.get("whatsInTheBoxMedia"))
    box_items = [i for i in box.get("items") or [] if isinstance(i, dict)]
    if box_items:
        image = _mapping(box_items[0].get("data")).get("url")
        if image:
            lines.extend(
                [
                    "## In the box",
                    f"Published only as an illustration, not a list: {image}",
                    "",
                ]
            )

    stores = [str(_mapping(s).get("storeId")).upper() for s in product.get("alternativeStores") or [] if _mapping(s).get("storeId")]
    if stores:
        lines.append(f"Also sold in: {', '.join(dict.fromkeys(stores))}")

    lines.append(f"Source: {url}")
    return "\n".join(lines)


def render_techspecs_product(product: dict, url: str) -> str:
    """techspecs.ui.com — the same spec tree with no storefront around it."""
    title = product.get("title") or product.get("name") or "Ubiquiti product"
    lines = [f"# {title}", ""]

    identity = []
    sku = product.get("shortTitle") or product.get("name")
    if sku:
        identity.append(f"**Model:** {sku}")
    variants = [v for v in product.get("variants") or [] if isinstance(v, dict)]
    skus = [str(v.get("sku")) for v in variants if v.get("sku")]
    if skus:
        identity.append(f"**SKU:** {', '.join(dict.fromkeys(skus))}")
    if identity:
        lines.append(" | ".join(identity))

    description = product.get("shortDescription")
    if description:
        lines.extend(["", str(description).strip()])

    specs = spec_sections(product.get("technicalSpecification"))
    if specs:
        lines.extend(["", "## Technical specifications", *specs])
    else:
        lines.extend(["", "This page published no specifications.", ""])

    documents = _documents(product, url)
    if documents:
        lines.extend(["## Documents", *documents, ""])

    lines.append(f"Source: {url}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


def _category_row(product: dict, base_url: str, *, priced: bool, link_prefix: str = "products/") -> str:
    name = product.get("shortTitle") or product.get("name") or product.get("title") or "Product"
    sku = product.get("displaySku") or product.get("name")
    head = f"**{name}**" + (f" ({sku})" if sku and sku != name else "")

    meta: list[str] = []
    if priced:
        meta.append(price_line(product))
        meta.append(availability(product))
    badges = spec_badges(product.get("technicalSpecification"))
    if badges:
        meta.append(", ".join(badges))

    lines = [f"- {head}" + (f" — {' | '.join(meta)}" if meta else "")]
    description = product.get("shortDescription") or product.get("description")
    if description:
        lines.append(f"  {str(description).strip()}")
    slug = product.get("slug")
    if slug:
        # The store nests products under `<category>/products/<slug>` and
        # resolves them under whichever category path they are reached through;
        # techspecs puts the slug directly under the category and 404s on the
        # store's shape. Hence the prefix rather than one hardcoded form.
        lines.append(f"  {urljoin(base_url, f'{link_prefix}{slug}')}")
    return "\n".join(lines)


# Title-casing a slug mangles Ubiquiti's own product vocabulary ("Wifi", "Poe").
_LABEL_FIXUPS = {
    "Wifi": "WiFi",
    "Poe": "PoE",
    "Unifi": "UniFi",
    "Uisp": "UISP",
    "Lte": "LTE",
    "Ac": "AC",
    "Ai": "AI",
    "G2": "G2",
}


def _readable_group(identifier: str) -> str:
    if not identifier:
        return "Products"
    words = [word.title() for word in identifier.split("-") if word]
    return " ".join(_LABEL_FIXUPS.get(word, word) for word in words)


def render_category(groups: list[tuple[str, list[dict]]], props: dict, url: str) -> str:
    """A store category page, listing the group the URL named.

    Sibling groups arrive in the same payload because the store switches between
    them without navigating. They are named with their counts rather than
    expanded, so the listing answers the URL that was asked for and still says
    what else is one click away.
    """
    wanted = str(_mapping(props.get("queryParams")).get("category") or "")
    base = url if url.endswith("/") else url + "/"

    selected = [(gid, items) for gid, items in groups if gid == wanted]
    showing_all = not selected
    shown = groups if showing_all else selected

    heading = _readable_group(wanted) if wanted else "Ubiquiti Store"
    total = sum(len(items) for _gid, items in shown)
    lines = [f"# Ubiquiti Store — {heading}", "", f"{total} product{'s' if total != 1 else ''}", ""]

    for gid, items in shown:
        if len(shown) > 1:
            lines.append(f"## {_readable_group(gid)} ({len(items)})")
        for product in items:
            lines.append(_category_row(product, base, priced=True))
        lines.append("")

    if not showing_all:
        siblings = [(gid, items) for gid, items in groups if gid != wanted]
        if siblings:
            lines.append("Sibling categories in this payload:")
            for gid, items in siblings:
                lines.append(f"- {_readable_group(gid)} ({len(items)}) — {urljoin(base, '../' + gid)}")
            lines.append("")

    lines.append(f"Source: {url}")
    return "\n".join(lines)


def render_techspecs_category(groups: list[tuple[str, list[dict]]], url: str) -> str:
    """A techspecs listing: models and their badges, no prices anywhere."""
    path = [s for s in urlparse(url).path.split("/") if s]
    heading = " / ".join(_readable_group(s) for s in path) or "All products"
    total = sum(len(items) for _label, items in groups)
    lines = [f"# Ubiquiti Tech Specs — {heading}", "", f"{total} product{'s' if total != 1 else ''}", ""]
    base = url if url.endswith("/") else url + "/"
    for label, items in groups:
        lines.append(f"## {_readable_group(label)} ({len(items)})")
        for product in items:
            # techspecs hangs a product straight off its category, with no
            # `products/` segment — that form 404s here.
            lines.append(_category_row(product, base, priced=False, link_prefix=""))
        lines.append("")
    lines.append(f"Source: {url}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Installation guides
# --------------------------------------------------------------------------


def render_guide(data: dict, *, manual_hint: bool = True) -> str:
    """An installation guide: what it is, what it links to, how to get it.

    The guide's wording is vector artwork, so there is no text to return. Saying
    so plainly — and pointing at the tool that reproduces the pages — is more
    use than a page of headings that implies the guide was read.
    """
    title = data.get("title") or "Ubiquiti installation guide"
    lines = [f"# {title}", ""]

    description = data.get("description")
    if description:
        lines.extend([str(description), ""])

    pages = data.get("pages") or []
    readable = [t for page in pages for t in page.get("texts") or []]
    unavailable = [p for p in pages if not p.get("svg")]

    lines.append(
        f"{len(pages)} page{'s' if len(pages) != 1 else ''}: "
        + ", ".join(str(p.get("id")) for p in pages)
    )
    dropped = data.get("dropped_pages") or 0
    if dropped:
        lines.append(f"{dropped} further page(s) exist but were not read.")
    if unavailable:
        detail = ", ".join(str(p["id"]) for p in unavailable)
        lines.append(f"{len(unavailable)} page(s) could not be reconstructed: {detail}")
    lines.append("")

    if readable:
        lines.extend(["## Text in the guide", *[f"- {t}" for t in readable], ""])
    else:
        lines.extend(
            [
                "Ubiquiti draws every word of this guide as vector outlines, so it "
                "carries no machine-readable text and none can be extracted. The "
                "pages themselves are reproduced faithfully — see below.",
                "",
            ]
        )

    links = data.get("links") or []
    if links:
        lines.extend(["## Links in the guide", *[f"- {link}" for link in links], ""])

    if manual_hint:
        lines.extend(
            [
                "To read the guide, download it with the `get_unifi_manual` tool, "
                "which rebuilds these pages as a PDF or as one PNG per page.",
                "",
            ]
        )

    lines.append(f"Source: {data.get('source') or ''}")
    return "\n".join(lines)
