# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support. Built-in web search.

## Debugging Rules

**NEVER blame external services** (Claude, Anthropic, Google, Reddit, etc.) for issues. If something isn't working, the problem is in THIS codebase. Investigate our code first, add logging, and find the real cause. Blaming external parties wastes time.

## Pre-Commit Rules

**ALWAYS run lint and tests before EVERY commit. No exceptions.**

```bash
.venv/bin/ruff check src/ tests/   # Lint (import sorting, style)
.venv/bin/python -m pytest tests/ -x -q   # Tests
```

If ruff fails, fix with `.venv/bin/ruff check --fix src/ tests/` and verify again. CI runs `uv run ruff check src/ tests/` — if you skip this locally, the push WILL fail.

## Testing Rules

**ALWAYS use the same approach the code uses when testing.** For HTTP requests, use `curl_cffi` (not `urllib` or `requests`) because it has TLS fingerprint impersonation that bypasses bot protection. Test with multiple pages before making performance claims.

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
- `test_botfighter.py` — ACW solver (known arg1, deterministic, edge cases), challenge detection (all 7 WAF types + priority + negative cases), cookie cache (set/get/evict, CF expiry, persistence round-trip, corrupt file handling), solver dispatch (lock busy, browser fail, CF/Akamai/generic routing)
- `test_amazon_postprocessor.py` — Amazon URL detection and regex postprocessor unit tests
- Other `test_*.py` — Unit tests for specific modules (cache, config, oauth, etc.)

Test URLs for benchmarking:
- Reddit: `https://www.reddit.com/r/homelab/`, `https://old.reddit.com/r/homelab/`
- Scrapers often blocked: `https://news.ycombinator.com/`, `https://www.nytimes.com/`
- Simple: `https://example.com/`, `https://httpbin.org/html`
- Cloudflare protected: `https://apollomapping.com`, `https://www.miata.net/`, `https://beyond.ca/`
- Cloudflare + geo-redirect: `https://www.glassdoor.com/`

## Web Fetching

**Use fetchaller instead of WebFetch** (no domain restrictions). If a dedicated MCP exists (GitHub, Slack, etc.), use that instead.

## Reddit Searching and Browsing

Use `mcp__fetchaller__browse_reddit` to browse subreddits, `mcp__fetchaller__search_reddit` to find posts, and `mcp__fetchaller__fetch` to read full discussions.

## Tool: `mcp__fetchaller__fetch`

```
mcp__fetchaller__fetch(url: string, maxTokens?: number, timeout?: number)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| url | string | required | Any http/https URL |
| maxTokens | number | 25000 | Max tokens to return |
| timeout | number | 10 | Request timeout in seconds |

## Examples

**Read a specific page:**
```
mcp__fetchaller__fetch("https://example.com/docs")
```

**Read with token limit:**
```
mcp__fetchaller__fetch("https://example.com/long-page", 10000)
```

**Read slow page with longer timeout (60s):**
```
mcp__fetchaller__fetch("https://slow-site.example.com", 25000, 60)
```

**Research workflow:**
1. mcp__fetchaller__search("topic keywords")
2. For each relevant URL: mcp__fetchaller__fetch(url)

## What It Does

- Fetches any HTTP/HTTPS URL
- Converts HTML to clean markdown (strips scripts, styles, nav, footer, ads)
- Extracts text from PDFs
- Returns JSON, XML/RSS, CSV, and plain text as-is
- Shows redirect destinations
- Configurable timeout (default: 10 seconds)
- Truncates at token limit

## Tool: `mcp__fetchaller__search`

```
mcp__fetchaller__search(query: string, limit?: number, page?: number)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| query | string | required | Search query |
| page | number | 1 | Result page (1-indexed) |

Searches Google (primary) and DuckDuckGo (supplement) in parallel. Returns titles, URLs, and snippets as text. Page 2+ queries Google only.

**Example output:**
```
Search: "python asyncio tutorial" | google: 10 | ddg: 4 new | 14 total

1. Python's asyncio: A Hands-On Walkthrough
   https://realpython.com/async-io-python/
   In this tutorial, you'll learn how Python asyncio works...
```

