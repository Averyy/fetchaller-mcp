# Testing Guide

## Writing Tests

**Every test must assert a meaningful outcome.** No useless tests.

- **Assert behavior, not existence.** Don't write `assert result is not None` or `assert len(x) > 0`. Assert the actual value, content, or effect.
- **Don't test constants.** Never assert that a default config value equals a hardcoded number — there's no logic to verify.
- **Don't test internal state.** Assert observable outcomes (return values, side effects), not private flags like `obj._running`.
- **Include negative cases.** If testing a lookup, also test that wrong keys return None/error.
- **Merge trivial tests.** A "register" test and a "get" test for the same store should be one test that does both.
- **Test through the pipeline.** For site-specific cleanup, prefer tests that go through `clean_html()`/`html_to_markdown()` with a URL (verifying detection + cleanup together) over tests that call a postprocessor in isolation.

## Live Testing Against Protected Sites

**NEVER rapid-fire requests during development.** Alibaba, AliExpress, Amazon, and similar sites have aggressive WAFs (TMD, ACW, Cloudflare) that will block you after a few fast requests, wasting time and making further testing impossible.

Rules for live testing:
- **One request at a time.** Verify the response before making the next request.
- **Wait 5+ seconds between requests** to the same domain. Use `asyncio.sleep()` or just wait between manual tests.
- **Always go through `fetch_url()`** — never make raw HTTP requests outside wafer.
- **Save HTML responses to files** during development (`/tmp/alibaba_search.html`, etc.) and test parsing against saved files instead of hitting the site repeatedly. This is the fastest way to iterate on extraction code.
- **When blocked, stop.** Don't retry — you'll make it worse. Switch to saved HTML, fix the code, and try again after 10+ minutes.
- **Test against the MCP tool** (not direct Python) when checking end-to-end — the MCP tool goes through wafer and has proper cookie caching.

## Manual Testing Required

**ALWAYS manually test every new feature, site, or support added.** Before committing, fetch real pages through the pipeline and verify the output is clean. Unit tests alone are not sufficient — real HTML varies wildly from test fixtures.

For each new site module:
1. Fetch at least one real page through `fetch_url()` or the MCP tool
2. Verify the output is clean (no leaked nav, ads, or boilerplate)
3. Fix any noise that leaks through, then re-test
4. Only commit after manual verification passes

For a deployed HTTP server, run the protocol/readiness/OAuth smoke after every
restart and use `--all-tools` for the release gate so all ten tools must return
semantically useful live data:

```bash
uv run python scripts/http_smoke_test.py \
  --url http://127.0.0.1:6000 \
  --api-key "$MCP_API_KEY" \
  --oauth-client-state /tmp/fetchaller-oauth-smoke.json \
  --all-tools
```

The same semantic suite must also pass through a real stdio MCP client against
the exact candidate image. `SMOKE_STDIO_COMMAND` is a JSON argument array, not
a shell command:

```bash
SMOKE_STDIO_COMMAND='["docker","run","--rm","-i","--platform","linux/amd64","fetchaller-mcp:test","python","-m","fetchaller.main"]' \
  uv run python scripts/smoke_test.py
```

For the New Reddit release gate, first run the independent legacy-contract and
offline fixture gate:

```bash
uv run pytest -q \
  tests/test_reddit_legacy_contract.py \
  tests/test_reddit_parity_corpus.py \
  tests/test_reddit.py
```

Then run the separate live route corpus. It records raw MCP bodies and a
timestamped/hash-addressed cold/warm/recreated report, paced by at least five
seconds:

```bash
SMOKE_STDIO_COMMAND='["docker","run","--rm","-i","fetchaller-mcp:candidate","python","-m","fetchaller.main"]' \
  uv run python scripts/reddit_parity.py \
    --strict \
    --include-unstable \
    --output /tmp/fetchaller-reddit-parity
```

Fixture-only routes never run live, including with `--include-unstable`; they
are restricted to inherently non-public access states. Removed public features
receive no waiver: the collection gate discovers Reddit's official archived
collection URL, validates its genuine archived Redux metadata, hydrates its
post IDs against current Reddit `/api/info`, and rejects shells, empty data, or
missing posts. `--include-unstable` dynamically discovers current real
post/comment, revision, multireddit, live, and collection IDs, records the
discovery bodies, and requires every such entry. Strict mode requires stable
live targets and any live class explicitly selected by `--include-unstable` or
`--require-oauth`. OAuth entries without
actual credentials remain `not_run`, never a pass; `--require-oauth` makes that
fatal. The publication workflow runs this credentialed live gate only when its
complete Reddit refresh set is configured; otherwise the exact image retains
the offline Reddit contract and omits hosted Reddit calls. The runner injects a
fresh host-directory bind beneath `/app/data`,
aligns the container UID/GID to its host owner, verifies an unexpired
owner-only Reddit cookie file after warm and recreated stages, requires
successful anonymous-cookie hydration with zero pure-HTTP Reddit verification
attempts after recreation, and separately requires zero guarded-browser
connections. It rejects a
conflicting cache/ownership environment or `--env-file`, so evidence cannot use
ambient cookies or silently re-solve. Eligible Reddit OAuth credentials are forwarded to Docker only by
environment variable name; the evidence records the credential mode and scopes,
never credential values. `report.json` derives its publication denominator from
the corpus and lists every offline entry and reason separately.

