"""Public entry points: ``search_oracle_jobs`` and ``get_oracle_job``."""

from __future__ import annotations

import asyncio
import re

from markdownify import markdownify

from ..jobfilter import (
    broadened_query,
    country_alpha3,
    filter_by_title,
    location_matches,
    tokens,
)
from . import api
from .employers import KNOWN_EMPLOYERS, OracleEmployer, resolve_employer

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")


def _clean(value, limit: int = 400) -> str:
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, limit) for v in value]
        return " · ".join(p for p in parts if p)
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _html(value: str) -> str:
    if not value:
        return ""
    md = markdownify(
        value,
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
    )
    return _BLANK_LINE_COLLAPSE_RE.sub("\n\n", md).strip()


def _locations(job: dict) -> str:
    names = [job.get("PrimaryLocation")]
    for entry in job.get("secondaryLocations") or []:
        if isinstance(entry, dict):
            names.append(entry.get("Name") or entry.get("LocationName"))
    seen: list[str] = []
    for name in names:
        text = _clean(name)
        if text and text not in seen:
            seen.append(text)
    return " · ".join(seen)


def _posting_url(employer: OracleEmployer, job: dict) -> str:
    job_id = job.get("Id")
    if not job_id:
        return ""
    if employer.posting_url:
        return employer.posting_url.format(id=job_id)
    return ""


