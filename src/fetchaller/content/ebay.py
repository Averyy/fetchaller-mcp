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


def is_ebay_search_url(url: str) -> bool:
    """Check if URL is an eBay search results page (/sch/)."""
    if not is_ebay(url):
        return False
    return "/sch/" in urlparse(url).path


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

                    # itemCondition/availability are usually schema.org URL
                    # strings but can arrive as nested objects — guard the type
                    # before .rsplit so a dict/list doesn't crash extraction.
                    condition = offer.get("itemCondition", "")
                    if isinstance(condition, str) and condition:
                        # Extract human-readable from URL like "NewCondition"
                        cond_label = condition.rsplit("/", 1)[-1].replace("Condition", "")
                        lines.append(f"**Condition:** {cond_label}")

                    avail = offer.get("availability", "")
                    if isinstance(avail, str) and avail:
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
# Search result extraction (runs BEFORE CSS selectors fire)
# ---------------------------------------------------------------------------

_SEARCH_MARKER = "__EBAY_SEARCH__"


def extract_ebay_search_results(soup: BeautifulSoup, url: str) -> None:
    """Extract structured search results from eBay search page DOM.

    Uses a class-agnostic approach: finds ``<li>`` elements inside
    ``ul.srp-results`` that contain ``/itm/`` links (the stable eBay URL
    pattern), then extracts title/price/condition/shipping using both
    class-based selectors and content heuristics. This survives eBay's
    frequent CSS class renames.

    Called from clean_html() before CSS selectors fire.
    """
    # Find result items: li elements inside srp-results with /itm/ links
    srp = soup.select_one("ul.srp-results")
    if not srp:
        return

    listing_items = []
    for li in srp.find_all("li", recursive=False):
        if li.find("a", href=lambda h: h and "/itm/" in h):
            listing_items.append(li)

    if not listing_items:
        return

    results = _extract_generic_results(listing_items)

    if not results:
        return

    # Get total result count from page heading
    total_str = ""
    for sel in (".srp-controls__count-heading", "h1.srp-controls__count-heading", "h2"):
        count_el = soup.select_one(sel)
        if count_el:
            text = count_el.get_text(strip=True)
            m = re.match(r"([\d,]+)", text)
            if m:
                total_str = m.group(1)
                break

    # Build header
    header = "eBay Search Results"
    if total_str:
        header += f" | {total_str} results"

    content = f"{header}\n\n" + "\n\n".join(results)

    body = soup.find("body")
    if body is not None:
        marker = soup.new_tag("div", id="ebay-search-marker")
        marker.string = _SEARCH_MARKER + content + _SEARCH_MARKER
        body.insert(0, marker)


# Price pattern: a currency symbol/code followed by digits.
# Covers $, £, €, C $, AU $, and EUR/GBP/USD prefix formats.
# A currency indicator is REQUIRED — previously both the symbol and code were
# optional, so bare numbers ("2024", "12", "1,234") matched as prices in the
# class-agnostic search fallback (item numbers, quantities, years leaked in).
_PRICE_RE = re.compile(
    r"^(?:"
    r"[A-Z]{0,3}\s*[£€$¥]"  # currency symbol, optional leading region/code (C $, AU $, $)
    r"|[A-Z]{2,3}\s+"       # or a bare currency-code prefix (EUR 15,00, USD 12.00)
    r")"
    r"\s*[\d.,]+(?:\s*(?:to|bis|-)\s*(?:[A-Z]{2,3}\s+)?[£€$¥]?[\d.,]+)?$"
)

# Known eBay condition labels (English + common locales)
_CONDITIONS = frozenset({
    "new", "brand new", "new (other)", "new with defects", "new with tags",
    "new without tags", "open box", "certified refurbished", "refurbished",
    "excellent - refurbished", "very good - refurbished", "good - refurbished",
    "seller refurbished", "like new", "pre-owned", "used", "for parts or not working",
    "parts only",
    # German
    "neu", "gebraucht", "vom verkäufer generalüberholt", "als ersatzteil / defekt",
    # French
    "neuf", "occasion", "reconditionné",
    # Italian
    "nuovo", "usato", "ricondizionato",
    # Spanish
    "nuevo", "usado", "reacondicionado",
})

