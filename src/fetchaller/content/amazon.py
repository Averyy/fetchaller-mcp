"""Amazon-specific HTML cleanup and postprocessing.

Exports the standard site interface (SELECTORS_LIST, is_amazon,
strip_amazon_junk, postprocess_amazon).

Covers all Amazon TLDs: .com, .ca, .co.uk, .de, .fr, .it, .es,
.co.jp, .com.au, .in, .com.br, .com.mx, .nl, .sg, .ae, .sa, .pl,
.se, .com.be, .com.tr, .eg.
"""

import re
from html import escape
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_AMAZON_HOSTS = re.compile(
    r"(?:^|\.)"
    r"amazon\."
    r"(?:com|ca|co\.uk|de|fr|it|es|co\.jp|com\.au|in|com\.br|com\.mx"
    r"|nl|sg|ae|sa|pl|se|com\.be|com\.tr|eg)$"
)


def is_amazon(url: str) -> bool:
    """Check if URL is an Amazon page."""
    hostname = urlparse(url).hostname or ""
    return bool(_AMAZON_HOSTS.search(hostname))


def is_amazon_store(url: str) -> bool:
    """Check if URL is an Amazon store/brand page (JS-rendered, not supported)."""
    if not is_amazon(url):
        return False
    path = urlparse(url).path.lower()
    return path.startswith("/stores/") or path.startswith("/stores?")


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # --- Navigation & chrome ---
    "#navbar",
    "#navbar-main",
    "#shortcut-menu",
    "#nav-belt",
    "#nav-main",
    "#nav-subnav",
    "#nav-progressive-subnav",
    "#nav-flyout-ewc",
    "#nav-flyout-shopAll",
    "#skiplink",
    "#skippedLink",
    "#searchDropdownBox",

    # --- Sponsored product carousels (biggest bloat source) ---
    "#sp_detail",
    "#sp_detail2",
    '[id^="sp_detail"]',
    "#sims-simsContainer_feature_div_01",
    "#sims-sponsoredProducts2_feature_div_01",
    "#sims-discoveryAndInspiration_feature_div_01",
    "#sims-productBundle_feature_div_01",

    # --- "Frequently bought together", "Compare with similar" ---
    "#sims-fbt",
    "#HLCXComparisonWidget_feature_div",
    "#cardStack",
    "#similarities_feature_div",
    "#anonCarousel1",  # "4 stars and above" carousel
    "#anonCarousel2",
    "#anonCarousel3",

    # --- Buy box noise (wish list, add-on prompts) ---
    "#addToWishlist_feature_div",
    "#wishlistButtonStack",
    "#attachAccessoryModal_feature_div",
    "#attachSideSheet_feature_div",
    "#tellAFriendBox_feature_div",
    "#primeDPUpsellStaticContainerNPA",
    "#trustBadge_feature_div",

    # --- Product quick-view overlay (duplicates main content) ---
    "#productQuickView_feature_div",

    # --- Pricing feedback modal ---
    "#pricingFeedback_contentContainer",
    '[id^="a-popover-pricingFeedback"]',
    "#productDetails_feedback_sections",

    # --- Footer ---
    "#navFooter",
    "#rhf",  # "Recently viewed" + footer helper

    # --- Image block thumbnails (small 40px sprites, not the main image) ---
    "#imageBlock_feature_div",

    # --- Misc chrome ---
    "#above-dp-container",
    "#desktop-dp-ilm_feature_div_01",
    "#desktop-dp-lpo_feature_div_01",
    "#prime-desktop-dp_feature_div_01",
    "#desktop-breadcrumbs_feature_div",
    "#iesABBanner_feature_div",
    "#orderInformationGroup",
    "#companyCompliancePolicies_feature_div",
    "#fodcx_feature_div",
    "#nav-global-location-toaster-script-container",

    # --- Review noise (histogram, aspect tags, image gallery, review images) ---
    "#cm_cr_dp_d_rating_histogram",
    '[id^="rh_controls_aspect_"]',
    ".cr-widget-FocalReviews",
    "#cr-media-gallery-popover",
    ".cr-lighthouse-terms",

    # --- Brand story carousel ---
    "#aplusBrandStory_feature_div",

    # --- Compatibility finder ---
    "#compatibilityContainerDesktop",
    "#compatibilityFinder_feature_div",

    # --- Tracking pixel and sprite images ---
    'img[src*="fls-na.amazon"]',
    'img[src*="fls-eu.amazon"]',
    'img[src*="/sprites/"]',
    'img[src*="transparent-pixel"]',
    'img[src*="grey-pixel"]',

    # --- "Report" links for reviews ---
    'a[href*="/hz/reviews-render/report-review"]',

    # --- Brand insights ---
    '[id^="brandInsights_feature_div"]',

    # --- Popover overlays ---
    '[id^="a-popover-"]',

    # --- Twister (variant picker) inline elements ---
    "#twisterPlusBuyBoxMessage_feature_div",

    # --- Duplicate "Product details" bullet list (tables already present) ---
    "#detailBullets_feature_div",
    "#detailBulletsWrapper_feature_div",

    # --- Safety documents section (just PDF links, not content) ---
    "#productSafety_feature_div",

    # --- "Newer version" upsell ---
    "#newer-version_feature_div",

    # --- Customer review images carousel ---
    ".cr-media-gallery",
    "[data-hook='cr-media-gallery-popover']",

    # --- "Brand in this category" / "Brands related" sponsored sections ---
    "#sims-brand-in-category_feature_div",
    "#brand-in-category-sims",
    "#similarities_feature_div",
    "#sims-consolidated-2_feature_div",
    "#sims-consolidated-1_feature_div",
    '[id^="sims-"]',

    # --- Ad tracking images ---
    'img[src*="aax-us-east"]',
    'img[src*="aax-eu"]',

    # --- "Customers who viewed/bought this" carousels ---
    "#p13n-sims-content-1",
    "#p13n-sims-content-2",
    "#p13n-sims-content-3",
]