## Test Organization

- `test_site_detection.py` — Tests `_detect_site()` directly (URL-based, HTML-based, priority rules)
- `test_fetch_integration.py` — Integration tests for `fetch_url()` with mocked wafer sessions (forum hijack, feed discovery, URL transforms, content types, errors)
- `test_dispatch_verification.py` — Verifies CSS selectors and postprocessors are dispatched for correct sites through the pipeline
- `test_<site>_postprocessor.py` — Per-site regex postprocessor unit tests
- `test_search.py` — Search module tests: Google/DDG extraction, dedup, merge, cache, CAPTCHA, output format, integration with mocked HTTP
- `test_reddit.py` — Strict Reddit host recognition; normal URL→structured routing;
  thread/listing/profile/rules/wiki renderers; score/upvote-ratio semantics;
  nested/deleted/rich-media comments; gallery/video/crosspost/poll/status
  metadata; access-state mapping; comment-boundary budgets; browse/search link
  parity; shared session/limiter behavior; exact-read OAuth host/header
  isolation, refresh/retry/reuse, pagination, timeouts, backoff, and secret
  redaction; strict same-origin JSON redirects and nonexistent-community
  state; canonical New Reddit wiki-tree SSR parsing plus the anonymous
  `WikiPageRevisionsV2` page tree (CSRF, fixed route, identity, node/path
  agreement, uniqueness) and the optional `wikiread` fallback; strict
  archived-collection identity/Redux parsing plus current-post
  hydration; failure truth, queue, deadline, and backoff behavior
- `test_reddit_parity_corpus.py` — Checked-in zero-gap corpus coverage for every
  routed representation, access-state contract, and credential/fixture gating
- `test_reddit_legacy_contract.py` — Independent versioned Old Reddit surface
  inventory; detects omissions from both corpus and production routing; missing
  route/schema/renderer/MCP fixture coverage
