"""Markdown rendering for LinkedIn guest results."""

from __future__ import annotations

from .parse import _MARKDOWN_ESCAPE, JobCard, JobDetail

_DESCRIPTION_BUDGET_RATIO = 0.75


def _truncate(text: str, limit: int) -> str:
    # A non-positive budget means "no room", not "no limit" — returning the
    # whole string there is how a bound becomes a no-op.
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def render_search_results(
    cards: list[JobCard],
    *,
    keywords: str = "",
    location: str = "",
    geo_id: str | None = None,
    start: int = 0,
    max_tokens: int = 25_000,
) -> str:
    # keywords/location are caller-supplied and land in a markdown heading.
    safe_keywords = keywords.translate(_MARKDOWN_ESCAPE)
    safe_location = location.translate(_MARKDOWN_ESCAPE)
    safe_geo = (geo_id or "").translate(_MARKDOWN_ESCAPE)
    scope = " · ".join(
        part
        for part in (
            f"“{safe_keywords}”" if safe_keywords else "",
            safe_location or (f"geoId {safe_geo}" if safe_geo else ""),
        )
        if part
    )
    lines = [f"# LinkedIn jobs{': ' + scope if scope else ''}", ""]
    plural = "" if len(cards) == 1 else "s"
    offset_note = f", from result {start + 1}" if start else ""
    lines.append(f"_{len(cards)} job{plural}{offset_note}_")
    lines.append("")

    for index, card in enumerate(cards, start=1):
        heading = card.title or "(untitled)"
        if card.company:
            heading += f" — {card.company}"
        lines.append(f"{index}. **{heading}**")

        meta = [part for part in (card.location, card.posted_label) if part]
        if card.badges:
            meta.append(" · ".join(card.badges))
        if card.posted_date and card.posted_date not in card.posted_label:
            meta.append(card.posted_date)
        if meta:
            lines.append(f"   {' · '.join(meta)}")
        if card.url:
            lines.append(f"   {card.url}")
        if card.company_url:
            lines.append(f"   Company: {card.company_url}")
        lines.append("")

    lines.append(
        "_Salary is not published in LinkedIn's logged-out results, so none is shown. "
        "Use get_linkedin_job(job_id) for the full description._"
    )
    return _truncate("\n".join(lines).rstrip() + "\n", max_tokens * 4)


def render_job_detail(detail: JobDetail, *, max_tokens: int = 25_000) -> str:
    lines = [f"# {detail.title or '(untitled)'}", ""]
    if detail.company:
        lines.append(f"**{detail.company}**")
    header = [part for part in (detail.location, detail.posted_label, detail.applicants) if part]
    if header:
        lines.append(" · ".join(header))
    if detail.url:
        lines.append("")
        lines.append(detail.url)
    if detail.company_url:
        lines.append(f"Company: {detail.company_url}")

    if detail.criteria:
        lines.extend(["", "## Details", ""])
        for key, value in detail.criteria.items():
            lines.append(f"- **{key}:** {value}")

    total_chars = max_tokens * 4
    if detail.description:
        lines.extend(["", "## Description", ""])
        used = len("\n".join(lines))
        budget = max(0, int(total_chars * _DESCRIPTION_BUDGET_RATIO) - used)
        lines.append(_truncate(detail.description, budget))

    lines.extend(
        [
            "",
            "_Applying requires a LinkedIn account; the public posting does not "
            "expose an apply link._",
        ]
    )
    return _truncate("\n".join(lines).rstrip() + "\n", total_chars)
