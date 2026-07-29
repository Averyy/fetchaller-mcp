"""HTML to markdown conversion with cleanup.

This module contains the generic HTML→markdown pipeline. Site-specific
selectors, soup-level cleanup, and markdown post-processing live in their
own modules (github.py, reddit.py, hackernews.py, wikipedia.py).

Includes a generic JSON-LD Product extractor as a fallback for sites
without dedicated modules — extracts brand, price, specs from
schema.org Product structured data.
"""

import asyncio
import json
import math
import multiprocessing
import re
import sys
import threading
from multiprocessing.connection import Connection
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from . import alibaba as _alibaba
from . import aliexpress as _aliexpress
from . import amazon as _amazon
from . import ashby as _ashby
from . import costco as _costco
from . import craigslist as _craigslist
from . import digikey as _digikey
from . import ebay as _ebay
from . import fcc as _fcc
from . import forums as _forums
from . import github as _github
from . import hackernews as _hackernews
from . import huggingface as _huggingface
from . import medium as _medium
from . import molex as _molex
from . import mouser as _mouser
from . import petsmart as _petsmart
from . import reddit as _reddit
from . import redflagdeals as _redflagdeals
from . import soylent as _soylent
from . import stackoverflow as _stackoverflow
from . import ti as _ti
from . import wikipedia as _wikipedia
from . import workatastartup as _workatastartup
from ._price import has_positive_price
from ._slots import SlotHandle


class HtmlProcessingError(RuntimeError):
    """HTML could not be processed safely."""


# HTML parsing and markdown conversion can execute expensive native code.
# Threads cannot be cancelled once parsing starts, so each conversion runs in a
# disposable, bounded worker that the parent can terminate on timeout.
_MAX_CONCURRENT_HTML_PROCESSES = 2
_MAX_HTML_INPUT_BYTES = 12 * 1024 * 1024
_MAX_HTML_INPUT_CHARS = 12 * 1024 * 1024
_MAX_HTML_OUTPUT_CHARS = 4 * 1024 * 1024
_MAX_HTML_TITLE_CHARS = 4_096
_HTML_PROCESSING_TIMEOUT = 20.0
_MAX_PROCESSING_TIMEOUT = 120.0
_PROCESS_MEMORY_LIMIT = 1024 * 1024 * 1024
_VIRTUAL_APPLE_PROCESS_MEMORY_LIMIT = 4 * 1024 * 1024 * 1024
_PROCESS_POLL_INTERVAL = 0.01
_HTML_SLOT_ATTRIBUTE = "_fetchaller_html_process_slots"
_PROCESS_RUNTIME_WARM = False
_PROCESS_RUNTIME_LOCK = threading.Lock()


def _html_slots() -> asyncio.Semaphore:
    """Return parser capacity bound to the current event loop."""
    loop = asyncio.get_running_loop()
    slots = getattr(loop, _HTML_SLOT_ATTRIBUTE, None)
    if slots is None:
        slots = asyncio.Semaphore(_MAX_CONCURRENT_HTML_PROCESSES)
        setattr(loop, _HTML_SLOT_ATTRIBUTE, slots)
    return slots


def _process_context() -> multiprocessing.context.BaseContext:
    """Choose a safe context while keeping documented ``python -c`` use working."""
    methods = multiprocessing.get_all_start_methods()
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if (not main_file or str(main_file).startswith("<")) and "fork" in methods:
        # spawn/forkserver cannot re-import a ``-c`` or stdin main module.
        # This fallback is limited to those interactive development entrypoints;
        # the MCP server and normal scripts use an isolated clean-start method.
        return multiprocessing.get_context("fork")
    if sys.platform.startswith("linux") and "forkserver" in methods:
        # Forkserver keeps worker creation isolated from the MCP server's
        # threads without paying Rosetta's synchronous spawn/import penalty
        # on every conversion.
        return multiprocessing.get_context("forkserver")
    return multiprocessing.get_context("spawn")


def _html_runtime_probe() -> None:
    """No-op clean child used to initialize the production start runtime."""


def _warm_html_process_runtime_sync() -> None:
    global _PROCESS_RUNTIME_WARM

    with _PROCESS_RUNTIME_LOCK:
        if _PROCESS_RUNTIME_WARM:
            return
        context = _process_context()
        process = context.Process(
            target=_html_runtime_probe,
            name="fetchaller-parser-runtime-probe",
            daemon=True,
        )
        process.start()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join()
        if process.exitcode != 0:
            raise HtmlProcessingError("The isolated parser process runtime failed its startup probe.")
        _PROCESS_RUNTIME_WARM = True


