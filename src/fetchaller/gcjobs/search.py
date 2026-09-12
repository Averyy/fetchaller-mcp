"""Public entry points: ``search_gcjobs`` and ``get_gcjobs_job``."""

from __future__ import annotations

import asyncio
import re

from ..jobfilter import filter_by_title, location_matches, tokens
from . import api
from .render import render_external, render_job, render_search_results

# How many postings one search examines, independent of `limit`. Fixed, and
# never a multiple of `limit`, so the answer does not depend on how many rows
# were asked for. Ten pages; the whole national board was 414 on 2026-09-11,
# and any filter brings it well inside this.
_EXAMINE_CEILING = 200

# The board is national, so a country name is the whole board. Not quite: an
# "Exclude international" checkbox exists because a handful of postings are
# abroad, which is why the note says "almost every" rather than "every".
_BOARD_WIDE_LOCATIONS = frozenset({"canada", "ca"})

# The board's own bands, as its ``jobSalaryRange`` checkbox values. Measured
# on 2026-09-11 with one band at a time: every posting band 8 returned had a
# salary *floor* of $100,265 or more, and every one band 5 returned a floor
# between $70,338 and $78,909 — the board files a posting by the bottom of its
# range. A minimum salary therefore selects every band whose top reaches it,
# and the floor is re-checked here because a band starts below the figure
# asked for.
_SALARY_BANDS = (
    (1, 1, 39_999),
    (2, 40_000, 49_999),
    (3, 50_000, 59_999),
    (4, 60_000, 69_999),
    (5, 70_000, 79_999),
    (6, 80_000, 89_999),
    (7, 90_000, 99_999),
    (8, 100_000, None),
)

_LANGUAGES = {"english": "1", "french": "2", "bilingual": "3"}

_VARIOUS_RE = re.compile(r"various\s+locations", re.I)

_PROVINCES = {
    "ab": "alberta",
    "bc": "british columbia",
    "mb": "manitoba",
    "nb": "new brunswick",
    "nl": "newfoundland and labrador",
    "ns": "nova scotia",
    "nt": "northwest territories",
    "nu": "nunavut",
    "on": "ontario",
    "pe": "prince edward island",
    "qc": "quebec",
    "sk": "saskatchewan",
    "yt": "yukon",
}

_SALARY_FLOOR_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_NON_ANNUAL_RE = re.compile(r"per\s+(?:hour|day|week)|hourly|/\s*h(?:ou)?r\b", re.I)


def _norm(value: str) -> str:
    """Casefold, fold diacritics and punctuation, collapse whitespace."""
    return " ".join(tokens(value))


def _split_place(value: str) -> tuple[str, str]:
    """``"St. Catharines, ON"`` -> ``("St. Catharines", "ontario")``."""
    text = " ".join((value or "").split())
    if "," in text:
        city, _, tail = text.rpartition(",")
        tail = tail.strip()
        province = _PROVINCES.get(tail.casefold(), "")
        if not province and _norm(tail) in _PROVINCES.values():
            province = _norm(tail)
        if province:
            return city.strip(), province
    return text, ""


def _desc_parts(desc: str) -> tuple[str, str]:
    """``"St. Catharines (Ontario)"`` -> ``("St. Catharines", "Ontario")``."""
    match = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", desc or "")
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return (desc or "").strip(), ""


async def _resolve_location(session, location: str) -> tuple[list[str], list[str], bool]:
    """Map a place name onto the board's own ``<code><id>`` values.

    Returns ``(codes, labels, board_wide)``. Exact name matches only: the
    autocomplete is a substring search, so "Toronto" also returns "Toronto
    Pearson International Airport" and "x" returns Ajax and Comox. A loose
    match sent to the board would be a filter the caller never asked for, and
    an unrecognised place sends no board filter at all — the client-side
    re-check over the examined pool is the guarantee either way.
    """
    text = " ".join((location or "").split())
    if not text:
        return [], [], False
    if _norm(text) in _BOARD_WIDE_LOCATIONS:
        return [], [], True
    city, province = _split_place(text)
    if not city:
        return [], [], False
    wanted = _norm(city)
    codes: list[str] = []
    labels: list[str] = []
    for entry in await api.lookup_locations(session, city):
        desc = str(entry.get("LOCATION_DESC") or "")
        name, prov = _desc_parts(desc)
        if _norm(name) != wanted:
            continue
        if province and _norm(prov) != province:
            continue
        codes.append(f"{entry['LOCATION_CD']}{entry['LOCATION_ID']}")
        labels.append(desc)
    return codes, labels, False


def _resolve_organization(
    organization: str, vocabulary: dict[str, tuple[str, str]]
) -> tuple[list[str], str]:
    """``(department_ids, label)`` for an exact name or abbreviation match."""
    text = " ".join((organization or "").split())
    if not text:
        return [], ""
    folded = _norm(text)
    if text in vocabulary:
        return [text], vocabulary[text][0]
    for dept_id, (name, abbreviation) in vocabulary.items():
        if folded == _norm(name) or (abbreviation and folded == _norm(abbreviation)):
            return [dept_id], name
    return [], ""


