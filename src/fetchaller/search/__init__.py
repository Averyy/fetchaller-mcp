"""Web search module — Google (primary) + DuckDuckGo (supplement)."""

import asyncio
import random
import sys
import time as time_module
from datetime import UTC, datetime

from ..config import get_wafer_cache_dir
from ..content.url import normalize_url
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


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


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
    now = time_module.time()
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
    summary = f'Search: "{query}"{page_str} | google: {google_str} | ddg: {ddg_str} | {total} total'

    errors = []
    if google_error:
        errors.append(f"  google: {google_error}")
    if ddg_error and page == 1:
        errors.append(f"  ddg: {ddg_error}")

    if not results:
        if errors:
            detail = "\n".join(errors)
            return (
                f"{summary}\n\nSearch FAILED — no engine returned results.\n"
                f"{detail}\n\n"
                "This is a transport failure on our side, not an empty result set. "
                "The query was never answered. Retry before concluding anything about "
                "these search terms."
            )
        return f"{summary}\n\nNo results found."

    if errors:
        detail = "\n".join(errors)
        return (
            f"{summary}\n\nPartial results — one engine failed:\n{detail}\n\n"
            + _format_results(results)
        )

    return f"{summary}\n\n{_format_results(results)}"


def _format_results(results: list[SearchResult]) -> str:
    """Render the numbered result list (no summary line)."""
    lines = []
    for i, r in enumerate(results, 1):
        # Align multi-digit numbers
        prefix = f"{i}."
        indent = " " * (len(prefix) + 1)
        lines.append(f"{prefix} {r.title}")
        lines.append(f"{indent}{r.url}")
        if r.snippet:
            lines.append(f"{indent}{r.snippet}")
        lines.append("")

    return "\n".join(lines).rstrip()


async def _rate_limited_google(session, query: str, page: int):
    """Execute Google search with rate limiting."""
    global _google_last_request
    async with _google_lock:
        now = time_module.time()
        wait = _GOOGLE_MIN_INTERVAL - (now - _google_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        # Add jitter
        await asyncio.sleep(random.uniform(1.0, 5.0))
        _google_last_request = time_module.time()
    return await search_google(session, query, page)


async def _rate_limited_ddg(session, query: str):
    """Execute DDG search with rate limiting."""
    global _ddg_last_request
    async with _ddg_lock:
        now = time_module.time()
        wait = _DDG_MIN_INTERVAL - (now - _ddg_last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _ddg_last_request = time_module.time()
    return await search_ddg(session, query)


def _handle_captcha() -> None:
    """Update CAPTCHA backoff state."""
    global _captcha_count, _captcha_last_time, _captcha_backoff_until
    now = time_module.time()

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
    return time_module.time() < _captcha_backoff_until


async def search(
    query: str, page: int = 1
) -> dict:
    """
    Search the web using Google + DuckDuckGo.

    Args:
        query: Search query
        page: Result page (1-indexed)

    Returns:
        Dict with "content" (formatted text) or "error" (error message).
    """
    # Input validation
    if not query or not query.strip():
        return {"error": "Search query cannot be empty."}

    query = query.strip()
    page = max(1, page)

    # Check cache
    cache_key = (query.lower(), page)
    _evict_cache()
    if cache_key in _cache:
        results, google_count, ddg_new_count, captcha, _, g_err, d_err = _cache[cache_key]
        return {
            "content": _format_output(
                query, results, google_count, ddg_new_count, captcha, page, g_err, d_err
            )
        }

    session = await _get_session()
    start_time = time_module.time()

    google_results: list[SearchResult] = []
    ddg_results: list[SearchResult] = []
    captcha = False
    google_error: str | None = None
    ddg_error: str | None = None

    if page == 1:
        # Page 1: query both engines in parallel
        google_backed_off = _google_backed_off()

        tasks = []
        if not google_backed_off:
            tasks.append(("google", asyncio.wait_for(_rate_limited_google(session, query, page), timeout=15)))
        # DDG gets its own session — the Opera Mini identity is Google-specific
        # and DDG answers it with a 202 homepage instead of results.
        ddg_session = await _get_ddg_session()
        tasks.append(("ddg", asyncio.wait_for(_rate_limited_ddg(ddg_session, query), timeout=15)))

        # Run in parallel
        async_tasks = {name: asyncio.create_task(coro) for name, coro in tasks}
        for name, task in async_tasks.items():
            try:
                result = await task
                if name == "google":
                    google_results, captcha, google_error = result
                    if captcha:
                        _handle_captcha()
                else:
                    ddg_results, ddg_error = result
            except TimeoutError:
                _log(f"{name} timed out")
                if name == "google":
                    google_error = "timed out after 15s"
                else:
                    ddg_error = "timed out after 15s"
            except Exception as e:
                _log(f"{name} error: {type(e).__name__}: {e}")
                if name == "google":
                    google_error = f"{type(e).__name__}: {e}"
                else:
                    ddg_error = f"{type(e).__name__}: {e}"

        if google_backed_off:
            captcha = True  # Show as captcha in summary
            google_error = None  # backoff is a deliberate skip, not a failure
    else:
        # Page 2+: Google only
        if _google_backed_off():
            captcha = True
        else:
            try:
                google_results, captcha, google_error = await asyncio.wait_for(
                    _rate_limited_google(session, query, page), timeout=15
                )
                if captcha:
                    _handle_captcha()
            except TimeoutError:
                _log("google timed out")
                google_error = "timed out after 15s"
            except Exception as e:
                _log(f"google error: {type(e).__name__}: {e}")
                google_error = f"{type(e).__name__}: {e}"

    # Merge and dedup
    merged, ddg_new_count = _dedup_and_merge(google_results, ddg_results)
    google_count = len(google_results)

    elapsed = time_module.time() - start_time
    _log(
        f'search query="{query}" google={google_count} ddg={ddg_new_count} '
        f"elapsed={elapsed:.1f}s"
        + (f" google_error={google_error}" if google_error else "")
        + (f" ddg_error={ddg_error}" if ddg_error else "")
    )

    # Cache when we have results (even DDG-only during Google captcha backoff).
    # Don't cache empty results — those may be transient engine failures.
    if merged:
        _cache[cache_key] = (
            merged, google_count, ddg_new_count, captcha, time_module.time(),
            google_error, ddg_error,
        )

    return {
        "content": _format_output(
            query, merged, google_count, ddg_new_count, captcha, page, google_error, ddg_error
        )
    }
