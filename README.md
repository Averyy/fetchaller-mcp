# fetchaller-mcp

Fetch any website in Claude Code without permission prompts. Built-in web search, Reddit support, and automatic bot challenge bypass.

## Why fetchaller?

Claude Code's built-in `WebFetch` asks permission for every new domain and blocks Reddit entirely. fetchaller fixes both:

- **`fetch`**: Read any URL — automatically bypasses Cloudflare, Akamai, and other bot challenges
- **`search`**: Web search via Google + DuckDuckGo
- **`browse_reddit`**: Browse subreddit listings (hot/new/top/rising)
- **`search_reddit`**: Search Reddit posts globally or within a subreddit
- **`search_marketplace`**: Search Kijiji, Craigslist, and Facebook Marketplace simultaneously with human-readable params (city name, category, price range)
- **`search_realtor`**: Search Canadian homes on realtor.ca for sale or rent with full filters (location, price, beds, baths, property/building type, ownership)
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
uv sync && patchright install chromium

# Add to Claude Code
claude mcp add fetchaller -- $(pwd)/.venv/bin/python -m fetchaller.main
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
- `mcp__fetchaller__get_aliexpress_product(product_id)` — AliExpress product details
- `mcp__fetchaller__search_aliexpress(query, page?, sort?, min_price?, max_price?)` — Search AliExpress
- `mcp__fetchaller__get_alibaba_product(product_id)` — Alibaba.com product details
- `mcp__fetchaller__search_alibaba(query, page?, sort?, min_price?, max_price?)` — Search Alibaba.com
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

### `fetch(url, maxTokens?, timeout?, raw?)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| url | string | required | URL to fetch (http/https) |
| maxTokens | number | 25000 | Max tokens to return |
| timeout | number | 10 | Request timeout in seconds |
| raw | boolean | false | Return raw HTML instead of markdown |

### Returns

Clean markdown with:
- Page title as H1
- Scripts, styles, nav, footer, iframes removed
- HTML converted to markdown
- Redirects noted
- Content truncated at token limit

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

### URL Transformation

All Reddit URLs are automatically transformed to `old.reddit.com` for 65-70% token savings. Trailing slashes are added to avoid 301 redirects (~50-100ms latency savings):

| Input URL | Transformed To |
|-----------|----------------|
| `www.reddit.com/r/foo` | `old.reddit.com/r/foo/` |
| `reddit.com/r/foo` | `old.reddit.com/r/foo/` |
| `old.reddit.com/r/foo` | `old.reddit.com/r/foo/` |

### Rate Limits

Reddit allows ~10 unauthenticated API requests per minute. `browse_reddit` and `search_reddit` each use 1 API call. `fetch` uses HTML (no API call).

## AliExpress & Alibaba Tools

### `get_aliexpress_product(product_id)` - Product Details

Accepts a numeric product ID (e.g., `1005006027485365`) or full URL. Returns price, specifications, ratings, and recent reviews via AliExpress's MTop API.

### `search_aliexpress(query, page?, sort?, min_price?, max_price?)` - Search Products

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| query | string | required | Search query |
| page | number | 1 | Page number (1-indexed) |
| sort | string | "default" | default, orders, price_asc, price_desc |
| min_price | number | — | Minimum price filter |
| max_price | number | — | Maximum price filter |

### `get_alibaba_product(product_id)` - B2B Product Details

Accepts a numeric product ID or full URL. Returns tiered pricing, MOQ, lead times, supplier info, and specifications.

### `search_alibaba(query, page?, sort?, min_price?, max_price?)` - Search B2B Products

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| query | string | required | Search query |
| page | number | 1 | Page number (1-indexed) |
| sort | string | "default" | default, price_asc, price_desc |
| min_price | number | — | Minimum price filter (USD) |
| max_price | number | — | Maximum price filter (USD) |

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

fetchaller transparently bypasses bot challenges. First requests to protected sites take longer (total wall time = solve + fetch, typically 10-40s), but subsequent requests use cached cookies and are fast (~0.5s). The `timeout` parameter only controls the HTTP fetch — the browser solve has its own internal timeouts.

