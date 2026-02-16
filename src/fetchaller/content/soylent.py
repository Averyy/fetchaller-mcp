"""Soylent-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_soylent,
extract_inventory, strip_soylent_junk, postprocess_soylent).

Covers soylent.com and soylent.ca (Shopify stores). The pages have massive
junk: mega-menu dropdowns (46% of body), Rebuy cart template (12K), duplicate
mobile/desktop benefit sections, subscribe & save UI noise, and more.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_SOYLENT_HOSTS = frozenset({
    "soylent.com", "www.soylent.com",
    "soylent.ca", "www.soylent.ca",
})


def is_soylent(url: str) -> bool:
    """Check if URL is a Soylent page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in _SOYLENT_HOSTS


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Announcement bar (stock shortages, free shipping, social links, country selector)
    "#shopify-section-announcement-banner",
    # NOTE: #shopify-section-header-logo-left wraps #MainContent on Soylent pages,
    # so we can't remove it. The generic `nav` selector handles all navigation
    # content (mega-menus, header bar) inside it.
    # Footer (link columns, newsletter signup, copyright)
    "#shopify-section-footer",
    # Empty swatches section
    "#shopify-section-swatches",
    # Tracking pixel section
    "#shopify-section-pixels-by-path",
    # Feature badges marquee (duplicates product description)
    "#shopify-section-nutrition-marquee",
    # "real science" tagline + use-case images (marketing)
    "#shopify-section-pdp-use-cases",
    # "5 things to consider" marketing section
    "#shopify-section-pdp-why-product",
    # Duplicate icon grid (20g protein, vegan, etc.)
    "#shopify-section-pdp-icon-grid",
    # "us vs them" comparison table (marketing)
    "#shopify-section-pdp-brand-comparison",
    # Empty related products section
    "#shopify-section-related-products",
    # Exit intent popup
    "#shopify-section-product-exit-intent-gwp",
    # Nutritional facts section (JS-rendered, just empty headings)
    "#shopify-section-pdp-nutritional-facts",
    # Rebuy cart flyout Vue.js template (12K chars)
    "#rebuy-cart-template",
    # Search overlay modal
    "#search-overlay",
    # EComposer toast notification
    "#ecom-toast",
    # Mobile navigation drawer
    ".mobile-nav",
    # Slideup notification bar
    ".slideup",
    # Product card purchase form overlay
    ".product-card__overlay",
    # Inline purchase form on collection cards
    ".product-card__form-rw",
    # Slideup template (script tag)
    "[data-slideup-template]",
    # Handlebars templates (slideup)
    'script[type="text/x-handlebars-template"]',
    # AJAX cart container
    ".ajax-cart-container",
    # Skip-to-content link
    ".skip-link",
    # Collection filter/sort UI
    ".plp-filter-bar",
    # Subscribe vs one-time toggle (duplicated)
    ".product-form__purchase-selector",
    # Delivery frequency selector
    ".product-form__frequency",
    # Subscriber benefits tooltip ("Why Subscribe?", icons)
    ".product-form__tooltip",
    # Okendo review summary widget (4.6/5, "Rated X out of Y stars", review count)
    ".oke-sr",
    # Okendo review histogram column (rating bars, star counts)
    ".oke-w-header-content-block--oneThird",
    # Okendo "90% would recommend" widget
    ".oke-w-recommendsModule",
    # Okendo review star SVG symbols
    "svg#oke-star-symbols",
    # Okendo reviews body template
    "template#oke-reviews-body-template",
    # Desktop benefits section (tab nav + duplicate of mobile accordion)
    ".pdp-bs__desktop",
    # Feature flags debug UI (leaks on soylent.com)
    ".ffui-container",
    # Country selector
    ".country-selector",
    # Country dropdown
    ".country-dropdown",
]


# ---------------------------------------------------------------------------
# Pre-CSS-selector extraction (runs BEFORE generic `script` selector fires)
# ---------------------------------------------------------------------------

_GSF_QUANTITY_RE = re.compile(r'quantity\s*[=:]\s*"?(\d+)"?')
_VARIANT_AVAILABLE_RE = re.compile(r'"available"\s*:\s*(true|false)')
_INVENTORY_MARKER = "__SOYLENT_INVENTORY_{}__"
_AVAILABLE_MARKER = "__SOYLENT_AVAILABLE_{}__"


