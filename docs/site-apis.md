# Site-Specific API & Extraction Clients

Reference for sites that use dedicated API clients or non-trivial extraction (not just CSS selectors + postprocessors).

## Alibaba/AliExpress

### Alibaba.com

SSR HTML only — no MTop API exists for the international site (`h5api.m.alibaba.com` serves 1688.com domestic China only). Extract embedded JSON from `window.detailData` (product) and `window.__page__data_sse10._offer_list` (search).

### AliExpress

MTop API at `acs.aliexpress.com` for product details (token bootstrap + MD5
signing). SSR HTML fallback for search. Wafer handles TMD transparently. When a
live search is immediately followed by detail for one of its exact canonical
product IDs, fetchaller retains a bounded 15-minute copy of that validated
listing. If MTop is unavailable, it returns that narrower title/price/rating
snapshot with an explicit source label before attempting the canonical
browser-rendered product document. Separate reviews API at
`feedback.aliexpress.com/pc/searchEvaluation.do`.

**MTop client**: `src/fetchaller/aliexpress/mtop.py` — token lifecycle, request signing, auto-refresh.

Key gotchas:
- Product pages are CSR (`isCSR: true`). `window.runParams` is declared empty — full modules come from MTop `mtop.aliexpress.pdp.pc.query`, not raw HTTP HTML. A wafer browser render may expose JSON-LD/DOM detail and is the final fallback after MTop and an exact recent search snapshot.
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

## realtor.ca (home search + listings)

`src/fetchaller/realtor/` — Canadian real-estate search via the Imperva-protected `api2.realtor.ca` XHR API plus SSR listing-detail pages. The public map page is a CSR shell, so all search data comes from the API. wafer 0.2.4 passes Imperva transparently: a fresh session free-passes via the native-TLS fallback at light load (no browser), and under escalation the `browser_solver` solves on the **origin page** (www.realtor.ca) and replays the request as a same-site XHR (with `.realtor.ca` cookie-replay as fallback). The old "Error 15" interstitial was self-inflicted by wafer ≤0.2.3 top-level-navigating the API host; 0.2.4 no longer does, so it never appears. fetchaller does no challenge handling. The shared session sets `rate_limit=1.5` **and** passes the shared `browser_solver`; both help avoid/clear Imperva's rate-based challenge.

- **`api.py`** — transport, URL detection, geocode, search, listing fetch/parse.
  - Geocode: `GET Location.svc/SubAreaSearch?Area={place}` → `SubArea[0].Viewport` (NE/SW bbox) + `GEOId` (city `g30_*`, neighbourhood `g20_*`; postal codes may yield an empty GEOId but a valid viewport).
  - Search: `POST Listing.svc/PropertySearch_Post` (form-encoded). Returns `Paging` (`TotalRecords`; only 600 = `MAX_API_RECORDS` are returnable across 50 pages), `Results` (list view) and `Pins` (map clusters: count/lat/long/propertyId). Either a bbox or `GeoIds` scopes the search — GeoId-only works.
  - Filters (all verified live): `TransactionTypeId` 2=sale / 3=rent; `PropertySearchTypeId` (PROPERTY_TYPE: any/residential/condo/recreational/vacant-land/multi-family/agriculture/parking); `BuildingTypeId` (BUILDING_TYPE: house=1/duplex=2/triplex=3/townhouse=16/apartment=17/other=19); `PriceMin/Max` for sale, **`RentMin/Max` for rent** (PriceMin is ignored on rentals); `BedRange`/`BathRange` in `min-max` form, 0=unbounded (`3-0` = 3+); `OwnershipTypeGroupId` freehold=1/condo=2; `Sort` newest=6-D/oldest=6-A/price-asc=1-A/price-desc=1-D.
  - Listing detail (`/real-estate/{id}/{slug}`, `/immobilier/...`): SSR HTML. Parses `#listingPriceValue`, `#listingAddress`, `#galleryBeds/Baths`, `#propertyDescriptionCon`, the `.propertyDetailsSectionContentLabel/Value` pairs (deduped), the room-by-room breakdown (`.listingDetailsRoomDetailsCon` → label + metric dimensions; condos often omit it, houses include it), the listing agent (`.realtorCardName`) + brokerage (`[id^=OfficeCard]`), the location/cross-streets block (`#LocationDescription`), MLS from the `id*=MLS` element, and coordinates from the embedded directions link.
  - Similar listings are lazy-loaded on the site (only a spinner in the SSR), so we reconstruct them: a geo-bounded `PropertySearch` around the listing's coords, price-banded 0.6–1.5×, excluding the listing itself.
  - Search-URL handling: SEO pages (`/{prov}/{city}[/{hood}]/real-estate`) are fetched to read the embedded GeoId (most-frequent `g\d0_*` token) + H1 place name, then searched by GeoId; `/map` URLs carry all filters in the query/hash fragment (`_map_kwargs`).
