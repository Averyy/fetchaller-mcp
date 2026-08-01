"""jobs.uber.com search.

Uber's board runs on Oracle Recruiting Cloud, so this package is a thin
adapter over ``fetchaller.oracle_recruiting`` plus the URL grammar the fetch
tool needs. Postings carry their full text, unlike Uber's older in-house
endpoint — see ``search.py`` for why that one was dropped.
"""

from .search import get_uber_job, search_uber_jobs

__all__ = ["get_uber_job", "search_uber_jobs"]
