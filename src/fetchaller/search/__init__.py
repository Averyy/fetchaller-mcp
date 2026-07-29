"""Web search module — Google (primary) + DuckDuckGo (supplement)."""

import asyncio
import json
import random
import sys
import time as time_module
from datetime import UTC, datetime

from ..config import get_wafer_cache_dir
from ..content.url import normalize_url
from ..security.xss import redact_secrets_for_log, sanitize_for_log
from .ddg import search_ddg
from .google import search_google
from .models import SearchResult

# Lazy sessions — created on first search, closed on shutdown.
# Google and DDG need different TLS identities (see _get_ddg_session).
_session = None
_session_lock = asyncio.Lock()
_ddg_session = None
_ddg_session_lock = asyncio.Lock()

# Query cache:
#   (query_lower, page) -> (results, google_count, ddg_new_count, captcha,
#                           timestamp, google_error, ddg_error)
# The engine errors are cached alongside the results so a replayed partial
# result still says which engine failed. Without them the first caller sees
# "ddg: ERROR" and everyone within the TTL sees a clean "ddg: 0 new" for the
# very same incomplete result set.
_cache: dict[
    tuple[str, int],
    tuple[list[SearchResult], int, int, bool, float, str | None, str | None],
] = {}
_CACHE_TTL = 300  # 5 minutes
_CACHE_MAX_SIZE = 1000

# Rate limiting: per-engine lock + last request timestamp
_google_lock = asyncio.Lock()
_google_last_request: float = 0.0
_GOOGLE_MIN_INTERVAL = 5.0  # seconds

_ddg_lock = asyncio.Lock()
_ddg_last_request: float = 0.0
_DDG_MIN_INTERVAL = 1.0  # seconds

# CAPTCHA backoff state
_captcha_count = 0
_captcha_last_time: float = 0.0
_captcha_backoff_until: float = 0.0
_CAPTCHA_DURATIONS = [120, 300, 900]  # 2min, 5min, 15min
_CAPTCHA_RESET_AFTER = 3600  # Reset counter after 1 hour clean
_MAX_QUERY_LENGTH = 512
_MAX_PAGE = 100
_ENGINE_TIMEOUT = 15.0
_MAX_LOG_LENGTH = 500
_MAX_RESULT_TITLE_CHARS = 500
_MAX_RESULT_URL_CHARS = 8_192
_MAX_RESULT_SNIPPET_CHARS = 1_000
_MAX_RESULTS_OUTPUT_CHARS = 240_000


def _log(msg: str) -> None:
    bounded = sanitize_for_log(
        " ".join(str(msg).splitlines()),
        max_length=_MAX_LOG_LENGTH * 2,
    )
    safe = sanitize_for_log(
        redact_secrets_for_log(bounded),
        max_length=_MAX_LOG_LENGTH,
    )
    print(f"[{datetime.now(UTC).isoformat()}] {safe}", file=sys.stderr)


def _consume_background_task(task: asyncio.Task) -> None:
    """Retrieve the result of a detached provider without blocking."""

    try:
        task.exception()
    except BaseException:
        pass


def _cancel_and_detach(tasks) -> None:
    """Request cancellation while preserving the caller's hard deadline."""

    for task in tasks:
        if task.done():
            _consume_background_task(task)
            continue
        task.cancel()
        task.add_done_callback(_consume_background_task)


async def _await_provider(awaitable, *, name: str):
    """Await one provider with a hard timeout and bounded cancellation."""

    task = asyncio.create_task(awaitable, name=f"fetchaller-search-{name}")
    try:
        done, _ = await asyncio.wait({task}, timeout=_ENGINE_TIMEOUT)
    except asyncio.CancelledError:
        _cancel_and_detach((task,))
        raise
    if not done:
        _cancel_and_detach((task,))
        raise TimeoutError
    return task.result()


async def _get_session():
    """Get or create the shared AsyncSession with Opera Mini profile.

    Google only: the SSR endpoint is requested with client=ms-opera-mini-android,
    so the TLS identity has to match the client the query claims to be.
    """
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                from wafer import AsyncSession, Profile

                _session = AsyncSession(
                    profile=Profile.OPERA_MINI,
                    max_rotations=0,
                    rate_limit=0.0,
                    cache_dir=get_wafer_cache_dir(),
                )
    return _session