- **`render.py`** — search-results and listing-detail markdown.
- **`search.py`** — `search_realtor()` (the `search_realtor` MCP tool: geocode → search → render) and `get_realtor()` (the `fetch_url` dispatch for listing + search URLs; `raw=True` on a listing falls through for the SSR HTML).

## wellfound.com (startup jobs)

`src/fetchaller/wellfound/` — startup job search, job-detail, and company pages. Every page is server-rendered; wellfound is behind DataDome (+ an XHR-only Cloudflare Turnstile that does **not** gate navigation), and wafer 0.2.4 returns the real SSR document via its browser passthrough — fetchaller does no challenge handling. The shared session passes the shared `browser_solver`, sets `rate_limit=2.5`, and relies on `cache_dir` (the earned DataDome cookie persists there so re-solves are rare — reliability depends on it; `create_server()` sets it). Job pages **require the slug**: `/jobs/{id}-{slug}` returns the `JobPosting` JSON-LD, but a bare `/jobs/{id}` resolves to wellfound's 200 "Page not found" shell (detected via `is_not_found_page`, surfaced as an actionable error). fetchaller's own search renderers always emit the id-slug form.

| Page | URL | Data source |
|------|-----|-------------|
| Job detail | `/jobs/{id}-{slug}` | `schema.org/JobPosting` JSON-LD (no Apollo) |
| Role search | `/role/r/{role}` (remote), `/role/l/{role}/{loc}` | `__NEXT_DATA__` Apollo: `JobListingSearchResult` + `StartupResult` |
| Location search | `/location/{loc}` | same as role search |
| Jobs feed | `/jobs` | `__NEXT_DATA__` Apollo: `JobListing` (+ `Startup` refs) |
| Company | `/company/{slug}` | `__NEXT_DATA__` Apollo: a full `Startup` object |

- **`api.py`** — URL detection, session, `__NEXT_DATA__`/JSON-LD extraction, and Apollo-cache navigation. The Apollo state is a normalized cache keyed `Type:id` with `{"__ref": ...}` links; `deref`, `entities`, `connection` (handles `field({"first":N})` arg-suffixed keys), and `connection_nodes` resolve it.
- **`render.py`** — three renderers. Search is company-grouped when `StartupResult` entries carry `highlightedJobListings` (role/location pages), else a flat `JobListing` list (the `/jobs` feed, whose `StartupResult`s are empty stubs). Helpers: `_money` (totalRaised → $4.6M), `_company_size` (`SIZE_51_200` → "51-200 employees"), `_clean_url` (wellfound stores junk like `twitter.com/https://x.com/foo`).
- **`page.py`** — `get_wellfound()` dispatch (job/company/search) for `fetch_url`; `raw=True` falls through for the SSR HTML. No MCP search tool — searches run via `fetch()` on the `/role/*`, `/location/*`, `/jobs` URLs.

## Job Boards (Ashby, Gem, Lever, Greenhouse, Dayforce, Cornerstone, Workday, BambooHR, JazzHR, HubSpot)

