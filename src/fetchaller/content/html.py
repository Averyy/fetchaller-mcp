"""HTML to markdown conversion with cleanup."""

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

# Elements to remove from HTML before conversion (combined into single selector for speed)
_JUNK_SELECTORS_LIST = [
    "script",
    "style",
    "nav",
    "footer",
    "iframe",
    "noscript",
    "svg",
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    ".nav",
    ".navbar",
    ".footer",
    ".sidebar",
    ".ads",
    ".advertisement",
]

# Reddit-specific elements to remove (old.reddit.com)
_REDDIT_SELECTORS_LIST = [
    ".side",
    ".footer-parent",
    ".listing-chooser",
    ".search-page",
    ".searchpane",
    ".infobar",
    ".premium-banner-outer",
    ".morelink",
    ".titlebox",
    ".login-form-side",
    ".promotedlink",
    ".organic-listing",
]

# Pre-combined CSS selectors for single-pass removal (much faster than iterating)
JUNK_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST)
REDDIT_SELECTOR = ", ".join(_REDDIT_SELECTORS_LIST)
JUNK_AND_REDDIT_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _REDDIT_SELECTORS_LIST)

# Pre-compiled regex for whitespace cleanup (faster than line-by-line iteration)
_EXCESSIVE_NEWLINES = re.compile(r"\n{4,}")


def clean_html(html: str, is_reddit: bool = False) -> BeautifulSoup:
    """
    Parse HTML and remove junk elements.

    Args:
        html: Raw HTML string
        is_reddit: If True, also remove Reddit-specific elements

    Returns:
        Cleaned BeautifulSoup object
    """
    soup = BeautifulSoup(html, "html.parser")

    # Single-pass removal using combined CSS selector (faster than iterating)
    selector = JUNK_AND_REDDIT_SELECTOR if is_reddit else JUNK_SELECTOR
    for element in soup.select(selector):
        element.decompose()

    return soup


def html_to_markdown(html: str, is_reddit: bool = False) -> tuple[str, str | None]:
    """
    Convert HTML to clean markdown.

    Args:
        html: Raw HTML string
        is_reddit: If True, also clean Reddit-specific elements

    Returns:
        Tuple of (markdown_content, title)
    """
    soup = clean_html(html, is_reddit)

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    # Get body content (or full document if no body)
    body = soup.find("body")
    content = str(body) if body else str(soup)

    # Convert to markdown
    markdown = markdownify(content, heading_style="ATX", code_language_callback=None)

    # Clean up excessive whitespace (regex is faster than line-by-line)
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n\n", markdown).strip()

    # Add title header if present
    if title:
        markdown = f"# {title}\n\n{markdown}"

    return markdown, title
