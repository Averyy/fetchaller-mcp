"""Transport for amazon.jobs.

``GET https://www.amazon.jobs/en/search.json`` answers anonymously and takes:

- ``base_query`` — free-text title/keyword search (fuzzy; see the package docstring).
- ``loc_query`` — free-text location, geocoded by Amazon and matched by radius.
- ``country`` — ISO-3166 alpha-3 ("CAN"), a hard filter.
- ``category[]`` — a slugified ``job_category`` ("design", "software-development").
  Repeatable. ``job_category[]`` and bare ``category`` are silently ignored.
- ``normalized_location[]`` — exact "Toronto, Ontario, CAN", repeatable.
- ``result_limit`` / ``offset`` / ``sort`` (``relevant`` | ``recent``).

The response is ``{hits, jobs: [...], facets}``. ``facets`` is always empty on
this route — the site computes them elsewhere — so category values are derived
by slugifying the ``job_category`` strings that come back on the postings.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import amazon_jobs_limiter

SITE = "https://www.amazon.jobs"
_SEARCH_PATH = "/en/search.json"

# Descriptions are long but bounded; the whole page of 100 stays well under this.
_MAX_RESPONSE = 12 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 5

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


class AmazonJobsBlockedError(Exception):
    """Amazon refused the request."""


class AmazonJobsUnavailableError(Exception):
    """Amazon failed to answer (5xx), as distinct from refusing."""


def category_slug(category: str) -> str:
    """Slugify a ``job_category`` the way ``category[]`` expects it."""
    return _SLUG_STRIP_RE.sub("-", (category or "").casefold()).strip("-")


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


def _check(resp) -> None:
    if resp.status_code in (401, 403, 429):
        raise AmazonJobsBlockedError(f"amazon.jobs declined the request ({resp.status_code}).")
    if resp.status_code >= 500:
        raise AmazonJobsUnavailableError(f"amazon.jobs returned {resp.status_code}.")


def _build_params(
    *,
    query: str,
    location: str,
    country: str,
    categories: list[str],
    normalized_locations: list[str],
    offset: int,
    limit: int,
    sort: str,
) -> list[tuple[str, str]]:
    # A list of pairs, not a dict: `category[]` and `normalized_location[]`
    # both repeat, and repeated values are OR'd.
    params: list[tuple[str, str]] = [
        ("base_query", query or ""),
        ("loc_query", location or ""),
        ("result_limit", str(limit)),
        ("offset", str(max(0, offset))),
        ("sort", sort),
    ]
    if country:
        params.append(("country", country))
    for category in categories:
        params.append(("category[]", category))
    for place in normalized_locations:
        params.append(("normalized_location[]", place))
    return params


async def search_jobs(
    *,
    session,
    query: str = "",
    location: str = "",
    country: str = "",
    categories: list[str] | None = None,
    normalized_locations: list[str] | None = None,
    offset: int = 0,
    limit: int = PAGE_SIZE,
    sort: str = "relevant",
) -> tuple[list[dict], int] | None:
    """One page. Returns ``(jobs, hits)`` or None on an unusable body."""
    await amazon_jobs_limiter.wait()
    resp = await session.get(
        SITE + _SEARCH_PATH,
        params=_build_params(
            query=query,
            location=location,
            country=country,
            categories=categories or [],
            normalized_locations=normalized_locations or [],
            offset=offset,
            limit=limit,
            sort=sort,
        ),
        headers={"accept": "application/json", "referer": f"{SITE}/en/search"},
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
    jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
    hits = payload.get("hits")
    return jobs, int(hits) if isinstance(hits, int) else len(jobs)


async def search_all_jobs(
    *,
    session,
    query: str = "",
    location: str = "",
    country: str = "",
    categories: list[str] | None = None,
    normalized_locations: list[str] | None = None,
    limit: int = 100,
    sort: str = "relevant",
) -> tuple[list[dict], int]:
    """Page until ``limit`` jobs are collected or Amazon runs out."""
    collected: list[dict] = []
    hits = 0
    for page in range(MAX_PAGES):
        if len(collected) >= limit:
            break
        result = await search_jobs(
            session=session,
            query=query,
            location=location,
            country=country,
            categories=categories,
            normalized_locations=normalized_locations,
            offset=page * PAGE_SIZE,
            limit=PAGE_SIZE,
            sort=sort,
        )
        if result is None:
            break
        jobs, page_hits = result
        if page == 0:
            hits = page_hits
        if not jobs:
            break
        collected.extend(jobs)
        if len(jobs) < PAGE_SIZE:
            break
    return collected[:limit], hits


async def discover_locations(
    *,
    session,
    country: str = "",
    query: str = "",
    pages: int = 3,
) -> list[str]:
    """Collect the distinct ``normalized_location`` values Amazon is using.

    ``normalized_location[]`` is the only parameter that actually filters by
    place — ``loc_query`` alone is ignored, returning the unfiltered board —
    but it demands Amazon's exact spelling ("Toronto, Ontario, CAN"; neither
    "Toronto" nor "Toronto, ON, CAN" matches). So the vocabulary is sampled
    here and matched against the caller's wording, the same way the Workday
    client resolves a location facet.
    """
    found: list[str] = []
    seen: set[str] = set()
    for page in range(max(1, pages)):
        result = await search_jobs(
            session=session,
            query=query,
            country=country,
            offset=page * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
        if result is None:
            break
        jobs, _hits = result
        if not jobs:
            break
        for job in jobs:
            value = job.get("normalized_location")
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                found.append(value)
        if len(jobs) < PAGE_SIZE:
            break
    return found


_JOB_ID_RE = re.compile(r"/jobs/(\d{4,20})")


async def fetch_job(job_path: str, *, session) -> dict | None:
    """One posting by its ``job_path`` (``/en/jobs/{id}/{slug}``).

    The posting page is server-rendered HTML with no JSON-LD and no ``.json``
    twin (that path answers 406), but ``search.json`` matches a bare requisition
    id exactly, so the id is looked up through search. That also returns the
    richer record — description, both qualification blocks, and the pay band —
    rather than what the page happens to show.
    """
    match = _JOB_ID_RE.search(job_path or "")
    if not match:
        return None
    job_id = match.group(1)
    result = await search_jobs(session=session, query=job_id, limit=10)
    if result is None:
        return None
    jobs, _hits = result
    for job in jobs:
        if str(job.get("id_icims") or job.get("id")) == job_id:
            return job
    return None
