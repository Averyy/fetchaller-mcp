"""Unified web search: Google by default, selectable providers, explicit fallback."""

import asyncio
import json
import sys
import time as time_module
from datetime import UTC, datetime

from ..config import get_wafer_cache_dir
from ..content.url import normalize_url
from ..security.xss import redact_secrets_for_log, sanitize_for_log
from .brave import fetch_brave
from .ddg import MAX_DDG_PAGE, search_ddg
from .google import search_google
from .models import SearchResult

PROVIDERS = ("google", "ddg", "brave")
_MAX_QUERY_LENGTH = 512
_MAX_PAGE = 100
_ENGINE_TIMEOUT = 15.0
_GOOGLE_TIMEOUT = 50.0
_BRAVE_TIMEOUT = 180.0  # Provider queue/pacing, 150s transport/solve, and parsing.
_DDG_WALK_BUDGET = 10.0
_CACHE_TTL = 300
_CACHE_MAX_SIZE = 1000
_MAX_LOG_LENGTH = 500
_MAX_RESULT_TITLE_CHARS = 500
_MAX_RESULT_URL_CHARS = 8192
_MAX_RESULT_SNIPPET_CHARS = 1000
_MAX_RESULTS_OUTPUT_CHARS = 240_000
_cache: dict[tuple[str, int, str], tuple[dict, float]] = {}
_sessions: dict[str, object] = {}
_session_solvers: dict[str, object] = {}
_session_lock = asyncio.Lock()


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
            f"{prefix} {r.title[:_MAX_RESULT_TITLE_CHARS]}" + (f" [{r.source}]" if r.source else ""),
            f"{indent}{r.url}",
        ]
        if r.snippet:
            lines.append(f"{indent}{r.snippet[:_MAX_RESULT_SNIPPET_CHARS]}")
        block = "\n".join(lines)
        separator = "\n\n" if blocks else ""
        remaining = len(results) - i
        marker = f"\n\n[Omitted {remaining + 1} trailing search results at the safe output limit]"
        if length + len(separator) + len(block) + (len(marker) if remaining else 0) > _MAX_RESULTS_OUTPUT_CHARS:
            if length + len(marker) <= _MAX_RESULTS_OUTPUT_CHARS:
                blocks.append(marker.lstrip("\n"))
            break
        blocks.append(block)
        length += len(separator) + len(block)

    return "\n\n".join(blocks)


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


async def _get_session(provider: str, *, browser_solver=None):
    async with _session_lock:
        # Never reuse a session bound to another server's solver/egress guard,
        # including a session first created by a browser-free library caller.
        changed_solver = provider == "brave" and _session_solvers.get(provider) is not browser_solver
        if provider not in _sessions or changed_solver:
            from wafer import AsyncSession

            # Google's JSON API never follows authentication or /sorry redirects.
            # Separate sessions keep each provider's transport state independent.
            options = {"max_rotations": 0, "max_retries": 0, "rate_limit": 0.0}
            if provider == "google":
                # Google owns its bounded attempt budget and global 429 backoff.
                options.update(follow_redirects=False, max_retries=0)
            else:
                options.update(cache_dir=get_wafer_cache_dir(), rate_limit=6.0)
            if provider == "brave":
                options["browser_solver"] = browser_solver
            _sessions[provider] = AsyncSession(**options)
            if provider == "brave":
                _session_solvers[provider] = browser_solver
        return _sessions[provider]


async def close_session() -> None:
    _sessions.clear()
    _session_solvers.clear()  # The server owns and closes the injected solver.


async def _run_engine(
    provider: str, query: str, page: int, *, browser_solver=None,
) -> tuple[list[SearchResult], str | None]:
    if provider == "ddg" and page > MAX_DDG_PAGE:
        return [], f"DuckDuckGo supports pages 1-{MAX_DDG_PAGE}; skipped page {page}"
    session = await _get_session(provider, browser_solver=browser_solver)
    if provider == "google":
        coroutine = search_google(session, query, page)
    elif provider == "ddg":
        coroutine = search_ddg(session, query, page)
    else:
        coroutine = fetch_brave(session, query, page)
    task = asyncio.create_task(coroutine, name=f"fetchaller-search-{provider}")
    timeout = _GOOGLE_TIMEOUT if provider == "google" else _ENGINE_TIMEOUT
    if provider == "brave":
        timeout = _BRAVE_TIMEOUT
    if provider == "ddg":
        timeout += (page - 1) * _DDG_WALK_BUDGET
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        _cancel_and_detach((task,))
        raise
    if not done:
        _cancel_and_detach((task,))
        return [], f"timed out after {timeout:g}s"
    try:
        outcome = task.result()
        if provider == "google":
            results, throttled, error = outcome
            return results, error or ("Google rate-limit backoff" if throttled else None)
        return outcome
    except asyncio.CancelledError:
        return [], "Provider task was cancelled"
    except Exception as error:
        return [], _safe_error(error)


