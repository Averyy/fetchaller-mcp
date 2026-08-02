"""Markdown rendering and pay-band extraction for amazon.jobs."""

from __future__ import annotations

import re

from markdownify import markdownify

from ..jobfilter import counts_line

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")
_MAX_FIELD_CHARS = 300

# Amazon appends its pay disclosure to the tail of `preferred_qualifications`,
# one line per location, e.g.
#   "CAN, ON, Toronto - 185,400.00 - 309,600.00 CAD annually"
#   "US, WA, Seattle - 151,300.00 - 261,500.00 USD annually"
_PAY_BAND_RE = re.compile(
    r"(?P<country>[A-Z]{2,3})\s*,\s*(?P<region>[A-Z]{2,3})\s*,\s*"
    r"(?P<city>[A-Za-z .'\-]{2,40}?)\s*-\s*"
    r"(?P<low>\d[\d,]*(?:\.\d{2})?)\s*-\s*(?P<high>\d[\d,]*(?:\.\d{2})?)\s*"
    r"(?P<currency>[A-Z]{3})\s*(?P<period>annually|hourly|monthly|weekly)?"
)


def _clean(value, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _money(low: str, high: str) -> tuple[str, str]:
    """Drop cents only when both ends have none.

    "185,400.00 - 309,600.00" reads better as "185,400-309,600", but an hourly
    band of "18.50 - 24.00" must not become "18.50-24" — mixing the two forms
    in one range reads as a different unit.
    """
    if low.endswith(".00") and high.endswith(".00"):
        return low[:-3], high[:-3]
    return low, high


def extract_pay_bands(job: dict) -> list[str]:
    """Pull every published pay band off a posting, most specific first.

    Returns strings like ``"Toronto, ON: 185,400-309,600 CAD annually"``.
    Amazon publishes these only where local law requires it, so an empty list
    means "not disclosed", not "unpaid".
    """
    seen: set[str] = set()
    bands: list[str] = []
    for field in ("preferred_qualifications", "basic_qualifications", "description"):
        text = job.get(field) or ""
        if not isinstance(text, str) or "-" not in text:
            continue
        for match in _PAY_BAND_RE.finditer(text):
            city = match.group("city").strip()
            region = match.group("region")
            period = match.group("period") or ""
            low, high = _money(match.group("low"), match.group("high"))
            band = (
                f"{city}, {region}: {low}-{high} {match.group('currency')}"
                f"{' ' + period if period else ''}"
            )
            if band not in seen:
                seen.add(band)
                bands.append(band)
    return bands


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


def job_url(job: dict) -> str:
    path = job.get("job_path") or ""
    return f"https://www.amazon.jobs{path}" if path.startswith("/") else ""


def render_search_results(
    jobs: list[dict],
    *,
    title: str = "",
    location: str = "",
    country: str = "",
    job_category: str = "",
    hits: int = 0,
    title_filtered: int = 0,
    location_filtered: int = 0,
) -> str:
    # Every filter that shaped the result belongs in the heading. A category
    # search with no title rendered as a bare "# Amazon jobs", which reads as
    # the whole board.
    scope = " · ".join(
        p
        for p in (
            f"“{_clean(title)}”" if title else "",
            _clean(job_category),
            _clean(location) or _clean(country),
        )
        if p
    )
    lines = [f"# Amazon jobs{': ' + scope if scope else ''}", ""]

    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            dropped_by_location=location_filtered,
            board_total=hits,
            board_label="Amazon's board",
        )
    )
    lines.append("")

    if not jobs:
        lines.append("No postings matched.")
        return "\n".join(lines) + "\n"

    for index, job in enumerate(jobs, start=1):
        name = _clean(job.get("title")) or "(untitled)"
        url = job_url(job)
        lines.append(f"## {index}. [{name}]({url})" if url else f"## {index}. {name}")
        for label, key in (
            ("Location", "normalized_location"),
            ("Category", "job_category"),
            ("Company", "company_name"),
            ("Posted", "posted_date"),
        ):
            value = _clean(job.get(key))
            if value:
                lines.append(f"- **{label}**: {value}")
        req = _clean(job.get("id_icims") or job.get("id"))
        if req:
            lines.append(f"- **Req ID**: {req}")
        bands = extract_pay_bands(job)
        if bands:
            lines.append(f"- **Pay**: {'; '.join(_clean(b) for b in bands[:4])}")
        summary = _clean(job.get("description_short"), 400)
        if summary:
            lines.append(f"- {summary}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_job(job: dict) -> str:
    name = _clean(job.get("title")) or "Job Posting"
    lines = [f"# {name}", ""]
    for label, key in (
        ("Location", "normalized_location"),
        ("Category", "job_category"),
        ("Job family", "job_family"),
        ("Company", "company_name"),
        ("Posted", "posted_date"),
        ("Updated", "updated_time"),
        ("Schedule", "job_schedule_type"),
    ):
        value = _clean(job.get(key))
        if value:
            lines.append(f"- **{label}**: {value}")
    req = _clean(job.get("id_icims") or job.get("id"))
    if req:
        lines.append(f"- **Req ID**: {req}")
    bands = extract_pay_bands(job)
    if bands:
        lines.append(f"- **Pay**: {'; '.join(_clean(b) for b in bands[:6])}")
    lines.append("")

    for heading, key in (
        ("Description", "description"),
        ("Basic qualifications", "basic_qualifications"),
        ("Preferred qualifications", "preferred_qualifications"),
    ):
        body = _html_to_markdown(job.get(key) or "")
        if body:
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(body)
            lines.append("")

    url = job_url(job)
    if url:
        lines.append(f"**Source**: {url}")
    return "\n".join(lines).rstrip() + "\n"
