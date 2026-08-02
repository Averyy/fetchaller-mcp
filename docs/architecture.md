# Architecture

## fetchaller vs wafer

**fetchaller-mcp** and **wafer** (`~/code/wafer`) have a strict separation of concerns:

- **wafer** owns ALL HTTP transport and anti-detection: TLS fingerprinting, bot challenge detection, challenge solving (inline + browser), cookie caching, fingerprint rotation, retry/backoff, Opera Mini impersonation, rate limiting. Fetchaller calls `session.get(url)` and gets back a response — it never knows or cares how wafer bypassed protections.

- **fetchaller-mcp** owns ALL content processing and MCP tooling: takes raw HTML/JSON/PDF from wafer and turns it into clean markdown for LLMs. Site-specific CSS selectors, BeautifulSoup cleanup, regex post-processors, HTML→markdown conversion, PDF extraction, JSON-LD extraction, search result parsing, response caching, MCP tool definitions.

Content-type dispatch in `src/fetchaller/tools/fetch.py` handles: `application/json` (as-is), `text/plain`/`text/csv` (as-is), XML/RSS/Atom (feed-parse then markdown, or raw XML fallback), `application/pdf` (text extraction via pymupdf), HTML (site dispatch → markdown, or raw with `raw=true`), `image/svg+xml` (returned as raw XML text), other `image/*` (metadata summary — type, filename, size, dimensions for PNG/GIF/JPEG/WebP, Last-Modified, ETag), and any other `text/*`/`application/javascript` (raw text). Everything else returns an "Unsupported content type" error. The header is sniffed against the body before any of that dispatches (`sniff_content_type`), because S3/CDN-hosted files carry whatever type the uploader's tooling sent — reolink serves its spec-sheet PDFs as `multipart/form-data`. A file signature (`%PDF-`, PNG/JPEG/GIF/WebP/BMP headers) overrides the declared type outright; a leading `<!doctype html`/`<html`/`<?xml`/`<rss`/`<feed` is weaker evidence and only fills in for a declared type nothing above can dispatch on, so an honest header is never second-guessed. Normal Reddit URLs are a deliberate pre-dispatch exception: they are mapped to bounded structured requests and compact-rendered; caller-selected `.json` remains JSON. Over-budget JSON keeps a whole-scalar structural prefix and an explicit `_fetchaller_truncated` marker, rather than returning a syntactically broken slice or silently changing an upstream value.

**The rule**: fetchaller never implements bot solving, impersonation,
challenge detection, or cookie management. It injects wafer's shared
`BrowserSolver` into sessions and, when configured, launches it once during
startup readiness preflight. All challenge behavior remains inside wafer.

### Challenge fallback: `get()` then `render()`

wafer solves WAF interstitials two ways and they do **not** have the same
reach. `get()` runs the inline/native/solver path; `render()` navigates in the
browser, solves in place with the same per-WAF handlers, then re-captures the
page. So `tools/fetch.py` retries a `ChallengeDetected` GET through
`session.render()` before reporting failure (`_render_after_challenge`).

Measured on `support.lutron.com` (Imperva): `get()` raised even with a real
system-Chrome solver attached, while `render()` returned 200 and the article.
The underlying cause was a wafer bug — `imperva_embedder` sent the reese84
solve to a *sibling* host (`www.lutron.com`, a different Imperva site that
never challenges), so the solve earned useless cookies and failed while
reporting success. Fixed in wafer 0.4.8, which solves in place when the
challenged host serves the sensor itself.

The fallback is kept regardless, because the asymmetry is general and not
Imperva-specific. Constraints, each load-bearing:

- **GET only.** A render is a navigation and cannot carry the caller's method,
  body or headers, so replaying a POST would send something never asked for.
- **Requires a browser solver**, and respects the remaining deadline.
- **The final-host check still runs on the rendered response.** A solve can
  surface a host that was never validated, so render's result is not trusted
  any more than a normal one.
- **Falls through to the original challenge error** when render does not help,
  so a genuinely unsolvable site still reports the challenge rather than a
  second, less useful error.

