"""jobs.uber.com search.

Uber runs its own ATS rather than a third-party one (the SmartRecruiters
"uber" tenant is an unrelated one-req stub). Its board is driven by a single
anonymous endpoint:

``POST https://www.uber.com/api/loadSearchJobsResults?localeCode=en`` with
``{"params": {"location": [{"country": "CAN", "city": "Toronto"}], "query": "..."},
"page": 0, "limit": 100}``.

An ``x-csrf-token`` header is required to be present but its value is never
validated. Counts come back as a Long triple ``{"low": N, "high": 0}``, and the
whole global board is only a few hundred reqs, so a filtered search is pulled
in full and matched exactly rather than trusting the ranking.
"""

from .search import get_uber_job, search_uber_jobs

__all__ = ["get_uber_job", "search_uber_jobs"]
