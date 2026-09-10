"""Vacuum Wars (vacuumwars.com) content extraction and cleanup.

Exports the standard site interface (SELECTORS_LIST, is_vacuumwars,
extract_compare_products, strip_vacuumwars_junk, postprocess_vacuumwars).

Two problems, both of which fail by rendering something plausible:

1. ``/compare/<category>/`` is an Alpine.js single-page app. The plain HTML
   path renders its empty state -- "No products found.", "No brand found",
   "Accordion Title" -- which reads like a board with nothing on it rather
   than like an extraction failure. The whole dataset is in fact inline in
   the page, as ``window.vwProducts``: every robot Vacuum Wars has listed,
   91 fields each, including their own lab measurements (suction, airflow,
   carpet deep clean, pet hair pickup, hair tangle, mopping, navigation) and
   the five star scores those roll up into. That data exists nowhere else on
   the site in machine-readable form, so it is extracted here and the SPA
   shell is discarded.

   The array is complete on the first page: the tool paginates client-side
   over it (``vwProducts.length / this.itemsPerPage``), so ``/page/2/``
   re-serves the same array and must never be fetched as though it held more.

2. The leaderboard card widget (``.vwx-pc``, on the Top 20 page and on every
   single-product review) renders each product twice -- once as a collapsed
   table row and once as the expanded panel behind it. Both survive
   markdownify, so each product's name, image, score, price and buy link
   appear three to four times. The collapsed row carries nothing the expanded
   panel lacks, so it is dropped.

A listed-but-never-tested robot is a real category here: 104 of the 301
entries have specs and no lab results. Those rows keep an explicit em dash
rather than an empty cell, because "we did not test this" and "it scored
nothing" must not look alike.

fetchaller has NO credentialed path to vacuumwars.com. Everything here is
served anonymously.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_VACUUMWARS_HOSTS = frozenset({
    "vacuumwars.com", "www.vacuumwars.com",
    # The comparison tool's own front end. The WordPress page embeds this same
    # app, and both serve a byte-identical dataset (91 fields, same scores and
    # prices for every slug present in both). This host is far leaner -- the
    # array starts ~2 KB in rather than ~285 KB in -- so it is the cheaper
    # source when a caller already has its URL.
    "compare.vacuumwars.com",
})

# On the WordPress site the tool lives under /compare/. On its own front end
# every route is the tool, so path alone cannot decide.
_COMPARE_HOSTS = frozenset({"compare.vacuumwars.com"})


def is_vacuumwars(url: str) -> bool:
    """Check if URL is a Vacuum Wars page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in _VACUUMWARS_HOSTS


def is_compare_url(url: str | None) -> bool:
    """Check if URL is a comparison-tool page.

    The dataset is only ever allowed to replace the page on these paths. A
    review page that happened to carry the same global would otherwise be
    thrown away and rendered as a spec table.
    """
    if not url:
        return False
    parts = urlparse(url)
    if (parts.hostname or "").lower() in _COMPARE_HOSTS:
        return True
    path = parts.path
    return path == "/compare" or path.startswith("/compare/")


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Collapsed leaderboard row. Duplicates .vwx-exp (image, name, score,
    # price, buy link) which is kept because it also carries the verdict.
    ".vwx-row",
    # "09/10/2026 06:49 pm GMT" price stamp, repeated once per card.
    ".vwx-price-date",
    # "Show more" / "Show less" methodology toggle.
    ".vwx-more",
    # Leaderboard column header ("RankRobotScorePrice today"), orphaned once
    # the collapsed rows it labelled are gone.
    ".vwx-lb-head",
    # "+2 more" chip. Purely an affordance: the chips it reveals are already
    # in the DOM, hidden by nth-of-type CSS, so nothing is lost by dropping it.
    ".vwx-chip-more",
]


# ---------------------------------------------------------------------------
# Comparison-tool dataset
# ---------------------------------------------------------------------------

_COMPARE_MARKER = "__VACUUMWARS_COMPARE__"
_COMPARE_MISSING_MARKER = "__VACUUMWARS_COMPARE_MISSING__"

# markdownify collapses consecutive newlines inside a text node, so blank
# lines in the injected block are carried as a sentinel and restored after.
_BLANK = "__VACUUMWARS_BLANK__"

