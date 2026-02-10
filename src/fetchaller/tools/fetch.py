"""Fetch tool - main URL fetching functionality."""

import re
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from curl_cffi.requests.errors import RequestsError

from ..cache.response_cache import ResponseCache
from ..config import Config
from ..content.fetcher import ContentFetcher, RetryConfig
from ..content.forums import (
    discover_feed_url,
    format_feed_as_markdown,
    is_discourse_html,
    is_forum_html,
    is_thread_url,
    parse_feed,
    transform_forum_url,
)
from ..content.github import extract_github_file_listing, transform_github_url
from ..content.html import html_to_markdown
from ..content.pdf import extract_pdf
from ..content.reddit import transform_reddit_url
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


async def fetch_url(
    url: str,
    max_tokens: int = 25000,
    timeout: int = 10,
    raw: bool = False,
    fetcher: ContentFetcher | None = None,
    cache: ResponseCache | None = None,
    config: Config | None = None,
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

    # Fetch the URL
    try:
        try:
            result = await fetcher.fetch(fetch_url_str, timeout=float(timeout))
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

        # XML/RSS/Atom — try structured feed parsing first
        if any(t in content_type for t in ("text/xml", "application/xml", "application/rss+xml", "application/atom+xml")):
            text = _decode_content(result.content, content_type)
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

            # GitHub tree/repo pages: extract file listing from embedded JSON (additive).
            # The listing is prepended to the normal HTML→markdown result, which provides
            # the README and any other server-rendered content.
            file_listing = None
            if is_github:
                file_listing = extract_github_file_listing(html, result.final_url or url)

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

        # Unsupported content type
        return {"error": f"Unsupported content type: {content_type}"}
    finally:
        if owns_fetcher:
            await fetcher.close()
