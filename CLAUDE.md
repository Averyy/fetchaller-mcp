# fetchaller-mcp

MCP server for fetching any URL without domain restrictions. Full Reddit support. Built-in web search.

## Architecture

fetchaller owns content processing + MCP tools. wafer (`~/code/wafer`) owns HTTP transport + anti-detection. fetchaller NEVER does bot solving, impersonation, or cookie management — if a site blocks requests, fix it in wafer. See `docs/architecture.md` for full details.

**Escalate to wafer only for active blocking** — bot detection, a WAF challenge
needing a solve, TLS rejection, clearance cookies, rate limiting. Anything that
is merely *finding* the right request — reading JS bundles, guessing an
endpoint, working out a required field, decoding a payload shape, telling one
JSON blob from another — is content analysis and must be built here. Before
writing anything down for wafer, ask: is this request being *refused*, or do I
just not know its shape yet? Only the first is wafer's. `src/fetchaller/discovery/`
exists for the second — see `docs/spa-discovery.md`. This boundary was settled
empirically: the discovery capability was built inside wafer, validated against
seven boards, and discarded because none of them needed a challenge solved
(`wafer-feedback.md` is the record).

### Reddit

Normal Reddit URLs map to New Reddit's logged-out JSON URLs, except the
wiki page index, which first reads New Reddit's canonical SSR page tree and,
when that exact tree is unavailable or its exact anonymous route returns an
unstructured 403, posts `WikiPageRevisionsV2` to the fixed
`www.reddit.com/svc/shreddit/graphql` route using the same anonymous session's
`csrf_token` cookie. Public wiki parity must pass anonymously.

**Two logged-out routes, both wafer's (>=0.7.0).** JSON reads go through
Reddit's Android app API on an anonymous install token that wafer mints and
caches itself (`cache_dir/reddit-app.state`); HTML, and any read the app route
cannot serve, take the web route's anonymous cookie bootstrap, which is
browser-free. fetchaller holds no Reddit credential of its own -- no account,
no login, no OAuth user token, no client ID or secret in this repo -- and must
never gain one; the anonymous app token is a transport detail of wafer's, like
the web route's anonymous cookies. Never put a `Cookie` or `Authorization`
header on the Reddit session: wafer reads either as an account request and
skips the app route. Explicit `.json` URLs take the generic fetch path, whose
SSRF pins cover only the validated host, so they stay on the web route.
Neither route may ever need the browser; the strict parity gate audits that
(`REDDIT_SESSION_AUDIT`, `BROWSER_DISPATCH_SUMMARY`). Routes Reddit serves only to a
logged-in account (exact moderator rosters, account-private vote activity)
return an explicit account-gated error and are covered offline as
`fixture_only`. The sitewide comment stream (`/comments/`, `r/all`, `r/popular`
comments) now answers anonymous reads with no children and reddit.com's own
`/comments/` page is "Page not found"; it renders with a statement that Reddit
withdrew it, never as a bare "0 items returned". **Gilded listings** (`/gilded/`,
`/comments/gilded/`, `/r/<sub>[/comments]/gilded/`, `/user/<name>/gilded/`)
were retired by Reddit, which answers them with 400, a structured 403, 404, or
a 301 to the profile; fetchaller reports exactly that answer. **Post
collections** answer HTTP 500 and the **gold-only communities directory**
redirects to the premium one; both are reported as they are. All three were
once reconstructed from Wayback captures to satisfy the parity gate -- slow (an
index lookup alone took up to 54 s), years stale, and the cause of gate
timeouts -- and that was removed on 2026-10-07. Never rebuild a retired Reddit
surface from an archive: if Reddit answers 400/404, so does fetchaller. Wafer owns verification, the app token and their persistence;
fetchaller owns strict URL mapping, SSR/API schema validation, and compact
rendering. Never add an Old Reddit fallback or copy wafer's
verification parser into this repo. Explicit `.json` stays raw JSON and
`raw=true` fetches canonical New Reddit HTML. Preserve Reddit's public score as
a score (not an upvote count); show `upvote_ratio` only when returned, and never
invent separate up/down vote counts.

### Ubiquiti (ui.com)

