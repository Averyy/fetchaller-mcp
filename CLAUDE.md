# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support. Built-in web search.

## Debugging Rules

**NEVER blame external services** (Claude, Anthropic, Google, Reddit, etc.) for issues. If something isn't working, the problem is in THIS codebase. Investigate our code first, add logging, and find the real cause. Blaming external parties wastes time.

**NEVER dismiss issues as "pre-existing" or "known".** Every issue is an issue. If something fails during testing, investigate the root cause and fix it — don't hand-wave it away as "that's always been broken" or "not caused by recent changes". The bar for shipping is: does it work? Not: did we break it?

## Pre-Commit Rules

**ALWAYS run lint and tests before EVERY commit. No exceptions.**

```bash
.venv/bin/ruff check src/ tests/   # Lint (import sorting, style)
.venv/bin/python -m pytest tests/ -x -q   # Tests
```

If ruff fails, fix with `.venv/bin/ruff check --fix src/ tests/` and verify again. CI runs `uv run ruff check src/ tests/` — if you skip this locally, the push WILL fail.

## Testing Rules

**ALWAYS use the same approach the code uses when testing.** For HTTP requests, use `curl_cffi` (not `urllib` or `requests`) because it has TLS fingerprint impersonation that bypasses bot protection. Test with multiple pages before making performance claims.

### Live Testing Against Protected Sites

**NEVER rapid-fire requests during development.** Alibaba, AliExpress, Amazon, and similar sites have aggressive WAFs (TMD, ACW, Cloudflare) that will block you after a few fast requests, wasting time and making further testing impossible.

Rules for live testing:
- **One request at a time.** Verify the response before making the next request.
- **Wait 5+ seconds between requests** to the same domain. Use `asyncio.sleep()` or just wait between manual tests.
- **Always use full impersonation** — go through `fetch_url()` with botfighter, never raw `curl_cffi` without impersonation.
- **Save HTML responses to files** during development (`/tmp/alibaba_search.html`, etc.) and test parsing against saved files instead of hitting the site repeatedly. This is the fastest way to iterate on extraction code.
- **When blocked, stop.** Don't retry — you'll make it worse. Switch to saved HTML, fix the code, and try again after 10+ minutes.
- **Test against the MCP tool** (not direct Python) when checking end-to-end — the MCP tool goes through botfighter and has proper cookie caching.

### Manual Testing Required

**ALWAYS manually test every new feature, site, or support added.** Before committing, fetch real pages through the pipeline and verify the output is clean. Unit tests alone are not sufficient — real HTML varies wildly from test fixtures.

For each new site module:
1. Fetch at least one real page through `fetch_url()` or the MCP tool
2. Verify the output is clean (no leaked nav, ads, or boilerplate)
3. Fix any noise that leaks through, then re-test
4. Only commit after manual verification passes

### Writing Tests

**Every test must assert a meaningful outcome.** No useless tests.

- **Assert behavior, not existence.** Don't write `assert result is not None` or `assert len(x) > 0`. Assert the actual value, content, or effect.
- **Don't test constants.** Never assert that a default config value equals a hardcoded number — there's no logic to verify.
- **Don't test internal state.** Assert observable outcomes (return values, side effects), not private flags like `obj._running`.
- **Include negative cases.** If testing a lookup, also test that wrong keys return None/error.
- **Merge trivial tests.** A "register" test and a "get" test for the same store should be one test that does both.
- **Test through the pipeline.** For site-specific cleanup, prefer tests that go through `clean_html()`/`html_to_markdown()` with a URL (verifying detection + cleanup together) over tests that call a postprocessor in isolation.

### Test Organization

- `test_site_detection.py` — Tests `_detect_site()` directly (URL-based, HTML-based, priority rules)
- `test_fetch_integration.py` — Integration tests for `fetch_url()` with MockFetcher (forum hijack, feed discovery, URL transforms, content types, errors)
- `test_dispatch_verification.py` — Verifies CSS selectors and postprocessors are dispatched for correct sites through the pipeline
- `test_<site>_postprocessor.py` — Per-site regex postprocessor unit tests
- `test_search.py` — Search module tests: Google/DDG extraction, dedup, merge, cache, CAPTCHA, output format, integration with mocked HTTP
- `test_botfighter.py` — ACW solver (known arg1, deterministic, edge cases), challenge detection (all WAF types + TMD + priority + negative cases), cookie cache (set/get/evict, CF expiry, persistence round-trip, corrupt file handling), solver dispatch (lock busy, browser fail, CF/Akamai/TMD/generic routing), Akamai HTML fallback
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
- `test_kijiji_postprocessor.py` — Kijiji URL detection and regex postprocessor unit tests
- `test_ebay_postprocessor.py` — eBay URL detection, JSON-LD extraction, regex postprocessor unit tests
- `test_digikey_postprocessor.py` — DigiKey URL detection and regex postprocessor unit tests
- `test_digikey_api.py` — DigiKey API client: URL parsing, token manager, product formatting, search, error handling
- `test_mouser_postprocessor.py` — Mouser URL detection and regex postprocessor unit tests
- `test_mouser_api.py` — Mouser API client: URL parsing, part formatting, search, error handling
- `test_ratelimit.py` — Per-domain rate limiter (DomainRateLimiter) unit tests
- Other `test_*.py` — Unit tests for specific modules (cache, config, oauth, etc.)

