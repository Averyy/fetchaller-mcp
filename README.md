# fetchaller-mcp

Fetch websites in Claude Code without per-domain permission prompts. Includes
web search, complete logged-out New Reddit support, challenge detection, and
automatic solving for the supported challenge types below.

## Why fetchaller?

Claude Code's built-in `WebFetch` asks permission for every new domain and blocks Reddit entirely. fetchaller fixes both:

- **`fetch`**: Read any URL — solves supported passive challenges and reports unresolved blocks as errors
- **`search`**: Web search via Google + DuckDuckGo
- **`browse_reddit`**: Browse subreddit listings (hot/new/top/rising)
- **`search_reddit`**: Search Reddit posts globally or within a subreddit
- **`search_marketplace`**: Search Kijiji, Craigslist, and Facebook Marketplace simultaneously with human-readable params (city name, category, price range)
- **`search_realtor`**: Search Canadian homes on realtor.ca for sale or rent with full filters (location, price, beds, baths, property/building type, ownership)
- **`search_linkedin_jobs`** / **`get_linkedin_job`**: Search LinkedIn's public logged-out job board (keywords, location, date posted, remote/hybrid/on-site, experience, job type, salary) and read full postings — no account needed
- **`get_aliexpress_product`**: AliExpress product details (price, specs, ratings, reviews)
- **`search_aliexpress`**: Search AliExpress products with price filters and sorting
- **`get_alibaba_product`**: Alibaba.com B2B product details (tiered pricing, MOQ, lead times, supplier info)
- **`search_alibaba`**: Search Alibaba.com B2B products

## Quick Start

### Local Installation (stdio mode)

```bash
# Clone and install
git clone https://github.com/Averyy/fetchaller-mcp.git
cd fetchaller-mcp
uv sync

# Point this at branded Google Chrome. The pinned Linux reference build in
# Dockerfile gives the closest browser/transport identity match.
export BROWSER_EXECUTABLE_PATH=/path/to/Chrome-for-Testing

# Add to Claude Code
claude mcp add fetchaller -- uv --directory "$(pwd)" run fetchaller-mcp
```

Add permissions to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__fetchaller__fetch",
      "mcp__fetchaller__search",
      "mcp__fetchaller__browse_reddit",
      "mcp__fetchaller__search_reddit",
      "mcp__fetchaller__search_marketplace",
      "mcp__fetchaller__search_realtor",
      "mcp__fetchaller__search_linkedin_jobs",
      "mcp__fetchaller__get_linkedin_job",
      "mcp__fetchaller__get_aliexpress_product",
      "mcp__fetchaller__search_aliexpress",
      "mcp__fetchaller__get_alibaba_product",
      "mcp__fetchaller__search_alibaba"
    ]
  }
}
```

Restart Claude Code.

## Recommended CLAUDE.md Addition

Add this to your project's `CLAUDE.md` (or global `~/.claude/CLAUDE.md`) to instruct Claude to prefer fetchaller:

```markdown
## Web Fetching & Search

**ALWAYS use fetchaller tools instead of WebFetch and WebSearch.** fetchaller has no domain restrictions and produces cleaner output.

- `mcp__fetchaller__fetch(url, maxTokens?, timeout?)` — Fetch any URL → clean markdown
- `mcp__fetchaller__search(query, page?)` — Web search (Google + DuckDuckGo)
- `mcp__fetchaller__browse_reddit(subreddit, sort?, time?, limit?)` — Browse subreddit listings
- `mcp__fetchaller__search_reddit(query, subreddit?, sort?, time?, limit?)` — Search Reddit posts
- `mcp__fetchaller__search_marketplace(query, location, platforms?, category?, sort?, condition?, min_price?, max_price?)` — Search Kijiji + Craigslist + Facebook Marketplace
- `mcp__fetchaller__search_realtor(location, transaction?, property_type?, building_type?, min_price?, max_price?, min_beds?, min_baths?, ownership?, sort?, page?)` — Search realtor.ca homes
- `mcp__fetchaller__search_linkedin_jobs(keywords, location?, date_posted?, workplace?, experience?, job_type?, min_salary?, sort?, start?, limit?)` — Search LinkedIn public jobs
- `mcp__fetchaller__get_linkedin_job(job_id)` — Full public detail for one LinkedIn posting
- `mcp__fetchaller__get_aliexpress_product(product_id, timeout?)` — AliExpress product details
- `mcp__fetchaller__search_aliexpress(query, page?, sort?, min_price?, max_price?, timeout?)` — Search AliExpress
- `mcp__fetchaller__get_alibaba_product(product_id, timeout?)` — Alibaba.com product details
- `mcp__fetchaller__search_alibaba(query, page?, sort?, min_price?, max_price?, timeout?)` — Search Alibaba.com
```

## Usage

The `mcp__fetchaller__fetch` tool is now available:

```
# Fetch a URL
fetch https://example.com