def extract_inventory(soup: BeautifulSoup) -> None:
    """Extract inventory/availability from embedded Shopify data and inject a marker.

    Uses two sources (checked in order):
    1. gsf_conversion_data quantity (soylent.ca) — gives exact count
    2. Shopify variant "available":true/false — gives in-stock/out-of-stock

    Called from clean_html() before CSS selectors fire (which remove all
    <script> tags). Injects a <p> marker at the end of <body> that survives
    markdownify and is picked up by the postprocessor.
    """
    quantity: int | None = None
    available: bool | None = None

    for script in soup.find_all("script"):
        text = script.string or ""
        if quantity is None and "gsf_conversion_data" in text:
            m = _GSF_QUANTITY_RE.search(text)
            if m:
                quantity = int(m.group(1))
        if available is None and '"available"' in text and "variants" in text:
            m = _VARIANT_AVAILABLE_RE.search(text)
            if m:
                available = m.group(1) == "true"

    body = soup.find("body")
    if body is not None:
        if quantity is not None:
            marker = soup.new_tag("p", id="soylent-inv-marker")
            marker.string = _INVENTORY_MARKER.format(quantity)
            body.append(marker)
        elif available is not None:
            marker = soup.new_tag("p", id="soylent-inv-marker")
            marker.string = _AVAILABLE_MARKER.format("yes" if available else "no")
            body.append(marker)


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_soylent_junk(soup: BeautifulSoup) -> None:
    """Remove Soylent-specific junk that CSS selectors can't easily target."""
    # Remove thumbnail images (100x size previews that duplicate main gallery)
    for img in list(soup.find_all("img")):
        src = img.get("src") or img.get("data-src") or ""
        if "_100x" in src:
            img.decompose()

    # Remove <img> tags with empty src (lazy-loaded placeholders with no fallback)
    for img in list(soup.find_all("img")):
        src = (img.get("src") or "").strip()
        if not src or src == "#":
            img.decompose()

    # Remove template script tags (text/template, text/x-template)
    for script in list(soup.find_all("script")):
        stype = script.get("type", "")
        if stype in ("text/template", "text/x-template"):
            script.decompose()

    # Remove all <input> elements (hidden form fields)
    for el in list(soup.find_all("input")):
        el.decompose()

    # Remove all <select> elements (quantity pickers, frequency dropdowns)
    for el in list(soup.find_all("select")):
        el.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")