# ---------------------------------------------------------------------------
# Pre-CSS-selector extraction (runs BEFORE [id^="sims-"] nukes everything)
# ---------------------------------------------------------------------------


def extract_related_products(soup: BeautifulSoup) -> None:
    """Extract 'Frequently bought together' into compact HTML before CSS selectors remove sims-* sections.

    Called from clean_html() before CSS selectors fire. Finds FBT product names,
    prices, and links, then replaces the heavy carousel HTML with a lightweight
    list that survives markdownify cleanly.
    """
    fbt = soup.find(id="sims-productBundle_feature_div_01")
    if not fbt:
        return

    items: list[tuple[str, str, str]] = []  # (title, price, href)
    seen_titles: set[str] = set()

    for a in fbt.find_all("a", href=True):
        href = a["href"]
        if "/dp/" not in href:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10 or "out of 5" in title:
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)

        # Find price: walk up a few levels from the link to find .a-offscreen
        price = ""
        parent = a.parent
        for _ in range(4):
            if parent is None:
                break
            price_el = parent.find("span", class_="a-offscreen")
            if price_el:
                price = price_el.get_text(strip=True)
                break
            parent = parent.parent

        # Clean the href — strip tracking params and ref path segment
        clean_href = href.split("?")[0] if "?" in href else href
        clean_href = re.sub(r"/ref=[^/]*$", "", clean_href)

        items.append((title, price, clean_href))

    if not items:
        return

    # Build compact replacement HTML
    parts = ["<h4>Frequently bought together</h4>", "<ul>"]
    for title, price, href in items:
        price_str = f" — {price}" if price else ""
        parts.append(f'<li><a href="{escape(href, quote=True)}">{escape(title)}</a>{escape(price_str)}</li>')
    parts.append("</ul>")

    # Replace the heavy FBT div with our compact version
    new_tag = BeautifulSoup("".join(parts), "html.parser")
    fbt.clear()
    fbt["id"] = "amazon-fbt-compact"  # Rename so [id^="sims-"] won't match
    for child in list(new_tag.children):
        fbt.append(child)


