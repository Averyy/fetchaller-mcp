"""Public entry points: ``search_meta_jobs`` and ``get_meta_job``."""

from __future__ import annotations

import asyncio

from ..jobfilter import counts_line, filter_by_title, location_matches, tokens
from . import api

_MARKDOWN_ESCAPE = str.maketrans({ch: "\\" + ch for ch in "\\`*_[]()#<>|"})


def _clean(value, limit: int = 400) -> str:
    if isinstance(value, (list, tuple)):
        parts = [_clean(v, limit) for v in value]
        return " · ".join(p for p in parts if p)
    text = " ".join(str(value or "").split())
    return text[:limit].translate(_MARKDOWN_ESCAPE)


def _job_url(job: dict) -> str:
    job_id = job.get("id")
    return f"https://www.metacareers.com/jobs/{job_id}/" if job_id else ""


async def _resolve_offices(session, location: str) -> tuple[list[str], list[str]]:
    """Match a place name against Meta's office vocabulary.

    Returns ``(matched_offices, all_offices)``. Meta spells its offices
    "Vancouver, Canada"; "Vancouver, BC" matches nothing and returns silently
    empty, so the caller's wording is resolved rather than passed through.
    """
    offices = await api.fetch_offices(session)
    wanted = tokens(location)
    if not wanted:
        return [], offices
    matched = [o for o in offices if location_matches(o, wanted)]
    return matched, offices