Every supported job board platform exposes both an individual-posting API and a board-listing API. `fetch_url()` intercepts both URL shapes per platform and skips the SPA body entirely.

| Platform | Posting URL | Board URL | API base |
|----------|-------------|-----------|----------|
| Ashby    | `jobs.ashbyhq.com/{org}/{uuid}` | `jobs.ashbyhq.com/{org}` | `api.ashbyhq.com/posting-api/job-board/{org}` |
| Gem      | `jobs.gem.com/{board}/{extId}`  | `jobs.gem.com/{board}`   | `api.gem.com/job_board/v0/{board}/job_posts/` (REST, Greenhouse-shaped) + `jobs.gem.com/api/public/graphql` (per posting) |
| Lever    | `jobs.lever.co/{company}/{id}`  | `jobs.lever.co/{company}` (SSR — no intercept) | `api.lever.co/v0/postings/{company}/{id}` |
| Greenhouse | `boards.greenhouse.io/{token}/jobs/{id}` (and `?gh_jid=&gh_src=` variants) | `boards.greenhouse.io/{token}` (SSR — no intercept) | `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` |
| Dayforce | `jobs.dayforcehcm.com/{lang}/{namespace}/{board}/jobs/{id}` | `jobs.dayforcehcm.com/{lang}/{namespace}/{board}` | Posting: `__NEXT_DATA__` in SSR'd HTML (no API). Board: POST `jobs.dayforcehcm.com/api/geo/{namespace}/jobposting/search` (CSRF-protected) |
| Cornerstone (CSOD) | `{tenant}.csod.com/ux/ats/careersite/{cid}/home/requisition/{reqid}` | `{tenant}.csod.com/ux/ats/careersite/{cid}/home` | Posting: `{tenant}.csod.com/services/x/job-requisition/v2/requisitions/{reqid}/jobDetails?cultureId={n}`. Board: POST `{us\|eu\|uk\|au}.api.csod.com/rec-job-search/external/jobs` (regional cloud host from `csod.context.endpoints.cloud`) |
| Workday  | `{tenant}.wd{1-103}.myworkdayjobs.com/[{lang}/]{site}/job/{externalPath}` | `{tenant}.wd{1-103}.myworkdayjobs.com/[{lang}/]{site}` | Posting: GET `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job{externalPath}`. Board: POST `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` |
| BambooHR | `{tenant}.bamboohr.com/careers/{id}` | `{tenant}.bamboohr.com/careers` | Posting: GET `{tenant}.bamboohr.com/careers/{id}/detail`. Board: GET `{tenant}.bamboohr.com/careers/list` |
| JazzHR   | `{tenant}.applytojob.com/apply/{id}[/{slug}]` | `{tenant}.applytojob.com/apply` | Posting: schema.org JSON-LD in the SSR'd HTML. Board: SSR'd HTML (`.list-group .list-group-item` items, optional preceding `.department-heading h3`) |
| HubSpot  | `www.hubspot.com/careers/jobs/{id}` (often with vestigial `?gh_jid={same id}`) | (no public board endpoint) | POST `wtcfns.hubspot.com/careers/graphql` with `Job(id: ID!)` operation |