Test URLs for benchmarking:
- Reddit: `https://www.reddit.com/r/homelab/`, `https://old.reddit.com/r/homelab/`
- Scrapers often blocked: `https://news.ycombinator.com/`, `https://www.nytimes.com/`
- Simple: `https://example.com/`, `https://httpbin.org/html`
- Cloudflare protected: `https://apollomapping.com`, `https://www.miata.net/`, `https://beyond.ca/`
- Cloudflare + geo-redirect: `https://www.glassdoor.com/`

## Web Fetching & Search

**ALWAYS use fetchaller MCP tools instead of WebFetch and WebSearch.** fetchaller has no domain restrictions, bypasses bot protection (Cloudflare, Akamai, etc.), and produces much cleaner markdown with site-specific content cleanup.

- **fetch** — Fetch any URL as clean markdown
- **search** — Web search (Google + DuckDuckGo combined)
- **browse_reddit** / **search_reddit** — Reddit listings and search

Exception: If a dedicated MCP tool exists for a service (e.g., GitHub via `gh` CLI), prefer that instead.

## Content Processing Architecture

`src/fetchaller/content/` handles HTML→markdown conversion:

- **`html.py`** — Generic pipeline + dispatch. Universal junk selectors (nav, footer, ads, cookie banners, modals), markdownify conversion, whitespace cleanup. Dispatches to site modules based on URL.
- **`amazon.py`** — Amazon (all TLDs): CSS selectors, soup cleanup, regex post-processors. Covers .com, .ca, .co.uk, .de, .fr, .it, .es, .co.jp, .com.au, .in, etc.
- **`github.py`** — GitHub: CSS selectors, soup cleanup, regex post-processors, URL transforms, file tree extraction, issue/PR/discussion extraction from embedded JSON.
- **`reddit.py`** — Reddit: CSS selectors for old.reddit.com, URL transforms (www→old), post formatting.
- **`hackernews.py`** — Hacker News: CSS selectors, table unwrapping, story block reformatter.
- **`medium.py`** — Medium: CSS selectors (data-testid), source param stripping, post-article block removal. HTML-based detection for unknown custom domains.
- **`huggingface.py`** — Hugging Face: data-target CSS selectors, filter tag/button soup cleanup, regex post-processors.
- **`stackoverflow.py`** — Stack Overflow / Stack Exchange: CSS selectors, soup cleanup, regex post-processors. Covers all SE network sites.
- **`redflagdeals.py`** — RedFlagDeals forums: RFD-specific CSS selectors, soup cleanup, regex post-processors.
- **`forums.py`** — Generic forum support (XenForo, vBulletin, phpBB, Discourse). URL transform → RSS/Atom feeds, autodiscovery, feed parser, HTML fallback with generic CSS selectors.
- **`wikipedia.py`** — Wikipedia: CSS selectors for edit buttons, navboxes, TOC, reference lists.
- **`alibaba.py`** — Alibaba.com: CSS selectors, embedded JSON extraction (`window.detailData`, `window.__page__data_sse10`), soup cleanup.
- **`aliexpress.py`** — AliExpress: CSS selectors, soup cleanup, regex post-processors.
- **`craigslist.py`** — Craigslist (all city subdomains): CSS selectors, regex post-processors for nav/safety/loading noise.
- **`digikey.py`** — DigiKey (all TLDs): CSS selectors, soup cleanup, regex post-processors. Behind Akamai (botfighter handles). HTML fallback when no API key configured.
- **`ebay.py`** — eBay (all TLDs): CSS selectors, JSON-LD product data extraction, soup cleanup, regex post-processors.
- **`kijiji.py`** — Kijiji (kijiji.ca): CSS selectors, soup cleanup, regex post-processors for sponsored/filter/nav noise.
- **`mouser.py`** — Mouser (all TLDs): CSS selectors, soup cleanup, regex post-processors. Behind Akamai (botfighter handles). HTML fallback when no API key configured.
- **`soylent.py`** — Soylent (soylent.com, soylent.ca): Shopify store cleanup, inventory extraction from `gsf_conversion_data`.
- **`ti.py`** — Texas Instruments (ti.com): CSS selectors, document viewer support for lazy-loaded datasheets, inventory placeholder extraction.