### Supported Challenges

| Challenge | Method | Speed |
|-----------|--------|-------|
| Alibaba Cloud WAF (ACW) | Inline Python solver | ~1ms |
| Alibaba Cloud WAF (TMD) | Inline warming + browser | ~5-10s |
| Cloudflare Managed Challenge | Patchright browser solver | ~3-30s |
| Akamai Bot Manager | Patchright browser solver | ~3-15s |
| Amazon rate-limit/CAPTCHA | Inline form parse + follow (no browser) | ~100ms |
| DataDome, PerimeterX, Imperva | Patchright browser solver | ~3-10s |
| Kasada | Browser CT token + Python SHA-256 PoW | ~3-10s |
| GeeTest v4 slide CAPTCHA | CV notch detection + drag replay | ~5-15s |
| hCaptcha | Checkbox → ONNX image grid | ~5-30s |
| reCAPTCHA v2 | Checkbox → audio (Whisper) → ONNX grid | ~5-30s |
| F5 Shape, AWS WAF | Patchright browser solver | ~3-15s |

All challenge solving is handled by wafer's `BrowserSolver` (Patchright-based). Cookies are cached per-domain so subsequent requests skip the challenge.

### Requirements

**Docker**: Patchright's bundled Chromium is included in the image. The `cookie-data` volume persists solved cookies across restarts. No extra setup needed.

**Local (stdio)**: Browser support (Patchright) is included by default. Run `patchright install chromium` after installing to download the browser binary. Docker includes it automatically.

## Architecture

### Content Processing

`src/fetchaller/content/` handles HTML→markdown conversion. Each site module exports `is_<site>(url)`, `SELECTORS_LIST`, and optionally `strip_<site>_junk(soup)` / `postprocess_<site>(markdown)`:

- **`html.py`** — Generic pipeline + dispatch. Universal junk selectors, markdownify, whitespace cleanup. Generic JSON-LD Product fallback.
- **`amazon.py`** — All TLDs (.com, .ca, .co.uk, .de, etc.). CSS selectors, soup cleanup, regex post-processors.
- **`github.py`** — CSS selectors, URL transforms, file tree extraction, issue/PR/discussion extraction from embedded JSON.
- **`reddit.py`** — CSS selectors for old.reddit.com, URL transforms (www→old), post formatting.
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
# Run with authentication
MCP_API_KEY=your-secret-key python -m fetchaller.main --http

# Or use Docker
docker compose up -d
```

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
| `MCP_API_KEY` | (required) | Bearer token for auth |
| `MCP_SERVER_URL` | `http://localhost:$PORT` | Public URL for OAuth |
| `JWT_SECRET` | (required with API keys) | Stable signing secret for OAuth tokens — see below |
| `ALLOW_EPHEMERAL_JWT` | `0` | Set to `1` only for local HTTP development without a stable `JWT_SECRET` |
| `DATA_DIR` | `/app/data` | Persistent OAuth client and refresh-token state |
| `ACCESS_TOKEN_TTL` | `2592000` | OAuth access-token lifetime in seconds (30 days) |
| `RATE_LIMIT_REQUESTS` | 100 | Requests/minute per IP |
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

- **SSRF Protection**: Blocks localhost, private IPs, CGNAT/shared space, link-local addresses, and DNS rebinding services (nip.io, xip.io, etc.). Resolves hostnames, verifies every resolved address, and pins the connection to those addresses to close the DNS-rebinding window. Transition addresses (NAT64, 6to4, Teredo, IPv4-mapped) are judged by the IPv4 they actually translate to, so `64:ff9b::` + a public host works on IPv6-only networks while `64:ff9b::7f00:1` (loopback) and `64:ff9b::a9fe:a9fe` (cloud metadata) stay blocked. Unresolvable hosts fail closed but are reported as a DNS failure, not as a private-host block.
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
.venv/bin/ruff check src/ tests/
.venv/bin/python -m pytest tests/ -x -q

# Test in Docker
docker compose -f docker-compose.local.yml up --build
curl http://localhost:6000/health
```

## License

MIT
