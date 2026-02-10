"""Wikipedia-specific HTML cleanup.

Exports the standard site interface (SELECTORS_LIST, is_wikipedia).
"""

from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_wikipedia(url: str) -> bool:
    """Check if URL is a Wikipedia page."""
    hostname = urlparse(url).hostname or ""
    return hostname.endswith(".wikipedia.org")


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    ".mw-editsection",
    "#p-lang-btn",
    ".interlanguage-link",
    ".catlinks",
    ".navbox",
    ".sistersitebox",
    ".ambox",
    "#coordinates",
    ".noprint",
    ".reflist",
    ".mw-jump-link",
    "#toc",
    ".mw-indicators",
    # Dropdown menu labels (Main menu, Appearance, Personal tools, language picker)
    ".vector-dropdown-label",
    # Sticky header (duplicate title + language picker + Add topic)
    ".vector-sticky-header",
    # Site header (logo, search, personal tools)
    ".vector-header",
    # Left sidebar (Main page, Contents, Current events, etc.)
    ".vector-column-start",
    # Footer icons (Wikimedia/MediaWiki logos)
    "#footer-icons",
    # Sister project links (Commons, Wikiquote, Wikibooks, etc.)
    ".sister-logo",
    ".portalbox",
]
