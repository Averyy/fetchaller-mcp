"""Google careers search.

Google runs its own board at ``google.com/about/careers/applications``. The
page is server-rendered, so a plain fetch returns readable text — but it
returns it with the sidebar chrome attached, only the first page, and with no
way to tell how many of Google's "matched" results actually match.

That last point is the reason this client exists. Google's free-text matching
is extremely loose: of the 20 results on page one of a "product designer"
search in Canada, exactly two had a title containing both words, and none
contained the literal word "designer". Reporting Google's own count as the
answer would be wrong by an order of magnitude.

Underneath the page is a JSON RPC — Google's internal BOQ ``batchexecute``
endpoint — which serves the same data structured, filtered, and paginated. It
answers anonymously. See ``api`` for the wire format.
"""

from .search import get_google_job, search_google_jobs

__all__ = ["get_google_job", "search_google_jobs"]
