"""Transport for LinkedIn's logged-out guest job endpoints.

Three public endpoints, all verified live from a logged-out session:

- ``/jobs-guest/jobs/api/seeMoreJobPostings/search`` — HTML fragment of ``<li>``
  cards, at most 10 per response, offset by an absolute ``start`` row.
- ``/jobs-guest/api/typeaheadHits?typeaheadType=GEO`` — JSON array resolving a
  human location to a ``geoId``. Note the path: the ``/jobs-guest/jobs/api/``
  variant of it returns 404.
- ``/jobs-guest/jobs/api/jobPosting/{id}`` — HTML fragment for one posting.

wafer owns challenge handling as everywhere else in this repo; this module only
issues requests and hands bodies to the parser.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from urllib.parse import urlencode

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import linkedin_limiter

SITE = "https://www.linkedin.com"
_SEARCH_PATH = "/jobs-guest/jobs/api/seeMoreJobPostings/search"
# The public JSERP page carries its own card list — 60 of them, six times what
# the fragment endpoint returns per call. It ignores `start` (every offset
# returns the same first card), so it is a one-shot first-page surface, not a
# pagination route. Used for start=0; the fragment endpoint handles the rest.
_PAGE_PATH = "/jobs/search"
PAGE_CARD_CAPACITY = 60
_TYPEAHEAD_PATH = "/jobs-guest/api/typeaheadHits"
_DETAIL_PATH = "/jobs-guest/jobs/api/jobPosting"

# The guest fragments are small; a posting description is the largest thing
# returned and is nowhere near this.
_MAX_RESPONSE = 5 * 1024 * 1024
_PAGE_SIZE = 10
# `start` is an absolute row offset. 999 is the last row that answers; 1000 and
# above return HTTP 400 with an empty body.
MAX_START = 999

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    """Shared session so one identity and one cookie jar serve every call."""
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    timeout=timedelta(seconds=60),
                    # Measured safe operating point: no 403/429/challenge across
                    # 46 probes at this spacing. The blocking threshold was
                    # deliberately never probed, so this is a floor to stay
                    # under, not a limit to approach.
                    rate_limit=3.2,
                    rate_jitter=0.2,
                    # Stop rather than rotate. wafer defaults to 3 retries and
                    # 2 rotations; against a rate limit that is six requests
                    # under three identities when the honest answer is to back
                    # off. LinkedIn is answering us politely — keep it that way.
                    max_retries=0,
                    max_rotations=0,
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=_MAX_RESPONSE,
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


class LinkedInBlockedError(Exception):
    """LinkedIn refused the request. Never retried or rotated around."""


class LinkedInUnavailableError(Exception):
    """LinkedIn failed to answer (5xx). Distinct from a refusal."""


async def _get(url: str, session, timeout: float | None):
    await linkedin_limiter.wait()
    kwargs: dict = {}
    if timeout is not None and timeout > 0:
        kwargs["timeout"] = timedelta(seconds=timeout)
    return await session.get(url, **kwargs)


def build_search_url(params: dict[str, object]) -> str:
    """Search URL from already-validated parameters (empty values omitted)."""
    query = {key: value for key, value in params.items() if value not in (None, "", ())}
    return f"{SITE}{_SEARCH_PATH}?{urlencode(query)}"


async def fetch_search_fragment(
    params: dict[str, object],
    *,
    session,
    timeout: float | None = None,
) -> str:
    """One page of search results as an HTML fragment.

    A 400 means the caller walked past the last reachable row; that is the end
    of the result set, not an error, so it returns an empty fragment.
    """
    resp = await _get(build_search_url(params), session, timeout)
    if resp.status_code == 400:
        return ""
    if resp.status_code in (401, 403, 429):
        raise LinkedInBlockedError(f"LinkedIn returned HTTP {resp.status_code}")
    if resp.status_code != 200:
        # A 5xx is LinkedIn failing, not refusing. Naming it a block would send
        # the caller to back off for a reason that does not apply.
        raise LinkedInUnavailableError(f"LinkedIn returned HTTP {resp.status_code}")
    return resp.text


def build_page_url(params: dict[str, object]) -> str:
    """Public JSERP page URL for the same filters as the fragment endpoint."""
    query = {
        key: value
        for key, value in params.items()
        if value not in (None, "", ()) and key != "start"
    }
    return f"{SITE}{_PAGE_PATH}?{urlencode(query)}"


async def fetch_search_page(
    params: dict[str, object],
    *,
    session,
    timeout: float | None = None,
) -> str:
    """First page of results from the JSERP page (up to 60 cards, one request).

    Returns an empty string on anything unexpected so the caller can fall back
    to the fragment endpoint rather than fail.
    """
    try:
        resp = await _get(build_page_url(params), session, timeout)
    except LinkedInBlockedError:
        raise
    except Exception:
        return ""
    if resp.status_code in (401, 403, 429):
        raise LinkedInBlockedError(f"LinkedIn returned HTTP {resp.status_code}")
    if resp.status_code != 200:
        return ""
    return resp.text


async def fetch_job_detail(
    job_id: str,
    *,
    session,
    timeout: float | None = None,
) -> str | None:
    """One posting's detail fragment, or None if it is gone."""
    resp = await _get(f"{SITE}{_DETAIL_PATH}/{job_id}", session, timeout)
    if resp.status_code == 404:
        return None
    if resp.status_code in (401, 403, 429):
        raise LinkedInBlockedError(f"LinkedIn returned HTTP {resp.status_code}")
    if resp.status_code >= 500:
        raise LinkedInUnavailableError(f"LinkedIn returned HTTP {resp.status_code}")
    if resp.status_code != 200:
        return None
    return resp.text


async def resolve_geo_id(
    location: str,
    *,
    session,
    timeout: float | None = None,
) -> str | None:
    """Resolve a human location to a geoId via the public GEO typeahead.

    Prefers an exact case-insensitive ``displayName`` match, then the first hit.
    Returns None on anything unexpected — the caller falls back to sending the
    plain ``location`` string, which LinkedIn also accepts.
    """
    if not location.strip():
        return None
    url = f"{SITE}{_TYPEAHEAD_PATH}?" + urlencode(
        {"typeaheadType": "GEO", "query": location}
    )
    try:
        resp = await _get(url, session, timeout)
    except LinkedInBlockedError:
        raise
    except Exception:
        return None
    # A refusal here is a refusal for the whole flow. Returning None would fall
    # through to a plain-location search and issue MORE requests against a host
    # that just told us to stop — the opposite of backing off.
    if resp.status_code in (401, 403, 429):
        raise LinkedInBlockedError(f"LinkedIn returned HTTP {resp.status_code}")
    if resp.status_code != 200:
        return None
    try:
        # Served as text/plain despite being JSON.
        hits = json.loads(resp.text)
    except (ValueError, TypeError):
        return None
    if not isinstance(hits, list):
        return None

    wanted = location.strip().casefold()
    fallback: str | None = None
    for hit in hits[:25]:
        if not isinstance(hit, dict) or hit.get("type") != "GEO":
            continue
        hit_id = hit.get("id")
        if not isinstance(hit_id, str) or not hit_id.isdigit():
            continue
        display = hit.get("displayName")
        if isinstance(display, str) and display.strip().casefold() == wanted:
            return hit_id
        if fallback is None:
            fallback = hit_id
    return fallback


def page_size() -> int:
    return _PAGE_SIZE