# Keywords that identify shipping/delivery text (English + common locales)
_SHIPPING_KEYWORDS = (
    "delivery", "shipping", "postage", "pickup", "free",
    "livraison", "versand", "spedizione", "envío", "verzending",  # localized
    "lieferung", "fracht",  # German
)

# Title text patterns to skip (ads, promotions, non-listing cards)
_SKIP_TITLES = frozenset({"shop on ebay", "results matching fewer words"})

# Accessibility text eBay appends to link/title text (all locales)
_ACCESSIBILITY_RE = re.compile(
    r"(?:"
    r"Opens in a new (?:window|tab)(?:\s+or\s+(?:window|tab))?"  # English
    r"|Wird in neuem Fenster oder Tab ge[öo]ffnet"  # German
    r"|S'ouvre dans une nouvelle fen[eê]tre ou un nouvel onglet"  # French
    r"|Si apre in una nuova finestra o scheda"  # Italian
    r"|Se abre en una nueva ventana o pesta[ñn]a"  # Spanish
    r"|Opens in een nieuw venster of tabblad"  # Dutch
    r"|Otwiera si[ęe] w nowym oknie lub karcie"  # Polish
    r")",
    re.I,
)

# "New Listing" label prefixed to recently listed items (all locales)
_NEW_LISTING_RE = re.compile(
    r"^(?:"
    r"New [Ll]isting"  # English
    r"|Neues Angebot"  # German
    r"|Nouvelle annonce"  # French
    r"|Nuovo [Ii]nserzione"  # Italian
    r"|Nuevo anuncio"  # Spanish
    r"|Nieuwe advertentie"  # Dutch
    r"|Nowa oferta"  # Polish
    r")",
)


def _is_leaf_element(el) -> bool:
    """Check if element is leaf-ish (no block-level children)."""
    return not el.find(["div", "ul", "ol", "table", "section", "article"])


