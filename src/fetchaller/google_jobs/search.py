"""Public entry points: ``search_google_jobs`` and ``get_google_job``."""

from __future__ import annotations

import asyncio
import re

from markdownify import markdownify

from ..jobfilter import (
    broadened_query,
    counts_line,
    filter_by_title,
    location_matches,
    tokens,
)
from . import api

SORT = ("relevance", "date")
_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")


def _clean(value, limit: int = 400) -> str:
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, limit) for v in value]
        return " · ".join(p for p in parts if p)
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _html(value: str, limit: int = 0) -> str:
    if not value:
        return ""
    md = markdownify(
        value,
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
    )
    md = _BLANK_LINE_COLLAPSE_RE.sub("\n\n", md).strip()
    return md[:limit] if limit else md


def _title(job: list) -> str:
    try:
        return job[api.JOB_TITLE] or ""
    except (IndexError, TypeError):
        return ""


def _render_results(
    jobs: list,
    *,
    title: str,
    location: str,
    google_total: int,
    title_filtered: int,
    location_filtered: int,
) -> str:
    scope = " · ".join(p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p)
    lines = [f"# Google jobs{': ' + scope if scope else ''}", ""]

    # Google's own count is reported separately and explicitly labelled,
    # because its free-text matching is loose enough that presenting it as the
    # answer would overstate the result by an order of magnitude: a "product
    # designer" search returns 38, of which two have a matching title.
    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            dropped_by_location=location_filtered,
            board_total=google_total,
            board_label="Google's own search",
        )
    )
    lines.append("")

    if not jobs:
        lines.append("No postings matched.")
        return "\n".join(lines) + "\n"

    for index, job in enumerate(jobs, start=1):
        name = _clean(_title(job)) or "(untitled)"
        url = api.posting_url(job[api.JOB_ID])
        lines.append(f"## {index}. [{name}]({url})")
        where = _clean(api.locations(job))
        if where:
            lines.append(f"- **Location**: {where}")
        company = _clean(job[api.JOB_COMPANY_NAME] if len(job) > api.JOB_COMPANY_NAME else "")
        if company:
            lines.append(f"- **Company**: {company}")
        posted = api.timestamp(job, api.JOB_CREATED_TS)
        if posted:
            lines.append(f"- **Posted**: {posted}")
        updated = api.timestamp(job, api.JOB_UPDATED_TS)
        if updated and updated != posted:
            lines.append(f"- **Updated**: {updated}")
        lines.append(f"- **Req ID**: {_clean(job[api.JOB_ID])}")
        summary = _html(api.html_field(job, api.JOB_DESCRIPTION), 300)
        if summary:
            lines.append(f"- {summary.splitlines()[0][:300]}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_job(job: list) -> str:
    lines = [f"# {_clean(_title(job)) or 'Job Posting'}", ""]
    where = _clean(api.locations(job))
    if where:
        lines.append(f"- **Location**: {where}")
    company = _clean(job[api.JOB_COMPANY_NAME] if len(job) > api.JOB_COMPANY_NAME else "")
    if company:
        lines.append(f"- **Company**: {company}")
    posted = api.timestamp(job, api.JOB_CREATED_TS)
    if posted:
        lines.append(f"- **Posted**: {posted}")
    updated = api.timestamp(job, api.JOB_UPDATED_TS)
    if updated and updated != posted:
        lines.append(f"- **Updated**: {updated}")
    lines.append(f"- **Req ID**: {_clean(job[api.JOB_ID])}")
    lines.append("")

    for heading, index in (
        ("About the job", api.JOB_DESCRIPTION),
        ("Minimum qualifications", api.JOB_MIN_QUALIFICATIONS),
        ("Qualifications", api.JOB_QUALIFICATIONS),
        ("Responsibilities", api.JOB_RESPONSIBILITIES),
    ):
        body = _html(api.html_field(job, index))
        if body:
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(body)
            lines.append("")

    lines.append(f"**Source**: {api.posting_url(job[api.JOB_ID])}")
    return "\n".join(lines).rstrip() + "\n"


async def search_google_jobs(
    *,
    title: str = "",
    location: str = "",
    strict_title: bool = True,
    strict_location: bool = True,
    remote_only: bool = False,
    sort: str = "relevance",
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search Google careers, filtered by title and location.

    ``location`` should be fully qualified for a city — "Waterloo, ON, Canada"
    rather than "Waterloo", which Google resolves to Waterloo, Belgium. A
    country name works as-is.
    """
    if sort not in SORT:
        return {"error": f"sort must be one of: {', '.join(SORT)}"}
    limit = max(1, min(int(limit or 25), 100))

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            # Over-fetch whenever *either* filter will thin the results. Google's
            # city filter is radius-based, so a location alone discards plenty:
            # fetching only `limit` rows and then dropping some would report
            # "2 jobs in Toronto" from a board that has far more.
            filtering = (strict_title and title) or (strict_location and location)
            fetch_limit = min(limit * 6, 200) if filtering else limit
            locations = [location] if location else None

            async def run(query: str):
                return await api.search_all(
                    session,
                    query=query,
                    locations=locations,
                    limit=fetch_limit,
                    remote_only=remote_only,
                    sort=sort,
                )

            jobs, google_total = await run(title)

            # Google matches query tokens loosely rather than literally, but a
            # differently-spelled title can still miss, so widen on the stem
            # and merge when the first pass came back thin.
            if strict_title and title and len(jobs) < fetch_limit:
                wider = broadened_query(title)
                if wider:
                    extra, extra_total = await run(wider)
                    seen = {j[api.JOB_ID] for j in jobs}
                    for job in extra:
                        if job[api.JOB_ID] not in seen:
                            seen.add(job[api.JOB_ID])
                            jobs.append(job)
                    google_total = max(google_total, extra_total)

            # Google's city filter is radius-based: a Toronto search returned 61
            # results of which only 25 actually list Toronto. The requested
            # place is therefore re-checked against each posting.
            location_dropped = 0
            if strict_location and location:
                wanted = tokens(location)
                kept = [j for j in jobs if location_matches(" ".join(api.locations(j)), wanted)]
                location_dropped = len(jobs) - len(kept)
                jobs = kept

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, _title, title)
            jobs = jobs[:limit]

            return {
                "content": _render_results(
                    jobs,
                    title=title,
                    location=location,
                    google_total=google_total,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"Google careers search timed out after {timeout:.0f}s."}
    except api.GoogleJobsBlockedError:
        return {"error": "Google declined the request. Retry shortly."}
    except api.GoogleJobsUnavailableError:
        return {"error": "Google is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"Google careers search failed ({type(exc).__name__})."}


async def get_google_job(
    job_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one Google posting."""
    job_id = (job_id or "").strip()
    if not job_id.isdigit():
        return {"error": "job_id must be the numeric id from the posting URL."}
    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            job = await api.fetch_job(session, job_id)
            if job is None:
                return {"error": f"Google posting {job_id} was not found (it may be closed)."}
            return {"content": _render_job(job), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"Google careers fetch timed out after {timeout:.0f}s."}
    except api.GoogleJobsBlockedError:
        return {"error": "Google declined the request. Retry shortly."}
    except api.GoogleJobsUnavailableError:
        return {"error": "Google is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"Google careers fetch failed ({type(exc).__name__})."}
