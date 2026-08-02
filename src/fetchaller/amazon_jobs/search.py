"""Public entry points: ``search_amazon_jobs`` and ``get_amazon_job``."""

from __future__ import annotations

import asyncio

from ..jobfilter import (
    broadened_query,
    country_alpha3,
    filter_by_title,
    location_matches,
    strip_country_tokens,
    tokens,
)
from . import api
from .render import render_job, render_search_results

# How many postings one search examines, independent of `limit`. `limit`
# sizes the output; this sizes the pool the filters run over. Tying the two
# together made the result depend on how many rows the caller asked to see:
# limit=1 examined 4 and returned 0 where limit=25 examined 100 and returned 13.
_EXAMINE_CEILING = 300


SORT = ("relevant", "recent")


def _resolve_country(location: str, country: str) -> str:
    """Turn an explicit country or a country-shaped location into an alpha-3 code."""
    return country_alpha3(country) or country_alpha3(location)


async def search_amazon_jobs(
    *,
    title: str = "",
    location: str = "",
    country: str = "",
    category: str = "",
    strict_title: bool = True,
    strict_location: bool = True,
    sort: str = "relevant",
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search amazon.jobs, filtered by title, location, and category.

    Amazon's own search is fuzzy in both dimensions — a title query returns
    adjacent roles and a location query returns the surrounding radius — so
    both are re-applied against each posting unless the caller opts out.
    """
    if sort not in SORT:
        return {"error": f"sort must be one of: {', '.join(SORT)}"}
    limit = max(1, min(int(limit or 25), 100))

    resolved_country = _resolve_country(location, country)
    categories = [api.category_slug(category)] if category else []

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            # `loc_query` does not filter — "Toronto" alone returns the whole
            # global board — so the caller's wording is resolved to Amazon's
            # exact `normalized_location` spellings and applied as a real
            # filter. Without this, a city search silently degrades into
            # "top N worldwide, then hope".
            places: list[str] = []
            wanted_place = (
                strip_country_tokens(tokens(location), resolved_country) if location else []
            )
            if wanted_place:
                # Probe with the place name itself, not the job title: Amazon's
                # free-text search matches the location words in a posting, so
                # this lands on the target city directly. A title-shaped probe
                # samples wherever that role happens to be hiring and misses it.
                for probe in (location, ""):
                    vocabulary = await api.discover_locations(
                        session=session, country=resolved_country, query=probe
                    )
                    places = [v for v in vocabulary if location_matches(v, wanted_place)]
                    if places:
                        break

            # Over-fetch when a filter will thin the results afterwards.
            filtering = (strict_title and title) or (strict_location and location)
            fetch_limit = _EXAMINE_CEILING if filtering else limit

            async def run(query: str):
                return await api.search_all_jobs(
                    session=session,
                    query=query,
                    location=location,
                    country=resolved_country,
                    categories=categories,
                    normalized_locations=places,
                    limit=fetch_limit,
                    sort=sort,
                )

            jobs, hits = await run(title)

            # Amazon matches query tokens literally enough that "product
            # designer" and "product design" are different searches. Widen on
            # the stem and merge when the first pass came back thin.
            if strict_title and title and len(jobs) < fetch_limit:
                wider = broadened_query(title)
                if wider:
                    extra, extra_hits = await run(wider)
                    seen = {j.get("id_icims") or j.get("id") for j in jobs}
                    for job in extra:
                        key = job.get("id_icims") or job.get("id")
                        if key not in seen:
                            seen.add(key)
                            jobs.append(job)
                    hits = max(hits, extra_hits)

            examined = len(jobs)
            location_dropped = 0
            if strict_location and location:
                # Amazon writes the country as an alpha-3 code
                # ("Vancouver, British Columbia, CAN"), so the caller's country
                # word never matches it. `country` has already filtered on it
                # server-side, so drop it here rather than failing every row.
                wanted = strip_country_tokens(tokens(location), resolved_country)
                if wanted:
                    kept = [
                        j
                        for j in jobs
                        if location_matches(
                            f"{j.get('normalized_location') or ''} {j.get('city') or ''} "
                            f"{j.get('state') or ''} {j.get('country_code') or ''}",
                            wanted,
                        )
                    ]
                    location_dropped = len(jobs) - len(kept)
                    jobs = kept

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, lambda j: j.get("title"), title)
            matched = len(jobs)
            jobs = jobs[:limit]

            return {
                "content": render_search_results(
                    jobs,
                    title=title,
                    location=location,
                    country=country,
                    job_category=category,
                    hits=hits,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                    truncated_by_limit=matched - len(jobs),
                    examined=examined,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"amazon.jobs search timed out after {timeout:.0f}s."}
    except api.AmazonJobsBlockedError:
        return {"error": "amazon.jobs declined the request. Retry shortly."}
    except api.AmazonJobsUnavailableError:
        return {"error": "amazon.jobs is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"amazon.jobs search failed ({type(exc).__name__})."}


async def get_amazon_job(
    job_path: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one amazon.jobs posting.

    ``job_path`` is the ``/en/jobs/{id}/{slug}`` path or a full amazon.jobs URL.
    """
    path = (job_path or "").strip()
    if path.startswith("http"):
        from urllib.parse import urlparse

        parsed = urlparse(path)
        if (parsed.hostname or "").casefold() not in ("www.amazon.jobs", "amazon.jobs"):
            return {"error": "Not an amazon.jobs URL."}
        path = parsed.path or ""
    if not path.startswith("/"):
        return {"error": "job_path must look like /en/jobs/{id}/{slug}."}

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            job = await api.fetch_job(path, session=session)
            if job is None:
                return {"error": "That amazon.jobs posting was not found (it may be closed)."}
            return {"content": render_job(job), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"amazon.jobs fetch timed out after {timeout:.0f}s."}
    except api.AmazonJobsBlockedError:
        return {"error": "amazon.jobs declined the request. Retry shortly."}
    except api.AmazonJobsUnavailableError:
        return {"error": "amazon.jobs is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"amazon.jobs fetch failed ({type(exc).__name__})."}