# ---------------------------------------------------------------------------
# Soup-level cleanup (before markdownify)
# ---------------------------------------------------------------------------


def strip_amazon_junk(soup: BeautifulSoup) -> None:
    """Remove Amazon-specific junk that CSS selectors can't easily target."""
    # Remove all <input> elements (hidden form fields, CSRF tokens)
    for el in soup.find_all("input"):
        el.decompose()

    # Remove review "Report" links
    for a in soup.find_all("a", href=True):
        if "/hz/reviews-render/report-review" in a["href"]:
            a.decompose()

    # Remove "Translate review to English" / "Translate all reviews" links
    for a in soup.find_all("a", string=re.compile(r"Translate.*(?:review|English)", re.I)):
        a.decompose()

    # Remove "Read more" links in reviews
    for span in soup.find_all("span", string=re.compile(r"^Read more$")):
        span.decompose()
    for a in soup.find_all("a", string=re.compile(r"^Read more$")):
        a.decompose()

    # Remove junk images (tracking pixels, ad beacons, grey placeholders)
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if any(s in src for s in ("grey-pixel", "transparent-pixel", "aax-us-east", "aax-eu")):
            img.decompose()

    # Remove links with aax tracking URLs (sponsored ad links)
    for a in soup.find_all("a", href=True):
        if "aax-us-east" in a["href"] or "aax-eu" in a["href"]:
            a.decompose()


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