- **`src/fetchaller/content/ashby.py`** — Postings extracted from `window.__appData.posting` in the SSR'd HTML (no API needed per-posting). Board index uses the public posting-api REST endpoint. Board response is grouped by `department` for readability; `isListed=False` jobs are filtered out.
- **`src/fetchaller/content/gem.py`** — Postings via Apollo GraphQL (`ExternalJobPostingQuery`). Board listings via the public REST endpoint, which is Greenhouse-shaped (flat list of jobs with `departments[]`, `location.name`, `location_type`, `employment_type`, `absolute_url`).
- **`src/fetchaller/content/lever.py`** + **`src/fetchaller/content/greenhouse.py`** — Posting API paths only. Their board index pages are SSR'd and render correctly through the generic HTML pipeline.
- **`src/fetchaller/content/dayforce.py`** — Postings extracted from `__NEXT_DATA__.props.pageProps.jobData` in the SSR'd HTML; `site-info` (clientNamespace/jobBoardCode/cultureCode) also pulled from `dehydratedState.queries`. Board listing needs three round-trips: (1) GET the board page for session cookies + `site-info`, (2) GET `/api/auth/csrf` for the NextAuth token, (3) POST `/api/geo/{namespace}/jobposting/search` with `X-CSRF-TOKEN`, `Content-Type: application/json`, and body `{clientNamespace, jobBoardCode, cultureCode, pageNumber, pageSize}`. The validator is strict — `jobBoardId` instead of `jobBoardCode` returns 400 "Culture not found".
- **`src/fetchaller/content/cornerstone.py`** — Both posting and board fetchers start by parsing the inline `csod.context = {...}` blob from the SPA shell for the per-page JWT, `cultureID`, `cultureName`, `corp` tenant slug, and `endpoints.cloud` regional host (one of `us.api.csod.com`, `eu.api.csod.com`, `uk.api.csod.com`, `au.api.csod.com` — the tenant's data residency dictates which). All API calls send `Authorization: Bearer <jwt>` + `CSOD-Accept-Language: {cultureName}`. The JWT carries an explicit `rurls` allowlist of permitted endpoint paths and short expiry, so callers must extract it fresh per request rather than cache it.
- **`src/fetchaller/content/workday.py`** — Workday tenants live on cloud-specific subdomains (`wd1` through `wd103`); the API base `/wday/cxs/{tenant}/{site}` is the same regardless of cloud. Board fetcher pages through `POST /jobs` in batches of 20 (capped at 200 jobs across 10 pages). The request body is `{"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}`; no CSRF or referer required. Posting fetcher just `GET`s `/job{externalPath}` (the `externalPath` carried in each board entry, always starts with `/job/`). URL detection rejects `site` values that look like language codes (e.g. `en-US`) when no language segment is present, to avoid mis-parsing stripped-lang URLs.
- **`src/fetchaller/content/bamboohr.py`** — Both endpoints return clean JSON unauthenticated, no widget script needed. Inactive/unknown tenants are detected naturally: BambooHR redirects unknown `{tenant}.bamboohr.com/careers*` to the marketing site at `www.bamboohr.com/` with `Content-Type: text/html`, which fails JSON parse and falls through. Board renderer groups by `departmentLabel`. Posting renderer flattens the `location` / `atsLocation` dicts into single-line addresses.
- **`src/fetchaller/content/jazzhr.py`** — JazzHR career pages are fully SSR'd. The board page (`/apply`) carries each posting as `<li class="list-group-item">` (optional preceding `.department-heading h3` for grouping); we parse the title link + location text and trim the trailing department name (JazzHR templates append it to the location string). Posting pages embed a `<script type="application/ld+json">` with the full `schema.org/JobPosting` payload (description HTML, datePosted, validThrough, employmentType, jobLocation, hiringOrganization) — we render that directly. Inactive tenants surface as `<title>JazzHR - Inactive Career Page</title>` and parse to zero items.
- **`src/fetchaller/content/hubspot_careers.py`** — HubSpot's careers SPA at `www.hubspot.com/careers/jobs/{id}`. Pure CSR — the served HTML is ~200 KB of chrome with zero job content; the page POSTs a single `Job(id: ID!)` operation to the unauthenticated GraphQL endpoint at `wtcfns.hubspot.com/careers/graphql` to render. The URL's `?gh_jid={id}` query parameter is vestigial: it's HubSpot's own job id, not a Greenhouse one (the `hubspot` Greenhouse board exists but is empty). The `content` field is double-encoded HTML (entity refs inside an HTML string), `description` on questions is single-encoded — `unescape` is idempotent so the renderer always runs it. Dispatch must intercept BEFORE `extract_greenhouse_params_guess`, otherwise that returns `('hubspot', '{id}')` and we burn a 404 round-trip before falling through to the empty-SPA HTML path.

### Embed / white-label detection (post-fetch HTML phase)

When a URL doesn't match a known ATS host, `fetch_url()` still falls through to the generic HTML pipeline, which runs five embed detectors against the page markup before junk-stripping:

| Embed | Detector | Action |
|-------|----------|--------|
| Greenhouse `<div id="grnhse_app">` / `boards.greenhouse.io/embed/job_app` iframe / `?gh_jid=&gh_src=` query params | `is_greenhouse_html` + `extract_greenhouse_params_from_html` | Fetch posting via `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` |
| Dayforce white-label SSR (`__NEXT_DATA__.runtimeConfig.BASE_URL == "https://jobs.dayforcehcm.com/"`) | `extract_dayforce_canonical_board_url` | Rewrite to canonical `jobs.dayforcehcm.com/{lang}/{namespace}/{board}` and run the standard Dayforce board flow |
| Ashby `<script src="https://jobs.ashbyhq.com/{org}/embed">` | `extract_ashby_embed_slug_from_html` | Fetch the canonical board via Ashby's posting-api REST endpoint |
| BambooHR `<div id="BambooHR" data-domain="{tenant}.bamboohr.com">` | `extract_bamboohr_embed_tenant` | Fetch `/careers/list` on the tenant subdomain |
| JazzHR `*.applytojob.com/apply` references (any tag/attribute) | `extract_jazzhr_embed_tenants` | Fetch each tenant's board and aggregate via `render_jazzhr_boards()` |

Each detector runs in order; the first match returns and short-circuits the rest. Output is prefixed with a small `[ATS-hosted board: …]` breadcrumb so callers see which subsystem produced the markdown.

Key behaviors:
- **API 404 fall-through**: When an org/board isn't hosted on that platform (e.g. `jobs.ashbyhq.com/anthropic` — Anthropic doesn't use Ashby), the API returns 404 and the dispatch falls through to the normal HTML fetch. No error surfaced to the caller.
- **Order in dispatch matters**: Posting URLs are checked before board URLs (`/{org}/{uuid}` is more specific than `/{org}`). Posting regex requires two path segments; board regex requires exactly one. For Dayforce, CSOD, BambooHR, JazzHR, and Workday the URL shapes already disambiguate (`.../jobs/{id}` vs `.../home/requisition/{reqid}` vs `.../careers/{id}` vs `.../apply/{id}` vs `.../job/{externalPath}` for postings; bare board paths otherwise).
- **Renderers preserve raw field names**: Each platform's renderer dumps the API's own keys/enums (`FullTime`, `REMOTE`, `full_time`, `hybrid`, `postingType`, `availableCultures`, `locationType: 2`) without translation — companies expose different metadata, and translation loses signal.
- **Pagination quirks**: Workday's salesforce.wd12 tenant returns `total=N` only on page 1 and zeros it on subsequent pages — the Workday board fetcher locks `total` to the first page's value to avoid an early break. Other Workday tenants (CAE, NVIDIA, Mastercard, Adobe) all behave normally.


