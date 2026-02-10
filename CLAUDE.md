# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support.

## Debugging Rules

**NEVER blame external services** (Claude, Anthropic, Google, Reddit, etc.) for issues. If something isn't working, the problem is in THIS codebase. Investigate our code first, add logging, and find the real cause. Blaming external parties wastes time.

## Testing Rules

**ALWAYS use the same approach the code uses when testing.** For HTTP requests, use `curl_cffi` (not `urllib` or `requests`) because it has TLS fingerprint impersonation that bypasses bot protection. Test with multiple pages before making performance claims.

Test URLs for benchmarking:
- Reddit: `https://www.reddit.com/r/homelab/`, `https://old.reddit.com/r/homelab/`
- Scrapers often blocked: `https://news.ycombinator.com/`, `https://www.nytimes.com/`
- Simple: `https://example.com/`, `https://httpbin.org/html`
- Cloudflare protected: `https://apollomapping.com`

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
1. WebSearch "topic keywords"
2. For each relevant URL: mcp__fetchaller__fetch(url)

## What It Does

- Fetches any HTTP/HTTPS URL
- Converts HTML to clean markdown (strips scripts, styles, nav, footer, ads)
- Extracts text from PDFs
- Returns JSON, XML/RSS, CSV, and plain text as-is
- Shows redirect destinations
- Configurable timeout (default: 10 seconds)
- Truncates at token limit

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

- **`html.py`** — Generic pipeline only. Universal junk selectors (nav, footer, ads, cookie banners, modals), markdownify conversion, whitespace cleanup. Dispatches to site modules based on URL.
- **`github.py`** — GitHub: ~30 CSS selectors, soup cleanup, ~30 regex post-processors, URL transforms, file tree extraction.
- **`reddit.py`** — Reddit: ~47 CSS selectors for old.reddit.com, URL transforms (www→old), post formatting.
- **`hackernews.py`** — Hacker News: CSS selectors, table unwrapping, story block reformatter (compact `▲score 💬comments` format).
- **`medium.py`** — Medium: CSS selectors (data-testid buttons), source param stripping, post-article block removal (Published in/Written by/Responses), footer, avatar dedup. HTML-based detection for unknown custom domains.
- **`huggingface.py`** — Hugging Face: ~16 data-target CSS selectors, filter tag/button soup cleanup, ~30 regex post-processors for tabs/like/follow/deploy/inference/license gate. DatasetViewer removal (192k+ chars), gated model license stripping.
- **`stackoverflow.py`** — Stack Overflow / Stack Exchange: ~16 CSS selectors (sidebars, vote buttons, post menus, user signatures, pagination, stats), soup cleanup (avatars, yellow banners, Collectives promo), ~20 regex post-processors for badges, date attributions, comment headers, footer CTAs. Covers stackoverflow.com, *.stackexchange.com, superuser.com, serverfault.com, askubuntu.com, mathoverflow.net.
- **`wikipedia.py`** — Wikipedia: CSS selectors for edit buttons, navboxes, TOC, reference lists.

Each site module exports the same interface: `is_<site>(url)`, `SELECTORS_LIST`, and optionally `strip_<site>_junk(soup)` / `postprocess_<site>(markdown)`. To add cleanup for a new site, create a new module following this pattern.

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
