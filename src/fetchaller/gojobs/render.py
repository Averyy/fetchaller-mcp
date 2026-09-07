"""Markdown rendering for gojobs.gov.on.ca results."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

from ..jobfilter import counts_line
from ..security.xss import is_safe_markdown_link

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")
_MAX_FIELD_CHARS = 400
_MAX_BODY_CHARS = 20000

# Where the posting's copy stops. Its start is decided in ``api.split_job_page``
# — the same rule that closes the facts block — so the renderer only has to
# find the end.
_BODY_END_RE = re.compile(r'<(?:footer|div[^>]*class="[^"]*footer)', re.I)

# Fields worth promoting into the summary line of a search hit, in order.
_SUMMARY_FIELDS = ("organization", "location", "salary", "closing_date")

_DETAIL_ORDER = (
    "Posting status",
    "Organization",
    "Division",
    "City",
    "Salary",
    "Job term",
    "Job code",
    "Category",
    "Position(s) language",
    "Apply By",
)


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, limit) for v in value]
        return " · ".join(p for p in parts if p)
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.find_all("a", href=True):
        if not is_safe_markdown_link(anchor.get("href")):
            # Preserve readable employer copy without exposing an executable
            # or embedded-data URI to the consuming Markdown renderer.
            anchor.unwrap()
    md = markdownify(
        str(soup.body or soup),
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
    )
    return _BLANK_LINE_COLLAPSE_RE.sub("\n\n", md).strip()


def job_body_markdown(body_html: str) -> str:
    """The posting's own copy, as markdown."""
    if not body_html:
        return ""
    end = _BODY_END_RE.search(body_html)
    if end:
        body_html = body_html[: end.start()]
    return _html_to_markdown(body_html)[:_MAX_BODY_CHARS]


def render_search_results(
    jobs: list[dict],
    *,
    title: str = "",
    location: str = "",
    category: str = "",
    career_level: str = "",
    min_salary: str = "",
    board_total: int = 0,
    title_filtered: int = 0,
    location_filtered: int = 0,
    truncated_by_limit: int = 0,
    examined: int = 0,
    board_scope: str = "",
    notes: list[str] | None = None,
) -> str:
    salary_scope = ""
    if min_salary:
        try:
            salary_scope = f"minimum salary: ${int(min_salary):,}+"
        except (TypeError, ValueError):
            salary_scope = f"minimum salary: {_clean(min_salary)}"
    scope = " · ".join(
        p
        for p in (
            f"“{_clean(title)}”" if title else "",
            _clean(location),
            f"category: {_clean(category)}" if category else "",
            f"career level: {_clean(career_level)}" if career_level else "",
            salary_scope,
        )
        if p
    )
    lines = [f"# Ontario Public Service jobs{': ' + scope if scope else ''}", ""]

    count_scope = "; ".join(
        part
        for part in (
            _clean(board_scope),
            f"in category “{_clean(category)}”" if category else "",
            f"at career level “{_clean(career_level)}”" if career_level else "",
            f"at ${int(min_salary):,}+" if min_salary and min_salary.isdigit() else "",
        )
        if part
    )

    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            dropped_by_location=location_filtered,
            board_total=board_total,
            board_label="The OPS board",
            board_scope=count_scope,
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
        summary = [
            _clean(job.get(field)) for field in _SUMMARY_FIELDS if job.get(field)
        ]
        if summary:
            lines.append(" · ".join(summary))
        lines.append(f"Job ID: {_clean(job.get('job_id'))}")

    return "\n".join(lines)


def render_job(job: dict) -> str:
    """One posting in full."""
    lines = [f"# {_clean(job.get('title')) or 'Ontario Public Service posting'}", ""]

    # A filled or closed competition still reports "Posting status: Open", so
    # the competition block goes first and unmissably. Rendering the status
    # field alone would present a finished recruitment as a live opening.
    status = job.get("competition_status") or ""
    if status:
        lines.extend([f"> **Competition status:** {_clean(status, 300)}", ""])

    fields = job.get("fields") or {}
    for label in _DETAIL_ORDER:
        value = fields.get(label)
        if value:
            lines.append(f"- **{label}:** {_clean(value)}")
    lines.append(f"- **URL:** {job.get('url', '')}")

    body = job_body_markdown(job.get("body_html") or "")
    if body:
        lines.extend(["", "---", "", body])

    return "\n".join(lines)
