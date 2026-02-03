"""HTML to markdown conversion with cleanup."""

from bs4 import BeautifulSoup
from markdownify import markdownify

# Elements to remove from HTML before conversion
JUNK_SELECTORS = [
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
REDDIT_SELECTORS = [
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

    # Remove standard junk elements
    for selector in JUNK_SELECTORS:
        for element in soup.select(selector):
            element.decompose()

    # Remove Reddit-specific elements
    if is_reddit:
        for selector in REDDIT_SELECTORS:
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

    # Clean up excessive whitespace
    lines = markdown.split("\n")
    cleaned_lines = []
    blank_count = 0

    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            blank_count += 1
            if blank_count <= 2:  # Allow max 2 consecutive blank lines
                cleaned_lines.append("")
        else:
            blank_count = 0
            cleaned_lines.append(stripped)

    markdown = "\n".join(cleaned_lines).strip()

    # Add title header if present
    if title:
        markdown = f"# {title}\n\n{markdown}"

    return markdown, title