async def warm_html_process_runtime() -> None:
    """Initialize forkserver/spawn before the server reports readiness."""

    await asyncio.to_thread(_warm_html_process_runtime_sync)


def _apply_worker_limits(timeout: float) -> None:
    """Bound a parser worker's memory and CPU on platforms that support it."""
    if not sys.platform.startswith("linux"):
        return

    try:
        import resource

        address_soft, address_hard = resource.getrlimit(resource.RLIMIT_AS)
        address_limit = _worker_address_space_limit()
        if address_soft != resource.RLIM_INFINITY:
            address_limit = min(address_limit, address_soft)
        if address_hard != resource.RLIM_INFINITY:
            address_limit = min(address_limit, address_hard)
        resource.setrlimit(resource.RLIMIT_AS, (address_limit, address_hard))

        cpu_soft, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
        cpu_limit = max(1, min(120, math.ceil(timeout) + 1))
        if cpu_soft != resource.RLIM_INFINITY:
            cpu_limit = min(cpu_limit, cpu_soft)
        if cpu_hard != resource.RLIM_INFINITY:
            cpu_limit = min(cpu_limit, cpu_hard)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_hard))
    except (OSError, ValueError):
        # The parent still owns a hard wall-clock timeout and kills this
        # disposable process. RLIMIT is defense in depth on Linux.
        pass


def _worker_address_space_limit() -> int:
    """Allow Rosetta's translator mappings while retaining a hard AS bound."""

    if not sys.platform.startswith("linux"):
        return _PROCESS_MEMORY_LIMIT
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpu_info:
            prefix = cpu_info.read(16_384)
    except OSError:
        return _PROCESS_MEMORY_LIMIT
    if re.search(r"^vendor_id\s*:\s*VirtualApple\s*$", prefix, re.MULTILINE):
        # Docker Desktop's amd64-on-Apple runtime reserves substantially more
        # virtual address space than native x86_64. A 1 GiB RLIMIT_AS makes
        # even a tiny lxml parse die in Rosetta before Python can report an
        # error. The input/output caps and disposable worker remain enforced.
        return _VIRTUAL_APPLE_PROCESS_MEMORY_LIMIT
    return _PROCESS_MEMORY_LIMIT


def _stop_process(process: multiprocessing.Process) -> None:
    """Terminate and synchronously reap a disposable parser process."""
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.25)
    if process.is_alive():
        process.kill()
        process.join()
    else:
        process.join(timeout=0)


def _stop_process_and_release(
    process: multiprocessing.Process,
    handle: SlotHandle,
) -> None:
    """Reap ``process``, then hand its slot back. Runs on a cleanup thread."""
    try:
        _stop_process(process)
    finally:
        handle.release_from_thread()


def _bounded_text(text: str, max_chars: int, marker: str) -> str:
    """Bound worker output while preserving an explicit truncation marker."""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return marker[:max_chars]
    return text[: max_chars - len(marker)].rstrip() + marker


def validate_html_input_size(html: str | bytes) -> None:
    """Reject HTML before decoding or parsing can amplify its memory use."""
    if isinstance(html, bytes):
        size = len(html)
        limit = _MAX_HTML_INPUT_BYTES
        unit = "bytes"
    else:
        size = len(html)
        limit = _MAX_HTML_INPUT_CHARS
        unit = "characters"
    if size > limit:
        raise HtmlProcessingError(f"HTML input is too large to process safely ({size} {unit}; max {limit}).")


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
_JUNK_AND_REDFLAGDEALS_SELECTOR = ", ".join(
    _JUNK_SELECTORS_LIST + _forums.SELECTORS_LIST + _redflagdeals.SELECTORS_LIST
)
_JUNK_AND_STACKOVERFLOW_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _stackoverflow.SELECTORS_LIST)
_JUNK_AND_FORUM_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _forums.SELECTORS_LIST)
_JUNK_AND_COSTCO_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _costco.SELECTORS_LIST)
_JUNK_AND_PETSMART_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _petsmart.SELECTORS_LIST)
_JUNK_AND_CRAIGSLIST_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _craigslist.SELECTORS_LIST)
_JUNK_AND_DIGIKEY_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _digikey.SELECTORS_LIST)
_JUNK_AND_EBAY_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _ebay.SELECTORS_LIST)
_JUNK_AND_FCC_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _fcc.SELECTORS_LIST)
_JUNK_AND_MOLEX_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _molex.SELECTORS_LIST)
_JUNK_AND_MOUSER_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _mouser.SELECTORS_LIST)
_JUNK_AND_SOYLENT_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _soylent.SELECTORS_LIST)
_JUNK_AND_WIKIPEDIA_SELECTOR = ", ".join(_JUNK_SELECTORS_LIST + _wikipedia.SELECTORS_LIST)

