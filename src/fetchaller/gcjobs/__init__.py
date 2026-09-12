"""emploisfp-psjobs.cfp-psc.gc.ca — GC Jobs, the federal public service board.

A JSF application that builds its search form in JavaScript and loads the
results in a second request, so the page a plain fetch returns is a shell
saying "JavaScript must be enabled" over zero postings. ``api`` owns the
two-flag protocol that unlocks the results, ``render`` the markdown,
``search`` the entry points.
"""

from .search import get_gcjobs_job, search_gcjobs

__all__ = ["get_gcjobs_job", "search_gcjobs"]
