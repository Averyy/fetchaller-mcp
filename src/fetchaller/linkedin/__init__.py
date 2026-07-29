"""LinkedIn public guest job search.

Covers only the logged-out ``/jobs-guest/`` endpoints LinkedIn serves to
visitors with no account: the search fragment, the GEO typeahead, and the job
detail fragment. Nothing here authenticates, and no apply flow is followed —
the detail fragment shows that an apply button exists but never exposes an
unauthenticated apply URL, so that boundary is where this stops.
"""

from .search import get_linkedin_job, search_linkedin_jobs

__all__ = ["get_linkedin_job", "search_linkedin_jobs"]