- `test_amazon_postprocessor.py` — Amazon URL detection and regex postprocessor unit tests
- `test_alibaba_postprocessor.py` — Alibaba.com URL detection and regex postprocessor unit tests
- `test_alibaba_product.py` — Alibaba product JSON extraction (detailData, SSE data, tiered pricing, supplier info)
- `test_alibaba_search.py` — Alibaba search extraction (embedded JSON, product listing formatting)
- `test_aliexpress_postprocessor.py` — AliExpress URL detection and regex postprocessor unit tests
- `test_aliexpress_product.py` — AliExpress MTop product API extraction (pricing, specs, reviews)
- `test_aliexpress_search.py` — AliExpress search extraction (HTML parsing, Chrome fallback)
- `test_aliexpress_mtop.py` — MTop client unit tests (token lifecycle, MD5 signing, JSONP stripping)
- `test_soylent_postprocessor.py` — Soylent URL detection, inventory extraction, regex postprocessor tests
- `test_craigslist_postprocessor.py` — Craigslist URL detection and regex postprocessor unit tests
- `test_craigslist_sapi.py` — Craigslist SAPI client: URL detection, area ID extraction/caching, SAPI item parsing (URL construction, title/price/location/posted time), total count, area name extraction, relative time formatting, search result formatting
- `test_kijiji_api.py` — Kijiji GraphQL API client: URL detection, price formatting (cents, FIXED/FREE/PLEASE_CONTACT/SWAP_TRADE), listing/search formatting, error handling
- `test_ebay_postprocessor.py` — eBay URL detection, JSON-LD extraction, regex postprocessor unit tests
- `test_ebay_search_extraction.py` — eBay search URL detection, DOM extraction from `.s-item` elements, search marker postprocessing
- `test_molex_postprocessor.py` — Molex URL detection, JSON-LD extraction (additionalProperty specs), regex postprocessor unit tests
- `test_digikey_postprocessor.py` — DigiKey URL detection and regex postprocessor unit tests
- `test_digikey_api.py` — DigiKey API client: URL parsing, token manager, product formatting, search, error handling
- `test_mouser_postprocessor.py` — Mouser URL detection and regex postprocessor unit tests
- `test_mouser_api.py` — Mouser API client: URL parsing, part formatting, search, error handling
- `test_facebook_marketplace_graphql.py` — Facebook Marketplace URL detection (search/listing/browse/reserved paths), price filter extraction, GraphQL variable building (search + listing), response parsing (search + listing detail + images), search result formatting
- `test_marketplace_search.py` — Unified marketplace search: Craigslist location resolution (exact/alias/fuzzy/province), Kijiji location cross-check, cross-platform alias mapping (sort/category/condition completeness + resolution), orchestrator tests (all-succeed, partial-fail, all-fail, exception handling, platform filtering, price headers, FB location disambiguation for Canadian cities)
- `test_dayforce.py` — Dayforce URL detection (posting + board), `__NEXT_DATA__` parsing, posting render (metadata, postingLocations, jobPostingAttributes, jobDescriptionHeader/Body/Footer, skipped internal handles), board render (header, posting lines, link construction)
- `test_dayforce_whitelabel.py` — White-label Dayforce detection: `extract_dayforce_canonical_board_url()` parses `__NEXT_DATA__` from company-domain candidate portals (BASE_URL gate, clientNamespace + careerSiteXRefCode required, locale default)
- `test_cornerstone.py` — Cornerstone (CSOD) URL detection (posting + board, hyphenated tenants), `csod.context` parsing, posting render (header with corp, metadata, primary/additional locations, externalDescription, skipped internal handles), board render (header, location formatting, link construction, hyphen-postingDate suppression)
- `test_workday.py` — Workday URL detection (board + posting, with/without language segment, underscored sites, nested job paths, rejection of stripped-lang ambiguity), `WKQ0` layout-span stripping in description HTML, board render (grouping, link construction, bulletFields)
- `test_bamboohr.py` — BambooHR URL detection (board + posting, hyphenated tenants, non-numeric ID rejection), widget embed detection (data-domain regex variants, wrong domain rejection), posting render (location flattening, description + additionalInformation), board render (department grouping, atsLocation/location fallback)
- `test_jazzhr.py` — JazzHR URL detection (board + posting, with/without slug, hyphenated tenants, short ID rejection), multi-tenant embed extraction (dedupe + order), posting render (JSON-LD field passthrough, @context/@type top-level skip), board render (department grouping), multi-board render (per-tenant `##` sections)
- `test_ashby_embed_script.py` — Ashby script-tag embed detection (`<script src="https://jobs.ashbyhq.com/{org}/embed">`): basic match, embed-with-query, no-match cases, and the `/api`/`/embed`/`/_next` slug blocklist
- `test_realtor.py` — realtor.ca: URL detection (listing/SEO/map, EN `/real-estate/` + FR `/immobilier/`), filter encodings (range, sort/property/building/ownership inversion, place-from-slug), `/map` kwarg parsing (bbox + hash, rent params), agent/brokerage extraction (EN "Brokerage" / FR "Bureau de courtage" / no-keyword fallback), listing-HTML parsing (price/address/beds/rooms/agent/MLS/coords), search + listing-detail rendering
- `test_wellfound.py` — wellfound.com: URL detection (job/company/search, jobs-feed-vs-job), Apollo helpers (deref/entities/connection with arg-qualified keys), format helpers (money/size/date/url-clean, salary with decimal-string bounds), search title, job/company/search rendering (Open Jobs total from resolved connection), JobPosting JSON-LD extraction + soft-404 "Page not found" detection
- `test_ratelimit.py` — Per-domain rate limiter (DomainRateLimiter) unit tests
- Other `test_*.py` — Unit tests for specific modules (cache, config, oauth, etc.)

## Test URLs for Benchmarking

- Reddit listing/thread: `https://www.reddit.com/r/homelab/`,
  `https://www.reddit.com/r/Python/comments/1v6gbps/`
- Reddit cold/warm transport: point `WAFER_CACHE_DIR` at a new temporary
  directory, make one bounded request, recreate the shared Reddit session with
  the same directory, and repeat. Assert no requested/generated URL contains
  `old.reddit.com`; never print cookie values.
- Scrapers often blocked: `https://news.ycombinator.com/`, `https://www.nytimes.com/`
- Simple: `https://example.com/`, `https://httpbin.org/html`
- Cloudflare protected: `https://apollomapping.com`, `https://www.miata.net/`, `https://beyond.ca/`
- Cloudflare + geo-redirect: `https://www.glassdoor.com/`
