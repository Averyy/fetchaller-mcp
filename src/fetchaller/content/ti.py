"""Texas Instruments (ti.com): inventory placeholder cleanup and document viewer support.

TI loads inventory status and pricing via JavaScript. The static HTML
contains "Out of stock" as a fallback, which misleads LLMs into thinking
parts are unavailable when the real status requires login.

TI's HTML document viewer (datasheets) lazy-loads content section by section
via AJAX. This module fetches all sections concurrently using the ?raw=1
parameter to reconstruct the full document.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import UTC, datetime
from html import escape
from urllib.parse import urlparse


def is_ti(url: str) -> bool:
    """Detect ti.com URLs."""
    host = urlparse(url).hostname or ""
    return host in ("www.ti.com", "ti.com")


# No CSS selectors needed — TI's junk is in JS-populated placeholders
SELECTORS_LIST: list[str] = []


def postprocess_ti(markdown: str) -> str:
    """Remove misleading inventory placeholder blocks.

    TI loads inventory status via JavaScript. The static HTML contains
    "Out of stock" as a fallback that doesn't reflect actual availability.
    Also removes empty pricing tables (values populated by JS).
    """
    # Remove "Log in to order...Out of stock" blocks (appears 2x on part-details pages)
    markdown = re.sub(
        r"Log in to order\nlock\nLog in to view inventory\n"
        r".*?\*\*Out of stock\*\*",
        "",
        markdown,
        flags=re.DOTALL,
    )

    # Remove empty pricing tables (values loaded by JS, shows only dashes)
    markdown = re.sub(
        r"(?:^|\n)## Pricing\n+"
        r"\| Qty \| Price[^\n]*\n"
        r"\| ---[^\n]*\n"
        r"(?:\|[^\n]*\| +\|\n)+",
        "\n",
        markdown,
    )

    return markdown


# ---------------------------------------------------------------------------
# Document viewer: lazy-loaded datasheet reconstruction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PDF → document viewer upgrade
# ---------------------------------------------------------------------------

# TI datasheet PDF URLs: /lit/ds/symlink/{part}.pdf or /lit/gpn/{part}
_TI_PDF_PART_RE = re.compile(
    r"/lit/(?:ds/symlink/([^/.]+)\.pdf|gpn/([^/.]+))", re.IGNORECASE
)


def extract_ti_part_from_pdf_url(url: str) -> str | None:
    """Extract part number from a TI datasheet PDF URL.

    Supports:
    - /lit/ds/symlink/bq25622e.pdf
    - /lit/gpn/BQ25622E
    """
    host = urlparse(url).hostname or ""
    if host not in ("www.ti.com", "ti.com"):
        return None
    m = _TI_PDF_PART_RE.search(url)
    if m:
        return m.group(1) or m.group(2)
    return None


# ---------------------------------------------------------------------------
# Document viewer: lazy-loaded datasheet reconstruction
# ---------------------------------------------------------------------------

_TI_DOC_VIEWER_RE = re.compile(
    r"^https?://(?:www\.)?ti\.com/document-viewer/", re.IGNORECASE
)

# Match GUID-based section hrefs in the TOC (protocol-relative //www.ti.com/...)
# GUIDs aren't standard UUIDs — TI uses arbitrary alphanumeric chars (e.g. GUID-XXXXXXXX-SF0T-...)
_GUID_HREF_RE = re.compile(
    r'href="(//(?:www\.)?ti\.com/document-viewer/[^"]*?/GUID-[A-Za-z0-9-]+)',
    re.IGNORECASE,
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


def is_ti_document_viewer(url: str) -> bool:
    """Detect TI document viewer URLs (lazy-loaded datasheets)."""
    return bool(_TI_DOC_VIEWER_RE.match(url))


def extract_section_urls(html: str) -> list[str]:
    """Extract section GUID URLs from TI document viewer TOC HTML.

    Returns list of section URLs with ?raw=1 parameter for direct
    HTML fragment access.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for match in _GUID_HREF_RE.finditer(html):
        href = match.group(1)
        # Normalize: add https: prefix, strip fragment, add ?raw=1
        clean_url = "https:" + href.split("#")[0] + "?raw=1"
        if clean_url not in seen:
            seen.add(clean_url)
            urls.append(clean_url)
    return urls


async def fetch_document_sections(
    session,
    initial_html: str,
    timeout: float,
    max_concurrent: int = 5,
) -> str | None:
    """Fetch all lazy-loaded sections from a TI document viewer page.

    Uses the same wafer session to look like a single browser session.
    Concurrency is limited and requests are staggered to avoid triggering
    TI's rate limiter.

    Args:
        session: wafer.AsyncSession to use for requests.
        initial_html: The initial page HTML containing the TOC.
        timeout: Per-request timeout in seconds.
        max_concurrent: Maximum concurrent section requests.

    Returns:
        Combined HTML string with all sections, or None if no sections found.
    """
    section_urls = extract_section_urls(initial_html)
    if not section_urls:
        return None

    # Extract title from initial page
    title_match = _TITLE_RE.search(initial_html)
    title = title_match.group(1).strip() if title_match else "TI Datasheet"

    _log(f"TI document viewer: fetching {len(section_urls)} sections")

    sem = asyncio.Semaphore(max_concurrent)
    failed = 0

    async def fetch_one(url: str, index: int) -> str:
        nonlocal failed
        async with sem:
            # Stagger requests slightly to mimic browser prefetch behavior
            # (~25ms per slot so a batch of 8 spreads over ~200ms)
            await asyncio.sleep(index * 0.025)
            try:
                resp = await session.get(url, timeout=timeout)
                if resp.status_code < 400:
                    return resp.text
                failed += 1
            except Exception:
                failed += 1
            return ""

    tasks = [fetch_one(url, i) for i, url in enumerate(section_urls)]
    results = await asyncio.gather(*tasks)

    sections = [r for r in results if r.strip()]
    if not sections:
        return None

    _log(
        f"TI document viewer: got {len(sections)}/{len(section_urls)} sections"
        + (f" ({failed} failed)" if failed else "")
    )

    return (
        f"<html><head><title>{escape(title)}</title></head><body>"
        + "\n".join(sections)
        + "</body></html>"
    )
