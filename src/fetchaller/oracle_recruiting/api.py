"""Transport for Oracle Recruiting Cloud's anonymous candidate-experience API."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from urllib.parse import quote

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import oracle_recruiting_limiter

_REST_BASE = "/hcmRestApi/resources/latest"
_SEARCH_RESOURCE = "recruitingCEJobRequisitions"
_DETAIL_RESOURCE = "recruitingCEJobRequisitionDetails"

# Nearly every ORC deployment publishes its external site as CX_1, and the
# number is not exposed in page markup, so it is a default rather than a
# discovered value. Callers can override it per employer.
DEFAULT_SITE_NUMBER = "CX_1"

_MAX_RESPONSE = 12 * 1024 * 1024
# The resource caps a page well below this; it bounds a runaway loop.
PAGE_SIZE = 200
MAX_PAGES = 10

# https://{tenant}.fa.{region}.oraclecloud.com
_FUSION_HOST_RE = re.compile(
    r"https://[a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com", re.IGNORECASE
)

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()
_host_cache: dict[str, str] = {}


class OracleRecruitingBlockedError(Exception):
    """The tenant refused the request."""


class OracleRecruitingUnavailableError(Exception):
    """The tenant failed to answer (5xx), as distinct from refusing."""


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
    _host_cache.clear()


def _check(resp, what: str) -> None:
    if resp.status_code in (401, 403, 429):
        raise OracleRecruitingBlockedError(
            f"Oracle Recruiting declined the {what} request ({resp.status_code})."
        )
    if resp.status_code >= 500:
        raise OracleRecruitingUnavailableError(
            f"Oracle Recruiting returned {resp.status_code} for {what}."
        )


async def discover_host(careers_url: str, session) -> str | None:
    """Find a tenant's Fusion host by reading its careers page.

    The Fusion hostname is deployment-controlled (``iaziqy.fa.ocs`` for Uber
    today) and can change, so it is read from the employer's own careers page
    rather than pinned here. The page links the ORC host directly.
    """
    cached = _host_cache.get(careers_url)
    if cached:
        return cached
    await oracle_recruiting_limiter.wait()
    try:
        resp = await session.get(careers_url, headers={"accept": "text/html"})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    match = _FUSION_HOST_RE.search(resp.text)
    if not match:
        return None
    host = match.group(0)
    _host_cache[careers_url] = host
    return host


def _finder(site_number: str, *, limit: int, offset: int, keyword: str, location: str) -> str:
    """Build the ``findReqs`` finder string.

    Oracle's finder syntax is ``name;key=value,key=value`` with string values
    quoted, and it must NOT be URL-encoded as a whole — only the values are.
    """
    parts = [f"siteNumber={site_number}", f"limit={limit}"]
    if offset:
        parts.append(f"offset={offset}")
    if keyword:
        parts.append(f'keyword="{quote(keyword, safe="")}"')
    if location:
        parts.append(f'location="{quote(location, safe="")}"')
    return "findReqs;" + ",".join(parts)


async def search_requisitions(
    host: str,
    *,
    session,
    site_number: str = DEFAULT_SITE_NUMBER,
    keyword: str = "",
    location: str = "",
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[dict], int] | None:
    """One page. Returns ``(requisitions, total)`` or None on an unusable body.

    ``expand=requisitionList`` is mandatory: without it the response still
    returns HTTP 200 and a correct ``TotalJobsCount``, but the postings array
    is absent entirely — a silent empty result rather than an error.
    """
    url = (
        f"{host}{_REST_BASE}/{_SEARCH_RESOURCE}"
        f"?onlyData=true&expand=requisitionList"
        f"&finder={_finder(site_number, limit=limit, offset=offset, keyword=keyword, location=location)}"
    )
    await oracle_recruiting_limiter.wait()
    resp = await session.get(url, headers={"accept": "application/json"})
    _check(resp, "search")
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    search = items[0]
    requisitions = [r for r in (search.get("requisitionList") or []) if isinstance(r, dict)]
    total = search.get("TotalJobsCount")
    return requisitions, int(total) if isinstance(total, (int, float)) else len(requisitions)


async def search_all_requisitions(
    host: str,
    *,
    session,
    site_number: str = DEFAULT_SITE_NUMBER,
    keyword: str = "",
    location: str = "",
    limit: int = 200,
) -> tuple[list[dict], int]:
    """Page until ``limit`` requisitions are collected or the tenant runs out."""
    collected: list[dict] = []
    total = 0
    for page in range(MAX_PAGES):
        if len(collected) >= limit:
            break
        result = await search_requisitions(
            host,
            session=session,
            site_number=site_number,
            keyword=keyword,
            location=location,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )
        if result is None:
            break
        requisitions, page_total = result
        if page == 0:
            total = page_total
        if not requisitions:
            break
        collected.extend(requisitions)
        if len(requisitions) < PAGE_SIZE:
            break
    return collected[:limit], total


async def fetch_requisition(
    host: str,
    requisition_id: str,
    *,
    session,
    site_number: str = DEFAULT_SITE_NUMBER,
) -> dict | None:
    """Full detail for one requisition, description included."""
    url = (
        f"{host}{_REST_BASE}/{_DETAIL_RESOURCE}"
        f'?expand=all&onlyData=true&finder=ById;Id="{quote(str(requisition_id), safe="")}"'
        f",siteNumber={site_number}"
    )
    await oracle_recruiting_limiter.wait()
    resp = await session.get(url, headers={"accept": "application/json"})
    _check(resp, "requisition detail")
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    return items[0]
