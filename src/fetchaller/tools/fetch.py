"""Fetch tool - main URL fetching functionality."""

import os
import re
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from curl_cffi.requests.errors import RequestsError

from ..botfighter import (
    ChallengeSolver,
    CookieCache,
    detect_challenge,
    solve_acw_sc_v2,
)
from ..cache.response_cache import ResponseCache
from ..config import Config
from ..content.alibaba import (
    extract_product_id_from_url as extract_alibaba_product_id,
)
from ..content.alibaba import (
    is_alibaba_search_url,
)
from ..content.aliexpress import extract_product_id_from_url, extract_search_products, is_aliexpress_search_url
from ..content.amazon import is_amazon_store
from ..content.digikey import is_digikey as _is_digikey
from ..content.fetcher import ContentFetcher, FetchResult, RetryConfig
from ..content.forums import (
    discover_feed_url,
    format_feed_as_markdown,
    is_discourse_html,
    is_forum_html,
    is_thread_url,
    parse_feed,
    transform_forum_url,
)
from ..content.github import (
    extract_github_file_listing,
    extract_github_issue,
    transform_github_url,
)
from ..content.html import html_to_markdown
from ..content.mouser import is_mouser as _is_mouser
from ..content.pdf import extract_pdf
from ..content.reddit import transform_reddit_url
from ..content.soylent import is_soylent as _is_soylent
from ..content.ti import extract_ti_part_from_pdf_url, fetch_document_sections, is_ti_document_viewer
from ..content.url import normalize_url
from ..security.ssrf import is_private_host

# Byte-level regex to sniff charset from HTML before full decode
_META_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?([a-zA-Z0-9_-]+)', re.IGNORECASE)
_CONTENT_TYPE_CHARSET_RE = re.compile(r'charset=([a-zA-Z0-9_-]+)', re.IGNORECASE)


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


def _quick_challenge_possible(result: FetchResult) -> bool:
    """Fast header-only check to decide if body decode is needed for challenge detection.

    Returns True if a challenge is possible, False if we can skip body decode. [#11]
    """
    if result.status_code in (403, 429):
        return True
    # CF header can appear on any status
    if result.headers.get("cf-mitigated") == "challenge":
        return True
    # Amazon captcha: status 200 but very small body (normal pages are 1-3M).
    # Quick byte check avoids full decode for the common case.
    if result.status_code == 200 and len(result.content) < 50_000 and (
        b"ontinue shopping" in result.content and (
            b"amazon" in result.content or b"Amazon" in result.content or b"amzn" in result.content
        )
    ):
        return True
    # Alibaba Cloud WAF TMD punish: status 200 with punish marker.
    # AliExpress serves this as a rate-limit block page — needs browser solve.
    # Alibaba punish pages can be ~90KB+ (inline JS/CSS), so use 200KB threshold.
    if result.status_code == 200 and len(result.content) < 200_000 and b"/_____tmd_____/punish" in result.content:
        return True
    # ACW can appear on 200
    # Check cookie markers for Akamai/DataDome/PerimeterX/Imperva (non-200 with cookies)
    set_cookie = result.headers.get("set-cookie", "")
    if set_cookie and any(
        marker in set_cookie
        for marker in ("_abck", "ak_bmsc", "datadome", "_px3", "_pxhd", "reese84", "___utmvc")
    ):
        return True
    return False


