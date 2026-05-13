"""Fetch tool - main URL fetching functionality."""

import os
import re
import struct
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import wafer

from ..cache.response_cache import ResponseCache
from ..config import Config, get_wafer_cache_dir
from ..content.alibaba import (
    extract_product_id_from_url as extract_alibaba_product_id,
)
from ..content.alibaba import (
    is_alibaba_search_url,
)
from ..content.aliexpress import extract_product_id_from_url, extract_search_products, is_aliexpress_search_url
from ..content.amazon import is_amazon_store
from ..content.ashby import (
    extract_ashby_board_slug,
    fetch_ashby_board,
    is_ashby_board_url,
    is_ashby_embed_url,
    render_ashby_board,
    resolve_ashby_embed_url,
)
from ..content.cornerstone import (
    fetch_cornerstone_board,
    fetch_cornerstone_job,
    is_cornerstone_board_url,
    is_cornerstone_url,
    render_cornerstone_board,
    render_cornerstone_job,
)
from ..content.costco import is_costco as _is_costco
from ..content.costco import is_costco_category_url as _is_costco_category
from ..content.costco import is_costco_search_url as _is_costco_search
from ..content.craigslist import is_craigslist_search_url as _is_craigslist_search
from ..content.dayforce import (
    fetch_dayforce_board,
    fetch_dayforce_job,
    is_dayforce_board_url,
    is_dayforce_url,
    render_dayforce_board,
    render_dayforce_job,
)
from ..content.digikey import is_digikey as _is_digikey
from ..content.facebook_marketplace import (
    extract_listing_id as _extract_fb_listing_id,
)
from ..content.facebook_marketplace import (
    is_facebook_marketplace_listing as _is_fb_listing,
)
from ..content.facebook_marketplace import (
    is_facebook_marketplace_search as _is_fb_search,
)
from ..content.forums import (
    discover_feed_url,
    format_feed_as_markdown,
    is_discourse_html,
    is_forum_html,
    is_thread_url,
    parse_feed,
    transform_forum_url,
)
from ..content.gem import (
    extract_gem_board_slug,
    extract_gem_params,
    fetch_gem_board,
    fetch_gem_job,
    is_gem_board_url,
    is_gem_url,
    render_gem_board,
    render_gem_job,
)
from ..content.github import (
    extract_github_file_listing,
    extract_github_issue,
    transform_github_url,
)
from ..content.greenhouse import (
    extract_greenhouse_params,
    extract_greenhouse_params_from_html,
    extract_greenhouse_params_guess,
    fetch_greenhouse_job,
    is_greenhouse_html,
    is_greenhouse_url,
    render_greenhouse_job,
)
from ..content.html import html_to_markdown
from ..content.lever import (
    extract_lever_params,
    fetch_lever_job,
    is_lever_url,
    render_lever_job,
)
from ..content.mouser import is_mouser as _is_mouser
from ..content.pdf import extract_pdf
from ..content.reddit import transform_reddit_url
from ..content.soylent import is_soylent as _is_soylent
from ..content.ti import extract_ti_part_from_pdf_url, fetch_document_sections, is_ti_document_viewer
from ..content.url import normalize_url
from ..kijiji.api import is_kijiji as _is_kijiji
from ..security.ssrf import is_private_host

MAX_RESPONSE_SIZE = 20 * 1024 * 1024  # 20MB

# Byte-level regex to sniff charset from HTML before full decode
_META_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?([a-zA-Z0-9_-]+)', re.IGNORECASE)
_CONTENT_TYPE_CHARSET_RE = re.compile(r'charset=([a-zA-Z0-9_-]+)', re.IGNORECASE)


@dataclass
class FetchResult:
    """Result from fetching a URL."""

    content: bytes
    content_type: str
    status_code: int
    final_url: str
    headers: dict[str, str]


def _decode_content(content: bytes, content_type: str) -> str:
    """Decode bytes using charset from Content-Type or HTML meta, falling back to UTF-8."""
    charset = None
    # 1. Check Content-Type header
    match = _CONTENT_TYPE_CHARSET_RE.search(content_type)
    if match:
        charset = match.group(1).strip()
    # 2. Sniff HTML <meta charset="..."> from raw bytes
    if not charset and b"<meta" in content[:4096]:
        meta_match = _META_CHARSET_RE.search(content[:4096])
        if meta_match:
            charset = meta_match.group(1).decode("ascii", errors="ignore")
    # 3. Try detected charset, fall back to UTF-8
    if charset:
        try:
            return content.decode(charset)
        except (UnicodeDecodeError, LookupError):
            pass
    return content.decode("utf-8", errors="replace")


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


def truncate(text: str, max_tokens: int, chars_per_token: int = 4) -> str:
    """Truncate text to max tokens."""
    max_chars = max_tokens * chars_per_token
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[Truncated at ~{max_tokens} tokens]"


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} TB"


