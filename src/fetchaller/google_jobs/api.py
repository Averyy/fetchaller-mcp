"""Transport for Google careers' anonymous ``batchexecute`` RPC.

Google's careers front end is a BOQ app. Its data calls go to::

    POST /about/careers/applications/_/HiringCportalFrontendUi/data/batchexecute
    Content-Type: application/x-www-form-urlencoded;charset=UTF-8
    f.req=[[["<rpc>","<json-encoded args>",null,"generic"]]]

No cookie, token, or referer is required. Two RPCs are used here: ``r06xKb``
for search and ``sf9Qmf`` for a single posting.

The response is XSSI-guarded and doubly encoded: it opens with ``)]}'``, and
the useful payload is a *JSON string* sitting at ``outer[0][2]`` of a
``[["wrb.fr", ...]]`` envelope, which has to be decoded a second time.

Everything is positional. The search argument list is a single array whose
slots carry the filters, and the job record is a 21-element array. Both are
documented by index below because there are no field names to rely on.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import google_jobs_limiter

SITE = "https://www.google.com"
_BASE = "/about/careers/applications"
_RPC_PATH = f"{_BASE}/_/HiringCportalFrontendUi/data/batchexecute"
SEARCH_RPC = "r06xKb"
DETAIL_RPC = "sf9Qmf"

# Fixed server-side. `page_size`, `size`, and `limit` are all silently ignored.
PAGE_SIZE = 20
MAX_PAGES = 10
_MAX_RESPONSE = 12 * 1024 * 1024

# Search argument slots. Anything not set stays null.
_ARG_QUERY = 0
_ARG_COMPANY = 1
_ARG_DEGREE = 2
_ARG_EMPLOYMENT = 3
_ARG_LOCALE = 4
_ARG_LOCATIONS = 6
_ARG_PAGE = 7
_ARG_SKILLS = 8
_ARG_REMOTE = 9
_ARG_SORT = 10
_ARG_TARGET_LEVEL = 16
_ARG_SLOTS = 17

SORT_CODES = {"date": 1, "relevance": 2}
EMPLOYMENT_CODES = {"full_time": 1, "intern": 2, "part_time": 3, "temporary": 4}
TARGET_LEVEL_CODES = {
    "early": 1,
    "mid": 2,
    "advanced": 3,
    "director_plus": 4,
    "intern_and_apprentice": 5,
}

# Job record slots.
JOB_ID = 0
JOB_TITLE = 1
JOB_APPLY_URL = 2
JOB_RESPONSIBILITIES = 3
JOB_QUALIFICATIONS = 4
JOB_COMPANY_NAME = 7
JOB_LOCATIONS = 9
JOB_DESCRIPTION = 10
JOB_CREATED_TS = 12
JOB_UPDATED_TS = 13
JOB_MIN_QUALIFICATIONS = 19

_XSSI_PREFIX = ")]}'"
_ENVELOPE_PREFIX = '[["wrb.fr"'

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


class GoogleJobsBlockedError(Exception):
    """Google refused the request."""


class GoogleJobsUnavailableError(Exception):
    """Google failed to answer (5xx), as distinct from refusing."""


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
        raise GoogleJobsBlockedError(f"Google declined the request ({resp.status_code}).")
    if resp.status_code >= 500:
        raise GoogleJobsUnavailableError(f"Google returned {resp.status_code}.")


def build_f_req(rpc: str, args: list) -> str:
    """Wrap RPC arguments in the ``f.req`` envelope batchexecute expects."""
    return json.dumps([[[rpc, json.dumps(args), None, "generic"]]])


def decode_response(raw: str):
    """Unwrap the XSSI guard and the doubly-encoded payload.

    Returns the decoded inner value, or None when the RPC answered with no
    payload — which is how this endpoint reports both "no results" and "your
    argument shape was wrong", so callers must not read None as "empty board".
    """
    for line in (raw or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith(_XSSI_PREFIX) or not stripped:
            continue
        if not stripped.startswith(_ENVELOPE_PREFIX):
            continue
        try:
            envelope = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        try:
            payload = envelope[0][2]
        except (IndexError, TypeError):
            continue
        if not payload:
            return None
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def build_search_args(
    *,
    query: str = "",
    locations: list[str] | None = None,
    page: int = 1,
    skills: str = "",
    remote_only: bool = False,
    sort: str = "",
    employment_types: list[str] | None = None,
    target_levels: list[str] | None = None,
) -> list:
    """Build the positional search argument list.

    Multi-value filters are arrays and behave as a union; comma-joining them
    into one string makes Google treat the whole thing as a single fuzzy value
    instead, which quietly returns unrelated results.
    """
    slots: list = [None] * _ARG_SLOTS
    slots[_ARG_QUERY] = query or None
    slots[_ARG_LOCALE] = "en-US"
    slots[_ARG_LOCATIONS] = [[loc] for loc in (locations or [])] or None
    slots[_ARG_PAGE] = max(1, page)
    if skills:
        slots[_ARG_SKILLS] = skills
    if remote_only:
        slots[_ARG_REMOTE] = 1
    if sort in SORT_CODES:
        slots[_ARG_SORT] = SORT_CODES[sort]
    if employment_types:
        codes = [EMPLOYMENT_CODES[t] for t in employment_types if t in EMPLOYMENT_CODES]
        slots[_ARG_EMPLOYMENT] = codes or None
    if target_levels:
        codes = [TARGET_LEVEL_CODES[t] for t in target_levels if t in TARGET_LEVEL_CODES]
        slots[_ARG_TARGET_LEVEL] = codes or None
    # The RPC takes the argument list wrapped in one more array.
    return [slots]


async def _call(session, rpc: str, args: list):
    await google_jobs_limiter.wait()
    resp = await session.post(
        f"{SITE}{_RPC_PATH}",
        form={"f.req": build_f_req(rpc, args)},
        headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    _check(resp)
    if resp.status_code != 200:
        return None
    return decode_response(resp.text)


async def search_page(session, **kwargs) -> tuple[list, int] | None:
    """One page. Returns ``(jobs, total)`` or None when the RPC gave no payload."""
    inner = await _call(session, SEARCH_RPC, build_search_args(**kwargs))
    if not isinstance(inner, list) or not inner:
        return None
    jobs = inner[0] if isinstance(inner[0], list) else []
    total = inner[2] if len(inner) > 2 and isinstance(inner[2], int) else len(jobs)
    return [j for j in jobs if isinstance(j, list)], total


async def search_all(
    session,
    *,
    query: str = "",
    locations: list[str] | None = None,
    limit: int = 25,
    **kwargs,
) -> tuple[list, int]:
    """Page until ``limit`` jobs are collected or Google runs out.

    Past the last page the RPC returns a null job list rather than an empty
    one, which is the same shape as a malformed request; pagination therefore
    stops on the first page that yields nothing.
    """
    collected: list = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        if len(collected) >= limit:
            break
        result = await search_page(
            session, query=query, locations=locations, page=page, **kwargs
        )
        if result is None:
            break
        jobs, page_total = result
        if page == 1:
            total = page_total
        if not jobs:
            break
        collected.extend(jobs)
        if len(jobs) < PAGE_SIZE:
            break
    return collected[:limit], total


async def fetch_job(session, job_id: str) -> list | None:
    """One posting by id. Returns the same 21-element record search returns."""
    inner = await _call(session, DETAIL_RPC, [str(job_id)])
    if not isinstance(inner, list) or not inner:
        return None
    record = inner[0]
    if isinstance(record, list) and record and isinstance(record[0], list):
        record = record[0]
    return record if isinstance(record, list) and len(record) > JOB_TITLE else None


def posting_url(job_id: str) -> str:
    """Public permalink. The slug is cosmetic — the id alone resolves."""
    return f"{SITE}{_BASE}/jobs/results/{job_id}"


def html_field(job: list, index: int) -> str:
    """Read one of the ``[null, "<html>"]`` body fields."""
    try:
        value = job[index]
    except (IndexError, TypeError):
        return ""
    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return value[1]
    return value if isinstance(value, str) else ""


def timestamp(job: list, index: int) -> str:
    """Format one of the protobuf-style ``[seconds, nanos]`` fields.

    Slot 12 is always <= slots 13/14, and all three are equal on postings that
    were never revised, so 12 reads as first posted and 13 as last updated.
    That is inferred from ordering, not from anything Google documents or
    displays, so it is only ever rendered as a date and never as a claim about
    application deadlines or freshness.
    """
    try:
        value = job[index]
    except (IndexError, TypeError):
        return ""
    if not isinstance(value, list) or not value or not isinstance(value[0], (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(value[0], UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def locations(job: list) -> list[str]:
    """Display strings for a posting's locations.

    Each entry is ``[display, [display], city, null, region, country_code]``.
    """
    try:
        entries = job[JOB_LOCATIONS]
    except (IndexError, TypeError):
        return []
    names: list[str] = []
    for entry in entries or []:
        if isinstance(entry, list) and entry and isinstance(entry[0], str):
            if entry[0] not in names:
                names.append(entry[0])
    return names
