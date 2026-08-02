"""Transport for metacareers.com's anonymous GraphQL."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import meta_careers_limiter

SITE = "https://www.metacareers.com"
_GRAPHQL_PATH = "/graphql"
_JOBS_PATH = "/jobs"

# Operation names, with the doc_id last seen working. These are a starting
# guess only: Meta rotates them on deploy, so a miss triggers rediscovery from
# the JS bundles rather than a failure.
SEARCH_OPERATION = "CareersJobSearchResultsV2DataQuery"
FILTERS_OPERATION = "CareersJobSearchFiltersV3Query"
LOCATIONS_OPERATION = "CareersJobSearchLocationFilterV3Query"
_KNOWN_DOC_IDS = {
    SEARCH_OPERATION: "27129360303422352",
    FILTERS_OPERATION: "25103492705924273",
    LOCATIONS_OPERATION: "24867916029505828",
}

# Meta answers a throttled GraphQL search with HTTP 200 and this code.
_RATE_LIMIT_CODES = frozenset({1675004})
# Long enough to actually clear. A throttle that is merely paused around gets
# re-tripped, and Meta's persists far longer than a normal backoff.
_RATE_LIMIT_BACKOFF_SECONDS = 120.0

_MAX_RESPONSE = 12 * 1024 * 1024
_LSD_RE = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"\}')
_BUNDLE_RE = re.compile(r'https://static\.xx\.fbcdn\.net/rsrc\.php/[^"\\\s]+\.js')
# __d("CareersJobSearchResultsV2DataQuery_candidate_portalRelayOperation",[],
#   (function(...){a.exports="27129360303422352"
_DOC_ID_TEMPLATE = (
    r'__d\("{operation}_[A-Za-z_]*[Rr]elayOperation".{{0,400}}?exports\s*=\s*"(\d{{15,25}})"'
)

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()
_lsd_cache: str | None = None
_warmed = False
_doc_id_cache: dict[str, str] = {}


class MetaCareersBlockedError(Exception):
    """Meta refused the request."""


class MetaCareersUnavailableError(Exception):
    """Meta failed to answer (5xx), as distinct from refusing."""


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
    global _session, _lsd_cache, _warmed
    _session = None
    _lsd_cache = None
    _warmed = False
    _doc_id_cache.clear()


def _check(resp, what: str) -> None:
    if resp.status_code == 429:
        # Hold every caller off this host, not just this one, and honour the
        # server's own number when it gives one. Without this the limiter keeps
        # letting requests through at its base interval into an active throttle.
        wait = float(getattr(resp, "retry_after", None) or _RATE_LIMIT_BACKOFF_SECONDS)
        meta_careers_limiter.defer(wait)
        raise MetaCareersBlockedError(
            f"Meta rate-limited the {what} request; backing off {wait:.0f}s."
        )
    if resp.status_code in (401, 403):
        raise MetaCareersBlockedError(f"Meta declined the {what} request ({resp.status_code}).")
    if resp.status_code >= 500:
        raise MetaCareersUnavailableError(f"Meta returned {resp.status_code} for {what}.")


async def _warm_origin(session) -> None:
    """Visit the site root once per session before asking for the board page.

    Meta throttles per path, and a cold session arriving straight at ``/jobs``
    is the pattern it throttles: measured directly, ``/`` answered 200 while
    ``/jobs?q=…`` raised ``RateLimited`` on the same session, and the block
    persisted for hours. One root fetch earns the cookies that make the board
    request read as a continuation rather than a first contact.
    """
    global _warmed
    if _warmed:
        return
    _warmed = True
    with contextlib.suppress(Exception):
        await meta_careers_limiter.wait()
        await session.get(SITE + "/", headers={"accept": "text/html"})


async def _fetch_jobs_page(session) -> str:
    await _warm_origin(session)
    await meta_careers_limiter.wait()
    resp = await session.get(SITE + _JOBS_PATH, headers={"accept": "text/html"})
    _check(resp, "board page")
    return resp.text if resp.status_code == 200 else ""


async def get_lsd(session, *, refresh: bool = False) -> str | None:
    """The per-page CSRF token GraphQL requires."""
    global _lsd_cache
    if _lsd_cache and not refresh:
        return _lsd_cache
    html = await _fetch_jobs_page(session)
    match = _LSD_RE.search(html or "")
    if not match:
        return None
    _lsd_cache = match.group(1)
    return _lsd_cache


async def discover_doc_id(session, operation: str) -> str | None:
    """Find an operation's persisted-query id in Meta's JS bundles.

    Meta publishes each one as a tiny module whose only export is the id, so
    the bundles are scanned rather than the id being pinned in this repo.
    """
    html = await _fetch_jobs_page(session)
    if not html:
        return None
    pattern = re.compile(_DOC_ID_TEMPLATE.format(operation=re.escape(operation)), re.DOTALL)
    for bundle in dict.fromkeys(_BUNDLE_RE.findall(html)):
        await meta_careers_limiter.wait()
        try:
            resp = await session.get(bundle, headers={"accept": "*/*"})
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        match = pattern.search(resp.text)
        if match:
            doc_id = match.group(1)
            _doc_id_cache[operation] = doc_id
            return doc_id
    return None


def _parse_graphql(text: str) -> dict | None:
    # Comet streams responses as newline-delimited JSON; the first line is the
    # complete payload for these queries.
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def graphql(
    session,
    operation: str,
    variables: dict,
    *,
    allow_rediscovery: bool = True,
) -> dict | None:
    """Run a persisted query, rediscovering its ``doc_id`` if the known one fails."""
    lsd = await get_lsd(session)
    if not lsd:
        return None
    doc_id = _doc_id_cache.get(operation) or _KNOWN_DOC_IDS.get(operation)
    if not doc_id:
        doc_id = await discover_doc_id(session, operation)
        if not doc_id:
            return None

    async def call(current_doc_id: str):
        await meta_careers_limiter.wait()
        resp = await session.post(
            SITE + _GRAPHQL_PATH,
            form={
                "lsd": lsd,
                "__a": "1",
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": operation,
                "server_timestamps": "true",
                "variables": json.dumps(variables),
                "doc_id": current_doc_id,
            },
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "*/*",
                "origin": SITE,
                "referer": SITE + _JOBS_PATH,
                "x-fb-lsd": lsd,
            },
        )
        _check(resp, operation)
        if resp.status_code != 200:
            return None
        return _parse_graphql(resp.text)

    payload = await call(doc_id)
    if _usable(payload):
        _doc_id_cache[operation] = doc_id
        return payload

    # Being throttled must never trigger rediscovery. A rate-limit reply and a
    # rotated-doc_id reply look identical to _usable() — both carry `errors` and
    # no `data` — but rediscovery fetches the board page *and walks every JS
    # bundle*, so answering a "slow down" with a bundle scan is precisely the
    # amplification that turns one throttle into a lasting one. Back off instead.
    if _rate_limited(payload):
        meta_careers_limiter.defer(_RATE_LIMIT_BACKOFF_SECONDS)
        raise MetaCareersBlockedError(
            f"Meta rate-limited the {operation} request; backing off "
            f"{_RATE_LIMIT_BACKOFF_SECONDS:.0f}s. The query is fine — retry shortly."
        )

    # A rotated doc_id answers with an error rather than data. Rediscover once.
    if allow_rediscovery:
        fresh = await discover_doc_id(session, operation)
        if fresh and fresh != doc_id:
            payload = await call(fresh)
            if _usable(payload):
                return payload
    return payload


def _usable(payload) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("data"), dict) and payload["data"]


def _rate_limited(payload) -> bool:
    """Whether a GraphQL reply means "slow down" rather than "wrong query".

    Meta answers a throttled search with ``HTTP 200`` and::

        {"errors":[{"message":"Rate limit exceeded","code":1675004}], ...}

    Indistinguishable from a rotated ``doc_id`` unless the error is read.
    """
    if not isinstance(payload, dict):
        return False
    for error in payload.get("errors") or ():
        if not isinstance(error, dict):
            continue
        if error.get("code") in _RATE_LIMIT_CODES:
            return True
        message = str(error.get("message") or "").casefold()
        if "rate limit" in message or "too many requests" in message:
            return True
    return False


def build_search_input(
    *,
    query: str = "",
    offices: list[str] | None = None,
    teams: list[str] | None = None,
    divisions: list[str] | None = None,
    sub_teams: list[str] | None = None,
    is_remote_only: bool = False,
    sort_by_new: bool = False,
) -> dict:
    """The ``search_input`` shape the board's own client sends."""
    return {
        "q": query or "",
        "divisions": divisions or [],
        "offices": offices or [],
        "roles": [],
        "leadership_levels": [],
        "saved_jobs": [],
        "saved_searches": [],
        "sub_teams": sub_teams or [],
        "teams": teams or [],
        "is_leadership": False,
        "is_remote_only": is_remote_only,
        "sort_by_new": sort_by_new,
        "results_per_page": None,
    }


