"""Web search module — Google (primary) + DuckDuckGo (supplement)."""

import asyncio
import random
import sys
import time as time_module
from datetime import UTC, datetime

from ..content.url import normalize_url
from .ddg import search_ddg
from .google import search_google
from .models import SearchResult

# Lazy session — created on first search, closed on shutdown
_session = None
_session_lock = asyncio.Lock()

# Query cache: (query_lower, page) -> (results, google_count, ddg_new_count, captcha, timestamp)
_cache: dict[tuple[str, int], tuple[list[SearchResult], int, int, bool, float]] = {}
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
    """Get or create the shared AsyncSession (no impersonate)."""
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                from curl_cffi.requests import AsyncSession

                _session = AsyncSession()
    return _session


async def close_session() -> None:
    """Close the shared search session. Called on server shutdown."""
    global _session
    async with _session_lock:
        if _session is not None:
            await _session.close()
            _session = None


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
) -> str:
    """Format search results as text output."""
    total = len(results)

    # Build summary line
    google_str = "captcha" if captcha else str(google_count)
    ddg_str = "n/a" if page > 1 else f"{ddg_new_count} new"
    page_str = f" (page {page})" if page > 1 else ""
    summary = f'Search: "{query}"{page_str} | google: {google_str} | ddg: {ddg_str} | {total} total'

    if not results:
        return f"{summary}\n\nNo results found."

    lines = [summary, ""]
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
        results, google_count, ddg_new_count, captcha, _ = _cache[cache_key]
        return {"content": _format_output(query, results, google_count, ddg_new_count, captcha, page)}

    session = await _get_session()
    start_time = time_module.time()

    google_results: list[SearchResult] = []
    ddg_results: list[SearchResult] = []
    captcha = False

    if page == 1:
        # Page 1: query both engines in parallel
        google_backed_off = _google_backed_off()

        tasks = []
        if not google_backed_off:
            tasks.append(("google", asyncio.wait_for(_rate_limited_google(session, query, page), timeout=15)))
        tasks.append(("ddg", asyncio.wait_for(_rate_limited_ddg(session, query), timeout=15)))

        # Run in parallel
        async_tasks = {name: asyncio.create_task(coro) for name, coro in tasks}
        for name, task in async_tasks.items():
            try:
                result = await task
                if name == "google":
                    google_results, captcha = result
                    if captcha:
                        _handle_captcha()
                else:
                    ddg_results = result
            except TimeoutError:
                _log(f"{name} timed out")
            except Exception as e:
                _log(f"{name} error: {type(e).__name__}: {e}")

        if google_backed_off:
            captcha = True  # Show as captcha in summary
    else:
        # Page 2+: Google only
        if _google_backed_off():
            captcha = True
        else:
            try:
                google_results, captcha = await asyncio.wait_for(
                    _rate_limited_google(session, query, page), timeout=15
                )
                if captcha:
                    _handle_captcha()
            except TimeoutError:
                _log("google timed out")
            except Exception as e:
                _log(f"google error: {type(e).__name__}: {e}")

    # Merge and dedup
    merged, ddg_new_count = _dedup_and_merge(google_results, ddg_results)
    google_count = len(google_results)

    elapsed = time_module.time() - start_time
    _log(f'search query="{query}" google={google_count} ddg={ddg_new_count} elapsed={elapsed:.1f}s')

    # Cache when we have results (even DDG-only during Google captcha backoff).
    # Don't cache empty results — those may be transient engine failures.
    if merged:
        _cache[cache_key] = (merged, google_count, ddg_new_count, captcha, time_module.time())

    return {"content": _format_output(query, merged, google_count, ddg_new_count, captcha, page)}
