# SPA API discovery: findings, and why this belongs in fetchaller

Response to `docs/wafer-request-spa-api-discovery.md`.

**Outcome:** the capability was built end to end inside wafer and validated live
against seven boards on 2026-08-01. It works. It is also almost entirely *not* a
bot-blocking problem, so the wafer implementation was **discarded** and this
document is the record. Everything needed to build it here is below, including
the parts that took several wrong turns to get right.

Every number below came from a real request against the live board, not from
reasoning.

---

## 1. Why this was discarded from wafer

wafer solves bot blocking: TLS fingerprinting, challenge detection and solving,
clearance cookies, retry, rate limiting. Deciding *which JSON payload on a page
is the job listing* is not that.

Three measurements settle it:

- Of the 1,181 lines the prototype needed, **39% was content analysis** -
  comparing payloads against rendered page text, counting records, telling a job
  listing apart from a branding blob, extracting search terms from URLs. None of
  it touches a WAF.
- **No new bypass code was written.** The browser-observation method called into
  wafer's existing machinery at two points (`_ensure_browser` /
  `_setup_headless_patches` to launch the hardened browser, and
  `_solve_challenge_in_place` for an interstitial). Both were copied from
  `render()`. Nothing was added to wafer's anti-detection surface.
- **None of the seven boards needed a challenge solved.** Meta, Apple, Workday,
  amazon.jobs, Eightfold/Netflix and Google all replayed over plain HTTP. Finding
  those endpoints was never a bot-blocking problem.

### The one caveat worth keeping in mind

Observation needs *a* browser. For these seven boards any browser works, because
none of them challenge. If you ever need to discover an endpoint on a board that
*is* protected (a Cloudflare or DataDome careers site), a plain Playwright will
be blocked, and that is the one case where the work has to run inside wafer's
browser. Route that case through `session.render()`-style access or ask for a
narrow `observe()` in wafer at that point. Do not pre-build it.

---

## 2. Capturing the traffic

fetchaller has to do this itself. The prototype used patchright through wafer's
solver, but the mechanics are plain Playwright.

Attach before navigating:

```python
page.on("response", record)
```

In `record(response)`:

- `request = response.request`. Skip `request.resource_type` in
  `{image, stylesheet, font, media, manifest, script, texttrack, websocket,
  eventsource, ping, csp_violation, preflight}`. Everything else is a candidate.
- Request headers: `request.all_headers()`, falling back to `request.headers`.
  Lower-case the keys.
- Request body: `request.post_data_buffer` (bytes), falling back to
  `request.post_data` (str). Both can raise; guard each.
- Response body: `response.body()`. Raises on redirects and on transfers Chrome
  never buffered -treat a failure as empty rather than dropping the exchange,
  because a partial record is still useful for token provenance.
- Response headers: `response.all_headers()`. **Strip `content-encoding`,
  `content-length` and `transfer-encoding`** -`response.body()` returns the
  decoded bytes, so those headers describe different bytes.
- Record a monotonic `order` and a `phase` (`"load"` / `"nudge"`). Provenance
  needs the ordering; ranking benefits from the phase.

Cap the capture: 4 MiB per body and ~400 exchanges, and log when either is hit.
A media-heavy page will otherwise hold the whole transfer in memory.

**Skipping `script` is a deliberate trade.** It keeps the capture small and
scannable, at the cost of not being able to trace a token that only exists in a
JS chunk. Meta's `doc_id` is exactly that case; see section 10.

### Settling

Three steps, each bounded, each falling through rather than failing:

1. `page.goto(url, wait_until="domcontentloaded")`.
2. `page.wait_for_load_state("networkidle")`, bounded to about half the
   remaining budget. Analytics beacons and long polls mean some pages never go
   idle.
3. Poll `document.documentElement.outerHTML.length` every 0.25s and stop after
   3 consecutive equal readings, capped at ~10s total. A page with a running
   animation never looks stable, so the cap matters.

Then take `page.content()` as the settled DOM and `context.cookies()`.

### The nudge, and why it is mandatory

A board that server-renders its first page issues **no XHR at all** on load.
Apple and Google both do this. Navigating to a search URL and waiting produces
nothing; the data request only appears when the front end asks for page two.

Perform at most **one** interaction: a single click on a conventionally labelled
pagination control. These are accessibility and HTML conventions, not site
knowledge. Try in order, take the first that is present, visible **and enabled**:

```
[rel~=next]
[aria-label*='next page' i]
[aria-label*='next result' i]
[data-testid*='next' i]
[aria-label*='next' i]
button[class*='next' i]
a[class*='next' i]
```

Check `is_enabled()` as well as `is_visible()`: a disabled next control is the
single-page case, and clicking it wastes the budget. If the click throws, move
to the next selector. After a successful click, re-settle for ~8s.

**Gate it on same-host, not on "did anything rank".** Nudge unless the load
phase produced an `xhr`/`fetch` candidate on the page's **own host**. Apple's
page issues no XHR of its own but pulls a global-header payload from
`www.apple.com` while the page is on `jobs.apple.com`. That payload scores
**9.95** under the ranking in section 3 -more than enough to suppress a naive
gate and leave `POST /api/v1/search` undiscovered forever. Erring toward nudging
costs a few seconds; erring away returns nothing.

---

## 3. Ranking: which exchange is the data

The hardest part, and the one that took the most iterations. Two plausible
signals disagree, and **neither is correct alone**.

**Signal A, how much of the payload is on screen.** Puts the chrome first. A
page renders every string of its branding and configuration blobs but only the
visible page of its listing. Measured on Netflix's Eightfold board:

| payload | head-window coverage | score | records |
|---|---|---|---|
| `/api/apply/v2/branding` | 1.00 (31 of 31 values) | 18.15 | 4 |
| `/api/apply/v2/jobs/{id}/jobs` | 0.29 (12 of 41) | 8.94 | 10 |

The branding blob wins by 2x. Its 31 values are `Netflix Jobs Home`, `CAREERS`,
`LOCATIONS`, `CULTURE MEMO` -pure navigation.

**Signal B, largest record set.** Puts a lookup table first. Workday serves
`/wday/cxs/{tenant}/videoplayerlabels`: 334 records shaped
`{"key": "WDRES.BUTTON.Close", "label": "Close"}`. Structurally identical to job
postings, and five times more numerous than the 70 in `/jobs`.

No weighting fixes both. For a linear combination to pick the listing on Netflix
it needs weight > 27 on the record term; to pick `/jobs` on Workday it needs
weight < 21.6. The cases are directly opposed.

**What resolves it: chrome does not depend on the query.** The page URL says
what was searched. A payload that mentions it is the one that answered the
search. Branding never mentions "engineer"; the listing does.

### Selection rule

```
hints = [expect] if caller supplied one else query_hints(final_page_url)
on_subject = {exchanges whose decoded body contains any hint, case-insensitive}

if on_subject:
    order by (not same_registrable_domain, not on_subject, -collection_size, -score)
else:
    order by (not same_registrable_domain, -score)
```

`query_hints(url)` takes query-string **values** that read as words: length 3 to
60, matching `[A-Za-z][A-Za-z0-9 ,._'+-]*`, excluding
`{true, false, asc, desc, relevant, recent, date, all, any, none, null}`.
Analytics blobs like Google's `_gl=1*16occzp*_up*MQ..` fail the pattern and are
ignored.

Verified against every board: Meta picks the 588-record results query, Netflix
picks the listing over both the branding blob and the single-posting detail
endpoint beside it, Google picks `batchexecute`, and Workday -whose board URL
carries no query at all, so there are no hints -falls through to score and picks
`/jobs`.

Same registrable domain is the outermost key throughout, because a consent or
analytics vendor can ship a larger array than the board does. Netflix's
cookielaw payload has 200 records.

### Scoring (used when there are no hints, and to break ties)

Exclude any exchange whose `resource_type` is outside
`{xhr, fetch, document, other}`, whose status is not 2xx, or whose body is
empty. Then:

```
score  = 100                      if `expect` was supplied and matched (else exclude entirely)
      +  12 * coverage * confidence
      +   4 * dom_token_overlap
      +   3                       if content type contains json/javascript/text-plain/ndjson/xml
      +   2                       if method != GET
      +  min(2, log10(len(body)+1) / 3)
      +  min(3, log10(records+1))
      -   5                       if different host AND zero token overlap
      -   4                       if it is the page document itself and not a data type
exclude if score <= 0
```

`coverage` is the fraction of the payload's **first 40** distinctive string
values that appear in the page's visible text. `confidence = min(1, values / 8)`
so a payload offering two strings cannot score full marks on both appearing.

