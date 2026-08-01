"""Transport for jobs.uber.com."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import uber_jobs_limiter

SITE = "https://www.uber.com"
_SEARCH_PATH = "/api/loadSearchJobsResults"
_DETAIL_PATH = "/api/loadSearchJobsResults"

_MAX_RESPONSE = 8 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 10

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


class UberJobsBlockedError(Exception):
    """Uber refused the request."""


class UberJobsUnavailableError(Exception):
    """Uber failed to answer (5xx), as distinct from refusing."""


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    timeout=timedelta(seconds=60),
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=_MAX_RESPONSE,
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


def _headers() -> dict[str, str]:
    # The endpoint requires the header to exist but never checks its value.
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "x-csrf-token": "x",
        "origin": SITE,
        "referer": f"{SITE}/us/en/careers/list/",
    }


def _check(resp) -> None:
    if resp.status_code in (401, 403, 429):
        resp_code = resp.status_code
        raise UberJobsBlockedError(f"jobs.uber.com declined the request ({resp_code}).")
    if resp.status_code >= 500:
        raise UberJobsUnavailableError(f"jobs.uber.com returned {resp.status_code}.")


def _total(data: dict) -> int:
    """Unwrap Uber's Long-shaped count, ``{"low": N, "high": 0}``."""
    raw = data.get("totalResults")
    if isinstance(raw, dict):
        low = raw.get("low")
        return int(low) if isinstance(low, (int, float)) else 0
    return int(raw) if isinstance(raw, (int, float)) else 0


def build_location(country: str = "", city: str = "", region: str = "") -> list[dict]:
    """Uber's ``params.location`` entry. An empty dict means "anywhere"."""
    entry: dict[str, str] = {}
    if country:
        entry["country"] = country
    if city:
        entry["city"] = city
    if region:
        entry["region"] = region
    return [entry] if entry else []


async def search_page(
    *,
    session,
    query: str = "",
    location: list[dict] | None = None,
    page: int = 0,
    limit: int = PAGE_SIZE,
) -> tuple[list[dict], int] | None:
    """One page. Returns ``(results, total)`` or None on an unusable body."""
    params: dict = {}
    if location:
        params["location"] = location
    if query:
        # A plain string; a list here silently returns zero results.
        params["query"] = query

    await uber_jobs_limiter.wait()
    resp = await session.post(
        f"{SITE}{_SEARCH_PATH}?localeCode=en",
        json={"params": params, "page": page, "limit": limit},
        headers=_headers(),
    )
    _check(resp)
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
    return results, _total(data)


async def search_all(
    *,
    session,
    query: str = "",
    location: list[dict] | None = None,
    limit: int = 100,
) -> tuple[list[dict], int]:
    """Page until ``limit`` results are collected or Uber runs out."""
    collected: list[dict] = []
    total = 0
    for page in range(MAX_PAGES):
        if len(collected) >= limit:
            break
        result = await search_page(
            session=session, query=query, location=location, page=page, limit=PAGE_SIZE
        )
        if result is None:
            break
        results, page_total = result
        if page == 0:
            total = page_total
        if not results:
            break
        collected.extend(results)
        if len(results) < PAGE_SIZE:
            break
    return collected[:limit], total


async def fetch_job(job_id: str, *, session) -> dict | None:
    """One posting, found by id through the same search endpoint.

    Uber exposes no per-posting JSON route — ``/api/{name}`` answers
    ``ERR_MISSING_HANDLER`` for every detail-shaped handler name tried, and the
    posting page is client-rendered with the body absent from the HTML. The
    board's own ``description`` field is returned empty on this endpoint, so a
    posting resolves to its metadata plus a link, not its full text.
    """
    result = await search_page(session=session, query=str(job_id), page=0, limit=PAGE_SIZE)
    if result is None:
        return None
    results, _total_count = result
    for job in results:
        if str(job.get("id")) == str(job_id):
            return job
    return None
