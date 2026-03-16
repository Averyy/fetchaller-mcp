# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support. Built-in web search.

## Architecture

fetchaller owns content processing + MCP tools. wafer (`~/code/wafer`) owns HTTP transport + anti-detection. fetchaller NEVER does bot solving, impersonation, or cookie management — if a site blocks requests, fix it in wafer. See `docs/architecture.md` for full details.

## Pre-Commit Rules

**ALWAYS run lint and tests before EVERY commit. No exceptions.**

```bash
.venv/bin/ruff check src/ tests/   # Lint (import sorting, style)
.venv/bin/python -m pytest tests/ -x -q   # Tests
```

If ruff fails, fix with `.venv/bin/ruff check --fix src/ tests/` and verify again. CI runs `uv run ruff check src/ tests/` — if you skip this locally, the push WILL fail.

## Testing Rules

**ALWAYS use wafer** (not `urllib`, `requests`, or `httpx`) for HTTP requests — wafer handles TLS fingerprinting and bot protection transparently.

**ALWAYS manually test** every new feature/site before committing. Unit tests alone are not sufficient. See `docs/testing.md` for full testing guide (writing tests, live testing, test organization).

## Development & Testing

**CRITICAL**: When testing changes to this MCP server, you MUST use the local version, not the production Docker image.

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

Do NOT test against the production version (Docker image from GHCR).

## Landing Page

`landing/` contains the static site deployed to fetchaller.com. Read `docs/design-style-guide.md` before any visual changes. Always invoke the `frontend-design` skill (`/frontend-design`) when making visual changes.

**`landing/llms.txt`** — LLM-readable project summary. **Keep this in sync when adding new tools, sites, or features.**

## Docs Reference

- `docs/architecture.md` — System design: fetchaller vs wafer boundary, content modules, search, HTTP transport
- `docs/site-apis.md` — Site-specific API clients: AliExpress MTop, Mouser/DigiKey, Kijiji GraphQL, Craigslist SAPI, Facebook Marketplace GraphQL, eBay search extraction
- `docs/testing.md` — Test organization, writing tests, live testing rules, test URLs
