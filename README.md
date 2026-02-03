# fetchaller-mcp

Fetch Reddit and any website in Claude Code without permission prompts. A WebFetch alternative with no domain restrictions.

## Why fetchaller?

Claude Code's built-in `WebFetch` asks permission for every new domain and blocks Reddit entirely. fetchaller fixes both:

- **`fetch`**: Read any URL without permission prompts
- **`browse_reddit`**: Browse subreddit listings (hot/new/top/rising)
- **`search_reddit`**: Search Reddit posts globally or within a subreddit

## Quick Start

```bash
# Clone and install
git clone https://github.com/Averyy/fetchaller-mcp.git
cd fetchaller-mcp
npm install

# Add to Claude Code
claude mcp add fetchaller -- node /path/to/fetchaller-mcp/index.js
```

Add permissions to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__fetchaller__fetch",
      "mcp__fetchaller__browse_reddit",
      "mcp__fetchaller__search_reddit"
    ]
  }
}
```

Restart Claude Code.

## Recommended CLAUDE.md Addition

Add this to your project's `CLAUDE.md` (or global `~/.claude/CLAUDE.md`) to instruct Claude to prefer fetchaller:

```markdown
## Web Fetching

**Use fetchaller instead of WebFetch** (no domain restrictions). If a dedicated MCP exists (GitHub, Slack, etc.), use that instead.

## Reddit Searching and Browsing

Use `mcp__fetchaller__browse_reddit` to browse subreddits, `mcp__fetchaller__search_reddit` to find posts, and `mcp__fetchaller__fetch` to read full discussions.
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

### Web Research Pattern

1. Use `WebSearch` to find URLs
2. Use `mcp__fetchaller__fetch` to read them

The CLAUDE.md file instructs Claude to prefer fetchaller over WebFetch.

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
| PDF/binary | Error message |
| Timeout | Error after timeout (default 10s) |
| Huge page | Truncated at maxTokens |

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

## How It Works

1. Validates URL (http/https only)
2. Fetches with browser-like headers
3. Detects content type
4. For HTML: strips junk, converts to markdown via Turndown
5. For JSON/XML/CSV/text: returns raw
6. Truncates to token limit

## Remote Deployment (HTTP Mode)

Deploy fetchaller as a remote MCP server for Claude.ai, Claude Desktop, or any MCP client.

### Quick Start

```bash
# Run with authentication
MCP_API_KEY=your-secret-key node index.js --http

# Or use Docker
docker compose up -d
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
| `HTTP_PORT` | 6000 | Server port |
| `MCP_API_KEY` | (required) | Bearer token for auth |
| `MCP_SERVER_URL` | `http://localhost:$PORT` | Public URL for OAuth |
| `RATE_LIMIT_REQUESTS` | 100 | Requests/minute per IP |

## Files

```
fetchaller-mcp/
├── package.json        # Dependencies
├── index.js            # MCP server
├── CLAUDE.md           # Instructions for Claude
├── docker-compose.yml  # Docker deployment
├── Dockerfile          # Container build
└── README.md           # This file
```

## Dependencies

- `@modelcontextprotocol/sdk` - MCP protocol
- `cheerio` - HTML parsing
- `turndown` - HTML to markdown

## Testing

```bash
# Verify syntax
node --check index.js

# Test imports
node -e "import('./index.js')"

# In Claude Code after setup
"fetch https://example.com and show me the content"
```

## License

MIT
