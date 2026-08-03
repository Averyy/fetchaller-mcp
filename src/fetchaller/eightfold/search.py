"""Public entry points: ``search_eightfold_jobs`` and ``get_eightfold_job``.

The title filter here is deliberate. Eightfold ranks by relevance rather than
filtering — a ``query`` narrows the ordering but still returns adjacent roles,
so a "product designer" search surfaces engineering reqs. Callers asking for a
title almost always mean it as a constraint, so the query tokens are re-applied
against each posting's own name and the count of drops is reported rather than
hidden.
"""

from __future__ import annotations

import asyncio

from ..jobfilter import broadened_query, filter_by_title
from . import api
from .render import render_position, render_search_results
from .url import KNOWN_EMPLOYERS, board_root, extract_position_id, resolve_employer

# How many postings one search examines, independent of `limit`. `limit`
# sizes the output; this sizes the pool the filters run over. Tying the two
# together made the result depend on how many rows the caller asked to see:
# limit=1 examined 4 and returned 0 where limit=25 examined 100 and returned 13.
_EXAMINE_CEILING = 100


SORT = ("relevance", "recent")
_SORT_PARAM = {"relevance": "relevance", "recent": "most_recent"}


def _employer_label(board_url: str, employer: str) -> str:
    alias = (employer or "").strip().casefold()
    if alias in KNOWN_EMPLOYERS:
        return alias.capitalize() if alias != "paypal" else "PayPal"
    root = board_root(board_url) or board_url
    return root.split("://", 1)[-1]


def _location_hint(facets: dict) -> str:
    """A few spellings the tenant actually uses, for a location that matched nothing.

    These facet counts are the tenant's board-wide totals, not counts for the
    caller's query — Eightfold returns the same location facets whatever the
    query is. They are offered only as valid spellings to retry with.
    """
    locations = (facets or {}).get("locations")
    if not isinstance(locations, list):
        return ""
    names: list[str] = []
    for entry in locations[:12]:
        if isinstance(entry, (list, tuple)) and entry:
            names.append(str(entry[0]))
        elif isinstance(entry, str):
            names.append(entry)
    return ", ".join(n for n in names if n)