def _render(
    jobs: list[dict],
    *,
    title: str,
    location: str,
    title_filtered: int,
    location_applied: bool,
    office_hint: str,
    total_before_filter: int,
) -> str:
    scope = " · ".join(p for p in (f"“{_clean(title)}”" if title else "", _clean(location)) if p)
    lines = [f"# Meta jobs{': ' + scope if scope else ''}", ""]

    lines.extend(
        counts_line(
            len(jobs),
            dropped_by_title=title_filtered,
            board_total=total_before_filter,
            board_label="Meta's board",
            board_scope=f"in {_clean(location)}" if location and location_applied else "",
        )
    )
    if location and not location_applied:
        lines.append("")
        lines.append(
            f"_Meta has no office matching “{_clean(location)}”, "
            "so the results are not location-filtered._"
        )
    lines.append("")

    if not jobs:
        lines.append("No postings matched.")
        if office_hint:
            lines.append("")
            lines.append(f"Offices Meta lists: {office_hint}")
        return "\n".join(lines) + "\n"

    for index, job in enumerate(jobs, start=1):
        name = _clean(job.get("title")) or "(untitled)"
        url = _job_url(job)
        lines.append(f"## {index}. [{name}]({url})" if url else f"## {index}. {name}")
        for label, key in (
            ("Location", "locations"),
            ("Team", "teams"),
            ("Sub-team", "sub_teams"),
        ):
            value = _clean(job.get(key))
            if value:
                lines.append(f"- **{label}**: {value}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


async def search_meta_jobs(
    *,
    title: str = "",
    location: str = "",
    teams: list[str] | None = None,
    remote_only: bool = False,
    strict_title: bool = True,
    sort_by_new: bool = False,
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search metacareers.com, filtered by title and office."""
    limit = max(1, min(int(limit or 25), 100))

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            offices: list[str] = []
            all_offices: list[str] = []
            if location:
                offices, all_offices = await _resolve_offices(session, location)
            location_applied = bool(offices) or not location

            search_input = api.build_search_input(
                query=title,
                offices=offices,
                teams=teams or [],
                is_remote_only=remote_only,
                sort_by_new=sort_by_new,
            )
            jobs, featured = await api.search_jobs(session, search_input)

            # Meta returns the whole matching set in one response rather than
            # paginating, so `all_jobs` is already complete. Featured jobs are
            # promotional and location-agnostic, so they are not merged in.
            total_before = len(jobs)

            # An office Meta did not recognise must still constrain the result.
            # Meta answers an unknown office with the UNFILTERED board, so
            # without this a "Narnia" search returns Shanghai and Menlo Park
            # postings under a heading naming Narnia.
            if location and not location_applied:
                wanted = tokens(location)
                jobs = [j for j in jobs if location_matches(" ".join(j.get("locations") or []), wanted)]

            dropped = 0
            if strict_title and title:
                jobs, dropped = filter_by_title(jobs, lambda j: j.get("title"), title)
            jobs = jobs[:limit]

            # Only offer the office list when the caller's wording matched no
            # office. When it did match, an empty result means Meta is simply
            # not hiring there, and listing every office would imply otherwise.
            hint = ""
            if not location_applied and all_offices:
                wanted = tokens(location)
                near = [o for o in all_offices if any(t in o.casefold() for t in wanted)]
                hint = ", ".join(_clean(o) for o in (near or all_offices)[:16])

            return {
                "content": _render(
                    jobs,
                    title=title,
                    location=location,
                    title_filtered=dropped,
                    location_applied=location_applied,
                    office_hint=hint,
                    total_before_filter=total_before,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"Meta careers search timed out after {timeout:.0f}s."}
    except api.MetaCareersBlockedError:
        return {"error": "Meta declined the request. Retry shortly."}
    except api.MetaCareersUnavailableError:
        return {"error": "Meta is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the request URL and query.
        return {"error": f"Meta careers search failed ({type(exc).__name__})."}


def _render_detail(detail: dict) -> str:
    title = _clean(detail.get("title")) or "Job Posting"
    lines = [f"# {title}", ""]
    # The detail page names teams `departments`/`internal_departments`; the
    # search query calls the same things `teams`/`sub_teams`.
    for label, keys in (
        ("Location", ("locations",)),
        ("Team", ("teams", "departments")),
        ("Sub-team", ("sub_teams", "internal_departments")),
        ("Posted", ("datePosted",)),
        ("Type", ("employmentType",)),
        ("Compensation", ("public_compensation",)),
    ):
        value = next((_clean(detail.get(k)) for k in keys if _clean(detail.get(k))), "")
        if value:
            lines.append(f"- **{label}**: {value}")
    job_id = detail.get("id")
    if job_id:
        lines.append(f"- **Req ID**: {_clean(job_id)}")
    lines.append("")

    summary = _clean(detail.get("job_description"), 4000)
    if summary:
        lines.append("## Description")
        lines.append("")
        lines.append(summary)
        lines.append("")

    for heading, key in (
        ("Responsibilities", "responsibilities"),
        ("Minimum qualifications", "minimum_qualifications"),
        ("Preferred qualifications", "preferred_qualifications"),
    ):
        value = detail.get(key)
        items: list[str] = []
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    items.append(_clean(entry.get("item") or entry.get("text")))
                else:
                    items.append(_clean(entry))
        elif value:
            items = [_clean(value, 4000)]
        items = [i for i in items if i]
        if items:
            lines.append(f"## {heading}")
            lines.append("")
            lines.extend(f"- {item}" for item in items)
            lines.append("")

    if job_id:
        lines.append(f"**Source**: https://www.metacareers.com/jobs/{job_id}/")
    return "\n".join(lines).rstrip() + "\n"


async def get_meta_job(
    job_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one Meta posting."""
    job_id = (job_id or "").strip()
    if not job_id.isdigit():
        return {"error": "job_id must be the numeric id from the posting URL."}
    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            detail = await api.fetch_job_detail(session, job_id)
            if detail is None:
                return {"error": f"Meta posting {job_id} was not found (it may be closed)."}
            return {"content": _render_detail(detail), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"Meta careers fetch timed out after {timeout:.0f}s."}
    except api.MetaCareersBlockedError:
        return {"error": "Meta declined the request. Retry shortly."}
    except api.MetaCareersUnavailableError:
        return {"error": "Meta is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"Meta careers fetch failed ({type(exc).__name__})."}
