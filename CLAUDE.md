# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support.

## Tool Selection

**DO NOT use `WebFetch`** - use fetchaller instead (no domain restrictions, no permission prompts).

**fetchaller replaces `WebFetch`, not dedicated MCPs.** If a dedicated MCP exists for a service (GitHub, Slack, etc.), use that MCP instead. Use fetchaller for general web fetching.

For searching the web, continue using `WebSearch` (it works well).

## Web Research Workflow

1. **Search**: Use `WebSearch` to find relevant URLs
2. **Fetch**: Use `mcp__fetchaller__fetch` to read the content

This workflow avoids permission prompts for every domain.

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
- Returns JSON, XML/RSS, CSV, and plain text as-is
- Shows redirect destinations
- Configurable timeout (default: 10 seconds)
- Truncates at token limit

## Reddit Support

fetchaller automatically transforms Reddit URLs to use `old.reddit.com` for 65-70% token savings:

- `www.reddit.com/*` → `old.reddit.com/*`
- `reddit.com/*` → `old.reddit.com/*`
- `old.reddit.com/*` → unchanged
- Explicit `.json` URLs → unchanged (if you specifically want JSON)

Just pass any Reddit URL directly - the tool handles the optimization automatically.

## Security Note

This tool bypasses domain restrictions. It's intended for research workflows where permission prompts are disruptive. The user has explicitly allowed this via their settings.