## LinkedIn public guest jobs

Logged-out `/jobs-guest/` endpoints only. No credentials, no injected cookies,
no apply flow — the detail fragment shows whether an apply button exists but
never exposes an unauthenticated apply URL, and that is where this stops.

| Endpoint | Returns |
|---|---|
| `/jobs-guest/jobs/api/seeMoreJobPostings/search` | HTML `<li>` cards, max 10 per response |
| `/jobs-guest/api/typeaheadHits?typeaheadType=GEO&query=` | JSON array (served as `text/plain`) resolving a location to a `geoId` |
| `/jobs-guest/jobs/api/jobPosting/{id}` | HTML fragment for one posting |

Note the typeahead path: the `/jobs-guest/jobs/api/typeaheadHits` variant is a 404.

**Filters** (all live-confirmed against the logged-out filter form):
`f_TPR` = `r86400`/`r604800`/`r2592000`; `f_WT` = 1 on-site, 2 remote, 3 hybrid;
`f_E` = 1..6 internship→executive; `f_JT` = F/P/C/T/I;
`f_SB2` = 21..25 for $40k..$120k; `f_AL=true` Easy Apply; `f_EA=true` "Under 10
applicants".

`f_AL`/`f_EA` labels come from LinkedIn's own logged-out filter bar, and both
were verified against each returned posting's detail fragment (2026-07-29):
`f_AL` returned 5/5 postings with an Easy Apply button and no off-site link
against a baseline of 0/5; `f_EA` returned 5/5 reading "Be among the first 25
applicants" against a baseline of "Over 200 applicants". The public detail
bands applicant counts at 25, so the exact "under 10" threshold is not
independently observable — what is proven is that it selects low-applicant
postings.

