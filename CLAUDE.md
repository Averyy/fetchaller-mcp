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

### Ubiquiti (ui.com)

A UniFi store page ships its price in the HTML but **none of its
specifications** — those render client-side from `__NEXT_DATA__` — so the plain
HTML path returns a product page that looks complete and silently has no specs
on it. Dispatch on the Next.js route (`__NEXT_DATA__["page"]`), never on URL
shape, because the store rewrites `/pro/category/...` onto the same route.
Two route traps, both of which fail by rendering something plausible: door
access and cameras file products under a **collection** route, and missing it
drops every spec section while still producing a page; and an unknown category
is a **soft 404** — the store answers 200 and rewrites onto its home route, so
falling through renders the storefront under a heading the caller supplied.
Report that instead. Quote
`minDisplayPriceWithSurcharges` as the price, since that is what the store
charges and displays; name the base only when it differs. Money is in minor
units scaled by the **currency's** exponent, not a flat 1/100 — the JP store
prices in whole yen, so dividing by 100 there is a silent 100x error with no
symptom. Both `minDisplay*` fields are the minimum across variants, so say
"from" when the variants disagree rather than presenting the cheapest as the
price. A spec section's
features are a flat list — group children are linked by `feature.parentId`, not
nesting — and an absent capability flag renders as `—` rather than vanishing,
because "no 6 GHz radio" and "unstated" must not look alike.

Installation guides carry **no readable text**: every word is outlined vector
art. Never render one as though it had been read — say what it is and reproduce
the pages via `get_unifi_manual`, which rebuilds them with PyMuPDF (already a
dependency; add none). `dl.ui.com` returns **200 with an app shell** for a slug
that has no guide, so a status code proves nothing — detect the bootstrap
markers, and report a missing guide against the URL the caller asked for, not
the redirect target. fetchaller has NO credentialed path to any ui.com property.

### Vacuum Wars (vacuumwars.com)

The site's **comparison tool is the whole dataset and the plain HTML path
returned none of it**. `/compare/<category>/` is an Alpine.js app whose empty
state ("No products found.", "No brand found.", "Accordion Title") renders as a
board with nothing on it rather than as a failed read — the trap is that it
looks like an answer. Every model is in fact inline in the page as
`window.vwProducts`, 91 fields each, carrying Vacuum Wars' **own lab
measurements** and the star scores they roll up into; nothing else on the site
publishes that in machine-readable form. Parse it and discard the shell. That is
content analysis, not blocking, so none of it is wafer's.

One request gets everything: the tool paginates **client-side** over that array,
so `/page/2/` re-serves the same payload and must never be fetched as though it
held more. Tested and merely-listed robots are two populations — roughly half
the models were never put through the lab — so count them separately and render a
missing measurement as `-`, never as blank, because "never tested" and "scored
nothing" must not look alike. Colour variants are the same robot listed twice
and collapse only when brand, base name **and every measurement** match; when
their prices disagree say **"from"** and quote the cheaper, exactly as ui.com's
`minDisplay*` fields require. Prices in the dataset are the tool's last cached
Amazon figure and drift from the live figure the article pages show, so label
them cached and never present them as the current price.

Never hardcode a total. The array is larger than one capture through this
repo's own fetch tool can hold and both hosts ignore `Range`, so no count taken
from a client is a total — only a floor. The server-side parse has no such
ceiling. `compare.vacuumwars.com` is the tool's own front end serving the
identical dataset with the array ~2 KB in rather than ~285 KB in, so it is the
cheaper source when a caller has that URL; every route there is the tool, so
decide on host for that one and on path for the main site.

The comparison tool is **robot vacuums only** — `/compare/cordless-vacuums/` is
a hard 404. Cordless, upright and carpet-cleaner data is article prose and
tables, which render fine; do not go looking for a dataset that isn't there.
Gate extraction on the `/compare/` path *and* the global being present, or a
review page that happens to carry it gets thrown away and re-rendered as a spec
table. A `/compare/` path with no readable dataset must say so.

Separately, the leaderboard card (`.vwx-pc`, on the Top 20 page and on every
review) renders each product **twice** — a collapsed row and the expanded panel
behind it — and both survive markdownify, so every model appeared three to four
times. Drop the collapsed `.vwx-row`; it carries nothing the panel lacks. Drop
`.vwx-chip-more` ("+2 more") **only** because the chips it reveals are already
in the DOM behind `nth-of-type` CSS — verify that before treating any other
"more" affordance as noise. fetchaller has NO credentialed path to vacuumwars.com.

### Job boards

Every job board ranks rather than filters: a title query returns adjacent roles
and a location query returns a radius. So the board's own filter is treated as
an optimisation and the client's filter as the guarantee — see
`src/fetchaller/jobfilter.py`, which every board client shares. Never report a
board's raw result count as if it were the filtered count, and always surface
how many postings a filter dropped rather than hiding the difference. Those are
two different pools and must never share a clause — `jobfilter.counts_line()`
renders both for every board, keeping `shown + dropped` reconcilable and giving
the board's own figure a separate, labelled sentence. `limit` sizes the output
and nothing else: the examined pool is a per-board `_EXAMINE_CEILING` constant,
never a multiple of `limit`, because deriving it from `limit` made the *answer*
depend on how many rows the caller asked for. Whatever a window could not
reach must be stated, never left implied. A tool's published
`maximum` must equal what the boundary validator accepts (`_TOOL_INTEGER_RANGES`
in `server.py`); advertising a bound and then rejecting it is worse than
publishing no bound. Workday's
`searchText` in particular silently drops real matches on some tenants, so a
located slice is pulled whole and filtered here instead. fetchaller has NO
credentialed path to any board — all of them answer anonymously and must
continue to.

