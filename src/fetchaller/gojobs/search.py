"""Public entry points: ``search_gojobs`` and ``get_gojobs_job``."""

from __future__ import annotations

import asyncio
import re

from ..jobfilter import filter_by_title, location_matches, tokens
from . import api
from .render import render_job, render_search_results

# How many postings one search examines, independent of `limit`. Fixed, and
# never a multiple of `limit`: deriving it from the caller's page size makes
# the *answer* depend on how many rows were asked for. The board runs to a few
# hundred open postings, so this normally reaches the end of it; `search_all`
# stops early once the board's own total is covered, so the ceiling costs
# nothing when the result set is small.
_EXAMINE_CEILING = 250

# gojobs is the Ontario Public Service board: every posting on it is in Ontario,
# Canada. Filtering on those names would match nothing, because a row reads
# "Mississauga, Central Region" and never spells out the province. Treating
# them as a filter emptied the result set for the most natural query a caller
# could type, so they are recognised as board-wide and reported as a no-op.
_BOARD_WIDE_LOCATIONS = frozenset(
    {"ontario", "on", "canada", "ca", "ontario canada", "canada ontario"}
)

# Some postings are open province-wide and say so instead of naming a city:
# "Any City, Anywhere in Ontario". A city filter must keep them — they are
# genuinely available in that city, and dropping one is losing a real match,
# not tightening the result. This was reported for a whole session as an
# unexplained "dropped 1 by location" on every Toronto search.
_PROVINCE_WIDE_RE = re.compile(r"any\s*city|anywhere\s+in\s+ontario|province[- ]?wide", re.I)


def _location_is_open_to_anywhere(value: str) -> bool:
    return bool(_PROVINCE_WIDE_RE.search(value or ""))


def _norm_place(value: str) -> str:
    """Fold punctuation for exact matching.

    Still an exact match, just not defeated by a full stop: the board spells the
    city "St Catharines" and a caller naturally types "St. Catharines", so a
    plain casefold comparison missed it and silently applied no board filter at
    all. This is punctuation-insensitivity, not the fuzzy matching deliberately
    rejected below.
    """
    return " ".join(re.sub(r"[,.\u2019']", "", (value or "").casefold()).split())


def _resolve_location(
    location: str, vocab: dict[str, dict[str, str]]
) -> tuple[list[str], list[str], bool]:
    """Map a place name onto the board's own city/region codes.

    Returns ``(city_codes, region_codes, board_wide)``. The board filter is only
    ever an optimisation here — whatever it does or does not apply, the caller's
    location is re-checked against each posting's own location text.
    """
    value = " ".join((location or "").split())
    if not value:
        return [], [], False
    if _norm_place(value) in _BOARD_WIDE_LOCATIONS:
        return [], [], True

    folded = _norm_place(value)

    # Exact matches only, and city before region because a city is the more
    # specific reading of a name that is both ("Toronto" is a city and a
    # region; the city gives 32 postings, the region 35).
    cities = [
        code for code, label in (vocab.get("city") or {}).items() if _norm_place(label) == folded
    ]
    if cities:
        return cities, [], False

    regions = [
        code for code, label in (vocab.get("region") or {}).items() if _norm_place(label) == folded
    ]
    if regions:
        return [], regions, False

    # Deliberately no fuzzy fallback.
    #
    # Matching city labels loosely looked like a free optimisation and is a
    # correctness bug: "North" fuzzily matches the cities North Bay, North
    # York, Northbrook … so the board was asked for ~10 postings when the North
    # *region* has 39, and the 29 the client filter would have kept were never
    # fetched. A board filter may only ever be an optimisation over the pool the
    # client filter then checks — the moment it can exclude a posting that
    # would have matched, it stops being safe. Truncating the code list (it was
    # capped at 20) made that worse by dropping candidates arbitrarily.
    #
    # So an unrecognised place name applies no board filter at all and the
    # client filter runs over the full pool. That costs pages, not accuracy.
    return [], [], False


def _resolve_facet(value: str, vocabulary: dict[str, str]) -> list[str]:
    """Match an exact human label (or raw code) against a facet vocabulary.

    These facets are not present on search cards, so they cannot be rechecked
    locally. A fuzzy board-side match could therefore exclude valid postings
    before this process ever sees them; exact matching is the only safe scope.
    """
    text = " ".join((value or "").split())
    if not text:
        return []
    if text in vocabulary:
        return [text]
    folded = text.casefold()
    exact = [code for code, label in vocabulary.items() if label.casefold() == folded]
    return exact


# The board's own dropdown values. A free-text minimum is accepted by the form
# and silently ignored, so "banana" produced a whole-board search under a
# salary-filtered heading.
_SALARY_STEPS = ("0", "40000", "50000", "70000", "80000", "90000", "100000", "110000")