def _render(query: str, page: int, provider: str, outcomes: dict) -> dict:
    merged = []
    seen = set()
    columns = []
    errors = []
    answered = False
    for name, (results, error) in outcomes.items():
        skipped = name == "ddg" and page > MAX_DDG_PAGE
        columns.append(f"{name}: {'skipped' if skipped else 'ERROR' if error else len(results)}")
        if skipped:
            continue
        if error:
            errors.append(f"{name}: {error}")
        else:
            answered = True
        for result in results:
            key = _dedup_key(result.url)
            if key not in seen:
                seen.add(key)
                # Preserve the actual source after merging or fallback.
                merged.append(SearchResult(result.title, result.url, result.snippet, source=name))
    summary = f"Search: {json.dumps(query, ensure_ascii=False)} | requested: {provider} | page {page} | {' | '.join(columns)} | {len(merged)} total"
    notices = []
    if "ddg" in outcomes and page > MAX_DDG_PAGE:
        notices.append(f"DuckDuckGo supports pages 1-{MAX_DDG_PAGE}; skipped page {page}.")
    if provider == "google" and len(outcomes) > 1:
        sources = [name for name, (results, error) in outcomes.items() if name != "google" and error is None]
        notices.append(
            f"Google unavailable; automatic fallback to {', '.join(sources) if sources else 'other providers (all failed)'}. Results are labelled by source."
        )
    if errors:
        notices.append("Provider failures:\n" + "\n".join(errors))
    if merged:
        content = _format_results(merged)
    elif answered:
        content = (
            "No results found."
            if not errors
            else "Partial search: an available provider returned no results; coverage is incomplete."
        )
    else:
        content = "Search FAILED — no provider answered the query. Retry before treating this as an empty result set."
    text = "\n\n".join([summary, *notices, content])
    return {"content" if merged or answered else "error": text}


async def search(query: str, page: int = 1, provider: str = "google", *, browser_solver=None) -> dict:
    """Select Google, DDG, Brave or all. Google failures fall back to DDG then Brave."""
    if not isinstance(query, str) or not query.strip():
        return {"error": "Search query cannot be empty."}
    if len(query) > _MAX_QUERY_LENGTH:
        return {"error": f"Search query is too long (maximum {_MAX_QUERY_LENGTH} characters)."}
    if type(page) is not int or not 1 <= page <= _MAX_PAGE:
        return {"error": f"Search page must be an integer between 1 and {_MAX_PAGE}."}
    if not isinstance(provider, str) or provider not in (*PROVIDERS, "all"):
        return {"error": "Search provider must be google, ddg, brave, or all."}
    if provider == "ddg" and page > MAX_DDG_PAGE:
        return {"error": f"DuckDuckGo supports pages 1-{MAX_DDG_PAGE}; choose google, brave, or all."}
    query = " ".join(query.split())
    # Preserve case: search operators and quoted phrases can be case-sensitive.
    key = (query, page, provider)
    now = time_module.monotonic()
    for cached_key, (_, saved) in list(_cache.items()):
        if now - saved >= _CACHE_TTL:
            del _cache[cached_key]
    if key in _cache:
        return dict(_cache[key][0])
    outcomes = {}
    engine_options = {"browser_solver": browser_solver} if browser_solver is not None else {}
    if provider == "all":
        # Await all providers without letting one exception abandon its siblings.
        tasks = {name: asyncio.create_task(_run_engine(name, query, page, **engine_options)) for name in PROVIDERS}
        try:
            values = await asyncio.gather(*tasks.values(), return_exceptions=True)
        except asyncio.CancelledError:
            _cancel_and_detach(tasks.values())
            raise
        for name, value in zip(tasks, values, strict=True):
            outcomes[name] = ([], _safe_error(value)) if isinstance(value, BaseException) else value
    else:
        names = PROVIDERS if provider == "google" else (provider,)
        for name in names:
            if name == "ddg" and page > MAX_DDG_PAGE:
                outcomes[name] = ([], f"DuckDuckGo supports pages 1-{MAX_DDG_PAGE}; skipped page {page}")
                continue
            try:
                outcomes[name] = await _run_engine(name, query, page, **engine_options)
            except Exception as error:
                outcomes[name] = ([], _safe_error(error))
            if outcomes[name][1] is None:
                break  # An honest zero answers the query and does not trigger fallback.
    result = _render(query, page, provider, outcomes)
    _log(
        f"search query_len={len(query)} provider={provider} "
        + " ".join(f"{name}={len(rows)} failed={error is not None}" for name, (rows, error) in outcomes.items())
    )
    # Cache complete successes only: recovery is retried on the next fallback call.
    if (
        "content" in result
        and any(rows for rows, _ in outcomes.values())
        and all(error is None for _, error in outcomes.values())
    ):
        if len(_cache) >= _CACHE_MAX_SIZE:
            del _cache[min(_cache, key=lambda k: _cache[k][1])]
        _cache[key] = (dict(result), time_module.monotonic())
    return result