**Card badges.** `.job-posting-benefits__text` carries "Be an early applicant"
and "Actively Hiring" — present on 46 of 60 cards for one live query. It is the
only applicant-volume signal on a logged-out card (no count, no salary), so it
is extracted and rendered.

**Pagination.** `start` is an absolute row offset, not a page number. Rows
0–999 answer; 1000+ returns HTTP 400 with an empty body, which the client
treats as end-of-results rather than an error.

**Two-surface strategy.** The public JSERP page (`/jobs/search`) carries its own
list of **60** cards — six times the fragment endpoint's 10 — in one request. It
ignores `start` (every offset returns the same first card), so it is a one-shot
first-page surface, not a pagination route. `start=0` therefore uses the page
(one request covers any allowed limit); `start>0` uses the fragment endpoint.
Both parse with the same card selectors.

**Deliberately not exposed:**

- `sortBy` — LinkedIn's logged-out surface does not honour it. Measured
  2026-07-29 against `keywords=engineer&location=Toronto`:
  `sortBy=R`, `sortBy=DD`, `sortBy=RD` and `f_SORT=DD` all returned identical
  results from the fragment endpoint across 3 pages; the JSERP page returned an
  identical job-ID sequence for `R` and `DD`; and posting dates were never in
  descending order under any of them. `f_TPR` DID change the result set in the
  same session, so filters reach the backend and sort specifically does not.
  The page merely echoes a supplied `sortBy` back into its own filter links.
  `sort=recent` is applied client-side over the fetched window instead.
- Job types `V`/`O`. Accepted syntactically, but sampled postings reported
  Full-time, so their meaning was never established.
- Salary on cards. No salary markup appeared in any sampled fragment, including
  salary-filtered searches. The output says salary is not published rather than
  implying these postings have none.

**Rate limit.** 3.2s minimum interval (`linkedin_limiter`), the measured safe
operating point across 46 probes with no 403/429/challenge. The blocking
threshold was deliberately never probed, so treat it as a floor.

Modules: `src/fetchaller/linkedin/{api,parse,render,search,url}.py`.
Full endpoint evidence: `.codex-dobby/linkedin_guest_api_spec.md`.

## Big-tech career boards (Eightfold, Workday search, amazon.jobs, Apple, Meta, Uber)

These are the SPA boards behind the large employers. Each is reachable
anonymously, but none of them filters honestly on its own — every one ranks
rather than filters, so a title query returns adjacent roles and a location
query returns a radius. The shared rule for all six clients: **the board's
filter is an optimisation, the client's filter is the guarantee.**
`src/fetchaller/jobfilter.py` holds the matching used by all of them.