## Reddit Tools

### Quick Overview

| Tool | Purpose | API Calls |
|------|---------|-----------|
| `browse_reddit` | Browse subreddit listings (hot/new/top/rising) | 1 per call |
| `search_reddit` | Search Reddit posts | 1 per call |
| `fetch` | Read full post content | 0 (uses HTML) |

**Workflow**: Browse/search first (1 API call for 25 posts), then fetch specific posts you want to read.

### Tool: `mcp__fetchaller__browse_reddit`

Browse a subreddit's posts. Returns metadata and URLs.

```
mcp__fetchaller__browse_reddit({
  subreddit: "LocalLLaMA",
  sort: "hot",        // hot, new, top, rising
  time: "day",        // hour, day, week, month, year, all (for "top" only)
  limit: 10,          // 1-25
  after: "t3_..."     // pagination cursor (optional)
})
```

### Tool: `mcp__fetchaller__search_reddit`

Search Reddit posts globally or within a subreddit.

```
mcp__fetchaller__search_reddit({
  query: "best IDE 2025",
  subreddit: "programming",  // optional - limit to subreddit
  sort: "relevance",         // relevance, hot, top, new, comments
  time: "all",               // hour, day, week, month, year, all
  limit: 10                  // 1-25
})
```

### Reddit URL Transformation

fetchaller automatically transforms Reddit URLs to use `old.reddit.com` for 65-70% token savings:

- `www.reddit.com/*` → `old.reddit.com/*/`
- `reddit.com/*` → `old.reddit.com/*/`
- `old.reddit.com/*` → adds trailing slash if missing
- Explicit `.json` URLs → unchanged

### Rate Limits

Reddit allows ~10 unauthenticated API requests per minute. The browse/search tools use the JSON API (1 call each), while fetch uses HTML (no API call).

## Content Processing Architecture

`src/fetchaller/content/` handles HTML→markdown conversion:

- **`amazon.py`** — Amazon (all TLDs): ~30 CSS selectors (sponsored carousels, buy box noise, quick-view overlay, rating histograms, aspect tags, footer, tracking pixels), soup cleanup (hidden inputs, translate/report links), ~30 regex post-processors for tracking URLs, feedback blocks, footer sections, delivery prompts. Covers amazon.com, .ca, .co.uk, .de, .fr, .it, .es, .co.jp, .com.au, .in, etc.
- **`html.py`** — Generic pipeline only. Universal junk selectors (nav, footer, ads, cookie banners, modals), markdownify conversion, whitespace cleanup. Dispatches to site modules based on URL.
- **`github.py`** — GitHub: ~30 CSS selectors, soup cleanup, ~30 regex post-processors, URL transforms, file tree extraction.
- **`reddit.py`** — Reddit: ~47 CSS selectors for old.reddit.com, URL transforms (www→old), post formatting.
- **`hackernews.py`** — Hacker News: CSS selectors, table unwrapping, story block reformatter (compact `▲score 💬comments` format).
- **`medium.py`** — Medium: CSS selectors (data-testid buttons), source param stripping, post-article block removal (Published in/Written by/Responses), footer, avatar dedup. HTML-based detection for unknown custom domains.
- **`huggingface.py`** — Hugging Face: ~16 data-target CSS selectors, filter tag/button soup cleanup, ~30 regex post-processors for tabs/like/follow/deploy/inference/license gate. DatasetViewer removal (192k+ chars), gated model license stripping.
- **`stackoverflow.py`** — Stack Overflow / Stack Exchange: ~16 CSS selectors (sidebars, vote buttons, post menus, user signatures, pagination, stats), soup cleanup (avatars, yellow banners, Collectives promo), ~20 regex post-processors for badges, date attributions, comment headers, footer CTAs. Covers stackoverflow.com, *.stackexchange.com, superuser.com, serverfault.com, askubuntu.com, mathoverflow.net.
- **`redflagdeals.py`** — RedFlagDeals forums: ~30 RFD-specific CSS selectors (header, ads, nav sidebar, filter bar, action buttons), soup cleanup (PTO banners, RFD logo images, SVG icons), ~20 regex post-processors for auth lines, deal scores, voting stats, breadcrumbs, forum rule links. Generic phpBB selectors applied via combined selector from forums.py. Covers forums.redflagdeals.com.
- **`forums.py`** — Generic forum support (XenForo, vBulletin, phpBB, Discourse). Tier 1: URL transform rewrites known forum listing/thread URLs to RSS/Atom feed URLs. Tier 2: autodiscovery via `<link rel="alternate">` for unknown forum pages. Feed parser (stdlib XML), structured markdown formatter. Generic CSS selectors for XenForo/vBulletin/phpBB thread cleanup as HTML fallback. Discourse detected separately (`is_discourse_html`) — gets its own site key with noscript unwrap and generic-only cleanup (no forum selectors) since Discourse content lives inside `<noscript>` for SEO. Known domains: *.bimmerpost.com, vwvortex.com, golfmk7.com, rdforum.org, forums.redflagdeals.com (listings only — threads use redflagdeals.py).
- **`wikipedia.py`** — Wikipedia: CSS selectors for edit buttons, navboxes, TOC, reference lists.