_INVENTORY_MARKER_RE = re.compile(r"__SOYLENT_INVENTORY_(\d+)__\n*")
_AVAILABLE_MARKER_RE = re.compile(r"__SOYLENT_AVAILABLE_(yes|no)__\n*")

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Radio button artifacts from markdownify: "(/) " and "( ) "
    (re.compile(r"\(/?[/ ]\)\s*"), ""),

    # Subscribe & save noise (case-insensitive — page uses lowercase)
    (re.compile(r"(?:^|\n)(?:subscribe & save|subscribe and save)[^\n]*\n", re.I), "\n"),
    (re.compile(r"(?:^|\n)one-time purchase[^\n]*\n", re.I), "\n"),
    (re.compile(r"(?:^|\n)prepaid subscription[^\n]*\n", re.I), "\n"),

    # "|" before "Why Subscribe?" (pipe artifact from toggle UI)
    (re.compile(r"(?:^|\n)\|\s*\n"), "\n"),

    # "Why Subscribe?" heading (just the heading, not a greedy block match)
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Why Subscribe\?\n"), "\n"),

    # Standalone "View Details" / "More Details" lines (plain or linked)
    (re.compile(r"(?:^|\n)\[?(?:View|More) Details\]?(?:\([^\)]*\))?\n"), "\n"),

    # Quantity stepper artifacts (+/-) — use [ \t]* not \s* to avoid eating newlines
    (re.compile(r"(?:^|\n)[ \t]*[+\u2212-][ \t]*\n"), "\n"),

    # Standalone "%" (leftover from subscribe save percentage)
    (re.compile(r"(?:^|\n)%\n"), "\n"),

    # Button text (separate patterns to avoid consecutive-line \n consumption)
    (re.compile(r"(?:^|\n)Sold Out\n"), "\n"),
    (re.compile(r"(?:^|\n)Add to Cart\n"), "\n"),
    (re.compile(r"(?:^|\n)Add To Cart\n"), "\n"),
    (re.compile(r"(?:^|\n)Shop Now\n"), "\n"),
    (re.compile(r"(?:^|\n)Play\n"), "\n"),
    # CTA links/buttons (standalone, case-insensitive)
    (re.compile(r"(?:^|\n)\[?(?:LEARN MORE|SHOP ALL)\]?(?:\([^\)]*\))?\n"), "\n"),
    (re.compile(r"(?:^|\n)[ \t]*learn more\n"), "\n"),
    (re.compile(r"(?:^|\n)\[?Take The Quiz\]?(?:\([^\)]*\))?\n"), "\n"),

    # Badge text
    (re.compile(r"(?:^|\n)Best Seller\n"), "\n"),
    (re.compile(r"(?:^|\n)Bundle & Save\n"), "\n"),

    # Form labels
    (re.compile(r"(?:^|\n)Duration:\n"), "\n"),
    (re.compile(r"(?:^|\n)Title\n"), "\n"),

    # "try this instead" (out-of-stock suggestion header)
    (re.compile(r"(?:^|\n)try this instead\n"), "\n"),

    # Page fraction navigation: "1/1", "1/3"
    (re.compile(r"(?:^|\n)\d+/\d+\n"), "\n"),

    # "N meals" standalone (case count)
    (re.compile(r"(?:^|\n)\d+\nmeals\n"), "\n"),

    # Review voting UI: "Was this helpful?" + Yes/No (may be joined on one line)
    (re.compile(
        r"(?:^|\n)\s*Was this helpful\?\n+"
        r"(?:\s*Yes[^\n]*\n+)?"
        r"(?:\s*No[^\n]*\n+)?",
    ), "\n"),

    # Loading / Show More
    (re.compile(r"(?:^|\n)Loading\.{3}\n"), "\n"),
    (re.compile(r"(?:^|\n)Show More\n"), "\n"),

    # Filter/sort/review UI
    (re.compile(r"(?:^|\n)Filters\s*\n"), "\n"),
    (re.compile(r"(?:^|\n)Sort\s*\n"), "\n"),
    (re.compile(r"(?:^|\n)Sort Most Recent[^\n]*\n"), "\n"),
    # "Write a Review" (plain text or linked, with optional extra text)
    (re.compile(r"(?:^|\n)\[?Write a Review[^\]\n]*\]?(?:\([^\)]*\))?\n"), "\n"),

    # Okendo review summary / histogram noise
    (re.compile(r"(?:^|\n)\d\.\d\s*\n"), "\n"),  # standalone rating score ("4.6")
    (re.compile(r"(?:^|\n)Rated\s+\d*[\d.]*\s*out of \d+ stars\n"), "\n"),
    (re.compile(r"(?:^|\n)[\d,.]+ reviews\n", re.I), "\n"),
    (re.compile(r"(?:^|\n)Based on [\d,.]+ reviews\n"), "\n"),
    (re.compile(r"(?:^|\n)Total \d+ star reviews:[^\n]*\n"), "\n"),
    (re.compile(r"(?:^|\n)\*\*\d+%\*\*would recommend[^\n]*\n"), "\n"),

    # "Read More" / "Read more about this review" / "Read less" (plain or linked)
    (re.compile(r"(?:^|\n)\s*Read [Mm]ore(?:Read more about this review)?\n"), "\n"),
    (re.compile(r"(?:^|\n)\s*Read less\n"), "\n"),

    # Okendo review metadata noise
    (re.compile(r"(?:^|\n)\s*Verified Buyer\n"), "\n"),
    (re.compile(r"(?:^|\n)\s*I recommend this product\n"), "\n"),
    (re.compile(r"(?:^|\n)\s*Reviewing\n"), "\n"),
    # Okendo review survey labels ("**I like Soylent because...**  answers")
    (re.compile(r"(?:^|\n)\s*\*\*I (?:like|use) Soylent[^\n]*\n"), "\n"),
    # Okendo AI review summary metadata
    (re.compile(r"(?:^|\n)Reviews Summary\s*\n"), "\n"),
    (re.compile(r"(?:^|\n)Generated by AI[^\n]*\n"), "\n"),
    (re.compile(r"(?:^|\n)Last updated[^\n]*\n"), "\n"),
    # Standalone product name in reviews (e.g., "Soylent drink chocolate")
    (re.compile(r"(?:^|\n)\s*Soylent drink [a-z]+\n"), "\n"),

    # UI instructions
    (re.compile(r"(?:^|\n)scroll to view[^\n]*\n", re.I), "\n"),
    (re.compile(r"(?:^|\n)swipe to (?:compare|view)\n", re.I), "\n"),
    (re.compile(r"(?:^|\n)\*?Click or tap to learn[^\n]*\n", re.I), "\n"),

    # Empty Nutrition Facts section (JS-rendered, just headers with no values)
    (re.compile(
        r"(?:^|\n)#{1,6}\s+Nutrition Facts\n+"
        r"Servings Per Container\n+"
        r"[\s\S]*?"
        r"general nutrition advice\.\n",
    ), "\n"),

    # "View Nutritional Information" standalone
    (re.compile(r"(?:^|\n)View Nutritional Information\n"), "\n"),

    # Empty headings (### with no text, just spaces/tabs)
    (re.compile(r"(?:^|\n)#{1,6}[ \t]*\n"), "\n"),

    # "us vs them" heading and subtitle (remove heading only, table data is small)
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?us vs\.? them\n", re.I), "\n"),
    (re.compile(r"(?:^|\n)science-backed nutrition wins[^\n]*\n", re.I), "\n"),

    # "5 things to consider" marketing section (heading only)
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?\d+ things to consider[^\n]*\n", re.I), "\n"),

    # Duplicate consecutive images (same URL on adjacent lines)
    (re.compile(r"(!\[[^\]]*\]\([^\)]+\))\n+\1"), r"\1"),

    # Duplicate consecutive text lines (mobile/desktop duplication)
    (re.compile(r"(?:^|\n)([^\n#!\[*\d][^\n]{5,})\n+\1\n"), r"\n\1\n"),

    # Standalone Soylent Canada brand name line (appears before product heading)
    (re.compile(r"(?:^|\n)Soylent Canada\n"), "\n"),

    # French duplicate section header "ingrédients" (bilingual page)
    (re.compile(
        r"(?:^|\n)ingrédients\n"
        r"[\s\S]*?"
        r"(?:\*\*Contains:\*\*\n)",
    ), "\n"),

    # "Contains:" label standalone (after ingredient list removed)
    (re.compile(r"(?:^|\n)\*\*Contains:\*\*\n"), "\n"),
]