# Fetch with token limit
fetch https://example.com maxTokens=10000

# Fetch slow site with longer timeout
fetch https://slow-site.com maxTokens=25000 timeout=60
```

### Web Search

```
# Search the web
search "python asyncio tutorial"

# Page 2 of results
search "python asyncio tutorial" page=2
```

### Web Research Pattern

1. Use `search` to find URLs
2. Use `fetch` to read them

## Tool Reference

### `fetch(url, maxTokens?, timeout?, raw?, method?, headers?, body?)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| url | string | required | URL to fetch (http/https) |
| maxTokens | number | 25000 | Max tokens to return |
| timeout | number | 10 | Request timeout in seconds |
| raw | boolean | false | Return raw HTML instead of markdown |
| method | string | `GET` | `GET` or `POST` |
| headers | object | — | Extra request headers (max 32) |
| body | string | — | Request body, `POST` only (max 1MB) |

#### POST

Many search and listing APIs answer only to POST — Getro (which powers a large
share of VC portfolio job boards), Algolia, GraphQL gateways. Their pages look
static and fetch fine, so a GET-only fetcher reports an empty board on a site
that appears to work.

```json
{
  "url": "https://api.getro.com/api/v2/collections/246/search/jobs",
  "method": "POST",
  "headers": { "Accept": "application/json" },
  "body": "{\"hitsPerPage\":100,\"page\":0}"
}
```

`Content-Type` defaults to `application/json` when the body is valid JSON.
A POST skips site-specific handling and the response cache — every site
interceptor answers a URL pattern by issuing its own request, which would
discard the caller's body, and the cache is keyed by URL alone.

Only `GET` and `POST` are accepted. `PUT`/`PATCH`/`DELETE` have no retrieval
use; connection and body-framing headers (`Host`, `Content-Length`,
`Transfer-Encoding`, …) cannot be set. Credentials (`Authorization`, `Cookie`,
…) are dropped if a redirect crosses to another host, and a 301/302/303
downgrades a POST to a bodyless GET as browsers do.

### Returns

Clean markdown with:
- Page title as H1
- Scripts, styles, nav, footer, iframes removed (careers/jobs links in the site
  chrome are preserved — they are almost never in the body copy)
- HTML converted to markdown
- Redirects noted
- Content truncated at token limit
- A `possibly_js_rendered` warning when the page's content is assembled by
  JavaScript, so an un-hydrated shell is not mistaken for an empty site — with
  whatever the shell still declares (description, canonical URL, company
  profiles from JSON-LD) recovered alongside it, since on such a page that is
  the entire available answer

### Edge Cases

| Scenario | Behavior |
|----------|----------|
| Invalid URL | Error message |
| Non-200 response | Error + partial body |
| JSON content | Returned as-is |
| XML/RSS feeds | Returned as-is |
| CSV files | Returned as-is |
| Plain text | Returned as-is |
| PDF files | Text extracted |
| Timeout | Error after timeout (default 10s) |
| Huge page | Truncated at maxTokens |

### `search(query, page?)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| query | string | required | Search query |
| page | number | 1 | Result page (1-indexed) |

Searches Google (primary) and DuckDuckGo (supplement) in parallel. Returns titles, URLs, and snippets. Page 2+ queries Google only.

## Reddit Tools

Two tools for Reddit research:

### `browse_reddit` - Browse Subreddit Listings

```javascript
browse_reddit({
  subreddit: "LocalLLaMA",   // without r/ prefix
  sort: "hot",               // hot, new, top, rising
  time: "day",               // hour, day, week, month, year, all (for "top" only)
  limit: 10                  // 1-25
})
```

Returns post titles, scores, comment counts, and URLs. Use `fetch` to read full posts.

### `search_reddit` - Search Posts

```javascript
search_reddit({
  query: "best mass spectrometry software",
  subreddit: "labrats",      // optional - limit to subreddit
  sort: "relevance",         // relevance, hot, top, new, comments
  time: "year",              // hour, day, week, month, year, all
  limit: 10                  // 1-25
})
```

Returns matching posts with metadata. Use `fetch` to read full discussions.

### Compact New Reddit Fetching

Normal Reddit URLs and emitted links are canonicalized to `www.reddit.com`.
Structured JSON reads use the logged-out `api.reddit.com` origin and are
rendered as compact Markdown; HTML-only legs remain on the fixed New Reddit
`www` origin.
`/r/{subreddit}/wiki/pages/` first reads the canonical New Reddit SSR page tree,
then falls back to New Reddit's own logged-out `WikiPageRevisionsV2` page tree on
the fixed `/svc/shreddit/graphql` route when a community does not server-render
that tree. Both paths are anonymous: the page index needs no OAuth scope and no
browser. For a genuine historical collection URL, fetchaller
validates exact collection metadata from its archived New Reddit Redux snapshot
and hydrates every archived post ID from Reddit's current `/api/info` endpoint.
Archive shells, mismatched identities, empty collections, and missing current
posts are errors rather than successful empty or fabricated output.
Thread output includes the public vote score and upvote ratio returned by
Reddit, comment count and nested comments, outbound/discussion links, and
structured gallery, video, crosspost, poll, NSFW/spoiler/locked/archived, and
deleted-content state. Comment permalink URLs preserve the selected comment and
its parent context.