def _extract_generic_results(items: list) -> list[str]:
    """Extract results from eBay search items using class-agnostic heuristics.

    For each ``<li>`` item, extracts:
    - **URL**: from the ``a[href*='/itm/']`` link (most stable anchor)
    - **Title**: link text or first heading, with accessibility noise stripped
    - **Price**: element whose text matches ``$NNN.NN`` pattern, or class-based
    - **Condition**: element matching known condition labels, or class-based
    - **Shipping**: element containing delivery/shipping keywords
    """
    results: list[str] = []
    for item in items:
        # URL — the /itm/ link is the most stable eBay pattern
        link_el = item.find("a", href=lambda h: h and "/itm/" in h)
        if not link_el:
            continue
        item_url = link_el.get("href", "")
        if "?" in item_url:
            item_url = item_url.split("?")[0]

        # Title — try class-based selectors first, fall back to link text
        title = ""
        for sel in (".s-card__title", ".s-item__title"):
            title_el = item.select_one(sel)
            if title_el:
                title = title_el.get_text(strip=True)
                break
        if not title:
            # Fall back to the /itm/ link's own text
            title = link_el.get_text(strip=True)
        # Strip accessibility noise (all eBay locales)
        title = _ACCESSIBILITY_RE.sub("", title).strip()
        # Strip "New Listing" / "Neues Angebot" etc. prefix
        title = _NEW_LISTING_RE.sub("", title).strip()
        if not title or title.lower() in _SKIP_TITLES:
            continue

        # Price — try class-based, fall back to $-pattern heuristic
        price = ""
        for sel in (".s-card__price", ".s-item__price"):
            price_el = item.select_one(sel)
            if price_el:
                price = price_el.get_text(strip=True)
                break
        if not price:
            for el in item.find_all(["span", "div"]):
                if not _is_leaf_element(el):
                    continue
                text = el.get_text(strip=True)
                if _PRICE_RE.match(text):
                    price = text
                    break

        # Condition — try class-based, fall back to known labels
        condition = ""
        for sel in (".s-card__subtitle", ".SECONDARY_INFO"):
            cond_el = item.select_one(sel)
            if cond_el:
                condition = cond_el.get_text(strip=True)
                break
        if not condition:
            for el in item.find_all(["span", "div"]):
                if not _is_leaf_element(el):
                    continue
                text = el.get_text(strip=True)
                if text.lower() in _CONDITIONS:
                    condition = text
                    break

        # Shipping — try class-based, fall back to keyword heuristic
        shipping = ""
        for sel in (".s-item__shipping", ".s-item__freeXDays"):
            ship_el = item.select_one(sel)
            if ship_el:
                shipping = ship_el.get_text(strip=True)
                break
        if not shipping:
            for el in item.find_all(["span", "div"]):
                if not _is_leaf_element(el):
                    continue
                text = el.get_text(strip=True)
                lower = text.lower()
                if any(kw in lower for kw in _SHIPPING_KEYWORDS) and len(text) < 80:
                    shipping = text
                    break

        # Build formatted line
        idx = len(results) + 1
        parts: list[str] = []
        if price:
            parts.append(price)
        if condition:
            parts.append(condition)
        if shipping:
            parts.append(shipping)

        line = f"{idx}. {title}"
        if parts:
            line += f"\n   {' | '.join(parts)}"
        if item_url:
            line += f"\n   {item_url}"
        results.append(line)

    return results


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

    # -----------------------------------------------------------------------
    # Global header/nav noise
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?Sign in\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Register\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?eBay Home\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[Skip to main content\]\([^\)]*\)(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Shop by category(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Expand Cart(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Image gallery noise (product pages)
    # -----------------------------------------------------------------------
    # "Picture N of M" text (standalone or heading)
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Picture \d+ of \d+(?=\n|$)", re.I), "\n"),
    # "Opens image gallery" text
    (re.compile(r"(?:^|\n)Opens image gallery(?=\n|$)"), "\n"),
    # Image gallery: repeated thumbnail images (![Picture N of M](url))
    (re.compile(r"(?:^|\n)!\[Picture \d+ of \d+\]\([^\)]+\)(?=\n|$)"), "\n"),
    # Inline concatenated gallery thumbnails (multiple on one line)
    (re.compile(r"!\[Picture \d+ of \d+\]\([^\)]+\)"), ""),
    # "Gallery" standalone text
    (re.compile(r"(?:^|\n)Gallery(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Action buttons and links
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)-?\s*\[?Add to (?:Watchlist|cart)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Buy It Now\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Make Offer\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Place bid\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Report this item[^\]]*\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?(?:Back to home page|Return to top)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?See other items\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Contact seller\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Save seller\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?See all\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[Seller's other items\]\([^\)]*\)(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Social proof / urgency (product pages)
    # -----------------------------------------------------------------------
    # "In N carts" / "N active offers"
    (re.compile(r"(?:^|\n)(?:In )?\d+ (?:active offers?|carts?)(?=\n|$)", re.I), "\n"),
    # "This one's trending. N have already sold."
    (re.compile(r"(?:^|\n)This one.s trending\.[^\n]*(?=\n|$)"), "\n"),
    # "People want this. N people are watching this."
    (re.compile(r"(?:^|\n)People want this\.[^\n]*(?=\n|$)"), "\n"),
    # "N have added this to their watchlist."
    (re.compile(r"(?:^|\n)(?:People are checking this out\.\s*)?[\d,]+ have added this to their watchlist\.?(?=\n|$)"), "\n"),
    # "N sold" standalone
    (re.compile(r"(?:^|\n)\d+ sold(?=\n|$)"), "\n"),
    # "N available N sold" (missing space — eBay concatenates)
    (re.compile(r"(?:^|\n)\d+ available\d+ sold(?=\n|$)"), "\n"),
    # "Quantity:" followed by availability lines
    (re.compile(r"(?:^|\n)Quantity:(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Price explanation noise
    # -----------------------------------------------------------------------
    (re.compile(r"What does this price mean\?"), ""),  # inline or standalone
    (re.compile(r"(?:^|\n)Recent sales price provided by the seller(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Price details(?=\n|$)"), "\n"),
    # "Ask a question" link
    (re.compile(r"(?:^|\n)\[?Ask a question\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # SKU / variant selection noise
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)Most popular(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Please select a[^\n]*(?=\n|$)"), "\n"),
    # "Select X:Select" header and standalone "Select" lines
    (re.compile(r"(?:^|\n)Select [^:\n]+:Select(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Select(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Sold listing noise
    # -----------------------------------------------------------------------
    # "This listing sold on..." / "This listing has ended"
    (re.compile(r"(?:^|\n)This listing (?:sold on|has ended)[^\n]*(?=\n|$)"), "\n"),
    # "See original listing" link
    (re.compile(r"(?:^|\n)\[?See original listing\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    # "SOLD" standalone
    (re.compile(r"(?:^|\n)SOLD(?=\n|$)"), "\n"),
    # "Sold" standalone (triggers sold summary)
    (re.compile(r"(?:^|\n)Sold(?=\n\n)"), "\n"),
    # "It's free to sell on eBay" promotional
    (re.compile(r"(?:^|\n)It.s free to sell on eBay(?=\n|$)"), "\n"),
    # "Excludes Vehicles and business sellers"
    (re.compile(r"(?:^|\n)Excludes [^\n]*(?:sellers|vehicles)(?=\n|$)", re.I), "\n"),
    # "New" standalone after sold block (sell prompt)
    # Handled by the general "standalone short words" approach below

    # -----------------------------------------------------------------------
    # Live streaming event banner
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[LIVE[^\]]*\]\([^\)]*ebaylive[^\)]*\)(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Streaming now[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Shop exclusive items[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Join event(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Sale event banners
    # -----------------------------------------------------------------------
    # "SAVE UP TO N%" + "See all eligible items and terms" link
    (re.compile(r"(?:^|\n)(?:!\[[^\]]*\]\([^\)]+\))?SAVE UP TO \d+%[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[?See all eligible items and terms\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Trust badges and boilerplate
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)(?:#{1,4}\s+)?Shop with confidence(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)eBay Money Back Guarantee(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Get the item you ordered or your money back\.[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Seller assumes all responsibility for this listing\.?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)About this item(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Last updated on [^\n]+\[View all revisions\]\([^\)]+\)(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)eBay item number:\s*\d+(?=\n|$)", re.I), "\n"),
    # "Special financing available" + "Learn more" link
    (re.compile(r"(?:^|\n)Special financing available\.[^\n]*(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Delivery estimates boilerplate
    # -----------------------------------------------------------------------
    # "[Estimated delivery dates](...) include seller's handling time..." paragraph
    (re.compile(r"(?:^|\n)\[Estimated delivery dates\][^\n]*(?=\n|$)"), "\n"),
    # "Delivery times may vary" addendum
    (re.compile(r"(?:^|\n)Delivery times may vary[^\n]*(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # International shipping boilerplate
    # -----------------------------------------------------------------------
    # Multi-line customs processing block (various forms)
    (re.compile(
        r"(?:^|\n)International shipm(?:ent|ping)[^\n]*customs processing[^\n]*(?=\n|$)",
        re.I,
    ), "\n"),
    (re.compile(
        r"(?:^|\n)International shipping - items may be subject to[^\n]*(?=\n|$)",
        re.I,
    ), "\n"),
    # "Your country's customs office..." line
    (re.compile(r"(?:^|\n)Your country.s customs office[^\n]*(?=\n|$)"), "\n"),
    # Customs sub-bullets
    (re.compile(r"(?:^|\n)• (?:Delays from customs|Import duties|Brokerage fees)[^\n]*(?=\n|$)"), "\n"),
    # "Sellers declare the item's customs value" line
    (re.compile(r"(?:^|\n)Sellers declare the item.s customs value[^\n]*(?=\n|$)"), "\n"),
    # "As the buyer, you should be aware of possible:"
    (re.compile(r"(?:^|\n)As the buyer, you should be aware of possible:(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Global Shipping Programme boilerplate (UK eBay)
    # -----------------------------------------------------------------------
    # "This item will be sent through eBay's Global Shipping Programme." + description
    (re.compile(
        r"(?:^|\n)This item will be sent through eBay.s Global Shipping Programme\.[^\n]*(?=\n|$)"
    ), "\n"),
    # "Includes international tracking, simplified customs clearance..." standalone
    (re.compile(
        r"(?:^|\n)Includes (?:international tracking|customs clearance)[^\n]*(?=\n|$)"
    ), "\n"),
    # GSP terms: "This amount includes seller specified domestic postage..."
    (re.compile(
        r"(?:^|\n)This amount includes (?:seller specified|applicable)[^\n]*(?=\n|$)"
    ), "\n"),
    # "For additional information, see the Global Shipping Programme terms and conditions"
    (re.compile(
        r"(?:^|\n)For additional information, see the Global Shipping Programme[^\n]*(?=\n|$)"
    ), "\n"),
    # GSP/import terms and conditions link
    (re.compile(
        r"(?:^|\n)\[terms and conditions\]\([^\)]*globalshipping[^\)]*\)(?=\n|$)"
    ), "\n"),
    # Import charges section header + estimate
    (re.compile(r"(?:^|\n)Import charges:(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Est\.\s*[£€$]\d[\d.,]*\s*Amount confirmed at checkout(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Payment boilerplate
    # -----------------------------------------------------------------------
    # "Get more time to pay." + PayPal/credit link
    (re.compile(r"(?:^|\n)Get more time to pay\.[^\n]*(?=\n|$)"), "\n"),
    # "International postage and import charges paid to Pitney Bowes Inc."
    (re.compile(r"(?:^|\n)International postage and import charges paid to[^\n]*(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Seller business information (UK/EU)
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)#{1,4}\s+Seller business information(?=\n|$)"), "\n"),
    (re.compile(
        r"(?:^|\n)I certify that all my selling activities will comply with all EU laws[^\n]*(?=\n|$)"
    ), "\n"),
    (re.compile(r"(?:^|\n)Seller contact information(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Registered as a business seller(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Business(?=\n\n)"), "\n"),
    # "Ask about this item" (UK variant of "Ask a question")
    (re.compile(r"(?:^|\n)\[?Ask about this item\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Sold listing summary block (appears at top of ended listings)
    # -----------------------------------------------------------------------
    # Sold date line: "Mon, 23 Feb, 11:13" or "Sat, Feb 22, 2025, 3:00 PM"
    (re.compile(
        r"(?:^|\n)(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [\d]{1,2}\s+\w+(?:,\s*\d{2,4})?,?\s+\d{1,2}:\d{2}(?:\s*[AP]M)?(?=\n|$)"
    ), "\n"),
    # US-style date: "Mon, Feb 22, 2025, 3:00 PM"
    (re.compile(
        r"(?:^|\n)(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w+ \d{1,2}, \d{4},?\s+\d{1,2}:\d{2}(?:\s*[AP]M)?(?=\n|$)"
    ), "\n"),
    # Standalone "New" after sold block (sell prompt, not "New" condition)
    (re.compile(r"(?:^|\n)New(?=\n\n)"), "\n"),

    # -----------------------------------------------------------------------
    # "More to explore" / "Shop Top Sellers" / "Related searches" sections
    # These are entire sections at the bottom — remove heading + content
    # -----------------------------------------------------------------------
    # "## More to explore :" heading + bulleted list following
    (re.compile(
        r"(?:^|\n)#{1,4}\s+More to explore\s*:?\s*\n"
        r"(?:\n?- \[[^\]]*\]\([^\)]*\),?\n?)*",
    ), "\n"),
    # "## Shop Top Sellers..." heading + "### Best Sellers" + "### Top Rated" + bulleted lists
    (re.compile(
        r"(?:^|\n)#{1,4}\s+Shop Top Sellers[^\n]*\n"
        r"(?:\n?#{1,4}\s+(?:Best Sellers|Top Rated)\n(?:\n?- \[[^\]]*\]\([^\)]*\)\n?)*)*",
    ), "\n"),
    # "## Related searches" heading + bulleted list
    (re.compile(
        r"(?:^|\n)#{1,4}\s+Related [Ss]earches\s*\n"
        r"(?:\n?- \[[^\]]*\]\([^\)]*\)\n?)*",
    ), "\n"),
    # Standalone "### Best Sellers" / "### Top Rated" headings (when parent heading was removed)
    (re.compile(r"(?:^|\n)#{1,4}\s+(?:Best Sellers|Top Rated)(?=\n|$)"), "\n"),
    # eBay internal /b/ category links (appear in "More to explore" even when heading is gone)
    (re.compile(r"(?:^|\n)- \[[^\]]*\]\(https://www\.ebay\.[^/]+/b/[^\)]*\),?(?=\n|$)"), "\n"),
    # eBay /p/ product links (appear in "Best Sellers"/"Top Rated" lists)
    (re.compile(r"(?:^|\n)- \[[^\]]*\]\(https://www\.ebay\.[^/]+/p/[^\)]*\)(?=\n|$)"), "\n"),
    # eBay /shop/ links (appear in "Related searches" lists)
    (re.compile(r"(?:^|\n)- \[[^\]]*\]\(https://www\.ebay\.[^/]+/shop/[^\)]*\)(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Product ratings cleanup
    # -----------------------------------------------------------------------
    # "Learn more" link right after "## Product ratings and reviews"
    (re.compile(r"(?:^|\n)\[Learn more\]\(https://www\.ebay\.com/help/selling/listings/product-reviews[^\)]*\)(?=\n|$)"), "\n"),
    # Duplicate review text (eBay doubles the review body)
    # The pattern: "text.text." where both halves are identical
    # We handle this in a dedicated function below instead of regex

    # -----------------------------------------------------------------------
    # Search page noise
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)#{1,4}\s+Filter(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Search(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Include description(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[?Advanced\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Related:(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[Tell us what you think[^\]]*\]\([^\)]*\)(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*(?:\[?\d+)?Items Per Page[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Save this search(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Customize(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-?\s*\[?Gallery View\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Sort:[^\n]*(?=\n|$)"), "\n"),

    # Sponsored label
    (re.compile(r"(?:^|\n)\s*Sponsored(?=\n|$)"), "\n"),

    # Accessibility text (inline, sometimes survives CSS removal)
    (re.compile(r"Opens in a new (?:window|tab)(?:\s+or\s+(?:window|tab))?"), ""),
    (re.compile(r"Wird in neuem Fenster oder Tab ge[öo]ffnet"), ""),

    # Footer pricing/currency disclaimer block
    (re.compile(
        r"(?:^|\n)\[?\*Learn about pricing\]?(?:\([^\)]*\))?"
        r"[\s\S]*?"
        r"(?:See each listing for international shipping[^\n]*)(?=\n|$)",
    ), "\n"),

    # "Feedback" standalone
    (re.compile(r"(?:^|\n)Feedback(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[Leave feedback[^\]]*\]\([^\)]*\)(?=\n|$)"), "\n"),

    # Duplicate title line (eBay repeats page title in body: "X for sale | eBay")
    (re.compile(r"(?:^|\n)[^\n]+ for sale \| eBay(?=\n|$)"), "\n"),

    # Buying format / condition filter labels in search
    (re.compile(r"(?:^|\n)(?:Buying Format|Any Condition)[^\n]*(?:Filter Applied)?(?=\n|$)"), "\n"),

    # Category dropdown options
    (re.compile(r"(?:^|\n)All Categories[^\n]*(?=\n|$)"), "\n"),

    # "Have one to sell?" / "Sell one like this" / "Sell something else"
    (re.compile(r"(?:^|\n)Have one to sell\?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[Sell (?:one like this|something else)\]\([^\)]*\)(?=\n|$)"), "\n"),

    # "Share" standalone
    (re.compile(r"(?:^|\n)Share(?=\n|$)"), "\n"),

    # "show original title" / "Original Text"
    (re.compile(r"(?:^|\n)show original title(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Original Text(?=\n|$)"), "\n"),

    # Standalone large number (watchers, sold count leftover)
    (re.compile(r"(?:^|\n)\d{2,}(?=\n\n)"), "\n"),

    # "See details" standalone or at end of line
    (re.compile(r"(?:^|\n)See details(?=\n|$)"), "\n"),
    (re.compile(r"\.\s*See details$", re.M), "."),

    # Standalone period (leftover from ". See details" or GSP terms removal)
    (re.compile(r"(?:^|\n)\.[^\S\n]*(?=\n|$)"), "\n"),

    # "[See all condition definitions](...)" link — must run BEFORE condition dedup
    (re.compile(r"\[See all condition definitions\]\([^\)]*\)"), ""),

    # Condition definition: "... Read moreNew: ..." → "..."
    # The "Read more" link expands the full definition inline.
    # IMPORTANT: Use [^\S\n]* (not \s*) at the end to avoid eating newlines.
    # The text after "full details" varies: "full details." or
    # "full details and description of any imperfections."
    (re.compile(
        r"(\.{3})[^\S\n]*Read more[^\S\n]*"
        r"(?:New|Used|Open Box|Refurbished|Pre-Owned|Certified|Like New)"
        r".+?See the seller.s listing for full details[^\n]*?\.?[^\S\n]*",
    ), r"\1"),
    # Fallback without "Read more" (some conditions skip it)
    (re.compile(
        r"(\.{3})[^\S\n]*"
        r"(?:New|Used|Open Box|Refurbished|Pre-Owned|Certified|Like New)"
        r".+?See the seller.s listing for full details[^\n]*?\.?[^\S\n]*",
    ), r"\1"),

    # "Read more" at end of condition descriptions (standalone)
    (re.compile(r"\.\.\.\s*Read more"), "..."),

    # Seller store tiny avatar images (< 100px)
    (re.compile(r"(?:^|\n)\[!\[\]\([^\)]+/s-l64\.\w+\)\]\([^\)]+\)(?=\n|$)"), "\n"),
    # Standalone seller avatar
    (re.compile(r"(?:^|\n)!\[\]\([^\)]+/s-l64\.\w+\)(?=\n|$)"), "\n"),
]

# Replace JSONLD markers with clean content
_JSONLD_MARKER_RE = re.compile(
    r"__EBAY_JSONLD__([\s\S]*?)__EBAY_JSONLD__\n*"
)

# Replace search result markers with clean content
_SEARCH_MARKER_RE = re.compile(
    r"__EBAY_SEARCH__([\s\S]*?)__EBAY_SEARCH__\n*"
)

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def _dedup_reviews(markdown: str) -> str:
    """Fix eBay's review text duplication.

    eBay product pages render each review body twice (once visible, once
    as a hidden expanded block). After markdownify both end up concatenated.
    Detect and remove the duplicate half.
    """
    # Pattern: a block of text followed by an exact repeat
    # We look for lines within review list items (indented with 2+ spaces)
    def _dedup_block(m: re.Match) -> str:
        text = m.group(1)
        half = len(text) // 2
        if half > 20 and text[:half] == text[half:]:
            return text[:half]
        return text

    return re.sub(
        r"(  (?:[A-Z]|[a-z]).{40,})\1",
        lambda m: m.group(1),
        markdown,
    )


# "About this product" section duplicates the Item specifics data.
# Remove the entire section (heading + following key-value lines until next heading).
_ABOUT_THIS_PRODUCT_RE = re.compile(
    r"\n## About this product\n+"
    r"(?:## Product Identifiers\n+(?:[^\n#]+\n+)*)*"
    r"(?:## Product Key Features\n+(?:[^\n#]+\n+)*)*"
)


def postprocess_ebay(markdown: str) -> str:
    """Clean up eBay-specific markdown noise."""
    # Search results: if we extracted structured data, use it directly
    # and discard the noisy markdownified HTML
    sm = _SEARCH_MARKER_RE.search(markdown)
    if sm:
        return sm.group(1).strip()

    # Strip "| eBay" suffix from title heading and standalone title line
    markdown = re.sub(r"^(# .+?)\s*\|\s*eBay\s*$", r"\1", markdown, count=1, flags=re.M)
    # Also strip from non-heading title lines (prepended by the pipeline)
    markdown = re.sub(r"^([^\n#]+?)\s*\|\s*eBay\s*$", r"\1", markdown, count=1, flags=re.M)

    # Extract and reformat JSON-LD marker.
    # Place structured data at the very top of the markdown so it's visible
    # immediately. The pipeline will prepend a title heading above this.
    m = _JSONLD_MARKER_RE.search(markdown)
    jsonld_content = ""
    if m:
        jsonld_content = m.group(1).strip()
        markdown = markdown[:m.start()] + markdown[m.end():]
        markdown = markdown.lstrip()  # clean leading whitespace after marker removal

    # Remove duplicate "About this product" section
    markdown = _ABOUT_THIS_PRODUCT_RE.sub("\n", markdown)

    # Note: duplicate title removal happens after JSON-LD injection below

    # Deduplicate review text
    markdown = _dedup_reviews(markdown)

    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()

    # If no heading at start, find and promote the first # heading to top.
    # This ensures the pipeline won't prepend a duplicate title heading.
    # (Sold listings have the title heading in the middle after the summary block.)
    if not markdown.startswith("# "):
        heading_m = re.search(r"(?:^|\n)(# [^\n]+)\n", markdown)
        if heading_m:
            heading_text = heading_m.group(1)
            # Remove from current position and prepend
            markdown = (
                markdown[:heading_m.start()]
                + markdown[heading_m.end():]
            ).strip()
            markdown = f"{heading_text}\n\n{markdown}"

    # Inject JSON-LD structured data after the first heading (or at top)
    if jsonld_content:
        heading_m = re.match(r"(# [^\n]+\n)", markdown)
        if heading_m:
            insert_pos = heading_m.end()
            markdown = markdown[:insert_pos] + f"\n{jsonld_content}\n" + markdown[insert_pos:]
        else:
            markdown = f"{jsonld_content}\n\n{markdown}"

    # Remove duplicate title heading/text. The first heading is now at the top;
    # remove any later duplicate (heading or plain text).
    heading_m = re.match(r"(# [^\n]+)\n", markdown)
    if heading_m:
        first_title = heading_m.group(1)[2:].strip()  # strip "# " and whitespace
        escaped = re.escape(first_title)
        # Remove duplicate as heading (# Title) — whitespace-tolerant
        dm = re.search(r"\n# " + escaped + r"\s*\n", markdown[heading_m.end():])
        if dm:
            pos = heading_m.end() + dm.start()
            markdown = markdown[:pos] + "\n" + markdown[pos + dm.end() - dm.start():]
        # Remove duplicate as plain text (sold listings show title without #)
        pm = re.search(r"\n" + escaped + r"\s*\n", markdown[heading_m.end():])
        if pm:
            pos = heading_m.end() + pm.start()
            markdown = markdown[:pos] + "\n" + markdown[pos + pm.end() - pm.start():]

    # Remove empty section headers left behind after content cleanup
    markdown = re.sub(r"(?:^|\n)Payments:(?=\n\n)", "\n", markdown)
    markdown = re.sub(r"(?:^|\n)Postage:(?=\n\n)", "\n", markdown)

    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()

    return markdown
