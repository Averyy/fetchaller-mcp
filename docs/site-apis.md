# Site-Specific API & Extraction Clients

Reference for sites that use dedicated API clients or non-trivial extraction (not just CSS selectors + postprocessors).

## Alibaba/AliExpress

### Alibaba.com

SSR HTML only — no MTop API exists for the international site (`h5api.m.alibaba.com` serves 1688.com domestic China only). Extract embedded JSON from `window.detailData` (product) and `window.__page__data_sse10._offer_list` (search).

### AliExpress

MTop API at `acs.aliexpress.com` for product details (token bootstrap + MD5 signing). SSR HTML fallback for search. Wafer handles TMD transparently. Separate reviews API at `feedback.aliexpress.com/pc/searchEvaluation.do`.

**MTop client**: `src/fetchaller/aliexpress/mtop.py` — token lifecycle, request signing, auto-refresh.

Key gotchas:
- Product pages are CSR (`isCSR: true`). `window.runParams` is declared empty — data comes from MTop `mtop.aliexpress.pdp.pc.query`, NOT embedded HTML.
- MTop response may be JSONP (`mtopjsonp1({...})`). Always strip wrapper before JSON.parse.
- MTop needs locale params: `_lang`, `_currency`, `country`, `clientType` required in data dict.
- `SITEM_NOT_EXIST`: Delisted products return `ret: ["SUCCESS"]` but `errorCode == "SITEM_NOT_EXIST"`.
- TMD = session-based block (not rate limit). Wafer solves this transparently.
- API URL uses dots: `/h5/mtop.aliexpress.pdp.pc.query/1.0/` — dots preserved in path.
- `sellerPositiveRate` (not `positiveRate`) for store info. `salePriceLocal` alongside `salePriceString`.

## Mouser/DigiKey APIs

Both block HTML scraping. When API keys are configured, `fetch_url()` intercepts their URLs and routes to dedicated API modules. Without keys, falls through to HTML pipeline (wafer handles Akamai challenges).

- **`src/fetchaller/mouser/api.py`** — Mouser Search API client. Simple API key auth (`?apiKey=KEY`). Keyword search + part number lookup. Extracts MPN from URL path. Rate limited (30 req/min).
- **`src/fetchaller/digikey/api.py`** — DigiKey API client with OAuth2 `client_credentials` token manager. Keyword search + product details lookup. Extracts part info from URL path. Rate limited (120 req/min burst).

Env vars: `MOUSER_API_KEY`, `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`.

## Kijiji GraphQL

`src/fetchaller/kijiji/` — Unauthenticated Apollo GraphQL client for search (`/b-*`) and listing (`/v-*`) pages. CSR site — HTML pipeline is fallback only. See `api.py` docstrings for query details.

Key gotchas:
- Price type names: API returns `GIVE_AWAY` (not `FREE`), `CONTACT` (not `PLEASE_CONTACT`). `FIXED` and `SWAP_TRADE` are correct.
- Price union types: `StandardAmountPrice`, `AutosDealerAmountPrice` (both `... on AmountPrice`), `NonAmountPrice` (CONTACT, GIVE_AWAY, SWAP_TRADE).
- Free stuff category: Items have `price: null`. `GIVE_AWAY` only for explicitly marked items.
- Bathroom tenths encoding: `numberbathrooms` = value * 10 (10=1, 15=1.5).
- Condition values: `new`, `usedlikenew`, `usedgood`, `usedfair`, `usedpoor`.
- Listing ID regex: Must accept trailing `/`, `?`, `#` after digits.
- Seller types: `OWNER`, `COMMERCIAL`. Formatted via `.replace("_", " ").title()`.

## Craigslist SAPI

Craigslist search pages are client-side rendered — the HTML pipeline produces garbage. `fetch_url()` intercepts CL search URLs (`/search/*`) and routes to SAPI (`sapi.craigslist.org`) for structured JSON.

- **`src/fetchaller/craigslist/sapi.py`** — SAPI v8 client using wafer. Returns proper JSON objects (not compact-encoded). Up to 120 items per request with `totalResultCount`. Items have `title`, `priceString`, `postingId`, `seo`, `categoryAbbr`, `postedDate` (Unix timestamp), and `location` (hostname, subareaAbbr, description). Area IDs discovered from page HTML (`"areaId":NNN`) and cached in memory per hostname. Response also provides `location.city` for proper area names (e.g., "new york city", "SF bay area"). Relative time formatting for posted dates.
- **`src/fetchaller/craigslist/search.py`** — Search entry point. Flow: extract params from URL → resolve area ID (cached after first fetch) → call SAPI → parse items → format numbered markdown with "showing X of Y" when paginated. Forwards CL search params to SAPI (query, sort, min/max price, hasPic, vehicle filters, etc.). Rate limited (2s base).
- **`src/fetchaller/content/craigslist.py`** — URL detection + HTML cleanup (for individual listing pages which are SSR).
- **Individual listing pages**: SSR HTML, handled by existing `content/craigslist.py` pipeline (no SAPI intercept).