`old.reddit.com` input URLs are canonicalized to New Reddit; no normal request
depends on Old Reddit. Explicit `.json` URLs still return raw JSON. `raw=true`
on a normal Reddit URL returns the canonical New Reddit HTML representation.
Embedded author-written Old Reddit links are rewritten to their equivalent
`www.reddit.com` target.

Explicit JSON always remains parseable. When it exceeds `maxTokens`, fetchaller
retains only whole source scalars in a structural prefix and adds the top-level
JSON marker `"_fetchaller_truncated": true` (or a marker object at the end of a
top-level array). It never slices a string, substitutes a value, or reports
invalid JSON as success; a budget too small for both useful content and the
marker returns an explicit error.

fetchaller reads Reddit **anonymously only** and has no credential path: there
is no OAuth flow, no client ID/secret, and no refresh or access token. Every
Reddit read uses New Reddit's logged-out path. The wiki page index is served by
the SSR tree or New Reddit's logged-out page-tree route. Routes Reddit serves
only to a logged-in account — exact moderator rosters, and account-private
upvoted/downvoted activity — return an explicit error saying so; no names or
counts are ever guessed or reconstructed.

Vote data is not reconstructed: `score` is Reddit's public, fuzzed vote score,
and `upvote_ratio` is displayed only when Reddit returns it. Separate true
upvote/downvote counts are not available anonymously and are never invented.

#### Bounded Context Use

The compact renderer requests limits and nesting depth from the caller's token
budget, emits complete sections, and names every omitted section. Compactness
is never allowed to remove public content silently. Any future size benchmark
must ship its corpus, raw outputs, command, and timestamp with the result.

#### Reproducible Live Parity Evidence

`baselines/reddit-legacy-contract-v1.json` is an independent, versioned
inventory derived from the last generic Old Reddit implementation. The
offline gate compares that contract with both the production router and
`baselines/reddit-parity-corpus.json`, then exercises route/schema/renderer/MCP
fixtures:

```bash
uv run pytest -q \
  tests/test_reddit_legacy_contract.py \
  tests/test_reddit_parity_corpus.py \
  tests/test_reddit.py
```

The separate live gate runs the corpus through a real stdio MCP server with its
six-second default pacing (five-second minimum):

```bash
SMOKE_STDIO_COMMAND='["docker","run","--rm","-i","fetchaller-mcp:candidate","python","-m","fetchaller.main"]' \
  uv run python scripts/reddit_parity.py \
    --strict \
    --include-unstable \
    --output /tmp/fetchaller-reddit-parity
```

The runner starts with an empty dedicated wafer-cache directory, keeps it for
the warm and recreated-process stages, and records every body (including a
failed/challenge body), hash, command, timestamp, and outcome. For an exact
`docker run` command it mounts beneath entrypoint-owned `/app/data`, aligns the
runtime UID/GID with the host, validates a real owner-only Reddit cookie file
after warm and recreated stages, records non-secret Reddit cache hydration and
pure-HTTP verification counts, and rejects either recreated verification or
guarded-browser egress. A conflicting cache/ownership environment or `--env-file`
fails closed. When real OAuth credentials make the two OAuth corpus entries eligible,
Docker receives only `--env NAME` forwarding (never credential values in the
command or report); anonymous SSR output never counts as OAuth evidence.
Default runs require stable anonymous targets in `--strict` mode. The complete
release gate adds `--include-unstable`: the runner discovers current real
post/comment, wiki-revision, public-multireddit, live-update, and official
archived-collection IDs through separately recorded MCP calls, then requires
every public route
cold, warm, and recreated. It never substitutes a fake opaque ID.
Fixture-only targets always remain offline and can never become live evidence;
they are limited to inherently non-public access states such as private,
quarantined, banned, gated, forbidden, and not-found. A removed feature is not
a fixture-only waiver: its real public-read capability must still pass live.
Every route in the corpus is an anonymous public read, so the live Reddit
gates always run in CI and require no configuration. Every offline entry must
carry a mandatory reason and complete fixture evidence.

### Rate Limits

`browse_reddit` and `search_reddit` each use one JSON call. A normal `fetch`
uses one bounded JSON call for most URLs; a profile root uses five independent
public sources for metadata, activity, trophies, multireddits, and moderated
communities. Mapped `fetch` calls and both dedicated tools share the server's
request queue. Anonymous reads share a durable wafer session; configured API
reads share a separate cookie-isolated application session. Caller-selected
raw representations use the generic fetch path and its Reddit domain limiter.
Token refreshes and every API request use the same bounded request budget.

