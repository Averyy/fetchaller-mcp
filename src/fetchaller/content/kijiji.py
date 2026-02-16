"""Kijiji-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_kijiji,
strip_kijiji_junk, postprocess_kijiji).

Covers kijiji.ca. Pages are SSR with no bot protection. Main noise:
header/search bar, category sidebar, sponsored labels, filter UI,
safety tips, and "View more" overlay text.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_KIJIJI_HOSTS = frozenset({
    "kijiji.ca", "www.kijiji.ca",
})


def is_kijiji(url: str) -> bool:
    """Check if URL is a Kijiji page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in _KIJIJI_HOSTS


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Main header (logo, search bar, post button, sign in)
    "#MainHeader",
    "[data-testid='header']",
    # Search bar component
    "#SearchBar",
    "[data-testid='search-bar']",
    # SEO footer (city links, category links)
    ".seo-footer",
    "[class*='seoFooter']",
    # Footer
    "#PageFooter",
    "[data-testid='footer']",
    # Related searches section
    "[class*='relatedSearches']",
    "[data-testid='related-searches']",
    # Save search / alert buttons
    "[class*='saveSearch']",
    "[data-testid='save-search']",
    # Ad/promo blocks
    "[data-testid='sponsored-ad']",
    "[class*='adBanner']",
    "[class*='top-feature']",
    # Category filter sidebar
    "[class*='filtersSidebar']",
    "[data-testid='filters-sidebar']",
    # Sort/filter bar
    "[class*='sortFilter']",
    "[data-testid='sort-filter']",
    # Login/registration modals
    "[class*='loginModal']",
    "[data-testid='login-modal']",
    # Cookie consent
    "[class*='cookieConsent']",
    # Breadcrumbs
    "[class*='breadcrumb']",
    # Safety tips panel
    "[class*='safetyTips']",
    "[data-testid='safety-tips']",
    # Map view toggle
    "[class*='mapToggle']",
    # Download app banner
    "[class*='appBanner']",
    "[data-testid='app-banner']",
    # "Post your ad" CTA
    "[class*='postAdButton']",
    # Pagination
    "[class*='pagination']",
    "[data-testid='pagination']",
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_kijiji_junk(soup: BeautifulSoup) -> None:
    """Remove Kijiji-specific junk that CSS selectors can't easily target."""
    # Remove all <input> elements (hidden form fields)
    for el in list(soup.find_all("input")):
        el.decompose()

    # Remove all <select> elements (filter dropdowns)
    for el in list(soup.find_all("select")):
        el.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # NOTE: Single-line patterns use (?=\n|$) lookahead to avoid consuming
    # trailing \n (prevents greedy \n consumption bug on consecutive lines).

    # "Register" / "Sign In" / "Post" header nav items
    (re.compile(r"(?:^|\n)(?:Register|Sign In|Post)(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)RegisterorSign In(?=\n|$)"), "\n"),

    # "FR" language toggle and "Canada" country label
    (re.compile(r"(?:^|\n)FR(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Canada(?=\n|$)"), "\n"),

    # "Search" standalone
    (re.compile(r"(?:^|\n)Search(?=\n|$)"), "\n"),

    # "Notify me when new ads are posted"
    (re.compile(r"(?:^|\n)Notify me when new ads are posted(?=\n|$)"), "\n"),

    # Filter labels: "Price", "For Sale By", "Price type", "All Filters"
    (re.compile(r"(?:^|\n)(?:Price|For Sale By|Price type|All Filters)(?=\n|$)"), "\n"),

    # "List View" toggle (sometimes repeated)
    (re.compile(r"(?:^|\n)List View(?=\n|$)"), "\n"),

    # "Sponsored" labels
    (re.compile(r"(?:^|\n)\s*Sponsored(?=\n|$)"), "\n"),

    # "View more" overlay text on listing cards
    (re.compile(r"(?:^|\n)\s*View more(?=\n|$)"), "\n"),

    # "Save" / "Share" / "Reveal phone number" action buttons on listing pages
    (re.compile(r"(?:^|\n)(?:Save|Share)(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Reveal phone number(?=\n|$)"), "\n"),

    # "Business" seller type label
    (re.compile(r"(?:^|\n)Business(?=\n|$)"), "\n"),

    # Results count header: "Results 1 - 40 of 147,779" or "147,779 results"
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Results \d+[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?[\d,]+ results(?=\n|$)"), "\n"),

    # "Popular:" section header
    (re.compile(r"(?:^|\n)Popular:(?=\n|$)"), "\n"),

    # Category sidebar: "All Categories" with counts
    (re.compile(r"(?:^|\n)-\s*All Categories(?=\n|$)"), "\n"),

    # "Category" standalone heading
    (re.compile(r"(?:^|\n)Category(?=\n|$)"), "\n"),

    # "Location" standalone heading followed by city name
    (re.compile(r"(?:^|\n)Location(?=\n|$)"), "\n"),

    # Safety tips boilerplate (multi-line block match — consumes \n explicitly)
    (re.compile(
        r"(?:^|\n)\s*(?:#{1,4}\s+)?Safety Tips\n"
        r"[\s\S]*?"
        r"(?:Report this ad|suspicious activity)[^\n]*(?=\n|$)",
    ), "\n"),

    # "Report this ad" standalone
    (re.compile(r"(?:^|\n)\[?Report this ad\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # Duplicate listing images (same image repeated with "View more" text)
    (re.compile(r"(\d+) / (\d+)\n"), ""),

    # "Please Contact" price placeholder
    # (keep — it's meaningful that no price is listed)
]

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_kijiji(markdown: str) -> str:
    """Clean up Kijiji-specific markdown noise."""
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