# Vacuum Wars' own lab measurements. A robot carrying none of these is listed
# but untested, which is reported rather than blended in with the tested ones.
_TEST_FIELDS = (
    "suction_test_kpa",
    "airflow_test_cfm",
    "carpet_deep_clean_test",
    "crevice_pickup_test",
    "battery_efficiency_test_mpp",
    "sf_per_charge",
    "flattened_pet_hair_pickup_test_2_inches_5",
    "hair_tangle_test_seven_inches",
    "mop_water_test",
    "vacuum_wars_score_stars",
    "vw_mop_score_stars",
    "obstacle_avoidance_score_stars",
    "overall_navigation_score_stars",
    "overall_pet_score_stars",
)

# (field, header) for the ranked score table.
_SCORE_COLUMNS = (
    ("vacuum_wars_score_stars", "VW *"),
    ("vw_mop_score_stars", "Mop *"),
    ("obstacle_avoidance_score_stars", "Obstacle *"),
    ("overall_navigation_score_stars", "Nav *"),
    ("overall_pet_score_stars", "Pet *"),
    ("carpet_deep_clean_test", "Carpet clean"),
    ("flattened_pet_hair_pickup_test_2_inches_5", "Pet hair %"),
    ("hair_tangle_test_seven_inches", "Tangle %"),
    ("crevice_pickup_test", "Crevice"),
    ("suction_test_kpa", "Suction kPa"),
    ("airflow_test_cfm", "Airflow cfm"),
    ("sf_per_charge", "ft2/charge"),
)

# (field, header, kind) for the spec table. kind drives formatting.
_SPEC_COLUMNS = (
    ("vacuum_wars_score_stars", "VW *", "raw"),
    ("official_suction_power", "Suction Pa", "int"),
    ("official_battery_life", "Battery min", "int"),
    ("robot_height_inches", "Height in", "raw"),
    ("threshold_height_mm", "Threshold mm", "raw"),
    ("internal_dustbin_capacity_ml", "Dustbin mL", "int"),
    ("navigation_type", "Navigation", "raw"),
    ("mop_pad_type", "Mop", "raw"),
    ("self_emptying_bin", "Self-empty", "bool"),
    ("mop_self_washing", "Mop wash", "bool"),
    ("obstacle_avoidance", "Obstacle avoid", "bool"),
    ("matter_compatible", "Matter", "bool"),
)

_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_VWPRODUCTS_RE = re.compile(r"window\s*\.\s*vwProducts\s*=\s*\[")


def _format_price(value) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _format_bool(value) -> str:
    if value in (None, ""):
        return "-"
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return "yes"
    if text in ("0", "false", "no"):
        return "no"
    return text


def _format_int(value) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value)


def _format_raw(value) -> str:
    if value in (None, ""):
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    # Keep the site's own pipe-free text; a stray pipe would break the table.
    return str(value).replace("|", "/").strip()


_FORMATTERS = {"raw": _format_raw, "int": _format_int, "bool": _format_bool}


def _base_name(name: str) -> str:
    """Model name with a trailing colour/finish parenthetical removed."""
    return _TRAILING_PAREN_RE.sub("", name or "").strip()


def _test_signature(product: dict) -> tuple:
    return tuple(product.get(field) for field in _TEST_FIELDS)


def _is_tested(product: dict) -> bool:
    return any(product.get(field) not in (None, "") for field in _TEST_FIELDS)