async def search_jobs(session, search_input: dict) -> tuple[list[dict], list[dict]]:
    """Return ``(all_jobs, featured_jobs)`` for a search input."""
    payload = await graphql(
        session,
        SEARCH_OPERATION,
        {"search_input": search_input, "viewasUserID": None, "isLoggedIn": False},
    )
    if not _usable(payload):
        return [], []
    node = payload["data"].get("job_search_with_featured_jobs_v2")
    if not isinstance(node, dict):
        # Older deploys returned a flat `job_search` list.
        flat = payload["data"].get("job_search")
        return ([j for j in flat if isinstance(j, dict)], []) if isinstance(flat, list) else ([], [])
    all_jobs = [j for j in (node.get("all_jobs") or []) if isinstance(j, dict)]
    featured = [j for j in (node.get("featured_jobs") or []) if isinstance(j, dict)]
    return all_jobs, featured


async def fetch_offices(session) -> list[str]:
    """Every office name the board will accept in ``search_input.offices``.

    The filter query returns entries shaped
    ``{id, location_display_name, is_remote, state, country}``, and it is the
    *display name* ("Vancouver, Canada") that ``search_input.offices`` matches
    — passing the ``id`` ("vancouver") filters nothing and Meta answers with
    the unfiltered board rather than an error.
    """
    payload = await graphql(session, LOCATIONS_OPERATION, {})
    if not _usable(payload):
        return []
    names: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            display = node.get("location_display_name")
            if isinstance(display, str) and display:
                names.append(display)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload["data"].get("job_search_filters"))
    return list(dict.fromkeys(names))