Each site module exports the same interface: `is_<site>(url)`, `SELECTORS_LIST`, and optionally `strip_<site>_junk(soup)` / `postprocess_<site>(markdown)`. To add cleanup for a new site, create a new module following this pattern.

## Search Architecture

`src/fetchaller/search/` handles web search:

- **`__init__.py`** — Main `search()` function, result merging/dedup, 5-minute query cache, per-engine rate limiters (2s Google, 1s DDG), CAPTCHA escalating backoff (2m→5m→15m), lazy session lifecycle.
- **`google.py`** — Google via Opera Mini SSR. UA pool (~14 variants), Opera proxy header fingerprint (X-OperaMini-Features, Phone, Device-Stock-UA), `/url?q=` extraction, `<h3>` title extraction with breadcrumb removal, structural snippet walk-up, CAPTCHA detection (sorry.google.com, /sorry, "unusual traffic", 429).
- **`ddg.py`** — DuckDuckGo HTML endpoint (`html.duckduckgo.com/html/`). `.result` CSS selectors, `uddg=` URL decoding. Only queried on page 1.
- **`models.py`** — `SearchResult` dataclass (title, url, snippet).
- **`tools/search.py`** — MCP tool wrapper calling `search()`.

## Bot Challenge Bypass (Botfighter)

`src/fetchaller/botfighter.py` — Transparent bot challenge detection and solving. ACW (Alibaba Cloud WAF) solved inline with pure Python (~1ms). All others (Cloudflare, Akamai, DataDome, PerimeterX, Imperva, Kasada) use PyDoll headful Chrome with Xvfb. Cookies cached per-domain with optional JSON persistence (auto-detects `/app/data/` in Docker). Geo-redirects handled via `final_url` dual-domain caching.

Key rules: cached cookies MUST use pinned UA + impersonate (no rotation). CF detects headless — always use Xvfb or offscreen window. Extract ALL cookies from browser (sites layer multiple protections).

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

### Docker Local Testing

```bash
# Build and run locally
docker compose -f docker-compose.local.yml up --build

# Test endpoints
curl http://localhost:6000/health
curl -X POST http://localhost:6000/mcp \
  -H "Authorization: Bearer test-api-key-local" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'

# Stop
docker compose -f docker-compose.local.yml down
```

### Common Mistake

Do NOT test against the production version (Docker image from GHCR). Changes to `src/fetchaller/` won't be reflected unless you rebuild locally.

## Security Note

This tool bypasses domain restrictions. It's intended for research workflows where permission prompts are disruptive. The user has explicitly allowed this via their settings.

SSRF protection blocks:
- localhost, 127.0.0.1, ::1
- Private IPs (10.x, 172.16-31.x, 192.168.x)
- Link-local addresses
- DNS rebinding services (nip.io, xip.io, localtest.me, sslip.io)
- Hostnames that resolve to private IPs

