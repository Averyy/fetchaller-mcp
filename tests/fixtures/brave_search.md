# Brave Search captures

Captured 2026-10-25 through wafer's ordinary default profile, without a browser
solver or account. Query: `Python asyncio documentation`.

- `brave_page1.html`: HTTP 200, 20 organic results.
- `brave_page2.html`: HTTP 200 with `offset=1`, 20 organic results, no URL overlap
  with page 1.
- `brave_captcha_429.html`: HTTP 429, Brave's CAPTCHA/JavaScript interstitial.

The first paced direct diagnostic exposed an unguarded-script/multiprocessing
problem; those successful responses remain real captures, but that run is not
a clean acceptance proof. The subsequent guarded stdio MCP run returned page 1
successfully and was blocked on page 2. See `docs/brave-search-investigation.md`
for the pacing fix, local wire-timing measurements and remaining uncertainty.
