"""jobs.apple.com search.

Apple's board is a React SPA with no usable JSON API: ``/api/v1/*`` answers
``401 User Unauthorized`` for the reference routes and ``200`` with zero
results for the search route unless the caller carries a page-issued token.

The server-rendered page, however, embeds the complete result set in
``window.__staticRouterHydrationData``, and every filter is expressible in the
query string:

- ``?search=`` — free-text title search.
- ``?location={slug}-{CODE}`` — e.g. ``toronto-TOR``, ``canada-CANC``.
- ``?page=`` — 1-indexed, 20 results per page.

So this client drives the public search URL and reads the hydration blob,
which is the same data the page renders from.
"""

from .search import get_apple_job, search_apple_jobs

__all__ = ["get_apple_job", "search_apple_jobs"]