**The converse rule**: escalate to wafer only when something is actively
*blocked*. Working out an endpoint's shape — reading bundles, guessing a
required field, telling one JSON blob from another — is content analysis, and
belongs here. `src/fetchaller/discovery/` does it by observing a page in a
browser and replaying what it saw. That package was first built inside wafer and
discarded: 39% of it was content analysis, it added zero bypass code, and none of
the seven boards it was validated against needed a challenge solved. See
`docs/spa-discovery.md`, and `wafer-feedback.md` for the record.

### Reddit read path

Mapped normal-URL `fetch` calls, `browse_reddit`, and `search_reddit` share the
server's `RedditRequestQueue`. Every read is logged out: one long-lived
`wafer.AsyncSession` with a persistent anonymous cookie jar. There is no
credentialed transport -- no OAuth origin, no DART-profile session -- because
fetchaller holds no Reddit credential to select one with. Wafer >=0.4.6 owns
that transport; fetchaller never parses a verification
challenge. Direct/library
`fetch_url` calls without the injected queue use the process-wide Reddit domain
limiter. Caller-selected `.json`, `raw=true`, and unmapped HTML fallbacks still
use the generic fetch path and its Reddit domain limiter.

Every mapped route is an anonymous read; fetchaller has no Reddit credential
path of any kind. Routes Reddit serves only to a logged-in account -- exact
moderator rosters and account-private upvoted/downvoted activity -- return an
explicit account-gated error. No roster, vote count, or wiki page is ever
inferred or reconstructed. Anonymous roster pages are merged until Reddit
removes its cursor; invalid/repeated cursors or the bounded page cap are
explicit errors, never silent truncation.

JSON reads use the exact HTTPS `api.reddit.com` transport origin; canonical
route identity and emitted links remain on `www.reddit.com`. Anonymous safe-equivalent redirects from
either public Reddit origin are normalized back to the API origin, charge every
hop to the shared queue, and share the original deadline. Authenticated reads
reject every redirect. Missing locations, unrelated origins, loops, and excess
hops fail closed.
Reddit's exact nonexistent-subreddit redirect to its JSON community search is
mapped directly to a not-found content state instead of returning search noise.

Reddit removed Post Collections in 2024, so their former metadata endpoint can
no longer satisfy the legacy public read. For an exact collection URL,
fetchaller queries Wayback CDX for pre-removal captures, accepts only the
matching Reddit URL and a single exact `window.___r` collection model, and
extracts its ordered post fullnames. It then hydrates those fullnames through
current Reddit `/api/info`. Output labels archived metadata separately from
current post data; identity mismatches, shells, empty models, missing posts, or
partial hydration fail explicitly.

For normal public URLs, fetchaller validates the path shape, builds a bounded
`www.reddit.com/*.json` request, and renders only fields visible/useful to a
public reader. Thread limits are derived from `maxTokens` (5–500 items, depth
1–10), then the final character budget is enforced at whole-section
boundaries. `more` nodes provide bounded continuation links, and anonymous
`/api/morechildren` URLs are mapped into readable nested comments. Profile
roots use five independent public sources under one deadline: metadata,
overview, trophies, public multireddits, and moderated communities.

The wiki page index is the one structured HTML-first route:
`/r/{subreddit}/wiki/pages.json` is no longer readable anonymously, while canonical
New Reddit server-renders the public tree under
`/r/{subreddit}/wiki/pages/`. Fetchaller reads that exact same-origin document
through the durable Reddit session and queue, accepts only the named
`#wikis-right-rail-container .page-tree` inside a matching
`community_wiki`/`subreddit_wiki` page, and rejects challenge shells, error
shells, foreign links, malformed paths, and empty trees instead of reporting
zero pages.

