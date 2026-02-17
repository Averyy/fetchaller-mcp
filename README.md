# fetchaller-mcp

Fetch any website in Claude Code without permission prompts. Built-in web search, Reddit support, and automatic bot challenge bypass.

## Why fetchaller?

Claude Code's built-in `WebFetch` asks permission for every new domain and blocks Reddit entirely. fetchaller fixes both:

- **`fetch`**: Read any URL — automatically bypasses Cloudflare, Akamai, and other bot challenges
- **`search`**: Web search via Google + DuckDuckGo
- **`browse_reddit`**: Browse subreddit listings (hot/new/top/rising)
- **`search_reddit`**: Search Reddit posts globally or within a subreddit
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
uv venv && uv pip install -e .

# Add to Claude Code
claude mcp add fetchaller -- /path/to/fetchaller-mcp/.venv/bin/python -m fetchaller.main
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

### `fetch(url, maxTokens?, timeout?)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| url | string | required | URL to fetch (http/https) |
| maxTokens | number | 25000 | Max tokens to return |
| timeout | number | 10 | Request timeout in seconds |

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

Three tools for Reddit research:

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

## How It Works

1. Validates URL (http/https only)
2. Blocks private/internal IPs (SSRF protection with DNS rebinding prevention)
3. Checks cookie cache for domain — if cached, uses pinned cookies + UA + TLS fingerprint
4. Fetches with browser-like TLS fingerprints (curl_cffi)
5. If bot challenge detected: solves automatically (see Bot Challenge Bypass below)
6. Detects content type
7. For HTML: removes junk elements (nav, footer, ads, cookie banners), applies site-specific cleanup (20+ sites including GitHub, Reddit, HN, Wikipedia, Medium, Stack Overflow, Amazon, eBay, AliExpress, Alibaba, DigiKey, Mouser, and more), converts to markdown
8. For JSON/XML/CSV/text: returns raw
9. For PDF: extracts text
10. Truncates to token limit

## Bot Challenge Bypass

fetchaller transparently bypasses bot challenges. First requests to protected sites take longer (total wall time = solve + fetch, typically 10-40s), but subsequent requests use cached cookies and are fast (~0.5s). The `timeout` parameter only controls the HTTP fetch — the browser solve has its own internal timeouts.

### Supported Challenges

| Challenge | Method | Speed |
|-----------|--------|-------|
| Alibaba Cloud WAF (`acw_sc__v2`) | Pure Python solver (fixed shuffle + XOR) | ~1ms |
| Alibaba Cloud WAF TMD | Headful Chrome, session warming via homepage | ~5-10s |
| Cloudflare Managed Challenge | Headful Chrome via PyDoll + Xvfb | ~3-30s |
| Akamai Bot Manager | Headful Chrome, poll for `_abck` cookie + HTML fallback | ~3-15s |
| Amazon rate-limit/CAPTCHA | Headful Chrome, click "Continue shopping" | ~3-10s |
| DataDome, PerimeterX, Imperva, Kasada | Headful Chrome, network idle wait | ~3-10s |

### How It Works

1. **ACW challenges** (Alibaba Cloud WAF): Solved inline with pure Python — no browser needed. Extracts `arg1` from challenge HTML, applies fixed shuffle permutation + XOR → cookie value.
2. **Browser challenges** (everything else): Launches headful Chrome via PyDoll with Xvfb virtual display (CF detects headless mode). Solves the challenge, extracts all cookies + User-Agent, caches per-domain.
3. **Cookie caching**: Cookies are bound to UA + TLS fingerprint. Cached cookies are replayed on subsequent requests with the exact same UA and fingerprint. CF cookies track expiry; all others cached until re-challenged.
4. **Geo-redirects**: Sites like Glassdoor redirect based on location (.com → .ca). Cookies are cached under both domains and requests retry from the final URL.
5. **Persistence**: Cookie cache auto-persists to `/app/data/cookies.json` in Docker (survives container restarts). In-memory only outside Docker.

### Requirements

**Docker**: Chrome and Xvfb are included in the image. The `cookie-data` volume persists solved cookies across restarts. No extra setup needed.

**Local (stdio)**: Requires Chrome or Chromium installed on your system for browser-based challenges. Without Chrome, fetchaller still works — it just can't bypass Cloudflare/Akamai/etc. (ACW challenges still work since they're pure Python). macOS uses an offscreen window; Linux needs Xvfb for headful mode.

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
| `JWT_SECRET` | (derived from API key) | Secret for OAuth tokens |
| `RATE_LIMIT_REQUESTS` | 100 | Requests/minute per IP |
| `CHROME_IDLE_TIMEOUT` | 60 | Minutes before idle Chrome shuts down |
| `COOKIE_CACHE_PATH` | auto | Cookie persistence path (auto-detects `/app/data/` in Docker) |
| `MOUSER_API_KEY` | — | Mouser Search API key ([free registration](https://www.mouser.com/MyMouser/MouserSearchApplication.aspx)) |
| `DIGIKEY_CLIENT_ID` | — | DigiKey API client ID ([free registration](https://developer.digikey.com)) |
| `DIGIKEY_CLIENT_SECRET` | — | DigiKey API client secret |

## Security

- **SSRF Protection**: Blocks localhost, private IPs, link-local addresses, and DNS rebinding services (nip.io, xip.io, etc.). Resolves hostnames to verify final IP addresses.
- **OAuth 2.1**: PKCE required for all token exchanges. Timing-safe comparisons for auth codes.
- **Rate Limiting**: Per-IP rate limiting with configurable limits.

## Files

```
fetchaller-mcp/
├── pyproject.toml           # Python package config
├── src/fetchaller/          # Python source
│   ├── main.py              # Entry point
│   ├── server.py            # MCP server setup
│   ├── config.py            # Configuration
│   ├── botfighter.py        # Bot challenge detection, solving, cookie cache
│   ├── http/                # HTTP server (FastAPI)
│   ├── tools/               # MCP tools (fetch, search, reddit, aliexpress, alibaba)
│   ├── content/             # Content processing (HTML→markdown, site-specific cleanup)
│   ├── search/              # Web search (Google + DuckDuckGo)
│   ├── aliexpress/          # AliExpress MTop API client, product, search, reviews
│   ├── alibaba/             # Alibaba.com product and search extraction
│   ├── mouser/              # Mouser Search API client
│   ├── digikey/             # DigiKey API client (OAuth2 + product/search)
│   ├── cache/               # Response caching
│   ├── queue/               # Reddit rate limiting
│   └── security/            # SSRF, crypto, XSS
├── docker-compose.yml       # Production deployment
├── docker-compose.local.yml # Local testing
├── Dockerfile               # Container build
├── CLAUDE.md                # Instructions for Claude
├── README.md                # This file
└── landing/                 # Static site (fetchaller.com)
    ├── index.html           # Landing page
    └── llms.txt             # LLM-readable project summary (llmstxt.org spec)
```

## Dependencies

- `mcp` - MCP protocol SDK
- `fastapi` + `uvicorn` - HTTP server
- `curl-cffi` - TLS fingerprint impersonation
- `beautifulsoup4` + `markdownify` - HTML to markdown
- `pdfplumber` - PDF text extraction
- `pyjwt` - OAuth tokens
- `pydoll-python` - Headful Chrome automation for bot challenge bypass

## Testing

```bash
# Run tests locally
uv venv && source .venv/bin/activate
python -c "from fetchaller.http.app import create_app; print('OK')"

# Test in Docker
docker compose -f docker-compose.local.yml up --build
curl http://localhost:6000/health
```

## License

MIT
