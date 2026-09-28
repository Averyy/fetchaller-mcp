# TODO: New Reddit — zero gaps

> **Status: met in 3.8.2 (2026-09-28)**, apart from Reddit's own intermittent
> faults. Strict `scripts/reddit_parity.py --strict --include-unstable` runs:
> local 279/281 and container 280/281 checks passed; every failure was a Reddit
> fault that re-fetched clean (a 2xx "Server error" page, and a search page 2
> that came back empty once). Zero browser use in both, one anonymous app-token
> mint cold and none after recreation (wafer 0.7.0 app route). Only the eight
> inherently non-public access states stay fixture-only. The gate itself had
> been unpassable since 3.3.1 (session-audit format drift) and, in the
> container, since 2026-07-29 (startup Chrome egress); both are fixed. Container
> stdio smoke: 17/17 with every live gate armed.

Done means every Old Reddit public read still works without `old.reddit.com`.
No retired-feature waiver is allowed: routes, fields, links, media/comments,
cursors, access states, and failures must be preserved or recovered from real
evidence. Output may never omit, invent, or hide a gap.

Release only when:

- Docs, contract, router, corpus, schema, renderer, MCP fixtures, Ruff, and
  pytest all pass; archived collections use genuine Redux metadata and current
  post data.
- Every public route passes cold, warm, and recreated live gates with real IDs;
  only inherently non-public access states may remain fixture-only.
- Real stdio and container runs pass all tools, JSON/raw semantics, bounded
  output, failures, OAuth, persistence, readiness, browser, and restart gates.
