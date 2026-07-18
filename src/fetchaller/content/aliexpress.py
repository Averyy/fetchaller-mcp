"""AliExpress-specific HTML cleanup, post-processing, and data extraction.

Exports the standard site interface (SELECTORS_LIST, is_aliexpress,
strip_aliexpress_junk, postprocess_aliexpress) plus:
- Product URL detection for fetch pipeline intercept
- Search data extraction from embedded ``_init_data_`` JSON
"""

import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from ._json_extract import extract_json_object

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

# AliExpress uses many regional TLDs
_ALIEXPRESS_HOSTS = frozenset((
    "aliexpress.com",
    "www.aliexpress.com",
    "m.aliexpress.com",
    "aliexpress.ru",
    "www.aliexpress.ru",
    "aliexpress.us",
    "www.aliexpress.us",
))


def is_aliexpress(url: str) -> bool:
    """Check if URL is an AliExpress page."""
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in _ALIEXPRESS_HOSTS:
        return True
    # Catch regional subdomains like ko.aliexpress.com, pt.aliexpress.com
    if hostname.endswith(".aliexpress.com") or hostname.endswith(".aliexpress.ru"):
        return True
    return False


# Product URL: /item/1005006367324382.html
_PRODUCT_PATH_RE = re.compile(r"/item/(\d{8,20})(?:\.html)?")


def extract_product_id_from_url(url: str) -> str | None:
    """Extract product ID from an AliExpress product URL.

    Returns the numeric product ID if the URL matches, or None.
    Only matches AliExpress hostnames (won't match bare numeric strings).
    """
    if not is_aliexpress(url):
        return None
    m = _PRODUCT_PATH_RE.search(urlparse(url).path)
    return m.group(1) if m else None


# Search URL: /w/wholesale-*.html
_SEARCH_PATH_RE = re.compile(r"/w/wholesale-(.+?)\.html")


def is_aliexpress_search_url(url: str) -> bool:
    """Check if URL is an AliExpress search results page."""
    if not is_aliexpress(url):
        return False
    return bool(_SEARCH_PATH_RE.search(urlparse(url).path))


# ---------------------------------------------------------------------------
# Search data extraction (from embedded _init_data_ JSON)
# ---------------------------------------------------------------------------


def extract_init_data(html: str) -> dict | None:
    """Extract _init_data_ JSON from AliExpress page HTML.

    Uses brace counting to find the JSON boundary since regex is unreliable
    with 400KB+ payloads.
    """
    # Strategy 1: Comment markers (most reliable)
    start_idx = html.find("init-data-start")
    end_idx = html.find("init-data-end")
    if start_idx != -1 and end_idx != -1:
        data_offset = html.find("data:", start_idx)
        if data_offset != -1 and data_offset < end_idx:
            json_start = html.find("{", data_offset + 5)
            if json_start != -1 and json_start < end_idx:
                result = extract_json_object(html, json_start, end_idx - json_start + 100)
                if result is not None:
                    return result

    # Strategy 2: Direct assignment (no comment markers)
    assign_idx = html.find("_dida_config_._init_data_=")
    if assign_idx == -1:
        return None
    data_idx = html.find("data:", assign_idx + 26)
    if data_idx == -1 or data_idx - assign_idx > 50:
        return None
    json_start = html.find("{", data_idx + 5)
    if json_start == -1:
        return None
    return extract_json_object(html, json_start, 2_000_000)


def _format_search_product(idx: int, product: dict) -> str:
    """Format a single search result product."""
    lines = []

    title = ""
    title_mod = product.get("title", {})
    if isinstance(title_mod, dict):
        title = title_mod.get("displayTitle") or title_mod.get("seoTitle", "")
    elif isinstance(title_mod, str):
        title = title_mod

    lines.append(f"{idx}. {title}")

    # Price. Use `or {}` (not just a default): the API sends explicit JSON null
    # for these fields on some listings, and `.get("prices", {})` only defaults
    # when the key is *absent* — a null would slip through and crash `.get()`.
    prices = product.get("prices") or {}
    sale = prices.get("salePrice") or {}
    original = prices.get("originalPrice") or {}
    price_str = sale.get("formattedPrice") or sale.get("minPrice", "")
    orig_str = original.get("formattedPrice", "")
    discount = sale.get("discount", "")

    price_parts = []
    if price_str:
        price_parts.append(price_str)
    if orig_str:
        price_parts.append(f"(was {orig_str})")
    if discount:
        price_parts.append(f"-{discount}%")
    if price_parts:
        lines.append(f"   Price: {' '.join(price_parts)}")

    # Rating & orders
    eval_mod = product.get("evaluation") or {}
    trade_mod = product.get("trade") or {}
    meta_parts = []
    if eval_mod.get("starRating"):
        meta_parts.append(f"★{eval_mod['starRating']}")
    if trade_mod.get("tradeDesc"):
        meta_parts.append(trade_mod["tradeDesc"])
    if meta_parts:
        lines.append(f"   {' | '.join(meta_parts)}")

    # Product URL
    product_id = product.get("productId", "")
    if product_id:
        lines.append(f"   https://www.aliexpress.com/item/{product_id}.html")

    return "\n".join(lines)


def format_search_results(products: list[dict], query: str, page: int, total: int) -> str:
    """Format search results into numbered list."""
    header = f'Search: "{query}" | page {page} | {total} results'
    if not products:
        return f"{header}\n\nNo products found."

    formatted = [_format_search_product(i + 1 + (page - 1) * 60, p) for i, p in enumerate(products)]
    return f"{header}\n\n" + "\n\n".join(formatted)