# Patterns to remove from final markdown
_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "Sponsored" section headers
    (re.compile(r"(?:^|\n)\[Sponsored\]\(#[^\)]*\)\n?"), "\n"),
    # "Page X of Y" carousel navigation
    (re.compile(r"(?:^|\n)Page \d+ of \d+\s*(?:Start over)?\n?"), "\n"),
    # "Previous/Next page of related Sponsored Products"
    (re.compile(r"(?:^|\n)\*(?:Previous|Next) page of related (?:Sponsored )?Products\*\n?"), "\n"),
    # "Feedback" labels that appear after each sponsored product
    (re.compile(r"(?:^|\n)\s*Feedback\n"), "\n"),
    # "Show More" / "Show Less" expandable text markers
    (re.compile(r"(?:^|\n)Show (?:More|Less)\n"), "\n"),
    # "See more product details" links
    (re.compile(r"(?:^|\n)›\s*\[See more product details\]\([^\)]*\)\n"), "\n"),
    # "Report an issue with this product or seller" links
    (re.compile(r"(?:^|\n)\[Report an issue with this product[^\]]*\]\([^\)]*\)\n?"), "\n"),
    # Duplicate "Product summary presents key product information" header (may be at EOF)
    (re.compile(r"(?:^|\n)# Product summary presents key product information[^\n]*(?:\n|$)"), "\n"),
    # "Keyboard shortcut" hints
    (re.compile(r"---\s*Keyboard\s*shortcut[^\n]*"), ""),
    # "Did you find this product summary feature useful?" feedback block
    (re.compile(
        r"(?:^|\n)## Feedback\n\n"
        r"Did you find this product summary feature useful\?\n"
        r"(?:.*\n)*?"
        r"Change your feedback\n?"
    ), "\n"),
    # "How are ratings calculated?" explanatory paragraph
    (re.compile(
        r"(?:^|\n)How are ratings calculated\?\n"
        r"(?:.*\n)*?"
        r"(?:verify trustworthiness\.\n)"
    ), "\n"),
    # "Customers say" AI-generated summary header + aspect mentions
    (re.compile(
        r"\d+ customers mention[^\n]*\n"
    ), ""),
    # Aspect expand/collapse images
    (re.compile(r"!\[\]\(https://m\.media-amazon\.com/images/G/01/cf-at-glance/[^\)]+\)\n?"), ""),
    # "Select to learn more" instruction
    (re.compile(r"(?:^|\n)#### Select to learn more\n"), "\n"),
    # "View Image Gallery" link
    (re.compile(r"(?:^|\n)View Image Gallery\n"), "\n"),
    # "Customer image" placeholder
    (re.compile(r"(?:^|\n)!\[Customer image\]\([^\)]*\)\n"), "\n"),
    # "Verified Purchase" labels (redundant noise)
    (re.compile(r"(?:^|\n)\s*Verified Purchase\n"), "\n"),
    # Bare "Back to top" link
    (re.compile(r"(?:^|\n)Back to top\n"), "\n"),
    # Footer section headers (use lookahead to avoid consuming the \n needed by next match)
    (re.compile(r"(?:^|\n)(?:Get to Know Us|Make Money with Us|Amazon Payment Products|Let Us Help You)(?=\n|$)"), ""),
    # Subsidiary links block (Amazon Music, AbeBooks, AWS, etc.)
    (re.compile(r"- ##### (?:Amazon (?:Music|Advertising|Business|Drive|Web Services|Photos|Resale|Renewed)|AbeBooks|Goodreads|IMDb|Shopbop|Whole Foods|Blink)\n(?:\s+[^\n]+\n)+"), ""),
    # Legal footer
    (re.compile(r"(?:^|\n)- \[Conditions of Use\].*$", re.DOTALL), ""),
    # Copyright line
    (re.compile(r"(?:^|\n)©\s*\d{4}-\d{4},?\s*Amazon\.com[^\n]*\n?"), ""),
    # Amazon address line
    (re.compile(r"(?:^|\n)- Amazon\.com\.ca ULC[^\n]*\n?"), ""),
    # Language/country selectors in footer
    (re.compile(r"(?:^|\n)\[(?:English|Canada|United States)\]\([^\)]*\)\n?"), ""),
    # "Top reviews from other countries" header (reviews are kept)
    (re.compile(r"(?:^|\n)### Top reviews from other countries\n"), "\n"),
    # "VIDEOS / 360° VIEW / IMAGES" media tab labels
    (re.compile(r"(?:^|\n)- (?:VIDEOS|360° VIEW|IMAGES)(?=\n|$)"), ""),
    # "Image Unavailable" blocks
    (re.compile(r"(?:^|\n)-\s*####\s*Image Unavailable\n(?:\s+[^\n]+\n)*"), "\n"),
    # "Currently unavailable" duplicate (keep first occurrence)
    (re.compile(r"(Currently unavailable\..*?\n)(?:.*?Currently unavailable\..*?\n)"), r"\1"),
    # Amazon tracking/sprite images that survived CSS selectors
    (re.compile(r"!\[\]\(https://(?:m\.media-amazon|images-na\.ssl-images-amazon)\.com/images/[GS]/[^\)]+\)\n?"), ""),
    # /sspa/click tracking URLs (sponsored product links that survived)
    (re.compile(r"\[([^\]]*)\]\(/sspa/click[^\)]*\)"), ""),
    # "Sign in to continue" prompt
    (re.compile(r"(?:^|\n)Sign in to continue\n"), "\n"),
    # Wish list error messages
    (re.compile(r"(?:^|\n)(?:Added to|Unable to add item to Wish List[^\n]*|### Sorry, there was a problem\.\n(?:\s+[^\n]+\n)*)\n?"), "\n"),
    # Delivery location line
    (re.compile(r"(?:^|\n)Delivering to [^\n]+ – Update location\n"), "\n"),
    # "See more reviews" link
    (re.compile(r"(?:^|\n)\[See more reviews\]\([^\)]*\)\n"), "\n"),
    # "Reviews with images" section header + "See all photos"
    (re.compile(r"(?:^|\n)### Reviews with images\n"), "\n"),
    (re.compile(r"(?:^|\n)See all photos\n"), "\n"),
    # "Previous/Next slide" carousel navigation
    (re.compile(r"(?:^|\n)\*(?:Previous|Next) slide\*\n"), "\n"),
    # Customer review thumbnail images (community-reviews)
    (re.compile(r"\d+\.\s*!\[Customer Image[^\]]*\]\([^\)]+\)\n?"), ""),
    # "AI Generated from the text of customer reviews" label
    (re.compile(r"(?:^|\n)AI Generated from the text of customer reviews[^\n]*\n"), "\n"),
    # Aspect tags like "Quality(174)" on their own line (lookahead to avoid consuming \n)
    (re.compile(r"(?:^|\n)[A-Z][a-z ]+\(\d+\)(?=\n|$)"), ""),
    # "Brief/Full content visible" toggle instructions
    (re.compile(r"(?:^|\n)(?:Brief|Full) content visible[^\n]*\n"), "\n"),
    # "Report an issue with this product" (no link variant)
    (re.compile(r"(?:^|\n)Report an issue with this product\n"), "\n"),
    # "Documents and guides" header (under Safety section, no content)
    (re.compile(r"(?:^|\n)Documents and guides\n"), "\n"),
    # "Safety Information (PDF)" link text
    (re.compile(r"(?:^|\n)Safety Information \(PDF\)\n"), "\n"),
    # Bare "# Feedback" heading (pricing feedback section)
    (re.compile(r"(?:^|\n)# Feedback\n"), "\n"),
    # "See more" expand links
    (re.compile(r"(?:^|\n)See more\n"), "\n"),
    # Empty "Make a Size selection" prompt
    (re.compile(r"(?:^|\n)Make a (?:Size|Colour|Style) selection\s*\n"), "\n"),
    # Quantity selector "Quantity: 1  2  3  4  5"
    (re.compile(r"(?:^|\n)Quantity:\s*(?:\d+\s*)+Quantity:\d+\n"), "\n"),
    # "Add gift options" link
    (re.compile(r"(?:^|\n)Add gift options\n"), "\n"),
    # "Other sellers on Amazon" section
    (re.compile(r"(?:^|\n)Other sellers on Amazon\n"), "\n"),
    # "See 0 options with no featured offers"
    (re.compile(r"(?:^|\n)See \d+ options with no featured offers\n"), "\n"),
    # Raw JSON buybox data blob (twisterPlusWWDesktop leaks this)
    (re.compile(r'\{"desktop_buybox_group_1":\[.*?\]\}'), ""),
    # "Purchase options and add-ons" header (empty after JSON removal)
    (re.compile(r"(?:^|\n)### Purchase options and add-ons\n"), "\n"),
    # "Added to cart" / "Cart" / "Proceed to checkout" UI noise
    (re.compile(r"(?:^|\n)×\s*\n"), "\n"),
    (re.compile(r"(?:^|\n)# Added to cart\n"), "\n"),
    (re.compile(r"(?:^|\n)Cart\s+Proceed to checkout\n"), "\n"),
    # Empty "Safety and product resources" sections (header with no content)
    (re.compile(r"(?:^|\n)##? Safety and product resources\n+(?:###? Safety documents\n)?"), "\n"),
    # "Customers say" section header
    (re.compile(r"(?:^|\n)### Customers say\n"), "\n### Customers say\n"),
    # Bare "Generated from the text of customer reviews" remnant
    (re.compile(r"Generated from the text of customer reviews\n"), ""),
    # "Brand in this category on Amazon" section (sponsored brand carousel)
    (re.compile(r"(?:^|\n)##? (?:Brand in this category|Brands related to this category) on Amazon\n"), "\n"),
    # Amazon ad tracking URLs (aax-us-east-retail-direct.amazon.com)
    (re.compile(r"\[!\[[^\]]*\]\([^\)]*\)\]\(https://aax-[^\)]+\)\n?"), ""),
    (re.compile(r"!\[[^\]]*\]\(https://aax-[^\)]+\)\n?"), ""),
    # "Sponsored" bare labels (not links)
    (re.compile(r"(?:^|\n)Sponsored\n"), "\n"),
    # Huge aax tracking URL links (any remaining)
    (re.compile(r"\[[^\]]*\]\(https://aax-us-east[^\)]+\)\n?"), ""),
]


def postprocess_amazon(markdown: str) -> str:
    """Clean up Amazon-specific markdown noise."""
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)

    # Final whitespace cleanup
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()

    return markdown