## AliExpress & Alibaba Tools

### `get_aliexpress_product(product_id, timeout?)` - Product Details

Accepts a numeric product ID (e.g., `1005006027485365`) or full URL. Returns
price, specifications, ratings, and recent reviews via AliExpress's MTop API.
If MTop is unavailable after a live search, an exact matching validated search
listing can supply a clearly labeled, narrower title/price/rating snapshot.
The optional timeout is an end-to-end budget from 1 to 180 seconds (default 180).

### `search_aliexpress(query, page?, sort?, min_price?, max_price?, timeout?)` - Search Products

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| query | string | required | Search query |
| page | number | 1 | Page number (1-indexed) |
| sort | string | "default" | default, orders, price_asc, price_desc |
| min_price | number | — | Minimum price filter |
| max_price | number | — | Maximum price filter |
| timeout | integer | 180 | End-to-end budget in seconds (1–300) |

### `get_alibaba_product(product_id, timeout?)` - B2B Product Details

Accepts a numeric product ID or full URL. Returns tiered pricing, MOQ, lead times, supplier info, and specifications.
The optional timeout is an end-to-end budget from 1 to 300 seconds (default 180).

### `search_alibaba(query, page?, sort?, min_price?, max_price?, timeout?)` - Search B2B Products

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| query | string | required | Search query |
| page | number | 1 | Page number (1-indexed) |
| sort | string | "default" | default, price_asc, price_desc |
| min_price | number | — | Minimum price filter (USD) |
| max_price | number | — | Maximum price filter (USD) |
| timeout | integer | 180 | End-to-end budget in seconds (1–300) |

## Marketplace Search

### `search_marketplace` — Search Kijiji, Craigslist, and Facebook Marketplace

Searches all three platforms concurrently with human-readable parameters and returns grouped results.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| query | string | required | Search keywords |
| location | string | required | City name (e.g. "toronto", "st catharines, ON", "seattle") |
| platforms | string[] | all | Platforms to search: kijiji, craigslist, facebook |
| category | string | "all" | all, cars, electronics, furniture, clothing, tools, free, bikes, phones, motorcycles, boats, rvs, auto_parts, sporting, toys, baby |
| sort | string | "date" | date, price_asc, price_desc, relevance |
| condition | string | — | new, like_new, good, fair |
| min_price | number | — | Minimum price in dollars |
| max_price | number | — | Maximum price in dollars |

Kijiji is Canada-only and automatically skipped for US locations. Location matching supports exact names, common aliases (e.g. "niagara" → Hamilton CL area), and fuzzy matching for typos.

## Real Estate Search

### `search_realtor` — Search realtor.ca homes

Searches Canadian homes for sale or rent via realtor.ca's api2, with the full filter set so an assistant can narrow a home search. Returns listings with price, address, beds/baths, size, agent, and a realtor.ca URL.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| location | string | required | City, neighbourhood, or postal code (e.g. "Ottawa", "Orleans, Ottawa", "M5V") |
| transaction | string | "sale" | sale, rent |
| property_type | string | "any" | any, residential, condo, recreational, vacant-land, multi-family, agriculture, parking |
| building_type | string | — | house, duplex, triplex, townhouse, apartment, other |
| min_price | integer | — | Minimum price (sale) or monthly rent |
| max_price | integer | — | Maximum price (sale) or monthly rent |
| min_beds | integer | — | Minimum bedrooms |
| min_baths | integer | — | Minimum bathrooms |
| ownership | string | — | freehold, condo |
| sort | string | "newest" | newest, oldest, price-asc, price-desc |
| page | integer | 1 | Result page (~20 per page, up to 600 returnable) |

Call `fetch(url)` on any listing URL for the full description, every property detail, and similar nearby homes. `fetch` also handles realtor.ca search/SEO/map pages (`/{prov}/{city}/real-estate`, `/map`) and all wellfound.com pages (startup job search, job detail, company profiles).

## How It Works

1. Validates URL (http/https only)
2. Blocks private/internal IPs (SSRF protection with DNS rebinding prevention)
3. Fetches with browser-like TLS fingerprints via wafer (Rust/BoringSSL) — rotates Chrome versions automatically
4. If bot challenge detected: solves automatically (see Bot Challenge Bypass below)
5. Detects content type
6. For HTML: removes junk elements (nav, footer, ads, cookie banners), applies site-specific cleanup (25+ sites including GitHub, Reddit, HN, Wikipedia, Medium, Stack Overflow, Amazon, eBay, AliExpress, Alibaba, DigiKey, Mouser, realtor.ca, wellfound.com, plus Ashby/Greenhouse/Lever/Gem/Dayforce/Cornerstone/Workday/BambooHR/JazzHR/Work-at-a-Startup job boards with embed + white-label detection, and more), converts to markdown
7. For JSON/XML/CSV/text: returns raw
8. For PDF: extracts text
9. Truncates to token limit

