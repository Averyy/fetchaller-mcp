"""DuckDuckGo HTML search endpoint scraper."""

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup

from ..content._isolated import IsolatedProcessingError, run_isolated
from ..ratelimit import ddg_search_limiter
from ..security.xss import redact_secrets_for_log, sanitize_for_log
from .models import SearchResult
from .pacing import retry_after_seconds

_MAX_SEARCH_HTML_CHARS = 12 * 1024 * 1024
_MAX_RESULTS = 20
_MAX_TITLE_CHARS = 500
_MAX_URL_CHARS = 8_192
_MAX_SNIPPET_CHARS = 1_000
_PARSER_TIMEOUT = 10.0

# DDG's html endpoint has no page parameter. Each response carries a "Next"
# form (`s` offset, `dc`, `vqd`, ...) and the next page is that form POSTed
# back, so page N costs N requests unless the forms of the pages before it are
# remembered. The cap bounds that walk; past it search() leaves DDG out.
MAX_DDG_PAGE = 10
_PAGE_STATE_TTL = 300.0
_PAGE_STATE_MAX = 64
_NEXT_FORM_FIELDS = {"q", "s", "dc", "vqd"}
_ENDPOINT = "https://html.duckduckgo.com/html/"

Pace = Callable[[], Awaitable[None]]


@dataclass
class _QueryPages:
    """What walking one query's pages has taught us, so page N+1 is one POST."""

    created: float = field(default_factory=time.monotonic)
    forms: dict[int, dict[str, str]] = field(default_factory=dict)  # page -> form that fetches it
    urls: dict[int, list[str]] = field(default_factory=dict)  # page -> every URL it returned


_pages: dict[str, _QueryPages] = {}
_pages_lock = asyncio.Lock()


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


def extract_next_form(html: str, after: int = 0) -> dict[str, str] | None:
    """Return the fields of the form that moves forward, or None on the last page.

    From page 2 on a response carries a "Previous" form (`s` back at the start)
    ahead of the "Next" one, and both have the same hidden fields. The first
    match is therefore the way back, and walking it from page 2 returned page 1
    again. A form moves forward only if its offset is past ``after``, the offset
    of the form that fetched this page (0 for page 1). That also holds on the
    last page, where "Previous" is all there is, and needs no button label.
    """
    soup = BeautifulSoup(html, "html.parser")
    forward = []
    for form in soup.select("form"):
        fields = {i["name"]: i.get("value", "") for i in form.select("input[name]")}
        if not _NEXT_FORM_FIELDS <= fields.keys():
            continue
        try:
            offset = int(fields["s"])
        except ValueError:
            continue
        if offset > after:
            forward.append((offset, fields))
    return max(forward, key=lambda f: f[0])[1] if forward else None


def _parse_response(html: str, after: int = 0) -> tuple[list[SearchResult], bool, dict[str, str] | None]:
    results = extract_results(html)
    return results, not results and is_explicit_no_results(html), extract_next_form(html, after)


