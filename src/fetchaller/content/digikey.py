"""DigiKey-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_digikey,
strip_digikey_junk, postprocess_digikey).

Covers all DigiKey TLDs (digikey.com, digikey.ca, digikey.co.uk, etc.).
Site is behind Akamai WAF (botfighter handles bypass). Pages are SSR
after bypass — just needs content cleanup.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_DIGIKEY_HOST_RE = re.compile(
    r"^(?:www\.)?digikey\."
    r"(?:com|ca|co\.uk|de|fr|it|es|nl|se|no|dk|fi|at|ch|be|ie|"
    r"com\.au|co\.nz|jp|kr|tw|sg|hk|in|co\.za)$"
)


def is_digikey(url: str) -> bool:
    """Check if URL is a DigiKey page."""
    hostname = (urlparse(url).hostname or "").lower()
    return bool(_DIGIKEY_HOST_RE.match(hostname))


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Site header (logo, search, cart, sign in)
    "#header",
    "[data-testid='header']",
    "header",
    # Site footer
    "#footer",
    "[data-testid='footer']",
    # Top navigation bar
    "#topnav",
    "[class*='top-nav']",
    # Category navigation / mega menu
    "[class*='mega-menu']",
    "[class*='megamenu']",
    # Cart / sign in prompts
    "[class*='minicart']",
    "[class*='sign-in']",
    # Cookie consent banner
    "#onetrust-banner-sdk",
    "[class*='cookie-banner']",
    # Filter sidebar (parametric search)
    "[class*='filter-panel']",
    "[class*='filterPanel']",
    "#filter-panel",
    # Comparison checkbox column
    "[class*='compare-checkbox']",
    "[class*='compareCheckbox']",
    # "Apply Filters" / "Clear Filters" buttons
    "[class*='filter-actions']",
    # Pagination controls
    "[class*='pagination']",
    # Feedback / survey widgets
    "[class*='feedback']",
    "[class*='survey']",
    # Ad blocks / promotional banners
    "[class*='promo-banner']",
    "[class*='promoBanner']",
    "[class*='ad-block']",
    # "Recently Viewed" section
    "[class*='recently-viewed']",
    # Breadcrumbs
    "[class*='breadcrumb']",
    # Environmental / compliance badges
    "[class*='compliance']",
    # Print-only elements
    ".print-only",
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_digikey_junk(soup: BeautifulSoup) -> None:
    """Remove DigiKey-specific junk that CSS selectors can't easily target."""
    # Remove all <input> elements (hidden form fields, search boxes)
    for el in list(soup.find_all("input")):
        el.decompose()

    # Remove all <select> elements (quantity pickers, sort dropdowns)
    for el in list(soup.find_all("select")):
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
    # NOTE: Use (?=\n|$) lookahead instead of consuming \n at pattern end.
    # This prevents consecutive lines from being missed when the same pattern
    # matches multiple adjacent lines (consuming \n makes it unavailable as
    # the leading \n for the next match).

    # "Sign In" / "Register" standalone lines
    (re.compile(r"(?:^|\n)\[?Sign In\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Register\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Create Account\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Add to Cart" / "Add to Order" buttons
    (re.compile(r"(?:^|\n)\[?Add to (?:Cart|Order)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Check Stock" / "Buy Now" buttons
    (re.compile(r"(?:^|\n)\[?(?:Check Stock|Buy Now)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "View All" / "Show More" links
    (re.compile(r"(?:^|\n)\[?(?:View All|Show More)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Comparison UI: "Compare" / "Add to Compare"
    (re.compile(r"(?:^|\n)\[?(?:Compare|Add to Compare)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Filter section headers
    (re.compile(r"(?:^|\n)\[?Apply Filters\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Clear Filters\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Part #" search prompt
    (re.compile(r"(?:^|\n)Enter part number or keyword(?=\n|$)", re.I), "\n"),

    # Environmental / export control notices (multi-line: keeps internal content, changes end)
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Export Control Notice[^\n]*(?=\n|$)", re.I), "\n"),

    # "Contact Us" / "Request Quote" standalone
    (re.compile(r"(?:^|\n)\[?(?:Contact Us|Request Quote)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
]

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_digikey(markdown: str) -> str:
    """Clean up DigiKey-specific markdown noise."""
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