def _render_results(
    jobs: list[dict],
    *,
    employer: OracleEmployer,
    title: str,
    location: str,
    total: int,
    title_filtered: int,
    location_filtered: int,
) -> str:
    scope = " · ".join(p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p)
    lines = [f"# {_clean(employer.label)} jobs{': ' + scope if scope else ''}", ""]

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
        name = _clean(job.get("Title")) or "(untitled)"
        url = _posting_url(employer, job)
        lines.append(f"## {index}. [{name}]({url})" if url else f"## {index}. {name}")
        where = _locations(job)
        if where:
            lines.append(f"- **Location**: {where}")
        for label, key in (
            ("Department", "Department"),
            ("Job family", "JobFamily"),
            ("Workplace", "WorkplaceType"),
            ("Schedule", "JobSchedule"),
            ("Posted", "PostedDate"),
        ):
            value = _clean(job.get(key))
            if value:
                lines.append(f"- **{label}**: {value}")
        req = _clean(job.get("Id"))
        if req:
            lines.append(f"- **Req ID**: {req}")
        summary = _clean(job.get("ShortDescriptionStr"), 400)
        if summary:
            lines.append(f"- {summary}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_job(job: dict, *, employer: OracleEmployer) -> str:
    name = _clean(job.get("Title")) or "Job Posting"
    lines = [f"# {name}", ""]
    where = _locations(job)
    if where:
        lines.append(f"- **Location**: {where}")
    for label, key in (
        ("Employer", "LegalEmployer"),
        ("Department", "Department"),
        ("Organization", "Organization"),
        ("Job function", "JobFunction"),
        ("Workplace", "WorkplaceType"),
        ("Schedule", "JobSchedule"),
        ("Worker type", "WorkerType"),
        ("Posted", "ExternalPostedStartDate"),
    ):
        value = _clean(job.get(key))
        if value:
            lines.append(f"- **{label}**: {value}")
    req = _clean(job.get("Id"))
    if req:
        lines.append(f"- **Req ID**: {req}")
    lines.append("")

    for heading, key in (
        ("Description", "ExternalDescriptionStr"),
        ("Responsibilities", "ExternalResponsibilitiesStr"),
        ("Qualifications", "ExternalQualificationsStr"),
        ("About the employer", "CorporateDescriptionStr"),
    ):
        body = _html(job.get(key) or "")
        if body:
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(body)
            lines.append("")

    url = _posting_url(employer, job)
    if url:
        lines.append(f"**Source**: {url}")
    return "\n".join(lines).rstrip() + "\n"


def _server_location(location: str) -> str:
    """The part of a location ORC's finder will actually honour.

    The ``location`` finder parameter filters on country names but silently
    ignores city names: ``location="Canada"`` narrows Uber's board from 640 to
    13, while ``location="Toronto"`` returns all 640. Sending a city therefore
    looks like a filter and is not one, so only the country is sent and the
    city is matched against each posting afterwards.
    """
    text = (location or "").strip()
    if not text:
        return ""
    if "," not in text and country_alpha3(text):
        return text
    tail = text.rsplit(",", 1)[-1].strip()
    return tail if country_alpha3(tail) else ""


async def _resolve_host(employer: OracleEmployer, session) -> str | None:
    host = await api.discover_host(employer.careers_url, session)
    return host or employer.fallback_host


async def search_oracle_jobs(
    employer: str,
    *,
    title: str = "",
    location: str = "",
    strict_title: bool = True,
    strict_location: bool = True,
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search one Oracle Recruiting Cloud tenant.

    ``employer`` is an alias (``uber``), a Fusion host, or a careers URL the
    Fusion host can be discovered from.
    """
    record = resolve_employer(employer)
    if record is None:
        known = ", ".join(sorted(KNOWN_EMPLOYERS))
        return {
            "error": (
                f"'{employer}' is not a recognised Oracle Recruiting board. Pass one of "
                f"{known}, a Fusion host (https://{{tenant}}.fa.{{region}}.oraclecloud.com), "
                "or the employer's careers URL."
            )
        }
    limit = max(1, min(int(limit or 25), 200))

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            host = await _resolve_host(record, session)
            if not host:
                return {
                    "error": (
                        f"Could not find an Oracle Recruiting host from {record.careers_url}. "
                        "The employer may have moved off Oracle Recruiting."
                    )
                }

            server_location = _server_location(location)
            fetch_limit = min(limit * 6, 400) if (strict_title and title) else limit
            # A city the board will not filter on means the whole board comes
            # back, so pull deeper before matching locally — otherwise a real
            # match sitting past the window reads as "no jobs there".
            if location and not server_location:
                fetch_limit = max(fetch_limit, 400)

            async def run(keyword: str):
                return await api.search_all_requisitions(
                    host,
                    session=session,
                    site_number=record.site_number,
                    keyword=keyword,
                    location=server_location,
                    limit=fetch_limit,
                )

            jobs, total = await run(title)

            # ORC matches keyword tokens literally, so widen on the stem and
            # merge when the first pass came back thin.
            if strict_title and title and len(jobs) < fetch_limit:
                wider = broadened_query(title)
                if wider:
                    extra, extra_total = await run(wider)
                    seen = {j.get("Id") for j in jobs}
                    for job in extra:
                        if job.get("Id") not in seen:
                            seen.add(job.get("Id"))
                            jobs.append(job)
                    total = max(total, extra_total)

            location_dropped = 0
            if strict_location and location:
                wanted = tokens(location)
                kept = [j for j in jobs if location_matches(_locations(j), wanted)]
                location_dropped = len(jobs) - len(kept)
                jobs = kept

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, lambda j: j.get("Title"), title)
            jobs = jobs[:limit]

            return {
                "content": _render_results(
                    jobs,
                    employer=record,
                    title=title,
                    location=location,
                    total=total,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"Oracle Recruiting search timed out after {timeout:.0f}s."}
    except api.OracleRecruitingBlockedError:
        return {"error": "The career site declined the request. Retry shortly."}
    except api.OracleRecruitingUnavailableError:
        return {"error": "The career site is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"Oracle Recruiting search failed ({type(exc).__name__})."}


async def get_oracle_job(
    employer: str,
    requisition_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one Oracle Recruiting posting."""
    record = resolve_employer(employer)
    if record is None:
        return {"error": f"'{employer}' is not a recognised Oracle Recruiting board."}
    requisition_id = (requisition_id or "").strip()
    if not requisition_id.isdigit():
        return {"error": "requisition_id must be the numeric id from the posting URL."}

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            host = await _resolve_host(record, session)
            if not host:
                return {"error": f"Could not find an Oracle Recruiting host from {record.careers_url}."}
            job = await api.fetch_requisition(
                host, requisition_id, session=session, site_number=record.site_number
            )
            if job is None:
                return {"error": f"Posting {requisition_id} was not found (it may be closed)."}
            return {"content": _render_job(job, employer=record), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"Oracle Recruiting fetch timed out after {timeout:.0f}s."}
    except api.OracleRecruitingBlockedError:
        return {"error": "The career site declined the request. Retry shortly."}
    except api.OracleRecruitingUnavailableError:
        return {"error": "The career site is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"Oracle Recruiting fetch failed ({type(exc).__name__})."}