def _group_variants(products: list[dict]) -> list[dict]:
    """Collapse colour/finish variants of one model into a single row.

    Two entries merge only when they share a brand, a base name and every one
    of Vacuum Wars' own measurements -- i.e. they are the same tested robot in
    a different shell. Prices are kept: when the variants disagree the row
    shows the cheapest as "from", because presenting one variant's price as
    the model's price is how a 20% difference disappears silently.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for product in products:
        key = (
            (product.get("brand") or "").strip().lower(),
            _base_name(product.get("name") or "").lower(),
            _test_signature(product),
        )
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(product)

    merged: list[dict] = []
    for key in order:
        members = groups[key]
        head = dict(members[0])
        head["name"] = _base_name(head.get("name") or "") or (head.get("name") or "")
        prices = []
        for member in members:
            try:
                prices.append(float(member["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        head["_variants"] = len(members)
        head["_price_min"] = min(prices) if prices else None
        head["_price_varies"] = bool(prices) and min(prices) != max(prices)
        merged.append(head)
    return merged


def _price_cell(product: dict) -> str:
    price = product.get("_price_min", product.get("price"))
    text = _format_price(price)
    if text != "-" and product.get("_price_varies"):
        return f"from {text}"
    return text


def _name_cell(product: dict) -> str:
    name = _format_raw(product.get("name"))
    variants = product.get("_variants", 1)
    if variants > 1:
        name = f"{name} ({variants} listings)"
    return name


def _sort_key(product: dict) -> float:
    try:
        return -float(product.get("vacuum_wars_score_stars"))
    except (TypeError, ValueError):
        return 1.0


def _render_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |"]
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def _render_compare(products: list[dict], category: str) -> str:
    merged = _group_variants(products)
    tested = sorted((p for p in merged if _is_tested(p)), key=_sort_key)
    untested = sorted(
        (p for p in merged if not _is_tested(p)),
        key=lambda p: (p.get("brand") or "", p.get("name") or ""),
    )
    listings = sum(p.get("_variants", 1) for p in merged)

    lines = [f"# Vacuum Wars comparison data: {category}"]
    lines.append(_BLANK)
    lines.append(
        f"{len(merged)} models ({listings} listings including colour variants). "
        f"{len(tested)} lab-tested by Vacuum Wars, {len(untested)} listed with "
        f"manufacturer specs only and never tested."
    )
    lines.append(_BLANK)
    lines.append(
        "Star columns are Vacuum Wars' own 0-5 scores. Test columns are their "
        "measurements, not manufacturer claims: carpet clean and pet hair are "
        "percent removed, tangle is percent of runs that wrapped (lower is "
        "better), crevice and the star columns are their own scales. A dash "
        "means not published for that model."
    )
    lines.append(_BLANK)

    if tested:
        lines.append(f"## Lab-tested, ranked by Vacuum Wars score ({len(tested)})")
        lines.append(_BLANK)
        header = ["#", "Model", "Brand", "Price"] + [h for _, h in _SCORE_COLUMNS]
        rows = []
        for rank, product in enumerate(tested, 1):
            row = [
                str(rank),
                _name_cell(product),
                _format_raw(product.get("brand")),
                _price_cell(product),
            ]
            row += [_format_raw(product.get(field)) for field, _ in _SCORE_COLUMNS]
            rows.append(row)
        lines.append(_render_table(header, rows))
        lines.append(_BLANK)

    if merged:
        lines.append(f"## Specifications ({len(merged)})")
        lines.append(_BLANK)
        lines.append(
            "Tested models first in score order, then the untested ones by brand."
        )
        lines.append(_BLANK)
        header = ["Model", "Brand", "Price"] + [h for _, h, _ in _SPEC_COLUMNS]
        rows = []
        for product in tested + untested:
            row = [
                _name_cell(product),
                _format_raw(product.get("brand")),
                _price_cell(product),
            ]
            for field, _, kind in _SPEC_COLUMNS:
                row.append(_FORMATTERS[kind](product.get(field)))
            rows.append(row)
        lines.append(_render_table(header, rows))
        lines.append(_BLANK)

    lines.append(
        "Prices are the comparison tool's last cached Amazon figure and move "
        "independently of the scores. Per-model detail, including how each "
        "test was run, is on that model's review page."
    )
    return "\n".join(lines)


def _parse_vwproducts(text: str) -> list[dict] | None:
    """Pull the ``window.vwProducts`` array out of an inline script."""
    match = _VWPRODUCTS_RE.search(text)
    if not match:
        return None
    start = text.index("[", match.start())
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def _category_label(soup: BeautifulSoup, url: str | None) -> str:
    path = urlparse(url or "").path.strip("/")
    parts = [p for p in path.split("/") if p and p != "compare"]
    if parts and parts[0] != "page":
        return parts[0].replace("-", " ")
    heading = soup.find("h1")
    if heading:
        return heading.get_text(strip=True)
    return "products"


def extract_compare_products(soup: BeautifulSoup, url: str | None = None) -> None:
    """Extract the comparison tool's inline dataset and inject a marker.

    Called from clean_html() before CSS selectors fire, which remove all
    ``<script>`` tags. Detection is by the ``window.vwProducts`` assignment
    rather than by URL shape, so it still fires if the tool moves.
    """
    if not is_compare_url(url):
        return

    products = None
    for script in soup.find_all("script"):
        text = script.string or ""
        if "vwProducts" not in text:
            continue
        products = _parse_vwproducts(text)
        if products:
            break

    body = soup.find("body")
    if body is None:
        return

    if not products:
        # The shell renders "No products found." either way, so a silent
        # fall-through here would report an empty comparison tool rather than
        # a failed read. Say which it is.
        marker = soup.new_tag("div", id="vacuumwars-compare-marker")
        marker.string = _COMPARE_MISSING_MARKER
        body.insert(0, marker)
        return

    content = _render_compare(products, _category_label(soup, url))

    marker = soup.new_tag("div", id="vacuumwars-compare-marker")
    marker.string = _COMPARE_MARKER + content + _COMPARE_MARKER
    body.insert(0, marker)


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_vacuumwars_junk(soup: BeautifulSoup) -> None:
    """Remove Vacuum Wars chrome that CSS selectors can't easily target."""
    # Accordion toggle buttons render as a bare "▼" line per product card.
    for el in list(soup.find_all("button")):
        text = el.get_text(strip=True)
        if not text or text in ("▼", "▲", "Show more", "Show less"):
            el.decompose()

    # Empty table-of-contents anchors left behind inside every card heading.
    for el in list(soup.select("span.ez-toc-section, span.ez-toc-section-end")):
        el.decompose()

    # Feature chips are adjacent inline spans, so markdownify runs them into
    # one another ("...extending side brushPad mop, washing and lifting..."),
    # which reads as a single garbled feature. Separate them.
    for group in soup.select(".vwx-feat"):
        chips = group.select(".vwx-chip")
        for chip in chips[:-1]:
            chip.insert_after(NavigableString("; "))


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_COMPARE_MARKER_RE = re.compile(
    r"__VACUUMWARS_COMPARE__([\s\S]*?)__VACUUMWARS_COMPARE__\n*"
)

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# Sentinel lines standing in for blank lines markdownify would have collapsed.
_BLANK_LINE_RE = re.compile(r"^__VACUUMWARS_BLANK__$", re.M)

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Affiliate disclosure, repeated at the top of every page.
    (re.compile(
        r"(?:^|\n)Vacuum Wars is reader supported\.[^\n]*\n"
        r"(?:\[Details\][^\n]*\n)?"
    ), "\n"),
    # Price timestamp left over where the card markup varies.
    (re.compile(r"(?:^|\n)\d{2}/\d{2}/\d{4} \d{2}:\d{2} [ap]m [A-Z]{2,4}\n"), "\n"),
    # Accordion caret that survived as text rather than a button.
    (re.compile(r"(?:^|\n)[ \t]*[▼▲][ \t]*\n"), "\n"),
    # Comparison-tool empty state, when the dataset could not be read.
    (re.compile(r"(?:^|\n)No (?:products|brand) found\.?\n"), "\n"),
    (re.compile(r"(?:^|\n)Accordion\s*\n+Title\n"), "\n"),
    # Repeated buy-link chrome.
    (re.compile(r"(?:^|\n)Add to Compare\n"), "\n"),
    (re.compile(r"(?:^|\n)No Review Yet\n"), "\n"),
    # Empty headings left by stripped card fragments.
    (re.compile(r"(?:^|\n)#{1,6}[ \t]*\n"), "\n"),
]


def postprocess_vacuumwars(markdown: str) -> str:
    """Clean up Vacuum Wars markdown noise."""
    # Comparison tool: the extracted dataset replaces the SPA shell entirely.
    match = _COMPARE_MARKER_RE.search(markdown)
    if match:
        content = match.group(1).strip()
        return _BLANK_LINE_RE.sub("", content).strip()

    if _COMPARE_MISSING_MARKER in markdown:
        markdown = markdown.replace(_COMPARE_MISSING_MARKER, "")
        markdown = (
            "**Vacuum Wars comparison tool: the product dataset could not be "
            "read from this page, so no models are listed below.** The page's "
            "own empty state looks the same as a comparison tool with nothing "
            "in it.\n\n"
        ) + markdown

    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)

    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
