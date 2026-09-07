"""gojobs.gov.on.ca — Ontario Public Service Careers.

An ASP.NET WebForms board. Everything interesting about it follows from that:
the listing is reachable only by POSTing the page's own ``__VIEWSTATE`` back,
paging is a postback rather than a URL, and the whole exchange is stateful.
``api`` owns that protocol, ``render`` the markdown, ``search`` the entry points.
"""

from .search import get_gojobs_job, search_gojobs

__all__ = ["get_gojobs_job", "search_gojobs"]