Communities without that New SSR tree, or whose exact anonymous HTML route
returns an unstructured 403, fall through without transport backoff to New
Reddit's own logged-out page tree: a `WikiPageRevisionsV2` POST on the fixed
`https://www.reddit.com/svc/shreddit/graphql` route, authorized only by the
`csrf_token` cookie the same anonymous session already holds. This is the route
the wiki UI itself uses, so the page index needs no OAuth scope and no browser.
The reply is accepted only when it stays on that exact route, returns
`application/json`, carries no GraphQL `errors`, and describes the requested
community (`__typename`, `name`, and `prefixedName` must all agree). Every node
must agree with its own path: `name` is the last path component, `parent` is the
remaining prefix, `depth` is the component count minus one, components are safe,
and paths are unique. Namespace parents (`isPagePresent` false) are excluded
exactly as Reddit's own index excludes them; a valid tree with no present pages
renders zero pages, while any structural disagreement is an explicit error.

There is no credentialed fallback for the wiki index: the SSR tree and the
anonymous `WikiPageRevisionsV2` route are the entire contract, and an
unavailable tree is an explicit error.

The renderer reports Reddit's returned `score` as the public fuzzed score and
`upvote_ratio` only when present. It never derives separate upvote/downvote
counts. It preserves post/comment Markdown, outbound and discussion links,
gallery order, native video URLs, oEmbed provider/title, crosspost source, poll
vote data, NSFW/spoiler/locked/archived/deleted states, and useful URLs embedded
in rich comments. Private, quarantined, banned, and not-found responses are
mapped from Reddit's structured reason without treating those content states as
transport failures or queue-wide backoff events.

Compactness is a bounded-output property, not a parity tradeoff: the renderer
keeps complete sections and explicit omission/continuation markers. Published
size claims require a checked-in reproducible corpus and raw outputs.

## Content Processing Architecture

`src/fetchaller/content/` handles HTML→markdown conversion:

- **`html.py`** — Generic pipeline + dispatch. Universal junk selectors (nav, footer, ads, cookie banners, modals), markdownify conversion, whitespace cleanup. Dispatches to site modules based on URL. Includes generic JSON-LD Product fallback for sites without dedicated modules.
- **`amazon.py`** — Amazon (all TLDs): CSS selectors, soup cleanup, regex post-processors. Covers .com, .ca, .co.uk, .de, .fr, .it, .es, .co.jp, .com.au, .in, etc.
- **`github.py`** — GitHub: CSS selectors, soup cleanup, regex post-processors, URL transforms, file tree extraction, issue/PR/discussion extraction from embedded JSON.
- **`reddit.py`** — Reddit: strict hostname recognition; canonical
  `www.reddit.com` URLs; public thread/listing/search/profile/about/rules/wiki
  structured mapping (JSON plus the canonical SSR wiki page tree);
  comment-boundary token budgeting; compact post, nested-comment, rich-media,
  poll, and access-state renderers. Explicit `.json` stays raw and `raw=true`
  uses canonical New Reddit HTML.