## Facebook Marketplace GraphQL

Facebook Marketplace is 100% CSR with obfuscated CSS — HTML scraping is not viable. `fetch_url()` intercepts Marketplace URLs and routes to the GraphQL API.

- **`src/fetchaller/facebook_marketplace/`** — GraphQL client, search, and listing detail. See package `README.md` for doc_ids and architecture details.
- **`src/fetchaller/content/facebook_marketplace.py`** — URL detection only. Only matches `/marketplace/*` paths.
- **IP reputation concern**: Datacenter IPs may get blocked (error 1675004). Rate limited (3s base).

## Marketplace Search Orchestrator

`src/fetchaller/marketplace/` — Unified search across Kijiji, Craigslist, and Facebook Marketplace. Runs platform searches concurrently via `asyncio.gather` with 45s timeout.

- **`search.py`** — Main `search_marketplace()` orchestrator. Resolves locations, maps aliases, launches concurrent searches, collects results into grouped markdown.
- **`aliases.py`** — Cross-platform alias dicts: `SORT_MAP`, `CATEGORY_MAP`, `CONDITION_MAP`. Maps human-readable values ("cars", "price_asc", "like_new") to platform-specific codes.

Key behaviors:
- **Kijiji auto-skip**: Detects Canadian vs US location via `is_canadian_location()`. Kijiji is Canada-only and auto-removed for US cities.
- **FB geocode disambiguation**: Appends ", Canada" to bare Canadian city names before FB geocoding (e.g. "vancouver" → "vancouver, Canada") to avoid US bias (Vancouver, WA).
- **Location resolution**: CL and Kijiji use static maps with fuzzy matching (`difflib.get_close_matches`, cutoff=0.7). FB uses its native geocode API.
- **Graceful degradation**: If one platform errors or times out, others still return. All-fail returns an error with per-platform details.

## eBay Search Extraction

eBay search pages are SSR — no API intercept needed. Search results are extracted from `.s-item` DOM elements in `clean_html()` (before CSS selectors fire), formatted as a numbered list, and injected as a marker div. The postprocessor replaces all markdownified noise with the clean extracted data.

## Job Boards (Ashby, Gem, Lever, Greenhouse)

Every supported job board platform exposes both an individual-posting API and a board-listing API. `fetch_url()` intercepts both URL shapes per platform and skips the SPA body entirely.

| Platform | Posting URL | Board URL | API base |
|----------|-------------|-----------|----------|
| Ashby    | `jobs.ashbyhq.com/{org}/{uuid}` | `jobs.ashbyhq.com/{org}` | `api.ashbyhq.com/posting-api/job-board/{org}` |
| Gem      | `jobs.gem.com/{board}/{extId}`  | `jobs.gem.com/{board}`   | `api.gem.com/job_board/v0/{board}/job_posts/` (REST, Greenhouse-shaped) + `jobs.gem.com/api/public/graphql` (per posting) |
| Lever    | `jobs.lever.co/{company}/{id}`  | `jobs.lever.co/{company}` (SSR — no intercept) | `api.lever.co/v0/postings/{company}/{id}` |
| Greenhouse | `boards.greenhouse.io/{token}/jobs/{id}` (and `?gh_jid=&gh_src=` variants) | `boards.greenhouse.io/{token}` (SSR — no intercept) | `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` |

- **`src/fetchaller/content/ashby.py`** — Postings extracted from `window.__appData.posting` in the SSR'd HTML (no API needed per-posting). Board index uses the public posting-api REST endpoint. Board response is grouped by `department` for readability; `isListed=False` jobs are filtered out.
- **`src/fetchaller/content/gem.py`** — Postings via Apollo GraphQL (`ExternalJobPostingQuery`). Board listings via the public REST endpoint, which is Greenhouse-shaped (flat list of jobs with `departments[]`, `location.name`, `location_type`, `employment_type`, `absolute_url`).
- **`src/fetchaller/content/lever.py`** + **`src/fetchaller/content/greenhouse.py`** — Posting API paths only. Their board index pages are SSR'd and render correctly through the generic HTML pipeline.

Key behaviors:
- **API 404 fall-through**: When an org/board isn't hosted on that platform (e.g. `jobs.ashbyhq.com/anthropic` — Anthropic doesn't use Ashby), the API returns 404 and the dispatch falls through to the normal HTML fetch. No error surfaced to the caller.
- **Order in dispatch matters**: Posting URLs are checked before board URLs (`/{org}/{uuid}` is more specific than `/{org}`). Posting regex requires two path segments; board regex requires exactly one.
- **Renderers preserve raw field names**: Each platform's renderer dumps the API's own keys/enums (`FullTime`, `REMOTE`, `full_time`, `hybrid`) without translation — companies expose different metadata, and translation loses signal.
