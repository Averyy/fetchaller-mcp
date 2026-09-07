"""Markdown rendering for ca.indeed.com results."""

from __future__ import annotations

from ..jobfilter import counts_line

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_MAX_FIELD_CHARS = 300
_MAX_DESCRIPTION_CHARS = 20_000
_SUMMARY_FIELDS = ("employer", "location", "salary", "posted")


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def render_search_results(
    jobs: list[dict],
    *,
    title: str = "",
    location: str = "",
    scoped: bool = True,
    board_total: int = 0,
    title_filtered: int = 0,
    location_filtered: int = 0,
    truncated_by_limit: int = 0,
    examined: int = 0,
    notes: list[str] | None = None,
) -> str:
    scope = " · ".join(
        p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p
    )
    lines = [f"# Indeed{': ' + scope if scope else ''}", ""]
    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            dropped_by_location=location_filtered,
            board_total=board_total,
            board_label="Indeed",
            board_scope=f"near {_clean(location)}" if (location and scoped) else "",
            truncated_by_limit=truncated_by_limit,
            examined=examined,
        )
    )
    for note in notes or []:
        lines.extend(["", f"_{note}_"])
    if not jobs:
        lines.extend(["", "No matching postings."])
        return "\n".join(lines)
    for job in jobs:
        lines.append("")
        lines.append(f"### [{_clean(job.get('title'))}]({job.get('url', '')})")
        summary = [_clean(job.get(f)) for f in _SUMMARY_FIELDS if job.get(f)]
        if summary:
            lines.append(" · ".join(summary))
    return "\n".join(lines)


def _band(job: dict) -> str:
    low, high, unit = job.get("salary_min"), job.get("salary_max"), job.get("salary_unit")
    suffix = {"HOUR": "hourly", "YEAR": "annually", "MONTH": "monthly"}.get(
        str(unit or "").upper(), str(unit or "").lower()
    )
    currency = job.get("currency") or ""
    # Joined rather than interpolated: an unknown unit makes `suffix` empty, and
    # "$19 - $23  CAD" (two spaces) is the kind of detail that reads as a bug.
    if low and high and low != high:
        head = f"${low:,.0f} - ${high:,.0f}"
    elif low or high:
        head = f"${low or high:,.0f}"
    else:
        return ""
    return " ".join(part for part in (head, suffix, currency) if part)


def render_job(job: dict) -> str:
    lines = [f"# {_clean(job.get('title')) or 'Indeed posting'}", ""]
    for label, key in (
        ("Employer", "employer"),
        ("Location", "location"),
        ("Employment type", "employment_type"),
        ("Posted", "posted"),
    ):
        if job.get(key):
            lines.append(f"- **{label}:** {_clean(job[key])}")
    band = _band(job)
    if band:
        lines.append(f"- **Salary:** {band}")
    # Stated plainly: an expiry is the one thing that distinguishes a live
    # posting from an index entry, and stale indexes are why this board is here.
    if job.get("valid_through"):
        lines.append(f"- **Listing valid through:** {_clean(job['valid_through'])}")
    lines.append(f"- **URL:** {job.get('url', '')}")
    if job.get("description"):
        # The parser has flattened this to plain text, not trusted Markdown.
        lines.extend(
            ["", "---", "", _clean(job["description"], _MAX_DESCRIPTION_CHARS)]
        )
    return "\n".join(lines)
