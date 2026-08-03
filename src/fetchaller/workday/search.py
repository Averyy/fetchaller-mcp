"""Public entry point: ``search_workday_jobs``."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from urllib.parse import urlparse

import wafer

from ..config import get_wafer_cache_dir
from ..content.workday import (
    extract_workday_board_params,
    fetch_workday_facets,
    fetch_workday_job,
    flatten_facets,
    resolve_location_facet,
    search_workday_board,
)
from ..jobfilter import (
    broadened_query,
    counts_line,
    filter_by_title,
    location_matches,
    tokens,
)
from .employers import KNOWN_EMPLOYERS, resolve_employer

_MAX_RESPONSE = 10 * 1024 * 1024
# A located board is usually small; pulling the whole slice makes the
# client-side title filter exact rather than a filter over page one.
_FETCH_CEILING = 200
_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})


def _clean(value, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


# Workday's list API summarises a multi-location posting instead of listing
# it: "11 Locations", or the first place plus ", More...". Geo eligibility is
# the screen that decides whether a posting is worth reading at all, so a
# summary is the one thing a listing must not leave the caller to discover by
# opening every req. The full list is on the detail endpoint as
# `additionalLocations`; nothing in the list response carries it.
_SUMMARY_LOCATION_RE = re.compile(r"^\d+\s+Locations?$|,\s*More\.\.\.$")
# Detail lookups are one request each, so they run only for the postings that
# need them, concurrently, and only ever this many.
_EXPAND_CAP = 25
_EXPAND_CONCURRENCY = 6
_LOCATIONS_SHOWN = 8


def _is_location_summary(posting: dict) -> bool:
    return bool(_SUMMARY_LOCATION_RE.search((posting.get("locationsText") or "").strip()))


async def _expand_locations(postings: list[dict], board_url: str, session) -> None:
    """Replace summarised location text with the posting's real location list.

    Mutates in place, adding ``allLocations``. Any posting whose detail fetch
    fails keeps its summary — a missing expansion degrades the display, and
    must never remove a posting from the result.
    """
    targets = [p for p in postings if _is_location_summary(p)][:_EXPAND_CAP]
    if not targets:
        return
    semaphore = asyncio.Semaphore(_EXPAND_CONCURRENCY)

    async def one(posting: dict) -> None:
        url = _posting_url(board_url, posting)
        if not url:
            return
        async with semaphore:
            data = await fetch_workday_job(url, session)
        info = (data or {}).get("jobPostingInfo") or {}
        places = [info.get("location"), *(info.get("additionalLocations") or [])]
        clean = [p.strip() for p in places if isinstance(p, str) and p.strip()]
        if clean:
            posting["allLocations"] = clean

    await asyncio.gather(*(one(p) for p in targets), return_exceptions=True)


def _locations_of(posting: dict, wanted: list[str] | None = None) -> str:
    """Every location the caller can be screened on, or the board's summary.

    Places matching the requested location come first. Motorola's 60-location
    "US REMOTE" req is genuinely open to Alberta, BC, Ontario and Quebec; in
    board order the first eight are US states and the Canadian ones fall behind
    "+52 more", which answers a Canada search by hiding the answer.
    """
    places = posting.get("allLocations")
    if not places:
        return _clean(posting.get("locationsText"))
    if wanted:
        matched = [p for p in places if location_matches(p, wanted)]
        places = matched + [p for p in places if p not in matched]
    shown = [_clean(p, 80) for p in places[:_LOCATIONS_SHOWN]]
    text = " · ".join(shown)
    if len(places) > _LOCATIONS_SHOWN:
        text += f" · +{len(places) - _LOCATIONS_SHOWN} more"
    return text


def _req_id(posting: dict) -> str:
    """The requisition id alone.

    ``bulletFields`` is a tenant-configured list of list-view columns, not an
    id field. Motorola puts the location code first and the req second, so
    joining them produced "British Columbia Remote Work, R65471".

    ``externalPath`` ends in the requisition — ``..._R65471``,
    ``..._26WD97217-2`` — so the req is the entry matching that final segment.
    Matching the whole path instead would be too loose: a one-word location
    like "Remote" appears in ``/job/Remote/Engineer_R123`` as readily as the id
    does. A tenant whose id is not in the path falls back to every column,
    which is the previous behaviour.
    """
    bullets = posting.get("bulletFields")
    if not isinstance(bullets, list):
        return ""
    values = [str(b).strip() for b in bullets if isinstance(b, (str, int)) and str(b).strip()]
    if not values:
        return ""
    external = posting.get("externalPath") or ""
    # Workday appends "-1", "-2" to the path when a req is posted more than
    # once; the id itself keeps no such suffix.
    tail = external.rsplit("_", 1)[-1] if "_" in external else ""
    embedded = [v for v in values if tail and (v == tail or tail.startswith(f"{v}-"))]
    return _clean(", ".join(embedded or values))


async def _diagnose_board(board_url: str, session) -> str:
    """Say which failure it was.

    Every path in ``search_workday_board`` returns ``None``, so a gated board,
    a wrong site id and a wrong host all read as "did not answer" — three
    different problems, only one of which the caller can fix. Measured:
    Intuit answers 401, Visa 422, Thomson Reuters 404. One request, and only
    on the failure path.
    """
    generic = f"Workday board {board_url} did not answer."
    params = extract_workday_board_params(board_url)
    if not params:
        return (
            f"{board_url} is not a Workday board URL. Expected "
            "https://{tenant}.wd{N}.myworkdayjobs.com/{site}."
        )
    tenant, _lang, site = params
    api = f"https://{urlparse(board_url).netloc}/wday/cxs/{tenant}/{site}/jobs"
    try:
        resp = await session.post(
            api,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"accept": "application/json", "content-type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - the type is the diagnosis
        return (
            f"Could not reach {urlparse(board_url).netloc} ({type(exc).__name__}). "
            "Check the tenant host — Workday numbers them wd1 through wd103 and "
            "the number is part of the hostname."
        )
    status = resp.status_code
    if status in (401, 403):
        return (
            f"Workday board {board_url} requires a login (HTTP {status}). "
            "This tenant does not publish its jobs anonymously, so there is no "
            "public listing to read."
        )
    if status == 404:
        return (
            f"No Workday board at {board_url} (HTTP 404). Either the host is "
            f"wrong or '{tenant}' publishes under a different wd number."
        )
    if status == 422:
        return (
            f"Workday rejected the site id '{site}' on tenant '{tenant}' "
            f"(HTTP 422). The host is right; the path segment after it is not — "
            "check the board's own URL for the exact spelling."
        )
    if status >= 500:
        return f"Workday tenant '{tenant}' returned HTTP {status}. Try again shortly."
    return f"{generic} (HTTP {status})"


def _posting_url(board_url: str, posting: dict) -> str:
    external = (posting.get("externalPath") or "").strip()
    if not external:
        return ""
    parsed = urlparse(board_url)
    base = f"{parsed.scheme}://{parsed.netloc}{(parsed.path or '').rstrip('/')}"
    return f"{base}{external}"


def _render(
    postings: list[dict],
    *,
    board_url: str,
    employer: str,
    title: str,
    location: str,
    total: int,
    dropped: int,
    location_applied: bool,
    location_hint: str,
    truncated_by_limit: int = 0,
    examined: int = 0,
) -> str:
    scope = " · ".join(p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p)
    lines = [f"# {_clean(employer)} jobs{': ' + scope if scope else ''}", ""]

    lines.extend(
        counts_line(
            len(postings),
            dropped_by_title=dropped,
            board_total=total,
            board_label="This board",
            # With a facet applied, `total` counts the located slice, not the board.
            board_scope=f"in {_clean(location)}" if location_applied and location else "",
            truncated_by_limit=truncated_by_limit,
            examined=examined,
        )
    )
    if location and not location_applied:
        lines.append("")
        lines.append(
            f"_No location facet on this board matched “{_clean(location)}”, "
            "so the results are not location-filtered._"
        )
    lines.append("")

    if not postings:
        lines.append("No postings matched.")
        if location_hint:
            lines.append("")
            lines.append(f"Location values this board uses: {location_hint}")
        return "\n".join(lines) + "\n"

    for index, posting in enumerate(postings, start=1):
        name = _clean(posting.get("title")) or "(untitled)"
        url = _posting_url(board_url, posting)
        lines.append(f"## {index}. [{name}]({url})" if url else f"## {index}. {name}")
        where = _locations_of(posting, tokens(location) if location else None)
        if where:
            lines.append(f"- **Location**: {where}")
        posted = _clean(posting.get("postedOn"))
        if posted:
            lines.append(f"- **Posted**: {posted}")
        req = _req_id(posting)
        if req:
            lines.append(f"- **Req ID**: {req}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _location_values(facets, limit: int = 14) -> str:
    """Sample real location values so a caller can retry with a spelling that exists.

    Prefers the richest location facet: a caller who asked for a city is not
    helped by being shown a list of countries.
    """
    candidates = [f for f in flatten_facets(facets) if f["isLocation"] and f["values"]]
    if not candidates:
        return ""
    richest = max(candidates, key=lambda f: len(f["values"]))
    names = [_clean(v.get("descriptor")) for v in richest["values"][:limit]]
    return ", ".join(n for n in names if n)


async def search_workday_jobs(
    employer: str,
    *,
    title: str = "",
    location: str = "",
    strict_title: bool = True,
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search one Workday board, filtered by location and title.

    ``employer`` is an alias (``adobe``, ``nvidia``, ``salesforce``, ...) or any
    ``*.myworkdayjobs.com`` board URL.
    """
    board_url = resolve_employer(employer)
    if board_url is None:
        known = ", ".join(sorted(KNOWN_EMPLOYERS))
        return {
            "error": (
                f"'{employer}' is not a recognised Workday board. Pass one of "
                f"{known}, or the board URL "
                "(e.g. https://acme.wd1.myworkdayjobs.com/External)."
            )
        }
    if extract_workday_board_params(board_url) is None:
        return {
            "error": (
                f"'{board_url}' is not a Workday board URL. It should look like "
                "https://{tenant}.wd{N}.myworkdayjobs.com/{site}."
            )
        }
    limit = max(1, min(int(limit or 25), 100))

    session = wafer.AsyncSession(
        browser_solver=browser_solver,
        timeout=timedelta(seconds=timeout),
        cache_dir=get_wafer_cache_dir(),
        max_response_size=_MAX_RESPONSE,
    )
    try:
        async with asyncio.timeout(timeout):
            applied: dict = {}
            location_applied = False
            facets = None
            if location:
                facets = await fetch_workday_facets(board_url, session)
                resolved = resolve_location_facet(facets, location)
                if resolved:
                    parameter, ids = resolved
                    applied[parameter] = ids
                    location_applied = True

            # searchText cannot be trusted to filter honestly. On Adobe the
            # Canada slice is 6 postings; searchText="engineer" cuts it to 4
            # and searchText="ux" to 0, while searchText="designer" changes
            # nothing at all — the same tenant filters, over-filters, and
            # ignores depending on the token. So when a location facet pinned
            # the set down, the whole located set is pulled with no searchText
            # and the title is applied here, which is exact by construction.
            fetch_limit = _FETCH_CEILING if (strict_title and title) else limit
            board_query = "" if (location_applied and strict_title and title) else title
            result = await search_workday_board(
                board_url,
                session,
                search_text=board_query,
                applied_facets=applied,
                limit=fetch_limit,
            )
            if result is None:
                return {"error": await _diagnose_board(board_url, session)}

            postings = result.get("jobPostings") or []
            total = result.get("total") or len(postings)

            # Only when the located set is bigger than one pull, or there is no
            # location to pin it down, does the board's own search have to help.
            # Its ranking is literal, so widen on the stem and merge as well.
            needs_board_search = total > len(postings) or not location_applied
            if strict_title and title and needs_board_search:
                seen = {p.get("externalPath") for p in postings}
                for query in (title, broadened_query(title)):
                    if not query:
                        continue
                    extra = await search_workday_board(
                        board_url,
                        session,
                        search_text=query,
                        applied_facets=applied,
                        limit=fetch_limit,
                    )
                    if not extra:
                        continue
                    # The retries widen the pool, so the count has to widen
                    # with it. Without this, GAF — whose first query matched
                    # nothing and whose retries found 41 — reported a total of
                    # 0 and suppressed the summary line entirely.
                    total = max(total, extra.get("total") or 0)
                    for posting in extra.get("jobPostings") or []:
                        key = posting.get("externalPath")
                        if key not in seen:
                            seen.add(key)
                            postings.append(posting)

            # A location no facet matched must still constrain the result,
            # otherwise the board's full listing comes back under a heading
            # naming the requested place. "11 Locations" names no place, so
            # judging a multi-location posting on the summary alone drops it
            # however well it matches; expand those first. Past the cap a
            # posting stays summarised and is dropped as before — that needs
            # both a failed facet lookup and 25+ multi-location postings.
            if location and not location_applied:
                wanted = tokens(location)
                await _expand_locations(postings, board_url, session)
                postings = [
                    p
                    for p in postings
                    if any(
                        location_matches(value, wanted)
                        for value in (p.get("allLocations") or [p.get("locationsText") or ""])
                    )
                ]

            located = len(postings)
            dropped = 0
            if strict_title and title:
                postings, dropped = filter_by_title(postings, lambda p: p.get("title"), title)
            matched = len(postings)
            postings = postings[:limit]

            # Only the postings actually being shown, so this costs at most
            # `limit` requests and only for the ones the board summarised.
            await _expand_locations(postings, board_url, session)

            # Only when the location itself came back empty. With postings in
            # hand, an empty result is the title filter's doing and listing
            # other place names blames the wrong term.
            hint = ""
            if not postings and location and not located:
                if facets is None:
                    facets = await fetch_workday_facets(board_url, session)
                hint = _location_values(facets)

            return {
                "content": _render(
                    postings,
                    board_url=board_url,
                    employer=employer,
                    title=title,
                    location=location,
                    total=total,
                    dropped=dropped,
                    truncated_by_limit=matched - len(postings),
                    examined=located,
                    location_applied=location_applied,
                    location_hint=hint,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"Workday search timed out after {timeout:.0f}s."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and body.
        return {"error": f"Workday search failed ({type(exc).__name__})."}
