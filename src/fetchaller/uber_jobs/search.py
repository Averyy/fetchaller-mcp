"""Public entry points: ``search_uber_jobs`` and ``get_uber_job``."""

from __future__ import annotations

import asyncio
import re

from markdownify import markdownify

from ..jobfilter import (
    broadened_query,
    country_alpha3,
    filter_by_title,
    location_matches,
    strip_country_tokens,
    tokens,
)
from . import api

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")


def _clean(value, limit: int = 400) -> str:
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, limit) for v in value]
        return " · ".join(p for p in parts if p)
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _location_text(job: dict) -> str:
    entries = job.get("allLocations") or []
    if not isinstance(entries, list) or not entries:
        entries = [job.get("location")] if isinstance(job.get("location"), dict) else []
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # Uber often ships every location field as null (remote or
        # unspecified reqs). Dropping the line silently reads as "we failed to
        # parse it", so those are labelled instead.
        parts = [entry.get("city"), entry.get("region"), entry.get("countryName")]
        name = ", ".join(p for p in parts if p)
        if name and name not in names:
            names.append(name)
    if not names and entries:
        return "Not specified"
    return " · ".join(names)


def _job_url(job: dict) -> str:
    job_id = job.get("id")
    return f"https://www.uber.com/global/en/careers/list/{job_id}/" if job_id else ""


def _render_results(
    jobs: list[dict],
    *,
    title: str,
    location: str,
    total: int,
    title_filtered: int,
    location_filtered: int,
) -> str:
    scope = " · ".join(p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p)
    lines = [f"# Uber jobs{': ' + scope if scope else ''}", ""]

    plural = "" if len(jobs) == 1 else "s"
    counts = f"_{len(jobs)} job{plural} shown"
    if total and total > len(jobs):
        counts += f" of {total} matching"
    dropped = []
    if title_filtered:
        dropped.append(f"{title_filtered} by title")
    if location_filtered:
        dropped.append(f"{location_filtered} by location")
    if dropped:
        counts += "; dropped " + " and ".join(dropped)
    lines.append(counts + "_")
    lines.append("")

    if not jobs:
        lines.append("No postings matched.")
        return "\n".join(lines) + "\n"

    for index, job in enumerate(jobs, start=1):
        name = _clean(job.get("title")) or "(untitled)"
        url = _job_url(job)
        lines.append(f"## {index}. [{name}]({url})" if url else f"## {index}. {name}")
        where = _location_text(job)
        if where:
            lines.append(f"- **Location**: {where}")
        for label, key in (
            ("Department", "department"),
            ("Team", "team"),
            ("Level", "level"),
            ("Type", "type"),
        ):
            value = _clean(job.get(key))
            if value:
                lines.append(f"- **{label}**: {value}")
        req = _clean(job.get("id"))
        if req:
            lines.append(f"- **Req ID**: {req}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_job(job: dict) -> str:
    name = _clean(job.get("title")) or "Job Posting"
    lines = [f"# {name}", ""]
    where = _location_text(job)
    if where:
        lines.append(f"- **Location**: {where}")
    for label, key in (
        ("Department", "department"),
        ("Team", "team"),
        ("Level", "level"),
        ("Type", "type"),
        ("Program", "programAndPlatform"),
    ):
        value = _clean(job.get(key))
        if value:
            lines.append(f"- **{label}**: {value}")
    req = _clean(job.get("id"))
    if req:
        lines.append(f"- **Req ID**: {req}")
    lines.append("")

    description = job.get("description") or ""
    if description:
        body = markdownify(
            description,
            heading_style="ATX",
            bullets="-",
            escape_asterisks=False,
            escape_underscores=False,
        )
        body = _BLANK_LINE_COLLAPSE_RE.sub("\n\n", body).strip()
        if body:
            lines.append("## Description")
            lines.append("")
            lines.append(body)
            lines.append("")

    url = _job_url(job)
    if url:
        lines.append(f"**Source**: {url}")
    return "\n".join(lines).rstrip() + "\n"


def _split_location(location: str) -> tuple[str, str]:
    """Return ``(alpha3, city)`` for a free-text location."""
    alpha3 = country_alpha3(location)
    remainder = strip_country_tokens(tokens(location), alpha3)
    # Uber's `city` is a single token in practice ("Toronto", "Amsterdam").
    city = remainder[0].title() if remainder else ""
    return alpha3, city


async def search_uber_jobs(
    *,
    title: str = "",
    location: str = "",
    strict_title: bool = True,
    strict_location: bool = True,
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search jobs.uber.com, filtered by title and location."""
    limit = max(1, min(int(limit or 25), 100))
    alpha3, city = _split_location(location)
    location_param = api.build_location(country=alpha3, city=city)

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            fetch_limit = min(limit * 6, 300) if (strict_title and title) else limit

            async def run(query: str):
                return await api.search_all(
                    session=session,
                    query=query,
                    location=location_param,
                    limit=fetch_limit,
                )

            jobs, total = await run(title)

            # Uber matches query tokens literally, so widen on the stem and
            # merge when the first pass came back thin.
            if strict_title and title and len(jobs) < fetch_limit:
                wider = broadened_query(title)
                if wider:
                    extra, extra_total = await run(wider)
                    seen = {j.get("id") for j in jobs}
                    for job in extra:
                        if job.get("id") not in seen:
                            seen.add(job.get("id"))
                            jobs.append(job)
                    total = max(total, extra_total)

            location_dropped = 0
            if strict_location and location:
                # The country is already filtered server-side; only a city or
                # region still has to be checked against the posting.
                wanted = strip_country_tokens(tokens(location), alpha3)
                if wanted:
                    kept = [j for j in jobs if location_matches(_location_text(j), wanted)]
                    location_dropped = len(jobs) - len(kept)
                    jobs = kept

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, lambda j: j.get("title"), title)
            jobs = jobs[:limit]

            return {
                "content": _render_results(
                    jobs,
                    title=title,
                    location=location,
                    total=total,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"jobs.uber.com search timed out after {timeout:.0f}s."}
    except api.UberJobsBlockedError:
        return {"error": "jobs.uber.com declined the request. Retry shortly."}
    except api.UberJobsUnavailableError:
        return {"error": "jobs.uber.com is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"jobs.uber.com search failed ({type(exc).__name__})."}


async def get_uber_job(
    job_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one Uber posting."""
    job_id = (job_id or "").strip()
    if not job_id.isdigit():
        return {"error": "job_id must be the numeric id from the posting URL."}
    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            job = await api.fetch_job(job_id, session=session)
            if job is None:
                return {"error": f"Uber posting {job_id} was not found (it may be closed)."}
            return {"content": _render_job(job), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"jobs.uber.com fetch timed out after {timeout:.0f}s."}
    except api.UberJobsBlockedError:
        return {"error": "jobs.uber.com declined the request. Retry shortly."}
    except api.UberJobsUnavailableError:
        return {"error": "jobs.uber.com is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"jobs.uber.com fetch failed ({type(exc).__name__})."}
