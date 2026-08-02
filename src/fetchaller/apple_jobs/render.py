"""Markdown rendering for jobs.apple.com results."""

from __future__ import annotations

import re

from markdownify import markdownify

from ..jobfilter import counts_line

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")
_MAX_FIELD_CHARS = 400


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, limit) for v in value]
        return " · ".join(p for p in parts if p)
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _locations(job: dict) -> str:
    names = []
    for entry in job.get("locations") or []:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("city") or ""
            if name and name not in names:
                names.append(name)
    return _clean(names)


def _team(job: dict) -> str:
    team = job.get("team")
    if isinstance(team, dict):
        return _clean(team.get("teamName") or team.get("name"))
    return _clean(team)


def job_url(job: dict, locale: str) -> str:
    position_id = job.get("positionId") or job.get("id") or ""
    slug = job.get("transformedPostingTitle") or ""
    if not position_id:
        return ""
    return f"https://jobs.apple.com/{locale}/details/{position_id}/{slug}".rstrip("/")


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
    md = markdownify(
        html,
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
    )
    return _BLANK_LINE_COLLAPSE_RE.sub("\n\n", md).strip()


def render_search_results(
    jobs: list[dict],
    *,
    locale: str,
    title: str = "",
    location: str = "",
    total: int = 0,
    title_filtered: int = 0,
    location_applied: bool = True,
) -> str:
    scope = " · ".join(p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p)
    lines = [f"# Apple jobs{': ' + scope if scope else ''}", ""]

    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            board_total=total,
            board_label="Apple's board",
            board_scope=f"in {_clean(location)}" if location and location_applied else "",
        )
    )
    if location and not location_applied:
        lines.append("")
        lines.append(
            f"_Apple's board has no location matching “{_clean(location)}”, "
            "so the results are not location-filtered._"
        )
    lines.append("")

    if not jobs:
        lines.append("No postings matched.")
        return "\n".join(lines) + "\n"

    for index, job in enumerate(jobs, start=1):
        name = _clean(job.get("postingTitle")) or "(untitled)"
        url = job_url(job, locale)
        lines.append(f"## {index}. [{name}]({url})" if url else f"## {index}. {name}")
        where = _locations(job)
        if where:
            lines.append(f"- **Location**: {where}")
        team = _team(job)
        if team:
            lines.append(f"- **Team**: {team}")
        for label, key in (("Posted", "postingDate"), ("Type", "type")):
            value = _clean(job.get(key))
            if value:
                lines.append(f"- **{label}**: {value}")
        req = _clean(job.get("reqId") or job.get("positionId"))
        if req:
            lines.append(f"- **Req ID**: {req}")
        summary = _clean(job.get("jobSummary"), 400)
        if summary:
            lines.append(f"- {summary}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_job(job: dict, *, locale: str) -> str:
    name = _clean(job.get("postingTitle")) or "Job Posting"
    lines = [f"# {name}", ""]

    where = _locations(job)
    if where:
        lines.append(f"- **Location**: {where}")
    team = _team(job)
    if team:
        lines.append(f"- **Team**: {team}")
    for label, key in (
        ("Posted", "postingDate"),
        ("Type", "type"),
        ("Weekly hours", "standardWeeklyHours"),
        ("Home office", "homeOffice"),
    ):
        value = _clean(job.get(key))
        if value:
            lines.append(f"- **{label}**: {value}")
    req = _clean(job.get("reqId") or job.get("positionId"))
    if req:
        lines.append(f"- **Req ID**: {req}")
    lines.append("")

    for heading, key in (
        ("Summary", "jobSummary"),
        ("Description", "jobDescription"),
        ("Minimum qualifications", "minimumQualifications"),
        ("Preferred qualifications", "preferredQualifications"),
        ("Education & experience", "educationExperience"),
        ("Pay & benefits", "payAndBenefits"),
    ):
        raw = job.get(key)
        if isinstance(raw, list):
            raw = "".join(str(x) for x in raw)
        body = _html_to_markdown(raw or "") if raw else ""
        if body:
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(body)
            lines.append("")

    url = job_url(job, locale)
    if url:
        lines.append(f"**Source**: {url}")
    return "\n".join(lines).rstrip() + "\n"
