"""Markdown rendering for GC Jobs results."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

from ..jobfilter import counts_line
from ..security.xss import is_safe_markdown_link

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")
_MAX_FIELD_CHARS = 400
_MAX_BODY_CHARS = 30000

_SUMMARY_FIELDS = ("organization", "location", "language", "salary")

# Left-column facts of a posting, in reading order. "Who can apply" is
# rendered separately and first — see render_job.
_FACT_ORDER = (
    "Location",
    "Salary",
    "Positions to be filled",
    "Language requirements",
    "Reference number",
    "Selection process number",
)

# Page furniture inside the posting body that carries nothing: the
# "On this page" jump list, back-to-top blocks, Apply buttons, section
# anchors. The spacer div is the posting's own paragraph break.
_BODY_NOISE_RE = re.compile(
    r'<h2 class="h2NoSpace">On this page</h2>\s*<ul class="list-unstyled">.*?</ul>'
    r'|<div class="bottomSpace">\s*<a href="#backToTop".*?</div>'
    r'|<a\s+href="/psrs-srfp/applicant/page1710[^"]*"[^>]*>\s*Apply\s*</a>'
    r'|<a id="somcAnchor\d+"\s*></a>'
    r'|<div id="backToTop">\s*</div>'
    r'|<div class="bottom-space-before-apply"></div>',
    re.S | re.I,
)
_SPACER_RE = re.compile(r'<div class="spacer"></div>', re.I)
_HR_EMPTY_RE = re.compile(r'<hr class="hr-empty-line">', re.I)
# Where the posting's copy stops.
_BODY_END_RE = re.compile(r'<div data-gc-analytics-pageid|<dl id="wb-dtmd">', re.I)

# Eligibility phrasing that should be surfaced as a restriction. The board
# writes the open case as "Persons residing in Canada, and Canadian citizens
# and Permanent residents abroad."
_RESTRICTED_RE = re.compile(
    r"employees|public servants|residing within|residing in [A-Z][^,.]*(?:region|radius)"
    r"|enrolled under|members of|only",
    re.I,
)


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _html_to_markdown(fragment: str) -> str:
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "lxml")
    for anchor in soup.find_all("a", href=True):
        if not is_safe_markdown_link(anchor.get("href")):
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
    """The posting's own sections, as markdown."""
    if not body_html:
        return ""
    end = _BODY_END_RE.search(body_html)
    if end:
        body_html = body_html[: end.start()]
    body_html = _BODY_NOISE_RE.sub("", body_html)
    body_html = _HR_EMPTY_RE.sub("", body_html)
    body_html = _SPACER_RE.sub("<br><br>", body_html)
    return _html_to_markdown(body_html)[:_MAX_BODY_CHARS]


def render_search_results(
    jobs: list[dict],
    *,
    title: str = "",
    location: str = "",
    organization: str = "",
    min_salary: int = 0,
    language: str = "",
    board_total: int = 0,
    title_filtered: int = 0,
    location_filtered: int = 0,
    salary_filtered: int = 0,
    truncated_by_limit: int = 0,
    examined: int = 0,
    exhausted: bool = False,
    board_scope: str = "",
    notes: list[str] | None = None,
) -> str:
    scope = " · ".join(
        p
        for p in (
            f"“{_clean(title)}”" if title else "",
            _clean(location),
            f"organization: {_clean(organization)}" if organization else "",
            f"minimum salary: ${min_salary:,}" if min_salary else "",
            f"language: {_clean(language)}" if language else "",
        )
        if p
    )
    lines = [f"# GC Jobs (federal public service){': ' + scope if scope else ''}", ""]

    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            dropped_by_location=location_filtered,
            dropped_by_salary=salary_filtered,
            board_total=board_total,
            board_label="GC Jobs",
            board_scope=_clean(board_scope),
            truncated_by_limit=truncated_by_limit,
            examined=examined,
            exhausted=exhausted,
        )
    )

    for note in notes or []:
        lines.extend(["", f"_{note}_"])

    if not jobs:
        lines.extend(["", "No matching postings."])
        return "\n".join(lines)

    # Eligibility is the most common reason a federal posting is useless to
    # the person reading it, and it is not on the listing. Said once, up
    # front, rather than discovered per posting.
    lines.extend(
        [
            "",
            "_Rows are in the board's order, soonest closing first. Eligibility is "
            "not on the listing: many federal processes are open only to current "
            "public servants, to Canadian citizens, or to people living in a named "
            "area. `get_gcjobs_job` reports each posting's “Who can apply” line, and "
            "says when a posting is hosted on the organization's own site instead._",
        ]
    )

    for job in jobs:
        lines.append("")
        lines.append(f"### [{_clean(job.get('title'))}]({job.get('url', '')})")
        summary = [_clean(job.get(field)) for field in _SUMMARY_FIELDS if job.get(field)]
        if job.get("closing_date"):
            summary.append(f"closes {_clean(job['closing_date'])}")
        if summary:
            lines.append(" · ".join(summary))
        if job.get("note"):
            lines.append(f"_{_clean(job['note'])}_")
        lines.append(f"Poster ID: {_clean(job.get('poster_id'))}")

    return "\n".join(lines)


def render_external(job: dict) -> str:
    """A posting GC Jobs hosts elsewhere: say so, and where."""
    title = _clean(job.get("title")) or "GC Jobs posting"
    lines = [f"# {title}", ""]
    lines.append(
        "> **Hosted outside GC Jobs.** The hiring organization advertises this "
        "process on its own site, and GC Jobs serves only a departure notice for "
        "it — no closing date, location, salary or eligibility is published here."
    )
    lines.append("")
    external = job.get("external_url") or ""
    if external:
        lines.append(f"- **Posting:** {external}")
        lines.append("- Read it with `fetch`.")
    else:
        lines.append("- The departure notice carried no outbound link.")
    lines.append(f"- **GC Jobs notice:** {job.get('url', '')}")
    return "\n".join(lines)


def render_job(job: dict) -> str:
    """One posting in full, eligibility and closure first."""
    lines = [f"# {_clean(job.get('title')) or 'GC Jobs posting'}", ""]

    closing_text = job.get("closing_text") or ""
    if job.get("closed"):
        # The board serves two-decade-old postings at low poster ids as
        # though live; nothing on the page says "archived".
        lines.extend(
            [
                f"> **Closed.** This posting's closing date was {_clean(closing_text, 200)}; "
                "it is not accepting applications.",
                "",
            ]
        )

    facts = dict(job.get("facts") or {})
    who = facts.pop("Who can apply", "")
    if who:
        flag = " ⚠" if _RESTRICTED_RE.search(who) else ""
        lines.extend([f"> **Who can apply:**{flag} {_clean(who)}", ""])
    elif job.get("kind") == "posting":
        lines.extend(["> **Who can apply:** not stated on the posting.", ""])

    if job.get("organization"):
        lines.append(f"- **Organization:** {_clean(job['organization'])}")
    if closing_text:
        lines.append(f"- **Closing date:** {_clean(closing_text)}")
    for label in _FACT_ORDER:
        value = facts.pop(label, "")
        if value:
            lines.append(f"- **{label}:** {_clean(value)}")
    if job.get("classification"):
        lines.append(f"- **Classification:** {_clean(job['classification'])}")
    for label, value in facts.items():
        if value:
            lines.append(f"- **{label}:** {_clean(value)}")
    lines.append(f"- **URL:** {job.get('url', '')}")

    body = job_body_markdown(job.get("body_html") or "")
    if body:
        lines.extend(["", "---", "", body])

    return "\n".join(lines)