`dom_token_overlap` is the weaker reverse signal: rare long words sampled from
the rendered text that appear in the payload. Keep the weight low. On Meta it
sampled `pregnancy-related`, `accommodations`, `discriminate` -all footer
boilerplate, none of it in any payload.

**The head window is essential.** A listing renders the top of its list and
virtualizes the rest. Measured on Meta: of 466 distinct job titles in the
payload, **14 were on the page** (3%), but 62% of the first forty values were.
Coverage over the whole payload understates the very payload you are seeking.

Distinctive values are string leaves of length 6 to 120 not starting with
`http://`, `https://`, `/` or `data:`.

### Refusing to answer

Ranking must be allowed to reject everything. Require either a caller-supplied
`expect` that matched, or a payload that parses with `records > 0` or has >= 5
distinctive values.

Uber's board fails this, correctly. `jobs.uber.com` is server-rendered Next.js:
results are in the 89 KB document and the only XHRs are RSC flight responses
(`text/x-component`, not JSON). There is no client-side data endpoint -Oracle
Recruiting is called server-side. Reporting "no data request" is the useful
answer, and far better than returning a plan that fetches a page and calls it an
API.

---

## 4. The oracle: verifying a replay

The browser's own response for the same request is ground truth, so a replay can
be checked rather than believed. This is what separates "there is no data" from
"your request was malformed", which these APIs report as `HTTP 200`.

Equality is useless -timestamps, request ids and ordering all vary. Compare a
signature of `(shape, records, collections, length, parsed, values)`:

- **shape**: the set of dotted key paths, array indices collapsed to `[]`,
  recursion capped at depth 12. A 20-record and a 200-record response of the
  same kind share a shape.
- **records**: the largest list length anywhere.
- **values**: a sample of the payload's own distinctive string leaves.

A candidate matches when all hold:

1. `parsed` state unchanged.
2. Shape Jaccard overlap >= **0.85**, or the observed shape is a subset of the
   candidate's.
3. If observed records > 0: candidate records within **[0.5x, 2.0x]**.
   If observed records == 0: candidate records == 0.
4. At least **50%** of the observed payload's sampled values came back.
5. If neither parsed: candidate length within [0.5x, 2.0x] and non-empty.

**Rule 3's upper bound and rule 4 are both load-bearing.** Each was added after
the minimizer silently broke a plan. See traps 7 and 8.

### `collection_size`, not raw record count

A record set is a list whose entries **share structure**:

- all entries are dicts, and the intersection of the first 8 entries' key sets
  has >= 2 keys, or
- all entries are lists of the same length >= 3 (a positional protocol)

This correctly excludes Amazon's facet arrays, which are dicts with one key each
and *different* keys (`[{"job_function_corporate_80rdb4": 6286}, ...]`), and
correctly includes Google's 21-slot job arrays.

It does **not** exclude Workday's video-player labels, which really are 334
homogeneous records. Only the query hint separates those.

### Decoding payloads

Four shapes beyond plain JSON, or real boards read as zero records:

- **Anti-hijacking guards.** Strip a leading `)]}'`, `)]}`, `while(1);`,
  `for (;;);` or `for(;;);`. Google uses `)]}'`.
- **Newline-delimited JSON**, first non-empty line is the payload. Meta's
  GraphQL responses are NDJSON.
- **Length-prefixed chunk streams.** Google's `batchexecute` emits a bare
  number, then a JSON array, repeatedly. Parse line by line and take the first
  that decodes to a **container**, not a scalar -otherwise you decode the chunk
  length `1234` and conclude there are no records.
- **JSON nested inside a string value.** Positional RPCs put the real payload in
  a string field. Follow any string starting with `[` or `{` that parses.
  Without this, Google's 110 KB of job data reads as 3 records instead of 21.

---

## 5. Minimization

Delta debugging over **one combined mapping** of headers, query parameters and
body fields, namespaced `header:`, `query:`, `field:`.

Minimizing all three together matters. An endpoint may demand an `Origin` it
never validates. A POST's build ids live in its query string, not its body: on
Google, minimization drops
`bl=boq_corp-hiring-boq-cportal-frontend_20260728.05_p0`, `f.sid` and `_reqid`,
leaving one field. Those are exactly the values that make a pinned request rot
at the next deploy.