Each site module exports the same interface: `is_<site>(url)`, `SELECTORS_LIST`, and optionally `strip_<site>_junk(soup)` / `postprocess_<site>(markdown)`. To add cleanup for a new site, create a new module following this pattern.

## Search Architecture

`src/fetchaller/search/` handles web search:

- **`__init__.py`** — Main `search()` function, result merging/dedup, 5-minute query cache, per-engine rate limiters (2s Google, 1s DDG), CAPTCHA escalating backoff (2m→5m→15m), lazy session lifecycle.
- **`google.py`** — Google via Opera Mini SSR. UA pool, Opera proxy header fingerprint, CAPTCHA detection.
- **`ddg.py`** — DuckDuckGo HTML endpoint. Only queried on page 1.
- **`models.py`** — `SearchResult` dataclass.
- **`tools/search.py`** — MCP tool wrapper.

## Bot Challenge Bypass (Botfighter)

`src/fetchaller/botfighter.py` — Transparent bot challenge detection and solving. ACW (Alibaba Cloud WAF) solved inline with pure Python (~1ms). All others (Cloudflare, Akamai, DataDome, PerimeterX, Imperva, Kasada) use PyDoll headful Chrome with Xvfb. Cookies cached per-domain with optional JSON persistence (auto-detects `/app/data/` in Docker). Geo-redirects handled via `final_url` dual-domain caching.

Key rules: cached cookies MUST use pinned UA + impersonate (no rotation). CF detects headless — always use Xvfb or offscreen window. Extract ALL cookies from browser (sites layer multiple protections).

Browser fingerprints auto-discovered from curl_cffi's `BrowserType` enum. `BROWSER_FINGERPRINTS` (newest 3 Chrome desktop) and `DEFAULT_IMPERSONATE` (newest) update automatically when curl_cffi is upgraded — zero code changes needed. curl_cffi handles Sec-Ch-Ua, User-Agent, and TLS fingerprint natively via `impersonate` — no manual header dicts.

## Alibaba/AliExpress Architecture

- **Alibaba.com**: SSR HTML only — no MTop API exists for the international site (`h5api.m.alibaba.com` serves 1688.com domestic China only). Extract embedded JSON from `window.detailData` (product) and `window.__page__data_sse10._offer_list` (search).
- **AliExpress**: MTop API at `acs.aliexpress.com` for product details (token bootstrap + MD5 signing). SSR HTML fallback for search. Chrome fallback when TMD blocks curl_cffi. Separate reviews API at `feedback.aliexpress.com/pc/searchEvaluation.do`.
- **MTop client**: `src/fetchaller/aliexpress/mtop.py` — token lifecycle, request signing, auto-refresh.

## Mouser/DigiKey API Architecture

Both Mouser and DigiKey block HTML scraping even with botfighter. When API keys are configured, `fetch_url()` intercepts their URLs and routes to dedicated API modules. Without keys, falls through to HTML pipeline.

- **`src/fetchaller/mouser/api.py`** — Mouser Search API client. Simple API key auth (`?apiKey=KEY`). Keyword search + part number lookup. Extracts MPN from URL path. Rate limited (30 req/min).
- **`src/fetchaller/digikey/api.py`** — DigiKey API client with OAuth2 `client_credentials` token manager. Keyword search + product details lookup. Extracts part info from URL path. Rate limited (120 req/min burst).

Env vars: `MOUSER_API_KEY`, `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`.

## Landing Page

`landing/` contains the static site deployed to fetchaller.com. Single-file `index.html` with Win95/98 retro aesthetic.

**For any frontend or design work**, read `design-style-guide.md` first — it defines the color palette, bevel system, window types, typography, animations, and anti-patterns. Always invoke the `frontend-design` skill (`/frontend-design`) when making visual changes.

**`landing/llms.txt`** — LLM-readable project summary following the [llmstxt.org](https://llmstxt.org) spec. Contains tools, site cleanup list, install instructions, and permission setup. **Keep this in sync when adding new tools, sites, or features.**

## Development & Testing

**CRITICAL**: When testing changes to this MCP server, you MUST use the local version, not the production Docker image.

### Testing Local Changes

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

### Common Mistake

Do NOT test against the production version (Docker image from GHCR). Changes to `src/fetchaller/` won't be reflected unless you rebuild locally.
