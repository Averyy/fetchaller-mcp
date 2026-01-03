# fetchaller-mcp

MCP server that fetches any URL and returns clean markdown. No domain restrictions, no permission prompts.

## The Problem

Claude Code's built-in `WebFetch` requires per-domain permissions with no wildcard support. Every new domain triggers a permission prompt. This is tedious for research tasks.

## The Solution

One MCP server with one permission rule = all domains work without prompts.

- **WebSearch**: Keep using it for discovering URLs (it's good)
- **fetchaller**: Use for reading URLs (no restrictions)

## Quick Start

```bash
# Clone and install
git clone https://github.com/Averyy/fetchaller-mcp.git
cd fetchaller-mcp
npm install

# Add to Claude Code
claude mcp add fetchaller -- node /path/to/fetchaller-mcp/index.js
```

Add permission to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp__fetchaller__fetch"]
  }
}
```

Restart Claude Code.

## Recommended CLAUDE.md Addition

Add this to your project's `CLAUDE.md` (or global `~/.claude/CLAUDE.md`) to instruct Claude to prefer fetchaller:

```markdown
## Web Research Workflow

When you need to read/fetch content from URLs:

**DO use:** `mcp__fetchaller__fetch` - no domain restrictions, no permission prompts
**DO NOT use:** `WebFetch` - requires per-domain permission prompts

1. **Search**: Use `WebSearch` to find relevant URLs
2. **Fetch**: Use `mcp__fetchaller__fetch` to read the content
```

## Usage

The `mcp__fetchaller__fetch` tool is now available:

```
# Fetch a URL
fetch https://example.com

# Fetch with token limit
fetch https://example.com maxTokens=10000
```

### Web Research Pattern

1. Use `WebSearch` to find URLs
2. Use `mcp__fetchaller__fetch` to read them

The CLAUDE.md file instructs Claude to prefer fetchaller over WebFetch.

## Tool Reference

### `fetch(url, maxTokens?)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| url | string | required | URL to fetch (http/https) |
| maxTokens | number | 25000 | Max tokens to return |

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
| Plain text | Returned as-is |
| PDF/binary | Error message |
| Timeout | Error after 10s |
| Huge page | Truncated at maxTokens |

## How It Works

1. Validates URL (http/https only)
2. Fetches with browser-like headers
3. Detects content type
4. For HTML: strips junk, converts to markdown via Turndown
5. For JSON/text: returns raw
6. Truncates to token limit

## Files

```
fetchaller-mcp/
├── package.json    # Dependencies
├── index.js        # MCP server (~170 lines)
├── CLAUDE.md       # Instructions for Claude
└── README.md       # This file
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