| Board | Search endpoint | Detail | Location filter |
|---|---|---|---|
| Eightfold PCS-X | GET `{host}/api/pcsx/search?domain={groupId}` | GET `/api/pcsx/position_details` | `location=` free text |
| Eightfold classic | GET `{host}/api/apply/v2/jobs?domain={groupId}` | GET `/api/apply/v2/jobs/{id}` | `location=` free text |
| Workday | POST `/wday/cxs/{tenant}/{site}/jobs` | GET `/job{externalPath}` | `appliedFacets` (facet name is per-tenant) |
| amazon.jobs | GET `/en/search.json` | via `search.json?base_query={id}` | `normalized_location[]` (exact) |
| Apple | GET `/{locale}/search` (SSR hydration) | `/{locale}/details/{id}/{slug}` | `location={slug}-{CODE}` |
| Meta | POST `/graphql` (persisted query) | SSR blob on `/jobs/{id}/` | `search_input.offices[]` (display name) |
| Uber | POST `/api/loadSearchJobsResults` | none (metadata only) | `params.location[].{country,city}` |

### Eightfold (`src/fetchaller/eightfold/`)

Two generations are live and tenants are split across them, so the client
probes and caches per tenant:

- **PCS-X** (Microsoft, PayPal) — `/api/pcsx/search`. `count` is the real total.
- **classic** (Netflix) — `/api/apply/v2/jobs`. `num` is capped at 10 and
  `count` is only `start + len(positions)`, so the grand total is never known.

A PCS-X-disabled tenant answers `/api/pcsx/search` with `403 {"message": "PCSX
is not enabled for this user."}`; that exact 403 selects the classic path,
while any *other* 403 is a real refusal and is raised. Classic records are
renamed to the PCS-X field names before leaving the module so callers see one
shape.

The `domain` parameter is the Eightfold **group id**, published by every tenant
page as `window._EF_GROUP_ID` (`microsoft.com`, `netflix.com`, `paypal.com`).
It is read live rather than tabled, so a vanity host this repo has never seen
works from its board URL alone. Page size is fixed at 10 on both generations —
`num` and `pageSize` are ignored.

### Workday search (`src/fetchaller/workday/`)

`content/workday.py` owns transport and URL grammar; this package adds
filtering. Two tenant-specific traps:

- **`searchText` cannot be trusted.** On Adobe the Canada slice is 6 postings;
  `searchText="engineer"` cuts it to 4, `searchText="ux"` to 0, and
  `searchText="designer"` changes nothing at all — the same tenant filters,
  over-filters, and ignores depending on the token. So when a location facet
  pins the set down, the whole located set is pulled with **no** `searchText`
  and the title is applied client-side, which is exact by construction. The
  board's own search is only used when the located set exceeds one pull or no
  location was given.
- **Facet names and values are per-tenant.** The country facet is
  `locationCountry` (Adobe, Autodesk, CrowdStrike, Motorola),
  `locationHierarchy1` (NVIDIA), a 90-character `CF_-_REC_-_LRV_-_…` custom
  field (Salesforce), or absent entirely (ServiceTitan). Values disagree too
  for the same city: `Canada, Toronto` (NVIDIA), `Canada - Toronto`
  (Salesforce), `Canada Ontario Remote` (ServiceTitan), `Toronto` (Adobe).

Nothing is therefore keyed by facet name. Location facets are found
structurally — the children of `locationMainGroup`, plus any top-level facet
whose human descriptor reads like a place — and values are matched by token
containment. When several facets match, the one with the **fewest** matching
values wins, so "Canada" applies the single country value rather than the
twelve city values that also contain the word.

### amazon.jobs (`src/fetchaller/amazon_jobs/`)

- `loc_query` **does not filter**. "Toronto" alone returns the whole global
  board (6,641 reqs). The only real location filter is `normalized_location[]`,
  which demands Amazon's exact spelling — `Toronto, Ontario, CAN` works;
  `Toronto` and `Toronto, ON, CAN` both return zero. So the client samples the
  vocabulary first (probing with the *place name* as the query, since Amazon's
  free-text search matches location words) and then applies the exact values.
  Repeated values are OR'd.
