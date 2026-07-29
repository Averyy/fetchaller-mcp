# TODO: New Reddit — zero gaps

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
