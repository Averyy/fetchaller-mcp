"""eBay-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_ebay,
extract_ebay_jsonld, strip_ebay_junk, postprocess_ebay).

Covers all eBay TLDs (.com, .ca, .co.uk, .de, .fr, .it, .es, .com.au).
Product pages include JSON-LD with structured data (price, condition,
availability, seller, brand) which we extract before scripts are removed.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_EBAY_HOST_RE = re.compile(
    r"^(?:www\.)?ebay\."
    r"(?:com|ca|co\.uk|de|fr|it|es|com\.au|at|ch|ie|nl|be|pl|ph|com\.sg|com\.my)$"
)


def is_ebay(url: str) -> bool:
    """Check if URL is an eBay page."""
    hostname = (urlparse(url).hostname or "").lower()
    return bool(_EBAY_HOST_RE.match(hostname))


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Global header (logo, search, cart, sign in)
    "#gh",
    "#gh-top",
    "#gh-header",
    "#gh-cat",
    "#gh-minicart-hover",
    "#gh-eb",
    "#gh-p",
    "#gh-ac",
    "#gh-shipto-click",
    # Global footer
    "#glbfooter",
    "#Footer",
    # Search-related
    ".srp-related_searches",
    ".srp-save-null-search",
    # Merchandising / sponsored cards
    ".merch-card",
    "[class*='merch-card']",
    ".vlp-merch",
    # Store information sidebar
    "#STORE_INFORMATION",
    # Seller banner / promotional
    ".seller-banner",
    "[class*='seller-banner']",
    # "Report this item" link
    "#report-button",
    # Breadcrumb navigation
    ".seo-breadcrumbs",
    "[class*='breadcrumb']",
    # Tab navigation on product pages
    ".tabbed-content-tabs",
    # Watch / save buttons
    "[class*='watchBtn']",
    # Share buttons
    "[class*='socialShare']",
    # Ad placements
    ".adSlot",
    "[class*='adSlot']",
    "[id*='rtm_html']",
    # Feedback / review form links
    "#UserFeedback",
    # Similar / sponsored items carousels
    "[class*='sim-list']",
    "[class*='recentlyViewed']",
    "[class*='merch__items']",
    # Notification banners
    "[class*='toastNotification']",
    # Top bar (category nav)
    "#mainContent .hl-cat-nav",
    # Sign in prompt overlays
    "[class*='signin']",
    # Item location map
    "[class*='itemLocation'] iframe",
    # eBay refurbished certification badge (noise)
    "[class*='rr-certification']",
    # Search page: "Skip to main content" link
    ".gh-ar-topbar",
    # Search page: category navigation
    "#gh-cat-box",
    # Search page: category/filter sidebar
    ".srp-rail__left",
    ".x-refine",
    "[class*='srp-sidebar']",
    # Search page: related searches
    ".srp-related-searches",
    ".srp-related_searches__container",
    # Search page: "Shop by category" menu
    "#gh-shop-a",
    "[class*='gh-shop']",
    # Search page: sort/view controls
    ".srp-controls",
    "[class*='srp-format']",
    # Search page: results count bar
    ".srp-controls__count",
    # "Include description" checkbox
    "[class*='cbx-search']",
    # "Advanced" search link
    "[class*='gh-search-helpers']",
    # Top deals / promotional banners
    "[class*='top-deals']",
    "[class*='vlp-merch']",
    # Search page: river-answer blocks (carousels, FAQs, pagination, Popular Filters)
    ".srp-river-answer",
    # Search page: related searches (bottom of page)
    ".srp-refinements-guidance",
    # Search page: "Shop on eBay" ad cards (wrapped in div.s-clipped)
    "div.s-clipped",
    # Search page: SRP footer (items per page, currency disclaimer)
    "[class*='srp-footer']",
    # Visually-hidden accessibility text ("Opens in a new window or tab", etc.)
    "span.clipped",
    "span.s-clipped",
]


# ---------------------------------------------------------------------------
# JSON-LD extraction (runs BEFORE CSS selectors fire)
# ---------------------------------------------------------------------------

_JSONLD_MARKER = "__EBAY_JSONLD__"


def extract_ebay_jsonld(soup: BeautifulSoup) -> None:
    """Extract structured product data from JSON-LD and inject as a marker.

    eBay product pages include `<script type="application/ld+json">` with
    @type: "Product" containing price, condition, availability, seller, brand.

    Called from clean_html() before CSS selectors fire (which remove scripts).
    Injects a <div> marker that survives markdownify.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle both single object and array of objects
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue

            lines = []

            # Brand
            brand = item.get("brand")
            if isinstance(brand, dict):
                brand_name = brand.get("name")
            else:
                brand_name = brand
            if brand_name:
                lines.append(f"**Brand:** {brand_name}")

            # Offers (price, condition, availability)
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
                        lines.append(f"**Price:** {currency} {price}")

                    condition = offer.get("itemCondition", "")
                    if condition:
                        # Extract human-readable from URL like "NewCondition"
                        cond_label = condition.rsplit("/", 1)[-1].replace("Condition", "")
                        lines.append(f"**Condition:** {cond_label}")

                    avail = offer.get("availability", "")
                    if avail:
                        avail_label = avail.rsplit("/", 1)[-1]
                        lines.append(f"**Availability:** {avail_label}")

                    seller = offer.get("seller")
                    if isinstance(seller, dict):
                        seller_name = seller.get("name")
                        if seller_name:
                            lines.append(f"**Seller:** {seller_name}")

            if lines:
                body = soup.find("body")
                if body is not None:
                    marker = soup.new_tag("div", id="ebay-jsonld-marker")
                    marker.string = _JSONLD_MARKER + "\n".join(lines) + _JSONLD_MARKER
                    body.insert(0, marker)
                return  # Only process the first Product


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_ebay_junk(soup: BeautifulSoup) -> None:
    """Remove eBay-specific junk that CSS selectors can't easily target."""
    # Remove all <input> elements (hidden form fields, CSRF tokens)
    for el in list(soup.find_all("input")):
        el.decompose()

    # Remove tracking pixels (1x1 images)
    for img in list(soup.find_all("img")):
        width = img.get("width", "")
        height = img.get("height", "")
        if width in ("0", "1") or height in ("0", "1"):
            img.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # NOTE: Use (?=\n|$) lookahead instead of consuming \n at pattern end.
    # This prevents consecutive lines from being missed when the same pattern
    # matches multiple adjacent lines (consuming \n makes it unavailable as
    # the leading \n for the next match).

    # "Sign in" / "Register" standalone lines
    (re.compile(r"(?:^|\n)\[?Sign in\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Register\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "eBay Home" breadcrumb
    (re.compile(r"(?:^|\n)\[?eBay Home\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Report this item" links
    (re.compile(r"(?:^|\n)\[?Report this item[^\]]*\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Add to Watchlist" / "Add to cart" / "Buy It Now" buttons (standalone or in lists)
    (re.compile(r"(?:^|\n)-?\s*\[?Add to (?:Watchlist|cart)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Buy It Now\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Make Offer\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Place bid\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Shop with confidence" section
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Shop with confidence(?=\n|$)", re.I), "\n"),

    # "Back to home page" link
    (re.compile(r"(?:^|\n)\[?(?:Back to home page|Return to top)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "See other items" link
    (re.compile(r"(?:^|\n)\[?See other items\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Contact seller" standalone
    (re.compile(r"(?:^|\n)\[?Contact seller\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Save seller" button
    (re.compile(r"(?:^|\n)\[?Save seller\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "See all" / "Show more" links
    (re.compile(r"(?:^|\n)\[?See all\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Sponsored label (standalone or indented inside list items)
    (re.compile(r"(?:^|\n)\s*Sponsored(?=\n|$)"), "\n"),

    # "Picture N of M" image gallery nav
    (re.compile(r"(?:^|\n)Picture \d+ of \d+(?=\n|$)", re.I), "\n"),

    # "Opens image gallery" text
    (re.compile(r"(?:^|\n)Opens image gallery(?=\n|$)"), "\n"),

    # Item number line
    (re.compile(r"(?:^|\n)eBay item number:\s*\d+(?=\n|$)", re.I), "\n"),

    # "Skip to main content" link
    (re.compile(r"(?:^|\n)\[Skip to main content\]\([^\)]*\)(?=\n|$)"), "\n"),

    # "Shop by category" text
    (re.compile(r"(?:^|\n)Shop by category(?=\n|$)"), "\n"),

    # "Include description" checkbox text
    (re.compile(r"(?:^|\n)Include description(?=\n|$)"), "\n"),

    # "Advanced" search link
    (re.compile(r"(?:^|\n)\[?Advanced\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # "Related:" search suggestions (standalone header)
    (re.compile(r"(?:^|\n)Related:(?=\n|$)"), "\n"),

    # "## Filter" section heading
    (re.compile(r"(?:^|\n)#{1,4}\s+Filter(?=\n|$)"), "\n"),

    # "Search" standalone (header search box)
    (re.compile(r"(?:^|\n)Search(?=\n|$)"), "\n"),

    # "Opens in a new window or tab" (accessibility text, sometimes survives CSS)
    (re.compile(r"Opens in a new window or tab"), ""),

    # "Tell us what you think" feedback link
    (re.compile(r"(?:^|\n)-?\s*\[Tell us what you think[^\]]*\]\([^\)]*\)(?=\n|$)"), "\n"),

    # "Items Per Page" lines (pagination controls)
    (re.compile(r"(?:^|\n)-?\s*(?:\[?\d+)?Items Per Page[^\n]*(?=\n|$)"), "\n"),

    # "Save this search" standalone
    (re.compile(r"(?:^|\n)Save this search(?=\n|$)"), "\n"),

    # "Customize" standalone
    (re.compile(r"(?:^|\n)Customize(?=\n|$)"), "\n"),

    # "Gallery View" link
    (re.compile(r"(?:^|\n)-?\s*\[?Gallery View\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # Sort controls: "Sort: Best Match"
    (re.compile(r"(?:^|\n)Sort:[^\n]*(?=\n|$)"), "\n"),

    # "## Related Searches" heading
    (re.compile(r"(?:^|\n)#{1,4}\s+Related Searches(?=\n|$)"), "\n"),

    # Footer pricing/currency disclaimer block
    (re.compile(
        r"(?:^|\n)\[?\*Learn about pricing\]?(?:\([^\)]*\))?"
        r"[\s\S]*?"
        r"(?:See each listing for international shipping[^\n]*)(?=\n|$)",
    ), "\n"),

    # "Feedback" standalone
    (re.compile(r"(?:^|\n)Feedback(?=\n|$)"), "\n"),

    # "Leave feedback about" link
    (re.compile(r"(?:^|\n)\[Leave feedback[^\]]*\]\([^\)]*\)(?=\n|$)"), "\n"),

    # Duplicate title line (eBay repeats page title in body: "X for sale | eBay")
    (re.compile(r"(?:^|\n)[^\n]+ for sale \| eBay(?=\n|$)"), "\n"),

    # Buying format / condition filter labels in search
    (re.compile(r"(?:^|\n)(?:Buying Format|Any Condition)[^\n]*(?:Filter Applied)?(?=\n|$)"), "\n"),

    # Category dropdown options (standalone All Categories line)
    (re.compile(r"(?:^|\n)All Categories[^\n]*(?=\n|$)"), "\n"),

    # Product page: "Have one to sell?" / "Sell one like this" / "Sell something else"
    (re.compile(r"(?:^|\n)Have one to sell\?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[Sell (?:one like this|something else)\]\([^\)]*\)(?=\n|$)"), "\n"),

    # Product page: "Share" standalone
    (re.compile(r"(?:^|\n)Share(?=\n|$)"), "\n"),

    # Product page: "show original title" / "Original Text"
    (re.compile(r"(?:^|\n)show original title(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Original Text(?=\n|$)"), "\n"),

    # Product page: "Picture N of M" heading (image gallery)
    (re.compile(r"(?:^|\n)#{1,4}\s+Picture \d+ of \d+(?=\n|$)", re.I), "\n"),

    # Product page: "Seller's other items" link
    (re.compile(r"(?:^|\n)\[Seller's other items\]\([^\)]*\)(?=\n|$)"), "\n"),

    # Product page: standalone large number (watchers, sold count leftover)
    (re.compile(r"(?:^|\n)\d{2,}(?=\n\n)"), "\n"),
]

# Replace JSONLD markers with clean content
_JSONLD_MARKER_RE = re.compile(
    r"__EBAY_JSONLD__([\s\S]*?)__EBAY_JSONLD__\n*"
)

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_ebay(markdown: str) -> str:
    """Clean up eBay-specific markdown noise."""
    # Extract and reformat JSON-LD marker
    m = _JSONLD_MARKER_RE.search(markdown)
    if m:
        jsonld_content = m.group(1).strip()
        markdown = markdown[:m.start()] + markdown[m.end():]
        # Inject structured data after first heading
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
