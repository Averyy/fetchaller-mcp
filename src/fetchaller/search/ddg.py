"""DuckDuckGo HTML search endpoint scraper."""

import sys
from datetime import UTC, datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup

from ..content._isolated import IsolatedProcessingError, run_isolated
from ..security.xss import redact_secrets_for_log, sanitize_for_log
from .models import SearchResult

_MAX_SEARCH_HTML_CHARS = 12 * 1024 * 1024
_MAX_RESULTS = 20
_MAX_TITLE_CHARS = 500
_MAX_URL_CHARS = 8_192
_MAX_SNIPPET_CHARS = 1_000
_PARSER_TIMEOUT = 10.0


def _log(msg: str) -> None:
    bounded = sanitize_for_log(
        " ".join(str(msg).splitlines()),
        max_length=1_000,
    )
    safe = sanitize_for_log(redact_secrets_for_log(bounded), max_length=500)
    print(f"[{datetime.now(UTC).isoformat()}] {safe}", file=sys.stderr)


def _error_detail(error: Exception) -> str:
    """Build a bounded diagnostic safe for returned output and logs."""
    bounded = sanitize_for_log(
        " ".join(str(error).splitlines()),
        max_length=400,
    )
    detail = sanitize_for_log(redact_secrets_for_log(bounded), max_length=200)
    return f"{type(error).__name__}: {detail}"


def extract_results(html: str) -> list[SearchResult]:
    """Extract search results from DDG HTML response."""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for result_div in soup.select(".result"):
        link = result_div.select_one(".result__a")
        if not link or not link.get("href"):
            continue

        title = link.get_text(strip=True)
        if not title:
            continue

        # Extract URL from DDG redirect wrapper
        href = link["href"]
        if "uddg=" in href:
            url = unquote(href.split("uddg=")[1].split("&")[0])
        else:
            url = href

        # Skip non-HTTP URLs
        if not url.startswith(("http://", "https://")) or len(url) > _MAX_URL_CHARS:
            continue

        # Extract snippet
        snippet_el = result_div.select_one(".result__snippet")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""

        results.append(
            SearchResult(
                title=title[:_MAX_TITLE_CHARS],
                url=url,
                snippet=snippet[:_MAX_SNIPPET_CHARS],
            )
        )
        if len(results) >= _MAX_RESULTS:
            break

    return results


def _parse_response(html: str) -> tuple[list[SearchResult], bool]:
    results = extract_results(html)
    return results, not results and is_explicit_no_results(html)


def is_explicit_no_results(html: str) -> bool:
    """Recognize DuckDuckGo HTML's dedicated zero-results result block."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".no-results, .result--no-result")
    if node is None:
        return False
    return "no results found for" in " ".join(node.get_text(" ", strip=True).split()).lower()


async def search_ddg(session, query: str) -> tuple[list[SearchResult], str | None]:
    """
    Search DuckDuckGo via HTML endpoint.

    Only called on page 1 — DDG pagination is fragile.

    Returns:
        Tuple of (results, error). ``error`` is a short description when the
        request failed at the transport/HTTP layer, so the caller can tell a
        genuine empty result set apart from a network failure.
    """
    params = {"q": query, "kp": "-2"}

    try:
        response = await session.get(
            "https://html.duckduckgo.com/html/",
            params=params,
            headers={"Accept": "text/html"},
            timeout=10,
        )
    except Exception as e:
        detail = _error_detail(e)
        _log(f"ddg request error: {type(e).__name__}")
        return [], detail

    if response.status_code != 200:
        _log(f"ddg non-200 status: {response.status_code}")
        return [], f"HTTP {response.status_code}"

    html = response.text
    if len(html) > _MAX_SEARCH_HTML_CHARS:
        return [], "DuckDuckGo response exceeded the safe HTML size limit"
    try:
        results, explicit_no_results = await run_isolated(
            _parse_response,
            html,
            timeout=_PARSER_TIMEOUT,
        )
    except IsolatedProcessingError:
        return [], "DuckDuckGo response parsing failed within safety limits"

    if not results:
        if explicit_no_results:
            _log("ddg explicit zero-results response")
            return [], None
        _log(f"ddg unexpected 200 response shape (response_chars={len(response.text)})")
        return [], "Unexpected DuckDuckGo response shape (HTTP 200)"

    return results, None