async def _handle_botfighter(
    result: FetchResult,
    fetch_url_str: str,
    timeout: float,
    fetcher: ContentFetcher,
    cookie_cache: CookieCache | None,
    challenge_solver: ChallengeSolver | None,
    had_cached_cookies: bool,
    cookie_lookup_domain: str = "",
) -> FetchResult | dict:
    """Handle bot challenge detection and solving after initial fetch.

    Args:
        cookie_lookup_domain: The domain used for the cache lookup in fetch_url().
            When a cached final_url overrides fetch_url_str, this differs from the
            domain derived from fetch_url_str. Needed to evict the original cache
            entry on re-challenge (prevents stale cookie loop with geo-redirects).

    Returns:
        FetchResult if challenge was solved or no challenge detected.
        Dict with 'error' key if solve failed or lock busy.
    """
    # [#11] Short-circuit: skip body decode if no challenge indicators in headers/status
    if not _quick_challenge_possible(result):
        # Still need to check for ACW (can appear on 200 with no header markers)
        # Only decode if content looks like it could be ACW (check raw bytes)
        if b"acw_sc__v2" not in result.content:
            return result

    content_type = result.content_type.lower()
    body = _decode_content(result.content, content_type)
    challenge = detect_challenge(result.status_code, result.headers, body)

    if not challenge:
        return result

    domain = urlparse(fetch_url_str).hostname or ""
    _log(f"Challenge detected: {challenge} for {domain}")

    # ACW: solve inline (~1ms, pure Python)
    if challenge == "acw":
        cookie_value = solve_acw_sc_v2(body)
        if cookie_value:
            # Use a dedicated fetcher to avoid mutating shared fetcher state
            acw_fetcher = ContentFetcher()
            try:
                await acw_fetcher.set_cookie("acw_sc__v2", cookie_value, domain=domain)
                # [#13] Wrap retry fetch in try/except
                try:
                    result = await acw_fetcher.fetch(fetch_url_str, timeout=timeout)
                except (TimeoutError, ConnectionError, RequestsError) as e:
                    return {"error": f"Request failed after ACW solve: {e}"}
                except Exception as e:
                    return {"error": f"Fetch failed after ACW solve ({type(e).__name__}): {e}"}
            finally:
                await acw_fetcher.close()
            # Check for additional challenges (layered protection: ACW + Akamai)
            body = _decode_content(result.content, result.content_type.lower())
            challenge = detect_challenge(result.status_code, result.headers, body)
            if not challenge:
                _log(f"ACW solved for {domain}")
                return result
            _log(f"Additional challenge after ACW: {challenge}")
            # Fall through to browser solve
        else:
            return result  # ACW solve failed, return as-is

    # Browser challenge
    if not challenge_solver:
        return result  # No solver available

    # Evict stale cookies if we had cached ones.
    # Evict both the current domain AND the original lookup domain to prevent
    # stale cookie loops with geo-redirects (e.g., glassdoor.com → glassdoor.ca).
    if had_cached_cookies and cookie_cache:
        cookie_cache.evict(domain)
        if cookie_lookup_domain and cookie_lookup_domain != domain:
            cookie_cache.evict(cookie_lookup_domain)

    solve_result = await challenge_solver.solve(fetch_url_str, challenge)
    if not solve_result:
        # [#14] Distinguish "Chrome not available" from "solve failed"
        return {"error": f"This page is protected by {challenge} bot detection and could not be bypassed. "
                "Ensure Chrome/Chromium is installed for browser-based challenge solving."}
    if "error" in solve_result:
        return solve_result

    # Cache cookies under original domain (with final_url for redirect-aware cache hits)
    final_url = solve_result.get("final_url", "")
    final_domain = urlparse(final_url).hostname or "" if final_url else ""

    # [#2] SSRF validation on browser solve final_url
    redirect_url = None
    if final_domain and final_domain != domain:
        if await is_private_host(final_domain):
            _log(f"Browser redirected to private host {final_domain}, ignoring redirect")
            final_domain = ""
        else:
            redirect_url = final_url

    # [#3/#8] Use impersonate from solver (not fetcher._browser)
    impersonate = solve_result.get("impersonate", fetcher.current_impersonate)

    # Determine which domains need caching (avoid duplicate writes for same domain)
    has_browser_redirect = bool(final_domain and final_domain != domain)
    needs_lookup_recache = bool(
        cookie_lookup_domain
        and cookie_lookup_domain != domain
        and cookie_lookup_domain != final_domain
    )

    if cookie_cache:
        # Use _save=False for intermediate calls, True only on the last (avoids extra disk writes)
        cookie_cache.set(
            domain,
            challenge,
            solve_result["cookies"],
            solve_result["user_agent"],
            impersonate,
            final_url=redirect_url,
            _save=not (has_browser_redirect or needs_lookup_recache),
        )

    # If browser ended up on a different domain (geo-redirect), cache there too
    if has_browser_redirect and cookie_cache:
        cookie_cache.set(
            final_domain,
            challenge,
            solve_result["cookies"],
            solve_result["user_agent"],
            impersonate,
            _save=not needs_lookup_recache,
        )

    # Re-cache original lookup domain so the next request to it gets a cache hit
    # instead of needing a redundant solve. final_url points to fetch_url_str
    # (the redirect URL that was fetched after the original cache hit).
    if needs_lookup_recache and cookie_cache:
        cookie_cache.set(
            cookie_lookup_domain,
            challenge,
            solve_result["cookies"],
            solve_result["user_agent"],
            impersonate,
            final_url=fetch_url_str,
        )

    # Create a dedicated fetcher for the post-solve retry to avoid mutating the
    # shared fetcher's cookies/identity (race condition with concurrent requests).
    retry_fetcher = ContentFetcher()
    try:
        await retry_fetcher.apply_cookies(solve_result["cookies"])
        retry_fetcher.pin_identity(impersonate)
        ua_header = {"User-Agent": solve_result["user_agent"]} if solve_result.get("user_agent") else None
        retry_url = final_url if final_domain and final_domain != domain else fetch_url_str

        # [#13] Wrap retry fetch in try/except
        try:
            result = await retry_fetcher.fetch(retry_url, timeout=timeout, headers=ua_header)
        except TimeoutError:
            return {"error": f"Request timed out after {timeout:.0f}s (after {challenge} challenge solve). "
                    "Try increasing the timeout parameter."}
        except (ConnectionError, RequestsError) as e:
            return {"error": f"Request failed after {challenge} challenge solve: {e}"}
        except Exception as e:
            return {"error": f"Fetch failed after {challenge} solve ({type(e).__name__}): {e}"}
    finally:
        await retry_fetcher.close()

    # Check if still challenged after solve
    body = _decode_content(result.content, result.content_type.lower())
    if detect_challenge(result.status_code, result.headers, body):
        # Cookie replay failed (common with Akamai TLS fingerprint binding).
        # Use the HTML we extracted directly from Chrome's DOM if available.
        chrome_html = solve_result.get("html", "")
        if chrome_html:
            _log(f"Cookie replay failed for {challenge}, using Chrome-extracted HTML ({len(chrome_html):,} chars)")
            return FetchResult(
                content=chrome_html.encode("utf-8"),
                content_type="text/html; charset=utf-8",
                status_code=200,
                final_url=solve_result.get("final_url", fetch_url_str),
                headers={},
            )
        return {"error": f"This page is protected by {challenge} bot detection and could not be bypassed."}

    _log(f"{challenge} challenge solved for {domain}")
    return result


