"""PetSmart: CSS selector cleanup and regex postprocessor.

Exports the standard site interface (SELECTORS_LIST, is_petsmart,
postprocess_petsmart, pre_clean_petsmart).

Covers PetSmart .com and .ca product pages. Product pages are SSR with
JSON-LD Product schema and scene7 images. Tab content (Description,
Ingredients, Directions) is in the SSR HTML.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_petsmart(url: str) -> bool:
    """Check if URL is a PetSmart page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in (
        "www.petsmart.com", "www.petsmart.ca",
        "petsmart.com", "petsmart.ca",
    )


# ---------------------------------------------------------------------------
# HTML pre-cleaning (before CSS selector removal and markdownify)
# ---------------------------------------------------------------------------


_NEXT_DATA_RE = re.compile(
    r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.DOTALL,
)

# Regex to find the start of HTML content inside a Next.js data chunk.
# The chunk may have a JS prefix like "63:T50a," before the first <p> tag.
_HTML_START_RE = re.compile(r"<(?:p|ul|ol|div|table|b)[ >]")


def pre_clean_petsmart(soup: BeautifulSoup) -> None:
    """Extract rating from JSON-LD and tab content from Next.js data scripts."""
    # --- 1. Extract aggregateRating from JSON-LD ---
    rating_value = None
    review_count = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and "aggregateRating" in data:
                ar = data["aggregateRating"]
                rating_value = ar.get("ratingValue")
                review_count = ar.get("reviewCount")
                break
        except (json.JSONDecodeError, TypeError):
            continue

    if rating_value:
        rating_el = soup.select_one('[data-testid="test-pdp-ratings"]')
        if rating_el:
            rating_text = f"{rating_value}/5"
            if review_count:
                rating_text += f" ({review_count} reviews)"
            rating_el.clear()
            rating_el.append(soup.new_string(rating_text))

    # --- 2. Extract tab content from Next.js self.__next_f.push scripts ---
    # All tab content (Description, Ingredients, Size, Directions, Warnings, etc.)
    # is stored as unicode-escaped HTML inside self.__next_f.push([1,"..."]) tags.
    # The visible HTML only has the Description tab; the rest are JS-hydrated.
    # We extract the FULL content and replace the visible description tab with it,
    # so all tabs are included without hardcoding tab names.
    best_html: str | None = None
    best_len = 0
    for script in soup.find_all("script"):
        text = script.string or ""
        if "self.__next_f.push" not in text:
            continue
        m = _NEXT_DATA_RE.search(text)
        if not m:
            continue
        raw = m.group(1)
        # Must contain HTML with bold headings (product content pattern)
        if "\\u003cp\\u003e\\u003cb\\u003e" not in raw and "<p><b>" not in raw:
            continue
        # raw is a JSON string body (<…). Decode via JSON so literal UTF-8
        # is preserved — unicode_escape mojibakes non-ASCII (bytes >127 read as
        # Latin-1, e.g. "Première" -> "PremiÃ¨re"). Fall back to the old decode
        # if the captured chunk isn't clean JSON.
        try:
            decoded = json.loads(f'"{raw}"')
        except (json.JSONDecodeError, ValueError):
            try:
                decoded = raw.encode().decode("unicode_escape")
            except (UnicodeDecodeError, ValueError):
                continue
        # Find where HTML actually starts (skip JS module prefix)
        html_m = _HTML_START_RE.search(decoded)
        if not html_m:
            continue
        html_content = decoded[html_m.start():]
        # Keep the longest chunk (it has the most complete content)
        if len(html_content) > best_len:
            best_html = html_content
            best_len = len(html_content)

    if best_html:
        tab_soup = BeautifulSoup(best_html, "html.parser")
        # Replace the visible description tab with the full extracted content
        tab_panel = soup.select_one(".product-description-tab")
        if tab_panel:
            tab_panel.clear()
            for child in list(tab_soup.children):
                tab_panel.append(
                    child.extract() if isinstance(child, Tag) else child
                )
        elif soup.body:
            container = soup.new_tag("div", attrs={"class": "petsmart-tab-content"})
            for child in list(tab_soup.children):
                container.append(
                    child.extract() if isinstance(child, Tag) else child
                )
            soup.body.append(container)


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Navigation, header, footer
    "nav", "header", "footer",
    '[role="banner"]', '[role="contentinfo"]',
    # Promo banners
    "[class*=sitewide-promo]", "[class*=promo-banner]",
    # Delivery details / fulfillment
    "[class*=delivery-details]",
    "[data-testid=pdp-delivery-details]",
    "[data-testid=bopis-delivery-details]",
    # "You may also like" carousel
    "[class*=related-products-carousel]",
    # Treats rewards
    "[class*=treats-reward]",
    # Thumbnail strip (we keep the main images)
    "[class*=thumbnail-carousel]",
    "[class*=image-thumbnails]",
    # Add to cart / purchase options
    "[class*=add-to-cart]",
    "[class*=purchase-option]",
    # Breadcrumbs
    "[class*=breadcrumb]",
    # Cookie consent
    "#onetrust-consent-sdk",
    # Manufacturer section (usually empty/JS-loaded)
    "[class*=from-manufacturer]",
]


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Promo banner text (EXTRA 20% OFF..., FREE shipping..., Online Only! Save...)
    (re.compile(r"(?:^|\n)\[?EXTRA[\s\xa0]+\d+%[\s\xa0]+OFF[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?(?:Enjoy[\s\xa0]+)?Free[\s\xa0]+(?:Same-day|Shipping)[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Buy[\s\xa0]+\d+,[\s\xa0]+get[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Online[\s\xa0]+Only![\s\xa0]+Save[\s\xa0]+\d+%[^\n]*(?=\n|$)", re.I), "\n"),

    # Favorite toggle
    (re.compile(r"(?:^|\n)Favorite[\s\xa0]+toggle[\s\xa0]+button(?=\n|$)", re.I), "\n"),

    # Hover/zoom text and video player text
    (re.compile(r"(?:^|\n)Hover[\s\xa0]+over[\s\xa0]+image[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Click[\s\xa0]+(?:image[\s\xa0]+to[\s\xa0]+open|to[\s\xa0]+play[\s\xa0]+video)[^\n]*(?=\n|$)", re.I), "\n"),
    # Video fallback text
    (re.compile(r"(?:^|\n)\[?!\[Your[\s\xa0]+browser[\s\xa0]+does[\s\xa0]+not[\s\xa0]+support[^\n]*(?=\n|$)", re.I), "\n"),

    # "+N more" text
    (re.compile(r"(?:^|\n)\+\d+[\s\xa0]+more(?=\n|$)", re.I), "\n"),

    # Autoship promo (various formats)
    (re.compile(r"(?:^|\n)Sign[\s\xa0]+in[\s\xa0]+&[\s\xa0]+Save[\s\xa0]+\d+%[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Save[\s\xa0]+\d+%[\s\xa0]+On[\s\xa0]+Your[\s\xa0]+First[\s\xa0]+Autoship[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Sign[\s\xa0]+In[\s\xa0]+&[\s\xa0]+Enjoy[\s\xa0]+Free[\s\xa0]+Shipping[^\n]*(?=\n|$)", re.I), "\n"),

    # Spend X get Y promo
    (re.compile(r"(?:^|\n)Spend[\s\xa0]+\$\d+[^\n]*(?:coupon|gift[\s\xa0]+card)[^\n]*(?=\n|$)", re.I), "\n"),

    # Strike-through price policy
    (re.compile(r"(?:^|\n)Open[\s\xa0]+strike-through[\s\xa0]+price[\s\xa0]+policy(?=\n|$)", re.I), "\n"),

    # "Details" standalone lines (from promo offer blocks)
    (re.compile(r"(?:^|\n)Details(?=\n|$)"), "\n"),

    # "Show more offers" / "Show more"
    (re.compile(r"(?:^|\n)Show[\s\xa0]+more(?:[\s\xa0]+offers)?[\s\xa0]*(?:\(\d+\))?(?=\n|$)", re.I), "\n"),

    # Purchase options
    (re.compile(r"(?:^|\n)One-time[\s\xa0]+purchase(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Pick[\s\xa0]+up[\s\xa0]+in[\s\xa0]+store(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Same-day[\s\xa0]+delivery(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Ship[\s\xa0]+to[\s\xa0]+me(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Add[\s\xa0]+to[\s\xa0]+cart\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Treats rewards section
    (re.compile(r"(?:^|\n)Estimated[\s\xa0]+\d+[\s\xa0]+points[\s\xa0]+earned(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n).*Don['\u2019]t[\s\xa0]+leave[\s\xa0]+points[\s\xa0]+behind[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?Activate\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),

    # Tab navigation links (Description, Ingredients, Directions, Size, etc.)
    (re.compile(r"(?:^|\n)[*-][\s\xa0]+\[[^\]]+\]\(#undefined\)(?=\n|$)", re.I), "\n"),

    # "About this item" heading (tab container heading, redundant)
    (re.compile(r"(?:^|\n)#{1,6}[\s\xa0]+About[\s\xa0]+this[\s\xa0]+item(?=\n|$)", re.I), "\n"),

    # Brand search link (e.g., [Natural Balance](/search/natural-balance?q=...))
    (re.compile(r"(?:^|\n)\[[^\]]+\]\(/search/[^\)]*\)(?=\n|$)"), "\n"),

    # "You may also like" section (if CSS selector missed it)
    (re.compile(r"(?:^|\n)##[\s\xa0]+You[\s\xa0]+may[\s\xa0]+also[\s\xa0]+like(?=\n|$)", re.I), "\n"),

    # "From the manufacturer"
    (re.compile(r"(?:^|\n)##[\s\xa0]+From[\s\xa0]+the[\s\xa0]+manufacturer(?=\n|$)", re.I), "\n"),

    # Arrow navigation text
    (re.compile(r"(?:^|\n)arrow-prev(?:arrow-next)?(?=\n|$)", re.I), "\n"),

    # Footer links (may appear as list items with - or * prefix, or standalone lines)
    (re.compile(r"(?:^|\n)[*-]?\s*\[?Pet[\s\xa0]+Services\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[*-]?\s*\[?Careers\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[*-]?\s*\[?Help[\s\xa0]+Center\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[*-]?\s*\[?Treats[\s\xa0]+Rewards[\s\xa0]+program\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[*-]?\s*\[?Accessibility[\s\xa0]+Statement\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Connect[\s\xa0]+With[\s\xa0]+Us(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)Copyright[\s\xa0]+©[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[*-]?\s*\[?(?:About|PetSmart[\s\xa0]+Charities|US[\s\xa0]+Site|Canada[\s\xa0]+Site)\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?(?:Recalls|Terms[\s\xa0]+of[\s\xa0]+Use|Privacy[\s\xa0]+Policy|Interest-Based[\s\xa0]+Ads|Canada[\s\xa0]+Modern[\s\xa0]+Slavery)[^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[^\n]*Promotional[\s\xa0]+Terms[^\n]*(?=\n|$)", re.I), "\n"),
    # "Exclusions may apply" disclaimer line
    (re.compile(r"(?:^|\n)\*?Exclusions[\s\xa0]+may[\s\xa0]+apply[^\n]*(?=\n|$)", re.I), "\n"),

    # US footer links (Track order, Contact Us, LegitScript, Dot Pharmacy)
    (re.compile(r"(?:^|\n)[*-]?\s*\[?Track[\s\xa0]+your[\s\xa0]+order\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[*-]?\s*\[?Contact[\s\xa0]+Us\]?(?:\([^\)]*\))?(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)\[?!\[(?:Legit[\s\xa0]*Script|Dot[\s\xa0]*Pharmacy)[^\]]*\][^\n]*(?=\n|$)", re.I), "\n"),

    # App store badges
    (re.compile(r"(?:^|\n)\[?!\[[^\]]*(?:App[\s\xa0]+Store|Google[\s\xa0]+Play)[^\]]*\][^\n]*(?=\n|$)", re.I), "\n"),

    # Social media icons
    (re.compile(r"(?:^|\n)\[?!\[[^\]]*(?:Instagram|Facebook|Threads|TikTok|Youtube|Twitter|Pinterest)[\s\xa0]+Icon[^\]]*\][^\n]*(?=\n|$)", re.I), "\n"),

    # "Enable accessibility" and other header UI
    (re.compile(r"(?:^|\n)Enable[\s\xa0]+accessibility(?=\n|$)", re.I), "\n"),

    # Treats rewards logo image
    (re.compile(r"(?:^|\n)\[?!\[[^\]]*Treats[\s\xa0]+Rewards[^\]]*\][^\n]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)!\[icon[\s\xa0]+treats[^\]]*\][^\n]*(?=\n|$)", re.I), "\n"),

    # Empty heading artifacts
    (re.compile(r"(?:^|\n)#{1,6}\s*(?=\n|$)"), "\n"),
]

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_petsmart(markdown: str) -> str:
    """Clean up PetSmart-specific markdown noise."""
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)

    # Remove inline thumbnail strips (multiple images on one line, wid=72)
    markdown = re.sub(
        r"(?:^|\n)(?:!\[[^\]]*\]\([^\)]*wid=72[^\)]*\)){2,}(?=\n|$)",
        "\n", markdown,
    )

    # Clean scene7 image URLs: strip query params, keep clean base URL
    # Deduplicate by image ID
    _seen_images: set[str] = set()

    def _clean_petsmart_image(m: re.Match) -> str:
        url = m.group(2)
        # Strip query params
        clean_url = re.sub(r"\?.*$", "", url)
        # Extract image ID for dedup
        img_match = re.search(r"/([^/]+)$", clean_url)
        if img_match:
            img_id = img_match.group(1)
            if img_id in _seen_images:
                return "\n"
            _seen_images.add(img_id)
        return f"\n![]({clean_url})"

    markdown = re.sub(
        r"(?:^|\n)!\[([^\]]*)\]\((https?://s7d2\.scene7\.com/[^\)]+)\)",
        _clean_petsmart_image,
        markdown,
    )

    # Replace review link "[24 reviews](#bv-reviews-section)" with plain text
    markdown = re.sub(
        r"(?:^|\n)-?\s*\[(\d+[\s\xa0]+reviews?)\]\(#[^\)]*\)",
        r"\n\1", markdown,
    )

    # Move the # heading above the image block.
    # Handles both "Title\n![](img)...\n# Title" and "![](img)...\n# Title"
    def _move_title_above_images(m: re.Match) -> str:
        heading = m.group(2)
        images = m.group(1)
        return f"# {heading}\n\n{images}"
    markdown = re.sub(
        r"(?:^[^\n#!]*\n+)?((?:!\[\]\(https?://s7d2\.scene7\.com/[^\)]+\)\n+)+)# ([^\n]+)",
        _move_title_above_images, markdown,
    )
    # Also deduplicate consecutive identical # headings
    def _dedup_title(m: re.Match) -> str:
        return m.group(1)
    markdown = re.sub(r"(# ([^\n]+))\n+# \2", _dedup_title, markdown)

    # Remove "You may also like" product cards (numbered list with carousel images)
    # These follow the "## You may also like" heading
    markdown = re.sub(
        r"\n## You may also like\n(?:.*\n)*?(?=\n##|\narrow-|\Z)",
        "\n", markdown, flags=re.I,
    )

    # Remove carousel product cards that leaked through
    markdown = re.sub(
        r"(?:^|\n)\d+\.\s+\[!\[[^\]]*\]\([^\)]*\$sclp-prd-main_small[^\)]*\)\][^\n]*(?:\n[^\n]*){0,4}(?=\n\d+\.\s|\narrow|\n##|\n\n|\Z)",
        "\n", markdown, flags=re.I,
    )

    # Variant attribute line cleanup (flavor:Xsize:Ycolor:Z → Flavor: X | Size: Y | Color: Z)
    markdown = re.sub(r"(?:^|\n)((?:flavor|size|color):[^\n]*)(?=\n|$)", _fix_variant_line, markdown, flags=re.I)

    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown


_VARIANT_ATTR_RE = re.compile(r"(flavor|size|color):", re.I)


def _fix_variant_line(m: re.Match) -> str:
    """Fix 'flavor:Chicken & Salmonsize:13 Ozcolor:Black' → 'Flavor: Chicken & Salmon | Size: 13 Oz | Color: Black'."""
    text = m.group(1)
    # Split on attribute boundaries, keeping the attribute name
    parts: list[str] = []
    last = 0
    for attr_m in _VARIANT_ATTR_RE.finditer(text):
        if attr_m.start() > last and parts:
            # Append the value to the previous part
            parts[-1] += text[last:attr_m.start()].rstrip()
        parts.append(attr_m.group(1).capitalize() + ": ")
        last = attr_m.end()
    if parts:
        parts[-1] += text[last:].strip()
    return "\n" + " | ".join(p.strip() for p in parts if p.strip())