- **`hackernews.py`** — Hacker News: CSS selectors, table unwrapping, story block reformatter.
- **`medium.py`** — Medium: CSS selectors (data-testid), source param stripping, post-article block removal. HTML-based detection for unknown custom domains.
- **`huggingface.py`** — Hugging Face: data-target CSS selectors, filter tag/button soup cleanup, regex post-processors.
- **`stackoverflow.py`** — Stack Overflow / Stack Exchange: CSS selectors, soup cleanup, regex post-processors. Covers all SE network sites.
- **`redflagdeals.py`** — RedFlagDeals forums: RFD-specific CSS selectors, soup cleanup, regex post-processors.
- **`forums.py`** — Generic forum support (XenForo, vBulletin, phpBB, Discourse). URL transform → RSS/Atom feeds, autodiscovery, feed parser, HTML fallback with generic CSS selectors.
- **`wikipedia.py`** — Wikipedia: CSS selectors for edit buttons, navboxes, TOC, reference lists.
- **`alibaba.py`** — Alibaba.com: CSS selectors, embedded JSON extraction (`window.detailData`, `window.__page__data_sse10`), soup cleanup.
- **`aliexpress.py`** — AliExpress: CSS selectors, soup cleanup, regex post-processors.
- **`craigslist.py`** — Craigslist (all city subdomains): CSS selectors, regex post-processors for nav/safety/loading noise. Search URL detection for SAPI intercept.
- **`facebook_marketplace.py`** — Facebook Marketplace URL detection only (`/marketplace/*` paths). GraphQL client lives in `facebook_marketplace/` package.
- **`digikey.py`** — DigiKey (all TLDs): CSS selectors, soup cleanup, regex post-processors. Behind Akamai (wafer handles). HTML fallback when no API key configured.
- **`ebay.py`** — eBay (all TLDs): CSS selectors, JSON-LD product data extraction, search result DOM extraction (`.s-item` elements), soup cleanup, regex post-processors.
- **`molex.py`** — Molex (molex.com): JSON-LD Product extraction (additionalProperty specs), AEM header/nav/account CSS selectors, regex post-processors for nav/About Us boilerplate. CSR site — product specs only available via JSON-LD.
- **`mouser.py`** — Mouser (all TLDs): CSS selectors, soup cleanup, regex post-processors. Behind Akamai (wafer handles). HTML fallback when no API key configured.
- **`fcc.py`** — FCC filings (fcc.report, fcc.gov EAS): fix broken nav nesting (lxml absorbs body content into unclosed navbar-collapse div), structured data extraction from frequency authorization / exhibit / application detail tables, CSS selectors for FCC navigation chrome, regex post-processors for boilerplate. Behind Cloudflare (wafer handles via BrowserSolver).
- **`soylent.py`** — Soylent (soylent.com, soylent.ca): Shopify store cleanup, inventory extraction from `gsf_conversion_data`.
- **`ti.py`** — Texas Instruments (ti.com): CSS selectors, document viewer support for lazy-loaded datasheets, inventory placeholder extraction.
Job-board modules (Ashby, Greenhouse, Lever, Gem, Dayforce, Cornerstone, Workday, BambooHR, JazzHR, HubSpot) follow a shared philosophy: **preserve the source's own structure and vocabulary**. Each company posts differently, so the renderer dumps raw field names, raw enum values, and the platform's own section titles instead of translating them into a uniform "Employment Type:", "Voluntary Demographic Questions:", etc. New fields the platform adds flow through automatically via iterate-and-dump over the raw response. The only opinionated pieces are (1) the `# Title @ Company` header, (2) the `**sourceUrl**:` footer, (3) the `## questions` / `## applicationForm` section dividers, and (4) small per-platform skip-lists for fields that duplicate content we already render (e.g. `descriptionPlainText` when `descriptionHtml` is rendered).