- Probe the full mapping first. **If the full set already fails, return it
  unchanged with nothing dropped** and mark the plan unverified. You cannot
  minimize what does not work, and shipping a shrunken broken request is worse
  than reporting failure.
- Standard ddmin from there: partition into n chunks starting at n=2, try each
  chunk alone, then each complement, doubling n on no progress.
- Memoize on the `frozenset` of candidate keys. Memoized hits must not count
  against the budget.
- Bound with `max_probes` (48 is reasonable; every probe is a live request). On
  exhaustion return the smallest passing subset found so far.
- A `raw` body yields no fields and is left whole. **This is the right answer
  for a positional protocol**: Google addresses arguments by index, so dropping
  a slot shifts every argument after it. Classify a JSON *array* body as raw for
  exactly this reason.

---

## 6. Tokens: verbatim first, then provenance

**Try verbatim first.** A captured request whose tokens are still accepted needs
no minting machinery at all, and the simplest plan that works is the one most
likely to keep working. Measured: Meta's `lsd` and Apple's `x-apple-csrf-token`
are both still accepted stale, from a fresh session with no cookies.

If verbatim fails, trace each volatile value (a `str` of length >= 12) back to
the exchange that minted it, searching only exchanges with `order <` the
target's:

1. An earlier response **header** whose value equals it exactly. Unambiguous.
2. An earlier response **body** containing it.
3. The settled page HTML.

For 2 and 3, build an anchored regex: `re.escape(prefix) + capture`, where
prefix is up to 48 characters immediately preceding the value, and capture is
`([A-Za-z0-9_-]{n,m})` when the value is that character class, else
`([^"'<>\s]{n,m})`, with `n = max(8, len // 2)` and `m = len * 3`.

**Refuse to build a pattern with under 8 characters of context.** An unanchored
pattern degenerates into "any run of token characters" and re-mints the first
random string in the document.

**Deduplicate identical sources.** A CSRF value often feeds both a header and
the body. They must share one mint step, or every replay refetches the same page
once per use and the two copies can disagree.

### Hardening

After minimization, try swapping surviving values for mint steps *even when
verbatim worked*, keeping the swap only if the answer is unchanged. A value the
plan can re-mint is more durable than a literal that happens to still work
today, and a CSRF token is exactly the kind of value that stops working later
for no visible reason.

On Meta this converts `lsd` into a re-mint of `["LSD",[],{"token":"…"}]` from
the page -**the same regex `meta_careers/api.py:get_lsd` already uses, derived
automatically**.

---

## 7. The plan model

Must be JSON round-trippable exactly; caching is the whole point.

```python
@dataclass
class RequestPlan:
    method: str
    url: str                     # query values may contain {{mint:NAME}}
    headers: dict                # delta only; values may contain markers
    body: str | None             # serialized; may contain markers
    body_kind: str | None        # "json" | "form" | "raw" | None
    mint: tuple[MintStep, ...]
    verified: bool
    required_fields: tuple[str, ...]   # namespaced survivors
    dropped_fields: tuple[str, ...]    # surface these: they are the parameters
                                       # the endpoint accepts
    record_count: int            # what the verified plan returned
    notes: tuple[str, ...]

@dataclass(frozen=True)
class MintStep:
    name: str
    method: str
    url: str
    source: str                  # "header" | "regex"
    selector: str                # header name, or pattern whose group(1) is the value
```

`record_count` is what makes decay detectable at runtime: a later replay
returning far fewer records means the plan rotted, not that the board is empty.
**Measured on Meta:** the healthy plan returns 128,515 bytes and 588 records;
incrementing `doc_id` by one returns `HTTP 200`, 141 bytes, 1 record. Trivially
distinguishable.

---

## 8. Traps. Every one of these cost real time

**1. `data=` is not a wafer kwarg.** wafer takes `json=`, `form=`, `body=`,
`multipart=`. `data=` (the requests/httpx spelling) falls through to wreq, which
silently ignores unknown kwargs, so the request goes out with **no body at all**
and the origin answers with an ordinary-looking 400. This produced two
confidently wrong conclusions before it was caught. When a replay 4xxs and the
shape looks right, verify the body actually went out before theorising about
tokens, cookies or fingerprints.