- `category[]` takes a slugified `job_category` (`design`,
  `software-development`). `job_category[]` and bare `category` are silently
  ignored. This is the reliable way to find roles whose titles vary — Amazon's
  only Design req in Canada is titled "Art Director", which no title search for
  "product designer" will ever surface.
- `facets` is always empty on this route, so category values are derived by
  slugifying the `job_category` strings the postings carry.
- **Pay bands.** Canadian and other disclosure-law reqs carry an inline band at
  the tail of `preferred_qualifications`:
  `CAN, ON, Toronto - 185,400.00 - 309,600.00 CAD annually`. These are lifted
  into a `Pay` field. Cents are dropped only when both ends have none, so an
  hourly `18.50 - 24.00` is not mangled into `18.50-24`.
- The posting page is server-rendered HTML with no JSON-LD, and the `.json`
  twin answers 406. A posting is therefore looked up through
  `search.json?base_query={requisition id}`, which matches exactly and returns
  the richer record anyway.

### Apple (`src/fetchaller/apple_jobs/`)

There is no usable JSON API: `/api/v1/refData/*` answers `401 User
Unauthorized`, and `/api/v1/search` answers `200` with zero results unless the
caller carries a page-issued token. The SSR page, however, embeds the complete
result set in `window.__staticRouterHydrationData` — a JS string literal handed
to `JSON.parse`, so it decodes twice (once as the JS literal, once as JSON),
and the scan for the closing `")` must skip escaped quotes.

Filters are all query-string: `?search=`, `?location={slug}-{CODE}`, `?page=`
(1-indexed, 20 per page). Location codes come from the postings themselves —
each carries `locations[].postLocationId` (`postLocation-TOR`) alongside the
display name, so `toronto-TOR` is reconstructed rather than tabled. The locale
sets the country scope: `en-ca` is Canadian postings, `en-us` American.

### Meta (`src/fetchaller/meta_careers/`)

`POST /graphql` with three things and no account: an `lsd` CSRF token
(published in the page as `["LSD",[],{"token":"…"}]`), a `doc_id`, and
`variables`.

`doc_id` values rotate with Meta's deploys, so they are **not** constants. Each
is published in a JS bundle as
`__d("{Operation}_candidate_portalRelayOperation", … a.exports="{id}")`, and a
known id that stops working triggers rediscovery from the bundles. Operations
used: `CareersJobSearchResultsV2DataQuery` (search),
`CareersJobSearchLocationFilterV3Query` (offices),
`CareersJobSearchFiltersV3Query` (other facets).

Two silent-failure traps: `search_input` must carry Meta's full key set, and
`offices[]` matches the **`location_display_name`** ("Vancouver, Canada"), not
the `id` ("vancouver") and not "Vancouver, BC" — an unrecognised office returns
the *unfiltered* board rather than an error. Responses are newline-delimited
JSON; the first line is the complete payload. Search returns the whole matching
set at once rather than paginating. Job detail is read from the
`xcp_requisition_job_description` object in the page's `data-sjs` blobs, which
avoids needing a second persisted-query id.

### Uber (`src/fetchaller/uber_jobs/`)

Uber runs its own ATS — the SmartRecruiters `uber` tenant is an unrelated
one-req stub. `POST /api/loadSearchJobsResults?localeCode=en` with
`{"params": {"location": [{"country": "CAN", "city": "Toronto"}], "query": "…"},
"page": 0, "limit": 100}`. An `x-csrf-token` header must be present but its
value is never validated. `params.query` must be a **string**; a list returns
zero. Counts arrive as a Long triple `{"low": N, "high": 0}`.

**Limitation:** there is no per-posting description. `/api/{name}` answers
`ERR_MISSING_HANDLER` for every detail-shaped handler name tried, the posting
page is client-rendered with the body absent from the HTML, and the board's own
`description` field comes back empty. A posting therefore resolves to metadata
plus a link. Uber also ships all-null location fields for some reqs, which are
labelled "Not specified" rather than dropped.
