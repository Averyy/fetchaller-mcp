"""AliExpress-specific HTML cleanup, post-processing, and data extraction.

Exports the standard site interface (SELECTORS_LIST, is_aliexpress,
strip_aliexpress_junk, postprocess_aliexpress) plus:
- Product URL detection for fetch pipeline intercept
- Search data extraction from embedded ``_init_data_`` JSON
"""

import math
import re
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from ._json_extract import extract_json_object
from ._numeric import bounded_number_text
from ._price import has_positive_price

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
_MAX_SEARCH_PRODUCTS = 60
_MAX_SEARCH_OUTPUT_CHARS = 100_000
_MAX_QUERY_CHARS = 512
_MAX_TITLE_CHARS = 500
_MAX_PRICE_CHARS = 256
_MAX_METADATA_CHARS = 256
_MAX_INIT_DATA_CHARS = 2_000_000


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
                payload_chars = end_idx - json_start
                if payload_chars > _MAX_INIT_DATA_CHARS:
                    return None
                result = extract_json_object(
                    html,
                    json_start,
                    payload_chars,
                )
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
    return extract_json_object(html, json_start, _MAX_INIT_DATA_CHARS)


def _bounded_scalar(value: object, maximum: int) -> str:
    """Return one compact scalar, rejecting complex or oversized fields."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = " ".join(str(value).split())
    if not text or len(text) > maximum:
        return ""
    return text


def _search_product_title(product: dict) -> str:
    """Return a bounded plain title from current or legacy item-list fields."""

    value = product.get("title")
    if isinstance(value, dict):
        value = value.get("displayTitle") or value.get("seoTitle")
    return _bounded_scalar(value, _MAX_TITLE_CHARS)


def valid_search_product(product: object) -> bool:
    """Require a substantive, product-bound AliExpress search offer.

    Challenge and hydration shells can expose an ``itemList.content`` array
    containing placeholder dictionaries.  A real offer must bind a canonical
    product ID to a human title and a positive sale price.
    """

    if not isinstance(product, dict):
        return False
    product_id = product.get("productId")
    if not isinstance(product_id, (str, int)) or isinstance(product_id, bool):
        return False
    product_id = str(product_id)
    if re.fullmatch(r"\d{8,20}", product_id) is None:
        return False

    title = _search_product_title(product)
    if not title:
        return False
    if not any(character.isalpha() for character in title):
        return False

    prices = product.get("prices")
    if not isinstance(prices, dict):
        return False
    sale = prices.get("salePrice")
    if not isinstance(sale, dict):
        return False
    formatted_price = sale.get("formattedPrice")
    if formatted_price not in (None, ""):
        return has_positive_price(
            formatted_price,
            require_currency=True,
        )
    return has_positive_price(
        sale.get("minPrice"),
        require_currency=False,
    )


def valid_search_products(products: object) -> list[dict]:
    """Filter an embedded item list down to substantive bound offers."""

    if not isinstance(products, list):
        return []
    # AliExpress exposes at most 60 products for one page. Bound before field
    # validation so a hostile embedded list cannot amplify CPU or MCP output.
    return [
        product
        for product in products[:_MAX_SEARCH_PRODUCTS]
        if valid_search_product(product)
    ]


def search_product_snapshot(product: object) -> dict | None:
    """Return a bounded product-ID-bound snapshot from one valid search offer."""

    if not valid_search_product(product):
        return None
    assert isinstance(product, dict)
    product_id = str(product["productId"])
    prices = product["prices"]
    sale = prices["salePrice"]
    original = prices.get("originalPrice")
    original = original if isinstance(original, dict) else {}
    evaluation = product.get("evaluation")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    trade = product.get("trade")
    trade = trade if isinstance(trade, dict) else {}
    trade_description = _trade_description(trade.get("tradeDesc"))
    return {
        "_source": "search_listing",
        "product_id": product_id,
        "title": _search_product_title(product),
        "sale_price": _bounded_scalar(
            sale.get("formattedPrice") or sale.get("minPrice"),
            _MAX_PRICE_CHARS,
        ),
        "original_price": _bounded_scalar(
            original.get("formattedPrice"),
            _MAX_PRICE_CHARS,
        ),
        "discount": _discount_percentage(sale.get("discount")),
        "rating": bounded_number_text(
            evaluation.get("starRating"),
            minimum=0,
            maximum=5,
        ),
        "orders": trade_description.partition(" ")[0],
    }


def _format_search_product(idx: int, product: dict) -> str:
    """Format a single search result product."""
    lines = []

    title = _search_product_title(product)
    lines.append(f"{idx}. {title}")

    # Price. Use `or {}` (not just a default): the API sends explicit JSON null
    # for these fields on some listings, and `.get("prices", {})` only defaults
    # when the key is *absent* — a null would slip through and crash `.get()`.
    prices = product.get("prices")
    prices = prices if isinstance(prices, dict) else {}
    sale = prices.get("salePrice")
    sale = sale if isinstance(sale, dict) else {}
    original = prices.get("originalPrice")
    original = original if isinstance(original, dict) else {}
    price_str = _bounded_scalar(
        sale.get("formattedPrice") or sale.get("minPrice"),
        _MAX_PRICE_CHARS,
    )
    orig_str = _bounded_scalar(
        original.get("formattedPrice"),
        _MAX_PRICE_CHARS,
    )
    if not has_positive_price(orig_str, require_currency=True):
        orig_str = ""
    discount = _discount_percentage(sale.get("discount"))

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
    eval_mod = product.get("evaluation")
    eval_mod = eval_mod if isinstance(eval_mod, dict) else {}
    trade_mod = product.get("trade")
    trade_mod = trade_mod if isinstance(trade_mod, dict) else {}
    meta_parts = []
    star_rating = bounded_number_text(
        eval_mod.get("starRating"),
        minimum=0,
        maximum=5,
    )
    trade_description = _trade_description(
        trade_mod.get("tradeDesc"),
    )
    if star_rating:
        meta_parts.append(f"★{star_rating}")
    if trade_description:
        meta_parts.append(trade_description)
    if meta_parts:
        lines.append(f"   {' | '.join(meta_parts)}")

    # Product URL
    product_id = product.get("productId", "")
    if product_id:
        lines.append(f"   https://www.aliexpress.com/item/{product_id}.html")

    return "\n".join(lines)


def _trade_description(value: object) -> str:
    """Return a finite AliExpress sale-count description."""

    text = _bounded_scalar(value, _MAX_METADATA_CHARS)
    if not text:
        return ""
    match = re.fullmatch(
        r"(\d+(?:,\d{3})*)(?:\+)?\s+(?:sold|orders?)",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return ""
    if not bounded_number_text(
        match.group(1),
        minimum=0,
        maximum=1_000_000_000,
        integral=True,
        allow_grouping=True,
    ):
        return ""
    return text


def _discount_percentage(value: object) -> str:
    """Return a finite, unsigned percentage or omit malformed metadata."""

    if isinstance(value, bool):
        return ""
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str) and len(value) <= 32 and re.fullmatch(
            r"(?:\d+(?:\.\d+)?|\.\d+)",
            value.strip(),
        ):
            number = float(value)
        else:
            return ""
    except (OverflowError, ValueError):
        return ""
    if not math.isfinite(number) or not 0 < number <= 100:
        return ""
    return f"{number:g}"


def format_search_results(products: list[dict], query: str, page: int, total: int) -> str:
    """Format search results into numbered list."""
    products = valid_search_products(products)
    query_text = _bounded_scalar(query, _MAX_QUERY_CHARS) or "aliexpress"
    page_number = (
        page
        if isinstance(page, int) and not isinstance(page, bool) and page >= 1
        else 1
    )
    total_count = (
        min(total, 1_000_000_000)
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0
        else len(products)
    )
    header = f'Search: "{query_text}" | page {page_number} | {total_count} results'
    if not products:
        return f"{header}\n\nNo products found."

    prefix = f"{header}\n\n"
    formatted: list[str] = []
    length = len(prefix)
    for index, product in enumerate(products[:_MAX_SEARCH_PRODUCTS]):
        item = _format_search_product(
            index + 1 + (page_number - 1) * _MAX_SEARCH_PRODUCTS,
            product,
        )
        separator = 2 if formatted else 0
        if length + separator + len(item) > _MAX_SEARCH_OUTPUT_CHARS:
            marker = "[Additional products omitted to enforce the search output limit.]"
            while formatted:
                removed = formatted.pop()
                length -= len(removed) + (2 if formatted else 0)
                marker_separator = 2 if formatted else 0
                if (
                    length + marker_separator + len(marker)
                    <= _MAX_SEARCH_OUTPUT_CHARS
                ):
                    break
            formatted.append(marker)
            break
        formatted.append(item)
        length += separator + len(item)
    return prefix + "\n\n".join(formatted)


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

    products = valid_search_products(products)
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
