"""HTML to markdown conversion with cleanup.

This module contains the generic HTML→markdown pipeline. Site-specific
selectors, soup-level cleanup, and markdown post-processing live in their
own modules (github.py, reddit.py, hackernews.py, wikipedia.py).

Includes a generic JSON-LD Product extractor as a fallback for sites
without dedicated modules — extracts brand, price, specs from
schema.org Product structured data.
"""

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify

from . import alibaba as _alibaba
from . import aliexpress as _aliexpress
from . import amazon as _amazon
from . import craigslist as _craigslist
from . import digikey as _digikey
from . import ebay as _ebay
from . import forums as _forums
from . import github as _github
from . import hackernews as _hackernews
from . import huggingface as _huggingface
from . import medium as _medium
from . import molex as _molex
from . import mouser as _mouser
from . import reddit as _reddit
from . import redflagdeals as _redflagdeals
from . import soylent as _soylent
from . import stackoverflow as _stackoverflow
from . import ti as _ti
from . import wikipedia as _wikipedia

# ---------------------------------------------------------------------------
# Generic junk selectors (apply to all sites)
# ---------------------------------------------------------------------------

_JUNK_SELECTORS_LIST = [
    # Structural
    "script",
    "style",
    "nav",
    "footer",
    "iframe",
    "noscript",
    "svg",
    # ARIA roles
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    "[role='complementary']",
    "[role='search']",
    "[role='dialog']",
    # Common class/id patterns
    ".nav",
    ".navbar",
    ".footer",
    ".sidebar",
    ".ads",
    ".advertisement",
    # Cookie/consent
    ".cookie-banner",
    ".cookie-consent",
    ".cookie-notice",
    "#cookie",
    # Popups/modals
    ".popup",
    ".modal",
    ".overlay",
    "#modal",
    # Social sharing
    ".share",
    ".social",
    ".sharing",
    ".social-media",
    ".social-links",
    "#social",
    "#share",
    # Related content
    ".related",
    ".related-posts",
    # Navigation aids
    ".breadcrumb",
    ".breadcrumbs",
    "#breadcrumbs",
    ".skip-link",
    ".skip-nav",
    # Language selectors
    ".lang-selector",
    "#language-selector",
]

# Pre-combined CSS selectors per site (single-pass removal, much faster)
_JUNK_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST)
_JUNK_AND_ALIBABA_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _alibaba.SELECTORS_LIST)
_JUNK_AND_ALIEXPRESS_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _aliexpress.SELECTORS_LIST)
_JUNK_AND_AMAZON_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _amazon.SELECTORS_LIST)
_JUNK_AND_REDDIT_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _reddit.SELECTORS_LIST)
_JUNK_AND_HACKERNEWS_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _hackernews.SELECTORS_LIST)
_JUNK_AND_GITHUB_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _github.SELECTORS_LIST)
_JUNK_AND_HUGGINGFACE_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _huggingface.SELECTORS_LIST)
_JUNK_AND_MEDIUM_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _medium.SELECTORS_LIST)
_JUNK_AND_REDFLAGDEALS_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _forums.SELECTORS_LIST + _redflagdeals.SELECTORS_LIST)
_JUNK_AND_STACKOVERFLOW_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _stackoverflow.SELECTORS_LIST)
_JUNK_AND_FORUM_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _forums.SELECTORS_LIST)
_JUNK_AND_CRAIGSLIST_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _craigslist.SELECTORS_LIST)
_JUNK_AND_DIGIKEY_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _digikey.SELECTORS_LIST)
_JUNK_AND_EBAY_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _ebay.SELECTORS_LIST)
_JUNK_AND_MOLEX_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _molex.SELECTORS_LIST)
_JUNK_AND_MOUSER_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _mouser.SELECTORS_LIST)
_JUNK_AND_SOYLENT_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _soylent.SELECTORS_LIST)
_JUNK_AND_WIKIPEDIA_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _wikipedia.SELECTORS_LIST)

