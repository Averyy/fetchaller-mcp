"""Public entry points: ``search_linkedin_jobs`` and ``get_linkedin_job``.

Filter values below are the ones confirmed live against the logged-out filter
form. Two things are deliberately NOT offered:

- ``sortBy``. Relevance, date, and an invalid value all returned the identical
  ordered sequence, so the endpoint's sort is unverified. Offering it would
  promise ordering we cannot show it honours; ``recent`` sorts the fetched
  window client-side by the card's own ``datetime`` instead.
- Job types ``V``/``O``. Both are accepted syntactically, but sampled postings
  came back as Full-time, so their meaning was never established.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from ..security.xss import safe_log_text
from . import api
from .parse import parse_job_detail, parse_search_fragment
from .render import render_job_detail, render_search_results

DATE_POSTED = {
    "any": "",
    "24h": "r86400",
    "week": "r604800",
    "month": "r2592000",
}
WORKPLACE = {"on_site": "1", "remote": "2", "hybrid": "3"}
EXPERIENCE = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}
JOB_TYPE = {
    "full_time": "F",
    "part_time": "P",
    "contract": "C",
    "temporary": "T",
    "internship": "I",
}
MIN_SALARY = {40000: "21", 60000: "22", 80000: "23", 100000: "24", 120000: "25"}
# Boolean filters, labelled by LinkedIn's own logged-out filter bar.
#
# Verified 2026-07-29 against each returned posting's detail fragment:
#   f_AL - 5/5 results carried an Easy Apply button and no off-site apply link;
#          the unfiltered baseline carried the button 0/5.
#   f_EA - 5/5 results read "Be among the first 25 applicants" against a
#          baseline of "Over 200 applicants". LinkedIn labels this "Under 10
#          applicants"; the public detail bands counts at 25, so the exact
#          threshold is not independently observable — what is proven is that
#          it selects low-applicant postings, which is the point of it.
EASY_APPLY_PARAM = "f_AL"
FEW_APPLICANTS_PARAM = "f_EA"
SORT = ("relevance", "recent")

MAX_LIMIT = 25


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] linkedin: {safe_log_text(msg)}",
        file=sys.stderr,
    )


def _build_params(
    keywords: str,
    location: str,
    geo_id: str | None,
    date_posted: str,
    workplace: str | None,
    experience: str | None,
    job_type: str | None,
    min_salary: int | None,
    start: int,
    easy_apply: bool = False,
    under_10_applicants: bool = False,
) -> dict[str, object]:
    params: dict[str, object] = {"start": start}
    if keywords:
        params["keywords"] = keywords
    if geo_id:
        # geoId wins over conflicting location text, so send one or the other.
        params["geoId"] = geo_id
    elif location:
        params["location"] = location
    if DATE_POSTED.get(date_posted):
        params["f_TPR"] = DATE_POSTED[date_posted]
    if workplace:
        params["f_WT"] = WORKPLACE[workplace]
    if experience:
        params["f_E"] = EXPERIENCE[experience]
    if job_type:
        params["f_JT"] = JOB_TYPE[job_type]
    if min_salary:
        params["f_SB2"] = MIN_SALARY[min_salary]
    if easy_apply:
        params[EASY_APPLY_PARAM] = "true"
    if under_10_applicants:
        params[FEW_APPLICANTS_PARAM] = "true"
    return params


def _validate(
    date_posted: str,
    workplace: str | None,
    experience: str | None,
    job_type: str | None,
    min_salary: int | None,
    sort: str,
    start: int,
    limit: int,
) -> str | None:
    if date_posted not in DATE_POSTED:
        return f"date_posted must be one of: {', '.join(sorted(DATE_POSTED))}"
    if workplace is not None and workplace not in WORKPLACE:
        return f"workplace must be one of: {', '.join(sorted(WORKPLACE))}"
    if experience is not None and experience not in EXPERIENCE:
        return f"experience must be one of: {', '.join(sorted(EXPERIENCE))}"
    if job_type is not None and job_type not in JOB_TYPE:
        return f"job_type must be one of: {', '.join(sorted(JOB_TYPE))}"
    if min_salary is not None and min_salary not in MIN_SALARY:
        return f"min_salary must be one of: {', '.join(str(v) for v in sorted(MIN_SALARY))}"
    if sort not in SORT:
        return f"sort must be one of: {', '.join(SORT)}"
    if not isinstance(start, int) or not 0 <= start <= api.MAX_START:
        return f"start must be an integer from 0 to {api.MAX_START}"
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        return f"limit must be an integer from 1 to {MAX_LIMIT}"
    return None


async def search_linkedin_jobs(
    keywords: str = "",
    location: str = "",
    *,
    geo_id: str | None = None,
    date_posted: str = "any",
    workplace: str | None = None,
    experience: str | None = None,
    job_type: str | None = None,
    min_salary: int | None = None,
    easy_apply: bool = False,
    under_10_applicants: bool = False,
    sort: str = "relevance",
    start: int = 0,
    limit: int = 10,
    max_tokens: int = 25_000,
    timeout: float = 45.0,
    browser_solver=None,
) -> dict:
    """Search LinkedIn's public job board. Returns rendered markdown or an error."""
    error = _validate(date_posted, workplace, experience, job_type, min_salary, sort, start, limit)
    if error:
        return {"error": error}
    keywords = (keywords or "").strip()
    location = (location or "").strip()
    if not keywords and not location and not geo_id:
        return {"error": "Provide keywords, a location, or a geo_id."}

    deadline = asyncio.get_running_loop().time() + timeout
    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            resolved_geo = geo_id
            if not resolved_geo and location:
                resolved_geo = await api.resolve_geo_id(
                    location, session=session, timeout=timeout
                )

            collected = []
            seen: set[str] = set()
            offset = start

            # One JSERP page request returns 60 cards; the fragment endpoint
            # returns 10. At start=0 that covers any allowed limit in a single
            # request instead of up to three, which matters at a 3.2s floor.
            # The page ignores `start`, so it is only usable for the first page.
            if start == 0:
                base_params = _build_params(
                    keywords, location, resolved_geo, date_posted,
                    workplace, experience, job_type, min_salary, 0,
                    easy_apply, under_10_applicants,
                )
                page_html = await api.fetch_search_page(
                    base_params, session=session, timeout=timeout
                )
                for card in parse_search_fragment(page_html):
                    key = card.job_id or card.url
                    if key and key not in seen:
                        seen.add(key)
                        collected.append(card)
                if len(collected) >= limit:
                    offset = api.MAX_START + 1  # satisfied; skip the fragment loop
                else:
                    offset = len(collected)

            while len(collected) < limit and offset <= api.MAX_START:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                params = _build_params(
                    keywords, location, resolved_geo, date_posted,
                    workplace, experience, job_type, min_salary, offset,
                    easy_apply, under_10_applicants,
                )
                fragment = await api.fetch_search_fragment(
                    params, session=session, timeout=remaining
                )
                page = parse_search_fragment(fragment)
                if not page:
                    break
                for card in page:
                    key = card.job_id or card.url
                    if key and key not in seen:
                        seen.add(key)
                        collected.append(card)
                # A short page is the end of the result set.
                if len(page) < api.page_size():
                    break
                offset += api.page_size()

            if not collected:
                return {
                    "content": (
                        "No LinkedIn jobs matched. Try broader keywords, a wider "
                        "date_posted window, or removing filters."
                    ),
                    "content_type": "text",
                }

            if sort == "recent":
                # Client-side: the endpoint's own sort is unverified.
                collected.sort(key=lambda card: card.posted_date or "", reverse=True)

            _log(f"search '{keywords[:40]}' -> {len(collected)} jobs")
            return {
                "content": render_search_results(
                    collected[:limit],
                    keywords=keywords,
                    location=location,
                    geo_id=resolved_geo,
                    start=start,
                    max_tokens=max_tokens,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"LinkedIn search timed out after {timeout:.0f}s."}
    except api.LinkedInBlockedError:
        return {"error": "LinkedIn declined the request. Slow down and retry later."}
    except api.LinkedInUnavailableError:
        return {"error": "LinkedIn is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        # Only the type: wafer exceptions embed the full request URL, which
        # carries the caller's query and filters.
        return {"error": f"LinkedIn search failed ({type(exc).__name__})."}


async def get_linkedin_job(
    job_id: str,
    *,
    max_tokens: int = 25_000,
    timeout: float = 30.0,
    browser_solver=None,
) -> dict:
    """Full public detail for one LinkedIn posting."""
    # Same range url.py accepts when recognising a permalink, so a URL that
    # fetch() maps and an ID passed straight to the tool cannot disagree.
    if not job_id or not job_id.isdigit() or not 6 <= len(job_id) <= 20:
        return {"error": "job_id must be a LinkedIn numeric job ID (6-20 digits)."}
    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            html = await api.fetch_job_detail(job_id, session=session, timeout=timeout)
            if html is None:
                return {"error": f"LinkedIn job {job_id} was not found (it may be closed)."}
            detail = parse_job_detail(html, job_id)
            if detail is None:
                return {"error": f"LinkedIn returned no readable detail for job {job_id}."}
            return {
                "content": render_job_detail(detail, max_tokens=max_tokens),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"LinkedIn job fetch timed out after {timeout:.0f}s."}
    except api.LinkedInBlockedError:
        return {"error": "LinkedIn declined the request. Slow down and retry later."}
    except api.LinkedInUnavailableError:
        return {"error": "LinkedIn is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"LinkedIn job fetch failed ({type(exc).__name__})."}
