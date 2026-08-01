"""Workday board search with location and title filtering.

``content.workday`` owns the transport and the URL grammar; this package adds
the filtering the raw board endpoint does not do, and the employer aliases that
save callers from having to know a tenant's site slug.

The two things Workday will not do for you:

- **searchText does not filter on every tenant.** Adobe returns its full 834
  postings for ``searchText="designer"``, merely reordered; NVIDIA does filter
  (2000 -> 1980). So the title is always re-applied client-side.
- **Facet names and values are per-tenant.** See ``content.workday`` for how
  the location facet is found structurally rather than by name.
"""

from .search import search_workday_jobs

__all__ = ["search_workday_jobs"]