def _image_dimensions(data: bytes, content_type: str) -> tuple[int, int] | None:
    """Parse pixel dimensions from the file header. PNG, GIF, JPEG, WebP."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            fourcc = data[12:16]
            if fourcc == b"VP8 ":  # lossy
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if fourcc == b"VP8L" and len(data) >= 25:  # lossless
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = ((b1 & 0x3F) << 8 | b0) + 1
                h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
                return w, h
            if fourcc == b"VP8X" and len(data) >= 30:  # extended
                w = (data[24] | data[25] << 8 | data[26] << 16) + 1
                h = (data[27] | data[28] << 8 | data[29] << 16) + 1
                return w, h
        if data[:3] == b"\xff\xd8\xff":  # JPEG: scan for SOFn marker
            i = 2
            while i < len(data) - 8:
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xD8, 0xD9):
                    break
                sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
                if marker in sof:
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
    except (struct.error, IndexError):
        pass
    return None


_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


def _format_image_summary(result: "FetchResult", url: str) -> str:
    """Build a short text summary for binary images — they can't be rendered as text."""
    lines = [f"[Image: {result.content_type or 'unknown'}]"]

    filename = None
    disp = result.headers.get("content-disposition", "")
    m = _FILENAME_RE.search(disp) if disp else None
    if m:
        filename = m.group(1).strip()
    else:
        path = urlparse(result.final_url or url).path
        tail = path.rsplit("/", 1)[-1] if path else ""
        if tail:
            filename = tail
    if filename:
        lines.append(f"Filename: {filename}")

    cl = result.headers.get("content-length", "")
    size = int(cl) if cl.isdigit() else len(result.content)
    lines.append(f"Size: {_format_bytes(size)}")

    dims = _image_dimensions(result.content, result.content_type)
    if dims:
        lines.append(f"Dimensions: {dims[0]}x{dims[1]}")

    lm = result.headers.get("last-modified")
    if lm:
        lines.append(f"Modified: {lm}")

    etag = result.headers.get("etag")
    if etag:
        lines.append(f"ETag: {etag}")

    lines.append(f"URL: {result.final_url or url}")
    return "\n".join(lines)