## Remote Usage (HTTP Mode)

fetchaller-mcp can be deployed remotely at `https://mcp.fetchaller.com/mcp`.

### Running in HTTP Mode

```bash
# With authentication (required for production)
MCP_API_KEY=your-secret-key python -m fetchaller.main --http

# Or use Docker
docker compose up -d
```

### Claude Code/Desktop Configuration (Remote)

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

### Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/mcp` | POST | Bearer token or OAuth | MCP protocol endpoint |
| `/mcp` | HEAD | None | Protocol version discovery |
| `/mcp` | GET/DELETE | None | Returns 405 Method Not Allowed |
| `/health` | GET | None | Health check for Docker |
| `/.well-known/oauth-protected-resource` | GET | None | OAuth resource metadata |
| `/.well-known/oauth-authorization-server` | GET | None | OAuth server metadata |
| `/register` | POST | None | Dynamic client registration |
| `/authorize` | GET/POST | None | OAuth authorization (login page) |
| `/token` | POST | None | OAuth token exchange (PKCE required) |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_PORT` | 6000 | Server port (1-65535) |
| `MCP_API_KEY` | (none) | Bearer token - **required** in HTTP mode |
| `MCP_SERVER_URL` | `http://localhost:$PORT` | Public URL for OAuth redirects |
| `JWT_SECRET` | (derived) | Secret for OAuth JWTs (set in production) |
| `RATE_LIMIT_REQUESTS` | 100 | Requests per minute per IP |
| `CHROME_IDLE_TIMEOUT` | 60 | Minutes before idle Chrome shuts down |
| `COOKIE_CACHE_PATH` | auto | Cookie persistence path. Auto-detects `/app/data/` in Docker. |
| `PUID` | 99 | User ID for file permissions (99 = Unraid `nobody`) |
| `PGID` | 100 | Group ID for file permissions (100 = Unraid `users`) |

## Claude.ai Custom Connector (OAuth)

fetchaller supports OAuth 2.1 for Claude.ai web/mobile connectors. This allows cross-platform sync without manual configuration.

### Setup

1. **Deploy fetchaller** with `MCP_API_KEY` and `MCP_SERVER_URL` set:
   ```bash
   MCP_API_KEY=your-secret-key \
   MCP_SERVER_URL=https://mcp.fetchaller.com \
   python -m fetchaller.main --http
   ```

2. **Add connector in Claude.ai**:
   - Go to Settings → Connectors → Add Custom Connector
   - Name: `fetchaller`
   - URL: `https://mcp.fetchaller.com/mcp`
   - Leave OAuth Client ID/Secret **blank** (uses Dynamic Client Registration)

3. **Authorize**: Enter your `MCP_API_KEY` when prompted

### How It Works

1. Claude discovers OAuth endpoints via `/.well-known/oauth-authorization-server`
2. Claude registers itself via `/register` (Dynamic Client Registration)
3. User enters API key on `/authorize` page
4. Claude exchanges auth code for JWT token via `/token` (PKCE required)
5. Claude uses JWT for all MCP requests

### Authentication Methods

The server accepts **either**:
- **Raw API key**: `Authorization: Bearer YOUR_MCP_API_KEY`
- **OAuth JWT token**: Issued by the `/token` endpoint

Both work identically for MCP requests. OAuth is for Claude.ai connectors; raw API key is for Claude Code/Desktop config files.

### Docker Deployment

```bash
# Build and run locally
docker compose -f docker-compose.local.yml up --build

# Production (uses GHCR image)
docker compose pull
docker compose up -d
```

### Scaling Limitations

**In-memory rate limiting**: Rate limits are stored in-memory per container. If you run multiple container replicas:
- Each replica has separate rate limit state
- A client could exceed the intended limit by hitting different replicas
- For horizontal scaling, use external rate limiting (e.g., Redis, Caddy rate-limit plugin, or a load balancer)
