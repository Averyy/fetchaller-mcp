"""HTML to markdown conversion with cleanup.

This module contains the generic HTML→markdown pipeline. Site-specific
selectors, soup-level cleanup, and markdown post-processing live in their
own modules (github.py, reddit.py, hackernews.py, wikipedia.py).
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify

from . import forums as _forums
from . import github as _github
from . import hackernews as _hackernews
from . import huggingface as _huggingface
from . import medium as _medium
from . import reddit as _reddit
from . import redflagdeals as _redflagdeals
from . import stackoverflow as _stackoverflow
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
_JUNK_AND_REDDIT_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _reddit.SELECTORS_LIST)
_JUNK_AND_HACKERNEWS_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _hackernews.SELECTORS_LIST)
_JUNK_AND_GITHUB_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _github.SELECTORS_LIST)
_JUNK_AND_HUGGINGFACE_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _huggingface.SELECTORS_LIST)
_JUNK_AND_MEDIUM_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _medium.SELECTORS_LIST)
_JUNK_AND_REDFLAGDEALS_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _forums.SELECTORS_LIST + _redflagdeals.SELECTORS_LIST)
_JUNK_AND_STACKOVERFLOW_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _stackoverflow.SELECTORS_LIST)
_JUNK_AND_FORUM_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _forums.SELECTORS_LIST)
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
# Main pipeline
# ---------------------------------------------------------------------------


def _detect_site(
    url: str | None, is_reddit: bool, soup: BeautifulSoup | None = None
) -> str | None:
    """Detect which site a URL belongs to.

    Returns a site key string ('reddit', 'hackernews', 'github', 'huggingface',
    'stackoverflow', 'medium', 'wikipedia') or None for generic pages.
    """
    if is_reddit:
        return "reddit"
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
    "reddit": _JUNK_AND_REDDIT_SELECTOR,
    "hackernews": _JUNK_AND_HACKERNEWS_SELECTOR,
    "github": _JUNK_AND_GITHUB_SELECTOR,
    "huggingface": _JUNK_AND_HUGGINGFACE_SELECTOR,
    "redflagdeals": _JUNK_AND_REDFLAGDEALS_SELECTOR,
    "stackoverflow": _JUNK_AND_STACKOVERFLOW_SELECTOR,
    "medium": _JUNK_AND_MEDIUM_SELECTOR,
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

    # Single-pass removal using combined CSS selector
    selector = _SITE_SELECTORS.get(site, _JUNK_SELECTOR)
    for element in soup.select(selector):
        element.decompose()

    # Strip useless links (javascript:, bare #) before conversion
    _strip_junk_links(soup)

    # Site-specific soup-level cleanup
    if site == "hackernews":
        _hackernews.strip_hn_junk(soup)
    elif site == "github":
        _github.strip_github_junk(soup)
    elif site == "huggingface":
        _huggingface.strip_huggingface_junk(soup)
    elif site == "stackoverflow":
        _stackoverflow.strip_stackoverflow_junk(soup)
    elif site == "medium":
        _medium.strip_medium_junk(soup)
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
    if site == "hackernews":
        markdown = _hackernews.postprocess_hn(markdown)
    elif site == "github":
        markdown = _github.postprocess_github(markdown)
    elif site == "huggingface":
        markdown = _huggingface.postprocess_huggingface(markdown)
    elif site == "stackoverflow":
        markdown = _stackoverflow.postprocess_stackoverflow(markdown)
    elif site == "medium":
        markdown = _medium.postprocess_medium(markdown)
    elif site == "redflagdeals":
        markdown = _redflagdeals.postprocess_rfd(markdown)
    elif site == "forum":
        markdown = _forums.postprocess_forum(markdown)

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