def board_title_term(title: str) -> str:
    """What to send as the board's ``title`` for a caller's title.

    The board matches a case-insensitive **substring in the given word
    order**: "policy analyst" returned 3 and "analyst policy" returned 0, so a
    multi-word title cannot be sent as typed. The client-side title filter is
    the guarantee and it accepts a posting when each query word shares a
    prefix of at least four characters with a title word, in either direction
    — so the widest thing it can accept for a word is a four-letter title
    word that the query word begins with. The first four characters of one
    query word are therefore the longest substring every accepted title is
    certain to contain. The longest word is sent, as the least common one.
    """
    words = tokens(title)
    if not words:
        return ""
    longest = max(words, key=len)
    return longest[:4]


def salary_bands_for(min_salary: int) -> list[int]:
    return [band for band, _low, high in _SALARY_BANDS if high is None or high >= min_salary]


def annual_floor(salary: str) -> int | None:
    """The bottom of an annual salary range, or None when it is not annual."""
    text = salary or ""
    if _NON_ANNUAL_RE.search(text):
        return None
    match = _SALARY_FLOOR_RE.search(text)
    if not match:
        return None
    try:
        value = int(float(match.group(1).replace(",", "")))
    except ValueError:
        return None
    return value if value >= 10_000 else None


def _is_various(location: str) -> bool:
    return bool(_VARIOUS_RE.search(location or ""))