async def search_eightfold_jobs(
    employer: str,
    *,
    title: str = "",
    location: str = "",
    strict_title: bool = True,
    include_remote: bool = True,
    distance_km: int | None = None,
    sort: str = "relevance",
    limit: int = 25,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Search one Eightfold tenant's board.

    ``employer`` is an alias (``microsoft``, ``netflix``, ``paypal``) or any
    Eightfold board URL — a tenant this module has never seen works as long as
    its page publishes ``_EF_GROUP_ID``.
    """
    board_url = resolve_employer(employer)
    if board_url is None:
        known = ", ".join(sorted(KNOWN_EMPLOYERS))
        return {
            "error": (
                f"'{employer}' is not a recognised Eightfold board. Pass one of "
                f"{known}, or the board URL (e.g. https://acme.eightfold.ai/careers)."
            )
        }
    if sort not in SORT:
        return {"error": f"sort must be one of: {', '.join(SORT)}"}
    limit = max(1, min(int(limit or 25), 100))

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            group_id = await api.discover_group_id(board_url, session)
            if not group_id:
                return {
                    "error": (
                        f"Could not read the Eightfold tenant id from {board_url}. "
                        "The board may have moved off Eightfold."
                    )
                }

            generation = await api.detect_generation(
                board_url, session=session, group_id=group_id
            )

            # Over-fetch when a title filter will thin the results afterwards.
            # Fixed, not a multiple of `limit`: see _EXAMINE_CEILING.
            fetch_limit = _EXAMINE_CEILING if (strict_title and title) else limit

            async def run(query: str):
                return await api.search_all_positions(
                    board_url,
                    session=session,
                    group_id=group_id,
                    generation=generation,
                    query=query,
                    location=location,
                    limit=fetch_limit,
                    sort_by=_SORT_PARAM[sort],
                    include_remote=include_remote,
                    distance_km=distance_km,
                )

            positions, total, facets = await run(title)

            # The board matches query tokens literally, so a title it spells
            # differently ("Product Design" vs "product designer") is missed
            # outright. Retry on the stem and merge when the first pass came
            # back thin — the strict filter below is what keeps precision.
            if strict_title and title and len(positions) < fetch_limit:
                wider = broadened_query(title)
                if wider:
                    extra, extra_total, extra_facets = await run(wider)
                    seen = {p.get("id") for p in positions}
                    positions.extend(
                        p for p in extra if p.get("id") not in seen
                    )
                    total = max(total, extra_total)
                    facets = facets or extra_facets

            fetched = len(positions)
            dropped = 0
            if strict_title and title:
                positions, dropped = filter_by_title(
                    positions, lambda p: p.get("name"), title
                )
            matched = len(positions)
            positions = positions[:limit]

            root = board_root(board_url) or board_url
            markdown = render_search_results(
                positions,
                employer=_employer_label(board_url, employer),
                board_root=root,
                query=title,
                location=location,
                total=total,
                title_filtered=dropped,
                truncated_by_limit=matched - len(positions),
                examined=fetched,
            )
            # Only when the *location* found nothing. If the board returned
            # postings for it and the title filter is what emptied the list,
            # offering alternative spellings blames the wrong term — Microsoft
            # answered "Canada" with 34 postings and still printed a spelling
            # hint listing Redmond and Tokyo.
            if not positions and location and not fetched:
                hint = _location_hint(facets)
                if hint:
                    # "Location spellings this board uses" reads as "you spelled
                    # it wrong". PayPal's vocabulary genuinely holds no Canadian
                    # value, so the honest statement is that the board hires
                    # nowhere in Canada — not that the caller mistyped it.
                    markdown += (
                        f"\n_No location on this board matches “{location}”, so it "
                        "has no postings there at all — this is not a spelling "
                        f"problem._\n\nLocations this board does use: {hint}\n"
                        "\n_(board-wide values, not counts for this search)_\n"
                    )
            return {"content": markdown, "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"Eightfold search timed out after {timeout:.0f}s."}
    except api.EightfoldBlockedError:
        return {"error": "The career site declined the request. Retry shortly."}
    except api.EightfoldUnavailableError:
        return {"error": "The career site is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL, which carries
        # the caller's query.
        return {"error": f"Eightfold search failed ({type(exc).__name__})."}


async def get_eightfold_job(
    employer: str,
    position_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one Eightfold posting."""
    board_url = resolve_employer(employer)
    if board_url is None:
        return {"error": f"'{employer}' is not a recognised Eightfold board."}
    position_id = (position_id or "").strip()
    if not position_id.isdigit():
        return {"error": "position_id must be the numeric id from the posting URL."}

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            group_id = await api.discover_group_id(board_url, session)
            if not group_id:
                return {"error": f"Could not read the Eightfold tenant id from {board_url}."}
            generation = await api.detect_generation(
                board_url, session=session, group_id=group_id
            )
            position = await api.fetch_position_details(
                board_url,
                position_id,
                session=session,
                group_id=group_id,
                generation=generation,
            )
            if position is None:
                return {"error": f"Posting {position_id} was not found (it may be closed)."}
            root = board_root(board_url) or board_url
            return {
                "content": render_position(
                    position,
                    employer=_employer_label(board_url, employer),
                    board_root=root,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"Eightfold job fetch timed out after {timeout:.0f}s."}
    except api.EightfoldBlockedError:
        return {"error": "The career site declined the request. Retry shortly."}
    except api.EightfoldUnavailableError:
        return {"error": "The career site is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"Eightfold job fetch failed ({type(exc).__name__})."}


async def fetch_eightfold_url(url: str, *, timeout: float = 60.0, browser_solver=None) -> dict:
    """Render any Eightfold board or posting URL. Used by ``fetch()``."""
    position_id = extract_position_id(url)
    if position_id:
        return await get_eightfold_job(
            url, position_id, timeout=timeout, browser_solver=browser_solver
        )
    return await search_eightfold_jobs(
        url, strict_title=False, timeout=timeout, browser_solver=browser_solver
    )
