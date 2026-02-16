"""Craigslist-specific HTML cleanup and post-processing.

Exports the standard site interface (SELECTORS_LIST, is_craigslist,
postprocess_craigslist).

Covers all Craigslist subdomains (city.craigslist.org). Pages are SSR with
no bot protection. Main noise: navigation UI, gallery thumbnails, post
footer with safety warnings, and JS loading indicators.
"""

import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_craigslist(url: str) -> bool:
    """Check if URL is a Craigslist page."""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname.endswith(".craigslist.org") or hostname == "craigslist.org"


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Left sidebar (search filters on listing pages)
    "#leftbar",
    # Right sidebar
    "#rightbar",
    # Search form
    "#searchform",
    # Pagination controls
    ".paginator",
    # Map container
    ".mapbox",
    "#map",
    # Post footer (post id, safety tips, best-of nomination)
    "#postinginfo",
    # Banner / header bar
    ".ban",
    # Gallery thumbnail strip (50x50c images)
    "#thumbs",
    # Nearby results / "few results found" suggestions
    ".nearby",
    # Result info bar ("showing X of Y")
    ".result-info",
    # Search options / sort bar
    ".search-options",
    # "cl" branding link at top
    "#logo",
    # Reply form / contact section
    "#replyForm",
    # "print" button area
    ".print-information",
    # Posting body buttons (reply, flag, favorite, hide)
    ".postinginfos",
    # "posted" / "updated" timestamps section
    ".postinginfos",
    # Page navigation (prev/next arrows)
    ".prevnext",
]


# ---------------------------------------------------------------------------
# Markdown post-processing (after markdownify)
# ---------------------------------------------------------------------------

_POSTPROCESS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # NOTE: All single-line patterns use (?=\n|$) lookahead instead of consuming
    # trailing \n. This prevents the classic "greedy \n consumption" bug where
    # removing line N eats the \n that line N+1 needs as its (?:^|\n) anchor.

    # CL branding link at top (appears as "[CL](...)" or "[craigslist](/)")
    (re.compile(r"(?:^|\n)\[(?:CL|craigslist)\]\([^\)]*\)(?=\n|$)"), "\n"),

    # Standalone "..." (image gallery ellipsis / loading)
    (re.compile(r"(?:^|\n)\.\.\.(?=\n|$)"), "\n"),

    # Navigation arrows: "◀ prev", "▲", "next ▶"
    (re.compile(r"(?:^|\n)[◀▶▲▼ \t]*(?:prev|next)[◀▶▲▼ \t]*(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)[◀▶▲▼](?=\n|$)"), "\n"),

    # Action buttons (standalone lines)
    (re.compile(r"(?:^|\n)(?:reply|favorite|hide|unhide|print)(?=\n|$)", re.I), "\n"),

    # Flag symbols, "flag" text, and "flagged" link (link may have title attr with space)
    (re.compile(r"(?:^|\n)[⚐⚑]+(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)flag(?=\n|$)"), "\n"),
    (re.compile(r'(?:^|\n)\[flagged\]\([^\)]*(?:\s"[^"]*")?\)(?=\n|$)'), "\n"),

    # "do NOT contact me with unsolicited services or offers" boilerplate
    (re.compile(r"(?:^|\n)\s*do NOT contact me with unsolicited[^\n]*(?=\n|$)", re.I), "\n"),

    # Safety / scam warning block (multi-line: keeps internal \n, lookahead at end)
    (re.compile(
        r"(?:^|\n)\[Avoid scams[^\]]*\]\([^\)]*\)"
        r"(?:\n\*[^\n]*\*)?(?=\n|$)",
    ), "\n"),

    # "post id:" / "posted:" / "updated:" metadata lines
    (re.compile(r"(?:^|\n)post id:\s*\d+(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)posted:\s*[^\n]+(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)updated:\s*[^\n]+(?=\n|$)"), "\n"),

    # "♥ best of" nomination link
    (re.compile(r"(?:^|\n)\[♥ best of\]\([^\)]*\)\s*(?:\[.*?\])?(?=\n|$)"), "\n"),

    # JS loading indicators (consecutive lines — lookahead critical here)
    (re.compile(r"(?:^|\n)(?:loading|reading|writing|saving|searching)(?=\n|$)", re.I), "\n"),
    (re.compile(r"(?:^|\n)refresh the page\.(?=\n|$)", re.I), "\n"),

    # QR Code text
    (re.compile(r"(?:^|\n)QR Code Link to This Post(?=\n|$)"), "\n"),

    # Standalone "Contact Information:" label
    (re.compile(r"(?:^|\n)Contact Information:(?=\n|$)"), "\n"),

    # Posted date on standalone line (e.g., "Posted\n2026-01-12 14:01")
    (re.compile(r"(?:^|\n)Posted\n\d{4}-\d{2}-\d{2}[^\n]*(?=\n|$)"), "\n"),

    # Gallery image nav: "‹", "›", "image N of M"
    (re.compile(r"(?:^|\n)[‹›](?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)image \d+ of \d+(?=\n|$)", re.I), "\n"),

    # "see also" standalone line (search results)
    (re.compile(r"(?:^|\n)\d+\.\s*see also(?=\n|$)"), "\n"),

    # Navigation list items: "post", "account"
    (re.compile(r"(?:^|\n)-\s*\[(?:post|account)\]\([^\)]*\)(?=\n|$)"), "\n"),
    (re.compile(r"(?:^|\n)-\s*(?:favorites|hidden)(?=\n|$)", re.I), "\n"),

    # Thumbnail images (50x50c in URL)
    (re.compile(r"(?:^|\n)\[!\[[^\]]*\]\([^\)]*50x50c[^\)]*\)\]\([^\)]*\)\n?"), ""),

    # Standalone "Sponsored" label
    (re.compile(r"(?:^|\n)Sponsored(?=\n|$)"), "\n"),
]

_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")


def postprocess_craigslist(markdown: str) -> str:
    """Clean up Craigslist-specific markdown noise."""
    for pattern, replacement in _POSTPROCESS_PATTERNS:
        markdown = pattern.sub(replacement, markdown)
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
