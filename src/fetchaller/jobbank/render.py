"""Markdown rendering for jobbank.gc.ca results."""

from __future__ import annotations

from ..jobfilter import counts_line

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_MAX_FIELD_CHARS = 300

_SUMMARY_FIELDS = ("employer", "location", "salary", "workplace", "posted")


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def render_search_results(
    jobs: list[dict],
    *,
    title: str = "",
    location: str = "",
    radius_km: int = 0,
    scoped: bool = True,
    board_total: int = 0,
    title_filtered: int = 0,
    location_filtered: int = 0,
    truncated_by_limit: int = 0,
    examined: int = 0,
    notes: list[str] | None = None,
) -> str:
    scope_bits = [f"“{_clean(title)}”" if title else "", _clean(location)]
    scope = " · ".join(p for p in scope_bits if p)
    lines = [f"# Job Bank{': ' + scope if scope else ''}", ""]

    # The radius is part of the answer, not a detail. Job Bank searches "near"
    # a city, so a St. Catharines search legitimately returns Thorold and
    # Hamilton postings; saying 50 km is the difference between that being
    # informative and it looking like a broken location filter.
    board_scope = ""
    if location and scoped:
        board_scope = f"within {radius_km} km of {_clean(location)}" if radius_km else f"near {_clean(location)}"

    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            dropped_by_location=location_filtered,
            board_total=board_total,
            board_label="Job Bank",
            board_scope=board_scope,
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
        lines.append(f"Job number: {_clean(job.get('job_id'))}")

    return "\n".join(lines)
