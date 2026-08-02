# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support. Built-in web search.

## Architecture

fetchaller owns content processing + MCP tools. wafer (`~/code/wafer`) owns HTTP transport + anti-detection. fetchaller NEVER does bot solving, impersonation, or cookie management — if a site blocks requests, fix it in wafer. See `docs/architecture.md` for full details.

**Escalate to wafer only for active blocking** — bot detection, a WAF challenge
needing a solve, TLS rejection, clearance cookies, rate limiting. Anything that
is merely *finding* the right request — reading JS bundles, guessing an
endpoint, working out a required field, decoding a payload shape, telling one
JSON blob from another — is content analysis and must be built here. Before
writing anything down for wafer, ask: is this request being *refused*, or do I
just not know its shape yet? Only the first is wafer's. `src/fetchaller/discovery/`
exists for the second — see `docs/spa-discovery.md`. This boundary was settled
empirically: the discovery capability was built inside wafer, validated against
seven boards, and discarded because none of them needed a challenge solved
(`wafer-feedback.md` is the record).

### Reddit

Normal Reddit URLs use New Reddit's logged-out anonymous JSON path, except the
wiki page index, which first reads New Reddit's canonical SSR page tree and,
when that exact tree is unavailable or its exact anonymous route returns an
unstructured 403, posts `WikiPageRevisionsV2` to the fixed
`www.reddit.com/svc/shreddit/graphql` route using the same anonymous session's
`csrf_token` cookie. Public wiki parity must pass anonymously. fetchaller has
NO Reddit credential path at all -- no OAuth, no client ID/secret, no refresh
or access token -- and must never gain one. Routes Reddit serves only to a
logged-in account (exact moderator rosters, account-private vote activity)
return an explicit account-gated error and are covered offline as
`fixture_only`. Wafer >=0.4.6 owns verification/cookie persistence;
fetchaller owns strict URL mapping, SSR/API schema validation, and compact
rendering. Never add an Old Reddit fallback or copy wafer's
verification parser into this repo. Explicit `.json` stays raw JSON and
`raw=true` fetches canonical New Reddit HTML. Preserve Reddit's public score as
a score (not an upvote count); show `upvote_ratio` only when returned, and never
invent separate up/down vote counts.

### Job boards

Every job board ranks rather than filters: a title query returns adjacent roles
and a location query returns a radius. So the board's own filter is treated as
an optimisation and the client's filter as the guarantee — see
`src/fetchaller/jobfilter.py`, which every board client shares. Never report a
board's raw result count as if it were the filtered count, and always surface
how many postings a filter dropped rather than hiding the difference. Workday's
`searchText` in particular silently drops real matches on some tenants, so a
located slice is pulled whole and filtered here instead. fetchaller has NO
credentialed path to any board — all of them answer anonymously and must
continue to.

## Pre-Commit Rules

**ALWAYS run lint and tests before EVERY commit. No exceptions.**

```bash
.venv/bin/ruff check src/ tests/   # Lint (import sorting, style)
.venv/bin/python -m pytest tests/ -x -q   # Tests
```

If ruff fails, fix with `.venv/bin/ruff check --fix src/ tests/` and verify again. CI runs `uv run ruff check src/ tests/` — if you skip this locally, the push WILL fail.

## Testing Rules

**ALWAYS use wafer** (not `urllib`, `requests`, or `httpx`) for HTTP requests — wafer handles TLS fingerprinting and bot protection transparently.

**ALWAYS manually test** every new feature/site before committing. Unit tests alone are not sufficient. See `docs/testing.md` for full testing guide (writing tests, live testing, test organization).

## Development & Testing

**CRITICAL**: When testing changes to this MCP server, you MUST use the local version, not the production Docker image.

1. **Update MCP config** to use the local Python:
   ```json
   {
     "mcpServers": {
       "fetchaller": {
         "command": "/Users/avery/Code/fetchaller-mcp/.venv/bin/python",
         "args": ["-m", "fetchaller.main"]
       }
     }
   }
   ```
2. **Restart Claude Code** to reload the MCP server with local changes
3. **Test the changes** using the fetchaller tools

Do NOT test against the production version (Docker image from GHCR).

**The MCP server caches loaded module code.** Even with the local config, the running fetchaller process loaded `src/fetchaller/**/*.py` at Claude Code startup. New modules and edits do NOT take effect until you restart Claude Code (or otherwise restart the MCP server process). When live-testing changes inline, run them via `.venv/bin/python -c "..."` against the fresh source on disk to confirm the code is correct before restarting.

## Landing Page

`landing/` contains the static site deployed to fetchaller.com. Read `docs/design-style-guide.md` before any visual changes. Always invoke the `frontend-design` skill (`/frontend-design`) when making visual changes.

**`landing/llms.txt`** — LLM-readable project summary. **Keep this in sync when adding new tools, sites, or features.**

## Docs Reference

- `docs/architecture.md` — System design: fetchaller vs wafer boundary, content modules, search, HTTP transport
- `docs/site-apis.md` — Site-specific API clients: AliExpress MTop, Mouser/DigiKey, Kijiji GraphQL, Craigslist SAPI, Facebook Marketplace GraphQL, eBay search extraction, realtor.ca (api2 home search + SSR listings + `search_realtor` tool), wellfound.com (Next.js/Apollo startup jobs). Job-board APIs and embed/white-label detection for Ashby, Greenhouse, Lever, Gem, Dayforce, Cornerstone, Workday, BambooHR, JazzHR. Big-tech career boards: Eightfold (Microsoft/Netflix/PayPal, two API generations), Workday search filtering, amazon.jobs (incl. inline pay bands), Apple SSR hydration, Meta persisted GraphQL, Uber.
- `docs/spa-discovery.md` — SPA API discovery (`src/fetchaller/discovery/`): observing a page in a browser and replaying what it made, so an endpoint's shape never needs bundle archaeology again. Ranking (why coverage and record count are directly opposed), the oracle (why a 200 that means "malformed" is the core problem), minimization, mint steps, and the measured per-board results
- `docs/testing.md` — Test organization, writing tests, live testing rules, test URLs