A UniFi store page ships its price in the HTML but **none of its
specifications** — those render client-side from `__NEXT_DATA__` — so the plain
HTML path returns a product page that looks complete and silently has no specs
on it. Dispatch on the Next.js route (`__NEXT_DATA__["page"]`), never on URL
shape, because the store rewrites `/pro/category/...` onto the same route.
Two route traps, both of which fail by rendering something plausible: door
access and cameras file products under a **collection** route, and missing it
drops every spec section while still producing a page; and an unknown category
is a **soft 404** — the store answers 200 and rewrites onto its home route, so
falling through renders the storefront under a heading the caller supplied.
Report that instead. Quote
`minDisplayPriceWithSurcharges` as the price, since that is what the store
charges and displays; name the base only when it differs. Money is in minor
units scaled by the **currency's** exponent, not a flat 1/100 — the JP store
prices in whole yen, so dividing by 100 there is a silent 100x error with no
symptom. Both `minDisplay*` fields are the minimum across variants, so say
"from" when the variants disagree rather than presenting the cheapest as the
price. A spec section's
features are a flat list — group children are linked by `feature.parentId`, not
nesting — and an absent capability flag renders as `—` rather than vanishing,
because "no 6 GHz radio" and "unstated" must not look alike.

Installation guides carry **no readable text**: every word is outlined vector
art. Never render one as though it had been read — say what it is and reproduce
the pages via `get_unifi_manual`, which rebuilds them with PyMuPDF (already a
dependency; add none). `dl.ui.com` returns **200 with an app shell** for a slug
that has no guide, so a status code proves nothing — detect the bootstrap
markers, and report a missing guide against the URL the caller asked for, not
the redirect target. fetchaller has NO credentialed path to any ui.com property.

### The Home Depot (homedepot.com, homedepot.ca)

**homedepot.com is read through its own federation gateway, never its HTML.**
Every www.homedepot.com page but the home page answers a session that has not
run Akamai's sensor script with a 2.5 KB behavioural-challenge interstitial
(`sec-if-cpt-container`) — 403 cold, 200 warm — so without a browser the HTML
path reads nothing. That block is wafer's and stays wafer's. But the data those
pages draw comes from `POST /federation-gateway/graphql?opname=...`, which
answers a plain cold request with no challenge; reading *that* is finding the
request, so it is built here (`src/fetchaller/homedepot/`). Products, search
(`/s/`), category/brand listings (`/b/.../N-...`, with `Nao`, `sortby`,
`lowerbound`/`upperbound`), review pages and store pages are all gateway reads.
`/c/` content pages, collections and the like still take the HTML path, cleaned by
`content/homedepot.py`; a landing page (`#root.landing-page`) is rendered from
its `__APOLLO_STATE__` `UniversalLayout`, because its HTML draws only some of its
sections (customer service drew none). With
wafer >= 0.7.6 the browser solver clears that challenge on macOS (~6 s cold)
and in the **Linux image** (~11 s cold), with plain reads after, and returns
the server's own document. Up to 0.7.5 Akamai refused every solve in the image:
with no GPU, Chrome drew WebGL on Mesa's llvmpipe, which Akamai rejects; 0.7.6
renders on SwiftShader there. That was wafer's to fix and was fixed there. The
solved document can carry one inline `<script>` the plain one lacks
(`Object.defineProperty(document, "referrer", ...)`); wafer reports it as
`source=network`, so it is the server's, and extraction drops it with every
other script. Where a solve still fails, these pages
fail — say so, do not fake them. The queries are
assembled from the site's component data models (its bundles carry no query
strings; introspection is 401) and validated against the gateway, which names
every unknown field. An unknown item, an unknown `N-` value and a redirected
keyword all answer 200 — `product: null`, a null `searchReport`, and a
`metadata.searchRedirect` with no products respectively — so each is checked
and either reported or (the redirect) followed and disclosed, never rendered as
an empty page.

