"""Costco: JSON-LD Product extraction, CSS selector cleanup, regex postprocessor.

Exports the standard site interface (SELECTORS_LIST, is_costco,
postprocess_costco).

Covers Costco .com and .ca. Product pages are Next.js SSR with JSON-LD
Product schema and data-testid elements. Search/category pages are CSR
(no useful SSR content — needs search API intercept).
"""

import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_costco(url: str) -> bool:
    """Check if URL is a Costco page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in ("www.costco.com", "www.costco.ca", "costco.com", "costco.ca")


def is_costco_product_url(url: str) -> bool:
    """Check if URL is a Costco product page."""
    if not is_costco(url):
        return False
    path = urlparse(url).path.lower()
    return ".product." in path or path.startswith("/p/")


# Search/category URL patterns
_SEARCH_PATH_RE = re.compile(r"^/s\b", re.I)
# Costco serves categories in two shapes: the newer ``/c/<slug>`` and the
# long-standing ``/<slug>.html`` (``/laptops.html``, ``/dog-food.html``). Only
# the first was recognized, so the ``.html`` form skipped the working search API
# and fell through to browser-solved HTML. Product pages carry ``.product.`` or
# ``/p/`` and are excluded by is_costco_product_url before this is consulted.
_CATEGORY_PATH_RE = re.compile(r"^/c/", re.I)
_CATEGORY_HTML_PATH_RE = re.compile(r"^/[a-z0-9][a-z0-9-]*\.html$", re.I)


def is_costco_search_url(url: str) -> bool:
    """Check if URL is a Costco search page (CSR, needs API)."""
    if not is_costco(url):
        return False
    parsed = urlparse(url)
    return bool(_SEARCH_PATH_RE.match(parsed.path)) or "keyword=" in (parsed.query or "")


def is_costco_category_url(url: str) -> bool:
    """Check if URL is a Costco category/browse page (CSR, needs API)."""
    if not is_costco(url) or is_costco_product_url(url):
        return False
    path = urlparse(url).path
    return bool(
        _CATEGORY_PATH_RE.match(path) or _CATEGORY_HTML_PATH_RE.match(path)
    )


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Navigation, header
    'nav', 'header', '[role="banner"]',
    # Footer
    'footer', '[role="contentinfo"]',
    # Warehouse/delivery widgets
    '[data-testid*="warehouse"]', '[data-testid*="delivery"]',
    '[data-testid*="Warehouse"]', '[data-testid*="Delivery"]',
    '[data-testid*="fulfillment"]',
    # Cart/account UI
    '[data-testid*="cart"]', '[data-testid*="Cart"]',
    # Cookie consent, feedback
    '#onetrust-consent-sdk', '[class*="feedback"]',
    # Promo banners, ads
    '[data-testid*="promo"]', '[data-testid*="sponsor"]', '[data-testid*="Sponsor"]',
    # Breadcrumbs
    '[data-testid*="Breadcrumb"]',
    # Image galleries (we keep the JSON-LD image)
    '[data-testid="ImageGallery"]',
]


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # NOTE: All single-line patterns use (?=\n|$) lookahead instead of consuming
    # trailing \n. This prevents the classic "greedy \n consumption" bug where
    # removing line N eats the \n that line N+1 needs as its (?:^|\n) anchor.

    # -----------------------------------------------------------------------
    # Sign in / register prompts
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?Sign[\s\xa0]+[Ii]n(?:[\s\xa0]+/[\s\xa0]+Register)?\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\[?Register\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Create[\s\xa0]+Account\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Skip-to-content links
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?Skip[\s\xa0]+(?:to[\s\xa0]+)?(?:Main Content|Results|Images)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Warehouse selection text
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)My[\s\xa0]+Warehouse(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Set[\s\xa0]+My[\s\xa0]+Warehouse(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Open[\s\xa0]+until[^\n]*(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)Find[\s\xa0]+a[\s\xa0]+Warehouse(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Delivery location / ZIP code prompts
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)Delivery[\s\xa0]+Location[:\s]*[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)(?:Enter|Update)[\s\xa0]+(?:your[\s\xa0]+)?(?:ZIP|postal)[\s\xa0]+code[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\d{5}(?=\n|$)"), "\n"),  # Standalone ZIP code

    # -----------------------------------------------------------------------
    # Add to Cart / Add to List button text
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)-?\s*\[?Add[\s\xa0]+to[\s\xa0]+(?:Cart|List)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Filter sidebar text (search/category pages)
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)2-Day[\s\xa0]+Delivery[\s\xa0]+\d+[\s\xa0]+results?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Price[\s\xa0]+range[\s\xa0]+\d+[\s\xa0]+results?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Customer[\s\xa0]+Reviews[\s\xa0]+\d+[\s\xa0]+&[\s\xa0]+Up(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Brand[\s\xa0]+\d+[\s\xa0]+results?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Shopping cart prompts
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?(?:View|Go[\s\xa0]+to)[\s\xa0]+Cart\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Shopping[\s\xa0]+Cart(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Membership prompts
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?(?:Join|Renew)[\s\xa0]+(?:Now|Membership)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)(?:Gold[\s\xa0]+Star|Executive)[\s\xa0]+Membership(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Members[\s\xa0]+also[\s\xa0]+bought(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Footer link sections
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)#{1,4}[\s\xa0]+About[\s\xa0]+Us(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)#{1,4}[\s\xa0]+Membership(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)#{1,4}[\s\xa0]+Customer[\s\xa0]+Service(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)#{1,4}[\s\xa0]+Locations[\s\xa0]+&[\s\xa0]+Services(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Social media links
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?Costco[\s\xa0]+(?:Facebook|Instagram|Twitter|YouTube|Pinterest|TikTok)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Copyright text
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)[^\n]*(?:\u00a9|Copyright)[\s\xa0]+\d{4}[\s\xa0]+Costco[^\n]*(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Cookie/privacy consent text
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)(?:We[\s\xa0]+use[\s\xa0]+cookies|This[\s\xa0]+site[\s\xa0]+uses[\s\xa0]+cookies)[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Accept[\s\xa0]+(?:All[\s\xa0]+)?Cookies\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Write a review prompts
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?Write[\s\xa0]+a[\s\xa0]+[Rr]eview\]?(?:\([^\)]*\))?(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Product page UI noise
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?Shop[\s\xa0]+[^\]\n]+\]?(?:\(/s\?[^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?View[\s\xa0]+(?:Product[\s\xa0]+Details|More[\s\xa0]+Images)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Skip[\s\xa0]+Images\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Online[\s\xa0]+Price(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Compare[\s\xa0]+(?:region[\s\xa0]+updated)?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Close[\s\xa0]+Menu(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Built[\s\xa0]+At:[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Set[\s\xa0]+Delivery[\s\xa0]+Location(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Type[\s\xa0]+and[\s\xa0]+press[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Orders[\s\xa0]+&[\s\xa0]+Returns\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?[\s\xa0]*\[?Online[\s\xa0]+Only\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-?[\s\xa0]*\[?AppleCare\+[\s\xa0]+Available\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # "Our Online Price Includes" benefits block (icons + links)
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)Our[\s\xa0]+Online[\s\xa0]+Price[\s\xa0]+Includes[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)-[\s\xa0]+!\[(?:Technical[\s\xa0]+Support|Return[\s\xa0]+Policy|Warranty|McAfee|M356[\s\xa0]+Benefit|Rewards)[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\s*\[?(?:Technical[\s\xa0]+Support|90[\s\xa0]+Day[\s\xa0]+Return[\s\xa0]+Policy|2[\s\xa0]+Year[\s\xa0]+Warranty|Up[\s\xa0]+to[\s\xa0]+\d+%[\s\xa0]+in[\s\xa0]+Rewards)[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\s+McAfee[\s\xa0]+Total[\s\xa0]+Protection[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\s+Microsoft[\s\xa0]+365[\s\xa0]+Personal[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\*Excludes[\s\xa0]+Apple[\s\xa0]+Computers[^\n]*(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Shop mega-menu category links (standalone category names)
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)-[\s\xa0]+(?:Appliances|Baby|Beauty|Clothing|Computers|Costco[\s\xa0]+(?:Direct|Next)|Electronics|Floral|Furniture|Gift[\s\xa0]+Cards|Grocery|Health|Holiday|Home[\s\xa0]+&|Home[\s\xa0]+Improvement|Jewelry|Mattresses|Office|Patio|Pet[\s\xa0]+Supplies|Special[\s\xa0]+Events|Sports|Tires|Toys|View[\s\xa0]+More)[^\n]*(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Navigation bar links
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)-[\s\xa0]+\[?(?:Grocery|Same[\s\xa0]+Day|Savings|Business[\s\xa0]+Delivery|Optical|Pharmacy|Services|Photo|Travel|Membership|Locations)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?(?:While[\s\xa0]+Supplies[\s\xa0]+Last|Online-Only|Treasure[\s\xa0]+Hunt|What['\u2019]s[\s\xa0]+New|Member[\s\xa0]+Favorites|Recommendations[\s\xa0]+for[\s\xa0]+You|Customer[\s\xa0]+Service)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # JSON-LD placeholder block (Price: USD 1, Availability: OutOfStock)
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\*\*Price:\*\*[\s\xa0]+USD[\s\xa0]+1(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)\*\*Availability:\*\*[\s\xa0]+OutOfStock(?=\n|$)"), "\n"),

    # -----------------------------------------------------------------------
    # Delivery / fulfillment UI
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)## How[\s\xa0]+To[\s\xa0]+Get[\s\xa0]+It(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?2-Day[\s\xa0]+Delivery\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Ad banners (Affirm, Pets Plus Us, Microsoft 365/McAfee promos)
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)\[?!\[[^\]]*(?:affirm|buy[\s\xa0]+now[\s\xa0]*,[\s\xa0]*pay[\s\xa0]+later|Pets[\s\xa0]+Plus|GET[\s\xa0]+A[\s\xa0]+QUOTE|insurance|Microsoft[\s\xa0]+365|McAfee|Costco[\s\xa0]+Member[\s\xa0]+Offer)[^\]]*\]\([^\)]*\)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Title suffix cleanup (strip " | Costco" from headings)
    # -----------------------------------------------------------------------
    (re.compile(r"(# [^\n]+?)[\s\xa0]+\|[\s\xa0]+Costco(?=\n|$)"), r"\1"),

    # -----------------------------------------------------------------------
    # Category page filter sidebar
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)#{1,4}[\s\xa0]+Filter[\s\xa0]+Results(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Clear[\s\xa0]+All(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Scrolled[\s\xa0]+to[\s\xa0]+top(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Grocery order bar
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)(?:Start|Continue)[\s\xa0]+(?:your[\s\xa0]+)?(?:Grocery|Same[\s\xa0]+Day)[\s\xa0]+Order[^\n]*(?=\n|$)", re.I), "\n"),

    # -----------------------------------------------------------------------
    # Empty heading artifacts (## \n or ### \n)
    # -----------------------------------------------------------------------
    (re.compile(r"(?:^|\n)#{1,6}\s*(?=\n|$)"), "\n"),
]

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_costco(markdown: str) -> str:
    """Clean up Costco-specific markdown noise."""
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)

    # Clean up product gallery images:
    # 1. Strip "Enlarge Product Preview N" alt text
    # 2. Remove query params from costco-static URLs (sizing/format cruft)
    # 3. Deduplicate (Costco renders full-size + thumbnail for each image)
    _seen_images: set[str] = set()
    def _clean_costco_image(m: re.Match) -> str:
        url_part = m.group(2)
        # Strip query params
        clean_url = re.sub(r"\?.*$", "", url_part)
        # Deduplicate by image path
        img_match = re.search(r"/([^/]+)$", clean_url)
        if img_match:
            img_id = img_match.group(1)
            if img_id in _seen_images:
                return "\n"
            _seen_images.add(img_id)
        # Strip noisy alt text like "Enlarge Product Preview 1"
        alt = m.group(1)
        alt = re.sub(r"Enlarge\s+Product\s+Preview\s*\d*", "", alt, flags=re.I).strip()
        return f"\n![{alt}]({clean_url})"

    markdown = re.sub(r"\n!\[([^\]]*)\]\(([^\)]*costco-static\.com[^\)]*)\)", _clean_costco_image, markdown)

    # Remove "Shop" standalone line (mega-menu trigger)
    markdown = re.sub(r"(?:^|\n)Shop(?=\n)", "\n", markdown)

    # Remove empty "Member Reviews" section (just heading + "0")
    markdown = re.sub(r"\n## Member Reviews\s*\n+0\s*$", "", markdown)

    # Remove trailing standalone "0" (empty cart/review count artifact)
    markdown = re.sub(r"\n0\s*$", "", markdown)

    # Remove badge rows in specs tables (key == value, e.g., "| Online Only | Online Only |")
    markdown = re.sub(
        r"(?:^|\n)\|[\s\xa0]*(?:AppleCare\+[\s\xa0]+Available|Online[\s\xa0]+Only)[\s\xa0]*\|[^\n]*(?=\n|$)",
        "\n", markdown, flags=re.I,
    )

    # Collapse consecutive horizontal rules (--- repeated) into one
    markdown = re.sub(r"(?:\n---){2,}", "\n---", markdown)

    # Remove duplicate top-level heading (page title repeated as section heading)
    # e.g., "# Nintendo\n\n---\n\n# Nintendo" → "# Nintendo"
    def _dedup_h1(m: re.Match) -> str:
        return m.group(1)
    markdown = re.sub(r"(# ([^\n]+))\n+(?:---\n+)?# \2", _dedup_h1, markdown)

    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