_JOB_DESCRIPTION_KEY = '"xcp_requisition_job_description"'


def _extract_json_object(text: str, start: int) -> dict | None:
    """Read one balanced JSON object out of an embedded blob."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


_JSON_LD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)


def _parse_job_posting_ld(html: str) -> dict | None:
    """Pull the schema.org ``JobPosting`` object out of a posting page.

    Meta publishes this for search engines, which makes it a standards-based
    and considerably less build-coupled surface than the internal
    ``xcp_requisition_job_description`` blob. It does not carry teams or
    compensation, so the internal object is still read for those.
    """
    for match in _JSON_LD_RE.finditer(html):
        try:
            parsed = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    return None


def _locations_from_ld(posting: dict) -> list[str]:
    raw = posting.get("jobLocation")
    entries = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, dict):
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            name = ", ".join(str(p) for p in parts if p)
        else:
            name = str(entry.get("name") or "")
        if name and name not in names:
            names.append(name)
    return names


async def fetch_job_detail(session, job_id: str) -> dict | None:
    """One posting from its canonical page.

    Two surfaces are merged, both on the same request: the schema.org
    ``JobPosting`` JSON-LD Meta publishes for search engines, and the internal
    ``xcp_requisition_job_description`` object. The JSON-LD is preferred for
    the standard fields because it is SEO-facing and therefore far less likely
    to move with a deploy; the internal object supplies teams, sub-teams, and
    compensation, which JSON-LD does not carry.
    """
    await meta_careers_limiter.wait()
    resp = await session.get(
        f"{SITE}/profile/job_details/{job_id}/", headers={"accept": "text/html"}
    )
    if resp.status_code == 404:
        # Older permalink shape.
        await meta_careers_limiter.wait()
        resp = await session.get(f"{SITE}/jobs/{job_id}/", headers={"accept": "text/html"})
    _check(resp, "job detail")
    if resp.status_code != 200:
        return None
    html = resp.text

    internal: dict = {}
    marker = html.find(_JOB_DESCRIPTION_KEY)
    if marker >= 0:
        brace = html.find("{", marker + len(_JOB_DESCRIPTION_KEY))
        if brace >= 0:
            internal = _extract_json_object(html, brace) or {}

    posting = _parse_job_posting_ld(html)
    if posting is None:
        return internal or None

    merged = dict(internal)
    merged.setdefault("id", job_id)
    if posting.get("title"):
        merged["title"] = posting["title"]
    if not merged.get("locations"):
        merged["locations"] = _locations_from_ld(posting)
    for source, target in (
        ("description", "job_description"),
        ("responsibilities", "responsibilities"),
        ("qualifications", "qualifications"),
    ):
        if posting.get(source) and not merged.get(target):
            merged[target] = posting[source]
    for key in ("datePosted", "employmentType", "validThrough"):
        if posting.get(key):
            merged[key] = posting[key]
    return merged