**The gateway session must never share wafer's cookie cache.** It once did
(`cache_dir=get_wafer_cache_dir()`), and a failed browser solve of a
homedepot.com page leaves Akamai's flagged cookies in that cache: every gateway
POST then carried them and Akamai's edge answered 206 "Generic errors" from
`AkamaiGHost` until they expired (~20 minutes). That read as an edge-wide
refusal "after a burst of testing" on 2026-10-07; on 2026-10-08 one container
settled it, with gateway reads before the failed solve, and on an empty cache
after it, passing while the read on the shared cache was refused. The cache
holds only solver cookies and this session never solves, so it gains nothing
from one. (wafer 0.7.6 also stopped keeping a rejected solve's cookies.)
`com.py` still holds the gateway after any 206, failing reads at once and saying
when it will try again, and one failed solve still holds homedepot.com pages
(the HTML path) for 10 minutes, since a solve that fails tends to fail every
time it is retried.

Prices, pickup stock and badges are **per store**: quote store #121
(Cumberland, GA), the one the site assigns with none selected, and name it
beside every price. `original == value` is the normal case, not a sale. Rows
the gateway marks `isSponsored` say "Sponsored". **Reviews are pooled across a
product's variant family**: a page returns reviews written about sibling
Internet #s, and `Includes.Products.store` holds whichever sibling leads *that
page* — take the statistics from the entry whose `Id` is the URL's, and mark
every review of another variant. Store IDs come back zero-padded (`0121`);
compare numerically. A relocated store is listed only as its replacement,
named "<name> (Relo <old id>)" by the site — follow that label and say so.

**homedepot.ca is not blocked; it loses data to client-side rendering.** Its
pages carry Angular transfer state in `<script id="hdca-state">`
(`product-<code>`, `aemContent-<slug>.plpData`) and the markup alone renders a
product with an empty Specifications heading and a category with no products.
Keyword search pages carry no results at all; the browser calls
`/api/search/v1/search`, whose `keywordRedirectUrl` is followed as the site
follows it ("drill" → the Drills category). Its JSON services answer **only
after a page**: called cold, the same request that succeeds after its product
page meets an Akamai challenge. That is our request sequence, so every service
call follows a page in the same session with that page as Referer, and a
challenge reloads the page and retries once. Prices are the "Online" store's
(7274, "CANADA ECOMMERCE"); its `storeStock` is always out of stock and means
nothing, so only online `stock` is reported. The product state's `price` has
no sale in it — was-price and savings come from `products-localized-basic`,
and when that service does not answer the output says the sale is unknown
rather than implying there is none. Items with `productStatus` "OU" get no
online price from anywhere the page draws one; the catalogue price in the page
data is named as such, never quoted as the price. The not-found product page
answers 200 with an all-null `product-<code>` skeleton — require a `code`.
The shared session turns off wafer's redirect following and follows homedepot.ca hops
by hand, on-site only. fetchaller has NO credentialed path to either site.

### Vacuum Wars (vacuumwars.com)

The site's **comparison tool is the whole dataset and the plain HTML path
returned none of it**. `/compare/robot-vacuums/` is an Alpine.js app whose empty
state ("No products found.", "No brand found.", "Accordion Title") renders as a
board with nothing on it rather than as a failed read — the trap is that it
looks like an answer. Every model is in fact inline in the page as
`window.vwProducts`, 91 fields each, carrying Vacuum Wars' **own lab
measurements** and the star scores they roll up into; nothing else on the site
publishes that in machine-readable form. Parse it and discard the shell. That is
content analysis, not blocking, so none of it is wafer's.

One request gets everything and there is no second one to make: the tool
paginates **client-side** over that array, and `/page/2/` is a hard 404 with a
306 KB error body carrying no dataset at all. Tested and merely-listed robots
are two populations and the tested one is the **minority** — on 2026-09-11, 894
listings held 222 with any lab result — so count them separately and render a
missing measurement as `-`, never as blank, because "never tested" and "scored
nothing" must not look alike.

Colour variants collapse only when brand, base name, every measurement **and
every spec the table prints** match. Measurements alone are not enough and that
is the real trap here: three quarters of the catalogue was never tested, so
every measurement is null there, the signature goes degenerate and the rule
decays to brand-plus-name — which merged an "OKP L1" at 1400 Pa with obstacle
avoidance into an "OKP L1 (White)" at 4000 Pa without it, and printed one
member's specs beside the other member's price. That is the same silent
substitution `from` exists to prevent, arriving through a different door.
Compare the **rendered** value, not the raw one: the site types the same field
`4000` on one listing and `"4000"` on the next for ~40% of the numeric specs.
Drop a trailing parenthetical **only where dropping it is what merged the
rows** — more than one listing, under more than one name. A row standing alone
keeps the name the site gave it, because a parenthetical on a single listing is
usually not a colour: "Eufy L60 (No self-empty station)" sits beside "Eufy L60
with Self Empty Station", and "(Amazon Exclusive)", "(No AutoEmpty Dock)" and
the bare model numbers "(7550)", "(2152)" all carry meaning. Stripping by
default renamed 158 rows, 16 of them load-bearing. When merged prices disagree
say **"from"** and quote the cheaper, exactly as ui.com's `minDisplay*` fields
require — and say it too when a member has **no** cached price, since a bare
figure on a two-listing row claims both cost it.

**Rank means rank.** Sixteen models carry individual test results but no overall
score. They belong in the tested table, but numbering them makes a dataset
position read as a placing, so their rank cell is `-` and the table says how
many rows below it are unordered. A genuine score of `0` is a score and still
ranks; that distinction is the whole point. Prices are the tool's last
cached Amazon figure, so label them cached and never present them as current —
and print that caveat **before** the tables: the full render is ~30k tokens,
past the fetch tool's 25k default, and truncation took the closing paragraph
off. Order the spec table tested-first for the same reason, carry no score column
there (the ranked table above already has it for every row that has one), and
print a per-brand index of the untested tail in the header. The tail is sorted
by brand, so a budget cut takes whole brands off the end of the alphabet and a
missing row otherwise reads as a model the site does not list.

There is **no JSON endpoint** and that question is closed: the app's bundle is
byte-identical on both hosts and makes exactly one network call, the feedback
form's `admin-ajax` — `vacuumwars_send_message` is the only action string in it.
The dataset is only ever inlined, so the 2.6 MB page fetch is already the
cheapest read available. `robots.txt` disallows `/compare/` and then explicitly
re-allows `/compare/robot-vacuums/` — the one path read here is the one the site
opened on purpose. Since each fetch costs a full render, the domain is throttled
at 2s (`vacuumwars_limiter`); no block has ever been seen.

Never hardcode a total; the catalogue grows weekly and every figure above is a
dated measurement. Three hosts serve it, and **every structured read of the
tool is routed to `compare.vacuumwars.com`** — see `route_compare_url()`. That
host is the tool's own front end: uncached nginx, the array ~2 KB into the
document rather than ~285 KB, 370 KB smaller overall, and one hop fewer for
`robotvacs.com`. The WordPress page is a WP Engine/Cloudflare cached copy of
the same dataset. Never assert the two are equal; the invariant, and the one
the live gate holds, is **containment** — lean host superset of cached page.

Be careful about the **size** of that lag. It was measured once at three
records and had closed within hours, which is consistent with the plain 600s
edge cache. The weeks-stale observation is the *cordless* page, a sibling on
the same origin, not this one. So do not write "weeks" about the compare page
in docs or output. Routing is justified without it: it wins at zero lag on
bytes alone. `scripts/verify_vacuumwars.py` prints a dated lag line every run,
and those lines accumulate into the real answer.

Routing is a strict URL map, never a prefix: only the exact tool URLs move, so
`/page/2/`, the `-vs-` URLs, `/compare/` and `/embed/` keep their settled
behaviour, and `raw=true` fetches what was named. The routed read is disclosed
with a `[Fetched via: ...]` line, on a cache hit too. If the lean host fails,
or answers 200 without the array, fetchaller re-reads the URL the caller named
and **says so at the top of the output** — a silent fallback would hand back a
cached copy as though it were current, which is the substitution this module
exists to prevent. Errors after that report against the asked URL. Note the
lean host serves no `robots.txt` at all. `robotvacs.com` is the tool's **public name** — the site's
own article links there, so it is the URL a caller most likely arrives with. It
301s onto the compare path and extraction keys off the URL wafer ended on, so
keep it in `is_vacuumwars()` or the rate limiter, which keys off the URL as
asked for, never fires on it.

The `-vs-` head-to-head URLs and `compare.vacuumwars.com/embed/` answer **404
while serving the real template** and carry no dataset. `fetch_url` errors on
any status >= 400, so the caller sees `HTTP 404` instead of a board rendered
from an error page. Settled — do not re-open.

The comparison tool is **robot vacuums only** — `/compare/cordless-vacuums/` is
a hard 404. Cordless, upright and carpet-cleaner data is article prose and the
same `.vwx-` card widget, which renders fine; do not go looking for a dataset
that isn't there. Do not assume those pages carry tables either: the cordless
page went from score tables to cards between 2026-08-04 and 2026-09-06, and now
has zero `<table>` elements. Gate them on the cards surviving, never on a header
string the site owns.

**A capture of this site is not evidence of what it serves.** That page change
was already weeks old when a fetch on the morning of 2026-09-11 still returned
the old table layout, matching the Internet Archive's 2026-08-04 copy; the
current layout appeared only when the cache regenerated that afternoon. So a
page here can be served **weeks** stale, not the 600s its `Cache-Control`
implies, and it flips without warning. Date every capture, re-fetch before
concluding the site changed, and check the Internet Archive before blaming a
same-day edit. `compare.vacuumwars.com` is uncached and is the only reliably
current source.
Gate extraction on the `/compare/` path *and* the global being present, or a
review page that happens to carry it gets thrown away and re-rendered as a spec
table. A `/compare/` path with no readable dataset must say so — **but only when
the page really is the app**, detected by its own two root Alpine components
(`fetchData`, `infiniteScroll`) with the empty state as backstop. Do not match
on Alpine merely being present: most bindings on that page are generic
disclosure widgets, so the theme adopting Alpine for a menu would put the
warning on every article under `/compare/`.
`/compare/` itself is an ordinary WordPress article announcing the tool, and
warning that its dataset "could not be read" invents a failure on a page that
rendered perfectly: the same bug this module exists to fix, pointed the other
way.

Separately, the leaderboard card (`.vwx-pc`, on the Top 20 page and on every
review) renders each product **twice** — a collapsed row and the expanded panel
behind it — and both survive markdownify, so every model appeared three to four
times. Drop the collapsed `.vwx-row`; it carries nothing the panel lacks. Drop
`.vwx-chip-more` ("+2 more") **only** because the chips it reveals are already
in the DOM behind `nth-of-type` CSS — verify that before treating any other
"more" affordance as noise. fetchaller has NO credentialed path to vacuumwars.com.

### Costco (costco.com, costco.ca)

**Read the service from the page, never from a constant.** Both sites now serve
every keyword search and category page from Google Retail Search behind
`POST gdx-api.costco.com/catalog/search/api/v1/search`; the `search.costco.*`
Fusion endpoint this client was first built on is no longer called by the site.
It still answers keywords, so nothing failed loudly: it answers a keyword the
site redirects ("tv") with zero documents plus `fusion.redirect`, and category
pages had been sent to it as keywords. Both rendered "No products found", an
empty shelf that read as an answer. `costco/grs.py` takes the endpoint, its
required headers, the request template, the warehouse locator and the default
location from the page config every Costco page ships, and falls back to the
2026-10-05 values only when that config is unreadable (logged). Prices and
stock are per location: with none set the site uses `M4V 2H7`/ON (`98101`/WA
on .com) and default coordinates, resolved to the nearest **Warehouse** (not a
Business Center) and its delivery centres; the output names the warehouse. A
redirected keyword is followed from the catalogue's own `redirectUri`, with the
caller's page and sort, and disclosed. `HIDE_OUT_OF_STOCK` is sent because the
site sends it. Fusion survives only as a disclosed fallback for keyword
searches; a category has no fallback and errors instead.

### Facebook Marketplace

Search failed by rendering "No listings found", which reads as an empty market.
Every GraphQL call must carry the logged-out Comet form's `__a=1` and
`__comet_req=15`: without them the search query answers 200, no errors, a
cursor that counts its matches, and zero edges (geocode and listing detail
answer either way, so nothing else went quiet). When the cursor (`end_cursor` →
`c2c.it`/`b2c.it`) counts matches and no listing comes back, that is an error,
never an empty result. A search or city URL is read from **the page it names**:
its preloaded `CometMarketplaceSearchContentContainerQuery` already has the slug
resolved and the URL's filters applied, and its first page of results streams
with it. Rebuilding it by hand put `/marketplace/vancouver/` in Washington
(free-text geocode) and sent `minPrice=100` as one dollar — URL prices are whole
units. A city browse page streams a feed of ~26 listings, which is all the
logged-out feed serves; an empty-query search for the same city returns nothing.

### Job boards

Every job board ranks rather than filters: a title query returns adjacent roles
and a location query returns a radius. So the board's own filter is treated as
an optimisation and the client's filter as the guarantee — see
`src/fetchaller/jobfilter.py`, which every board client shares. Never report a
board's raw result count as if it were the filtered count, and always surface
how many postings a filter dropped rather than hiding the difference. Those are
two different pools and must never share a clause — `jobfilter.counts_line()`
renders both for every board, keeping `shown + dropped` reconcilable and giving
the board's own figure a separate, labelled sentence. `limit` sizes the output
and nothing else: the examined pool is a per-board `_EXAMINE_CEILING` constant,
never a multiple of `limit`, because deriving it from `limit` made the *answer*
depend on how many rows the caller asked for. Whatever a window could not
reach must be stated, never left implied. A tool's published
`maximum` must equal what the boundary validator accepts (`_TOOL_INTEGER_RANGES`
in `server.py`); advertising a bound and then rejecting it is worse than
publishing no bound. Workday's
`searchText` in particular silently drops real matches on some tenants, so a
located slice is pulled whole and filtered here instead. fetchaller has NO
credentialed path to any board — all of them answer anonymously and must
continue to.

**gojobs.gov.on.ca** is the limiting case of that doctrine, because the board
has **no keyword search at all** — its only text input is an exact Job ID, so
`title` is matched entirely here. It is ASP.NET WebForms, and every one of its
traps fails by rendering something plausible: a GET of `Search.aspx` returns the
search *form* with zero postings, so the generic fetch path shows an empty board
rather than an error; paging is a `__EVENTTARGET` postback signed by the
previous response's `__VIEWSTATE`, so pages are strictly sequential and ten rows
each; and a facet set to a bare `REGION-TRNT` is accepted and silently ignored
where `["REGION-TRNT"]` filters. Bound a result row by the **English** title
anchor, never the repeater id — a bilingual posting's second anchor carries the
same id and halves the row. `Search.aspx` serves **two different pages**: a session's
first GET carries all 984 cities, every GET after it silently omits the city
list while keeping every other facet, so the page looks complete. Re-fetching
the form per search therefore lost city resolution and quietly downgraded
"Toronto" to the region filter (board reported 35, not 32) and "Thunder Bay" to
no filter at all (127) — right postings, wrong reported scope. Read the
vocabulary once from the cold page and never cache a city-less one. A search is
also a *conversation* keyed to `PHPSESSID`, so keep the whole exchange behind
its lock; overlapping searches answer each other's state. Report `Competition Status` before `Posting status`,
because a filled competition still reads "Open". Treat "Ontario"/"Canada" as the
whole board, not a filter: no row names the province. Never use `/alljobs.aspx`
or `/ReadPDF.aspx` — `robots.txt` disallows both.

**jobbank.gc.ca** (federal Job Bank) has the same disease in two places: a
parameter that is accepted, ignored, and never complained about. A bare
`locationstring` returns the whole country (64,017) — the real filter is
`locationparam`/`mid` carrying the numeric `city_id` from the Solr autocomplete
at `/core/ta-cityprovsuggest_en/select`, so never send a location that could not
be resolved. `searchstring` is dropped for *some* terms: "driver" filters to 11
but "assistant" returned the unfiltered 424 byte-identical to no keyword, so
when a title is given, compare against the same location's keyword-less total
and suppress the board's count if they match. Read the result total from
`id="results-count"` only — every distance facet renders its own "N jobs found
in" badge and a text match returns the facet. Radius (`d`, default 50km) is a
real filter and is part of the answer: the board searches *near* a city, so
state the radius or nearby towns read as a broken location filter.

**emploisfp-psjobs.cfp-psc.gc.ca** (GC Jobs, the federal public service board)
is the board a plain fetch reads as *empty*: the search page is a JavaScript
shell over zero postings, and the listing is a second request that needs
**two** flags — `isSecondPartOfPage=1` alone is a "Lost Connection" page, and
`isInitialNetworkCheck=1` is what unlocks it. That second flag is also the
search: criteria go in the query under the form's input names (`title`,
`addedLocation=W232`, `department`, `jobSalaryRange`, `officialLanguage`), are
stored in the JSF session only when it is present, and are **reset to the
whole board** by any later request that carries it — so paging omits it and
walks the stored search, which makes a search a conversation and the exchange
is locked like gojobs. An earlier probe sent `wLocation=232` (a display-button
index from the page's script, not an input name), saw it silently ignored and
concluded the location filter could not be applied; the input name works. The
page echoes every criterion it applied as an `addTopSearchCritButton` call and
that echo, not the request, decides whether a filter is reported as applied.
Title is a **substring in the given word order** ("policy analyst" 3, "analyst
policy" 0), so the board gets four characters of the longest word and the
client filter is the guarantee. A third of postings are hosted on the
organization's own site and `page1800` serves a departure notice for them;
`get_gcjobs_job` returns the outbound URL rather than an empty record, prints
"Who can apply" first, and marks a past closing date "Closed" — the board
still serves a 2001 posting at `poster=1` as though live. An unknown id is a
plain 404. fetchaller has NO credentialed path to GC Jobs; the internal tab
(`tab=2`) is never requested.

**jobs.ashbyhq.com** board index: an org can switch off Ashby's **public
posting API** and keep its hosted board (EvenUp, 2026-09-11, 40 live reqs), and
the REST endpoint then answers 404 for every casing of the slug — exactly what
it answers for a slug that was never an Ashby org. Treating that 404 as "no
board" fell through to the SPA shell, which rendered `# EvenUp Jobs` as a
successful read of an empty board, and a board sweep missed the company
entirely. Never run a case-variant ladder on it. The hosted board's own
unauthenticated GraphQL (`jobBoardWithTeams`) is what settles the ambiguity:
null means no such org, anything else is the board. It carries no descriptions,
so the render says which path it came from. A first-party ATS 404 is ambiguous
on every board; only Ashby has been given the second read so far.

**Teamtailor** (`{slug}[.{region}].teamtailor.com`, or the company's own domain
recognised from the page) looks complete as HTML and is not: the list stops at
20 with no salaries. The board is `jobs.json` (salary band, dates) joined to
`jobs.rss` (department, location name, `remoteStatus`) on the posting UUID. The
trap is that the two feeds **page differently with a filter than without**:
unfiltered, JSON pages at 100 with `next_url` and RSS returns 100, ignores
`page`, and honours `per_page`; with any board filter both page at 20, emit no
`next_url`, and honour `page=N`. Trusting `next_url` there stops at 20 with no
sign, and trusting RSS's default stops at 100 — walk both to the end, and never
report a JSON posting missing from RSS as "no location", or an RSS posting
missing from JSON as "salary not published"; both are *unknown*. The JSON-LD
street address is the location record's address on file, usually an office, so
it is never the job's location. `remoteStatus: none` is Teamtailor's default,
not "on-site" (that is `onsite`): its filter labels it "No Remote Work", its
posting pages show nothing for it, and employers leave it on postings located
"United States - Remote". Keep the enum and gloss only `none`. The subdomain's
HTML 301s to the customer's domain; the feeds answer on the subdomain, and that
redirect is never followed from the interceptor, because the target is
customer-controlled and unvalidated.

### robots.txt — settled, do not re-open

**fetchaller is a user-directed fetcher, not a crawler, and robots.txt is not
treated as a constraint on it.** A person asks for a specific page or a specific
search; the tool retrieves it on their behalf. That is not the activity the
exclusion protocol was written to govern, and Avery has confirmed this position
more than once.

This was re-litigated once, mid-build, and the Indeed client was gutted over it
— `get_indeed_job` deleted, pagination and radius stripped — before being
restored. Do not do that again. Specifically, `ca.indeed.com/robots.txt`
disallows `/viewjob?`, `/*&start=` and `/*radius=` for `User-agent: *`, and the
client uses all three deliberately.

The obligation that *does* apply is load: **be efficient, because fewer
well-chosen requests is the fair thing and the polite thing at once.** Prefer
the request that returns the most per call. Indeed is the worked example, including how it
went: a probe found `start=1` returned a disjoint all-organic page (thirty
postings for two requests), and on re-test every offset returned the login wall,
so the client fetches one page. Re-verify a finding in the session you will ship
before building on it. Keep the per-domain limiter honest, never loop blindly, and
stop when the marginal request stops paying.

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

**The MCP server caches loaded module code.** Even with the local config, the running fetchaller process loaded `src/fetchaller/**/*.py` at Claude Code startup. New modules and edits do NOT take effect until you restart Claude Code (or otherwise restart the MCP server process). When live-testing changes inline, run them via `.venv/bin/python -c "..."` against the fresh source on disk to confirm the code is correct before restarting.

## Landing Page

`landing/` contains the static site deployed to fetchaller.com. Read `docs/design-style-guide.md` before any visual changes. Always invoke the `frontend-design` skill (`/frontend-design`) when making visual changes.

**`landing/llms.txt`** — LLM-readable project summary. **Keep this in sync when adding new tools, sites, or features.**

## Docs Reference

- `docs/architecture.md` — System design: fetchaller vs wafer boundary, content modules, search, HTTP transport
- `docs/site-apis.md` — Site-specific API clients: AliExpress MTop, Mouser/DigiKey, Kijiji GraphQL, Craigslist SAPI, Facebook Marketplace GraphQL, eBay search extraction, realtor.ca (api2 home search + SSR listings + `search_realtor` tool), aartech.ca (React listing API + embedded product blob; no prices in HTML), vacuumwars.com (robot-vacuum comparison tool: the full lab dataset inline as `window.vwProducts`, client-side pagination, tested vs listed-only, colour-variant collapsing), ui.com (UniFi store/techspecs `__NEXT_DATA__` spec tree, and installation guides rebuilt from their JS page assets), The Home Depot (homedepot.com via its federation-gateway GraphQL: products, search/category listings, pooled variant reviews, stores; homedepot.ca via `hdca-state` page data, the search API with its keyword redirects, and the localized price service that must follow a page), wellfound.com (Next.js/Apollo startup jobs). Job-board APIs and embed/white-label detection for Ashby, Greenhouse, Lever, Gem, Dayforce, Cornerstone, Workday, BambooHR, JazzHR, Teamtailor (JSON Feed + RSS joined on the posting UUID; filtered vs unfiltered paging). Big-tech career boards: Eightfold (Microsoft/Netflix/PayPal, two API generations), Workday search filtering, amazon.jobs (incl. inline pay bands), Apple SSR hydration, Meta persisted GraphQL, Uber. gojobs.gov.on.ca (Ontario Public Service: ASP.NET WebForms postback listing, JSON-array facets, no keyword search). jobbank.gc.ca (federal Job Bank: city_id-gated location, keyword silently dropped for some terms, radius search). emploisfp-psjobs.cfp-psc.gc.ca (GC Jobs: two-flag second-part listing, session-stored search and paging, criteria echo, external/legacy posting shapes). ca.indeed.com (embedded Mosaic job-card JSON and JobPosting JSON-LD, one stable anonymous result page).
- `docs/spa-discovery.md` — SPA API discovery (`src/fetchaller/discovery/`): observing a page in a browser and replaying what it made, so an endpoint's shape never needs bundle archaeology again. Ranking (why coverage and record count are directly opposed), the oracle (why a 200 that means "malformed" is the core problem), minimization, mint steps, and the measured per-board results
- `docs/testing.md` — Test organization, writing tests, live testing rules, test URLs
