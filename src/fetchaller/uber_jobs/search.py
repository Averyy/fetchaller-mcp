"""Public entry points: ``search_uber_jobs`` and ``get_uber_job``.

Uber runs on Oracle Recruiting Cloud, so this module is a thin adapter over
``oracle_recruiting`` rather than a client of its own. See that package for the
transport.

It previously spoke Uber's older in-house endpoint
(``POST /api/loadSearchJobsResults``). That endpoint still answers, but it is
strictly worse: it returns an empty string for every ``description``, ships
all-null location fields for many postings, and its
``uber.com/…/careers/list/{id}`` pages now redirect to ``jobs.uber.com``. The
Oracle API behind the current site returns the full posting text and real
location strings, so it replaced the old one outright.
"""

from __future__ import annotations

from ..oracle_recruiting.search import get_oracle_job, search_oracle_jobs

EMPLOYER = "uber"


async def search_uber_jobs(
    *,
    title: str = "",
    location: str = "",
    strict_title: bool = True,
    strict_location: bool = True,
    limit: int = 25,
    timeout: float = 90.0,
    browser_solver=None,
) -> dict:
    """Search Uber's job board, filtered by title and location."""
    return await search_oracle_jobs(
        EMPLOYER,
        title=title,
        location=location,
        strict_title=strict_title,
        strict_location=strict_location,
        limit=limit,
        timeout=timeout,
        browser_solver=browser_solver,
    )


async def get_uber_job(
    job_id: str,
    *,
    timeout: float = 60.0,
    browser_solver=None,
) -> dict:
    """Full detail for one Uber posting, description included."""
    return await get_oracle_job(
        EMPLOYER, job_id, timeout=timeout, browser_solver=browser_solver
    )