## Bot Challenge Bypass

fetchaller detects bot challenges and attempts the supported solver. A request
is successful only after the resulting content is no longer a challenge or
block page; a Playwright navigation that remains blocked is an error, never a
successful fetch. First requests can be slower, while later requests can reuse
durably cached cookies. The `timeout` parameter is one total wall-clock budget
covering transport retries, queueing, and browser solving.

### Supported Challenges

| Challenge | Method | Speed |
|-----------|--------|-------|
| Alibaba Cloud WAF (ACW) | Inline Python solver | ~1ms |
| Alibaba Cloud WAF (TMD) | wafer browser solver | ~5-60s |
| Cloudflare Managed Challenge | Patchright browser solver | ~3-30s |
| Akamai Bot Manager | Patchright browser solver | ~3-15s |
| Amazon rate-limit/CAPTCHA | Inline form parse + follow (no browser) | ~100ms |
| DataDome, PerimeterX, Imperva | Patchright browser solver | ~3-10s |
| Kasada | Browser CT token + Python SHA-256 PoW | ~3-10s |
| GeeTest v4 slide CAPTCHA | CV notch detection + drag replay | ~5-15s |
| hCaptcha | Checkbox auto-pass; image escalation is detect-only | ~5-30s |
| reCAPTCHA v2 / Enterprise | Checkbox → ONNX image grid | ~5-60s |
| F5 Shape, AWS WAF | Patchright browser solver | ~3-15s |

All challenge solving is handled by wafer's `BrowserSolver` (Patchright-based). Cookies are cached per-domain so subsequent requests skip the challenge.

Arkose/FunCaptcha and hCaptcha image escalation are detect-only and return an
explicit challenge error. Vercel and generic-JS handling supports passive
JavaScript checks only. An interactive CAPTCHA or any page that still contains
the challenge is reported as unresolved.

### Requirements

**Docker**: The browser-complete image is `linux/amd64` because Google does not
publish Chrome for Testing for Linux arm64. The build downloads one immutable
Chrome-for-Testing archive, verifies its SHA-256 digest and exact version, and
sets `BROWSER_EXECUTABLE_PATH` to that binary. BrowserSolver validates that the
configured executable is branded Chrome and warns if its version differs from
wafer/wreq's default emulation; browser-bound HTTP identity is aligned to the
installed browser, while the pinned image also keeps the TLS shape matched. The
local Compose file requests amd64 automatically on Apple Silicon, and the
`cookie-data` volume persists solved cookies across restarts.

**Local (stdio)**: Browser support is included by default, but protected-site
solves require installed branded Google Chrome. Set `BROWSER_EXECUTABLE_PATH`
to that executable. Matching wafer's current emulation is recommended so the
browser and TLS identities stay aligned; the current Linux reference version
and archive checksum are the `CHROME_VERSION` and `CHROME_SHA256` arguments in
`Dockerfile`.

## Architecture

### Content Processing

`src/fetchaller/content/` handles HTML→markdown conversion. Each site module exports `is_<site>(url)`, `SELECTORS_LIST`, and optionally `strip_<site>_junk(soup)` / `postprocess_<site>(markdown)`:

- **`html.py`** — Generic pipeline + dispatch. Universal junk selectors, markdownify, whitespace cleanup. Generic JSON-LD Product fallback.
- **`amazon.py`** — All TLDs (.com, .ca, .co.uk, .de, etc.). CSS selectors, soup cleanup, regex post-processors.
- **`github.py`** — CSS selectors, URL transforms, file tree extraction, issue/PR/discussion extraction from embedded JSON.
- **`reddit.py`** — Strict Reddit host recognition, New Reddit canonicalization,
  public URL→structured routing (JSON plus the canonical SSR wiki page tree),
  bounded compact renderers for threads/listings/profiles/rules/wiki, rich
  media, and structured access states.