async def search_gcjobs(
    *,
    title: str = "",
    location: str = "",
    organization: str = "",
    min_salary: int = 0,
    language: str = "",
    exclude_various_locations: bool = False,
    strict_title: bool = True,
    limit: int = 25,
    timeout: float = 180.0,
    browser_solver=None,
) -> dict:
    """Search GC Jobs, the federal public service board.

    Every filter is sent to the board in the form its own search page uses,
    and the page's echo of the criteria it applied is checked before any
    board count is reported under that filter. Title, location and salary are
    then re-checked here against each row; organization and language are
    board facets the rows carry as free text and are trusted as the board
    scoped them.
    """
    limit = max(1, min(int(limit or 25), 100))
    min_salary = max(0, int(min_salary or 0))
    language_key = (language or "").strip().casefold()

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            notes: list[str] = []

            codes, labels, board_wide = await _resolve_location(session, location)
            if board_wide:
                notes.append(
                    f"Almost every posting on this board is in Canada, so “{location}” "
                    "was treated as the whole board rather than a filter."
                )
            elif location and not codes:
                notes.append(
                    f"“{location}” is not a place GC Jobs knows, so no location filter "
                    "was applied and the results below are only those whose own "
                    "location line mentions it. Try a city (“St. Catharines, ON”) or "
                    "a province (“Ontario”)."
                )

            departments: list[str] = []
            org_label = ""
            if organization:
                vocabulary = await api.get_departments(session)
                departments, org_label = _resolve_organization(organization, vocabulary)
                if not departments:
                    notes.append(
                        f"“{organization}” did not match an organization name or "
                        "abbreviation on this board, so no organization filter was "
                        "applied; the results below are those whose own organization "
                        "line mentions it."
                    )

            language_code = _LANGUAGES.get(language_key, "")
            if language and not language_code:
                notes.append(
                    f"“{language}” is not one of the board's language requirements "
                    "(english, french, bilingual); that filter was not applied."
                )

            bands = salary_bands_for(min_salary) if min_salary else []

            params = api.search_params(
                title_term=board_title_term(title),
                locations=codes,
                departments=departments,
                salary_bands=bands,
                language=language_code,
                exclude_various=exclude_various_locations,
            )
            jobs, total, complete, applied = await api.search_all(
                session=session, params=params, max_records=_EXAMINE_CEILING
            )
            examined = len(jobs)

            # The board's own echo decides whether a filter counts as applied.
            # A parameter it ignored yields a normal page of the whole board.
            location_applied = bool(codes) and any(
                code in applied.get("wLocation", []) for code in codes
            )
            if codes and not location_applied:
                notes.append(
                    "GC Jobs did not echo the location filter back as applied, so "
                    "its count is reported unscoped; the postings below were "
                    "filtered here."
                )
            org_applied = bool(departments) and "organization" in applied
            if departments and not org_applied:
                notes.append(
                    "GC Jobs did not echo the organization filter back as applied, "
                    "so its count is reported unscoped; the postings below were "
                    "filtered here on the organization line."
                )
            salary_applied = bool(bands) and any(
                str(band) in applied.get("salary", []) for band in bands
            )
            if bands and not salary_applied:
                notes.append(
                    "GC Jobs did not echo the salary filter back as applied, so its "
                    "count is reported unscoped; salaries were re-checked here."
                )
            language_applied = bool(language_code) and "lang" in applied
            if language_code and not language_applied:
                notes.append(
                    "GC Jobs did not echo the language filter back as applied, so "
                    "its count is reported unscoped and the language column is "
                    "unfiltered."
                )

            location_dropped = 0
            various_kept = 0
            if location and not board_wide:
                wanted = tokens(location)
                kept = []
                for job in jobs:
                    where = job.get("location") or ""
                    if location_matches(where, wanted):
                        kept.append(job)
                    elif _is_various(where) and not exclude_various_locations:
                        kept.append(job)
                        various_kept += 1
                location_dropped = len(jobs) - len(kept)
                jobs = kept

            org_dropped = 0
            if organization and not departments:
                wanted = tokens(organization)
                kept = [j for j in jobs if location_matches(j.get("organization") or "", wanted)]
                org_dropped = len(jobs) - len(kept)
                jobs = kept
                # Reported with the location drops: both are "the row's own
                # text did not name what was asked for".
                location_dropped += org_dropped

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, lambda j: j.get("title"), title)

            salary_dropped = 0
            if min_salary:
                kept = []
                for job in jobs:
                    floor = annual_floor(job.get("salary") or "")
                    if floor is None or floor >= min_salary:
                        kept.append(job)
                salary_dropped = len(jobs) - len(kept)
                jobs = kept

            matched = len(jobs)
            jobs = jobs[:limit]

            if various_kept:
                shown_various = sum(1 for j in jobs if _is_various(j.get("location") or ""))
                why = (
                    f"the board counts those as matches for “{location}”"
                    if location_applied
                    else "a posting open in various places may include it"
                )
                notes.append(
                    f"{various_kept} of the matches list “Various Locations” rather "
                    f"than naming a place ({shown_various} of them shown); {why}, and "
                    "the posting itself lists the actual places. Set "
                    "exclude_various_locations=true to drop them."
                )
            if not complete:
                notes.append(
                    "GC Jobs reported more postings than this window pulled; narrow "
                    "the query to bring the rest inside it."
                )

            scope_parts = []
            if location_applied:
                scope_parts.append("in " + " or ".join(labels))
            if org_applied:
                scope_parts.append(f"at {org_label}")
            if salary_applied:
                scope_parts.append(f"paying from ${min_salary:,} (board bands)")
            if language_applied:
                scope_parts.append(f"{language_key} language requirement")
            if exclude_various_locations:
                scope_parts.append("excluding “Various Locations”")

            return {
                "content": render_search_results(
                    jobs,
                    title=title,
                    location=location,
                    organization=org_label or organization,
                    min_salary=min_salary,
                    language=language_key if language_code else "",
                    board_total=total,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                    salary_filtered=salary_dropped,
                    truncated_by_limit=matched - len(jobs),
                    examined=examined,
                    exhausted=complete,
                    board_scope="; ".join(scope_parts),
                    notes=notes,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"GC Jobs search timed out after {timeout:.0f}s."}
    except api.GCJobsBlockedError:
        return {"error": "GC Jobs declined the request. Retry shortly."}
    except (api.GCJobsUnavailableError, api.GCJobsNotFoundError):
        return {"error": "GC Jobs is not responding correctly. Try again shortly."}
    except api.GCJobsSessionError:
        return {
            "error": (
                "GC Jobs answered its “Lost Connection” page instead of results, so "
                "the search state was not established. No empty-search conclusion "
                "was drawn from it; retry."
            )
        }
    except api.GCJobsUnrecognisedPageError:
        return {
            "error": (
                "GC Jobs answered 200 with a page that is not a search result — "
                "the JavaScript shell, a soft error, or changed markup. No "
                "empty-search conclusion was drawn from it."
            )
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"GC Jobs search failed ({type(exc).__name__})."}


async def get_gcjobs_job(
    poster_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one posting, by the numeric ``poster`` id in its URL."""
    value = " ".join(str(poster_id or "").split())
    if not value.isdigit():
        return {"error": "job_id must be the numeric poster ID from the posting URL."}

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            job = await api.fetch_job(value, session=session)
            if job is None:
                return {
                    "error": (
                        f"GC Jobs posting {value} was not found. The poster ID may be "
                        "wrong, or the posting may have been withdrawn."
                    )
                }
            if job["kind"] == "external":
                return {"content": render_external(job), "content_type": "markdown"}
            return {"content": render_job(job), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"GC Jobs fetch timed out after {timeout:.0f}s."}
    except api.GCJobsBlockedError:
        return {"error": "GC Jobs declined the request. Retry shortly."}
    except api.GCJobsUnavailableError:
        return {"error": "GC Jobs is not responding correctly. Try again shortly."}
    except api.GCJobsSessionError:
        return {"error": "GC Jobs answered its “Lost Connection” page for this posting; retry."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"GC Jobs fetch failed ({type(exc).__name__})."}
