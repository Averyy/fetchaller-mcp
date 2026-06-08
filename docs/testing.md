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

## Test Organization

- `test_site_detection.py` — Tests `_detect_site()` directly (URL-based, HTML-based, priority rules)
- `test_fetch_integration.py` — Integration tests for `fetch_url()` with mocked wafer sessions (forum hijack, feed discovery, URL transforms, content types, errors)
- `test_dispatch_verification.py` — Verifies CSS selectors and postprocessors are dispatched for correct sites through the pipeline
- `test_<site>_postprocessor.py` — Per-site regex postprocessor unit tests
- `test_search.py` — Search module tests: Google/DDG extraction, dedup, merge, cache, CAPTCHA, output format, integration with mocked HTTP
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

- Reddit: `https://www.reddit.com/r/homelab/`, `https://old.reddit.com/r/homelab/`
- Scrapers often blocked: `https://news.ycombinator.com/`, `https://www.nytimes.com/`
- Simple: `https://example.com/`, `https://httpbin.org/html`
- Cloudflare protected: `https://apollomapping.com`, `https://www.miata.net/`, `https://beyond.ca/`
- Cloudflare + geo-redirect: `https://www.glassdoor.com/`