- **`hackernews.py`** — CSS selectors, table unwrapping, story block reformatter.
- **`medium.py`** — CSS selectors (data-testid), HTML-based detection for unknown custom domains.
- **`huggingface.py`** — data-target attribute selectors, filter tag/button cleanup.
- **`stackoverflow.py`** — All Stack Exchange sites. CSS selectors, soup cleanup, regex post-processors.
- **`redflagdeals.py`** — RFD-specific CSS selectors, soup cleanup, regex post-processors.
- **`forums.py`** — Generic forum support (XenForo, vBulletin, phpBB, Discourse). RSS/Atom feed autodiscovery.
- **`wikipedia.py`** — CSS selectors for edit buttons, navboxes, TOC, reference lists.
- **`alibaba.py`** — Embedded JSON extraction (`window.detailData`, `window.__page__data_sse10`), soup cleanup.
- **`aliexpress.py`** — CSS selectors, soup cleanup, regex post-processors.
- **`craigslist.py`** — All city subdomains. CSS selectors, regex post-processors. Search URL detection for SAPI intercept.
- **`facebook_marketplace.py`** — URL detection only. GraphQL client in `facebook_marketplace/` package.
- **`digikey.py`** — All TLDs. CSS selectors, soup cleanup. Behind Akamai (wafer handles). HTML fallback without API key.
- **`ebay.py`** — All TLDs. JSON-LD product extraction, search result DOM extraction (`.s-item`), regex post-processors.
- **`molex.py`** — JSON-LD Product extraction (additionalProperty specs). CSR site — specs only in structured data.
- **`mouser.py`** — All TLDs. CSS selectors, soup cleanup. Behind Akamai. HTML fallback without API key.
- **`soylent.py`** — Shopify store cleanup, inventory extraction from `gsf_conversion_data`.
- **`ti.py`** — Document viewer support for lazy-loaded datasheets.
- **`ashby.py`** / **`greenhouse.py`** / **`lever.py`** / **`gem.py`** / **`dayforce.py`** / **`cornerstone.py`** / **`workday.py`** / **`bamboohr.py`** / **`jazzhr.py`** / **`workatastartup.py`** — Job-board platforms. All preserve the source's own field names, enum values, and section titles. Each posting and (where supported) board listing is dispatched to the platform's API/JSON shell before the generic HTML pipeline. Five embed/white-label detectors run during the HTML phase so company career pages like `synaptivemedical.com/job-openings` (white-label Dayforce), `skywatch.com/careers/` (Ashby `<script src="…/embed">`), `avidbots.com/company/careers/` (BambooHR `<div id="BambooHR">`), and `earthdaily.com/job-openings` (JazzHR multi-tenant) are upgraded to structured ATS output instead of returning empty SPA shells. See `docs/site-apis.md` for endpoints and detection details.

### Search

`src/fetchaller/search/` — Google + DuckDuckGo combined. Result merging/dedup, 5-minute cache, CAPTCHA backoff. Uses `wafer.AsyncSession(profile=Profile.OPERA_MINI)`.

### Site-Specific API Intercepts

CSR sites where HTML scraping produces garbage are intercepted in `fetch_url()` and routed to structured APIs:

- **Craigslist** (`src/fetchaller/craigslist/`) — SAPI v8 client (`sapi.craigslist.org`). Up to 120 items/request with total count. Area IDs from page HTML, cached per hostname. Listing pages stay in HTML pipeline.
- **Kijiji** (`src/fetchaller/kijiji/`) — Unauthenticated Apollo GraphQL (`kijiji.ca/anvil/api`). Search + listing detail. Prices in cents.
- **Facebook Marketplace** (`src/fetchaller/facebook_marketplace/`) — GraphQL (`facebook.com/api/graphql/`). Geocoded search, listing detail with photos.
- **AliExpress** (`src/fetchaller/aliexpress/`) — MTop API for products (token bootstrap + MD5 signing). SSR HTML for search. Reviews from `feedback.aliexpress.com`.
- **Alibaba.com** (`src/fetchaller/alibaba/`) — SSR HTML with embedded JSON. No MTop API for international site.
- **eBay** — SSR search results extracted from `.s-item` DOM elements, formatted as numbered list.
- **Mouser** (`src/fetchaller/mouser/`) — Search API client. Requires `MOUSER_API_KEY`.
- **DigiKey** (`src/fetchaller/digikey/`) — OAuth2 client_credentials API. Requires `DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET`.
- **Marketplace Search** (`src/fetchaller/marketplace/`) — Unified orchestrator searching Kijiji, Craigslist, and Facebook Marketplace concurrently. Human-readable params mapped to platform-specific values. Auto-skips Kijiji for non-Canadian locations.
- **Dayforce HCM** (`src/fetchaller/content/dayforce.py`) — Posting detail from SSR'd `__NEXT_DATA__`. Board listing via CSRF-protected POST to `/api/geo/{namespace}/jobposting/search` (NextAuth `/api/auth/csrf` round-trip required). White-label deployments on company domains are detected via `__NEXT_DATA__.runtimeConfig.BASE_URL` and rewritten to the canonical `jobs.dayforcehcm.com` board URL.
- **Cornerstone OnDemand** (`src/fetchaller/content/cornerstone.py`) — SPA shell carries a JWT in `csod.context`. Posting from `services/x/job-requisition/v2/requisitions/{reqid}/jobDetails`; board listing POSTed to `rec-job-search/external/jobs` on the regional cloud host (`us|eu|uk|au.api.csod.com`).
- **Workday** (`src/fetchaller/content/workday.py`) — `{tenant}.wd{1-103}.myworkdayjobs.com` boards and postings. Posting GET `/wday/cxs/{tenant}/{site}/job{externalPath}`; board POST `/wday/cxs/{tenant}/{site}/jobs` paginated in batches of 20 (capped at 200).
- **BambooHR** (`src/fetchaller/content/bamboohr.py`) — `{tenant}.bamboohr.com/careers`. Board GET `/careers/list`; posting GET `/careers/{id}/detail`. Both return clean JSON unauthenticated. Widget embeds (`<div id="BambooHR" data-domain="{tenant}.bamboohr.com">`) on company sites are auto-detected and resolved to the tenant subdomain.
- **JazzHR** (`src/fetchaller/content/jazzhr.py`) — `{tenant}.applytojob.com/apply`. Board SSR'd HTML (`.list-group .list-group-item`); posting reads schema.org `JobPosting` JSON-LD. Company sites that reference one or more JazzHR tenants via JS (e.g. `earthdaily.com/job-openings`) are auto-aggregated into a combined board.

