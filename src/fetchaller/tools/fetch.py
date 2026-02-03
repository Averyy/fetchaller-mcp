"""Fetch tool - main URL fetching functionality."""

from urllib.parse import urlparse

from ..cache.response_cache import ResponseCache
from ..config import Config
from ..content.fetcher import ContentFetcher, RetryConfig
from ..content.html import html_to_markdown
from ..content.pdf import extract_pdf
from ..content.reddit import transform_reddit_url
from ..content.url import normalize_url
from ..security.ssrf import is_private_host


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
    # Validate URL
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Invalid protocol: {parsed.scheme}. Only http/https supported."}
    except Exception:
        return {"error": "Invalid URL format. Expected http:// or https:// URL."}

    # SSRF protection
    hostname = parsed.hostname or ""
    if is_private_host(hostname):
        return {"error": "Access to private/internal hosts is not allowed."}

    # Transform Reddit URLs
    reddit_result = transform_reddit_url(url)
    fetch_url_str = reddit_result.url
    is_reddit = reddit_result.is_reddit

    # Compute normalized URL once for cache operations
    cache_key = normalize_url(fetch_url_str) if cache else None

    # Check cache (if not raw mode and cache is available)
    if cache and not raw and cache_key:
        cached = cache.get(cache_key)
        if cached:
            result = {
                "content": cached.content,
                "content_type": cached.content_type,
                "url": fetch_url_str,
                "cached": True,
            }
            if is_reddit and fetch_url_str != url:
                result["content"] = f"[Fetched via: {fetch_url_str}]\n\n{result['content']}"
            return result

    # Create fetcher if not provided
    if fetcher is None:
        retry_config = RetryConfig.from_config(config) if config else None
        fetcher = ContentFetcher(retry_config=retry_config)

    # Fetch the URL
    try:
        result = await fetcher.fetch(fetch_url_str, timeout=float(timeout))
    except TimeoutError:
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
        # Generic error without exposing internal details
        return {"error": "Connection error. Unable to reach the server."}
    except Exception:
        # Don't expose exception details which may contain sensitive info
        return {"error": "Fetch failed. An unexpected error occurred."}

    # SSRF protection: check final URL after redirects
    if result.final_url and result.final_url != fetch_url_str:
        try:
            final_parsed = urlparse(result.final_url)
            if is_private_host(final_parsed.hostname or ""):
                return {"error": "Redirect to private/internal host is not allowed."}
        except Exception:
            pass

    # Handle rate limiting
    if result.status_code == 429:
        retry_after = result.headers.get("retry-after", "")
        retry_msg = f" Retry after {retry_after} seconds." if retry_after else ""
        return {"error": f"Rate limited (HTTP 429).{retry_msg}"}

    # Handle errors
    if result.status_code >= 400:
        body = result.content.decode("utf-8", errors="replace")[:1000]
        return {"error": f"HTTP {result.status_code}", "body": body}

    content_type = result.content_type.lower()

    # JSON
    if "application/json" in content_type:
        text = result.content.decode("utf-8", errors="replace")
        return {
            "content": truncate(text, max_tokens),
            "content_type": "json",
            "url": result.final_url,
        }

    # Plain text
    if "text/plain" in content_type:
        text = result.content.decode("utf-8", errors="replace")
        return {
            "content": truncate(text, max_tokens),
            "content_type": "text",
            "url": result.final_url,
        }

    # XML/RSS/Atom
    if any(t in content_type for t in ("text/xml", "application/xml", "application/rss+xml", "application/atom+xml")):
        text = result.content.decode("utf-8", errors="replace")
        return {
            "content": truncate(text, max_tokens),
            "content_type": "xml",
            "url": result.final_url,
        }

    # CSV
    if "text/csv" in content_type:
        text = result.content.decode("utf-8", errors="replace")
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
        html = result.content.decode("utf-8", errors="replace")

        if raw:
            return {
                "content": truncate(html, max_tokens),
                "content_type": "html",
                "url": result.final_url,
            }

        markdown, _ = html_to_markdown(html, is_reddit=is_reddit)
        content = truncate(markdown, max_tokens)

        # Cache the result
        if cache and cache_key:
            cache.set(
                cache_key,
                content,
                "markdown",
                cache_control=result.headers.get("cache-control"),
            )

        response = {
            "content": content,
            "content_type": "markdown",
            "url": result.final_url,
        }

        # Note if we transformed the URL
        if is_reddit and fetch_url_str != url:
            response["content"] = f"[Fetched via: {fetch_url_str}]\n\n{content}"
        elif result.final_url and result.final_url != fetch_url_str:
            response["content"] = f"[Redirected to: {result.final_url}]\n\n{content}"

        return response

    # Unsupported content type
    return {"error": f"Unsupported content type: {content_type}"}