- **`ashby.py`** — Ashby job boards (jobs.ashbyhq.com): CSR SPA. Extracts `window.__appData.posting` + `organization`, dumps every non-empty scalar as `- **fieldName**: value` using Ashby's own keys (`departmentExternalName`, `workplaceType: Hybrid`, `employmentType: FullTime`, `compensationTierSummary`, …). Description HTML rendered as-is (no heading demotion). Application form preserves Ashby's own section titles and renders each field with its raw platform type (`String`, `File`, `LongText`, `Boolean`, `ValueSelect`). Survey forms (EEO/diversity) preserved with their own section titles + descriptions. Falls back to schema.org JobPosting JSON-LD when `__appData` isn't present. Also handles two embed shapes on company career sites: (1) `?ashby_jid=<uuid>` deep-link URLs — org slug extracted from the page's careers-*.js chunk, cached per hostname; (2) `<script src="https://jobs.ashbyhq.com/{org}/embed">` script-tag embeds — slug parsed from the src and the canonical `jobs.ashbyhq.com/{org}` board fetched.
- **`greenhouse.py`** — Greenhouse job boards (boards.greenhouse.io, job-boards.greenhouse.io) + embeds on company career sites (iframe `boards.greenhouse.io/embed/job_app`, `div#grnhse_app`, `?gh_jid=&gh_src=` query params). Fetches `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true` and dumps the JSON verbatim — each known top-level key (`first_published`, `updated_at`, `requisition_id`, `location`, `offices`, `education`, `employment`) and each `metadata` entry as `- **metadata.{CompanyFieldName}**: value`. Questions rendered in source order with raw field types (`input_text`, `input_file`, `multi_value_single_select`). Demographic block uses Greenhouse's own `header` + `description` HTML. Three entry points in `fetch.py`: direct URL detection (pre-fetch), embed HTML detection (post-fetch), and hostname-derived guess for company sites carrying `?gh_src=` without the board token in markup (e.g. `dropbox.jobs/en/jobs/{id}?gh_src=X` → probes `boards-api.greenhouse.io/v1/boards/dropbox/...`).
- **`lever.py`** — Lever job boards (jobs.lever.co/{company}/{id}): fetches `api.lever.co/v0/postings/{company}/{id}?mode=json` + parses the `/apply` HTML page. Dumps every raw posting field (`categories`, `workplaceType: remote`, `country`, `createdAt`, …) and preserves Lever's own `lists` array titles verbatim (Responsibilities, Required Qualifications, Physical/Cognitive Requirements, …). Application form sections pulled from the `/apply` markup preserve whatever section titles the company set ("Work Authorization Questions", "SMS/Text Messaging Consent", "eeo", …) instead of bucketing into our own categories. Each field rendered with its full HTML-level type hints (`resume`, `input[file]`, `multiple-choice`, `input[radio]`).
- **`gem.py`** — Gem job boards (jobs.gem.com/{board-slug}/{extId}): pure SPA with 4 KB of bootstrap HTML. Hits the unauthenticated Apollo GraphQL endpoint at `jobs.gem.com/api/public/graphql` with the `ExternalJobPostingQuery` operation lifted from the job-board JS bundle (the public schema forbids introspection so fragment field lists are hard-coded). Dumps every non-empty posting field using Gem's own key names (`firstPublishedTsSec`, `locationType: REMOTE`, `employmentType: FULL_TIME`). Nested objects (`job`, `locations`) serialized as inline JSON. Questions rendered with Gem's raw `answerType/displayType` combos (`SINGLE_SELECT/RADIO_BUTTON`, `LONG_TEXT`, `SHORT_TEXT`). Demographic section uses Gem's own `surveyType` as the heading.
- **`dayforce.py`** — Dayforce HCM job boards (jobs.dayforcehcm.com/{lang}/{namespace}/{board}/jobs/{id}): Next.js SPA, but the posting detail page SSRs the full `jobData` into `__NEXT_DATA__` — parses that and dumps every non-empty scalar (`jobPostingId`, `jobReqId`, `createdTimestampUTC`, `hasVirtualLocation`, `postingType`, …) plus `postingLocations[].formattedAddress` and `jobPostingAttributes[]` (e.g. `PayType: Salary`). Description body is split into Dayforce's own `## jobDescriptionHeader` / `## jobDescription` / `## jobDescriptionFooter` sections. Board-listing page only ships `site-info` in `__NEXT_DATA__`; the postings come from a CSRF-protected POST to `/api/geo/{namespace}/jobposting/search`, so the board fetcher GETs the page (for cookies + site-info), GETs `/api/auth/csrf` for the NextAuth token, then POSTs the search. Also handles white-label deployments where companies host the Dayforce Next.js portal on their own domain (e.g. `www.synaptivemedical.com/job-openings`) — `extract_dayforce_canonical_board_url()` reads `runtimeConfig.BASE_URL` + `query.clientNamespace` + `query.careerSiteXRefCode` from the SSR'd `__NEXT_DATA__` and rewrites to the canonical `jobs.dayforcehcm.com/{lang}/{namespace}/{board}` form so the same fetcher works.
- **`cornerstone.py`** — Cornerstone OnDemand job boards (`{tenant}.csod.com/ux/ats/careersite/{cid}/home/requisition/{reqid}` and `.../home`): ~5 KB SPA shell carrying a per-page JWT in an inline `csod.context = {...}` blob along with the regional cloud API base (`us.api.csod.com`, `eu.api.csod.com`, `uk.api.csod.com`, `au.api.csod.com`), `cultureID`, and `corp` tenant slug. Posting fetcher reads the JWT then GETs `services/x/job-requisition/v2/requisitions/{reqid}/jobDetails?cultureId={n}` on the tenant host. Board fetcher POSTs `rec-job-search/external/jobs` on the regional cloud host. Both use `Authorization: Bearer <jwt>` + `CSOD-Accept-Language` headers. Renders every non-empty `jobDetails` field plus the HTML `externalDescription` as markdown.
- **`workday.py`** — Workday "myworkdayjobs" boards (`{tenant}.wd{1-103}.myworkdayjobs.com/[{lang}/]{site}` + `.../job/{externalPath}`): the public pages are SPA shells; the data lives at `/wday/cxs/{tenant}/{site}/jobs` (POST, paginated) and `/wday/cxs/{tenant}/{site}/job{externalPath}` (GET). Board fetcher pages in batches of 20 (capped at 200 jobs) with body `{appliedFacets, limit, offset, searchText}`. Some tenants (e.g. salesforce.wd12) only return `total` on page 1 and zero it on subsequent pages — `total` is locked to the first page's value. Posting fetcher dumps every `jobPostingInfo` field plus the `jobDescription` HTML; layout-only `<span class="WKQ0">` blocks (Workday wraps spacing in 40-char space spans) are stripped before markdownify.
- **`bamboohr.py`** — BambooHR career boards (`{tenant}.bamboohr.com/careers` + `/careers/{id}`): both endpoints return clean JSON unauthenticated — `GET /careers/list` for the board (`{meta:{totalCount}, result:[{id, jobOpeningName, departmentLabel, employmentStatusLabel, location, atsLocation, locationType}]}`) and `GET /careers/{id}/detail` for the posting (`{result:{jobOpening:{...description HTML, additionalInformation, …}}}`). Board renderer groups by `departmentLabel`. Bogus/inactive tenants are detected naturally — BambooHR redirects unknown subdomains to `www.bamboohr.com/` with `Content-Type: text/html`, which fails JSON parse and falls through. Also handles widget embeds via `extract_bamboohr_embed_tenant()`: any HTML page containing `<div id="BambooHR" data-domain="{tenant}.bamboohr.com">` triggers a `/careers/list` fetch on that subdomain.
- **`jazzhr.py`** — JazzHR career pages (`{tenant}.applytojob.com/apply` + `/apply/{id}/{slug}`): the board page is SSR'd (parsed via `.list-group .list-group-item` selectors with optional `.department-heading h3` preceding each group); the posting page carries a full `schema.org/JobPosting` JSON-LD block we render directly. Multi-tenant aggregation: white-label company career pages (e.g. `earthdaily.com/job-openings`) often pull listings from more than one JazzHR tenant via JS — `extract_jazzhr_embed_tenants()` regex-scrapes all `*.applytojob.com/apply` references out of the markup (deduped/ordered) and `render_jazzhr_boards()` produces a single combined document with each tenant as a `##` section. Inactive tenants render as `# {tenant} — Job Board (0 open positions)` (their `/apply` page returns `<title>JazzHR - Inactive Career Page</title>`).
- **`hubspot_careers.py`** — HubSpot's careers SPA at `www.hubspot.com/careers/jobs/{id}` (often with a vestigial `?gh_jid={same id}` query). Pure CSR — the served HTML is ~200 KB of chrome with zero job content; the page POSTs a single `Job(id: ID!)` operation to the unauthenticated GraphQL endpoint at `wtcfns.hubspot.com/careers/graphql` to populate. The `?gh_jid=` parameter is HubSpot's own job id, not a Greenhouse one — the `hubspot` Greenhouse board exists but is empty. Dispatch must intercept BEFORE `extract_greenhouse_params_guess()` for that reason: the guess returns `('hubspot', '{id}')` and the resulting 404 falls through to the empty-SPA HTML path, producing the no-content output that prompted this module. The `content` field is double-encoded HTML (entity refs inside an HTML string); question `description` fields are single-encoded — the renderer always runs `unescape` (idempotent) before markdownify.