async def _get_ddg_session():
    """Get or create the DDG session — deliberately NOT the Opera Mini profile.

    DDG's html endpoint answers an Opera Mini identity with HTTP 202 and the
    generic DuckDuckGo homepage instead of results; the default profile gets a
    normal 200 with the result list. Sharing one Opera Mini session across both
    engines therefore broke DDG on every single query, and because the old code
    reported a non-200 as an empty list it showed up as a plausible-looking
    "ddg: 0 new" rather than a failure.
    """
    global _ddg_session
    if _ddg_session is None:
        async with _ddg_session_lock:
            if _ddg_session is None:
                from wafer import AsyncSession

                _ddg_session = AsyncSession(
                    max_rotations=0,
                    rate_limit=0.0,
                    cache_dir=get_wafer_cache_dir(),
                )
    return _ddg_session


async def close_session() -> None:
    """Release the shared search sessions. Called on server shutdown."""
    global _session, _ddg_session
    _session = None
    _ddg_session = None


def _dedup_key(url: str) -> str:
    """Normalize URL for dedup: strip fragment, strip www., then normalize."""
    base = url.split("#")[0]
    normalized = normalize_url(base)
    # Cache normalization deliberately preserves a syntactically present empty
    # query marker, but search dedup treats a URL containing only stripped
    # tracking parameters as the same resource as the query-free URL.
    if normalized.endswith("?"):
        normalized = normalized[:-1]
    # normalize_url doesn't strip www. — do it here for search dedup
    return normalized.replace("://www.", "://", 1)


def _dedup_and_merge(
    google_results: list[SearchResult],
    ddg_results: list[SearchResult],
) -> tuple[list[SearchResult], int]:
    """
    Merge results: Google first, then DDG supplements not in Google set.

    Returns:
        Tuple of (merged_results, ddg_new_count).
    """
    seen = set()
    merged = []

    # Google results first (preserving ranking)
    for r in google_results:
        key = _dedup_key(r.url)
        if key not in seen:
            seen.add(key)
            merged.append(r)

    # DDG supplements — only new URLs
    ddg_new = 0
    for r in ddg_results:
        key = _dedup_key(r.url)
        if key not in seen:
            seen.add(key)
            merged.append(r)
            ddg_new += 1

    return merged, ddg_new


def _evict_cache() -> None:
    """Remove expired cache entries and enforce max size."""
    now = time_module.monotonic()
    expired = [k for k, v in _cache.items() if now - v[4] > _CACHE_TTL]
    for k in expired:
        del _cache[k]

    # Enforce max size — evict oldest entries by timestamp
    if len(_cache) >= _CACHE_MAX_SIZE:
        by_age = sorted(_cache.items(), key=lambda kv: kv[1][4])
        for k, _ in by_age[: len(_cache) - _CACHE_MAX_SIZE + 1]:
            del _cache[k]


def _format_output(
    query: str,
    results: list[SearchResult],
    google_count: int,
    ddg_new_count: int,
    captcha: bool,
    page: int,
    google_error: str | None = None,
    ddg_error: str | None = None,
    *,
    answered: bool | None = None,
) -> str:
    """Format search results as text output.

    A transport failure must never render as a bare "0" — that reads as "the web
    has nothing on this" and sends the caller off to work around a problem that
    is really a one-off network error on our side.
    """
    total = len(results)

    # Build summary line
    if captcha:
        google_str = "captcha"
    elif google_error:
        google_str = "ERROR"
    else:
        google_str = str(google_count)

    if page > 1:
        ddg_str = "n/a"
    elif ddg_error:
        ddg_str = "ERROR"
    else:
        ddg_str = f"{ddg_new_count} new"

    page_str = f" (page {page})" if page > 1 else ""
    display_query = json.dumps(query, ensure_ascii=False)
    summary = f"Search: {display_query}{page_str} | google: {google_str} | ddg: {ddg_str} | {total} total"

    errors = []
    if google_error:
        errors.append(f"  google: {google_error}")
    if ddg_error and page == 1:
        errors.append(f"  ddg: {ddg_error}")

    if not results:
        if errors and answered:
            detail = "\n".join(errors)
            return (
                f"{summary}\n\nPartial search — a working engine returned no "
                f"results, but another engine failed:\n{detail}\n\n"
                "Retry before treating this as complete web coverage."
            )
        if errors:
            detail = "\n".join(errors)
            return (
                f"{summary}\n\nSearch FAILED — no engine returned results.\n"
                f"{detail}\n\n"
                "This is a transport failure on our side, not an empty result set. "
                "The query was never answered. Retry before concluding anything about "
                "these search terms."
            )
        if answered is False:
            return (
                f"{summary}\n\nSearch FAILED — no engine answered the query. "
                "Google is currently unavailable due to CAPTCHA/backoff. Retry later."
            )
        return f"{summary}\n\nNo results found."

    if errors:
        detail = "\n".join(errors)
        return f"{summary}\n\nPartial results — one engine failed:\n{detail}\n\n" + _format_results(results)

    return f"{summary}\n\n{_format_results(results)}"


