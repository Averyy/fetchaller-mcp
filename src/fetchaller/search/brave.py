"""Brave Search HTML scraper (search.brave.com)."""

import asyncio
import re
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
# Brave's cloaked result links (2026-11-22): /go/v1/<token>, the destination
# encrypted in the token. LINK_PREFIX + token is what a result carries.
LINK_PREFIX = "https://search.brave.com/go/v1/"
_TOKEN = r"[A-Za-z0-9_-]{40,2048}"
_HREF_TOKEN_RE = re.compile(rf"/go/v1/({_TOKEN})\Z")
_RESULT_LINK_RE = re.compile(
    rf"https://search\.brave\.com/go/v1(?:/|#)({_TOKEN})(?:\?(?:confirm&)?r=\d)?\Z"
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_LINK_TIMEOUT = 30.0
_MAX_LINK_RESPONSE_BYTES = 1024 * 1024
LINK_UNRESOLVED_ERROR = (
    "Brave did not resolve this result link (Brave resolves a link only for "
    "the session that searched, and only for a while); search again and open "
    "the new link"
)

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
        display = ""
        if link is not None:
            url = link["href"]
        else:
            # Cloaked: the destination is encrypted in Brave's own redirect,
            # and the page shows only a display of it (a ``cite`` such as
            # "https:// docs.python.org › 3 › library › asyncio.html").
            cloaked = snippet.select_one("a[href^='/go/v1/']")
            match = _HREF_TOKEN_RE.fullmatch(str(cloaked.get("href") or "")) if cloaked else None
            if match is None:
                continue
            url = LINK_PREFIX + match.group(1)
            cite = snippet.select_one("cite")
            display = _display(cite.get_text(" ") if cite else "")
        if len(url) > _MAX_URL_CHARS:
            continue

        title_el = snippet.select_one(".title")
        # The page's own spacing, collapsed. A space between every text node put
        # one before punctuation after each bolded term and inside paths
        # ("syntax .", "Lib/ asyncio /queues.py").
        title = " ".join(title_el.get_text().split()) if title_el else ""
        if not title:
            continue

        desc_el = snippet.select_one(".generic-snippet") or snippet.select_one(".content")
        description = " ".join(desc_el.get_text().split()) if desc_el else ""

        results.append(
            SearchResult(
                title=title[:_MAX_TITLE_CHARS],
                url=url,
                snippet=description[:_MAX_SNIPPET_CHARS],
                display=display,
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


def _display(text: str) -> str:
    """Brave's display of a cloaked destination, tidied ("https:// a.org › x"
    becomes "a.org › x"). Display text only: Brave elides and reorders it."""

    shown = " ".join(str(text).split())
    for scheme in ("https:// ", "http:// ", "https://", "http://"):
        if shown.startswith(scheme):
            shown = shown[len(scheme):]
            break
    return shown[:300]


def display_url(display: str) -> str | None:
    """The URL a display spells out, only to match it against a real URL from
    another engine (never shown): ``a.org › x › y.html`` is
    ``https://a.org/x/y.html``. None when the display is elided or odd."""

    parts = [part.strip() for part in display.split("›")]
    if not parts or not parts[0] or any(not part for part in parts) or "…" in display:
        return None
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parts[0]) or any(
        " " in part for part in parts
    ):
        return None
    return "https://" + "/".join(parts)


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


def result_link_token(url: object) -> str | None:
    """The token of a Brave result link (``LINK_PREFIX`` + token, optionally
    with Brave's own ``?r=`` or ``#`` form), else None."""

    if not isinstance(url, str) or len(url) > _MAX_URL_CHARS:
        return None
    match = _RESULT_LINK_RE.fullmatch(url)
    return match.group(1) if match else None


def _destination(location: object) -> str | None:
    if not isinstance(location, str) or len(location) > _MAX_URL_CHARS:
        return None
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in location):
        return None
    if not location.startswith(("https://", "http://")):
        return None
    host = location.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower().rstrip(".")
    if not host or host == "search.brave.com" or host.endswith(".brave.com") and host.startswith("search."):
        return None
    return location


async def resolve_result_link(session, token: str) -> tuple[str | None, str | None]:
    """Where one cloaked result link leads: ``(url, None)`` or ``(None, error)``.

    One GET of Brave's conventional redirect (``?r=1``, the path Brave keeps
    for clients without JavaScript) in the session that searched: Brave binds
    a link to that client and answers anyone else with its app shell. The
    session must not follow redirects, so the destination is read here and
    fetched by the caller through its own guarded path. It is one request,
    made only when a link is opened, on the same gate, limiter and backoff as
    a search: Brave counts link resolutions per results page against bulk
    scraping, which a person opening a link does not resemble.
    """

    global _backoff_until
    if not re.fullmatch(_TOKEN, token):
        return None, "invalid Brave result link"
    async with _request_lock:
        now = time.monotonic()
        if now < _backoff_until:
            return None, f"Brave rate-limit backoff ({_backoff_until - now:.0f}s remaining)"
        await brave_search_limiter.wait()
        cancelled = False
        try:
            request = asyncio.create_task(
                session.get(
                    f"{LINK_PREFIX}{token}?r=1",
                    headers={"Accept": "text/html"},
                    timeout=_LINK_TIMEOUT,
                    max_response_size=_MAX_LINK_RESPONSE_BYTES,
                ),
                name="fetchaller-brave-link",
            )
            while True:
                try:
                    response = await asyncio.shield(request)
                    break
                except asyncio.CancelledError:
                    if request.cancelled():
                        raise
                    # As in fetch_brave: browser work can outlive its async
                    # wrapper, so keep the gate until the bounded wafer call
                    # completes and record any cooldown it earned.
                    cancelled = True
        except Exception as e:
            _log(f"brave link request error: {type(e).__name__}")
            return None, _error_detail(e)
        challenge = getattr(response, "challenge_type", None)
        if response.status_code in {403, 429} or challenge:
            duration = max(
                retry_after_seconds(response.headers.get("retry-after")),
                60.0 if response.status_code == 429 else 300.0,
            )
            _backoff_until = time.monotonic() + duration
            _log(f"WARNING brave link HTTP {response.status_code}; backing off {duration:.0f}s")
            return None, f"HTTP {response.status_code}; Brave rate-limit/block backoff ({duration:.0f}s)"
        if cancelled:
            raise asyncio.CancelledError
        if response.status_code in _REDIRECT_STATUSES:
            destination = _destination(response.headers.get("location"))
            if destination is None:
                return None, "Brave redirected this result link somewhere it cannot be followed"
            return destination, None
        if response.status_code == 200:
            return None, LINK_UNRESOLVED_ERROR
        return None, f"HTTP {response.status_code}"