Each site module exports the same interface: `is_<site>(url)`, `SELECTORS_LIST`, and optionally `strip_<site>_junk(soup)` / `postprocess_<site>(markdown)`. To add cleanup for a new site, create a new module following this pattern.

## Search Architecture

`src/fetchaller/search/` handles web search:

- **`__init__.py`** — Main `search()` function, result merging/dedup, 5-minute query cache, per-engine rate limiters (2s Google, 1s DDG), CAPTCHA escalating backoff (2m→5m→15m), lazy session lifecycle.
- **`google.py`** — Google search result extraction, CAPTCHA detection. Returns `(results, is_captcha, error)`.
- **`ddg.py`** — DuckDuckGo HTML endpoint. Only queried on page 1. Returns `(results, error)`.
- **`models.py`** — `SearchResult` dataclass.
- **`tools/search.py`** — MCP tool wrapper.

### Per-engine sessions

The two engines need **different TLS identities**, so `search/__init__.py` keeps two lazily-created sessions:

- **Google** — `wafer.AsyncSession(profile=Profile.OPERA_MINI)`. Required, not incidental: the SSR request declares `client=ms-opera-mini-android`, so the TLS identity has to match the client the query claims to be. wafer owns the entire Opera Mini impersonation (52 confirmed versions, 21 real devices, correlated fingerprints); fetchaller just parses the HTML.
- **DDG** — `wafer.AsyncSession()` with the default profile. DDG answers an Opera Mini identity with **HTTP 202 and the DuckDuckGo homepage** instead of results. Sharing one Opera Mini session across both engines broke DDG on every query.