# Pre-compiled regex for whitespace cleanup
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# Known code language class names (for code_language_callback)
_CODE_LANGUAGES = frozenset((
    "python", "javascript", "js", "java", "cpp", "c", "go", "rust", "ruby",
    "bash", "sh", "sql", "json", "yaml", "xml", "html", "css", "typescript",
    "ts", "kotlin", "swift", "php", "r", "scala", "perl", "lua", "shell",
))
_CODE_LANG_PREFIXES = ("language-", "lang-", "highlight-")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_code_language(el):
    """Extract code language from class attribute of pre/code elements."""
    for cls in el.get("class") or []:
        for prefix in _CODE_LANG_PREFIXES:
            if cls.startswith(prefix):
                return cls[len(prefix):]
        if cls in _CODE_LANGUAGES:
            return cls
    code = el.find("code")
    if code:
        for cls in code.get("class") or []:
            for prefix in _CODE_LANG_PREFIXES:
                if cls.startswith(prefix):
                    return cls[len(prefix):]
    return None


def _fix_lazy_images(soup: BeautifulSoup) -> None:
    """Swap data-src into src for lazy-loaded images."""
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            for attr in ("data-src", "data-lazy-src", "data-original"):
                if img.get(attr):
                    img["src"] = img[attr]
                    break


def _resolve_urls(soup: BeautifulSoup, base_url: str) -> None:
    """Resolve relative URLs to absolute."""
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(base_url, base_tag["href"])
    # Only resolve image URLs (lazy image recovery produces relative paths)
    # Skip <a> href resolution — adds significant tokens with little LLM benefit
    for tag in soup.find_all("img", src=True):
        tag["src"] = urljoin(base_url, tag["src"])


def _strip_junk_links(soup: BeautifulSoup) -> None:
    """Remove links with javascript: or bare # hrefs — these are UI actions, not content."""
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href == "#" or href.startswith("javascript:"):
            tag.unwrap()


def _strip_data_uri_images(soup: BeautifulSoup) -> None:
    """Remove data: URI images, preserving useful alt text."""
    for img in soup.find_all("img", src=True):
        if img["src"].startswith("data:"):
            alt = (img.get("alt") or "").strip()
            if alt and alt.lower() not in ("", "icon", "image", "logo", "svg image"):
                img.replace_with(alt)
            else:
                img.decompose()


# ---------------------------------------------------------------------------
# Generic JSON-LD Product extraction (fallback for sites without modules)
# ---------------------------------------------------------------------------

_GENERIC_JSONLD_MARKER = "__GENERIC_JSONLD__"
_GENERIC_JSONLD_MARKER_RE = re.compile(
    r"__GENERIC_JSONLD__([\s\S]*?)__GENERIC_JSONLD__\n*"
)


