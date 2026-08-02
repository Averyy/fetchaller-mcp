"""Oracle Recruiting Cloud (ORC) career sites.

ORC is Oracle Fusion's candidate-experience recruiting module, used by a wide
set of large employers — Uber among them. Every tenant serves the same two
REST resources from its own Fusion host, unauthenticated:

- ``GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions``
  ``?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={site},limit=N``
- ``GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails``
  ``?expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={site}``

No cookie, token, or referer is required on either. The search resource is
unusual in shape: it returns a single "search" object whose ``requisitionList``
holds the postings and whose ``TotalJobsCount`` is the real total — and the
list is omitted entirely unless ``expand=requisitionList`` is passed.
"""

from .search import get_oracle_job, search_oracle_jobs

__all__ = ["get_oracle_job", "search_oracle_jobs"]
