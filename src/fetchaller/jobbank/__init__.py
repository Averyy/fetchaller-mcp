"""jobbank.gc.ca — the federal Job Bank.

``api`` owns the city-code resolution and the search request, ``render`` the
markdown, ``search`` the entry point.
"""

from .search import search_jobbank

__all__ = ["search_jobbank"]