**gojobs.gov.on.ca** is the limiting case of that doctrine, because the board
has **no keyword search at all** — its only text input is an exact Job ID, so
`title` is matched entirely here. It is ASP.NET WebForms, and every one of its
traps fails by rendering something plausible: a GET of `Search.aspx` returns the
search *form* with zero postings, so the generic fetch path shows an empty board
rather than an error; paging is a `__EVENTTARGET` postback signed by the
previous response's `__VIEWSTATE`, so pages are strictly sequential and ten rows
each; and a facet set to a bare `REGION-TRNT` is accepted and silently ignored
where `["REGION-TRNT"]` filters. Bound a result row by the **English** title
anchor, never the repeater id — a bilingual posting's second anchor carries the
same id and halves the row. `Search.aspx` serves **two different pages**: a session's
first GET carries all 984 cities, every GET after it silently omits the city
list while keeping every other facet, so the page looks complete. Re-fetching
the form per search therefore lost city resolution and quietly downgraded
"Toronto" to the region filter (board reported 35, not 32) and "Thunder Bay" to
no filter at all (127) — right postings, wrong reported scope. Read the
vocabulary once from the cold page and never cache a city-less one. A search is
also a *conversation* keyed to `PHPSESSID`, so keep the whole exchange behind
its lock; overlapping searches answer each other's state. Report `Competition Status` before `Posting status`,
because a filled competition still reads "Open". Treat "Ontario"/"Canada" as the
whole board, not a filter: no row names the province. Never use `/alljobs.aspx`
or `/ReadPDF.aspx` — `robots.txt` disallows both.

**jobbank.gc.ca** (federal Job Bank) has the same disease in two places: a
parameter that is accepted, ignored, and never complained about. A bare
`locationstring` returns the whole country (64,017) — the real filter is
`locationparam`/`mid` carrying the numeric `city_id` from the Solr autocomplete
at `/core/ta-cityprovsuggest_en/select`, so never send a location that could not
be resolved. `searchstring` is dropped for *some* terms: "driver" filters to 11
but "assistant" returned the unfiltered 424 byte-identical to no keyword, so
when a title is given, compare against the same location's keyword-less total
and suppress the board's count if they match. Read the result total from
`id="results-count"` only — every distance facet renders its own "N jobs found
in" badge and a text match returns the facet. Radius (`d`, default 50km) is a
real filter and is part of the answer: the board searches *near* a city, so
state the radius or nearby towns read as a broken location filter.

### robots.txt — settled, do not re-open

**fetchaller is a user-directed fetcher, not a crawler, and robots.txt is not
treated as a constraint on it.** A person asks for a specific page or a specific
search; the tool retrieves it on their behalf. That is not the activity the
exclusion protocol was written to govern, and Avery has confirmed this position
more than once.

This was re-litigated once, mid-build, and the Indeed client was gutted over it
— `get_indeed_job` deleted, pagination and radius stripped — before being
restored. Do not do that again. Specifically, `ca.indeed.com/robots.txt`
disallows `/viewjob?`, `/*&start=` and `/*radius=` for `User-agent: *`, and the
client uses all three deliberately.

The obligation that *does* apply is load: **be efficient, because fewer
well-chosen requests is the fair thing and the polite thing at once.** Prefer
the request that returns the most per call. Indeed is the worked example, including how it
went: a probe found `start=1` returned a disjoint all-organic page (thirty
postings for two requests), and on re-test every offset returned the login wall,
so the client fetches one page. Re-verify a finding in the session you will ship
before building on it. Keep the per-domain limiter honest, never loop blindly, and
stop when the marginal request stops paying.

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
- `docs/site-apis.md` — Site-specific API clients: AliExpress MTop, Mouser/DigiKey, Kijiji GraphQL, Craigslist SAPI, Facebook Marketplace GraphQL, eBay search extraction, realtor.ca (api2 home search + SSR listings + `search_realtor` tool), aartech.ca (React listing API + embedded product blob; no prices in HTML), vacuumwars.com (robot-vacuum comparison tool: the full lab dataset inline as `window.vwProducts`, client-side pagination, tested vs listed-only, colour-variant collapsing), ui.com (UniFi store/techspecs `__NEXT_DATA__` spec tree, and installation guides rebuilt from their JS page assets), wellfound.com (Next.js/Apollo startup jobs). Job-board APIs and embed/white-label detection for Ashby, Greenhouse, Lever, Gem, Dayforce, Cornerstone, Workday, BambooHR, JazzHR. Big-tech career boards: Eightfold (Microsoft/Netflix/PayPal, two API generations), Workday search filtering, amazon.jobs (incl. inline pay bands), Apple SSR hydration, Meta persisted GraphQL, Uber. gojobs.gov.on.ca (Ontario Public Service: ASP.NET WebForms postback listing, JSON-array facets, no keyword search). jobbank.gc.ca (federal Job Bank: city_id-gated location, keyword silently dropped for some terms, radius search). ca.indeed.com (embedded Mosaic job-card JSON and JobPosting JSON-LD, one stable anonymous result page).
- `docs/spa-discovery.md` — SPA API discovery (`src/fetchaller/discovery/`): observing a page in a browser and replaying what it made, so an endpoint's shape never needs bundle archaeology again. Ranking (why coverage and record count are directly opposed), the oracle (why a 200 that means "malformed" is the core problem), minimization, mint steps, and the measured per-board results
- `docs/testing.md` — Test organization, writing tests, live testing rules, test URLs