**2. Percent encoding defeats textual substitution.** A `{{mint:X}}` marker
inside a form body is stored as `%7B%7Bmint%3AX%7D%7D`. Substituting into the
serialized string finds nothing, and the marker itself goes out as the token.
**Always decode -> substitute -> re-encode, in that order.** This bit twice:
once in the replay path, and again in the *unresolved-marker check*, which
scanned the already-encoded body and reported everything resolved.

**3. Verify the serialized plan, not the in-memory one.** Everything upstream
verifies a request as dicts. What ships is the serialized plan, and trap 2 means
those can differ. Run the finished plan through **exactly** the code path replay
uses, including re-minting, before setting `verified = True`.

**4. Repeated query parameters collapse.** `dict(parse_qsl(...))` keeps only the
last value. Amazon's search route sends `facets[]` **twelve times**. Group
repeats into a list and encode with `urlencode(..., doseq=True)`.

**5. `form=` flattens repeats.** Because of trap 4, encode form bodies yourself
and set `Content-Type: application/x-www-form-urlencoded` explicitly. Do not
send a captured `content-type` alongside `json=`, which wafer sets itself -that
duplicates the header, and HTTP/2 treats a duplicate as a protocol error rather
than a last-wins overwrite.

**6. Headers must be a delta.** Strip what wafer's transport sets, or you
duplicate under HTTP/2: `accept-encoding`, `accept-language`, `connection`,
`content-length`, `cookie`, `host`, `keep-alive`, `proxy-connection`, `te`,
`trailer`, `transfer-encoding`, `upgrade`, `user-agent`, plus anything starting
with `sec-ch-`, `sec-fetch-` or `:`.

**7. The oracle needs an upper record bound.** Without it, minimization drops a
filter, gets the unfiltered listing back, sees the same shape and *more*
records, and calls it a match. The cached plan then silently searches
everything.

**8. The upper bound is not enough on its own.** Where the page size caps the
result, dropping the query does not move the record count at all: Apple answers
a search for "engineer" and a search for nothing with the same twenty rows. Only
comparing the payload's **content** catches it. Without the 50% value-overlap
rule, minimization dropped `query: "engineer"` from Apple's plan and reported
success.

**9. `EmptyResponse` on header-only endpoints.** A token endpoint can
legitimately answer `200` with an empty body and the value in a header (Apple's
`GET /api/v1/CSRFToken`). wafer's empty-200 guard raises. Catch `EmptyResponse`
and read `exc.response.headers`.

**10. Do not treat `resource_type == "document"` as page furniture.** An SSR
board can answer the navigation with the payload itself. Penalize it, do not
exclude it.

---

## 9. Measured results, per board

One discovery pass each, then the cached plan replayed **in a fresh process with
no browser**.

| Board | Endpoint discovered | Kept/total | Cold replay |
|---|---|---|---|
| Meta | `POST /graphql` | 6/34 | 200, 588 records |
| Apple | `POST /api/v1/search` (needed the nudge) | 4/14 | 200, 20 records |
| Workday (Adobe) | `POST /wday/cxs/{t}/{s}/jobs` | 0/9 | 200, 70 records |
| amazon.jobs | `GET /en/search.json` | 2/17 | 200, 602 records |
| Eightfold (Netflix) | `GET /api/apply/v2/jobs/{id}/jobs` | 0/6 | 200, 10 records |
| Google | `POST …/data/batchexecute` (needed the nudge) | 1/16 | 200, 21 records |
| Uber | none. Board is server-rendered | - | reported honestly |

### Meta

One page load recovered **all four** `doc_id`s, not just the search one:

| operation | doc_id |
|---|---|
| `CareersJobSearchResultsV2DataQuery` | 27129360303422352 |
| `CareersJobSearchFiltersV3Query` | 25103492705924273 |
| `CareersJobSearchLocationFilterV3Query` | 24867916029505828 |
| `CareersJobSearchHideFiltersBarV2Query` | 26210170368675892 |

plus the `lsd` token and the complete `search_input` key set. That is the whole
of `_KNOWN_DOC_IDS` and everything `discover_doc_id` currently reads bundles
for, from one navigation.

The minimized plan keeps `av`, `doc_id`, `jazoest`, `lsd`, `variables`,
`x-fb-friendly-name`, `x-fb-lsd`, and drops all **nineteen** `__`-prefixed
fields (`__csr`, `__hsdp`, `__dyn`, `__rev`, `__spin_*` …). Those encode build
state, so dropping them is what lets the plan survive a deploy.

