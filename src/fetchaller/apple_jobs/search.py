"""Public entry points: ``search_apple_jobs`` and ``get_apple_job``."""

from __future__ import annotations

import asyncio
import re

from ..jobfilter import broadened_query, filter_by_title, location_matches, tokens
from . import api
from .render import render_job, render_search_results

# Already in `{slug}-{CODE}` form, e.g. "toronto-TOR" — passed through as-is.
_LOCATION_SLUG_RE = re.compile(r"^[a-z0-9-]+-[A-Z0-9]{2,6}$")


def _job_locations(job: dict) -> str:
    """Flatten a posting's locations for matching."""
    names: list[str] = []
    for entry in job.get("locations") or []:
        if isinstance(entry, dict):
            for key in ("name", "city", "stateProvince", "countryName", "metro"):
                value = entry.get(key)
                if value:
                    names.append(str(value))
    return " ".join(names)


async def _resolve_location(session, locale: str, location: str) -> str:
    """Turn a place name into Apple's ``{slug}-{CODE}`` location parameter."""
    value = (location or "").strip()
    if not value:
        return ""
    if _LOCATION_SLUG_RE.match(value):
        return value

    wanted = tokens(value)
    # Probe with the place name: Apple's free-text search matches location
    # words, so the target city's own postings (and their codes) come back.
    for probe in (value, ""):
        vocabulary = await api.discover_locations(session=session, locale=locale, probe=probe)
        exact = [slug for name, slug in vocabulary.items() if name.casefold() == value.casefold()]
        if exact:
            return exact[0]
        matches = [slug for name, slug in vocabulary.items() if location_matches(name, wanted)]
        if matches:
            # Shortest slug is the least specific match, which is the safer
            # choice: "Toronto" over "Toronto Eaton Centre".
            return min(matches, key=len)
    return ""


async def search_apple_jobs(
    *,
    title: str = "",
    location: str = "",
    locale: str = api.DEFAULT_LOCALE,
    strict_title: bool = True,
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search jobs.apple.com, filtered by title and location.

    ``locale`` selects the storefront and with it the default country scope —
    ``en-ca`` shows Canadian postings, ``en-us`` American ones.
    """
    limit = max(1, min(int(limit or 25), 100))
    locale = api.normalize_locale(locale)

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            location_param = await _resolve_location(session, locale, location)
            location_applied = bool(location_param) or not location

            fetch_limit = min(limit * 4, 100) if (strict_title and title) else limit

            async def run(query: str):
                return await api.search_all(
                    session=session,
                    locale=locale,
                    search=query,
                    location=location_param,
                    limit=fetch_limit,
                )

            jobs, total = await run(title)

            # Apple's search matches query tokens literally, so "product
            # designer" and "product design" are different searches. Widen on
            # the stem and merge when the first pass came back thin.
            if strict_title and title and len(jobs) < fetch_limit:
                wider = broadened_query(title)
                if wider:
                    extra, extra_total = await run(wider)
                    seen = {j.get("id") or j.get("positionId") for j in jobs}
                    for job in extra:
                        key = job.get("id") or job.get("positionId")
                        if key not in seen:
                            seen.add(key)
                            jobs.append(job)
                    total = max(total, extra_total)

            # A location the board could not resolve must still constrain the
            # result. Returning postings from elsewhere under a heading naming
            # the requested place reads as "these are your matches" even with a
            # note attached, so the filter is applied here instead.
            if location and not location_param:
                wanted = tokens(location)
                jobs = [j for j in jobs if location_matches(_job_locations(j), wanted)]

            dropped = 0
            if strict_title and title:
                jobs, dropped = filter_by_title(jobs, lambda j: j.get("postingTitle"), title)
            jobs = jobs[:limit]

            return {
                "content": render_search_results(
                    jobs,
                    locale=locale,
                    title=title,
                    location=location,
                    total=total,
                    title_filtered=dropped,
                    location_applied=location_applied,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"jobs.apple.com search timed out after {timeout:.0f}s."}
    except api.AppleJobsBlockedError:
        return {"error": "jobs.apple.com declined the request. Retry shortly."}
    except api.AppleJobsUnavailableError:
        return {"error": "jobs.apple.com is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"jobs.apple.com search failed ({type(exc).__name__})."}


async def get_apple_job(
    position_id: str,
    *,
    slug: str = "",
    locale: str = api.DEFAULT_LOCALE,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one Apple posting.

    ``position_id`` is the number in ``/details/{positionId}/{slug}``. The slug
    is cosmetic — Apple serves the posting without a matching one.
    """
    position_id = (position_id or "").strip()
    if not position_id.isdigit():
        return {"error": "position_id must be the numeric id from the posting URL."}
    locale = api.normalize_locale(locale)

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            job = await api.fetch_job_detail(
                position_id, slug or "job", session=session, locale=locale
            )
            if job is None:
                return {"error": f"Apple posting {position_id} was not found (it may be closed)."}
            return {"content": render_job(job, locale=locale), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"jobs.apple.com fetch timed out after {timeout:.0f}s."}
    except api.AppleJobsBlockedError:
        return {"error": "jobs.apple.com declined the request. Retry shortly."}
    except api.AppleJobsUnavailableError:
        return {"error": "jobs.apple.com is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"jobs.apple.com fetch failed ({type(exc).__name__})."}
