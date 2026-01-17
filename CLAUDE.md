# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support.

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

## Development & Testing

**CRITICAL**: When testing changes to this MCP server, you MUST use the local version, not the production npm package.

### Testing Local Changes

1. **Update MCP config** to use the local path:
   ```json
   {
     "mcpServers": {
       "fetchaller": {
         "command": "node",
         "args": ["/Users/avery/Code/fetchaller-mcp/index.js"]
       }
     }
   }
   ```

2. **Restart Claude Code** to reload the MCP server with local changes

3. **Test the changes** using the fetchaller tools

### Common Mistake

Do NOT test against the production version (`npx fetchaller-mcp` or global install). Changes to `index.js` won't be reflected unless you're running the local file directly.

### Quick Verification

To verify you're using the local version, check that any code changes in `index.js` are reflected in the tool behavior.

## Security Note

This tool bypasses domain restrictions. It's intended for research workflows where permission prompts are disruptive. The user has explicitly allowed this via their settings.