def _dedup_benefits(markdown: str) -> str:
    """Remove all but the first 'benefits of drinking Soylent' section."""
    benefits_re = re.compile(
        r"(?:^|\n)#{1,4}\s+(?:the )?benefits of (?:drinking )?soylent",
        re.IGNORECASE,
    )
    # Find all occurrences; repeatedly remove the second one
    while True:
        matches = list(benefits_re.finditer(markdown))
        if len(matches) <= 1:
            break
        m = matches[1]
        # Find end of the duplicate section: next heading at same/higher level, or ---
        rest = markdown[m.end():]
        end_m = re.search(r"\n(?=#{1,2} |---)", rest)
        cut_end = m.end() + end_m.start() if end_m else len(markdown)
        markdown = markdown[:m.start()] + markdown[cut_end:]
    return markdown


def postprocess_soylent(markdown: str) -> str:
    """Clean up Soylent-specific markdown noise."""
    # Extract inventory/availability markers if present
    inventory: int | None = None
    available: bool | None = None

    m = _INVENTORY_MARKER_RE.search(markdown)
    if m:
        inventory = int(m.group(1))
        markdown = markdown[:m.start()] + markdown[m.end():]

    m = _AVAILABLE_MARKER_RE.search(markdown)
    if m:
        available = m.group(1) == "yes"
        markdown = markdown[:m.start()] + markdown[m.end():]

    # Apply all regex patterns
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)

    # Deduplicate "benefits of soylent" sections (mobile/desktop duplication)
    markdown = _dedup_benefits(markdown)

    # Inject stock line after first heading.
    # Priority: exact quantity (from gsf_conversion_data) > boolean available
    stock_line: str | None = None
    if inventory is not None:
        stock_line = "**Out of stock**" if inventory == 0 else f"**Stock: {inventory} units**"
    elif available is not None:
        stock_line = "**In stock**" if available else "**Out of stock**"

    if stock_line:
        heading_m = re.search(r"(# [^\n]+\n)", markdown)
        if heading_m:
            insert_pos = heading_m.end()
            markdown = markdown[:insert_pos] + f"\n{stock_line}\n" + markdown[insert_pos:]
        else:
            markdown = f"{stock_line}\n\n{markdown}"

    # Collapse excessive newlines
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