def extract_search_products(html: str, url: str) -> str | None:
    """Extract structured product list from AliExpress search page HTML.

    Called by the fetch pipeline when an AliExpress search URL is detected.
    Returns formatted text or None if extraction fails.
    """
    init_data = extract_init_data(html)
    if not init_data:
        return None

    try:
        root_fields = init_data["data"]["root"]["fields"]
        mods = root_fields.get("mods", {})
        item_list = mods.get("itemList", {})
        products = item_list.get("content", [])
        page_info = root_fields.get("pageInfo", {})
        total = page_info.get("totalResults", len(products))
        page = page_info.get("page", 1)
    except (KeyError, TypeError):
        return None

    if not products:
        return None

    # Extract query from URL path: /w/wholesale-{query}.html
    query = "aliexpress"
    m = _SEARCH_PATH_RE.search(urlparse(url).path)
    if m:
        query = unquote(m.group(1).replace("-", " "))

    return format_search_results(products, query, page, total)


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Header / top navigation
    ".header--header",
    ".snow-header",
    ".top-header",
    "#nav-global",
    ".navigation--nav",
    # Search bar
    ".search-bar",
    ".searchbar-input",
    # Category sidebar
    ".category-list",
    ".cate-list",
    # Cart / account
    ".my-account",
    ".shopping-cart",
    ".mini-cart",
    # Login / signup prompts
    ".login-container",
    ".register-container",
    '[class*="sign-in"]',
    '[class*="signIn"]',
    # Footer
    ".footer",
    ".site-footer",
    "#footer",
    ".footer-content",
    # Cookie consent
    ".cookie-consent",
    ".gdpr-banner",
    # App download banners
    ".app-download",
    ".download-app",
    '[class*="appBanner"]',
    # Sponsored / ads
    '[class*="sponsored"]',
    '[class*="Sponsored"]',
    '[class*="ad-slot"]',
    # Floating elements
    ".float-bar",
    ".fixed-bar",
    '[class*="floatBar"]',
    # Feedback / report
    '[class*="reportItem"]',
    # Wishlist / share buttons
    '[class*="wish-list"]',
    '[class*="share-"]',
]


# ---------------------------------------------------------------------------
# Soup-level cleanup
# ---------------------------------------------------------------------------


def strip_aliexpress_junk(soup: BeautifulSoup) -> None:
    """Remove AliExpress-specific junk that CSS selectors can't easily catch."""
    # Remove all hidden inputs (form tokens, tracking data)
    for inp in list(soup.find_all("input", type="hidden")):
        inp.decompose()

    # Remove tracking pixel images (1x1)
    for img in list(soup.find_all("img")):
        w = img.get("width", "")
        h = img.get("height", "")
        if w == "1" and h == "1":
            img.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing
# ---------------------------------------------------------------------------

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# "Sign in" / "Join" standalone lines
_SIGN_IN_RE = re.compile(r"(?:^|\n)Sign in(?:\n|$)")
_JOIN_RE = re.compile(r"(?:^|\n)Join(?:\n|$)")

# "Download the AliExpress app" / "Get the app" type prompts
_DOWNLOAD_APP_RE = re.compile(r"(?:^|\n).{0,10}Download the AliExpress app.{0,30}(?:\n|$)", re.IGNORECASE)
_GET_APP_RE = re.compile(r"(?:^|\n)Get the app(?:\n|$)", re.IGNORECASE)

# Ship to / currency selector noise
_SHIP_TO_RE = re.compile(r"(?:^|\n)Ship to\n.{0,50}(?:\n|$)")
_CURRENCY_RE = re.compile(r"(?:^|\n)[A-Z]{3}\n(?:EUR|USD|GBP|CAD|AUD|RUB|BRL)\n")

# Footer: "Alibaba Group" and related
_ALIBABA_GROUP_RE = re.compile(
    r"(?:^|\n)Alibaba Group.*?(?:Intellectual Property Protection|AliExpress Multi-Language Sites|Browse by Category)\n?",
    re.DOTALL,
)

# "Buyer Protection" boilerplate
_BUYER_PROTECTION_RE = re.compile(
    r"(?:^|\n)Buyer Protection\n.*?(?:Learn more|learn more)(?:\n|$)",
    re.DOTALL,
)

# Empty product image alt text lines
_IMG_ALT_RE = re.compile(r"(?:^|\n)!\[\]\(https://ae\d+\.alicdn\.com/[^\)]+\)(?:\n|$)")


def postprocess_aliexpress(markdown: str) -> str:
    """Strip AliExpress UI noise from markdown."""
    markdown = _SIGN_IN_RE.sub("\n", markdown)
    markdown = _JOIN_RE.sub("\n", markdown)
    markdown = _DOWNLOAD_APP_RE.sub("\n", markdown)
    markdown = _GET_APP_RE.sub("\n", markdown)
    markdown = _SHIP_TO_RE.sub("\n", markdown)
    markdown = _CURRENCY_RE.sub("\n", markdown)
    markdown = _ALIBABA_GROUP_RE.sub("\n", markdown)
    markdown = _BUYER_PROTECTION_RE.sub("\n", markdown)
    markdown = _IMG_ALT_RE.sub("\n", markdown)

    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