async def search_gojobs(
    *,
    title: str = "",
    location: str = "",
    category: str = "",
    career_level: str = "",
    min_salary: str = "",
    strict_title: bool = True,
    limit: int = 25,
    timeout: float = 180.0,
    browser_solver=None,
) -> dict:
    """Search the Ontario Public Service board.

    The board has **no keyword or title search** — its only text input is an
    exact Job ID lookup. So a title query is matched here, against each
    posting's own title, over a window of the board rather than by narrowing it
    server-side. Region and city are applied by the board and re-checked here.
    Job category, career level and minimum salary are only available as board
    facets, so their labels must resolve exactly before they are sent.
    """
    limit = max(1, min(int(limit or 25), 100))

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            vocab = await api.get_vocabularies(session)

            cities, regions, board_wide = _resolve_location(location, vocab)
            categories = _resolve_facet(category, vocab.get("category") or {})
            levels = _resolve_facet(career_level, vocab.get("career_level") or {})
            resolved_category = (
                (vocab.get("category") or {}).get(categories[0], category)
                if categories
                else ""
            )
            resolved_level = (
                (vocab.get("career_level") or {}).get(levels[0], career_level)
                if levels
                else ""
            )

            notes: list[str] = []
            if min_salary and min_salary not in _SALARY_STEPS:
                notes.append(
                    f"“{min_salary}” is not one of this board's minimum-salary "
                    f"steps ({', '.join(_SALARY_STEPS[1:])}), so no salary filter "
                    "was applied."
                )
                min_salary = ""
            if category and not categories:
                notes.append(
                    f"“{category}” did not match a job category on this board; "
                    "the category filter was not applied."
                )
            if career_level and not levels:
                notes.append(
                    f"“{career_level}” did not match a career level on this board; "
                    "that filter was not applied."
                )
            if location and not board_wide and not cities and not regions:
                # The board's vocabulary is 5 regions and its own city list;
                # colloquial areas like "Niagara" are in neither. Without this
                # the search silently degrades to a text match over the whole
                # board — "Niagara" returned 1 posting where "St. Catharines"
                # returns 7, quietly hiding the better fits.
                notes.append(
                    f"“{location}” is not a city or region this board knows, so no "
                    "location filter was applied and the results below are only "
                    "those whose own location text mentions it. Try a specific city "
                    "(e.g. “St. Catharines”, “Niagara Falls”) or one of its regions "
                    "(North, Central, East, West, Toronto)."
                )
            if board_wide:
                notes.append(
                    f"Every posting on this board is in Ontario, Canada, so "
                    f"“{location}” was treated as the whole board rather than a filter."
                )

            jobs, total, complete = await api.search_all(
                session=session,
                regions=regions,
                cities=cities,
                categories=categories,
                career_levels=levels,
                min_salary=min_salary,
                max_records=_EXAMINE_CEILING,
            )

            examined = len(jobs)

            location_dropped = 0
            if location and not board_wide:
                wanted = tokens(location)
                kept = [
                    j
                    for j in jobs
                    if location_matches(j.get("location") or "", wanted)
                    or _location_is_open_to_anywhere(j.get("location") or "")
                ]
                location_dropped = len(jobs) - len(kept)
                jobs = kept

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, lambda j: j.get("title"), title)

            matched = len(jobs)
            jobs = jobs[:limit]

            if not complete:
                notes.append(
                    "The board reported more postings than this window pulled; "
                    "narrow the query to bring the rest inside it."
                )

            scope = ""
            # The board's total is geographically scoped only when one of its
            # own city/region codes was sent.  An unresolved place is enforced
            # solely by the local card re-check, so attaching it to the remote
            # count would claim the board applied a filter it did not apply.
            if location and not board_wide and (cities or regions):
                scope = f"in {location}"

            return {
                "content": render_search_results(
                    jobs,
                    title=title,
                    location=location,
                    category=resolved_category,
                    career_level=resolved_level,
                    min_salary=min_salary,
                    board_total=total,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                    truncated_by_limit=matched - len(jobs),
                    examined=examined,
                    board_scope=scope,
                    notes=notes,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"gojobs.gov.on.ca search timed out after {timeout:.0f}s."}
    except api.GoJobsBlockedError:
        return {"error": "gojobs.gov.on.ca declined the request. Retry shortly."}
    except api.GoJobsUnavailableError:
        return {"error": "gojobs.gov.on.ca is not responding correctly. Try again shortly."}
    except api.GoJobsUnrecognisedPageError:
        return {
            "error": (
                "gojobs.gov.on.ca answered 200 with a page that is not a search "
                "result — a search-form shell, a soft error, or changed markup. "
                "No empty-search conclusion was drawn from it."
            )
        }
    except api.GoJobsProtocolError:
        return {
            "error": (
                "gojobs.gov.on.ca did not return its search form state, so the "
                "listing could not be requested. The page layout may have changed."
            )
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"gojobs.gov.on.ca search failed ({type(exc).__name__})."}


async def get_gojobs_job(
    job_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one OPS posting, by its numeric Job ID."""
    value = " ".join(str(job_id or "").split())
    if not value.isdigit():
        return {"error": "job_id must be the numeric Job ID from the posting URL."}

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            job = await api.fetch_job(value, session=session)
            if job is None:
                # Preview.aspx answers 200 and renders its shell for an unknown
                # id, so "not found" is a parse outcome rather than a status.
                return {
                    "error": (
                        f"gojobs posting {value} was not found. The Job ID may be "
                        "wrong, or the posting may have been withdrawn."
                    )
                }
            return {"content": render_job(job), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"gojobs.gov.on.ca fetch timed out after {timeout:.0f}s."}
    except api.GoJobsBlockedError:
        return {"error": "gojobs.gov.on.ca declined the request. Retry shortly."}
    except api.GoJobsUnavailableError:
        return {"error": "gojobs.gov.on.ca is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"gojobs.gov.on.ca fetch failed ({type(exc).__name__})."}
