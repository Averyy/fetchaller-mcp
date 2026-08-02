"""amazon.jobs search.

Amazon runs its own board rather than a third-party ATS, and serves the same
JSON its site uses from ``/en/search.json`` with no auth. Two things make it
worth a dedicated client:

- **Its search is fuzzy to a fault.** ``base_query="product designer"`` in
  Toronto returns a Software Development Engineer req. Title and location are
  both re-applied here against the posting's own fields.
- **It publishes pay.** Canadian reqs carry an inline band
  ("CAN, ON, Toronto - 185,400.00 - 309,600.00 CAD annually") at the tail of
  ``preferred_qualifications``, which this module lifts into a real field.
"""

from .search import get_amazon_job, search_amazon_jobs

__all__ = ["get_amazon_job", "search_amazon_jobs"]