async def fetch_url(
    url: str,
    max_tokens: int = 25000,
    timeout: int = 10,
    raw: bool = False,
    fetcher: ContentFetcher | None = None,
    cache: ResponseCache | None = None,
    config: Config | None = None,
    cookie_cache: CookieCache | None = None,
    challenge_solver: ChallengeSolver | None = None,
    _skip_aliexpress_intercept: bool = False,
    _skip_alibaba_intercept: bool = False,
) -> dict:
    """
    Fetch a URL and return its content.

    Args:
        url: URL to fetch
        max_tokens: Maximum tokens to return (default: 25000)
        timeout: Request timeout in seconds (default: 10)
        raw: Return raw HTML instead of markdown (default: False)
        fetcher: Optional ContentFetcher instance (creates new if not provided)
        cache: Optional ResponseCache instance
        config: Optional Config instance
        cookie_cache: Optional CookieCache for bot challenge cookies
        challenge_solver: Optional ChallengeSolver for browser-based challenges

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

    # AliExpress product pages are JS-rendered — use product tool which tries
    # MTop API first, then falls back to fetching through the full pipeline
    # (botfighter handles Akamai/TMD). Reviews always appended.
    # _skip_aliexpress_intercept prevents recursion when get_product calls fetch_url.
    ae_product_id = extract_product_id_from_url(url) if not _skip_aliexpress_intercept else None
    if ae_product_id:
        from ..aliexpress.product import get_product

        result = await get_product(
            ae_product_id,
            fetcher=fetcher,
            cache=cache,
            config=config,
            cookie_cache=cookie_cache,
            challenge_solver=challenge_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache_key = normalize_url(url)
                cache.set(cache_key, content, "text")
            _log(f"FETCH {url} -> AliExpress product ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Alibaba.com product pages — SSR with embedded JSON, use dedicated tool
    # _skip_alibaba_intercept prevents recursion when get_product/search calls fetch_url.
    alibaba_product_id = extract_alibaba_product_id(url) if not _skip_alibaba_intercept else None
    if alibaba_product_id:
        from ..alibaba.product import get_product as get_alibaba_product

        result = await get_alibaba_product(
            alibaba_product_id,
            fetcher=fetcher,
            cache=cache,
            config=config,
            cookie_cache=cookie_cache,
            challenge_solver=challenge_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache_key = normalize_url(url)
                cache.set(cache_key, content, "text")
            _log(f"FETCH {url} -> Alibaba product ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # Alibaba.com search pages — SSR with embedded JSON, use dedicated tool
    if not _skip_alibaba_intercept and is_alibaba_search_url(url):
        # Extract query from URL params
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
            fetcher=fetcher,
            cache=cache,
            config=config,
            cookie_cache=cookie_cache,
            challenge_solver=challenge_solver,
        )
        if "content" in result:
            content = truncate(result["content"], max_tokens)
            if cache:
                cache_key = normalize_url(url)
                cache.set(cache_key, content, "text")
            _log(f"FETCH {url} -> Alibaba search ({len(content)} chars, {time.monotonic() - start:.1f}s)")
            return {"content": content, "content_type": "text", "url": url}
        return result  # Error dict

    # AliExpress search pages — needs Chrome session warming for TMD bypass.
    # Route to dedicated search module which handles curl_cffi fast path + Chrome fallback.
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

        # Extract query from URL path: /w/wholesale-{query}.html
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
            fetcher=fetcher,
            cache=cache,
            config=config,
            cookie_cache=cookie_cache,
            challenge_solver=challenge_solver,
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
            # Unrecognized URL pattern — fall through to HTML pipeline
            if "Could not extract" not in result.get("error", ""):
                return result  # Definitive API failure (auth, rate limit, timeout)
            _log(f"FETCH {url} -> Mouser API couldn't parse URL, falling through to HTML")
        # No API key or unrecognized URL — fall through to HTML pipeline

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
            # Unrecognized URL pattern — fall through to HTML pipeline
            if "Could not extract" not in result.get("error", ""):
                return result  # Definitive API failure (auth, rate limit, timeout)
            _log(f"FETCH {url} -> DigiKey API couldn't parse URL, falling through to HTML")
        # No credentials or unrecognized URL — fall through to HTML pipeline

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

    # Compute normalized URL once for cache operations
    cache_key = normalize_url(fetch_url_str) if cache else None

    # Check cache (if not raw mode and cache is available)
    if cache and not raw and cache_key:
        cached = cache.get(cache_key)
        if cached:
            content = truncate(cached.content, max_tokens)
            result = {
                "content": content,
                "content_type": cached.content_type,
                "url": fetch_url_str,
                "cached": True,
            }
            if is_reddit and fetch_url_str != url:
                result["content"] = f"[Fetched via: {fetch_url_str}]\n\n{content}"
            _log(f"FETCH {url} -> CACHED ({time.monotonic() - start:.1f}s)")
            return result

    # Create fetcher if not provided
    owns_fetcher = fetcher is None
    if owns_fetcher:
        retry_config = RetryConfig.from_config(config) if config else None
        fetcher = ContentFetcher(retry_config=retry_config)

    # Reddit: use a dedicated fetcher to avoid cookie contamination.
    # The shared fetcher accumulates .reddit.com cookies from browse_reddit/
    # search_reddit JSON API calls (www.reddit.com). Those cookies get sent
    # to old.reddit.com HTML fetches and can trigger 403s from session mismatch.
    # A dedicated fetcher also avoids race conditions on the shared session
    # in HTTP mode where concurrent requests share the same fetcher instance.
    reddit_fetcher: ContentFetcher | None = None
    if is_reddit and isinstance(fetcher, ContentFetcher) and not owns_fetcher:
        retry_config = RetryConfig.from_config(config) if config else None
        reddit_fetcher = ContentFetcher(retry_config=retry_config)

    # Botfighter: check cookie cache and prepare a dedicated fetcher if needed.
    # A separate fetcher avoids race conditions on the shared instance when
    # concurrent requests apply/clear cookies for different domains.
    # Use fetch_url_str hostname (post-transform) to match how _handle_botfighter
    # stores cookies — the challenge is on the transformed URL, not the original.
    had_cached_cookies = False
    cached_bot_cookies = None
    bf_fetcher: ContentFetcher | None = None  # Dedicated fetcher for botfighter requests
    if cookie_cache:
        domain = urlparse(fetch_url_str).hostname or ""
        cached_bot_cookies = cookie_cache.get(domain)
        if cached_bot_cookies:
            had_cached_cookies = True
            retry_config = RetryConfig.from_config(config) if config else None
            bf_fetcher = ContentFetcher(retry_config=retry_config)
            await bf_fetcher.apply_cookies(cached_bot_cookies.cookies)
            bf_fetcher.pin_identity(cached_bot_cookies.impersonate)
            # [#1] SSRF validation on cached final_url before using it
            if cached_bot_cookies.final_url:
                try:
                    final_parsed = urlparse(cached_bot_cookies.final_url)
                    if not await is_private_host(final_parsed.hostname or ""):
                        fetch_url_str = cached_bot_cookies.final_url
                    else:
                        _log(f"Ignoring private cached final_url for {domain}")
                except Exception:
                    pass

    # Priority: botfighter fetcher > reddit fetcher > shared fetcher
    active_fetcher = bf_fetcher if bf_fetcher else (reddit_fetcher if reddit_fetcher else fetcher)

    # Per-domain rate limiting for sites that aggressively block rapid requests
    if is_reddit:
        from ..ratelimit import reddit_limiter
        await reddit_limiter.wait()
    elif _is_soylent(fetch_url_str):
        from ..ratelimit import soylent_limiter
        await soylent_limiter.wait()

    # Fetch the URL
    try:
        try:
            result = await active_fetcher.fetch(
                fetch_url_str,
                timeout=float(timeout),
                headers={"User-Agent": cached_bot_cookies.user_agent} if cached_bot_cookies else None,
            )
        except TimeoutError:
            _log(f"FETCH {url} -> ERROR: timeout after {timeout}s ({time.monotonic() - start:.1f}s)")
            return {"error": f"Request timed out after {timeout}s. Try increasing the timeout parameter for slow servers."}
        except ConnectionError as e:
            error_str = str(e).lower()
            if "enotfound" in error_str or "getaddrinfo" in error_str:
                return {"error": "Host not found. Check the URL for typos or verify the site is accessible."}
            if "econnrefused" in error_str:
                return {"error": "Connection refused. The server may be down or blocking requests."}
            if "econnreset" in error_str:
                return {"error": "Connection reset. The server closed the connection unexpectedly."}
            if "etimedout" in error_str:
                return {"error": "Connection timed out. The server may be slow or unreachable."}
            return {"error": f"Connection error: {e}"}
        except RequestsError as e:
            return {"error": f"Request failed: {e}"}
        except Exception as e:
            return {"error": f"Fetch failed ({type(e).__name__}): {e}"}

        # SSRF protection: check final URL after redirects
        if result.final_url and result.final_url != fetch_url_str:
            try:
                final_parsed = urlparse(result.final_url)
                if await is_private_host(final_parsed.hostname or ""):
                    return {"error": "Redirect to private/internal host is not allowed."}
            except Exception:
                pass

        # Botfighter: detect and solve challenges (ACW inline, browser via PyDoll)
        # When a challenge is detected, _handle_botfighter creates its own dedicated
        # fetcher internally to avoid mutating the shared fetcher's state.
        if cookie_cache is not None:
            bf_result = await _handle_botfighter(
                result, fetch_url_str, float(timeout),
                active_fetcher, cookie_cache, challenge_solver, had_cached_cookies,
                cookie_lookup_domain=domain if had_cached_cookies else "",
            )
            if isinstance(bf_result, dict):
                # Error from botfighter (solve failed or lock busy)
                _log(f"FETCH {url} -> BOTFIGHTER: {bf_result.get('error', '?')} ({time.monotonic() - start:.1f}s)")
                return bf_result
            result = bf_result

        # Handle rate limiting
        if result.status_code == 429:
            retry_after = result.headers.get("retry-after", "")
            retry_msg = f" Retry after {retry_after} seconds." if retry_after else ""
            _log(f"FETCH {url} -> ERROR: 429 rate limited ({time.monotonic() - start:.1f}s)")
            return {"error": f"Rate limited (HTTP 429).{retry_msg}"}

        content_type = result.content_type.lower()

        # Handle errors
        if result.status_code >= 400:
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
            # TI datasheets: try HTML document viewer (much better for LLMs than PDF extraction)
            ti_part = extract_ti_part_from_pdf_url(fetch_url_str)
            if not ti_part and result.final_url:
                ti_part = extract_ti_part_from_pdf_url(result.final_url)
            if ti_part:
                viewer_url = f"https://www.ti.com/document-viewer/{ti_part}/datasheet"
                try:
                    viewer_result = await active_fetcher.fetch(viewer_url, timeout=float(timeout))
                    if viewer_result.status_code == 200:
                        viewer_html = _decode_content(viewer_result.content, viewer_result.content_type.lower())
                        combined = await fetch_document_sections(active_fetcher, viewer_html, float(timeout))
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
                header = f"[PDF: {pdf_result.page_count} pages. Text extraction is approximate - complex layouts, tables, and formatting may not be preserved.]\n\n"
                # Account for header in token budget
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

            # Tier 2: Forum autodiscovery — if forum software detected but
            # Tier 1 didn't match, check for <link rel="alternate"> feed.
            # Skip if URL was identified as a thread (handled by site-specific cleanup).
            if not is_forum_feed and forum_result.forum_software and not forum_result.is_thread:
                feed_url = discover_feed_url(html, result.final_url or url)
                if feed_url:
                    try:
                        feed_result = await fetcher.fetch(feed_url, timeout=float(timeout))
                        if feed_result.status_code < 400:
                            feed_text = _decode_content(feed_result.content, feed_result.content_type)
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
                        pass  # Fall through to normal HTML pipeline
            # Also check HTML for forum markers when domain not in registry.
            # Skip if URL looks like a thread (same patterns as Tier 1).
            elif not is_forum_feed and not forum_result.forum_software and not is_thread_url(result.final_url or url):
                from bs4 import BeautifulSoup as _Soup  # noqa: N812

                _quick_soup = _Soup(html[:4096], "lxml")
                if is_forum_html(_quick_soup) or is_discourse_html(_quick_soup):
                    feed_url = discover_feed_url(html, result.final_url or url)
                    if feed_url:
                        try:
                            feed_result = await fetcher.fetch(feed_url, timeout=float(timeout))
                            if feed_result.status_code < 400:
                                feed_text = _decode_content(feed_result.content, feed_result.content_type)
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
                            pass  # Fall through to normal HTML pipeline

            # TI document viewer: reconstruct full datasheet from lazy-loaded sections.
            # The initial HTML is just a TOC shell — actual content is fetched via ?raw=1.
            effective_url = result.final_url or url
            if is_ti_document_viewer(effective_url):
                combined = await fetch_document_sections(active_fetcher, html, float(timeout))
                if combined:
                    html = combined

            # AliExpress search pages: extract structured product list from _init_data_ JSON.
            # The HTML is mostly JS-rendered noise — the embedded JSON has cleaner data.
            if not _skip_aliexpress_intercept and is_aliexpress_search_url(effective_url):
                search_content = extract_search_products(html, effective_url)
                if search_content:
                    content = truncate(search_content, max_tokens)
                    if cache and cache_key:
                        cache.set(cache_key, search_content, "text")
                    _log(f"FETCH {url} -> AliExpress search extraction ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                    return {"content": content, "content_type": "text", "url": effective_url}

            # GitHub issue/PR/discussion pages: extract from embedded JSON directly.
            # The HTML pipeline strips <script> tags, destroying the comment data.
            if is_github:
                issue_content = extract_github_issue(html, effective_url)
                if issue_content:
                    content = truncate(issue_content, max_tokens)
                    if cache and cache_key:
                        cache.set(cache_key, issue_content, "text")
                    _log(f"FETCH {url} -> GitHub issue extraction ({len(content)} chars, {time.monotonic() - start:.1f}s)")
                    return {"content": content, "content_type": "text", "url": effective_url}

            # GitHub tree/repo pages: extract file listing from embedded JSON (additive).
            # The listing is prepended to the normal HTML→markdown result, which provides
            # the README and any other server-rendered content.
            file_listing = None
            if is_github:
                file_listing = extract_github_file_listing(html, effective_url)

            markdown, _ = await html_to_markdown(html, is_reddit=is_reddit, url=result.final_url)

            # Prepend file listing if found
            if file_listing:
                # If the JSON already included a README, use that standalone
                # (tree subpages have empty HTML, so markdown would be just the title)
                if "\n---\n" in file_listing:
                    markdown = file_listing
                elif len(markdown) > 200:
                    # Repo root: file listing + separator + server-rendered content
                    # (skip if HTML-derived markdown is trivial — no README on page)
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
            if is_reddit and fetch_url_str != url:
                response["content"] = f"[Fetched via: {fetch_url_str}]\n\n{content}"
            elif github_result.is_blob and fetch_url_str != url:
                response["content"] = f"[Fetched raw: {fetch_url_str}]\n\n{content}"
            elif result.final_url and result.final_url != fetch_url_str:
                response["content"] = f"[Redirected to: {result.final_url}]\n\n{content}"

            _log(f"FETCH {url} -> {response['content_type']} ({len(response['content'])} chars, {time.monotonic() - start:.1f}s)")
            return response

        # Any other text-based content type (text/javascript, text/css, etc.)
        if content_type.startswith("text/") or content_type.startswith("application/javascript"):
            text = _decode_content(result.content, content_type)
            return {
                "content": truncate(text, max_tokens),
                "content_type": "text",
                "url": result.final_url,
            }

        # Unsupported content type
        return {"error": f"Unsupported content type: {content_type}"}
    finally:
        # Close dedicated fetchers if created
        if bf_fetcher:
            await bf_fetcher.close()
        if reddit_fetcher:
            await reddit_fetcher.close()
        if owns_fetcher:
            await fetcher.close()