`doc_id` is **not** in the page HTML -it lives in a JS bundle, which observation
deliberately does not capture. It stays a literal. When Meta rotates it the plan
stops verifying and re-running discovery yields the new one. That is the
intended cycle, and cheaper than the current bundle scan.

Verbatim replay of the captured request works: `200`, 128,606 bytes, byte
identical to what the browser received, from a fresh session with no cookies and
a stale `lsd`.

### Apple

The search page is SSR on first load and issues **no XHR at all**. Navigation
alone finds nothing. One click on `[aria-label*='next page' i]` produced:

```
POST https://jobs.apple.com/api/v1/search
{"query":"engineer","filters":{},"page":2,"locale":"en-ca","sort":"",
 "format":{"longDate":"MMMM D, YYYY","mediumDate":"MMM D, YYYY"}}
```

Confirming `format` mechanically, with no CSRF header and no prior page visit:

```
as captured      status=200 totalRecords=5258 n=20
format dropped   status=200 totalRecords=0    n=0
format: {}       status=200 totalRecords=5258 n=20
```

**Correction to a claim made earlier in the session:** I reported that Apple now
requires `x-apple-csrf-token` and that `docs/site-apis.md` was stale. That was
wrong. It came from reading `totalRecords` at the top level when it nests under
`res`. The API is anonymous exactly as documented. `GET /api/v1/CSRFToken` does
fire in the browser, but the endpoint answers fine without it.

### Workday

`POST /wday/cxs/adobe/external_experienced/jobs` minimizes to an **empty body**
`{}`. The observed `{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}`
is all defaults. Independently confirms `x-calypso-csrf-token` is not required.
`limit` / `offset` / `appliedFacets` land in `dropped_fields`, which is where a
caller learns the endpoint accepts them.

### amazon.jobs

Kept `base_query` and all twelve `facets[]` repeats; dropped `loc_query`.
Independently reproduces the documented trap:

```
base_query=engineer only    hits=6640
+ loc_query=Toronto         hits=6640
```

`loc_query` does not filter. Note also that `base_query=engineer` returns 6640
of a ~6641 board, so Amazon ranks rather than filters on that route too.
Client-side filtering stays necessary regardless.

### Google

The results page is a 1.25 MB SSR document with no data XHR on load. After the
nudge:

```
POST https://www.google.com/about/careers/applications/_/HiringCportalFrontendUi/data/batchexecute
f.req=[[["r06xKb","[[\"engineer\",null,null,null,\"en-US\",null,null,2]]",null,"3"]]]
```

Minimized to that **single field**. The positional array is fully exposed:
search term in slot 0, locale in slot 4, page in slot 7. Dropped `bl` (the build
id), `f.sid`, `_reqid`, `rpcids`, `source-path`, `hl`, `soc-*`, `rt`, and every
header. Response is `)]}'`-guarded and length-prefixed; the job records are
21-slot arrays nested inside a JSON string.

This is the case the request document describes as needing every index pinned by
a test. One page load plus one click produced it, and dropped the build id so it
survives the next deploy.

### Uber

Correctly reports no plan. `jobs.uber.com` is server-rendered Next.js; Oracle
Recruiting is called server-side and a browser never sees it. With an explicit
`expect`, discovery does surface the 190 KB RSC flight response, but that is a
route payload rather than a search API, and `oracle_recruiting/` remains the
right client.

---

## 10. Limits, stated honestly

- **A filter the board itself ignores cannot be detected.** The browser and the
  replay both get the unfiltered answer, so they agree. Amazon's `loc_query`,
  Oracle's city-level `location` and Workday's `searchText` all fall here.
  `jobfilter.py` remains the guarantee. Discovery narrows what must be
  re-filtered; it does not remove the need.
- **Ranking without a hint is a heuristic.** With no `expect` and no query in
  the page URL it falls back to the blended score. Supplying `expect` is
  decisive and cheap -you almost always know a string the page displays.
- **Script bundles are not observed**, so a bundle-only constant stays a literal
  and rotates the plan into failure rather than self-healing. Meta's `doc_id` is
  the live example. Re-running discovery is the recovery path.
- **Cost.** 5 to 40 live requests plus a browser launch, tens of seconds. Run it
  on failure, cache the result, never on a search.