### Transport errors are never silent

Both engines report transport/HTTP failures to `search()` instead of returning an empty list. A network error or non-200 renders as `google: ERROR` / `ddg: ERROR` plus the cause, and a total failure says `Search FAILED — no engine returned results`.

This exists because the previous "return `[]` on error" behaviour made a dead engine indistinguishable from a query with no hits — output read `ddg: 0 new`, which is precisely how the DDG breakage above stayed invisible. Engine errors are cached alongside results so a replayed partial result still names the engine that failed.

## Marketplace Search Architecture

`src/fetchaller/marketplace/` orchestrates concurrent searches across Kijiji, Craigslist, and Facebook Marketplace:

- **`search.py`** — Main `search_marketplace()` orchestrator. Launches platform searches via `asyncio.gather` with 45s timeout. Auto-skips Kijiji for non-Canadian locations. Appends ", Canada" to Facebook geocode queries for Canadian cities to avoid disambiguation issues (e.g. Vancouver, BC vs WA).
- **`aliases.py`** — Cross-platform alias mappings: `SORT_MAP`, `CATEGORY_MAP`, `CONDITION_MAP`. Maps human-readable values (e.g. "cars", "price_asc", "like_new") to platform-specific values.

Platform-specific clients used by the orchestrator:

- **Craigslist** (`src/fetchaller/craigslist/`) — SAPI v8 client + static location map (~50 CA + ~100 US cities with fuzzy matching).
- **Kijiji** (`src/fetchaller/kijiji/`) — Unauthenticated Apollo GraphQL + static location tree (from kijiji-scraper). Canada-only.
- **Facebook Marketplace** (`src/fetchaller/facebook_marketplace/`) — GraphQL search/listing/geocode via public `api/graphql/` endpoint. Session cookies seeded from marketplace page visit. Rate-limited via `facebook_limiter`.

## HTTP Transport (Wafer)

All HTTP fetching is handled by `wafer` (`~/code/wafer`). Fetchaller does NOT contain any bot protection, challenge solving, or TLS fingerprinting code.

- **`wafer.AsyncSession`** — per-request sessions with challenge detection and supported solving, cookie caching, fingerprint rotation, retry/backoff
- **`wafer.browser.BrowserSolver`** — Patchright-based browser solver for Cloudflare, Akamai, etc. One shared instance created at server startup, passed to sessions via `browser_solver=`
  - Every Chromium connection is forced through fetchaller's loopback-only
    SOCKS5 egress guard. The guard applies the same private/internal address
    policy to redirects and browser subresources, then dials only the approved
    numeric IP. This is separate from wafer's `resolve=` pins because Chromium
    has its own DNS/network stack. Public nonstandard ports remain available
    subject to Chromium's own unsafe-port policy; QUIC and non-proxied WebRTC
    UDP are disabled by the solver.