# Pre-compiled regex for whitespace cleanup
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# Known code language class names (for code_language_callback)
_CODE_LANGUAGES = frozenset(
    (
        "python",
        "javascript",
        "js",
        "java",
        "cpp",
        "c",
        "go",
        "rust",
        "ruby",
        "bash",
        "sh",
        "sql",
        "json",
        "yaml",
        "xml",
        "html",
        "css",
        "typescript",
        "ts",
        "kotlin",
        "swift",
        "php",
        "r",
        "scala",
        "perl",
        "lua",
        "shell",
    )
)
_CODE_LANG_PREFIXES = ("language-", "lang-", "highlight-")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_code_language(el):
    """Extract code language from class attribute of pre/code elements."""
    for cls in el.get("class") or []:
        for prefix in _CODE_LANG_PREFIXES:
            if cls.startswith(prefix):
                return cls[len(prefix) :]
        if cls in _CODE_LANGUAGES:
            return cls
    code = el.find("code")
    if code:
        for cls in code.get("class") or []:
            for prefix in _CODE_LANG_PREFIXES:
                if cls.startswith(prefix):
                    return cls[len(prefix) :]
    return None


# Reusable markdown converter (options are constant). Calling convert_soup() on
# the already-parsed soup avoids markdownify()'s internal str(body) serialize +
# full re-parse of every HTML document — ~10% of the HTML path's CPU on large
# pages. Output is identical after the newline collapse in _html_to_markdown_sync
# (the re-parse only ever differed by collapsible blank-line runs).
_MD_CONVERTER = MarkdownConverter(
    heading_style="ATX",
    bullets="-",
    escape_asterisks=False,
    escape_underscores=False,
    table_infer_header=True,
    code_language_callback=_extract_code_language,
)


_YOUTUBE_EMBED_RE = re.compile(r"https?://(?:www\.)?youtube(?:-nocookie)?\.com/embed/([a-zA-Z0-9_-]{11})")


_GENERIC_IFRAME_TITLES = {"youtube video player", "youtube video", ""}


def _convert_youtube_iframes(soup: BeautifulSoup) -> None:
    """Convert YouTube iframes to plain <a> links before iframe removal."""
    for iframe in list(soup.find_all("iframe")):
        src = iframe.get("src") or iframe.get("data-src") or ""
        m = _YOUTUBE_EMBED_RE.search(src)
        if m:
            video_id = m.group(1)
            url = f"https://www.youtube.com/watch?v={video_id}"
            link = soup.new_tag("a", href=url)
            # Use iframe title as link text if it's a real video title
            title = (iframe.get("title") or "").strip()
            link.string = url if title.lower() in _GENERIC_IFRAME_TITLES else title
            iframe.replace_with(link)


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


# Site chrome — nav, header, footer — is stripped as junk, which is right for
# reading a page and wrong for the one link a crawler is usually there to find.
# A company's careers page is almost never linked from the body copy; it lives
# in exactly the regions removed. These lift only that link back out.
# Matched as WHOLE path segments, never as substrings. "job" as a substring
# claims /books/steve-jobs, /blog/jobs-report-2026 and /products/jobscheduler
# are careers pages, and announcing one of those as a careers link is worse than
# staying silent — it asserts something false about the site.
_CAREERS_PATH_SEGMENTS = frozenset(
    {
        "career",
        "careers",
        "job",
        "jobs",
        "joinus",
        "join-us",
        "join-our-team",
        "work-with-us",
        "work-for-us",
        "working-at",
        "hiring",
        "we-are-hiring",
        "vacancies",
        "vacancy",
        "employment",
        "open-roles",
        "open-positions",
        "opportunities",
        "recruitment",
    }
)
# Link text is the weaker signal and only unambiguous phrases belong here.
# A bare "jobs" is the anchor text of "Jobs report" and "Steve Jobs" alike; the
# path segment above is what identifies those correctly.
_CAREERS_TEXT_HINTS = (
    "careers",
    "join us",
    "join our team",
    "work with us",
    "work for us",
    "we're hiring",
    "we are hiring",
    "open roles",
    "open positions",
    "vacancies",
)
_CHROME_TAGS = ("nav", "footer", "header")
_MAX_PRESERVED_CAREERS_LINKS = 5
_MAX_CHROME_LINKS_SCANNED = 500


