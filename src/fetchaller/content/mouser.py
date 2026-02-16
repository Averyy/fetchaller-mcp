"""Mouser-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_mouser,
strip_mouser_junk, postprocess_mouser).

Covers all Mouser TLDs (mouser.com, mouser.ca, mouser.co.uk, etc.).
Site is behind Akamai WAF (botfighter handles bypass). Pages are SSR
after bypass — just needs content cleanup.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_MOUSER_HOST_RE = re.compile(
    r"^(?:www\.)?mouser\."
    r"(?:com|ca|co\.uk|de|fr|it|es|nl|se|no|dk|fi|at|ch|be|ie|"
    r"com\.au|co\.nz|jp|kr|tw|sg|hk|in|co\.za|com\.br|com\.mx)$"
)


def is_mouser(url: str) -> bool:
    """Check if URL is a Mouser page."""
    hostname = (urlparse(url).hostname or "").lower()
    return bool(_MOUSER_HOST_RE.match(hostname))


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Site header
    "#header",
    "[id*='ctl00_header']",
    "header",
    # Site footer
    "#footer",
    "[id*='ctl00_footer']",
    # Top navigation bar
    "#topnav",
    "[class*='top-nav']",
    # Category navigation / mega menu
    "[class*='mega-menu']",
    "[class*='megamenu']",
    # Cart / sign in
    "[class*='minicart']",
    "[class*='sign-in']",
    # Cookie consent / privacy banner (Akamai remnants)
    "#onetrust-banner-sdk",
    "[class*='cookie-banner']",
    "[class*='privacy-banner']",
    # Filter sidebar
    "[class*='filter-panel']",
    "[class*='filterPanel']",
    "#filter-panel",
    # Comparison UI
    "[class*='compare-checkbox']",
    "[class*='compareCheckbox']",
    # Pagination
    "[class*='pagination']",
    # Feedback / survey
    "[class*='feedback']",
    "[class*='survey']",
    # Ad / promotional banners
    "[class*='promo-banner']",
    "[class*='promoBanner']",
    "[class*='ad-block']",
    # "Recently Viewed"
    "[class*='recently-viewed']",
    # Breadcrumbs
    "[class*='breadcrumb']",
    # Environmental info bar
    "[class*='environmental']",
    # Print-only
    ".print-only",
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_mouser_junk(soup: BeautifulSoup) -> None:
    """Remove Mouser-specific junk that CSS selectors can't easily target."""
    # Remove all <input> elements
    for el in list(soup.find_all("input")):
        el.decompose()

    # Remove all <select> elements
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

    # "Add to Cart" / "Add to BOM" buttons
    (re.compile(r"(?:^|\n)\[?Add to (?:Cart|BOM|Order)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Buy Now" / "Check Stock" buttons
    (re.compile(r"(?:^|\n)\[?(?:Buy Now|Check Stock)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "View All" / "Show More" links
    (re.compile(r"(?:^|\n)\[?(?:View All|Show More)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Comparison UI
    (re.compile(r"(?:^|\n)\[?(?:Compare|Add to Compare)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Filter buttons
    (re.compile(r"(?:^|\n)\[?Apply Filters\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Clear Filters\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # "Contact Us" / "Request Quote"
    (re.compile(r"(?:^|\n)\[?(?:Contact Us|Request Quote)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Search placeholder text (multi-line: keeps internal content, changes end)
    (re.compile(r"(?:^|\n)Search by part number[^\n]*(?=\n|$)", re.I), "\n"),

    # "Free shipping" promotional lines (multi-line: keeps internal content, changes end)
    (re.compile(r"(?:^|\n)Free shipping[^\n]*(?=\n|$)", re.I), "\n"),

    # Mouser part # prefix noise (standalone)
    (re.compile(r"(?:^|\n)Mouser Part #:\s*(?=\n|$)"), "\n"),

    # "In Stock" / availability as link noise (keep the text, strip link wrapper)
    # Actually, availability info is useful — keep it

    # "EDA / CAD Models" download noise
    (re.compile(r"(?:^|\n)\[?(?:Download|EDA / CAD Models)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
]

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_mouser(markdown: str) -> str:
    """Clean up Mouser-specific markdown noise."""
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
