# Architecture

## fetchaller vs wafer

**fetchaller-mcp** and **wafer** (`~/code/wafer`) have a strict separation of concerns:

- **wafer** owns ALL HTTP transport and anti-detection: TLS fingerprinting, bot challenge detection, challenge solving (inline + browser), cookie caching, fingerprint rotation, retry/backoff, Opera Mini impersonation, rate limiting. Fetchaller calls `session.get(url)` and gets back a response — it never knows or cares how wafer bypassed protections.

- **fetchaller-mcp** owns ALL content processing and MCP tooling: takes raw HTML/JSON/PDF from wafer and turns it into clean markdown for LLMs. Site-specific CSS selectors, BeautifulSoup cleanup, regex post-processors, HTML→markdown conversion, PDF extraction, JSON-LD extraction, search result parsing, response caching, MCP tool definitions.

**The rule**: fetchaller NEVER does bot solving, impersonation, challenge detection, or cookie management. If a site blocks requests, that's a wafer bug — fix it in wafer, not fetchaller. Fetchaller passes a `BrowserSolver` instance to wafer sessions at construction time (dependency injection), but never calls methods on it directly.

## Content Processing Architecture

`src/fetchaller/content/` handles HTML→markdown conversion:

- **`html.py`** — Generic pipeline + dispatch. Universal junk selectors (nav, footer, ads, cookie banners, modals), markdownify conversion, whitespace cleanup. Dispatches to site modules based on URL. Includes generic JSON-LD Product fallback for sites without dedicated modules.
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
- **`craigslist.py`** — Craigslist (all city subdomains): CSS selectors, regex post-processors for nav/safety/loading noise. Search URL detection for SAPI intercept.
- **`facebook_marketplace.py`** — Facebook Marketplace URL detection only (`/marketplace/*` paths). GraphQL client lives in `facebook_marketplace/` package.
- **`digikey.py`** — DigiKey (all TLDs): CSS selectors, soup cleanup, regex post-processors. Behind Akamai (wafer handles). HTML fallback when no API key configured.
- **`ebay.py`** — eBay (all TLDs): CSS selectors, JSON-LD product data extraction, search result DOM extraction (`.s-item` elements), soup cleanup, regex post-processors.
- **`molex.py`** — Molex (molex.com): JSON-LD Product extraction (additionalProperty specs), AEM header/nav/account CSS selectors, regex post-processors for nav/About Us boilerplate. CSR site — product specs only available via JSON-LD.
- **`mouser.py`** — Mouser (all TLDs): CSS selectors, soup cleanup, regex post-processors. Behind Akamai (wafer handles). HTML fallback when no API key configured.
- **`soylent.py`** — Soylent (soylent.com, soylent.ca): Shopify store cleanup, inventory extraction from `gsf_conversion_data`.
- **`ti.py`** — Texas Instruments (ti.com): CSS selectors, document viewer support for lazy-loaded datasheets, inventory placeholder extraction.

Each site module exports the same interface: `is_<site>(url)`, `SELECTORS_LIST`, and optionally `strip_<site>_junk(soup)` / `postprocess_<site>(markdown)`. To add cleanup for a new site, create a new module following this pattern.

## Search Architecture

`src/fetchaller/search/` handles web search:

- **`__init__.py`** — Main `search()` function, result merging/dedup, 5-minute query cache, per-engine rate limiters (2s Google, 1s DDG), CAPTCHA escalating backoff (2m→5m→15m), lazy session lifecycle.
- **`google.py`** — Google search result extraction, CAPTCHA detection.
- **`ddg.py`** — DuckDuckGo HTML endpoint. Only queried on page 1.
- **`models.py`** — `SearchResult` dataclass.
- **`tools/search.py`** — MCP tool wrapper.

Search uses `wafer.AsyncSession(profile=Profile.OPERA_MINI)` — wafer owns the entire Opera Mini impersonation (52 confirmed versions, 21 real devices, correlated fingerprints). Fetchaller just parses the HTML results.

## Marketplace Search Architecture

`src/fetchaller/marketplace/` orchestrates concurrent searches across Kijiji, Craigslist, and Facebook Marketplace:

- **`search.py`** — Main `search_marketplace()` orchestrator. Launches platform searches via `asyncio.gather` with 45s timeout. Auto-skips Kijiji for non-Canadian locations. Appends ", Canada" to Facebook geocode queries for Canadian cities to avoid disambiguation issues (e.g. Vancouver, BC vs WA).
- **`aliases.py`** — Cross-platform alias mappings: `SORT_MAP`, `CATEGORY_MAP`, `CONDITION_MAP`. Maps human-readable values (e.g. "cars", "price_asc", "like_new") to platform-specific values.

Platform-specific clients used by the orchestrator:

- **Craigslist** (`src/fetchaller/craigslist/`) — SAPI v8 client + static location map (~50 CA + ~100 US cities with fuzzy matching).
- **Kijiji** (`src/fetchaller/kijiji/`) — Unauthenticated Apollo GraphQL + static location tree (from kijiji-scraper). Canada-only.
- **Facebook Marketplace** (`src/fetchaller/facebook_marketplace/`) — GraphQL search/listing/geocode via public `api/graphql/` endpoint. Session cookies seeded from marketplace page visit. Rate-limited via `facebook_limiter`.

## HTTP Transport (Wafer)

All HTTP fetching is handled by `wafer` (`~/code/wafer`). Fetchaller does NOT contain any bot protection, challenge solving, or TLS fingerprinting code.

- **`wafer.AsyncSession`** — per-request sessions with automatic challenge detection/solving, cookie caching, fingerprint rotation, retry/backoff
- **`wafer.browser.BrowserSolver`** — Patchright-based browser solver for Cloudflare, Akamai, etc. One shared instance created at server startup, passed to sessions via `browser_solver=`
- **`wafer.Profile.OPERA_MINI`** — first-class Opera Mini impersonation for search
- **Challenge types handled by wafer**: ACW, TMD, Cloudflare, Akamai, DataDome, PerimeterX, Imperva, Kasada, F5 Shape, AWS WAF, Amazon, Vercel, Arkose, GeeTest, hCaptcha, reCAPTCHA, and generic JS fallback (17 WAF types total)

If a site blocks requests, **fix it in wafer, not fetchaller**.