def _has_careers_path_segment(href: str) -> bool:
    """True if the URL path has a whole segment naming a careers page."""
    try:
        path = urlsplit(href).path
    except ValueError:
        return False
    return any(segment.lower() in _CAREERS_PATH_SEGMENTS for segment in path.split("/") if segment)


def _collect_careers_links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Find careers/jobs links inside page chrome, before that chrome is removed.

    Deliberately narrow: a whole path segment or an unambiguous link phrase that
    names hiring, nothing else. Widening this to "all nav links" would re-add
    the hundreds of chrome links that stripping exists to remove (python.org
    alone carries 140).
    """
    found: list[tuple[str, str]] = []
    scanned = 0
    for container in soup.find_all(_CHROME_TAGS):
        # `<nav>` inside `<header>` is the common case, and find_all returns
        # both. Descending into a nested container would walk the same anchors
        # twice and could exhaust the scan budget before a later real careers
        # link is reached, so only outermost chrome is walked.
        if container.find_parent(_CHROME_TAGS) is not None:
            continue
        for anchor in container.find_all("a", href=True):
            scanned += 1
            if scanned > _MAX_CHROME_LINKS_SCANNED:
                return found
            href = anchor["href"].strip()
            # Same-page anchors and UI actions go nowhere a crawler can follow.
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            text = anchor.get_text(strip=True)
            lowered_text = text.lower()
            if not (
                _has_careers_path_segment(href)
                or any(hint in lowered_text for hint in _CAREERS_TEXT_HINTS)
            ):
                continue
            # Deduplication happens after resolution, in _append_careers_links:
            # "/careers" and "https://example.com/careers" are the same
            # destination and only become comparable once both are absolute.
            found.append((text or "Careers", href))
            if len(found) >= _MAX_PRESERVED_CAREERS_LINKS:
                return found
    return found


def _append_careers_links(
    soup: BeautifulSoup,
    links: list[tuple[str, str]],
    base_url: str | None = None,
) -> None:
    """Re-attach preserved careers links at the end of the body.

    Appended after junk removal so the new nodes are not themselves stripped.
    Unlike body links — where ``_resolve_urls`` deliberately leaves hrefs
    relative to save tokens — these are made absolute: the entire point of
    keeping them is that the caller can go fetch them, and by then the page's
    base URL is no longer at hand. At most five links, so the cost is nil.
    """
    if not links:
        return
    body = soup.find("body") or soup
    if body is None:
        return
    if base_url:
        base_tag = soup.find("base", href=True)
        if base_tag:
            base_url = urljoin(base_url, base_tag["href"])

    # Resolve first, then deduplicate. "/careers" and
    # "https://example.com/careers" are one destination written two ways, and
    # they only become comparable once both are absolute.
    resolved: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text, href in links:
        target = urljoin(base_url, href) if base_url else href
        key = target.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        resolved.append((text, target))
    if not resolved:
        return

    section = soup.new_tag("p")
    section.append(soup.new_string("Careers links (from site navigation): "))
    for index, (text, target) in enumerate(resolved):
        if index:
            section.append(soup.new_string(" · "))
        anchor = soup.new_tag("a", href=target)
        anchor.string = text
        section.append(anchor)
    body.append(section)


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
_GENERIC_JSONLD_MARKER_RE = re.compile(r"__GENERIC_JSONLD__([\s\S]*?)__GENERIC_JSONLD__\n*")
_MAX_GENERIC_JSONLD_SCRIPT_CHARS = 1_000_000
_MAX_GENERIC_JSONLD_ITEMS = 100
_MAX_GENERIC_JSONLD_OFFERS = 20
_MAX_GENERIC_JSONLD_SPECS = 50


def _bounded_jsonld_scalar(value: object, maximum: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    try:
        text = " ".join(str(value).split())
    except (OverflowError, ValueError):
        return ""
    return text if text and len(text) <= maximum else ""


def _extract_generic_jsonld(soup: BeautifulSoup) -> None:
    """Extract schema.org Product data from JSON-LD as a fallback.

    Fires for sites without dedicated modules. Extracts name, brand,
    description, price/availability, and additionalProperty specs.

    Uses the same marker-injection pattern as site-specific extractors
    (eBay, Molex) so structured data survives the CSS removal + markdownify
    pipeline.
    """
    for script in soup.find_all(
        "script",
        type="application/ld+json",
        limit=20,
    ):
        source = script.string or ""
        if not isinstance(source, str) or len(source) > _MAX_GENERIC_JSONLD_SCRIPT_CHARS:
            continue
        try:
            data = json.loads(source)
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items[:_MAX_GENERIC_JSONLD_ITEMS]:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue

            lines = []

            # Product name
            name = _bounded_jsonld_scalar(item.get("name"), 500)
            if name:
                lines.append(f"**Product:** {name}")

            # Brand
            brand = item.get("brand")
            if isinstance(brand, dict):
                brand_name = brand.get("name")
            else:
                brand_name = brand
            brand_name = _bounded_jsonld_scalar(brand_name, 500)
            if brand_name:
                lines.append(f"**Brand:** {brand_name}")

            # Description
            desc = _bounded_jsonld_scalar(item.get("description"), 4_000)
            if desc and desc != name:
                lines.append(f"**Description:** {desc}")

            # SKU / MPN
            sku = _bounded_jsonld_scalar(item.get("sku"), 256)
            mpn = _bounded_jsonld_scalar(item.get("mpn"), 256)
            if mpn:
                lines.append(f"**MPN:** {mpn}")
            if sku and sku != mpn:
                lines.append(f"**SKU:** {sku}")

            # Offers (price, availability)
            offers = item.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if isinstance(offers, list):
                for offer in offers[:_MAX_GENERIC_JSONLD_OFFERS]:
                    if not isinstance(offer, dict):
                        continue
                    price = _bounded_jsonld_scalar(offer.get("price"), 128)
                    currency = _bounded_jsonld_scalar(
                        offer.get("priceCurrency"),
                        16,
                    )
                    formatted_price = f"{currency} {price}".strip()
                    if has_positive_price(
                        formatted_price,
                        require_currency=True,
                    ):
                        lines.append(f"**Price:** {formatted_price}")
                    avail = _bounded_jsonld_scalar(
                        offer.get("availability"),
                        256,
                    )
                    if avail:
                        avail_label = avail.rsplit("/", 1)[-1]
                        lines.append(f"**Availability:** {avail_label}")

            # Specifications from additionalProperty
            specs = item.get("additionalProperty", [])
            if isinstance(specs, dict):
                specs = [specs]
            elif not isinstance(specs, list):
                specs = []
            spec_lines = []
            for spec in specs[:_MAX_GENERIC_JSONLD_SPECS]:
                if not isinstance(spec, dict):
                    continue
                spec_name = _bounded_jsonld_scalar(spec.get("name"), 256)
                spec_value = _bounded_jsonld_scalar(spec.get("value"), 1_000)
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
                    marker.string = _GENERIC_JSONLD_MARKER + "\n".join(lines) + _GENERIC_JSONLD_MARKER
                    body.insert(0, marker)
                return  # Only process the first Product


def _postprocess_generic_jsonld(markdown: str) -> str:
    """Inject generic JSON-LD data after first heading."""
    m = _GENERIC_JSONLD_MARKER_RE.search(markdown)
    if not m:
        return markdown

    jsonld_content = m.group(1).strip()
    markdown = markdown[: m.start()] + markdown[m.end() :]

    heading_m = re.search(r"(# [^\n]+\n)", markdown)
    if heading_m:
        insert_pos = heading_m.end()
        markdown = markdown[:insert_pos] + f"\n{jsonld_content}\n" + markdown[insert_pos:]
    else:
        markdown = f"{jsonld_content}\n\n{markdown}"

    return markdown


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def _detect_site(url: str | None, is_reddit: bool, soup: BeautifulSoup | None = None) -> str | None:
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
    if url and _ashby.is_ashby(url):
        return "ashby"
    if url and _costco.is_costco(url):
        return "costco"
    if url and _petsmart.is_petsmart(url):
        return "petsmart"
    if url and _craigslist.is_craigslist(url):
        return "craigslist"
    if url and _digikey.is_digikey(url):
        return "digikey"
    if url and _ebay.is_ebay(url):
        return "ebay"
    if url and _fcc.is_fcc(url):
        return "fcc"
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
    if url and _workatastartup.is_workatastartup(url):
        return "workatastartup"
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
    "costco": _JUNK_AND_COSTCO_SELECTOR,
    "petsmart": _JUNK_AND_PETSMART_SELECTOR,
    "craigslist": _JUNK_AND_CRAIGSLIST_SELECTOR,
    "digikey": _JUNK_AND_DIGIKEY_SELECTOR,
    "ebay": _JUNK_AND_EBAY_SELECTOR,
    "fcc": _JUNK_AND_FCC_SELECTOR,
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


def clean_html(html: str, is_reddit: bool = False, url: str | None = None) -> tuple[BeautifulSoup, str | None]:
    """
    Parse HTML and remove junk elements.

    Args:
        html: Raw HTML string
        is_reddit: If True, also remove Reddit-specific elements
        url: Page URL for site detection and relative URL resolution

    Returns:
        Tuple of (cleaned BeautifulSoup object, detected site key or None)
    """
    validate_html_input_size(html)

    soup = BeautifulSoup(html, "lxml")

    # Detect site type (single pass, reused by caller)
    site = _detect_site(url, is_reddit, soup)

    # Discourse: content lives inside <noscript> for SEO crawlers.
    # FCC: fix broken nav nesting and extract structured data before selectors fire
    if site == "fcc":
        _fcc.fix_fcc_nav(soup)
        _fcc.extract_fcc_data(soup)

    # Unwrap the noscript containing #main-outlet before generic selectors strip it.
    if site == "discourse":
        for noscript in soup.find_all("noscript"):
            if noscript.find(id="main-outlet"):
                noscript.unwrap()
                break

    # Amazon: extract related products before CSS selectors remove sims-* sections
    if site == "amazon":
        _amazon.extract_related_products(soup)

    # Ashby: extract posting + application form from window.__appData before
    # the generic script selector fires and decomposes it.
    if site == "ashby":
        _ashby.extract_ashby_data(soup, url)

    # Work at a Startup: extract Inertia data-page JSON before script removal.
    if site == "workatastartup":
        _workatastartup.extract_workatastartup_data(soup, url)

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

    # PetSmart: extract rating from JSON-LD before scripts are removed
    if site == "petsmart":
        _petsmart.pre_clean_petsmart(soup)

    # Generic: extract JSON-LD Product data for sites without dedicated modules
    if site is None:
        _extract_generic_jsonld(soup)

    # Convert YouTube iframes to plain links before the generic iframe selector
    # destroys them.  A link is always more useful than a silent removal.
    _convert_youtube_iframes(soup)

    # Read the chrome before it is removed; re-attached below.
    #
    # Generic sites only. On a site with a dedicated module — Reddit, GitHub,
    # Amazon, a forum — the chrome is the product's own navigation, and its
    # "/r/jobs" or "Jobs" link is not the company careers page anyone came for.
    # This exists for company sites, which are exactly the ones with no module.
    careers_links = _collect_careers_links(soup) if site is None else []

    # Single-pass removal using combined CSS selector
    selector = _SITE_SELECTORS.get(site, _JUNK_SELECTOR)
    for element in soup.select(selector):
        element.decompose()

    # Strip useless links (javascript:, bare #) before conversion
    _strip_junk_links(soup)

    _append_careers_links(soup, careers_links, url)

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
    elif site == "fcc":
        _fcc.strip_fcc_junk(soup)
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


def _html_to_markdown_sync(
    html: str,
    is_reddit: bool = False,
    url: str | None = None,
    max_output_chars: int = _MAX_HTML_OUTPUT_CHARS,
) -> tuple[str, str | None]:
    """
    Convert HTML to clean markdown (CPU-bound, synchronous).

    Args:
        html: Raw HTML string
        is_reddit: If True, also clean Reddit-specific elements
        url: Page URL for site detection and URL resolution
        max_output_chars: Maximum markdown characters returned to the parent

    Returns:
        Tuple of (markdown_content, title)
    """
    soup, site = clean_html(html, is_reddit, url)

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    if title:
        title = _bounded_text(
            title,
            _MAX_HTML_TITLE_CHARS,
            " [title truncated]",
        )

    # Get body content (or full document if no body)
    body = soup.find("body") or soup

    # Convert to markdown directly from the already-parsed soup (skips a full
    # serialize + re-parse — see _MD_CONVERTER).
    markdown = _MD_CONVERTER.convert_soup(body)

    # Clean up excessive whitespace
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()

    # Site-specific markdown post-processing (elif — a URL matches at most one site)
    if site == "alibaba":
        markdown = _alibaba.postprocess_alibaba(markdown)
    elif site == "aliexpress":
        markdown = _aliexpress.postprocess_aliexpress(markdown)
    elif site == "amazon":
        markdown = _amazon.postprocess_amazon(markdown)
    elif site == "ashby":
        markdown = _ashby.postprocess_ashby(markdown)
    elif site == "costco":
        markdown = _costco.postprocess_costco(markdown)
    elif site == "petsmart":
        markdown = _petsmart.postprocess_petsmart(markdown)
    elif site == "craigslist":
        markdown = _craigslist.postprocess_craigslist(markdown)
    elif site == "digikey":
        markdown = _digikey.postprocess_digikey(markdown)
    elif site == "ebay":
        markdown = _ebay.postprocess_ebay(markdown)
    elif site == "fcc":
        markdown = _fcc.postprocess_fcc(markdown)
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
    elif site == "workatastartup":
        markdown = _workatastartup.postprocess_workatastartup(markdown)
    elif site is None:
        markdown = _postprocess_generic_jsonld(markdown)

    # Strip "| SiteName" suffix from titles (eBay, other sites)
    if title:
        title = re.sub(r"\s*\|\s*eBay(?:\s+\w+)?\s*$", "", title)

    # Add title header only if markdown doesn't already start with a heading
    if title and not markdown.startswith("# "):
        markdown = f"# {title}\n\n{markdown}"

    markdown = _bounded_text(
        markdown,
        max_output_chars,
        "\n\n[HTML conversion truncated at the safe processing limit]",
    )

    return markdown, title


def _html_worker(
    send_connection: Connection,
    html: str,
    is_reddit: bool,
    url: str | None,
    max_output_chars: int,
    timeout: float,
) -> None:
    """Run one HTML conversion in an OS-isolated worker."""
    try:
        _apply_worker_limits(timeout)
        result = _html_to_markdown_sync(
            html,
            is_reddit,
            url,
            max_output_chars=max_output_chars,
        )
        send_connection.send(("result", result))
    except HtmlProcessingError as exc:
        try:
            send_connection.send(("processing_error", str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    except BaseException:
        # Do not expose native-parser internals or attacker-controlled details.
        try:
            send_connection.send(("error", None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send_connection.close()


def _html_worker_from_pipe(
    send_connection: Connection,
    task_connection: Connection,
) -> None:
    """Receive parser input after spawn so startup never pickles attacker HTML."""

    try:
        task = task_connection.recv()
    except (EOFError, OSError):
        send_connection.close()
        return
    finally:
        task_connection.close()
    _html_worker(send_connection, *task)


def _close_and_stop_html_process(
    process: multiprocessing.Process,
    *connections: Connection,
) -> None:
    """Close parser IPC and reap a process from a non-event-loop thread."""

    for connection in connections:
        connection.close()
    _stop_process(process)


def _defer_html_process_cleanup(
    process: multiprocessing.Process,
    handle: SlotHandle,
    *connections: Connection,
) -> None:
    """Reap after cancellation, then hand the parser slot back.

    Without the handle the caller's ``finally`` released the slot while this
    thread was still reaping, so a cancellation storm could run more children
    than the cap allows.
    """

    def _close_stop_and_release() -> None:
        try:
            _close_and_stop_html_process(process, *connections)
        finally:
            handle.release_from_thread()

    cleanup_thread = threading.Thread(
        target=_close_stop_and_release,
        name="fetchaller-html-parser-cleanup",
        daemon=True,
    )
    handle.transfer()
    try:
        cleanup_thread.start()
    except RuntimeError:
        handle.untransfer()
        _close_and_stop_html_process(process, *connections)


def _start_html_process(
    process: multiprocessing.Process,
    connections: tuple[Connection, ...],
    state: dict[str, bool],
    state_lock: threading.Lock,
) -> bool:
    """Start a parser and retain cleanup ownership if startup was cancelled."""

    try:
        process.start()
    except BaseException:
        _close_and_stop_html_process(
            process,
            *connections,
        )
        raise
    cleanup = False
    with state_lock:
        state["started"] = True
        if state["cancelled"] and not state["cleanup_claimed"]:
            state["cleanup_claimed"] = True
            cleanup = True
    if cleanup:
        _close_and_stop_html_process(
            process,
            *connections,
        )
        return False
    return True


async def _html_to_markdown_in_process(
    html: str,
    is_reddit: bool,
    url: str | None,
    max_output_chars: int,
    timeout: float,
    handle: SlotHandle,
) -> tuple[str, str | None]:
    """Start, monitor, and always reap one disposable parser process."""
    context = _process_context()
    try:
        receive_connection, send_connection = context.Pipe(duplex=False)
        task_receive_connection, task_send_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=_html_worker_from_pipe,
            args=(
                send_connection,
                task_receive_connection,
            ),
            name="fetchaller-html-parser",
            daemon=True,
        )
    except (OSError, RuntimeError):
        raise HtmlProcessingError("HTML processing failed because an isolated parser could not be started.") from None

    state = {
        "started": False,
        "cancelled": False,
        "cleanup_claimed": False,
    }
    state_lock = threading.Lock()
    connections = (
        receive_connection,
        send_connection,
        task_receive_connection,
        task_send_connection,
    )
    try:
        started = await asyncio.to_thread(
            _start_html_process,
            process,
            connections,
            state,
            state_lock,
        )
    except asyncio.CancelledError:
        cleanup = False
        with state_lock:
            state["cancelled"] = True
            if state["started"] and not state["cleanup_claimed"]:
                state["cleanup_claimed"] = True
                cleanup = True
        if cleanup:
            _defer_html_process_cleanup(
                process,
                handle,
                *connections,
            )
        raise
    except (AssertionError, OSError, RuntimeError):
        send_connection.close()
        receive_connection.close()
        task_receive_connection.close()
        task_send_connection.close()
        await asyncio.to_thread(_stop_process, process)
        raise HtmlProcessingError("HTML processing failed because an isolated parser could not be started.") from None
    if not started:
        raise asyncio.CancelledError
    send_connection.close()
    task_receive_connection.close()

    try:
        await asyncio.to_thread(
            task_send_connection.send,
            (
                html,
                is_reddit,
                url,
                max_output_chars,
                timeout,
            ),
        )
        task_send_connection.close()
        while True:
            if receive_connection.poll():
                try:
                    kind, payload = receive_connection.recv()
                except EOFError:
                    break
                if kind == "result":
                    return payload
                if kind == "processing_error":
                    raise HtmlProcessingError(payload)
                break
            if not process.is_alive():
                break
            await asyncio.sleep(_PROCESS_POLL_INTERVAL)
    finally:
        receive_connection.close()
        task_send_connection.close()
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # Hold the slot until the child is actually reaped; releasing here
            # let a cancellation storm run more parsers than the cap allows.
            # Transfer only AFTER the thread is running. Transferring first
            # made a failed Thread.start() permanently leak the permit: the
            # owner's release() had already become a no-op and no thread was
            # alive to release it.
            cleanup_thread = threading.Thread(
                target=_stop_process_and_release,
                args=(process, handle),
                name="fetchaller-html-parser-cleanup",
                daemon=True,
            )
            handle.transfer()
            try:
                cleanup_thread.start()
            except RuntimeError:
                handle.untransfer()
                await asyncio.to_thread(_stop_process, process)
        else:
            await asyncio.to_thread(_stop_process, process)

    raise HtmlProcessingError("HTML processing failed because the parser process exited unexpectedly.")


async def html_to_markdown(
    html: str,
    is_reddit: bool = False,
    url: str | None = None,
    *,
    timeout: float = _HTML_PROCESSING_TIMEOUT,
    max_output_chars: int = _MAX_HTML_OUTPUT_CHARS,
) -> tuple[str, str | None]:
    """
    Convert HTML to clean markdown without blocking the event loop.

    Runs CPU-bound parsing in a disposable worker process.

    Args:
        html: Raw HTML string
        is_reddit: If True, also clean Reddit-specific elements
        url: Page URL for site detection and URL resolution
        timeout: End-to-end parser queue and processing timeout
        max_output_chars: Maximum markdown characters returned

    Returns:
        Tuple of (markdown_content, title)
    """
    validate_html_input_size(html)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = 0
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        raise HtmlProcessingError("HTML processing timeout must be greater than zero.")
    if not isinstance(max_output_chars, int) or isinstance(max_output_chars, bool):
        raise HtmlProcessingError("HTML output limit must be a positive integer.")
    if max_output_chars <= 0:
        raise HtmlProcessingError("HTML output limit must be greater than zero.")
    timeout_value = min(timeout_value, _MAX_PROCESSING_TIMEOUT)
    effective_output_chars = min(max_output_chars, _MAX_HTML_OUTPUT_CHARS)

    try:
        async with asyncio.timeout(timeout_value):
            slots = _html_slots()
            await slots.acquire()
            handle = SlotHandle(slots, asyncio.get_running_loop())
            try:
                return await _html_to_markdown_in_process(
                    html,
                    is_reddit,
                    url,
                    effective_output_chars,
                    timeout_value,
                    handle,
                )
            finally:
                # No-op when a cleanup thread has taken ownership; that thread
                # releases only after the child is actually gone.
                handle.release()
    except TimeoutError:
        raise HtmlProcessingError(f"HTML processing timed out after {timeout_value:g}s.") from None
