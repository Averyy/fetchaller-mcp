"""Transport and hydration parsing for jobs.apple.com."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import apple_jobs_limiter

SITE = "https://jobs.apple.com"
DEFAULT_LOCALE = "en-ca"
# Apple renders 20 result cards per page.
PAGE_SIZE = 20
MAX_PAGES = 10

_MAX_RESPONSE = 12 * 1024 * 1024
_HYDRATION_MARKER = "window.__staticRouterHydrationData"
_JSON_PARSE_MARKER = 'JSON.parse("'
_LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


class AppleJobsBlockedError(Exception):
    """Apple refused the request."""


class AppleJobsUnavailableError(Exception):
    """Apple failed to answer (5xx), as distinct from refusing."""


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


def normalize_locale(locale: str) -> str:
    value = (locale or "").strip().casefold()
    return value if _LOCALE_RE.match(value) else DEFAULT_LOCALE


def location_slug(name: str, post_location_id: str) -> str:
    """Build the ``{slug}-{CODE}`` form the ``location`` parameter expects.

    ``postLocationId`` arrives as ``postLocation-TOR``; the URL wants
    ``toronto-TOR``.
    """
    code = (post_location_id or "").split("-", 1)[-1]
    slug = _SLUG_RE.sub("-", (name or "").casefold()).strip("-")
    if not code:
        return ""
    return f"{slug}-{code}" if slug else code


def parse_hydration(html: str) -> dict | None:
    """Extract ``window.__staticRouterHydrationData``.

    The blob is a JS string literal passed to ``JSON.parse``, so it is decoded
    twice: once as the JS literal, once as the JSON it contains.
    """
    start = html.find(_HYDRATION_MARKER)
    if start < 0:
        return None
    segment = html[start:]
    literal_start = segment.find(_JSON_PARSE_MARKER)
    if literal_start < 0:
        return None
    cursor = literal_start + len(_JSON_PARSE_MARKER)
    end = cursor
    while True:
        end = segment.find('")', end)
        if end == -1:
            return None
        # A `")` preceded by a backslash is escaped and still inside the string.
        if segment[end - 1] != "\\":
            break
        end += 1
    raw = segment[cursor:end]
    try:
        return json.loads(json.loads(f'"{raw}"'))
    except (json.JSONDecodeError, ValueError):
        return None


def _check(resp) -> None:
    if resp.status_code in (401, 403, 429):
        raise AppleJobsBlockedError(f"jobs.apple.com declined the request ({resp.status_code}).")
    if resp.status_code >= 500:
        raise AppleJobsUnavailableError(f"jobs.apple.com returned {resp.status_code}.")


async def fetch_search_page(
    *,
    session,
    locale: str = DEFAULT_LOCALE,
    search: str = "",
    location: str = "",
    page: int = 1,
) -> dict | None:
    """One search page. Returns the ``loaderData.search`` object."""
    params: list[tuple[str, str]] = []
    if search:
        params.append(("search", search))
    if location:
        params.append(("location", location))
    if page > 1:
        params.append(("page", str(page)))

    await apple_jobs_limiter.wait()
    resp = await session.get(
        f"{SITE}/{normalize_locale(locale)}/search",
        params=params or None,
        headers={"accept": "text/html"},
    )
    _check(resp)
    if resp.status_code != 200:
        return None
    data = parse_hydration(resp.text)
    if not isinstance(data, dict):
        return None
    search_data = (data.get("loaderData") or {}).get("search")
    return search_data if isinstance(search_data, dict) else None


async def search_all(
    *,
    session,
    locale: str = DEFAULT_LOCALE,
    search: str = "",
    location: str = "",
    limit: int = 25,
) -> tuple[list[dict], int]:
    """Page until ``limit`` results are collected or Apple runs out."""
    collected: list[dict] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        if len(collected) >= limit:
            break
        data = await fetch_search_page(
            session=session, locale=locale, search=search, location=location, page=page
        )
        if data is None:
            break
        results = data.get("searchResults")
        if not isinstance(results, list):
            break
        if page == 1:
            total = data.get("totalRecords") or 0
        if not results:
            break
        collected.extend(r for r in results if isinstance(r, dict))
        if len(results) < PAGE_SIZE:
            break
    return collected[:limit], total


async def discover_locations(
    *,
    session,
    locale: str = DEFAULT_LOCALE,
    probe: str = "",
) -> dict[str, str]:
    """Map location name -> ``{slug}-{CODE}`` from whatever postings come back.

    Apple's reference-data routes require a session token, but every posting
    carries its own ``locations[]`` with both the display name and the
    ``postLocationId``, so the vocabulary is recovered from results instead.
    """
    found: dict[str, str] = {}
    for page in (1, 2):
        data = await fetch_search_page(
            session=session, locale=locale, search=probe, page=page
        )
        if data is None:
            break
        results = data.get("searchResults")
        if not isinstance(results, list) or not results:
            break
        for job in results:
            for entry in job.get("locations") or []:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name") or ""
                slug = location_slug(name, entry.get("postLocationId") or "")
                if name and slug:
                    found.setdefault(name, slug)
        # A filter echo also names the resolved location.
        for entry in (data.get("filters") or {}).get("locations") or []:
            if isinstance(entry, dict):
                name = entry.get("titleName") or entry.get("name") or ""
                slug = location_slug(name, entry.get("id") or entry.get("uniqueKey") or "")
                if name and slug:
                    found.setdefault(name, slug)
        if len(results) < PAGE_SIZE:
            break
    return found


async def fetch_job_detail(
    position_id: str,
    slug: str,
    *,
    session,
    locale: str = DEFAULT_LOCALE,
) -> dict | None:
    """One posting from ``/{locale}/details/{positionId}/{slug}``."""
    await apple_jobs_limiter.wait()
    resp = await session.get(
        f"{SITE}/{normalize_locale(locale)}/details/{position_id}/{slug}",
        headers={"accept": "text/html"},
    )
    _check(resp)
    if resp.status_code != 200:
        return None
    data = parse_hydration(resp.text)
    if not isinstance(data, dict):
        return None
    details = (data.get("loaderData") or {}).get("jobDetails")
    if not isinstance(details, dict):
        return None
    jobs_data = details.get("jobsData")
    if isinstance(jobs_data, dict):
        return jobs_data
    if isinstance(jobs_data, list) and jobs_data and isinstance(jobs_data[0], dict):
        return jobs_data[0]
    return None
