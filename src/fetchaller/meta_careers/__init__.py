"""metacareers.com search.

Meta's board is a Relay/Comet SPA. It talks to ``POST /graphql`` with persisted
queries, so a request needs three things and no account:

- ``lsd`` — a per-page CSRF token, published in the page as
  ``["LSD",[],{"token":"..."}]``.
- ``doc_id`` — the persisted-query id for the operation.
- ``variables`` — the search input.

``doc_id`` values rotate with Meta's deploys, so they are not treated as
constants: each is published in a JS bundle as
``__d("{OperationName}_candidate_portalRelayOperation", ... a.exports="{id}")``
and is rediscovered from there whenever the known id stops working.

Office names come from the board's own filter query and are spelled
"Vancouver, Canada" — not "Vancouver, BC", which silently matches nothing.
"""

from .search import get_meta_job, search_meta_jobs

__all__ = ["get_meta_job", "search_meta_jobs"]
