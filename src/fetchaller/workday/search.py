"""Public entry point: ``search_workday_jobs``."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from urllib.parse import urlparse

import wafer

from ..config import get_wafer_cache_dir
from ..content.workday import (
    extract_workday_board_params,
    fetch_workday_facets,
    flatten_facets,
    resolve_location_facet,
    search_workday_board,
)
from ..jobfilter import broadened_query, filter_by_title, location_matches, tokens
from .employers import KNOWN_EMPLOYERS, resolve_employer

_MAX_RESPONSE = 10 * 1024 * 1024
# A located board is usually small; pulling the whole slice makes the
# client-side title filter exact rather than a filter over page one.
_FETCH_CEILING = 200
_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})


def _clean(value, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


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
) -> str:
    scope = " · ".join(p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p)
    lines = [f"# {_clean(employer)} jobs{': ' + scope if scope else ''}", ""]

    plural = "" if len(postings) == 1 else "s"
    counts = f"_{len(postings)} job{plural} shown"
    if total and total > len(postings):
        # With a facet applied, `total` counts the located slice, not the board.
        scope_word = f"in {_clean(location)}" if location_applied else "on the board"
        counts += f" of {total} {scope_word}"
    if dropped:
        counts += f"; {dropped} dropped by the title filter"
    lines.append(counts + "_")
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
        where = _clean(posting.get("locationsText"))
        if where:
            lines.append(f"- **Location**: {where}")
        posted = _clean(posting.get("postedOn"))
        if posted:
            lines.append(f"- **Posted**: {posted}")
        bullets = posting.get("bulletFields") or []
        if isinstance(bullets, list):
            shown = [_clean(b) for b in bullets if b not in (None, "")]
            if shown:
                lines.append(f"- **Req ID**: {', '.join(shown)}")
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
                return {"error": f"Workday board {board_url} did not answer."}

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
                    for posting in extra.get("jobPostings") or []:
                        key = posting.get("externalPath")
                        if key not in seen:
                            seen.add(key)
                            postings.append(posting)

            # A location no facet matched must still constrain the result,
            # otherwise the board's full listing comes back under a heading
            # naming the requested place.
            if location and not location_applied:
                wanted = tokens(location)
                postings = [
                    p for p in postings if location_matches(p.get("locationsText") or "", wanted)
                ]

            dropped = 0
            if strict_title and title:
                postings, dropped = filter_by_title(postings, lambda p: p.get("title"), title)
            postings = postings[:limit]

            hint = ""
            if not postings and location:
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