- **Nothing here is blocked.** All the boards have working clients, and Meta's
  rotating `doc_id` is already verified self-healing via bundle rediscovery. So
  discovery does not fix a break in the current set; it replaces a per-site
  mechanism with a general one. Judge it on that, not on urgency. The honest
  case for building it is the *next* board, where the alternative is another
  round of bundle archaeology.
- The general mechanism is also strictly weaker than the bundle scan in one
  respect: script bundles are not observed, so a bundle-only constant like
  `doc_id` cannot be re-derived from traffic at all. Discovery recovers it once,
  at capture time, and then it decays. `discover_doc_id` does not.

---

## Appendix: reference implementations

The wafer prototype was discarded, so the non-obvious pieces are reproduced here.
These are the ones that were tuned against live behaviour; the rest is ordinary
plumbing.

### Payload decoding

```python
def strip_json_guard(text):
    stripped = text.lstrip()
    for guard in (")]}'", ")]}", "while(1);", "for (;;);", "for(;;);"):
        if stripped.startswith(guard):
            return stripped[len(guard):].lstrip(",\n\r \t")
    return text


def decode_payload(text):
    """Plain JSON, guarded JSON, NDJSON, or a length-prefixed chunk stream."""
    body = strip_json_guard(text)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        pass
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        # Skip the bare chunk lengths batchexecute interleaves.
        if isinstance(decoded, (dict, list)):
            return decoded
    return None


def nested(value):
    """A string that is itself a JSON document, else None."""
    if not isinstance(value, str):
        return None
    candidate = value.lstrip()
    if not candidate.startswith(("[", "{")) or len(candidate) < 8:
        return None
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, (dict, list)) else None
```

### Structure metrics

```python
def json_shape(value, prefix="", depth=0):
    if depth > 12:
        return frozenset()
    if isinstance(value, dict):
        paths = set()
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths |= json_shape(child, child_prefix, depth + 1)
        return frozenset(paths)
    if isinstance(value, list):
        if not value:
            return frozenset()
        return json_shape(value[0], prefix + "[]", depth + 1)
    return frozenset({prefix})


def collection_size(value, depth=0):
    """Largest list of entries that share structure. Follows nested JSON."""
    if depth > 12:
        return 0
    best = 0
    if isinstance(value, dict):
        for child in value.values():
            best = max(best, collection_size(child, depth + 1))
        return best
    if isinstance(value, list):
        for child in value:
            best = max(best, collection_size(child, depth + 1))
        if is_record_list(value):
            best = max(best, len(value))
        return best
    inner = nested(value)
    return collection_size(inner, depth + 1) if inner is not None else 0


def is_record_list(items):
    if len(items) < 2:
        return False
    sample = [i for i in items[:8] if isinstance(i, (dict, list))]
    if len(sample) < 2:
        return False
    if all(isinstance(i, dict) for i in sample):
        shared = set(sample[0])
        for item in sample[1:]:
            shared &= set(item)
        return len(shared) >= 2          # excludes Amazon's 1-key facet dicts
    if all(isinstance(i, list) for i in sample):
        widths = {len(i) for i in sample}
        return len(widths) == 1 and next(iter(widths)) >= 3   # positional records
    return False
```

### The oracle

```python
def signatures_match(observed, candidate, *, min_shape_overlap=0.85,
                     min_content_overlap=0.5):
    if observed.parsed != candidate.parsed:
        return False

    if observed.parsed:
        union = observed.shape | candidate.shape
        overlap = len(observed.shape & candidate.shape) / len(union) if union else 1.0
        if not (overlap >= min_shape_overlap
                or (observed.shape and observed.shape <= candidate.shape)):
            return False

        if observed.records > 0:
            floor = max(1, observed.records * 0.5)
            if candidate.records < floor:
                return False                     # the silent-empty trap
            if candidate.records > max(floor, observed.records * 2.0):
                return False                     # a filter stopped applying
        elif candidate.records != 0:
            return False

        # Same shape, same size, different subject. Catches a dropped query
        # where the page size hides it from the record count.
        if observed.values:
            retained = len(observed.values & candidate.values) / len(observed.values)
            if retained < min_content_overlap:
                return False
        return True

    if candidate.length <= 0:
        return False
    return observed.length * 0.5 <= candidate.length <= observed.length * 2.0
```

### Delta debugging

