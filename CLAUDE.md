# fetchaller-mcp

MCP server for fetching any URL without domain restrictions.

## IMPORTANT: Tool Selection

When you need to read/fetch content from URLs:

**DO use:** `mcp__fetchaller__fetch` - no domain restrictions, no permission prompts
**DO NOT use:** `WebFetch` - requires per-domain permission prompts

For searching the web, continue using `WebSearch` (it works well).

## Web Research Workflow

1. **Search**: Use `WebSearch` to find relevant URLs
2. **Fetch**: Use `mcp__fetchaller__fetch` to read the content

This workflow avoids permission prompts for every domain.

## Tool: `mcp__fetchaller__fetch`

```
mcp__fetchaller__fetch(url: string, maxTokens?: number)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| url | string | required | Any http/https URL |
| maxTokens | number | 25000 | Max tokens to return |

## Examples

**Read a specific page:**
```
mcp__fetchaller__fetch("https://example.com/docs")
```

**Read with token limit:**
```
mcp__fetchaller__fetch("https://example.com/long-page", 10000)
```

**Research workflow:**
1. WebSearch "topic keywords"
2. For each relevant URL: mcp__fetchaller__fetch(url)

## What It Does

- Fetches any HTTP/HTTPS URL
- Converts HTML to clean markdown (strips scripts, styles, nav, footer, ads)
- Returns JSON and plain text as-is
- Shows redirect destinations
- Times out after 10 seconds
- Truncates at token limit

## Security Note

This tool bypasses domain restrictions. It's intended for research workflows where permission prompts are disruptive. The user has explicitly allowed this via their settings.
