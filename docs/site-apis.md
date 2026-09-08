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

## aartech.ca (category, manufacturer, search, product)

`src/fetchaller/aartech/` — a Canadian smart-home/security retailer whose pages ship **no prices at all** in their HTML. Two different mechanisms hide them, and both are handled from one page fetch.

- **Listings** (category `/network-cable`, manufacturer `/genesis`, search `/search?q=`) mount a React app on an empty `<div id="search">`. The SSR HTML has the category blurb and footer, nothing else. The app calls `{base_index_url}api/search/{searchId}[/{systemSearchFilterId}]` with a static `api-key: zs-search` header (a literal in the site's own http-client chunk — not a credential, not session-bound).
  - Both ids come from globals the SSR page *does* emit: `var searchId = '3'; var systemSearchFilterId = '198';`. `searchId` selects the configuration — **2** = keyword search, **3** = category, **4** = manufacturer — and the filter id scopes it to that category/brand.
  - The app round-trips its parameters through the address bar (`history.pushState`), so **the page URL's query string is already the API's query string** and is mostly passed through verbatim. Two keys are not:
    - `limit` — absent, the API returns the entire catalogue, so `DEFAULT_LIMIT`(24)/`MAX_LIMIT`(100) bound it. `resolve_window()` is the single source of truth for `(page, limit)`, shared by the request builder and the renderer so the "further products not shown" count is measured against the window actually fetched.
    - **the search term** — this one is a trap. `?q=` is *not* the term and the API does not error on it; it ignores every plain spelling (`q`, `query`, `keyword`, `searchText`, ...) and returns the whole 3,432-product catalogue, which renders as a completely plausible "Search Results" page for a term nobody searched. The real form comes from the site's own autocomplete: `search?controls[${searchControlId}]=${term}`, where `searchControlId` is another SSR global. `controls[1]=cat6` → 126 hits, `controls[1]=zzzznope` → 0. `_normalize_query()` rewrites a caller-supplied plain term into that shape and **raises** if no control id was found, because an unfiltered catalogue under a search heading is worse than no answer.
  - Item fields: `price.amountText`/`listPriceText` (sale when list > amount), `quantityBreaks` (volume tiers per UOM), `stock.display.message` ("In Stock" / "Out of Stock" / "Normally ships in 5-7 days" / "On order with vendor"), `partNumber`, `manufacturer.title`, `totalHits`.
- **Product pages** hide prices differently: the markup ships empty `<template>` elements (`product-pricing-template` and friends) that a script fills from one embedded blob, `new window.catalog.ProductPreview({...})`, extracted with the shared `extract_json_object` brace scanner. Price lives in `uoms[]` (`text`, `listPrice`, `packSize`) with `price`/`list_price` as fallback, plus `quantity_breaks`, `stock.display.message`, `custom_fields` (specs), `HTML_description`.
- **URL shape cannot tell them apart** — `/network-cable` is a category and `/intermatic-dt200lt` is a product, both a bare single segment; products also appear as `/gen-50881102/slug.html`. So `is_aartech_catalogue_url()` only excludes what is certainly neither (`/`, `page`, `cart`, `checkout`, ...) and `fetch_catalogue_page()` fetches once and dispatches on content: `searchId` → listing, else `ProductPreview` → product, else **None** so the URL falls through to the ordinary HTML path rather than erroring. Exclusions match **whole path segments**, never bare prefixes — `/cart` must not swallow a `/cartridge-heaters` category, whose only symptom would be a silently price-free HTML fallback. The trade-off of the permissive filter is that a page which is neither gets fetched twice (once here, once by the HTML path); that is accepted for correctness on a rare path.
- Transport failures are converted to `{"error": ...}` in `search_aartech()`. Letting a `wafer.WaferError` escape would crash the whole tool call instead of reporting the URL.
- `HTML_description` is full of `&nbsp;`; `_plain_text()` folds U+00A0 at the seam so the rendered text stays `\s`-matchable.

## ui.com (UniFi store, tech specs, installation guides)

`src/fetchaller/ubiquiti/` — three Ubiquiti properties, all Next.js, plus a new `get_unifi_manual` tool. The store is the dangerous one: its HTML carries the marketing copy **and the price**, so the generic path returns a product page that looks complete while every technical specification is missing. There is no visible symptom.

- **Dispatch is by Next.js route, not URL shape.** `__NEXT_DATA__["page"]` gives the route template, and the store serves the same product under `/us/en/category/...` *and* `/us/en/pro/category/...` while rewriting both onto one route. Handled routes: `…/category/[category]/products/[product]`, `…/category/[category]/collections/[collection]/products/[product]`, `…/category/[category]/collections/[collection]`, `…/category/[category]`, `/[productLine]/[category]/[product]`, `/[productLine]/[category]`.
  - The two **collection** routes matter: door access and cameras file their products under a collection, and a bare collection URL resolves to that collection's current product (`currentProductId` says which). Both carry a product payload and are read as product pages — without them a door-access product silently loses all five of its spec sections while still rendering a plausible page.
  - An **unknown category is a soft 404**: the store answers HTTP 200 and rewrites onto its home route (`/[store]/[language]`), whose payload is the storefront's own (`recommendedProducts`, `whatsNewCards`) with nothing about the category asked for. A URL that names a category but lands on that route is reported as "no such category" rather than falling through, which would render the home page under a heading the caller supplied.
  - Anything else returns **None** so ui.com's blog, help centre and downloads portal fall through to the ordinary HTML path. Hosts: `store.ui.com` and `<cc>.store.ui.com` (ca/eu/uk/…, each its own currency), `techspecs.ui.com`, and `ui.com`/`dl.ui.com` for `/qig/<slug>`.
- **Specs** live in `technicalSpecification.sections[]`, and each section's `features[]` is a **flat** list mixing three `__typename`s: `…FeatureGroup` (a heading such as "MIMO"), `…FeatureEntryText` (`value` + optional `note`), and `…FeatureEntryFlag` (`"True"`/`"Empty"`). Group children are *not* nested — they are later entries whose `feature.parentId` points back at the group, so `render.spec_sections()` rebuilds the nesting from that id.
  - Absent flags render as `—` rather than being dropped: "this model has no 6 GHz radio" and "nobody said" are different answers, and silence reads as the second.
  - Multi-line values are meaningful — `Operating Frequency` carries a whole regional band plan in one string — so newlines become indented continuation lines instead of one run-on.
  - Sections with `section.type == "Overview"` are the compare-grid badges. On a product page they duplicate the visible Overview section and are skipped; on a **category** page they are the only specs present and are exactly the card badges ("WiFi 7", "6 GHz Radio"), so there they are rendered by `spec_badges()`.
- **Price**: amounts are integer minor units, and **the divisor is the currency's ISO 4217 exponent, not a flat 1/100**. `jp.store.ui.com` prices the U7 Pro Wall at `{"amount": 36391, "currency": "JPY"}` and displays ¥36,391; dividing by 100 there reports ¥363.91 — a silent 100× error on every product in that store, with no symptom anywhere. `_CURRENCY_EXPONENTS` lists only the exceptions (0 for JPY/KRW/VND/…, 3 for KWD/BHD/…).
  - `minDisplayPriceWithSurcharges` is what the store charges and displays ("$290.00, Surcharge incl."); `minDisplayPrice` is the base. The charged figure always leads and the base is named only when they differ, with the surcharge type (`productSurchargeTypes`, e.g. `Memory`) spelled out — rendering the base as *the* price would undercount most of the catalogue.
  - Both `minDisplay*` fields are the **minimum across variants**. On a product sold in several configurations (USW Flex Mini: 39/119/189) a bare figure reads as *the* price, so it is rendered `from 39.00 CAD` whenever the variants disagree.
  - Availability comes from `variants[].status` plus `soldOutAt`/`restockEtaAt`.
- **Category pages** carry the *whole* sibling set — every sub-category of the parent, because the filter chips switch between them without navigating. `render_category()` expands the group `queryParams.category` names and lists the siblings by name and count rather than dumping all of them; an unrecognised category (e.g. `all-wifi`) lists everything.
- **Installation guides** (`ui.com/qig/<slug>` → `dl.ui.com/qig/<slug>/`) are SPAs whose HTML is a 7KB shell — read it and the only visible text is the copyright line. Pages are separate JS assets registering a serialized DOM tree: `[tag, attrs, children]` with text nodes `[0, "text"]`. It is **not JSON** (bare identifier keys: `viewBox:` but `"xmlns:xlink":`), so `guide._LiteralParser` parses it properly rather than eval'ing or regexing.
  - **The guides have no text layer.** Every word is outlined vector art — not one `<text>`/`<tspan>` across the guides sampled — so nothing can be extracted by trying harder, and `render_guide()` says so plainly instead of returning page furniture that implies the guide was read. It returns the page inventory and the real outbound links (support, help centre, app stores) instead. The parse still runs whenever a page *does* declare a text element.
  - **A 200 proves nothing here**: `dl.ui.com` answers with a ~440KB app shell (redirecting to techspecs.ui.com) for any slug with no guide. A guide is recognised by its bootstrap markers, and their absence is reported as "no guide published at <the URL you asked for>" — deliberately not the redirect target.
  - Two runtime generations: the current one declares `QIG_CONTEXT.PAGE_TUPLES`; older guides (`u6-pro`) predate it, so the page list is recovered from the show/hide CSS (`[data-current-page=A]`) paired with the build hash on `runtime-<hash>.js`. The legacy payload also registers the `<svg>` at top level instead of inside a `<div data-page>`.
  - Some guides ship links with Illustrator's export id glued on (`…watch?v=Oja-PE0fFew_00000111178…1150_`, a different suffix per page for the same video). The suffix is stripped — it does not resolve with it attached.
- **`get_unifi_manual(url, format)`** — Ubiquiti publishes **no PDF** for current products (every `dl.ui.com/**/*.pdf` path returns the app shell), so the web guide *is* the manual. Each page is reconstructed into a standalone SVG and PyMuPDF — already a dependency for PDF reading, **no new dependency** — turns those into one combined vector PDF, one PNG per page, or the raw SVGs. Two MuPDF limitations have to be worked around in `page_svg()`, and both otherwise produce a page of black silhouettes rather than an obvious error:
  - **No CSS selector support.** A document styled entirely through `.stNN` classes renders as solid black, so each class's declarations are folded into the element's own `style` (class first, inline style last so it still wins).
  - **No gradient support at all** — `fill:url(#gradient)` paints identically to an unresolvable reference. Gradients are flattened to the average of their stop colours (following Illustrator's `xlink:href` re-pointing, which leaves the referring gradient stopless), which turned the U6-Pro's hero illustration from a black blob into the white AP it is. Separately, these guides reference paint ids that exist **nowhere in the document** — 164 on one `u6-pro` page — and SVG 1.1 mandates `none` for an invalid paint reference with no fallback, so those are painted `none` rather than black. A reference that *is* defined but unsupported (a `<pattern>`) is left alone rather than silently erased, and `clip-path`/`mask` references are never rewritten since they are structural, not paint. Files are written under `DATA_DIR/manuals/<slug>/`, never a caller-supplied path. Guide pages are one tall scroll (320 × 4684 units), so PNG scaling is bounded by **total pixels**, not DPI.

## RedFlagDeals (forums.redflagdeals.com)

`src/fetchaller/content/redflagdeals.py` (cleanup) + the phpBB branch of `src/fetchaller/content/forums.py` (URL shapes). Since 2026-09 every page on `redflagdeals.com` is fronted by a home-grown SHA-256 proof-of-work served as **HTTP 202** — no vendor, `Server: Varnish`, a script that hashes `nonce + issued_at + counter` until the digest carries `difficulty` leading `difficulty_char`s and sets a `pow_bypass` cookie on `.redflagdeals.com`. wafer >= 0.6.0 detects it as `ChallengeType.POW` and solves it inline (no browser). The feed routes (`/feed/forum/{id}`, `/feed/topic/{id}`) and `robots.txt` are exempt, which is why the listing path never broke while every thread came back as an empty document. Full measurements: `docs/wafer-request-rfd-pow-gate.md`.

Three URL shapes matter, because the wrong one fails by rendering something plausible:

- **Listing** — `/{slug}-f{id}/` or stock `/viewforum.php?f={id}` → read as the Atom feed `/feed/forum/{id}` (15 newest threads with excerpts and `viewtopic.php?t=` links).
- **Thread** — `/{slug}-{id}/`, `/{slug}-t{id}.html`, **or stock `/viewtopic.php?t={id}`**. The stock form is what the feeds and post permalinks carry. Before 3.6.2 it was not recognised, so the page was treated as a listing and feed autodiscovery swapped it for the board's *site-wide* feed: right status, real content, wrong page. A thread renders as HTML: title heading, author profile links (`memberlist.php?mode=viewprofile`), `Posted: Sep 7th, 2026 8:21 pm` stamps.
- **Everything else** (`search.php`, `memberlist.php`, `ucp.php`) — rendered as its own page with autodiscovery off (`ForumTransformResult.autodiscover=False`), because each advertises the site-wide feed and following it would answer a keyword search with "latest posts anywhere". `search.php?keywords=` is a real server-side search ("Search found N matches") and renders fine.

The container smoke test fetches the Hot Deals listing, takes a topic id from it, and fetches that thread; the thread gate is the one that proves the proof-of-work was solved and the page was not swapped for a feed.

## realtor.ca (home search + listings)

`src/fetchaller/realtor/` — Canadian real-estate search via the `api2.realtor.ca` XHR API plus SSR listing-detail pages. The public map page is a CSR shell, so all search data comes from the API. **Since 2026-09 the `realtor.ca` zone is behind Cloudflare** (it was Imperva), and the two hosts answer a cold client differently: `www.realtor.ca` serves a managed challenge, which wafer's `browser_solver` clears; `api2.realtor.ca` serves a Cloudflare WAF **block** page — 403, "Sorry, you have been blocked", classified by wafer as `generic_js` — that nothing solves in place, because there is nothing to solve. A browser never sees it: it only reaches api2 after loading the site, carrying the `Domain=.realtor.ca` clearance the front page minted. So the client reproduces that order — `api._api()` loads the front page on the shared session before the first api2 call and, on a block mid-session (the clearance is time-bound), re-clears once and replays. Measured 2026-09-07: api2 cold → 403 block on wafer 0.4.9 and 0.5.0 alike; front page then api2 on one jar → 200. This was found by the container smoke gate, which had passed on 2026-08-09. wafer owns the solve; fetchaller owns the request order. Without a `browser_solver` the front page's own `cloudflare` challenge is the reported error, which is the accurate one. The shared session sets `rate_limit=1.5` (per host) and passes the shared `browser_solver`.

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

**`limit` sizes the output, never the work.** Each client has an
`_EXAMINE_CEILING` — the number of postings one search pulls before the
filters run — and it is a constant, independent of `limit`. Deriving it from
`limit` (as `min(limit * 4, 100)`) made the answer depend on how many rows the
caller asked to see: Apple's "designer in Cupertino" examined 4 postings at
`limit=1` and returned **none**, then examined 100 at `limit=25` and returned
13. Asking for fewer results returned fewer *matches*, and raising `limit`
surfaced different jobs rather than more of the same ranked list. Two
disclosures follow from the ceiling and both are mandatory: matches that did
not fit `limit` are counted in the summary line, and a board reporting more
than the window examined gets an explicit "the remaining N were not examined".
Apple ranks 1510 for that query against a 100-posting window — a bare "13 jobs
shown" reads as the answer when it is 6% of one page of it.

**A board's count describes the query it answered, not the pool examined.**
Every client merges a narrow title query with a broadened retry, and no single
board count describes the union. Microsoft's `designer` returned 7 and `design`
returned 31 sharing 6 of them, so the pool was 32 while `max()` reported 31 —
rendering as "31 matches / dropped 32" and reading as an off-by-one. GAF is the
same defect inverted: its first query matched nothing, the retries found 41,
and `total` stayed 0, which suppressed the summary line entirely. What was
examined is always known exactly, so `counts_line` takes `max(board_total,
examined)` as the reported figure and says "All N postings the board ranked …
were re-checked" whenever the examined pool covers the board's own count.
Workday additionally widens `total` across its retry queries, which is where
GAF's 0 came from.

**A country is one constraint however a board spells it.** `location_matches`
expands a country name to every alias in `COUNTRY_ALPHA3`, so `"United
States"` matches Google's `"New York, NY, USA"`. Without it the behaviour was
asymmetric — `"Canada"` matched `"Waterloo, ON, Canada"` while `"United
States"` dropped all 60 US postings.

**A subdivision implies its country, and the miss it fixes was biased.** A
posting pinned to a bare province was invisible to a country query, and not at
random: offices carry the country (`AMER - Canada - Ontario - Toronto`) while
remote and offsite postings frequently do not. Measured across Workday
tenants — Autodesk 11 province-only values, Motorola 9, Salesforce 3 — *every*
one of Salesforce's ends `- Remote` and *every* one of Motorola's ends `Remote
Work`. The values a country query silently dropped were disproportionately the
remote ones. `_implied_countries` covers Canadian provinces and US states by
full name and by the `City, ST` abbreviation, and `tokens()` now folds
diacritics so `Québec` and `Quebec` are one place. Validated against the live
facet vocabularies of four tenants: 76/76 Canadian values match.

The abbreviations are the delicate part. `CA` is California *and* Canada's
alpha-2, so each code resolves to the set of countries it could denote and the
sets are intersected: `Vancouver, BC, CA` gives `{CAN} ∩ {USA,CAN} = {CAN}`,
`San Jose, CA, US` gives `{USA}`. Anything other than a single survivor falls
back to the subdivision reading — without that, `Los Angeles, CA` and `San
Diego, CA` turned up in a Canada search, which unit tests passed and only an
end-to-end run caught. Matching requires uppercase after a comma, so Motorola's
`Vancouver on site (BRC06)` does not read `on` as Ontario, and the `ON`/`OR`/
`IN` stopword collision never arises.

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

**`workLocationOption` is not data on the classic generation.** Every Netflix
posting carries the constant `"onsite"`, including reqs whose own `location`
reads `"Canada - Remote"` and `"USA - Remote"`; `locationFlexibility` is
`null` throughout. Work mode is a hard screen, so a field contradicting the
location is worse than an absent one — the location is per-posting and the
board populates it. `render._work_type` prefers the location wherever the two
disagree and says why in the rendered value. PCS-X tenants populate the field
properly (`remote_local` and similar) and are passed through untouched.

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
- **The list response hides multi-location postings.** `locationsText` is a
  display summary, not data: Autodesk sends `"11 Locations"` and Motorola
  `"Maryland, US Offsite, More..."`. Geo eligibility is the screen that decides
  whether a posting is worth opening, so a summary makes the listing useless
  for the one question it most needs to answer. The real list is
  `additionalLocations` on the **detail** endpoint — nothing in the list
  response carries it. Summarised postings are therefore expanded with a
  bounded concurrent detail fetch (`_EXPAND_CAP`, `_EXPAND_CONCURRENCY`), once
  before the client-side location filter so a posting is never dropped on a
  summary it could have matched, and once over the postings actually shown.
  Places matching the requested location are listed first: Motorola's
  `R66106` ("US REMOTE" in its title) is genuinely open to four Canadian
  provinces, and in board order they sit past the display cap. A failed detail
  fetch keeps the summary — it may degrade the display, never the result.

  Province-only values are recognised — see the subdivision note below.
- **`bulletFields` is not an id field.** It is a tenant-configured list of
  list-view columns. Motorola puts the location code first and the requisition
  second, so joining them rendered `Req ID: British Columbia Remote Work,
  R65471`. The requisition is the entry matching the final `_`-segment of
  `externalPath` (`..._R65471`, `..._26WD97217-2`, where a trailing `-1`/`-2`
  marks a repost). Matching anywhere in the path is too loose — a one-word
  location like `Remote` appears in `/job/Remote/Engineer_R123` as readily as
  the id does.
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

`POST /api/v1/search` **is** anonymous — no CSRF token, cookie, Referer, or
Origin. What makes it look gated is that it answers `200` with
`totalRecords: 0` when the request body omits **`format`**, which reads as "no
jobs" rather than "malformed request". `format` only carries date-presentation
strings, but it is part of the request contract; `format: {}` is enough. (The
`/api/v1/refData/*` reference routes really do answer `401`, which is what
sent the first investigation down the wrong path.) A test pins `format` into
every request body so it cannot be tidied away.

**The locale is cosmetic.** `en-us` and `en-ca` return byte-identical results
— same 4878 total, same requisition ids in the same order — differing only in
the `/en-us/` or `/en-ca/` segment of each output link. It selects a
storefront for URLs, not a country scope. The tool description used to claim
otherwise ("en-ca shows Canadian postings, en-us American ones"), which would
have a caller believe they had scoped a search they had not. Country scope
comes from `location` alone.

The API is primary. The SSR page remains the fallback and embeds the same
result set in `window.__staticRouterHydrationData` — a JS string literal handed
to `JSON.parse`, so it decodes twice (once as the JS literal, once as JSON),
and the scan for the closing `")` must skip escaped quotes. An empty API
result is cross-checked against the page once, because that is the single case
where the silent-empty failure mode is indistinguishable from a genuinely
empty search.

The two surfaces disagree on how a location is named: the URL wants
`toronto-TOR` and the API wants `postLocation-TOR`, so the forms are
converted rather than discovered twice.

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

Job detail no longer depends on that internal object alone: posting pages at
`/profile/job_details/{id}/` carry a schema.org `JobPosting` JSON-LD block,
which is SEO-facing and therefore far less build-coupled. The client merges
both from one request — JSON-LD for the standard fields, the internal
`xcp_requisition_job_description` object for teams, sub-teams, and
compensation, which JSON-LD omits.

Search has no doc_id-free surface. Raw (non-persisted) GraphQL is disabled —
posting a `query` document without a `doc_id` returns HTTP 500. The
robots-advertised `/jobsearch/sitemap.xml` IS a complete, token-free inventory
(its 789 ids matched the persisted query's inventory exactly), but it carries
only URLs and a shared `lastmod`, so filtering by title or location through it
would cost one page fetch per posting. That is fine as a correctness check and
unusable for interactive search, so search keeps the persisted query with
bundle rediscovery.

**Rate limiting is a correctness concern here, not just politeness.** Meta
throttles per path and answers a throttled search with `HTTP 200` carrying
`{"errors":[{"message":"Rate limit exceeded","code":1675004}]}` — which is
indistinguishable from a rotated `doc_id` unless the error is read. It used to
be read as one, and rediscovery fetches the board page *and walks every JS
bundle*, so being throttled triggered a bundle scan and made the throttle
worse. Three rules now hold that off:

- `_rate_limited()` reads the error and backs off instead of rediscovering.
- `_check()` `defer()`s the shared limiter on a 429, honouring `Retry-After`.
- `meta_careers_limiter` is 3.5s, not 2s: one search costs three requests (root
  warm-up, board page for the `lsd`, then GraphQL).

`_warm_origin()` fetches `/` once per session before `/jobs`. Measured
directly: `/` answered 200 while `/jobs?q=…` raised `RateLimited` on the same
session. A cold session landing straight on a deep path is the pattern Meta
throttles. See `docs/spa-discovery.md`.

Two silent-failure traps: `search_input` must carry Meta's full key set, and
`offices[]` matches the **`location_display_name`** ("Vancouver, Canada"), not
the `id` ("vancouver") and not "Vancouver, BC" — an unrecognised office returns
the *unfiltered* board rather than an error. Responses are newline-delimited
JSON; the first line is the complete payload. Search returns the whole matching
set at once rather than paginating. Job detail is read from the
`xcp_requisition_job_description` object in the page's `data-sjs` blobs, which
avoids needing a second persisted-query id.

### Uber (`src/fetchaller/uber_jobs/`)

Uber migrated off its own ATS to **Oracle Recruiting Cloud**, so `uber_jobs/`
is a thin adapter over `oracle_recruiting/` rather than a client in its own
right. The SmartRecruiters `uber` tenant is an unrelated one-req stub. The
legacy `POST /api/loadSearchJobsResults` endpoint is gone from this repo: it
returned empty `description` fields and unreliable locations, while ORC answers
anonymously with the full posting text.

`uber.com/{region}/{lang}/careers/list/` redirects to
`jobs.uber.com/{lang}/jobs/`; both forms route here.

**The board itself is server-rendered and has no client-side data endpoint** —
established three ways rather than assumed:

- Driving it in a browser: pagination is plain
  `<a href="/en/jobs?query=…&page=2&pagesize=10">` links, no XHR. `pagesize` is
  capped at 10 server-side, so it is not tunable.
- Its React Flight (`text/x-component`) prefetches decode cleanly but carry
  navigation and i18n labels — the largest record set is a 16-entry menu,
  byte-identical between the detail and list pages.
- The real search runs server-side against Oracle Fusion.

Discovery therefore reports "this board server-renders its results" rather than
returning a plan; see `docs/spa-discovery.md`. Note the board's RSC prefetches
are challenged by Cloudflare when requested by an *unhardened* browser, which
is a property of the client, not of Uber — plain wafer gets 200.

### Oracle Recruiting Cloud (`src/fetchaller/oracle_recruiting/`)

ORC is Oracle Fusion's candidate-experience recruiting module. Uber migrated
onto it (``uber.com/…/careers/list/{id}`` now redirects to ``jobs.uber.com``),
and Oracle itself runs on it, so one client serves both. Two REST resources,
both unauthenticated:

- ``GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions``
  ``?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={site},limit=N``
- ``GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails``
  ``?expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={site}``

Three traps, all of which fail silently rather than erroring:

- **``expand=requisitionList`` is mandatory.** Without it the response is still
  HTTP 200 with a correct ``TotalJobsCount``, but the postings array is absent
  entirely.
- **``location`` filters on countries and ignores cities.**
  ``location="Canada"`` narrows Uber's board from 640 to 13;
  ``location="Toronto"`` returns all 640 while looking like a filter. Only the
  country is sent; the city is matched against each posting afterwards, and
  the fetch window widens when no country can be derived.
- **``siteNumber`` is not constant.** Nearly every deployment uses ``CX_1``,
  but Oracle's own site is ``CX_45001``, and the value appears nowhere in page
  markup — so it is per-employer configuration with ``CX_1`` as the default.

The Fusion hostname (``iaziqy.fa.ocs`` for Uber, ``eeho.fa.us2`` for Oracle) is
deployment-controlled, so it is discovered from the employer's own careers page
with the last-known host kept only as a fallback.

The search response carries ``ShortDescriptionStr`` on every row and the detail
resource carries ``ExternalDescriptionStr`` / ``ExternalResponsibilitiesStr`` /
``ExternalQualificationsStr``. Uber's older in-house endpoint
(``POST /api/loadSearchJobsResults``) still answers but returns an empty string
for every ``description`` and all-null locations for many postings, so it was
dropped rather than kept as a fallback.


### Employers with no honest alias

Some employers cannot be represented as an alias without implying a filter the
board does not have.

**Clearpath Robotics / OTTO Motors** are part of Rockwell Automation, and both
brands' careers pages link to
`rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation`. That
board's only facets are `jobFamilyGroup`, `timeType`, and location — there is
no company, brand, subsidiary, or business-unit facet, and no separate
Clearpath/OTTO site slug exists on any Workday cloud. `searchText` is not a
brand filter either: "OTTO" returns three reqs, one of which is an unrelated
Machine Operator. So a `clearpath` alias would return Rockwell-wide results
under a name promising Clearpath ones, and is deliberately absent.

Reach those roles by location instead: Cambridge, Kitchener, and Waterloo hold
10 of the board's 15 Canadian reqs, and a Waterloo search surfaces the robotics
postings directly.

**Buildertrend** (6 reqs) and **GAF** (95 reqs) have no Canadian jobs at all —
verified by paging every posting and dumping both boards' full location facets,
not merely by the absence of a "Canada" descriptor. Their boards are US-only,
so a Canada search there correctly reports the location filter was not applied.

### Google (`src/fetchaller/google_jobs/`)

`google.com/about/careers/applications` is server-rendered, so a plain fetch
returns readable text. It is nonetheless worth a client, for one reason:
**Google's own count cannot be used as the answer.** Its free-text matching is
extremely loose — on a "product designer" search in Canada, Google reports 38
matches, of which two have a title containing both words and *none* contain the
literal word "designer". A nonsense query returns 0, so the query does filter;
it just filters very generously.

Underneath the page is Google's internal BOQ RPC:

```
POST /about/careers/applications/_/HiringCportalFrontendUi/data/batchexecute
Content-Type: application/x-www-form-urlencoded;charset=UTF-8
f.req=[[["<rpc>","<json-encoded args>",null,"generic"]]]
```

`r06xKb` searches, `sf9Qmf` returns one posting. Neither needs a cookie, token,
or referer. The response is XSSI-guarded (`)]}'`) and doubly encoded: the
payload is a JSON *string* at `outer[0][2]` of a `[["wrb.fr", ...]]` envelope.

Everything is positional, so `api.py` pins every slot by index:

- Search args (one array, wrapped in one more array): 0 query, 1 company,
  2 degree, 3 employment type, 4 locale, 6 locations, 7 page (1-based),
  8 skills, 9 remote flag, 10 sort, 16 target level.
- Job record (21 elements): 0 id, 1 title, 2 apply URL, 3 responsibilities,
  4 qualifications, 7 company, 9 locations, 10 description, 12/13 timestamps,
  19 minimum qualifications.
- A location entry is `[display, [display], city, null, region, country_code]`.

Traps worth knowing:

- **Multi-value filters must repeat**, as arrays. Comma-joining them
  (`location=Canada, United States`) makes Google treat the whole string as one
  fuzzy location and return unrelated radius results.
- **Page size is fixed at 20**; `page_size`, `size`, and `limit` are ignored.
  Past the last page the RPC returns a *null* job list rather than an empty
  one — the same shape as a malformed request.
- **City filters are radius-based.** A Toronto search returned 61 results of
  which only 25 actually list Toronto, so the location is re-checked locally.
  A bare city name is also geocoded loosely: "Waterloo" resolves to Waterloo,
  Belgium. Pass a fully qualified city.
- **Slots 12–14 are protobuf-style `[seconds, nanos]` pairs.** Slot 12 is
  always ≤ 13/14, and all three are equal on postings that were never revised,
  so 12 is rendered as posted and 13 as updated. That is inferred from
  ordering, not documented, and is never presented as a deadline or a
  freshness guarantee.
- The posting page carries **no** JSON-LD; `sf9Qmf` is the structured detail
  surface. The public permalink needs no slug — the id alone resolves.

## gojobs.gov.on.ca — Ontario Public Service (`src/fetchaller/gojobs/`)

An ASP.NET WebForms board, which is the whole story. Three consequences, each
of which fails by returning something plausible rather than an error.

**The listing does not exist in any GET.** `Search.aspx` answers a GET with the
search *form* — 65KB of HTML containing zero job links. Results come only from
POSTing that form back with `__VIEWSTATE`, `__VIEWSTATEGENERATOR` and
`__EVENTVALIDATION` echoed from the page that issued them. Fetching the URL and
reading the HTML shows an empty board, not a failure, so the generic fetch path
renders 1.8KB of form chrome and looks like it worked.

**Paging is a postback, not a URL.** Page N is `__EVENTTARGET` set to
`ctl00$MainContent$lnkButton_Page{N}`, signed by the *previous* response's
`__VIEWSTATE`. Pages therefore cannot be fetched in parallel or out of order,
and ten rows per page is fixed — there is no page-size control. A full listing
is a dozen sequential POSTs, which is why the tool requires at least one filter
and why the limiter is spaced at 2s.

**Facet values are JSON arrays, not scalars.** `ucRegion$hiddenSelected` set to
`REGION-TRNT` is accepted and silently ignored; it has to be `["REGION-TRNT"]`.
Measured: the bare string returned all 127 postings, the JSON array returned 35.
The vocabularies (5 regions, 29 categories, 5 career levels, 984 cities) ship
inline in the page's own bootstrap JS as `var options_ucRegion = [...]`, so no
extra request is needed to learn what a facet accepts.

**There is no keyword or title search.** The only text input is an exact Job ID
lookup. So `title` is matched entirely client-side via `jobfilter`, over a
window of the board rather than by narrowing it server-side — the board-filter
-as-optimisation doctrine taken to its limit, because here the optimisation does
not exist. The board's own location filter *is* real and is still not trusted:
a Toronto search returned 35, of which one did not list Toronto.

**"Ontario" and "Canada" are the whole board, not a filter.** Every posting is
an Ontario government job and no row names the province — rows read
"Mississauga, Central Region" — so filtering on it matched nothing and emptied
the result set for the most natural query a caller could type. Those names are
recognised and reported as a no-op instead.

Parsing notes, each a real defect the live board produced:

- **Rows are bounded by the English title anchor** (`_lnkJobTitleEN`), not by
  the repeater id. A bilingual posting carries a second `_lnkJobTitleFR` anchor
  whose id contains the same repeater marker, which cut that row in half and
  left every one of its fields empty while its neighbours parsed perfectly.
- **The last row needs an explicit end.** With no following anchor it ran to the
  end of the document and absorbed the pagination strip, the footer and ~130KB
  of inline script into its closing date. The paging control is the marker that
  matters — it sits between the last row and the footer.
- **Row numbers precede the next title**, so the last field of every row picked
  up the following row's counter ("…11:59 pm EDT 17.").
- **The facts block ends at the first `<hr>` after the Job ID.** Splitting there
  stops Salary — the last labelled fact, with nothing after it — from running on
  into 600 characters of job description, and locates the body without guessing
  which tag the copy starts with. Measured: one posting opens on `<b>`, another
  on a bare text node.
- **`Competition Status` is reported separately and first.** A filled
  competition still reports `Posting status: Open`; posting 232882 reads "Open"
  while its competition block says "Position Filled" and 2064 people applied.
- `Preview.aspx` answers **200 and renders its shell** for an unknown Job ID, so
  "not found" is a parse outcome, never a status code.

**A search is a conversation, and two cannot share a session at once.** The
server keys the GET→POST→page-POST exchange to `PHPSESSID`, and the session is
cached process-wide, so overlapping searches interleave and the server answers
each with the other's state. Measured, Toronto and Thunder Bay under
`asyncio.gather` returned **35** and **127**; the same two run sequentially
return **32** and **17**. Both wrong answers were well-formed, and nothing in
either response said a filter had been dropped — the Thunder Bay result simply
reported the whole board and let the client-side filter do all the work.
Sequentially the shared session is stable (verified: repeated and interleaved
city/region searches on one session all match their isolated baselines), so the
fix is an `_exchange_lock` around the exchange, not a session per call.

**`Search.aspx` serves two different pages, and the difference is the city
list.** The first GET on a session returns ~499KB carrying
`var options_ucCity` with all 984 cities; every GET after that returns ~65KB
with no city declaration at all. Regions, categories and career levels are
present on both, so the light page looks complete and only the largest
vocabulary is missing.

That was the real cause of the wrong counts above. Re-fetching the form on each
search got the light page, no place name could resolve to a city code, and
`_resolve_location` fell through to its region match: "Toronto" quietly became
REGION-TRNT and the board reported **35** where the city filter gives **32**,
while "Thunder Bay" matched no region and the board returned the unfiltered
**127**. The postings shown stayed correct — the client-side filter is the
guarantee — but the board's own figure was reported for a scope nobody asked
for. So the vocabulary is read once from the cold page, and an empty city list
is deliberately **not** cached.

Radware Bot Manager fronts the domain; wafer >= 0.4.9 detects and clears it
inline. The session is cached at module level, which is also required for
correctness here — `PHPSESSID` has to survive from the form GET to the POST.

`robots.txt` disallows `/alljobs.aspx` and `/ReadPDF.aspx`. Neither is used.

## jobbank.gc.ca — federal Job Bank (`src/fetchaller/jobbank/`)

The highest-volume Canadian board (64,017 postings nationally at time of
writing) with salary published on most listings. Two of its parameters are
**accepted, ignored, and never complained about**, and both fail by rendering a
normal successful search.

**A location only filters when it carries the city's numeric id.** The obvious
`?locationstring=St.+Catharines%2C+ON` returns the entire national result set.
Measured: that URL, `locationstring=Toronto%2C+ON`, `locationstring=nonsensexyz`
and omitting the parameter altogether all returned **64,017**. The filter is
`locationparam` (the site's own pagination spells the same value `mid`),
carrying the numeric `city_id` from the Solr autocomplete the search box uses:

```
GET /core/ta-cityprovsuggest_en/select?q=St.+Catharines&fq=NOT postalcode_cnt:0&wt=json&rows=25
    -> {"name":"St. Catharines","city_id":"22415","province_cd":"ON"}
GET /jobsearch/jobsearch?locationparam=22415&d=25   -> 424
```

So an unresolvable place name sends **no** location filter at all and says so.
Falling back to the bare string would look like a filter and be none.

**`searchstring` is dropped for some terms.** Within 25 km of St. Catharines,
"driver" gives 11, "nurse" 6, "welder" 3, "clerk" 40 — but **"assistant"
returned 424, byte-identical to the unfiltered page down to the first five
titles** ("material handler", "groom - horse race track", …). Same failure
shape as the location: no error, no marker, just the whole slice under a query
heading. A search for "assistant" reported 1 match where the unfiltered pool
holds 15 assistant-titled jobs.

The client-side title filter is the guarantee either way, so the *postings* were
never wrong — but the board's count would be reported for a query it never ran.
When a title is supplied, one extra request fetches the same location's
keyword-less total; if the two match, the board's figure is suppressed and the
caller is told the keyword was ignored.

**Radius is real and is part of the answer.** `d` defaults to 50 km, and the
board searches *near* a city by design, so a St. Catharines search legitimately
returns Thorold and Hamilton postings. Measured from St. Catharines: 10 km →
152, 25 km → 424, 50 km → 1,036, 100 km → 10,327. The radius used is always
stated, because otherwise nearby towns read as a broken location filter.
`strict_location` defaults to False for the same reason.

Parsing notes:

- **The result total is `<span class="found" id="results-count">`, nothing
  else.** Every distance facet renders
  `<span class="badge">152 <span class="wb-inv">jobs found in</span></span> 10km`,
  so a "N jobs found" text match silently returns whichever facet came first —
  152 for the 10 km option on a search whose real total was 1,036. That misread
  produced three wrong measurements before it was caught.
- **`wb-inv` spans are screen-reader labels sharing markup with their values**
  ("Location", "Salary", "Job number:") and must be stripped before the text is
  flattened, or every field arrives with its own label glued on.
- **Posting hrefs carry `;jsessionid=` and `?source=`.** Both are session
  scratch; a shared link is rebuilt from the numeric id.
- The board is slow: a filtered search page is ~280KB and routinely takes
  30–60s, with real 180s timeouts during development. The tool's default
  timeout is 300s.


## ca.indeed.com (`src/fetchaller/indeed/`)

Two structured surfaces, both in the served HTML — no browser, no API key.
Search embeds its results as JSON in
`window.mosaic.providerData["mosaic-provider-jobcards"]`, with the salary band
as numbers (`extractedSalary: {min, max, type}`). A posting carries
`schema.org/JobPosting` JSON-LD: full description, the band again, and
`validThrough` — an expiry no other board indexed here publishes, and the direct
answer to indexes that list filled requisitions as open.

Parse the JSON-LD, not the markup: it is a published schema, and a posting page
renders to 0.7% readable text because the visible HTML is nearly all chrome.

**A search costs one request, after the two-request version stopped working.**
A probe run found the default page was 100% sponsored while `start=1` returned a
disjoint all-organic 15 — thirty postings for two requests. On re-test hours
later `start=1` and `start=2` both returned the login wall, 131,902 bytes and
zero cards, repeatably. The offset is not a stable surface, so the client fetches
the page that reliably answers and says it stopped there. Anyone re-testing an
offset should verify it in the same session as the default page; the difference
between those two runs was not the URL.

| probe | new postings | verdict |
|---|---|---|
| default (no `start`) | 15 | take |
| `start=1` | 0 on re-test (was +15) | login wall now — do not rely on it |
| `start=2` | 0 on re-test (was +1) | login wall now |
| `start` >= 3 | 0 | login wall, consistently |
| `page` / `p` / `offset` / `pagenum` | 0 | silently ignored, byte-identical to default |
| `sort=date` / `sort=relevance` / `explvl` / `/m/jobs` | 0 | identical all-sponsored page |
| `fromage=1/3/7`, `jt=parttime`, `radius=0` | +1 to +6 each | work only by thinning the ad mix |
| `filter=0`, `limit=50` | — | fingerprinted; deterministic Cloudflare 403 |

Facet partitioning was measured at ~60 of a claimed 91 in ~15 requests, but that
run depended on the same offsets that later walled, so treat the figure as
unverified. It is not automatic regardless: narrowing the query is the cheaper
lever than fifteen requests.

`&limit=` and `filter=0` are scraping tripwires, not features — on a *fresh*
session `&limit=50` was challenged while the byte-identical URL without it
returned 200 eight seconds later on the same session. It is the parameter, not
reputation or rate.

Indeed's robots.txt disallows `/viewjob?`, `/*&start=` and `/*radius=`; per the
project position on robots (see CLAUDE.md) this is a user-directed fetcher and
those paths are used. Note also that Indeed publishes an *inverted* group for
AI crawlers (`Allow: /viewjob?`, `Disallow: /jobs`) — do not switch user-agent
to select a different rule set; the fingerprint choice belongs to wafer.