```python
def ddmin(fields, probe, *, required=(), max_probes=48):
    """Minimal subset for which probe() still passes. Returns (kept, dropped, probes)."""
    ordered = list(fields)
    required_names = {n for n in required if n in fields}
    removable = [n for n in ordered if n not in required_names]
    cache, probes = {}, 0

    def candidate(names):
        included = required_names | set(names)
        return {n: fields[n] for n in ordered if n in included}

    def test(names):
        nonlocal probes
        current = candidate(names)
        key = frozenset(current)
        if key in cache:
            return cache[key]                    # memo hits are free
        if probes >= max_probes:
            return None
        cache[key] = result = bool(probe(current))
        probes += 1
        return result

    # Cannot minimize what does not work: report it whole.
    if test(removable) is not True:
        return dict(fields), (), probes

    current, n = removable, min(2, len(removable))
    while current:
        chunks = partition(current, n)
        progressed = False
        for chunk in chunks:                     # try each chunk alone
            if len(chunk) == len(current):
                continue
            result = test(chunk)
            if result is None:
                return finish(fields, required_names, current, probes)
            if result:
                current, n, progressed = chunk, min(max(2, n - 1), len(chunk)), True
                break
        if progressed:
            continue
        for chunk in chunks:                     # then each complement
            names = set(chunk)
            complement = [n_ for n_ in current if n_ not in names]
            result = test(complement)
            if result is None:
                return finish(fields, required_names, current, probes)
            if result:
                current, n, progressed = complement, min(max(2, n - 1), len(complement)), True
                break
        if progressed:
            continue
        if n >= len(current):
            break
        n = min(len(current), n * 2)
    return finish(fields, required_names, current, probes)
```

### Token provenance

```python
def anchored_pattern(text, value):
    position = text.find(value)
    if position < 0:
        return None
    prefix = text[max(0, position - 48):position]
    # Without real context the pattern matches any run of token characters and
    # would re-mint the first random string in the document.
    if len(prefix) < 8:
        return None
    low = max(8, len(value) // 2)
    high = max(low, len(value) * 3)
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        capture = r"([A-Za-z0-9_-]{%d,%d})" % (low, high)
    else:
        capture = r"([^\"'<>\s]{%d,%d})" % (low, high)
    return re.escape(prefix) + capture
```

Search order for a value (only exchanges with `order <` the target's): response
headers by exact equality first, then response bodies, then the settled page
HTML. Deduplicate steps sharing `(method, url, source, selector)`.

### Repeated parameters

```python
def pairs_to_fields(pairs):
    """Group query/form pairs, keeping repeats as a list."""
    fields = {}
    for name, value in pairs:
        if name in fields:
            existing = fields[name]
            fields[name] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            fields[name] = value
    return fields


def encode_fields(fields):
    encodable = {
        k: [str(i) for i in v] if isinstance(v, list) else str(v)
        for k, v in fields.items()
    }
    return urlencode(encodable, doseq=True)
```

### Resolving a stored plan

Order matters. Decode, substitute, then re-encode, and measure unresolved
markers on the **decoded** values.

```python
def resolve_plan(plan, minted):
    headers = substitute(dict(plan.headers), minted)
    url, send, pending = plan.url, {}, set()

    if plan.body_kind == "json" and plan.body is not None:
        send["json"] = substitute(json.loads(plan.body), minted)
        pending |= unresolved_markers(send["json"])
    elif plan.body_kind == "form" and plan.body is not None:
        fields = substitute(pairs_to_fields(parse_qsl(plan.body, keep_blank_values=True)), minted)
        pending |= unresolved_markers(fields)          # BEFORE encoding
        send["body"] = encode_fields(fields)
        headers["content-type"] = "application/x-www-form-urlencoded"
    elif plan.body is not None:
        send["body"] = substitute(plan.body, minted)
        pending |= unresolved_markers(send["body"])

    parts = urlsplit(url)
    if parts.query:
        query = substitute(pairs_to_fields(parse_qsl(parts.query, keep_blank_values=True)), minted)
        pending |= unresolved_markers(query)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, encode_fields(query), parts.fragment))
    else:
        url = substitute(url, minted)

    pending |= unresolved_markers([url, headers])
    return url, headers, send, pending
```

Raise rather than send when `pending` is non-empty. A marker that reaches the
origin comes back as an ordinary-looking rejection, which is precisely the
confusion this whole exercise exists to remove.
