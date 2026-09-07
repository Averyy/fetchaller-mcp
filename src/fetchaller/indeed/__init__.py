"""ca.indeed.com — job search, and the posting bodies other boards gate.

Indeed's value here is that it is a *mirror*: UKG and PeopleSoft serve a list
fine and gate the posting body behind a session or an "unsupported browser"
check, and Indeed carries that body in plain HTML with a structured salary band
and an expiry date attached. See ``api`` for why a search costs exactly two
requests.
"""

from .search import get_indeed_job, search_indeed

__all__ = ["get_indeed_job", "search_indeed"]
