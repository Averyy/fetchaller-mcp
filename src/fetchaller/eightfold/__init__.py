"""Eightfold AI career sites ("PCS-X").

Eightfold hosts the public career site for a growing set of large employers —
Microsoft, Netflix, and PayPal among them. Every tenant serves the same two
JSON routes off its own hostname, keyed by an Eightfold *group id* (the
``domain`` query parameter, e.g. ``microsoft.com``):

- ``GET /api/pcsx/search`` — the result list.
- ``GET /api/pcsx/position_details`` — one posting, description included.

Both answer anonymously: no cookie, no CSRF token, no referer. The group id is
published by every tenant page as ``window._EF_GROUP_ID``, so a board URL alone
is enough to talk to a tenant this module has never seen.
"""

from .search import get_eightfold_job, search_eightfold_jobs

__all__ = ["get_eightfold_job", "search_eightfold_jobs"]