def _extract_generic_jsonld(soup: BeautifulSoup) -> None:
    """Extract schema.org Product data from JSON-LD as a fallback.

    Fires for sites without dedicated modules. Extracts name, brand,
    description, price/availability, and additionalProperty specs.

    Uses the same marker-injection pattern as site-specific extractors
    (eBay, Molex) so structured data survives the CSS removal + markdownify
    pipeline.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue

            lines = []

            # Product name
            name = item.get("name")
            if name:
                lines.append(f"**Product:** {name}")

            # Brand
            brand = item.get("brand")
            if isinstance(brand, dict):
                brand_name = brand.get("name")
            else:
                brand_name = brand
            if brand_name:
                lines.append(f"**Brand:** {brand_name}")

            # Description
            desc = item.get("description")
            if desc and desc != name:
                lines.append(f"**Description:** {desc}")

            # SKU / MPN
            sku = item.get("sku")
            mpn = item.get("mpn")
            if mpn:
                lines.append(f"**MPN:** {mpn}")
            if sku and sku != mpn:
                lines.append(f"**SKU:** {sku}")

            # Offers (price, availability)
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
                        lines.append(f"**Price:** {currency} {price}".strip())
                    avail = offer.get("availability", "")
                    if avail:
                        avail_label = avail.rsplit("/", 1)[-1]
                        lines.append(f"**Availability:** {avail_label}")

            # Specifications from additionalProperty
            specs = item.get("additionalProperty", [])
            if isinstance(specs, dict):
                specs = [specs]
            spec_lines = []
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                spec_name = (spec.get("name") or "").strip()
                spec_value = (spec.get("value") or "").strip()
                if spec_name and spec_value:
                    spec_lines.append(f"- **{spec_name}:** {spec_value}")

            if spec_lines:
                lines.append("")
                lines.append("**Specifications:**")
                lines.extend(spec_lines)

            if lines:
                body = soup.find("body")
                if body is not None:
                    marker = soup.new_tag("div", id="generic-jsonld-marker")
                    marker.string = (
                        _GENERIC_JSONLD_MARKER
                        + "\n".join(lines)
                        + _GENERIC_JSONLD_MARKER
                    )
                    body.insert(0, marker)
                return  # Only process the first Product


def _postprocess_generic_jsonld(markdown: str) -> str:
    """Inject generic JSON-LD data after first heading."""
    m = _GENERIC_JSONLD_MARKER_RE.search(markdown)
    if not m:
        return markdown

    jsonld_content = m.group(1).strip()
    markdown = markdown[:m.start()] + markdown[m.end():]

    heading_m = re.search(r"(# [^\n]+\n)", markdown)
    if heading_m:
        insert_pos = heading_m.end()
        markdown = (
            markdown[:insert_pos] + f"\n{jsonld_content}\n" + markdown[insert_pos:]
        )
    else:
        markdown = f"{jsonld_content}\n\n{markdown}"

    return markdown


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def _detect_site(
    url: str | None, is_reddit: bool, soup: BeautifulSoup | None = None
) -> str | None:
    """Detect which site a URL belongs to.

    Returns a site key string ('amazon', 'reddit', 'hackernews', 'github',
    'huggingface', 'stackoverflow', 'medium', 'wikipedia') or None for
    generic pages.
    """
    if is_reddit:
        return "reddit"
    if url and _alibaba.is_alibaba(url):
        return "alibaba"
    if url and _aliexpress.is_aliexpress(url):
        return "aliexpress"
    if url and _amazon.is_amazon(url):
        return "amazon"
    if url and _craigslist.is_craigslist(url):
        return "craigslist"
    if url and _digikey.is_digikey(url):
        return "digikey"
    if url and _ebay.is_ebay(url):
        return "ebay"
    if url and _molex.is_molex(url):
        return "molex"
    if url and _mouser.is_mouser(url):
        return "mouser"
    if url and _hackernews.is_hackernews(url):
        return "hackernews"
    if url and _github.is_github(url):
        return "github"
    if url and _huggingface.is_huggingface(url):
        return "huggingface"
    if url and _redflagdeals.is_redflagdeals(url):
        return "redflagdeals"
    if url and _stackoverflow.is_stackoverflow(url):
        return "stackoverflow"
    if url and _medium.is_medium(url):
        return "medium"
    if url and _soylent.is_soylent(url):
        return "soylent"
    if url and _ti.is_ti(url):
        return "ti"
    if url and _wikipedia.is_wikipedia(url):
        return "wikipedia"
    # HTML-based fallback for Medium custom domains
    if soup is not None and _medium.is_medium_html(soup):
        return "medium"
    # Discourse gets its own site key (generic junk only, no forum-specific cleanup)
    if soup is not None and _forums.is_discourse_html(soup):
        return "discourse"
    # HTML-based fallback for generic forum software (XenForo, vBulletin, phpBB)
    if soup is not None and _forums.is_forum_html(soup):
        return "forum"
    return None


# Map site keys to pre-combined CSS selectors
_SITE_SELECTORS = {
    "alibaba": _JUNK_AND_ALIBABA_SELECTOR,
    "aliexpress": _JUNK_AND_ALIEXPRESS_SELECTOR,
    "amazon": _JUNK_AND_AMAZON_SELECTOR,
    "craigslist": _JUNK_AND_CRAIGSLIST_SELECTOR,
    "digikey": _JUNK_AND_DIGIKEY_SELECTOR,
    "ebay": _JUNK_AND_EBAY_SELECTOR,
    "molex": _JUNK_AND_MOLEX_SELECTOR,
    "mouser": _JUNK_AND_MOUSER_SELECTOR,
    "reddit": _JUNK_AND_REDDIT_SELECTOR,
    "hackernews": _JUNK_AND_HACKERNEWS_SELECTOR,
    "github": _JUNK_AND_GITHUB_SELECTOR,
    "huggingface": _JUNK_AND_HUGGINGFACE_SELECTOR,
    "redflagdeals": _JUNK_AND_REDFLAGDEALS_SELECTOR,
    "stackoverflow": _JUNK_AND_STACKOVERFLOW_SELECTOR,
    "medium": _JUNK_AND_MEDIUM_SELECTOR,
    "soylent": _JUNK_AND_SOYLENT_SELECTOR,
    "forum": _JUNK_AND_FORUM_SELECTOR,
    "wikipedia": _JUNK_AND_WIKIPEDIA_SELECTOR,
}


def clean_html(
    html: str, is_reddit: bool = False, url: str | None = None
) -> tuple[BeautifulSoup, str | None]:
    """
    Parse HTML and remove junk elements.

    Args:
        html: Raw HTML string
        is_reddit: If True, also remove Reddit-specific elements
        url: Page URL for site detection and relative URL resolution

    Returns:
        Tuple of (cleaned BeautifulSoup object, detected site key or None)
    """
    soup = BeautifulSoup(html, "lxml")

    # Detect site type (single pass, reused by caller)
    site = _detect_site(url, is_reddit, soup)

    # Discourse: content lives inside <noscript> for SEO crawlers.
    # Unwrap the noscript containing #main-outlet before generic selectors strip it.
    if site == "discourse":
        for noscript in soup.find_all("noscript"):
            if noscript.find(id="main-outlet"):
                noscript.unwrap()
                break

    # Amazon: extract related products before CSS selectors remove sims-* sections
    if site == "amazon":
        _amazon.extract_related_products(soup)

    # eBay: extract structured data before scripts are removed
    if site == "ebay":
        _ebay.extract_ebay_jsonld(soup)
        # Search pages: extract structured results from .s-item DOM
        if url and _ebay.is_ebay_search_url(url):
            _ebay.extract_ebay_search_results(soup, url)

    # Molex: extract JSON-LD product data before scripts are removed
    if site == "molex":
        _molex.extract_molex_jsonld(soup)

    # Soylent: extract inventory from gsf_conversion_data before scripts are removed
    if site == "soylent":
        _soylent.extract_inventory(soup)

    # Generic: extract JSON-LD Product data for sites without dedicated modules
    if site is None:
        _extract_generic_jsonld(soup)

    # Single-pass removal using combined CSS selector
    selector = _SITE_SELECTORS.get(site, _JUNK_SELECTOR)
    for element in soup.select(selector):
        element.decompose()

    # Strip useless links (javascript:, bare #) before conversion
    _strip_junk_links(soup)

    # Site-specific soup-level cleanup
    if site == "alibaba":
        _alibaba.strip_alibaba_junk(soup)
    elif site == "aliexpress":
        _aliexpress.strip_aliexpress_junk(soup)
    elif site == "amazon":
        _amazon.strip_amazon_junk(soup)
    elif site == "digikey":
        _digikey.strip_digikey_junk(soup)
    elif site == "ebay":
        _ebay.strip_ebay_junk(soup)
    elif site == "molex":
        _molex.strip_molex_junk(soup)
    elif site == "mouser":
        _mouser.strip_mouser_junk(soup)
    elif site == "hackernews":
        _hackernews.strip_hn_junk(soup)
    elif site == "github":
        _github.strip_github_junk(soup)
    elif site == "huggingface":
        _huggingface.strip_huggingface_junk(soup)
    elif site == "stackoverflow":
        _stackoverflow.strip_stackoverflow_junk(soup)
    elif site == "medium":
        _medium.strip_medium_junk(soup)
    elif site == "soylent":
        _soylent.strip_soylent_junk(soup)
    elif site == "redflagdeals":
        _redflagdeals.strip_rfd_junk(soup)
    elif site == "forum":
        _forums.strip_forum_junk(soup)

    # Fix lazy images (before URL resolution so we resolve the real src)
    _fix_lazy_images(soup)

    # Resolve relative URLs to absolute
    if url:
        _resolve_urls(soup, url)

    # Strip data URI images (after lazy fix and URL resolution)
    _strip_data_uri_images(soup)

    return soup, site


def _html_to_markdown_sync(html: str, is_reddit: bool = False, url: str | None = None) -> tuple[str, str | None]:
    """
    Convert HTML to clean markdown (CPU-bound, synchronous).

    Args:
        html: Raw HTML string
        is_reddit: If True, also clean Reddit-specific elements
        url: Page URL for site detection and URL resolution

    Returns:
        Tuple of (markdown_content, title)
    """
    soup, site = clean_html(html, is_reddit, url)

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    # Get body content (or full document if no body)
    body = soup.find("body")
    content = str(body) if body else str(soup)

    # Convert to markdown
    markdown = markdownify(
        content,
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
        table_infer_header=True,
        code_language_callback=_extract_code_language,
    )

    # Clean up excessive whitespace
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()

    # Site-specific markdown post-processing (elif — a URL matches at most one site)
    if site == "alibaba":
        markdown = _alibaba.postprocess_alibaba(markdown)
    elif site == "aliexpress":
        markdown = _aliexpress.postprocess_aliexpress(markdown)
    elif site == "amazon":
        markdown = _amazon.postprocess_amazon(markdown)
    elif site == "craigslist":
        markdown = _craigslist.postprocess_craigslist(markdown)
    elif site == "digikey":
        markdown = _digikey.postprocess_digikey(markdown)
    elif site == "ebay":
        markdown = _ebay.postprocess_ebay(markdown)
    elif site == "molex":
        markdown = _molex.postprocess_molex(markdown)
    elif site == "mouser":
        markdown = _mouser.postprocess_mouser(markdown)
    elif site == "hackernews":
        markdown = _hackernews.postprocess_hn(markdown)
    elif site == "github":
        markdown = _github.postprocess_github(markdown)
    elif site == "huggingface":
        markdown = _huggingface.postprocess_huggingface(markdown)
    elif site == "stackoverflow":
        markdown = _stackoverflow.postprocess_stackoverflow(markdown)
    elif site == "medium":
        markdown = _medium.postprocess_medium(markdown)
    elif site == "soylent":
        markdown = _soylent.postprocess_soylent(markdown)
    elif site == "ti":
        markdown = _ti.postprocess_ti(markdown)
    elif site == "redflagdeals":
        markdown = _redflagdeals.postprocess_rfd(markdown)
    elif site == "forum":
        markdown = _forums.postprocess_forum(markdown)
    elif site is None:
        markdown = _postprocess_generic_jsonld(markdown)

    # Strip "| SiteName" suffix from titles (eBay, other sites)
    if title:
        title = re.sub(r"\s*\|\s*eBay(?:\s+\w+)?\s*$", "", title)

    # Add title header only if markdown doesn't already start with a heading
    if title and not markdown.startswith("# "):
        markdown = f"# {title}\n\n{markdown}"

    return markdown, title


async def html_to_markdown(
    html: str, is_reddit: bool = False, url: str | None = None
) -> tuple[str, str | None]:
    """
    Convert HTML to clean markdown without blocking the event loop.

    Runs CPU-bound parsing in a thread pool executor.

    Args:
        html: Raw HTML string
        is_reddit: If True, also clean Reddit-specific elements
        url: Page URL for site detection and URL resolution

    Returns:
        Tuple of (markdown_content, title)
    """
    import asyncio
    from functools import partial

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(_html_to_markdown_sync, html, is_reddit, url))