def _format_results(results: list[SearchResult]) -> str:
    """Render a bounded list, omitting only complete trailing results."""
    blocks: list[str] = []
    length = 0
    for i, r in enumerate(results, 1):
        if not r.url.startswith(("http://", "https://")) or len(r.url) > _MAX_RESULT_URL_CHARS:
            continue
        # Align multi-digit numbers
        prefix = f"{i}."
        indent = " " * (len(prefix) + 1)
        lines = [
            f"{prefix} {r.title[:_MAX_RESULT_TITLE_CHARS]}",
            f"{indent}{r.url}",
        ]
        if r.snippet:
            lines.append(f"{indent}{r.snippet[:_MAX_RESULT_SNIPPET_CHARS]}")
        block = "\n".join(lines)
        separator = "\n\n" if blocks else ""
        remaining = len(results) - i
        marker = f"\n\n[Omitted {remaining + 1} trailing search results at the safe output limit]"
        if length + len(separator) + len(block) > _MAX_RESULTS_OUTPUT_CHARS:
            if length + len(marker) <= _MAX_RESULTS_OUTPUT_CHARS:
                blocks.append(marker.lstrip("\n"))
            break
        blocks.append(block)
        length += len(separator) + len(block)

    return "\n\n".join(blocks)


async def _rate_limited_google(session, query: str, page: int):
    """Execute Google search with rate limiting."""
    global _google_last_request
    async with _google_lock:
        now = time_module.monotonic()
        wait = _GOOGLE_MIN_INTERVAL - (now - _google_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        # Add jitter
        await asyncio.sleep(random.uniform(1.0, 5.0))
        _google_last_request = time_module.monotonic()
    return await search_google(session, query, page)


async def _rate_limited_ddg(session, query: str):
    """Execute DDG search with rate limiting."""
    global _ddg_last_request
    async with _ddg_lock:
        now = time_module.monotonic()
        wait = _DDG_MIN_INTERVAL - (now - _ddg_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _ddg_last_request = time_module.monotonic()
    return await search_ddg(session, query)


def _handle_captcha() -> None:
    """Update CAPTCHA backoff state."""
    global _captcha_count, _captcha_last_time, _captcha_backoff_until
    now = time_module.monotonic()

    # Reset counter if it's been clean for an hour
    if now - _captcha_last_time > _CAPTCHA_RESET_AFTER:
        _captcha_count = 0

    _captcha_count += 1
    _captcha_last_time = now

    idx = min(_captcha_count - 1, len(_CAPTCHA_DURATIONS) - 1)
    duration = _CAPTCHA_DURATIONS[idx]
    _captcha_backoff_until = now + duration
    _log(f"google captcha, backing off {duration}s")


def _google_backed_off() -> bool:
    """Check if Google is currently in CAPTCHA backoff."""
    return time_module.monotonic() < _captcha_backoff_until


async def search(query: str, page: int = 1) -> dict:
    """
    Search the web using Google + DuckDuckGo.

    Args:
        query: Search query
        page: Result page (1-indexed)

    Returns:
        Dict with "content" (formatted text) or "error" (error message).
    """
    # Input validation
    if not isinstance(query, str) or not query.strip():
        return {"error": "Search query cannot be empty."}
    if len(query) > _MAX_QUERY_LENGTH:
        return {"error": (f"Search query is too long (maximum {_MAX_QUERY_LENGTH} characters).")}
    if isinstance(page, bool) or not isinstance(page, int):
        return {"error": "Search page must be an integer."}
    if page < 1 or page > _MAX_PAGE:
        return {"error": f"Search page must be between 1 and {_MAX_PAGE}."}

    # Search providers treat whitespace runs equivalently. Normalizing them
    # keeps control characters out of result summaries/cache keys without
    # changing the terms sent to either engine.
    query = " ".join(query.split())
    # Check cache
    cache_key = (query.lower(), page)
    _evict_cache()
    if cache_key in _cache:
        results, google_count, ddg_new_count, captcha, _, g_err, d_err = _cache[cache_key]
        return {"content": _format_output(query, results, google_count, ddg_new_count, captcha, page, g_err, d_err)}

    session = await _get_session()
    start_time = time_module.monotonic()

    google_results: list[SearchResult] = []
    ddg_results: list[SearchResult] = []
    captcha = False
    google_error: str | None = None
    ddg_error: str | None = None
    google_answered = False
    ddg_answered = False

    if page == 1:
        # Page 1: query both engines in parallel
        google_backed_off = _google_backed_off()

        # Resolve sessions before constructing coroutine objects. If session
        # creation is cancelled, this avoids leaving an un-awaited engine
        # coroutine behind.
        ddg_session = await _get_ddg_session()
        jobs = []
        if not google_backed_off:
            jobs.append(
                (
                    "google",
                    _rate_limited_google(session, query, page),
                )
            )
        # DDG gets its own session — the Opera Mini identity is Google-specific
        # and DDG answers it with a 202 homepage instead of results.
        jobs.append(
            (
                "ddg",
                _rate_limited_ddg(ddg_session, query),
            )
        )

        # Run in parallel without asyncio.wait_for/gather cancellation
        # semantics: a provider that suppresses CancelledError must not extend
        # either the engine timeout or an MCP client-disconnect cancellation.
        engine_tasks = {name: asyncio.create_task(coro, name=f"fetchaller-search-{name}") for name, coro in jobs}
        try:
            done, pending = await asyncio.wait(
                set(engine_tasks.values()),
                timeout=_ENGINE_TIMEOUT,
            )
        except asyncio.CancelledError:
            _cancel_and_detach(engine_tasks.values())
            raise

        outcomes = []
        for name, _ in jobs:
            task = engine_tasks[name]
            if task in pending:
                outcomes.append(TimeoutError())
                continue
            try:
                outcomes.append(task.result())
            except BaseException as error:
                outcomes.append(error)
        _cancel_and_detach(pending)

        for (name, _), result in zip(jobs, outcomes, strict=True):
            try:
                if isinstance(result, asyncio.CancelledError):
                    detail = "CancelledError: provider task was cancelled"
                    if name == "google":
                        google_error = detail
                    else:
                        ddg_error = detail
                    continue
                if isinstance(result, BaseException):
                    raise result
                if name == "google":
                    google_results, captcha, google_error = result
                    google_answered = not captcha and google_error is None
                    if captcha:
                        _handle_captcha()
                else:
                    ddg_results, ddg_error = result
                    ddg_answered = ddg_error is None
            except TimeoutError:
                _log(f"{name} timed out")
                if name == "google":
                    google_error = f"timed out after {_ENGINE_TIMEOUT:g}s"
                else:
                    ddg_error = f"timed out after {_ENGINE_TIMEOUT:g}s"
            except Exception as e:
                detail = _safe_error(e)
                _log(f"{name} error: {type(e).__name__}")
                if name == "google":
                    google_error = detail
                else:
                    ddg_error = detail

        if google_backed_off:
            captcha = True  # Show as captcha in summary
            google_error = None  # backoff is a deliberate skip, not a failure
    else:
        # Page 2+: Google only
        if _google_backed_off():
            captcha = True
        else:
            try:
                google_results, captcha, google_error = await _await_provider(
                    _rate_limited_google(session, query, page),
                    name="google",
                )
                google_answered = not captcha and google_error is None
                if captcha:
                    _handle_captcha()
            except TimeoutError:
                _log("google timed out")
                google_error = f"timed out after {_ENGINE_TIMEOUT:g}s"
            except asyncio.CancelledError as error:
                if (
                    asyncio.current_task() is not None
                    and asyncio.current_task().cancelling()
                ):
                    raise
                google_error = _safe_error(error)
                _log("google provider cancelled itself")
            except Exception as e:
                google_error = _safe_error(e)
                _log(f"google error: {type(e).__name__}")

    # Merge and dedup
    merged, ddg_new_count = _dedup_and_merge(google_results, ddg_results)
    google_count = len(google_results)

    elapsed = time_module.monotonic() - start_time
    _log(
        f"search query_len={len(query)} google={google_count} ddg={ddg_new_count} "
        f"elapsed={elapsed:.1f}s"
        + (f" google_failed={google_error is not None}")
        + (f" ddg_failed={ddg_error is not None}")
    )

    # Cache when we have results (even DDG-only during Google captcha backoff).
    # Don't cache empty results — those may be transient engine failures.
    if merged:
        _cache[cache_key] = (
            merged,
            google_count,
            ddg_new_count,
            captcha,
            time_module.monotonic(),
            google_error,
            ddg_error,
        )

    output = _format_output(
        query,
        merged,
        google_count,
        ddg_new_count,
        captcha,
        page,
        google_error,
        ddg_error,
        answered=google_answered or ddg_answered,
    )
    if not merged and not (google_answered or ddg_answered):
        return {"error": output}

    return {"content": output}


def _safe_error(error: BaseException) -> str:
    """Return a bounded, single-line, secret-redacted engine diagnostic."""
    bounded = sanitize_for_log(
        " ".join(str(error).splitlines()),
        max_length=400,
    )
    detail = sanitize_for_log(
        redact_secrets_for_log(bounded),
        max_length=200,
    )
    return f"{type(error).__name__}: {detail}"
