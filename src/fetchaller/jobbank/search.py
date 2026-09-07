"""Public entry point: ``search_jobbank``."""

from __future__ import annotations

import asyncio

from ..jobfilter import filter_by_title, location_matches, tokens
from . import api
from .render import render_search_results

# Fixed, and never a multiple of `limit`: deriving the examined pool from the
# caller's page size makes the answer depend on how many rows were asked for.
# Job Bank pages 25 at a time and is slow, so this is 8 pages at worst.
_EXAMINE_CEILING = 200

# Job Bank's own default. Stated in the output rather than assumed, because
# "near St. Catharines" legitimately includes Thorold and Hamilton and a caller
# who thinks they asked for one city would read those as a broken filter.
DEFAULT_RADIUS_KM = 50
_ALLOWED_RADII = (10, 25, 50, 100, 150, 200, 250, 300, 400, 500)


def _nearest_radius(value: int) -> int:
    """Snap to a radius the board actually offers."""
    return min(_ALLOWED_RADII, key=lambda allowed: abs(allowed - value))


async def search_jobbank(
    *,
    title: str = "",
    location: str = "",
    radius_km: int = DEFAULT_RADIUS_KM,
    strict_title: bool = True,
    strict_location: bool = False,
    limit: int = 25,
    timeout: float = 300.0,
    browser_solver=None,
) -> dict:
    """Search jobbank.gc.ca, the federal Job Bank.

    ``location`` is resolved to Job Bank's own numeric city id before it can
    filter anything — the bare place name is accepted and silently ignored, and
    the search comes back national. If it cannot be resolved, no location filter
    is sent at all and the caller is told, rather than being handed 64,000
    postings under a heading naming one city.

    ``strict_location`` defaults to False because Job Bank searches a radius by
    design; the radius used is always reported.
    """
    limit = max(1, min(int(limit or 25), 100))
    radius_km = _nearest_radius(int(radius_km or DEFAULT_RADIUS_KM))

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            notes: list[str] = []
            city_id = ""
            city_label = ""
            if location:
                city = await api.resolve_city(session, location)
                if city:
                    city_id = str(city.get("city_id") or "")
                    name = city.get("name") or ""
                    province = city.get("province_cd") or ""
                    city_label = f"{name}, {province}".strip(", ")
                else:
                    notes.append(
                        f"“{location}” did not match a city Job Bank knows, so no "
                        "location filter was applied — the board ignores a place "
                        "name it cannot resolve and would otherwise have returned "
                        "the whole country. Results below were filtered here instead."
                    )

            jobs, total, complete = await api.search_all(
                session=session,
                keywords=title,
                city_id=city_id,
                location_label=city_label,
                radius_km=radius_km if city_id else 0,
                max_records=_EXAMINE_CEILING,
            )

            # Job Bank drops some keywords silently. "assistant" within 25 km of
            # St. Catharines returned 424 — the same count, and the same first
            # five titles, as no keyword at all. The postings shown stay correct
            # because the title filter below is the guarantee, but the board's
            # figure would be reported for a query it never ran. One extra
            # request settles it, and only when a title was actually given.
            keyword_ignored_total = 0
            if title and total:
                plain = await api.unfiltered_total(
                    session=session,
                    city_id=city_id,
                    location_label=city_label,
                    radius_km=radius_km,
                )
                if plain and total == plain:
                    keyword_ignored_total = plain
                    total = 0
                    plain_scope = (
                        f"result set near {city_label}"
                        if city_id
                        else "national result set"
                    )
                    notes.append(
                        f"Job Bank ignored the keyword “{title}” — it returned its "
                        f"entire {plain}-posting {plain_scope} "
                        "unchanged, so its own count is not reported. The first "
                        f"{len(jobs)} postings returned were matched here instead."
                    )

            examined = len(jobs)

            # An unresolved location must still constrain the answer, and a
            # caller asking for strictness gets the exact city.
            location_dropped = 0
            if location and (strict_location or not city_id):
                wanted = tokens(location)
                kept = [j for j in jobs if location_matches(j.get("location") or "", wanted)]
                location_dropped = len(jobs) - len(kept)
                jobs = kept

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, lambda j: j.get("title"), title)

            matched = len(jobs)
            jobs = jobs[:limit]

            if not complete:
                notes.append(
                    "Job Bank reported more postings than this window pulled; "
                    "narrow the query or reduce `radius_km` to bring the rest inside it."
                )

            return {
                "content": render_search_results(
                    jobs,
                    title=title,
                    location=location,
                    # Only scope the board's own count when the board actually
                    # scoped it. Otherwise a national figure was captioned with
                    # the caller's city.
                    scoped=bool(city_id),
                    radius_km=radius_km if city_id else 0,
                    board_total=total,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                    truncated_by_limit=matched - len(jobs),
                    # A suppressed board total is unknown for this keyword, not
                    # zero. Passing the examined window to counts_line would make
                    # it claim all N postings were rechecked immediately before
                    # the note above says the board returned a larger unfiltered
                    # set. The note reports the window explicitly instead.
                    examined=0 if keyword_ignored_total else examined,
                    notes=notes,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {
            "error": (
                f"jobbank.gc.ca search timed out after {timeout:.0f}s. The board "
                "is slow; try a narrower query or a smaller radius_km."
            )
        }
    except api.JobBankBlockedError:
        return {"error": "jobbank.gc.ca declined the request. Retry shortly."}
    except api.JobBankUnavailableError:
        return {"error": "jobbank.gc.ca is not responding correctly. Try again shortly."}
    except api.JobBankUnrecognisedPageError:
        return {
            "error": (
                "jobbank.gc.ca answered 200 with a page that is not a search "
                "result — a sign-in shell, a soft error, or changed markup. "
                "No empty-search conclusion was drawn from it."
            )
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"jobbank.gc.ca search failed ({type(exc).__name__})."}