- **`wafer.Profile.OPERA_MINI`** — first-class Opera Mini impersonation for search
- **Challenge types wafer detects (18)**: ACW, TMD, Amazon, Reddit, Cloudflare, Akamai, DataDome, PerimeterX, Imperva, Kasada, F5 Shape, AWS WAF, Vercel, Arkose, GeeTest, hCaptcha, reCAPTCHA (v2), generic JS. Detection is not the same as solving:
  - **Inline (pure Python, no browser)**: ACW (shuffle+XOR), TMD (homepage session-warming), Amazon ("Continue shopping" form parse + follow), Reddit (bounded logged-out New Reddit verification form parse + same-session submission). Reddit cookies are persisted without consuming the large solved homepage body.
  - **Browser solver (`BrowserSolver`)**: Cloudflare, Akamai, DataDome (WASM PoW; bails on interactive captcha), PerimeterX (press-and-hold), Imperva (native-TLS free-pass first, browser-solve on the origin page under escalation), Kasada, F5 Shape, AWS WAF, GeeTest v4 (slide), hCaptcha, reCAPTCHA **v2** (checkbox + ONNX grid).
  - **Detect-only — NOT solved**: Arkose / FunCaptcha (no solver → raises `ChallengeDetected`). Vercel and `generic_js` have no dedicated solver either, but a generic browser JS-wait passes their *passive* JS checks.
  - **reCAPTCHA v3**: not a detected challenge — minted browser-free via `session.mint_recaptcha_v3(sitekey, action)` (score token). fetchaller does not currently use this.

Any browser navigation that remains challenged or blocked is a failed solve,
not usable content. The caller's request timeout is the total budget, including
browser work. If a site blocks requests, **fix it in wafer, not fetchaller**.

## Persistent State

The container is restarted routinely — image updates, host maintenance, scheduled jobs — so
anything that must outlive a restart has to be on disk. `/app/data` is the only durable
location; it is a mounted volume and `entrypoint.sh` chowns it to `appuser`.

| State | Location | Survives restart? |
|---|---|---|
| OAuth clients + refresh token hashes | `/app/data/oauth_clients.json` | Yes |
| wafer cookie cache | `${WAFER_CACHE_DIR:-/app/data/wafer}` | Yes |
| Exact Chrome for Testing used by BrowserSolver | `${BROWSER_EXECUTABLE_PATH:-/opt/google/chrome/chrome}` (baked into amd64 image) | Yes |
| Pinned reCAPTCHA ONNX models | `/app/model-cache` (baked into image, read-only) | Yes |
| OAuth access tokens | none — stateless JWTs signed with `JWT_SECRET` | Yes, **if `JWT_SECRET` is set** |
| Authorization codes | memory | No — 10 min TTL, cheap to retry |
| CSRF tokens | memory | No — 10 min TTL |
| Response cache | memory | No — 5 min TTL by design |
| Reddit queue counters | memory | No — rate-limit windows, by design |

Three rules follow from this, each learned from a production bug:

1. **`JWT_SECRET` must be set and stable.** Unset, the server generates a random signing secret
   per process, silently invalidating every OAuth token on restart and forcing every user to
   re-pair by hand. HTTP mode now refuses to start without it (`ALLOW_EPHEMERAL_JWT=1` opts out
   for local dev), and `/health` exposes `jwt_secret_ephemeral`.
2. **Never default durable state to `$HOME`.** `appuser` has no home directory in the image, so
   `Path.home()` resolves to an uncreatable path. This silently disabled the wafer cookie cache.
   Durable mutable state now defaults into `/app/data`; browser and model artifacts are immutable
   image assets outside that volume.
3. **Import success is not availability.** `BrowserSolver` importing proves only that the Python
   package exists. Startup loads both pinned models and launches the exact configured Chrome
   executable before authenticated HTTP readiness can pass.