async def fetch_url(
    url: str,
    max_tokens: int = 25000,
    timeout: int = 10,
    raw: bool = False,
    cache: ResponseCache | None = None,
    config: Config | None = None,
    browser_solver=None,
    _skip_aliexpress_intercept: bool = False,
    _skip_alibaba_intercept: bool = False,
    _skip_craigslist_intercept: bool = False,
) -> dict:
    """
    Fetch a URL and return its content.

    Args:
        url: URL to fetch
        max_tokens: Maximum tokens to return (default: 25000)
        timeout: Request timeout in seconds (default: 10)
        raw: Return raw HTML instead of markdown (default: False)
        cache: Optional ResponseCache instance
        config: Optional Config instance
        browser_solver: Optional BrowserSolver for browser-based challenges

    Returns:
        Dict with:
        - content: The fetched content
        - content_type: Type of content (markdown, json, pdf, etc.)
        - url: Final URL (after redirects)
        - error: Error message if failed
    """
    start = time.monotonic()

    # Validate URL
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Invalid protocol: {parsed.scheme}. Only http/https supported."}
    except Exception:
        return {"error": "Invalid URL format. Expected http:// or https:// URL."}

    # SSRF protection
    hostname = parsed.hostname or ""
    if await is_private_host(hostname):
        return {"error": "Access to private/internal hosts is not allowed."}

    # Amazon store pages are JS-rendered SPAs — return helpful message
    if is_amazon_store(url):
        return {
            "error": "Amazon store/brand pages are JavaScript-rendered and not supported. "
            "Use the search tool to find products by brand instead: "
            "search('Brand Name products site:amazon.ca'). "
            "Or fetch individual product pages directly (e.g. amazon.ca/dp/ASIN)."
        }

    # AliExpress product pages — use product tool which tries MTop API first
    ae_product_id = extract_product_id_from_url(url) if not _skip_aliexpress_intercept else None
    if ae_product_id:
        from ..aliexpress.product import get_product

        result = await get_product(
            ae_product_id,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache_key = normalize_url(url)
                cache.set(cache_key, content, "text")
            _log(f"FETCH {url} -> AliExpress product ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Alibaba.com product pages — SSR with embedded JSON
    alibaba_product_id = extract_alibaba_product_id(url) if not _skip_alibaba_intercept else None
    if alibaba_product_id:
        from ..alibaba.product import get_product as get_alibaba_product

        result = await get_alibaba_product(
            alibaba_product_id,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache_key = normalize_url(url)
                cache.set(cache_key, content, "text")
            _log(f"FETCH {url} -> Alibaba product ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Alibaba.com search pages
    if not _skip_alibaba_intercept and is_alibaba_search_url(url):
        from urllib.parse import parse_qs

        from ..alibaba.search import search_alibaba
        qs = parse_qs(urlparse(url).query)
        query = qs.get("SearchText", [""])[0]
        try:
            page_num = int(qs.get("page", ["1"])[0])
        except (ValueError, IndexError):
            page_num = 1

        result = await search_alibaba(
            query=query or "alibaba",
            page=page_num,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache_key = normalize_url(url)
                cache.set(cache_key, content, "text")
            _log(f"FETCH {url} -> Alibaba search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # AliExpress search pages
    if not _skip_aliexpress_intercept and is_aliexpress_search_url(url):
        from urllib.parse import parse_qs

        from ..aliexpress.search import search_aliexpress

        qs = parse_qs(urlparse(url).query)
        try:
            page_num = int(qs.get("page", ["1"])[0])
        except (ValueError, IndexError):
            page_num = 1
        sort = qs.get("sortType", ["default"])[0]
        try:
            min_price = float(qs["minPrice"][0]) if "minPrice" in qs else None
        except (ValueError, IndexError):
            min_price = None
        try:
            max_price = float(qs["maxPrice"][0]) if "maxPrice" in qs else None
        except (ValueError, IndexError):
            max_price = None

        from urllib.parse import unquote
        path = urlparse(url).path
        query = "aliexpress"
        import re
        m = re.search(r"/w/wholesale-(.+?)\.html", path)
        if m:
            query = unquote(m.group(1).replace("-", " "))

        result = await search_aliexpress(
            query=query,
            page=page_num,
            sort=sort,
            min_price=min_price,
            max_price=max_price,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache_key = normalize_url(url)
                cache.set(cache_key, content, "text")
            _log(f"FETCH {url} -> AliExpress search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Mouser product/search pages — use API when key is configured
    if _is_mouser(url):
        mouser_key = os.environ.get("MOUSER_API_KEY")
        if mouser_key:
            from ..mouser.api import get_product as get_mouser_product

            result = await get_mouser_product(url, api_key=mouser_key)
            if "content" in result:
                content = truncate(result["content"], max_tokens)
                if cache:
                    cache.set(normalize_url(url), content, "text")
                _log(f"FETCH {url} -> Mouser API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            if "Could not extract" not in result.get("error", ""):
                return result
            _log(f"FETCH {url} -> Mouser API couldn't parse URL, falling through to HTML")
        else:
            return {
                "error": "Mouser requires an API key (MOUSER_API_KEY). "
                "Get a free key at https://www.mouser.com/api-hub/"
            }

    # DigiKey product/search pages — use API when credentials are configured
    if _is_digikey(url):
        dk_client_id = os.environ.get("DIGIKEY_CLIENT_ID")
        dk_client_secret = os.environ.get("DIGIKEY_CLIENT_SECRET")
        if dk_client_id and dk_client_secret:
            from ..digikey.api import get_product as get_digikey_product

            result = await get_digikey_product(url, client_id=dk_client_id, client_secret=dk_client_secret)
            if "content" in result:
                content = truncate(result["content"], max_tokens)
                if cache:
                    cache.set(normalize_url(url), content, "text")
                _log(f"FETCH {url} -> DigiKey API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            if "Could not extract" not in result.get("error", ""):
                return result
            _log(f"FETCH {url} -> DigiKey API couldn't parse URL, falling through to HTML")
        else:
            return {
                "error": "DigiKey requires API credentials (DIGIKEY_CLIENT_ID + DIGIKEY_CLIENT_SECRET). "
                "Register at https://developer.digikey.com/"
            }

    # Kijiji search/listing pages — use GraphQL API
    if _is_kijiji(url):
        from ..kijiji.api import get_listing as get_kijiji_listing
        from ..kijiji.api import is_kijiji_listing, is_kijiji_search

        if is_kijiji_search(url) or is_kijiji_listing(url):
            result = await get_kijiji_listing(url)
            if "content" in result:
                content = truncate(result["content"], max_tokens)
                if cache:
                    cache.set(normalize_url(url), content, "text")
                _log(f"FETCH {url} -> Kijiji GraphQL ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            if "Could not extract" not in result.get("error", ""):
                return result
            _log(f"FETCH {url} -> Kijiji GraphQL couldn't parse URL, falling through to HTML")

    # Costco search/category pages — CSR, use search API when available
    _costco_redirect_fallback = False
    _costco_fallback_url: str | None = None
    if _is_costco_search(url) or _is_costco_category(url):
        try:
            from ..costco.search import search_costco

            result = await search_costco(url, cache=cache, config=config, browser_solver=browser_solver)
            if "content" in result:
                content = result["content"]
                # If returned 0 results, try category page fallback.
                # Costco's JS frontend redirects some searches (e.g. "macbook",
                # "samsung") to brand/category pages. We can't follow JS redirects
                # so we construct the likely category URL: /{query}.html
                if "No products found" in content:
                    from ..costco.search import extract_search_params
                    sp = extract_search_params(url)
                    if sp:
                        q = sp["query"].strip().lower().replace(" ", "-")
                        d = sp["domain"]
                        _costco_fallback_url = f"https://www.costco.{d}/{q}.html"
                        _costco_redirect_fallback = True
                        _log(f"FETCH {url} -> Costco search returned 0 results, trying category fallback: {_costco_fallback_url}")
                    else:
                        _log(f"FETCH {url} -> Costco search returned 0 results, no fallback available")
                else:
                    content = truncate(content, max_tokens)
                    if cache:
                        cache.set(normalize_url(url), content, "text")
                    _log(f"FETCH {url} -> Costco search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                    return {"content": content, "content_type": "text", "url": url}
            if not _costco_redirect_fallback:
                if "Could not extract" not in result.get("error", ""):
                    return result
                _log(f"FETCH {url} -> Costco search failed, falling through to HTML")
        except ImportError:
            _log(f"FETCH {url} -> Costco search module not yet implemented, falling through to HTML")

    # Craigslist search pages — CSR, use SAPI
    if not _skip_craigslist_intercept and _is_craigslist_search(url):
        from ..craigslist.search import search_craigslist

        result = await search_craigslist(
            url,
            cache=cache,
            config=config,
            browser_solver=browser_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache.set(normalize_url(url), content, "text")
            _log(f"FETCH {url} -> Craigslist search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        if "Could not extract" not in result.get("error", ""):
            return result
        _log(f"FETCH {url} -> Craigslist search failed, falling through to HTML")

    # Facebook Marketplace — 100% CSR, use GraphQL API
    if _is_fb_search(url):
        from ..facebook_marketplace.search import search_marketplace

        result = await search_marketplace(url)
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache.set(normalize_url(url), content, "text")
            _log(f"FETCH {url} -> FB Marketplace search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result

    if _is_fb_listing(url):
        listing_id = _extract_fb_listing_id(url)
        if listing_id:
            from ..facebook_marketplace.listing import get_listing as get_fb_listing

            result = await get_fb_listing(listing_id)
            if "content" in result:
                content = truncate(result["content"], max_tokens)
                if cache:
                    cache.set(normalize_url(url), content, "text")
                _log(f"FETCH {url} -> FB Marketplace listing ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": url}
            return result

    # Greenhouse direct URL (boards.greenhouse.io / job-boards.greenhouse.io
    # / embed iframe URL / any URL carrying ?gh_jid=&gh_src=): fetch the
    # public JSON API directly and render. Skips the SPA body entirely.
    # Also handles company-site URLs like ``dropbox.jobs/en/jobs/{id}?gh_src=X``
    # via a hostname-derived board-token guess that's probed against the API.
    gh_params = extract_greenhouse_params(url)
    gh_guess = None
    if not gh_params:
        gh_guess = extract_greenhouse_params_guess(url)
    if gh_params and is_greenhouse_url(url):
        _gh_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _gh_cache_key:
            _gh_cached = cache.get(_gh_cache_key)
            if _gh_cached:
                content = truncate(_gh_cached.content, max_tokens)
                _log(f"FETCH {url} -> Greenhouse CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gh_cached.content_type, "url": url, "cached": True}
        _gh_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _gh_token, _gh_jid = gh_params
        _gh_data = await fetch_greenhouse_job(_gh_token, _gh_jid, _gh_session)
        if _gh_data:
            markdown = render_greenhouse_job(_gh_data, source_url=url)
            if cache and _gh_cache_key:
                cache.set(_gh_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Greenhouse API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # Greenhouse hostname-derived guess (e.g. dropbox.jobs/en/jobs/{id}?gh_src=X).
    # The board token isn't in the page HTML, so we derive it from the hostname
    # and probe the API. A 404 falls through to normal HTML fetch.
    if gh_guess:
        _gh_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _gh_cache_key:
            _gh_cached = cache.get(_gh_cache_key)
            if _gh_cached:
                content = truncate(_gh_cached.content, max_tokens)
                _log(f"FETCH {url} -> Greenhouse CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gh_cached.content_type, "url": url, "cached": True}
        _gh_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _gh_token, _gh_jid = gh_guess
        _gh_data = await fetch_greenhouse_job(_gh_token, _gh_jid, _gh_session)
        if _gh_data:
            markdown = render_greenhouse_job(_gh_data, source_url=url)
            if cache and _gh_cache_key:
                cache.set(_gh_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Greenhouse API (hostname guess: {_gh_token}, {len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Guess wrong or API unavailable — fall through to normal HTML fetch.

    # Ashby board index (jobs.ashbyhq.com/{org}): fetch listings via public REST.
    # Must run before the generic HTML fetch — without this, board pages return
    # just the SPA spinner and our postprocessor strips it to the page title.
    if is_ashby_board_url(url):
        _ab_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _ab_cache_key:
            _ab_cached = cache.get(_ab_cache_key)
            if _ab_cached:
                content = truncate(_ab_cached.content, max_tokens)
                _log(f"FETCH {url} -> Ashby board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _ab_cached.content_type, "url": url, "cached": True}
        _ab_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _ab_org = extract_ashby_board_slug(url)
        _ab_data = await fetch_ashby_board(_ab_org, _ab_session)
        if _ab_data is not None:
            markdown = render_ashby_board(_ab_data, _ab_org, source_url=url)
            if cache and _ab_cache_key:
                cache.set(_ab_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Ashby board ({len(_ab_data.get('jobs') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API 404 (not an Ashby-hosted board) or transient failure — fall through.

    # Gem direct URL (jobs.gem.com/{board}/{extId}): fetch via public GraphQL.
    if is_gem_url(url):
        _gm_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _gm_cache_key:
            _gm_cached = cache.get(_gm_cache_key)
            if _gm_cached:
                content = truncate(_gm_cached.content, max_tokens)
                _log(f"FETCH {url} -> Gem CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gm_cached.content_type, "url": url, "cached": True}
        _gm_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _gm_board, _gm_ext = extract_gem_params(url)
        _gm_data = await fetch_gem_job(_gm_board, _gm_ext, _gm_session)
        if _gm_data and _gm_data.get("oatsExternalJobPosting"):
            markdown = render_gem_job(_gm_data, source_url=url)
            if cache and _gm_cache_key:
                cache.set(_gm_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Gem GraphQL ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Gem API failed — fall through to normal HTML fetch.

    # Gem board index (jobs.gem.com/{board}): fetch listings via public REST.
    if is_gem_board_url(url):
        _gmb_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _gmb_cache_key:
            _gmb_cached = cache.get(_gmb_cache_key)
            if _gmb_cached:
                content = truncate(_gmb_cached.content, max_tokens)
                _log(f"FETCH {url} -> Gem board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _gmb_cached.content_type, "url": url, "cached": True}
        _gmb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _gmb_slug = extract_gem_board_slug(url)
        _gmb_jobs = await fetch_gem_board(_gmb_slug, _gmb_session)
        if _gmb_jobs is not None:
            markdown = render_gem_board(_gmb_jobs, _gmb_slug, source_url=url)
            if cache and _gmb_cache_key:
                cache.set(_gmb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Gem board ({len(_gmb_jobs)} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API 404 (not a Gem-hosted board) or transient failure — fall through.

    # Lever direct URL (jobs.lever.co/{company}/{id}): fetch JSON API +
    # parse /apply page for the application form, then render as markdown.
    if is_lever_url(url):
        _lv_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _lv_cache_key:
            _lv_cached = cache.get(_lv_cache_key)
            if _lv_cached:
                content = truncate(_lv_cached.content, max_tokens)
                _log(f"FETCH {url} -> Lever CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _lv_cached.content_type, "url": url, "cached": True}
        _lv_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _lv_company, _lv_id = extract_lever_params(url)
        _lv_data = await fetch_lever_job(_lv_company, _lv_id, _lv_session)
        if _lv_data and _lv_data.get("posting"):
            markdown = render_lever_job(_lv_data, source_url=url)
            if cache and _lv_cache_key:
                cache.set(_lv_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Lever API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # Dayforce posting (jobs.dayforcehcm.com/{lang}/{namespace}/{board}/jobs/{id}):
    # the page is SSR'd with full jobData in __NEXT_DATA__. Without this block
    # the generic HTML pipeline strips the Next.js shell down to "Sign In".
    if is_dayforce_url(url):
        _df_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _df_cache_key:
            _df_cached = cache.get(_df_cache_key)
            if _df_cached:
                content = truncate(_df_cached.content, max_tokens)
                _log(f"FETCH {url} -> Dayforce CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _df_cached.content_type, "url": url, "cached": True}
        _df_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _df_data = await fetch_dayforce_job(url, _df_session)
        if _df_data:
            markdown = render_dayforce_job(_df_data, source_url=url)
            if cache and _df_cache_key:
                cache.set(_df_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Dayforce posting ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Parse failed — fall through to normal HTML fetch.

    # Dayforce board index: list of postings comes from a CSRF-protected
    # POST to /api/geo/{namespace}/jobposting/search after hydration.
    if is_dayforce_board_url(url):
        _dfb_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _dfb_cache_key:
            _dfb_cached = cache.get(_dfb_cache_key)
            if _dfb_cached:
                content = truncate(_dfb_cached.content, max_tokens)
                _log(f"FETCH {url} -> Dayforce board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _dfb_cached.content_type, "url": url, "cached": True}
        _dfb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _dfb_data = await fetch_dayforce_board(url, _dfb_session)
        if _dfb_data is not None:
            markdown = render_dayforce_board(_dfb_data, source_url=url)
            if cache and _dfb_cache_key:
                cache.set(_dfb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Dayforce board ({len(_dfb_data.get('jobPostings') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Search call failed — fall through to normal HTML fetch.

    # Cornerstone OnDemand (CSOD) posting: SPA shell with a JWT in
    # csod.context. Hit services/x/job-requisition/v2/requisitions/{id}/jobDetails.
    if is_cornerstone_url(url):
        _cs_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _cs_cache_key:
            _cs_cached = cache.get(_cs_cache_key)
            if _cs_cached:
                content = truncate(_cs_cached.content, max_tokens)
                _log(f"FETCH {url} -> Cornerstone CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _cs_cached.content_type, "url": url, "cached": True}
        _cs_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _cs_data = await fetch_cornerstone_job(url, _cs_session)
        if _cs_data:
            markdown = render_cornerstone_job(_cs_data, source_url=url)
            if cache and _cs_cache_key:
                cache.set(_cs_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Cornerstone posting ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # API failed — fall through to normal HTML fetch.

    # Cornerstone board index: rec-job-search/external/jobs on the regional
    # cloud host carried in csod.context.endpoints.cloud.
    if is_cornerstone_board_url(url):
        _csb_cache_key = normalize_url(url) if cache else None
        if cache and not raw and _csb_cache_key:
            _csb_cached = cache.get(_csb_cache_key)
            if _csb_cached:
                content = truncate(_csb_cached.content, max_tokens)
                _log(f"FETCH {url} -> Cornerstone board CACHED ({time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": _csb_cached.content_type, "url": url, "cached": True}
        _csb_session = wafer.AsyncSession(
            browser_solver=browser_solver,
            timeout=timedelta(seconds=timeout),
            cache_dir=get_wafer_cache_dir(),
        )
        _csb_data = await fetch_cornerstone_board(url, _csb_session)
        if _csb_data is not None:
            markdown = render_cornerstone_board(_csb_data, source_url=url)
            if cache and _csb_cache_key:
                cache.set(_csb_cache_key, markdown, "markdown")
            content = truncate(markdown, max_tokens)
            _log(f"FETCH {url} -> Cornerstone board ({len(_csb_data.get('requisitions') or [])} jobs, {len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "markdown", "url": url}
        # Search call failed — fall through to normal HTML fetch.

    # Transform Reddit URLs
    reddit_result = transform_reddit_url(url)
    fetch_url_str = reddit_result.url
    is_reddit = reddit_result.is_reddit

    # Transform GitHub blob URLs to raw.githubusercontent.com
    github_result = transform_github_url(url)
    is_github = github_result.is_github
    if github_result.is_blob:
        fetch_url_str = github_result.url

    # Tier 1: Transform known forum URLs to RSS/Atom feeds
    forum_result = transform_forum_url(fetch_url_str)
    is_forum_feed = forum_result.is_forum_feed
    if is_forum_feed:
        fetch_url_str = forum_result.url

    # Costco fallback: when search API returns 0 results, fetch the category page
    if _costco_redirect_fallback and _costco_fallback_url:
        fetch_url_str = _costco_fallback_url

    # Compute normalized URL once for cache operations
    cache_key = normalize_url(fetch_url_str) if cache else None

    # Check cache (if not raw mode and cache is available)
    if cache and not raw and cache_key:
        cached = cache.get(cache_key)
        if cached:
            content = truncate(cached.content, max_tokens)
            result_dict = {
                "content": content,
                "content_type": cached.content_type,
                "url": fetch_url_str,
                "cached": True,
            }
            if is_reddit and fetch_url_str != url:
                result_dict["content"] = f"[Fetched via: {fetch_url_str}]\n\n{content}"
            _log(f"FETCH {url} -> CACHED ({time.monotonic() - start:.1f}s)")
            return result_dict

    # Per-domain rate limiting for sites that aggressively block rapid requests
    if _is_costco(fetch_url_str):
        from ..ratelimit import costco_limiter
        await costco_limiter.wait()
    elif is_reddit:
        from ..ratelimit import reddit_limiter
        await reddit_limiter.wait()
    elif _is_soylent(fetch_url_str):
        from ..ratelimit import soylent_limiter
        await soylent_limiter.wait()

    # Fetch the URL using a per-request wafer session.
    # NOTE: Do not use `async with` — __aexit__ calls browser_solver.close()
    # which would destroy the shared BrowserSolver singleton.
    session = wafer.AsyncSession(
        browser_solver=browser_solver,
        timeout=timedelta(seconds=timeout),
        cache_dir=get_wafer_cache_dir(),
    )
    # Bypass Reddit NSFW age gate on old.reddit.com
    if is_reddit:
        session.add_cookie("over18=1; Path=/; Domain=.reddit.com", fetch_url_str)

    # Resolve Ashby embed URLs (e.g. company.com/careers?ashby_jid=<uuid>) to
    # the canonical jobs.ashbyhq.com/{org}/{jid} form before fetching.
    if is_ashby_embed_url(fetch_url_str):
        canonical = await resolve_ashby_embed_url(fetch_url_str, session)
        if canonical:
            _log(f"FETCH {url} -> Ashby embed resolved to {canonical}")
            fetch_url_str = canonical
            cache_key = normalize_url(fetch_url_str) if cache else None
            if cache and not raw and cache_key:
                cached = cache.get(cache_key)
                if cached:
                    content = truncate(cached.content, max_tokens)
                    _log(f"FETCH {url} -> CACHED via embed resolution ({time.monotonic() - start:.1f}s)")
                    return {
                        "content": content,
                        "content_type": cached.content_type,
                        "url": fetch_url_str,
                        "cached": True,
                    }

    try:
        resp = await session.get(fetch_url_str)
        result = FetchResult(
            content=resp.content[:MAX_RESPONSE_SIZE],
            content_type=resp.headers.get("content-type", ""),
            status_code=resp.status_code,
            final_url=str(resp.url),
            headers=dict(resp.headers),
        )
    except wafer.ChallengeDetected as e:
        return {"error": f"Protected by {e.challenge_type} bot detection and could not be bypassed. Try again — this sometimes resolves on retry."}
    except wafer.RateLimited as e:
        retry_msg = f" Retry after {e.retry_after:.0f} seconds." if e.retry_after else ""
        return {"error": f"Rate limited (HTTP 429).{retry_msg}"}
    except wafer.EmptyResponse:
        return {"error": "Server returned an empty response after retries."}
    except wafer.TooManyRedirects:
        return {"error": "Too many redirects (redirect loop detected)."}
    except wafer.ConnectionFailed as e:
        return {"error": f"Connection error: {e.reason}"}
    except wafer.WaferTimeout:
        return {"error": f"Request timed out after {timeout}s. Try increasing the timeout parameter for slow servers."}
    except wafer.WaferError as e:
        return {"error": f"Request failed: {e}"}
    except Exception as e:
        return {"error": f"Fetch failed ({type(e).__name__}): {e}"}

    # SSRF protection: check final URL after redirects (fail-closed)
    if result.final_url and result.final_url != fetch_url_str:
        try:
            final_parsed = urlparse(result.final_url)
            if await is_private_host(final_parsed.hostname or ""):
                return {"error": "Redirect to private/internal host is not allowed."}
        except Exception:
            return {"error": "Could not validate redirect URL safety."}

    # Handle rate limiting (wafer passes 429 through when max_rotations=0)
    if result.status_code == 429:
        retry_after = result.headers.get("retry-after", "")
        retry_msg = f" Retry after {retry_after} seconds." if retry_after else ""
        _log(f"FETCH {url} -> ERROR: 429 rate limited ({time.monotonic() - start:.1f}s)")
        return {"error": f"Rate limited (HTTP 429).{retry_msg}"}

    content_type = result.content_type.lower()

    # Handle errors
    if result.status_code >= 400:
        # Costco fallback 404: the category page doesn't exist, return 0-results message
        if _costco_redirect_fallback and result.status_code == 404:
            _log(f"FETCH {url} -> Costco category fallback 404, returning no-results")
            return {
                "content": f"Costco search returned no results for this query. "
                f"The category page ({_costco_fallback_url}) also does not exist.",
                "content_type": "text",
                "url": url,
            }
        body = _decode_content(result.content, content_type)[:1000]
        _log(f"FETCH {url} -> ERROR: HTTP {result.status_code} ({time.monotonic() - start:.1f}s)")
        return {"error": f"HTTP {result.status_code}", "body": body}

    # JSON
    if "application/json" in content_type:
        text = _decode_content(result.content, content_type)
        return {
            "content": truncate(text, max_tokens),
            "content_type": "json",
            "url": result.final_url,
        }

    # Plain text
    if "text/plain" in content_type:
        text = _decode_content(result.content, content_type)
        return {
            "content": truncate(text, max_tokens),
            "content_type": "text",
            "url": result.final_url,
        }

    # XML/RSS/Atom — try structured feed parsing first (unless raw mode)
    if any(t in content_type for t in ("text/xml", "application/xml", "application/rss+xml", "application/atom+xml")):
        text = _decode_content(result.content, content_type)
        if raw:
            return {
                "content": truncate(text, max_tokens),
                "content_type": "xml",
                "url": result.final_url,
            }
        feed = parse_feed(text)
        if feed and feed.items:
            markdown = format_feed_as_markdown(feed)
            content = truncate(markdown, max_tokens)
            response = {
                "content": content,
                "content_type": "markdown",
                "url": result.final_url,
            }
            if is_forum_feed and forum_result.url != forum_result.original_url:
                response["content"] = f"[Feed: {forum_result.original_url}]\n\n{content}"
            _log(f"FETCH {url} -> feed ({len(feed.items)} items, {len(response['content'])} chars, {time.monotonic() - start:.1f}s)")
            return response
        # Not a feed — return raw XML
        return {
            "content": truncate(text, max_tokens),
            "content_type": "xml",
            "url": result.final_url,
        }

    # CSV
    if "text/csv" in content_type:
        text = _decode_content(result.content, content_type)
        return {
            "content": truncate(text, max_tokens),
            "content_type": "csv",
            "url": result.final_url,
        }

    # PDF
    if "application/pdf" in content_type:
        # TI datasheets: try HTML document viewer
        ti_part = extract_ti_part_from_pdf_url(fetch_url_str)
        if not ti_part and result.final_url:
            ti_part = extract_ti_part_from_pdf_url(result.final_url)
        if ti_part:
            viewer_url = f"https://www.ti.com/document-viewer/{ti_part}/datasheet"
            try:
                viewer_resp = await session.get(viewer_url)
                if viewer_resp.status_code == 200:
                    viewer_html = viewer_resp.text
                    combined = await fetch_document_sections(session, viewer_html, float(timeout))
                    if combined:
                        markdown, _ = await html_to_markdown(combined, url=viewer_url)
                        if cache and cache_key:
                            cache.set(cache_key, markdown, "markdown",
                                      cache_control=result.headers.get("cache-control"))
                        content = truncate(markdown, max_tokens)
                        _log(f"FETCH {url} -> TI doc viewer upgrade ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                        return {
                            "content": f"[Upgraded from PDF to HTML datasheet]\n\n{content}",
                            "content_type": "markdown",
                            "url": viewer_url,
                        }
            except Exception:
                pass  # Fall through to PDF extraction

        pdf_result = await extract_pdf(result.content, config)

        if pdf_result.error:
            return {"error": pdf_result.error}

        if pdf_result.is_empty:
            content = f"[PDF: {pdf_result.page_count} pages. No extractable text found - this may be a scanned document or image-based PDF.]"
        else:
            header = f"[PDF: {pdf_result.page_count} pages.]\n\n"
            chars_per_token = config.chars_per_token if config else 4
            reserved_tokens = len(header) // chars_per_token + 10
            content = header + truncate(pdf_result.text, max_tokens - reserved_tokens, chars_per_token)

        return {
            "content": content,
            "content_type": "pdf",
            "url": result.final_url,
        }

    # HTML - convert to markdown (unless raw mode)
    if "text/html" in content_type or "application/xhtml" in content_type:
        html = _decode_content(result.content, content_type)

        if raw:
            return {
                "content": truncate(html, max_tokens),
                "content_type": "html",
                "url": result.final_url,
            }

        # Greenhouse embed on a company career site (iframe / div#grnhse_app):
        # extract board token + job ID, fetch the public API, and return
        # rendered markdown instead of the JS-loader HTML body.
        from bs4 import BeautifulSoup as _Soup  # noqa: N812
        _gh_soup = _Soup(html, "lxml")
        if is_greenhouse_html(_gh_soup):
            _gh_params = extract_greenhouse_params_from_html(_gh_soup, page_url=result.final_url or url)
            if _gh_params:
                _gh_token, _gh_jid = _gh_params
                _gh_data = await fetch_greenhouse_job(_gh_token, _gh_jid, session)
                if _gh_data:
                    markdown = render_greenhouse_job(_gh_data, source_url=result.final_url or url)
                    if cache and cache_key:
                        cache.set(cache_key, markdown, "markdown")
                    content = truncate(markdown, max_tokens)
                    _log(f"FETCH {url} -> Greenhouse embed API ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                    return {"content": content, "content_type": "markdown", "url": result.final_url}

        # Tier 2: Forum autodiscovery
        if not is_forum_feed and forum_result.forum_software and not forum_result.is_thread:
            feed_url = discover_feed_url(html, result.final_url or url)
            if feed_url:
                try:
                    feed_resp = await session.get(feed_url, timeout=float(timeout))
                    if feed_resp.status_code < 400:
                        feed_text = feed_resp.text
                        feed = parse_feed(feed_text)
                        if feed and feed.items:
                            markdown = format_feed_as_markdown(feed)
                            content = truncate(markdown, max_tokens)
                            response = {
                                "content": f"[Feed: {url}]\n\n{content}",
                                "content_type": "markdown",
                                "url": result.final_url,
                            }
                            _log(f"FETCH {url} -> autodiscovered feed ({len(feed.items)} items, {len(response['content'])} chars, {time.monotonic() - start:.1f}s)")
                            return response
                except Exception:
                    pass
        elif not is_forum_feed and not forum_result.forum_software and not is_thread_url(result.final_url or url):
            from bs4 import BeautifulSoup as _Soup  # noqa: N812

            _quick_soup = _Soup(html[:4096], "lxml")
            if is_forum_html(_quick_soup) or is_discourse_html(_quick_soup):
                feed_url = discover_feed_url(html, result.final_url or url)
                if feed_url:
                    try:
                        feed_resp = await session.get(feed_url, timeout=float(timeout))
                        if feed_resp.status_code < 400:
                            feed_text = feed_resp.text
                            feed = parse_feed(feed_text)
                            if feed and feed.items:
                                markdown = format_feed_as_markdown(feed)
                                content = truncate(markdown, max_tokens)
                                response = {
                                    "content": f"[Feed: {url}]\n\n{content}",
                                    "content_type": "markdown",
                                    "url": result.final_url,
                                }
                                _log(f"FETCH {url} -> autodiscovered feed ({len(feed.items)} items, {len(response['content'])} chars, {time.monotonic() - start:.1f}s)")
                                return response
                    except Exception:
                        pass

        # TI document viewer
        effective_url = result.final_url or url
        if is_ti_document_viewer(effective_url):
            combined = await fetch_document_sections(session, html, float(timeout))
            if combined:
                html = combined

        # AliExpress search extraction
        if not _skip_aliexpress_intercept and is_aliexpress_search_url(effective_url):
            search_content = extract_search_products(html, effective_url)
            if search_content:
                content = truncate(search_content, max_tokens)
                if cache and cache_key:
                    cache.set(cache_key, search_content, "text")
                _log(f"FETCH {url} -> AliExpress search extraction ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": effective_url}

        # GitHub issue/PR/discussion extraction
        if is_github:
            issue_content = extract_github_issue(html, effective_url)
            if issue_content:
                content = truncate(issue_content, max_tokens)
                if cache and cache_key:
                    cache.set(cache_key, issue_content, "text")
                _log(f"FETCH {url} -> GitHub issue extraction ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                return {"content": content, "content_type": "text", "url": effective_url}

        # GitHub file listing extraction
        file_listing = None
        if is_github:
            file_listing = extract_github_file_listing(html, effective_url)

        markdown, _ = await html_to_markdown(html, is_reddit=is_reddit, url=result.final_url)

        # Prepend file listing if found
        if file_listing:
            if "\n---\n" in file_listing:
                markdown = file_listing
            elif len(markdown) > 200:
                markdown = file_listing + "\n\n---\n\n" + markdown
            else:
                markdown = file_listing

        # Cache full content, truncate only for response
        if cache and cache_key:
            cache.set(
                cache_key,
                markdown,
                "markdown",
                cache_control=result.headers.get("cache-control"),
            )

        content = truncate(markdown, max_tokens)

        response = {
            "content": content,
            "content_type": "markdown",
            "url": result.final_url,
        }

        # Note if we transformed the URL
        if _costco_redirect_fallback:
            note = "[Costco search returned no direct results and redirected to a category page]"
            if result.final_url and result.final_url != fetch_url_str:
                note += f"\n[Redirected to: {result.final_url}]"
            response["content"] = f"{note}\n\n{content}"
        elif is_reddit and fetch_url_str != url:
            response["content"] = f"[Fetched via: {fetch_url_str}]\n\n{content}"
        elif github_result.is_blob and fetch_url_str != url:
            response["content"] = f"[Fetched raw: {fetch_url_str}]\n\n{content}"
        elif result.final_url and result.final_url != fetch_url_str:
            response["content"] = f"[Redirected to: {result.final_url}]\n\n{content}"

        _log(f"FETCH {url} -> {response['content_type']} ({len(response['content'])} chars, {time.monotonic() - start:.1f}s)")
        return response

    # SVG - textual XML, return as raw
    if "image/svg+xml" in content_type or "image/svg" in content_type:
        text = _decode_content(result.content, content_type)
        return {
            "content": truncate(text, max_tokens),
            "content_type": "svg",
            "url": result.final_url,
        }

    # Binary images - return metadata only (can't render bytes as text)
    if content_type.startswith("image/"):
        summary = _format_image_summary(result, fetch_url_str)
        return {
            "content": summary,
            "content_type": "text",
            "url": result.final_url,
        }

    # Any other text-based content type
    if content_type.startswith("text/") or content_type.startswith("application/javascript"):
        text = _decode_content(result.content, content_type)
        return {
            "content": truncate(text, max_tokens),
            "content_type": "text",
            "url": result.final_url,
        }

    # Unsupported content type
    return {"error": f"Unsupported content type: {content_type}"}
