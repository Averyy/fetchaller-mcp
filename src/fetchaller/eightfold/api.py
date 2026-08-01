"""Transport for Eightfold's anonymous career-site routes.

Eightfold runs two generations of career site and tenants are split across
both, so this module speaks each and picks per tenant at runtime:

``pcsx`` (Microsoft, PayPal)
    ``GET /api/pcsx/search`` — ``{domain, query, location, start, sort_by}``,
    returning ``{data: {positions[], count, filterDef{facets}}}``. Page size is
    fixed at 10; ``num``/``pageSize`` are ignored. ``count`` is the real total.
    ``GET /api/pcsx/position_details`` carries ``jobDescription``.

``classic`` (Netflix)
    ``GET /api/apply/v2/jobs`` — ``{domain, query, location, start, num}``,
    returning ``{positions[], count, facets}``. ``num`` is capped at 10 and
    ``count`` is only ``start + len(positions)``, so the grand total is not
    knowable without exhausting the pages. ``GET /api/apply/v2/jobs/{id}``
    carries ``job_description``.

Both answer anonymously — no cookie, CSRF token, or referer. A PCSX tenant
answers the classic route too, but with a config blob rather than postings, so
generation is decided by asking PCSX first and falling back on its explicit
403 ("PCSX is not enabled for this user").

Positions are normalised to the PCSX field names before leaving this module so
callers see one shape.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import eightfold_limiter

_PCSX_SEARCH_PATH = "/api/pcsx/search"
_PCSX_DETAIL_PATH = "/api/pcsx/position_details"
_CLASSIC_SEARCH_PATH = "/api/apply/v2/jobs"

PCSX = "pcsx"
CLASSIC = "classic"

# Descriptions are the largest thing returned and run well under a megabyte.
_MAX_RESPONSE = 8 * 1024 * 1024
# Both generations serve 10 per page regardless of what is requested.
PAGE_SIZE = 10
# Eightfold stops paginating well before this; it bounds a runaway loop.
MAX_PAGES = 20

_GROUP_ID_RE = re.compile(r"_EF_GROUP_ID\s*=\s*[\"']([^\"']{1,120})[\"']")
# The tenant tells us plainly when PCSX is off; both wordings have been seen.
_NOT_PCSX_RE = re.compile(r"PCSX", re.IGNORECASE)

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()
# Group id and generation are both stable per tenant for the life of a process.
_group_id_cache: dict[str, str] = {}
_generation_cache: dict[str, str] = {}


class EightfoldBlockedError(Exception):
    """The tenant refused the request."""


class EightfoldUnavailableError(Exception):
    """The tenant failed to answer (5xx), as distinct from refusing."""


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    """Shared session so one identity and cookie jar serve every tenant call."""
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
    _group_id_cache.clear()
    _generation_cache.clear()


def _check(resp, what: str) -> None:
    status = resp.status_code
    if status in (401, 429):
        raise EightfoldBlockedError(f"Eightfold declined the {what} request ({status}).")
    if status >= 500:
        raise EightfoldUnavailableError(f"Eightfold returned {status} for {what}.")


def _root(board_url: str) -> str | None:
    from .url import board_root

    return board_root(board_url)


async def discover_group_id(board_url: str, session) -> str | None:
    """Read ``window._EF_GROUP_ID`` off a tenant's board page.

    This is what lets an unseen vanity host work: the group id the API needs is
    on the page, so nothing has to be hardcoded per employer.
    """
    root = _root(board_url)
    if root is None:
        return None
    cached = _group_id_cache.get(root)
    if cached:
        return cached

    await eightfold_limiter.wait()
    try:
        resp = await session.get(board_url)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    match = _GROUP_ID_RE.search(resp.text)
    if not match:
        return None
    group_id = match.group(1)
    _group_id_cache[root] = group_id
    return group_id


# ---------------------------------------------------------------------------
# Normalisation — classic field names mapped onto the PCSX ones
# ---------------------------------------------------------------------------

_CLASSIC_FIELD_MAP = {
    "display_job_id": "displayJobId",
    "ats_job_id": "atsJobId",
    "t_create": "creationTs",
    "t_update": "postedTs",
    "work_location_option": "workLocationOption",
    "location_flexibility": "locationFlexibility",
    "job_description": "jobDescription",
    "canonicalPositionUrl": "publicUrl",
    "business_unit": "businessUnit",
}


def _normalize_classic(position: dict) -> dict:
    """Rename classic fields to their PCSX equivalents, keeping the rest."""
    out: dict = {}
    for key, value in position.items():
        out[_CLASSIC_FIELD_MAP.get(key, key)] = value
    # Classic writes "Vancouver,Canada" with no space after the comma.
    locations = out.get("locations")
    if isinstance(locations, list):
        out["locations"] = [
            ", ".join(part.strip() for part in str(loc).split(",") if part.strip())
            for loc in locations
        ]
    return out


async def detect_generation(board_url: str, *, session, group_id: str) -> str:
    """Return ``"pcsx"`` or ``"classic"`` for a tenant, caching the answer."""
    root = _root(board_url)
    if root is None:
        return PCSX
    cached = _generation_cache.get(root)
    if cached:
        return cached

    await eightfold_limiter.wait()
    resp = await session.get(
        root + _PCSX_SEARCH_PATH,
        params={"domain": group_id, "start": "0"},
        headers={"accept": "application/json", "referer": board_url},
    )
    # A 403 naming PCSX is the tenant saying it runs the classic site. Any
    # other 403 is a real refusal and must not be papered over as a fallback.
    if resp.status_code == 403:
        if _NOT_PCSX_RE.search(resp.text or ""):
            generation = CLASSIC
        else:
            raise EightfoldBlockedError("Eightfold declined the request (403).")
    elif resp.status_code == 200:
        generation = PCSX
    else:
        _check(resp, "generation probe")
        generation = CLASSIC

    _generation_cache[root] = generation
    return generation


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search_positions(
    board_url: str,
    *,
    session,
    group_id: str,
    generation: str,
    query: str = "",
    location: str = "",
    start: int = 0,
    sort_by: str = "relevance",
    include_remote: bool = True,
    distance_km: int | None = None,
) -> tuple[list[dict], int, dict] | None:
    """One page. Returns ``(positions, count, facets)`` or None on a bad body."""
    root = _root(board_url)
    if root is None:
        return None

    if generation == CLASSIC:
        path = _CLASSIC_SEARCH_PATH
        params = {
            "domain": group_id,
            "query": query or "",
            "location": location or "",
            "start": str(max(0, start)),
            "num": str(PAGE_SIZE),
            "sort_by": sort_by,
        }
    else:
        path = _PCSX_SEARCH_PATH
        params = {
            "domain": group_id,
            "query": query or "",
            "location": location or "",
            "start": str(max(0, start)),
            "sort_by": sort_by,
            "filter_include_remote": "1" if include_remote else "0",
        }
        if distance_km is not None:
            params["filter_distance"] = str(distance_km)

    await eightfold_limiter.wait()
    resp = await session.get(
        root + path,
        params=params,
        headers={"accept": "application/json", "referer": board_url},
    )
    _check(resp, "search")
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    if generation == CLASSIC:
        body = payload
        positions = [
            _normalize_classic(p) for p in (body.get("positions") or []) if isinstance(p, dict)
        ]
        facets = body.get("facets") or {}
        # Classic's `count` is only start + len(positions), never the total.
        return positions, 0, facets if isinstance(facets, dict) else {}

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    positions = [p for p in (data.get("positions") or []) if isinstance(p, dict)]
    filter_def = data.get("filterDef")
    facets = filter_def.get("facets") or {} if isinstance(filter_def, dict) else {}
    return positions, data.get("count") or 0, facets


async def search_all_positions(
    board_url: str,
    *,
    session,
    group_id: str,
    generation: str,
    query: str = "",
    location: str = "",
    limit: int = 25,
    sort_by: str = "relevance",
    include_remote: bool = True,
    distance_km: int | None = None,
) -> tuple[list[dict], int, dict]:
    """Page until ``limit`` positions are collected or the tenant runs out.

    Returns ``(positions, total, facets)``. ``total`` is the tenant's own count
    for the whole query on PCSX and 0 on classic, which does not report one.
    """
    collected: list[dict] = []
    total = 0
    facets: dict = {}

    for page in range(MAX_PAGES):
        if len(collected) >= limit:
            break
        result = await search_positions(
            board_url,
            session=session,
            group_id=group_id,
            generation=generation,
            query=query,
            location=location,
            start=page * PAGE_SIZE,
            sort_by=sort_by,
            include_remote=include_remote,
            distance_km=distance_km,
        )
        if result is None:
            break
        positions, count, page_facets = result
        if page == 0:
            total = count
            facets = page_facets
        if not positions:
            break
        collected.extend(positions)
        if len(positions) < PAGE_SIZE:
            break

    return collected[:limit], total, facets


async def fetch_position_details(
    board_url: str,
    position_id: str,
    *,
    session,
    group_id: str,
    generation: str,
    locale: str = "en",
) -> dict | None:
    """Full detail for one posting, including its description HTML."""
    root = _root(board_url)
    if root is None:
        return None

    await eightfold_limiter.wait()
    if generation == CLASSIC:
        resp = await session.get(
            f"{root}{_CLASSIC_SEARCH_PATH}/{position_id}",
            params={"domain": group_id},
            headers={"accept": "application/json", "referer": board_url},
        )
    else:
        resp = await session.get(
            root + _PCSX_DETAIL_PATH,
            params={"position_id": position_id, "domain": group_id, "hl": locale},
            headers={"accept": "application/json", "referer": board_url},
        )
    _check(resp, "position detail")
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    if generation == CLASSIC:
        return _normalize_classic(payload) if payload.get("name") or payload.get("id") else None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    # Tenants differ on whether the record is nested under `position`.
    position = data.get("position")
    if isinstance(position, dict):
        return position
    return data if data.get("name") or data.get("id") else None
