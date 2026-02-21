"""Molex-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_molex,
extract_molex_jsonld, strip_molex_junk, postprocess_molex).

Molex product pages are CSR — the visible HTML body shows only
"Limited Information Available" while all product specs live in
a <script type="application/ld+json"> block with @type: "Product"
and additionalProperty PropertyValue arrays.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_MOLEX_HOST_RE = re.compile(r"^(?:www\.)?molex\.com$")


def is_molex(url: str) -> bool:
    """Check if URL is a Molex page."""
    hostname = (urlparse(url).hostname or "").lower()
    return bool(_MOLEX_HOST_RE.match(hostname))


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Header (logo, search, account, mega menu)
    ".cmp-header",
    ".cmp-megamenunavigation",
    "[data-cmp-is='megamenunavigation']",
    "[data-cmp-is='megamenuitem']",
    ".cmp-searchbar",
    "[data-cmp-is='searchbar']",
    ".cmp-account",
    "[data-cmp-is='account']",
    "#header-login",
    "#molex-account",
    "#user-sign-out",
    # Skip to main content
    ".cmp-page__skiptomaincontent",
    # Language navigation
    ".cmp-languagenavigation",
    # Cart
    "#cart-items",
    # Navigation headers (product categories)
    "[id^='nav-header-']",
    "[id^='nav-sec-header-']",
    # Experience fragments (header/footer chrome)
    ".cmp-experiencefragment--header",
    # Cookie consent
    "[data-domain-script]",
    # Loading overlay
    "#loader",
]


# ---------------------------------------------------------------------------
# JSON-LD extraction (runs BEFORE CSS selectors fire)
# ---------------------------------------------------------------------------

_JSONLD_MARKER = "__MOLEX_JSONLD__"

# Compliance/environmental properties to skip (noisy, low-value)
_SKIP_PROPERTIES = frozenset({
    "Add1 Display Name", "Add1 Status",
    "China Ro Hs Display Name", "China Ro Hs Status",
    "Elv Display Name", "Elv Status",
    "Hflh Display Name", "Hflh Status",
    "Prop65 Display Name", "Prop65 Status",
    "Reach Display Name", "Reach Status",
    "Ro Hs Display Name", "Ro Hs Status",
})


def extract_molex_jsonld(soup: BeautifulSoup) -> None:
    """Extract structured product data from JSON-LD and inject as a marker.

    Molex product pages include `<script type="application/ld+json">` with
    @type: "Product" containing name, brand, sku, mpn, description,
    and additionalProperty with PropertyValue specs.

    Called from clean_html() before CSS selectors fire (which remove scripts).
    Injects a <div> marker that survives markdownify.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue

            lines = []

            # Product name
            name = item.get("name")
            if name:
                lines.append(f"**Product:** {name}")

            # SKU / MPN
            sku = item.get("sku")
            mpn = item.get("mpn")
            if mpn and mpn != sku:
                lines.append(f"**MPN:** {mpn}")
            if sku:
                lines.append(f"**SKU:** {sku}")

            # Brand
            brand = item.get("brand")
            if isinstance(brand, dict):
                brand_name = brand.get("name")
            else:
                brand_name = brand
            if brand_name:
                lines.append(f"**Brand:** {brand_name}")

            # Description (from og:description via JSON-LD)
            desc = item.get("description")
            if desc and desc != name:
                lines.append(f"**Description:** {desc}")

            # Offers (price, availability)
            offers = item.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if isinstance(offers, list):
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue
                    price = offer.get("price")
                    currency = offer.get("priceCurrency", "")
                    if price:
                        lines.append(f"**Price:** {currency} {price}".strip())
                    avail = offer.get("availability", "")
                    if avail:
                        avail_label = avail.rsplit("/", 1)[-1]
                        lines.append(f"**Availability:** {avail_label}")

            # Specifications from additionalProperty
            specs = item.get("additionalProperty", [])
            if isinstance(specs, dict):
                specs = [specs]
            spec_lines = []
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                spec_name = (spec.get("name") or "").strip()
                spec_value = (spec.get("value") or "").strip()
                if not spec_name or not spec_value:
                    continue
                if spec_name in _SKIP_PROPERTIES:
                    continue
                spec_lines.append(f"- **{spec_name}:** {spec_value}")

            if spec_lines:
                lines.append("")
                lines.append("**Specifications:**")
                lines.extend(spec_lines)

            if lines:
                body = soup.find("body")
                if body is not None:
                    marker = soup.new_tag("div", id="molex-jsonld-marker")
                    marker.string = _JSONLD_MARKER + "\n".join(lines) + _JSONLD_MARKER
                    body.insert(0, marker)
                return  # Only process the first Product


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_molex_junk(soup: BeautifulSoup) -> None:
    """Remove Molex-specific junk that CSS selectors can't easily target."""
    # Remove all <input> elements (hidden form fields)
    for el in list(soup.find_all("input")):
        el.decompose()

    # Remove tracking pixels
    for img in list(soup.find_all("img")):
        width = img.get("width", "")
        height = img.get("height", "")
        if width in ("0", "1") or height in ("0", "1"):
            img.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "Limited Information Available" message and surrounding text
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Part Number Found(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Limited Information Available(?=\n|$)"), "\n"),
    (re.compile(
        r"(?:^|\n)This part is not formally published to our online part catalog\."
        r"[^\n]*(?=\n|$)"
    ), "\n"),

    # "Skip to main content" link
    (re.compile(r"(?:^|\n)\[Skip to main content\]\([^\)]*\)(?=\n|$)"), "\n"),

    # Login/Register standalone
    (re.compile(r"(?:^|\n)\[?Register\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Login\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Log Out\]?(?=\n|$)", re.I), "\n"),

    # Account menu items (Profile, Change Password, Sample History)
    (re.compile(r"(?:^|\n)-?\s*\[?Profile\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Change Password\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Sample History\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Account" standalone
    (re.compile(r"(?:^|\n)Account(?=\n|$)"), "\n"),

    # Navigation menu items (standalone category names)
    (re.compile(r"(?:^|\n)-?\s*Products & Services(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*Industries & Applications(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*Trends & Insights(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Careers\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*About(?=\n|$)"), "\n"),

    # "Label" standalone (placeholder text from AEM)
    (re.compile(r"(?:^|\n)Label(?=\n|$)"), "\n"),

    # Footer "About Us" boilerplate
    (re.compile(
        r"(?:^|\n)(?:#{1,4}\s+)?About Us\n+"
        r"[-—]+\n+"
        r"Molex is a global electronics leader[\s\S]*?"
        r"Creating Connections for Life\.\*(?=\n|$)"
    ), "\n"),
    (re.compile(
        r"(?:^|\n)\[Learn More About How We Create Connections for Life\]\([^\)]*\)(?=\n|$)"
    ), "\n"),

    # "More Information" footer section
    (re.compile(
        r"(?:^|\n)(?:#{1,4}\s+)?\*{0,2}More Information\*{0,2}\n+"
        r"(?:\[(?:Careers|Case Studies|Leadership|Locations|Stewardship)[^\]]*\]\([^\)]*\)\s*\n?)*"
    ), "\n"),

    # "Home" breadcrumb
    (re.compile(r"(?:^|\n)-?\s*\[?Home\]?(?:\(/en-us/home\))?(?=\n|$)"), "\n"),

    # Standalone horizontal rules from AEM separators
    (re.compile(r"(?:^|\n)---(?=\n|$)"), "\n"),
]

_JSONLD_MARKER_RE = re.compile(
    r"__MOLEX_JSONLD__([\s\S]*?)__MOLEX_JSONLD__\n*"
)

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_molex(markdown: str) -> str:
    """Clean up Molex-specific markdown noise."""
    # Extract and reformat JSON-LD marker
    m = _JSONLD_MARKER_RE.search(markdown)
    if m:
        jsonld_content = m.group(1).strip()
        markdown = markdown[:m.start()] + markdown[m.end():]
        # Inject structured data after first heading (or at start)
        heading_m = re.search(r"(# [^\n]+\n)", markdown)
        if heading_m:
            insert_pos = heading_m.end()
            markdown = markdown[:insert_pos] + f"\n{jsonld_content}\n" + markdown[insert_pos:]
        else:
            markdown = f"{jsonld_content}\n\n{markdown}"

    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
