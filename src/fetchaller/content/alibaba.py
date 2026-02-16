"""Alibaba.com-specific HTML cleanup, post-processing, and data extraction.

Exports the standard site interface (SELECTORS_LIST, is_alibaba,
strip_alibaba_junk, postprocess_alibaba) plus:
- Product URL detection for fetch pipeline intercept
- Search URL detection for fetch pipeline intercept
- JSON extraction from SSR pages (window.detailData, window.__page__data_sse10)
"""

import re
from urllib.parse import urlparse

from ._json_extract import extract_json_object

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_ALIBABA_HOSTS = frozenset((
    "alibaba.com",
    "www.alibaba.com",
    "m.alibaba.com",
))


def is_alibaba(url: str) -> bool:
    """Check if URL is an Alibaba.com page."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in _ALIBABA_HOSTS:
        return True
    # Regional subdomains like spanish.alibaba.com, german.alibaba.com
    if hostname.endswith(".alibaba.com"):
        return True
    return False


# Product URL: /product-detail/{slug}_{productId}.html
_PRODUCT_PATH_RE = re.compile(r"/product-detail/.+?_(\d{5,20})\.html")


def extract_product_id_from_url(url: str) -> str | None:
    """Extract product ID from an Alibaba.com product URL.

    Returns the numeric product ID if the URL matches, or None.
    """
    if not is_alibaba(url):
        return None
    m = _PRODUCT_PATH_RE.search(urlparse(url).path)
    return m.group(1) if m else None


# Search URL: /trade/search?SearchText=...
def is_alibaba_search_url(url: str) -> bool:
    """Check if URL is an Alibaba.com search results page."""
    if not is_alibaba(url):
        return False
    parsed = urlparse(url)
    return parsed.path.rstrip("/") == "/trade/search"


# ---------------------------------------------------------------------------
# JSON extraction from SSR pages
# ---------------------------------------------------------------------------


def _extract_json_var(html: str, var_name: str) -> dict | None:
    """Extract a JS variable assignment as JSON from HTML.

    Finds `var_name = {...}` or `var_name = {...};` in script blocks
    and parses via brace counting. Handles deeply nested objects.
    """
    idx = html.find(var_name)
    if idx == -1:
        return None

    # Find the opening brace after the assignment
    eq_idx = html.find("=", idx + len(var_name))
    if eq_idx == -1 or eq_idx - idx > len(var_name) + 5:
        return None

    brace_start = html.find("{", eq_idx)
    if brace_start == -1 or brace_start - eq_idx > 10:
        return None

    return extract_json_object(html, brace_start)


def extract_search_data(html: str) -> dict | None:
    """Extract search data from Alibaba.com search page HTML.

    Alibaba initializes ``window.__page__data_sse10 = {}`` then populates
    properties in separate ``<script>`` blocks via SSE-style hydration:
    ``window.__page__data_sse10._offer_list = {...}``.
    """
    # Try direct property assignment first (how real pages work)
    offer_list = _extract_json_var(html, "window.__page__data_sse10._offer_list")
    if offer_list:
        return offer_list

    # Fallback: full object (for tests or future changes)
    page_data = _extract_json_var(html, "window.__page__data_sse10")
    if page_data:
        return page_data.get("_offer_list")

    return None


def extract_product_data(html: str) -> dict | None:
    """Extract product data from Alibaba.com product page HTML.

    Looks for window.detailData and returns the globalData object.
    """
    detail_data = _extract_json_var(html, "window.detailData")
    if not detail_data:
        return None

    return detail_data.get("globalData")


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Header / top navigation
    ".header",
    ".topbar",
    ".top-bar",
    "#J_SC_header",
    '[class*="header-wrapper"]',
    # Search bar
    '[class*="search-bar"]',
    '[class*="searchbar"]',
    # Category navigation
    '[class*="category-nav"]',
    # Cart / account
    ".my-alibaba",
    '[class*="mini-cart"]',
    '[class*="shopping-cart"]',
    # Login / signup
    '[class*="sign-in"]',
    '[class*="join-free"]',
    # Footer
    ".footer",
    ".site-footer",
    "#footer",
    '[class*="footer-"]',
    # Cookie / GDPR
    ".cookie-consent",
    ".gdpr-banner",
    # App download
    ".app-download",
    '[class*="download-app"]',
    # Floating elements
    '[class*="float-bar"]',
    '[class*="fixed-bar"]',
    '[class*="side-bar"]',
    # Trade assurance badge noise (keep text, remove badges)
    '[class*="ta-badge"]',
    # Chat / messaging widgets
    '[class*="chat-"]',
    '[class*="messenger"]',
    # RFQ (Request for Quotation) popups
    '[class*="rfq-"]',
    # Supplier verification badges (repetitive noise)
    '[class*="verify-badge"]',
]


# ---------------------------------------------------------------------------
# Soup-level cleanup
# ---------------------------------------------------------------------------


def strip_alibaba_junk(soup) -> None:
    """Remove Alibaba.com-specific junk that CSS selectors can't easily catch."""
    # Remove hidden inputs (form tokens, tracking)
    for inp in list(soup.find_all("input", type="hidden")):
        inp.decompose()

    # Remove tracking pixels (1x1 images)
    for img in list(soup.find_all("img")):
        w = img.get("width", "")
        h = img.get("height", "")
        if w == "1" and h == "1":
            img.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing
# ---------------------------------------------------------------------------

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# "Sign in" / "Join Free" standalone lines
_SIGN_IN_RE = re.compile(r"(?:^|\n)Sign in(?:\n|$)")
_JOIN_FREE_RE = re.compile(r"(?:^|\n)Join Free(?:\n|$)", re.IGNORECASE)

# "Get the app" / "Download the app" prompts
_DOWNLOAD_APP_RE = re.compile(r"(?:^|\n).{0,10}Download the.{0,30}app.{0,30}(?:\n|$)", re.IGNORECASE)

# Alibaba Group footer blocks
_ALIBABA_GROUP_RE = re.compile(
    r"(?:^|\n)Alibaba\.com Site:.*?(?:Product Listing Policy|Intellectual Property Protection)\n?",
    re.DOTALL,
)

# "Browse Alphabetically" index blocks
_BROWSE_ALPHA_RE = re.compile(
    r"(?:^|\n)Browse Alphabetically:.*?(?:\n\n|\Z)",
    re.DOTALL,
)

# Supplier service / Trade Assurance boilerplate (standalone noise lines)
_TRADE_ASSURANCE_RE = re.compile(r"(?:^|\n)Trade Assurance(?:\n|$)")

# "Alibaba.com - " prefix on titles
_TITLE_PREFIX_RE = re.compile(r"Alibaba\.com - ")


def postprocess_alibaba(markdown: str) -> str:
    """Strip Alibaba.com UI noise from markdown."""
    markdown = _SIGN_IN_RE.sub("\n", markdown)
    markdown = _JOIN_FREE_RE.sub("\n", markdown)
    markdown = _DOWNLOAD_APP_RE.sub("\n", markdown)
    markdown = _ALIBABA_GROUP_RE.sub("\n", markdown)
    markdown = _BROWSE_ALPHA_RE.sub("\n", markdown)
    markdown = _TRADE_ASSURANCE_RE.sub("\n", markdown)
    markdown = _TITLE_PREFIX_RE.sub("", markdown)

    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