def is_explicit_no_results(html: str) -> bool:
    """Recognize DuckDuckGo HTML's dedicated zero-results result block."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".no-results, .result--no-result")
    if node is None:
        return False
    return "no results found for" in " ".join(node.get_text(" ", strip=True).split()).lower()


def _offset(form: dict[str, str] | None) -> int:
    """The result offset a form asks for; page 1, fetched with no form, is 0."""
    try:
        return int(form["s"]) if form else 0
    except ValueError:
        return 0


def _url_key(url: str) -> str:
    return url.split("#")[0].rstrip("/")


_request_lock = asyncio.Lock()
_backoff_until = 0.0


async def _fetch_page(
    session,
    query: str,
    form: dict[str, str] | None,
    pace: Pace | None,
) -> tuple[list[SearchResult], dict[str, str] | None, str | None]:
    """Fetch one page: a GET for page 1, the previous page's "Next" form after that.

    Returns (results, next_form, error). ``error`` is set for a transport/HTTP
    failure or an unrecognized 200, never for a genuinely empty page.
    """
    # Serialize the whole exchange, including wafer waits, so concurrent
    # queries cannot bunch up after passing the application-level pace gate.
    global _backoff_until
    async with _request_lock:
        now = time.monotonic()
        if now < _backoff_until:
            return [], None, f"DuckDuckGo rate-limit/block backoff ({_backoff_until - now:.0f}s remaining)"
        if pace is not None:
            await pace()
        await ddg_search_limiter.wait()
        headers = {"Accept": "text/html"}
        try:
            if form is None:
                response = await session.get(_ENDPOINT, params={"q": query, "kp": "-2"}, headers=headers, timeout=10)
            else:
                response = await session.post(_ENDPOINT, form={**form, "kp": "-2"}, headers=headers, timeout=10)
        except Exception as e:
            _log(f"ddg request error: {type(e).__name__}")
            return [], None, _error_detail(e)
        if response.status_code in {202, 403, 429}:
            duration = max(
                retry_after_seconds(response.headers.get("retry-after")),
                60.0 if response.status_code == 429 else 300.0,
            )
            _backoff_until = time.monotonic() + duration
            _log(f"WARNING ddg HTTP {response.status_code}; backing off {duration:.0f}s")
            detail = " (DuckDuckGo served its homepage instead of results)" if response.status_code == 202 else ""
            return (
                [],
                None,
                f"HTTP {response.status_code}{detail}; DuckDuckGo rate-limit/block backoff ({duration:.0f}s)",
            )
        if response.status_code != 200:
            _log(f"ddg non-200 status: {response.status_code}")
            return [], None, f"HTTP {response.status_code}"

    html = response.text
    if len(html) > _MAX_SEARCH_HTML_CHARS:
        return [], None, "DuckDuckGo response exceeded the safe HTML size limit"
    try:
        results, explicit_no_results, next_form = await run_isolated(
            _parse_response,
            html,
            _offset(form),
            timeout=_PARSER_TIMEOUT,
        )
    except IsolatedProcessingError:
        return [], None, "DuckDuckGo response parsing failed within safety limits"

    if not results:
        if explicit_no_results:
            _log("ddg explicit zero-results response")
            return [], None, None
        _log(f"ddg unexpected 200 response shape (response_chars={len(response.text)})")
        return [], None, "Unexpected DuckDuckGo response shape (HTTP 200)"

    return results, next_form, None


def _remember(state: _QueryPages, page: int, results: list[SearchResult], next_form: dict[str, str] | None) -> None:
    state.urls[page] = [r.url for r in results]
    if next_form is not None:
        state.forms[page + 1] = next_form


def _store_state(query: str, state: _QueryPages) -> None:
    now = time.monotonic()
    for key in [k for k, v in _pages.items() if now - v.created > _PAGE_STATE_TTL]:
        del _pages[key]
    while len(_pages) >= _PAGE_STATE_MAX:
        del _pages[min(_pages, key=lambda k: _pages[k].created)]
    _pages[query] = state


def _live_state(query: str) -> _QueryPages | None:
    state = _pages.get(query)
    if state is None or time.monotonic() - state.created > _PAGE_STATE_TTL:
        return None
    return state


async def search_ddg(
    session,
    query: str,
    page: int = 1,
    *,
    pace: Pace | None = None,
) -> tuple[list[SearchResult], str | None]:
    """
    Search DuckDuckGo via the HTML endpoint, any page up to ``MAX_DDG_PAGE``.

    Page 1 is a GET. Every later page is the previous page's "Next" form POSTed
    back, so a page whose predecessors are not remembered is reached by walking
    them (each request goes through ``pace``). DDG re-ranks between requests and
    a page repeats URLs from the ones before it; those are dropped here, so a
    page returns only results the caller has not already been given.

    Returns:
        Tuple of (results, error). ``error`` is a short description when the
        request failed at the transport/HTTP layer, so the caller can tell a
        genuine empty result set apart from a network failure. A page past the
        last one DDG offers is an empty result, not an error.
    """
    if page < 1 or page > MAX_DDG_PAGE:
        return [], f"DuckDuckGo paging is limited to {MAX_DDG_PAGE} pages"

    if page == 1:
        results, next_form, error = await _fetch_page(session, query, None, pace)
        if error is None:
            state = _QueryPages()
            _remember(state, 1, results, next_form)
            _store_state(query, state)
        return results, error

    async with _pages_lock:
        state = _live_state(query)
        if state is None or 1 not in state.urls:
            results, next_form, error = await _fetch_page(session, query, None, pace)
            if error is not None:
                return [], error
            state = _QueryPages()
            _remember(state, 1, results, next_form)
            _store_state(query, state)

        # Walk forward until the form for the wanted page is known.
        while page not in state.forms:
            last = max(state.urls)
            form = state.forms.get(last + 1)
            if form is None:
                _log(f"ddg offers no page {last + 1}")
                return [], None
            results, next_form, error = await _fetch_page(session, query, form, pace)
            if error is not None:
                return [], error
            _remember(state, last + 1, results, next_form)

        results, next_form, error = await _fetch_page(session, query, state.forms[page], pace)
        if error is not None:
            return [], error
        _remember(state, page, results, next_form)

        earlier = {_url_key(u) for p, urls in state.urls.items() if p < page for u in urls}
        return [r for r in results if _url_key(r.url) not in earlier], None
