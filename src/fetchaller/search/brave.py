"""Brave Search HTML scraper (search.brave.com)."""

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from bs4 import BeautifulSoup

from ..content._isolated import IsolatedProcessingError, run_isolated
from ..ratelimit import brave_search_limiter
from ..security.xss import redact_secrets_for_log, sanitize_for_log
from .models import SearchResult
from .pacing import retry_after_seconds

_MAX_SEARCH_HTML_CHARS = 12 * 1024 * 1024
_MAX_RESULTS = 25
_MAX_TITLE_CHARS = 500
_MAX_URL_CHARS = 8_192
_MAX_SNIPPET_CHARS = 1_000
_PARSER_TIMEOUT = 10.0
REQUEST_TIMEOUT = 150.0
BROWSER_SOLVE_TIMEOUT = 120.0
_ENDPOINT = "https://search.brave.com/search"

Pace = Callable[[], Awaitable[None]]

# One shared gate for explicit Brave, Google fallback, and all-provider calls.
# Keep it through the HTTP exchange so queued searches observe a 429 cooldown.
_request_lock = asyncio.Lock()
_backoff_until = 0.0


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
    """Extract organic results from a Brave results page.

    Only ``data-type="web"`` snippets are organic; the page also carries
    clusters (news, videos, discussions) that are not. The class names beside
    ``snippet`` carry a build hash and change, so nothing here keys off them.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for snippet in soup.select("div.snippet[data-type='web']"):
        link = snippet.select_one("a[href^='http']")
        if link is None:
            continue
        url = link["href"]
        if len(url) > _MAX_URL_CHARS:
            continue

        title_el = snippet.select_one(".title")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            continue

        desc_el = snippet.select_one(".generic-snippet") or snippet.select_one(".content")
        description = desc_el.get_text(" ", strip=True) if desc_el else ""

        results.append(
            SearchResult(
                title=title[:_MAX_TITLE_CHARS],
                url=url,
                snippet=description[:_MAX_SNIPPET_CHARS],
            )
        )
        if len(results) >= _MAX_RESULTS:
            break

    return results


def is_explicit_no_results(html: str) -> bool:
    """Recognize Brave's own "Not many great matches" banner.

    It is the page Brave serves past its last page of results, and for a query
    it has nothing for. Matched by element id, not by its English wording.
    """
    return BeautifulSoup(html, "html.parser").select_one("#bad-results-info-banner") is not None


def _parse_response(html: str) -> tuple[list[SearchResult], bool]:
    results = extract_results(html)
    return results, not results and is_explicit_no_results(html)


async def fetch_brave(
    session,
    query: str,
    page: int = 1,
    *,
    pace: Pace | None = None,
) -> tuple[list[SearchResult], str | None]:
    """
    Search Brave via its HTML results page.

    Ordinary retrieval is one GET; wafer may add bounded browser exchanges
    to clear a challenge. ``offset`` is the zero-based page index, so page 2 is
    ``offset=1``, and each page carries about twenty results.

    Returns:
        Tuple of (results, error). ``error`` is a short description when the
        request failed at the transport/HTTP layer or the page was not
        recognizable, so a network failure or a markup change never reads as
        "nothing exists". A page past Brave's last is an empty result.
    """
    params = {"q": query, "source": "web"}
    if page > 1:
        params["offset"] = str(page - 1)

    global _backoff_until
    async with _request_lock:
        now = time.monotonic()
        if now < _backoff_until:
            return [], f"Brave rate-limit backoff ({_backoff_until - now:.0f}s remaining)"
        if pace is not None:
            await pace()
        await brave_search_limiter.wait()
        cancelled = False
        try:
            request = asyncio.create_task(
                session.get(
                    _ENDPOINT,
                    params=params,
                    headers={"Accept": "text/html"},
                    timeout=REQUEST_TIMEOUT,
                    max_response_size=_MAX_SEARCH_HTML_CHARS,
                ),
                name="fetchaller-brave-http",
            )
            while True:
                try:
                    response = await asyncio.shield(request)
                    break
                except asyncio.CancelledError:
                    if request.cancelled():
                        raise
                    # Browser work can outlive cancellation of its async wrapper.
                    # Keep the gate until the bounded wafer call completes and
                    # process any resulting cooldown. The orchestrator detaches
                    # this task so the MCP caller still cancels immediately.
                    cancelled = True
        except Exception as e:
            _log(f"brave request error: {type(e).__name__}")
            return [], _error_detail(e)

        challenge = getattr(response, "challenge_type", None)
        if response.status_code in {403, 429} or challenge:
            duration = max(
                retry_after_seconds(response.headers.get("retry-after")),
                60.0 if response.status_code == 429 else 300.0,
            )
            _backoff_until = time.monotonic() + duration
            reason = f"HTTP {response.status_code}"
            if challenge:
                reason += "; unresolved Brave challenge (wafer could not clear it)"
            _log(f"WARNING brave {reason}; backing off {duration:.0f}s; no retry or identity rotation")
            return [], f"{reason}; Brave rate-limit/block backoff ({duration:.0f}s)"
        if response.status_code != 200:
            _log(f"brave non-200 status: {response.status_code}")
            return [], f"HTTP {response.status_code}"

    if cancelled:
        raise asyncio.CancelledError
    html = response.text
    if len(html) > _MAX_SEARCH_HTML_CHARS:
        return [], "Brave response exceeded the safe HTML size limit"
    try:
        results, explicit_no_results = await run_isolated(
            _parse_response,
            html,
            timeout=_PARSER_TIMEOUT,
        )
    except IsolatedProcessingError:
        return [], "Brave response parsing failed within safety limits"

    if not results:
        if explicit_no_results:
            _log("brave explicit zero-results response")
            return [], None
        _log(f"brave unexpected 200 response shape (response_chars={len(html)})")
        return [], "Unexpected Brave response shape (HTTP 200)"

    return results, None