### HTTP Transport (Wafer)

All HTTP is handled by `wafer` (`~/code/wafer`). Fetchaller does NOT contain bot protection, challenge solving, or TLS fingerprinting code. If a site blocks requests, fix it in wafer.

## Remote Deployment (HTTP Mode)

Deploy fetchaller as a remote MCP server for Claude.ai, Claude Desktop, or any MCP client.

### Quick Start

```bash
# Local loopback HTTP with authentication and restart-stable OAuth
export MCP_API_KEY='your-secret-key'
export JWT_SECRET="$(openssl rand -hex 32)"
export MCP_SERVER_URL='http://localhost:6000'
uv run python -m fetchaller.main --http

# Production Compose also requires a public HTTPS origin. It binds the app
# itself to 127.0.0.1:6000 for a host TLS reverse proxy.
export MCP_SERVER_URL='https://mcp.example.com'
docker compose up -d
```

Production traffic must terminate TLS before reaching the loopback-bound app.
Configure the host reverse proxy to forward to `127.0.0.1:6000`. If the proxy
sets `X-Forwarded-For`, set `TRUSTED_PROXY_IPS` to only that proxy's exact
address or CIDR; untrusted forwarded headers are ignored.

### Local Development

```bash
# Build and test locally
docker compose -f docker-compose.local.yml up --build

# Test endpoints
curl http://localhost:6000/health
curl -X POST http://localhost:6000/mcp \
  -H "Authorization: Bearer test-api-key-local" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

### Claude Code/Desktop Config

```json
{
  "mcpServers": {
    "fetchaller": {
      "type": "streamable-http",
      "url": "https://mcp.fetchaller.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### Claude.ai Custom Connector (OAuth)

For Claude.ai web/mobile with cross-platform sync:

1. Go to Settings → Connectors → Add Custom Connector
2. **Name**: `fetchaller`
3. **URL**: `https://mcp.fetchaller.com/mcp`
4. Leave Client ID/Secret **blank**
5. Enter your API key when prompted

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_PORT` | 6000 | Server port (1-65535) |
| `HTTP_STARTUP_TIMEOUT` | `180` | Container supervisor deadline for HTTP readiness; expiry exits nonzero so Docker restarts the service |
| `APP_SHUTDOWN_TIMEOUT` | `60` | Grace period after supervisor `TERM`; a stuck workload is killed and the container exits nonzero |
| `MCP_API_KEY` | (required) | Bearer token for auth |
| `MCP_SERVER_URL` | loopback default outside production Compose | Public HTTPS origin for OAuth; exact loopback HTTP is allowed for local use |
| `JWT_SECRET` | (required with API keys) | Stable signing secret for OAuth tokens — see below |
| `ALLOW_EPHEMERAL_JWT` | `0` | Set to `1` only for local HTTP development without a stable `JWT_SECRET` |
| `DATA_DIR` | `/app/data` | Persistent OAuth client and refresh-token state |
| `ACCESS_TOKEN_TTL` | `2592000` | OAuth access-token lifetime in seconds (30 days) |
| `RATE_LIMIT_REQUESTS` | 100 | Requests/minute per IP |
| `DNS_DOH_FALLBACK` | `0` | Set to `1` to re-resolve against public DoH resolvers (1.1.1.1, 8.8.8.8) when the system resolver answers `0.0.0.0`/`::`. **Off by default:** that answer is usually a blocklist doing its job, and resolving past it overrides local DNS policy and discloses the hostname to a third party. Either way such hosts are reported as a DNS failure, not as a private-host block, so the resolver can be fixed instead. Fallback addresses still face the same private-range checks. |
| `TRUSTED_PROXY_IPS` | — | Comma-separated addresses/CIDRs of reverse proxies whose rightmost `X-Forwarded-For` value is trusted |
| `MOUSER_API_KEY` | — | Mouser Search API key ([free registration](https://www.mouser.com/MyMouser/MouserSearchApplication.aspx)) |
| `DIGIKEY_CLIENT_ID` | — | DigiKey API client ID ([free registration](https://developer.digikey.com)) |
| `DIGIKEY_CLIENT_SECRET` | — | DigiKey API client secret |

### JWT_SECRET (required for persistent deployments)

OAuth access tokens are stateless JWTs. When `MCP_API_KEY` is configured, HTTP startup fails
if `JWT_SECRET` is unset. For local HTTP development only, `ALLOW_EPHEMERAL_JWT=1` opts into a
random per-process secret; every previously issued access token then becomes invalid when the
process restarts. Registered clients and hashed refresh tokens persist under `DATA_DIR`, while
short-lived authorization codes remain in memory.

Generate one value and keep it stable for the life of the deployment:

```bash
openssl rand -hex 32
```

Pass it to the container the same way as `MCP_API_KEY` (compose `environment:`, Unraid
template variable, etc.). Verify it landed with `printenv | grep JWT_SECRET` inside the
container — an empty value counts as unset and prevents authenticated HTTP startup.

Two caveats:

- **Changing it invalidates every access token.** Clients can normally recover with their
  persisted rotating refresh tokens, but rotating both the secret and OAuth state un-pairs
  every connector.
- **Do not derive it from `MCP_API_KEY`.** Token payloads carry `api_key_hash` in cleartext,
  so a derived secret is recoverable from any captured token, allowing forged tokens. Use an
  independent random value.

Bearer auth with a raw `MCP_API_KEY` is unaffected by any of this — it is validated against
the environment variable directly and survives restarts.

## Security

- **SSRF Protection**: Blocks localhost, private IPs, CGNAT/shared space, link-local addresses, and DNS rebinding services (nip.io, xip.io, etc.). Resolves hostnames, verifies every resolved address, and pins the connection to those addresses to close the DNS-rebinding window. Chromium challenge solving is covered too: every browser redirect and subresource is forced through a loopback-only SOCKS5 guard that re-applies the address policy and connects to an approved numeric IP. Public nonstandard ports remain supported; Chromium's own unsafe-port policy determines which ports a page may use. Transition addresses (NAT64, 6to4, Teredo, IPv4-mapped) are judged by the IPv4 they actually translate to, so `64:ff9b::` + a public host works on IPv6-only networks while `64:ff9b::7f00:1` (loopback) and `64:ff9b::a9fe:a9fe` (cloud metadata) stay blocked. Unresolvable hosts fail closed but are reported as a DNS failure, not as a private-host block.
- **OAuth 2.1**: PKCE required for authorization-code exchanges. Refresh tokens are hashed at rest and rotate on use.
- **Rate Limiting**: Per-IP rate limiting with configurable limits.

## Files

```
fetchaller-mcp/
├── pyproject.toml           # Python package config
├── src/fetchaller/          # Python source
│   ├── main.py              # Entry point
│   ├── server.py            # MCP server setup
│   ├── config.py            # Configuration
│   ├── http/                # HTTP server (FastAPI)
│   ├── tools/               # MCP tools (fetch, search, reddit, aliexpress, alibaba, marketplace)
│   ├── content/             # Content processing (HTML→markdown, site-specific cleanup)
│   ├── search/              # Web search (Google + DuckDuckGo)
│   ├── aliexpress/          # AliExpress MTop API client, product, search, reviews
│   ├── alibaba/             # Alibaba.com product and search extraction
│   ├── mouser/              # Mouser Search API client
│   ├── craigslist/          # Craigslist SAPI client + location resolution
│   ├── kijiji/              # Kijiji GraphQL API client + location resolution
│   ├── facebook_marketplace/# Facebook Marketplace GraphQL client
│   ├── marketplace/         # Unified marketplace search orchestrator
│   ├── digikey/             # DigiKey API client (OAuth2 + product/search)
│   ├── cache/               # Response caching
│   ├── queue/               # Reddit rate limiting
│   └── security/            # SSRF, crypto, XSS
├── docker-compose.yml       # Production deployment
├── docker-compose.local.yml # Local testing
├── Dockerfile               # Container build
├── docs/                    # Architecture & developer docs
├── CLAUDE.md                # Instructions for Claude
├── README.md                # This file
└── landing/                 # Static site (fetchaller.com)
    ├── index.html           # Landing page
    └── llms.txt             # LLM-readable project summary (llmstxt.org spec)
```

## Dependencies

- `wafer-py[browser]` - HTTP transport with TLS fingerprinting, bot challenge bypass, and browser solver (Rust/BoringSSL + Patchright)
- `mcp` - MCP protocol SDK
- `fastapi` + `uvicorn` - HTTP server
- `beautifulsoup4` + `markdownify` - HTML to markdown
- `pymupdf4llm` - PDF to markdown extraction
- `pyjwt` - OAuth tokens

## Testing

```bash
# Run tests
uv sync --extra dev
uv run ruff check src/ tests/
uv run pytest tests/ -x -q

# Test in Docker
docker compose -f docker-compose.local.yml up --build
curl http://localhost:6000/health
```

## License

MIT
