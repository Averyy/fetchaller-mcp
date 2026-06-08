"""Markdown renderers for realtor.ca search results and listing detail."""

from __future__ import annotations

from .api import MAX_API_RECORDS, SITE

# Detail-page fields worth surfacing first (the rest follow in document order).
_KEY_DETAIL_ORDER = [
    "Property Type", "Building Type", "Title", "Land Size", "Square Footage",
    "Annual Property Taxes", "Age Of Building", "Community Name",
]


def _photo_url(res: dict) -> str:
    photos = (res.get("Property") or {}).get("Photo") or []
    if photos:
        return photos[0].get("HighResPath") or photos[0].get("MedResPath") or ""
    return ""


def _result_summary(idx: int, res: dict) -> str:
    prop = res.get("Property") or {}
    building = res.get("Building") or {}
    addr = (prop.get("Address") or {}).get("AddressText", "").replace("|", ", ")
    price = prop.get("Price", "")

    facts: list[str] = []
    beds = building.get("Bedrooms")
    if beds:
        facts.append(f"{beds} bed")
    baths = building.get("BathroomTotal")
    if baths:
        facts.append(f"{baths} bath")
    btype = building.get("Type") or prop.get("Type")
    if btype:
        facts.append(btype)
    size = building.get("SizeInterior")
    if size:
        facts.append(size)
    land = (res.get("Land") or {}).get("SizeTotal")
    if land:
        facts.append(f"lot {land}")

    parking = prop.get("ParkingType")
    posted = res.get("TimeOnRealtor")

    # Agent / brokerage
    agent = ""
    individuals = res.get("Individual") or []
    if individuals:
        org = (individuals[0].get("Organization") or {}).get("Name", "")
        name = individuals[0].get("Name", "")
        agent = " / ".join(p for p in (name, org) if p)

    rel = res.get("RelativeDetailsURL") or ""
    url = f"{SITE}{rel}" if rel.startswith("/") else rel

    line = f"{idx}. **{price}** — {addr}" if price else f"{idx}. {addr}"
    parts: list[str] = []
    if facts:
        parts.append(" · ".join(facts))
    if parking:
        parts.append(f"Parking: {parking}")
    extra: list[str] = []
    if posted:
        extra.append(f"Listed {posted}")
    mls = res.get("MlsNumber")
    if mls:
        extra.append(f"MLS {mls}")
    if extra:
        parts.append(" · ".join(extra))
    if agent:
        parts.append(agent)

    out = [line]
    for p in parts:
        out.append(f"   {p}")
    if url:
        out.append(f"   {url}")
    return "\n".join(out)


def render_search_results(
    data: dict,
    *,
    location: str,
    transaction: str = "sale",
    filters_desc: str = "",
    page: int = 1,
) -> str:
    paging = data.get("Paging") or {}
    results = data.get("Results") or []
    pins = data.get("Pins") or []
    total = paging.get("TotalRecords", 0)
    total_pages = paging.get("TotalPages", 0)
    cur = paging.get("CurrentPage", page)

    verb = "for rent" if transaction == "rent" else "for sale"
    head = f"**{total:,} listings {verb}** in {location}"
    if filters_desc:
        head += f" · {filters_desc}"

    if not results:
        return head + "\n\nNo listings matched these criteria."

    lines = [head]
    notes: list[str] = []
    notes.append(f"Page {cur} of {total_pages} (showing {len(results)})")
    if pins:
        notes.append(f"{len(pins)} map clusters across the area")
    if total > MAX_API_RECORDS:
        notes.append(f"realtor.ca returns at most {MAX_API_RECORDS} of these; refine filters to narrow")
    lines.append("_" + " · ".join(notes) + "_")
    lines.append("")

    rpp = paging.get("RecordsPerPage") or len(results)
    for i, res in enumerate(results, 1 + (cur - 1) * rpp):
        lines.append(_result_summary(i, res))
        lines.append("")

    return "\n".join(lines).rstrip()


def render_listing_detail(parsed: dict, similar: list[dict] | None = None) -> str:
    lines: list[str] = []
    address = parsed.get("address") or "Listing"
    lines.append(f"# {address}")
    lines.append("")

    headline: list[str] = []
    if parsed.get("price"):
        headline.append(f"**{parsed['price']}**")
    if parsed.get("beds"):
        headline.append(parsed["beds"])
    if parsed.get("baths"):
        headline.append(parsed["baths"])
    if headline:
        lines.append(" · ".join(headline))
        lines.append("")

    meta_bits: list[str] = []
    if parsed.get("mls"):
        meta_bits.append(f"MLS {parsed['mls']}")
    listed_by = " / ".join(p for p in (parsed.get("agent"), parsed.get("brokerage")) if p)
    if listed_by:
        meta_bits.append(f"Listed by {listed_by}")
    if meta_bits:
        lines.append("_" + " · ".join(meta_bits) + "_")
        lines.append("")

    desc = parsed.get("description")
    if desc:
        lines.append("## Description")
        lines.append("")
        lines.append(desc)
        lines.append("")

    details = parsed.get("details") or []
    if details:
        lines.append("## Property Details")
        lines.append("")
        ordered = sorted(
            details,
            key=lambda kv: _KEY_DETAIL_ORDER.index(kv[0]) if kv[0] in _KEY_DETAIL_ORDER else len(_KEY_DETAIL_ORDER),
        )
        for key, value in ordered:
            lines.append(f"- **{key}:** {value}")
        lines.append("")

    rooms = parsed.get("rooms") or []
    if rooms:
        lines.append("## Rooms")
        lines.append("")
        for label, dims in rooms:
            lines.append(f"- **{label}**" + (f" — {dims}" if dims else ""))
        lines.append("")

    loc = parsed.get("location_description")
    if loc:
        lines.append("## Location")
        lines.append("")
        lines.append(loc)
        lines.append("")

    if similar:
        lines.append("## Similar Listings Nearby")
        lines.append("")
        for i, res in enumerate(similar, 1):
            lines.append(_result_summary(i, res))
            lines.append("")

    if parsed.get("url"):
        lines.append(parsed["url"])

    return "\n".join(lines).rstrip()
