# SPA API discovery

`src/fetchaller/discovery/` turns a page URL into a **cacheable plain-HTTP
request** that returns the page's data, without anyone reading minified
JavaScript.

Six of this repo's ten job-board clients originally needed JS bundles read by
hand to find the request their front end makes, and each would need it redone
when the site next ships. This package replaces that archaeology with an
observation pass.

## Where the boundary is

**Escalate to wafer only when something is actively blocked** — bot detection, a
WAF challenge, TLS rejection, clearance cookies. *Finding* the right request is
not that, and belongs here.

Settled empirically: the capability was first built inside wafer, validated
against seven boards, then discarded — 39% of it was content analysis, it added
**zero** bypass code, and none of the seven needed a challenge solved.
`wafer-feedback.md` is the record.

**The trap on this boundary is attribution, not detection.** Discovery runs a
browser; a browser can be refused for reasons that have nothing to do with the
board. Three separate investigations of Uber concluded the board was at fault —
"server-rendered", "decoder-induced", "challenge-protected, send it to wafer" —
and all three were wrong. So a browser-side refusal is never routed anywhere
until it has been **replayed over plain HTTP**: see
`pipeline._diagnose_refusals`. One request, decisive.

## Usage

```python
from fetchaller.discovery import discover, execute, replay, resolve_plan_for

result = await discover("https://jobs.apple.com/en-ca/search?search=engineer")
if result.ok:
    cached = result.plan.to_json()      # replay later, no browser
else:
    print(result.reason)                # says what happened, not just "nothing"

# Cached: discovers once, replays thereafter.
plan = await resolve_plan_for("apple:search", url, expect="Software Engineer")
response = await execute(session, plan)

# Full cycle: replay, detect decay, re-derive once, or say so honestly.
out = await replay(session, "meta:search", url)
if out.ok:
    use(out.response)                   # out.rediscovered: did it self-heal?
else:
    log(out.reason)
```

`expect` is a string the page displays. Supplying it is decisive and cheap —
ranking without a hint is a heuristic.

**Cost:** 5–40 live requests plus a browser launch, tens of seconds. Run it on
failure, cache the result, **never on a search**. Requires
**wafer-py[browser] >= 0.4.8**.

## The browser must not announce itself

This is the single highest-value thing in the package, because getting it wrong
does not fail loudly — it returns *degraded answers that read as facts about the
board*.

`_open()` originally launched with a bare `headless=True` and no
`ignore_default_args`, so Patchright's `--headless` and `--enable-automation`
survived and the browser advertised `HeadlessChrome/147.0.7727.15`. Meta
rate-limited it. Cloudflare challenged Uber's prefetches. Both degradations were
then written down as board behaviour:

| what was concluded | what is true |
|---|---|
| "Meta sometimes server-renders" | It never does — 0 of the first 25 GraphQL titles appear in the 461,620-byte document. |
| "Meta issues no search query" | It does, with the right `doc_id` and byte-identical variables. The reply was `HTTP 200` + `{"errors":[{"message":"Rate limit exceeded"}]}` in 114 bytes. |
| "Uber is challenge-protected" | Plain wafer: `200`, 383,021 bytes, 66 Flight rows, zero rotations. |

The fix **consumes wafer's configuration** rather than copying a flag list, so a
Chrome bump on wafer's side reaches discovery:

```python
config = hardened_launch_config(headless=headless)
launch = {"headless": headless, "args": list(config.args),
          "ignore_default_args": list(config.ignore_default_args)}
context = await browser.new_context(user_agent=scrub_headless_ua(raw_ua))
```

`--headless=new` does **not** strip the `HeadlessChrome` token, so the UA is
read from the launched browser and scrubbed — never composed, so the version
stays truthful. `config.init_scripts` is registered via CDP
`Page.addScriptToEvaluateOnNewDocument` after `Page.enable`, and the session is
deliberately not detached (detaching unregisters them).

After the fix every Uber RSC prefetch returns `200`.
`tests/test_discovery_observe.py` fails if the capture browser's
`navigator.userAgent` contains `Headless` — run it with
`FETCHALLER_RUN_BROWSER_CANARY=1`. That canary would have caught all of the
above before any board was blamed.

## Not getting blocked

Beyond the launch configuration, three mechanisms, each added after a measured
block:

1. **Carry the browser's session into the probes** (`plan.seed_cookies`).
   Otherwise an origin sees a browser load the page, then dozens of *anonymous*
   requests hit the endpoint it just called. On Meta this also cut the minimized
   plan from 7 kept fields to 3.
2. **A persistent browser profile**, so successive passes are a returning
   visitor rather than a new anonymous browser each time.
3. **Warm the origin before the deep link.** Measured on `metacareers.com`: `/`
   answered 200 while `/jobs?q=…` raised `RateLimited` on the same session. The
   root is derived from the target URL — no board is named. The same rule was
   applied to `meta_careers/api.py`.

**Rate limiting.** Probes go through `discovery_limiter` (`ratelimit.py`), which
unlike every other limiter there is not per-domain — discovery targets whatever
host it is pointed at. A pass is a *burst*: delta debugging fires ~50 replays at
one host. Learned the hard way, twice: an unlimited pass drew a 429 that
persisted for the session, and a later 3-attempt capture retry tripled volume
against an already-throttled host and made results worse. Replaying a *cached*
plan is not throttled — that is one request per user action.

## Throttle inside a 200

The measurable sibling of a challenge, and the reason the Meta misdiagnosis
survived so long. `payload.looks_rate_limited()` detects a 2xx whose decoded
payload is a refusal rather than data; `ranking` drops those candidates
outright.

Left in, Meta's 114-byte rate-limit error read as merely *thin*, and a
42,685-byte routing payload won as the largest same-host candidate — so
discovery minimized the routing endpoint and reported it **verified**.

## The pass, in order

| Module | Does |
|---|---|
| `observe.py` | Loads the page in a hardened browser, records every non-asset exchange |
| `ranking.py` | Decides which exchange is the data |
| `oracle.py` | Decides whether a replay returned the *same answer* |
| `minimize.py` | Delta-debugs the request down to what it actually needs |
| `provenance.py` | Traces volatile values back to whatever minted them |
| `plan.py` | The cacheable plan, and the replay path |
| `pipeline.py` | Orchestrates the above; attributes refusals |
| `store.py` | Persists plans; detects decay; the replay/self-heal cycle |
| `payload.py` | Decoding and measurement shared by all of it |

## Why ranking is the hard part

Two plausible signals disagree, and **neither is correct alone**:

- *How much of the payload is on screen* puts the chrome first. Netflix's
  `/api/apply/v2/branding` covers 1.00 and scores **17.47**; the real listing
  covers 0.23 and scores **8.03**. All 31 of branding's values are nav labels.
- *Largest record set* puts a lookup table first. Workday's `videoplayerlabels`
  is **334** homogeneous entries against 70 real postings.

No weighting fixes both — Netflix needs weight > 27 on the record term, Workday
< 21.6. What resolves it: **chrome does not depend on the query.** Branding
never mentions "engineer"; the listing does.

So: query hint first, then record count, then score. Boards whose URL carries no
query (Workday) fall through to score. Same registrable domain is the outermost
key, because a consent vendor can ship a bigger array than the board does —
Netflix's cookielaw payload has 200 records.

**Hints match payload *values*, never raw body text.** A routing payload echoes
the page URL back as a key, and Meta's `bulk-route-definitions` even stores the
parsed query parameter as a literal value.

Ranking is allowed to **refuse**, and the bar is a **record set**. This is a
deliberate deviation from the original spec, which also accepted any payload
with ≥5 distinctive values — that rule is exactly what let the routing payload
through, twice.

## Why the oracle exists

These APIs report a malformed request as `HTTP 200` with an empty-looking
result. The browser's own answer is ground truth, so a replay is **verified
rather than believed**.

Two of its five rules exist only because the minimizer silently shipped a broken
plan without them:

- **The upper record bound.** Without it, minimization drops a filter, gets the
  *unfiltered* listing back, sees the same shape and *more* records, and calls
  it a match.
- **The 50% content-overlap rule.** Measured on Apple: `query=engineer` and no
  query both return **20 records** — indistinguishable by count — but only **1%**
  of the content overlaps.

## The nudge

A board that server-renders its first page issues **no XHR at all** on load.
Two conventions, because pagination is only half of it: click a next/load-more
control if one exists, otherwise **scroll to the bottom**.

The gate is **exact-host, 2xx, carrying a record set**, each condition forced by
a real board:

- *Exact host* — Apple's page pulls a global-header payload from
  `www.apple.com` while sitting on `jobs.apple.com`; it scores 12.35, enough to
  suppress a lenient gate.
- *2xx with a body* — Google fires two `204` beacons at
  `www.google.com/g/collect` on load.
- *A record set* — routing and telemetry payloads satisfy anything weaker.

## Measured results

| Board | Endpoint | Kept | Records |
|---|---|---|---|
| Workday (Adobe) | `POST /wday/cxs/…/jobs` | 0/9 | 70 |
| Apple | `POST /api/v1/search` (nudged) | 4/14 | 20 |
| Google | `POST …/batchexecute` (nudged) | 1/16 | 20 |
| Amazon | `GET /en/search.json` | 2/17 | 10 |
| Eightfold (Netflix) | `GET /api/apply/v2/…/jobs` | 0/5 | 10 |
| Meta | `POST /graphql` | 3/35 | 586–588, when not rate-limited |
| Uber | none observed | — | refused, and says why |

Reproduced independently rather than trusted:

- **Apple's `format` is a required discriminator.** As captured: 5258. Drop
  `format`: **0**. Send `format: {}`: 5258 again — anonymous, no CSRF.
- **Workday minimizes to an empty body `{}`**, confirming
  `x-calypso-csrf-token` is not required.
- **Amazon's `loc_query` does not filter**: 6640 hits with and without it.
- **Google drops the build id** `bl`, plus `f.sid` and `_reqid` — which is what
  lets the plan survive a deploy.
- **Meta** keeps only `lsd`, `variables` and `doc_id`, drops all nineteen
  `__`-prefixed build-state fields, and hardens `lsd` into a mint step whose
  auto-derived regex anchors on the same `["LSD",[],{"token":"` that
  `meta_careers/api.py` uses by hand.

### Uber

Not challenge-protected and not server-rendered-only. Its prefetches succeed
once the browser is hardened, but they are shells (584–887 bytes); the full
383 KB Flight document comes from a direct request with an `RSC: 1` header,
which the browser's prefetch does not send. `payload.py` also has no
`text/x-component` decoder, so Flight rows measure as zero records either way.

Uber needs none of this: `uber_jobs/` delegates to `oracle_recruiting/`, which
calls Fusion directly and returns full descriptions and locations — richer than
the Flight route.

## Limits, stated honestly

- **A filter the board itself ignores cannot be detected.** The browser and the
  replay both get the unfiltered answer. Amazon's `loc_query`, Oracle's
  city-level `location` and Workday's `searchText` all fall here.
  `jobfilter.py` remains the guarantee.
- **Script bundles are not observed**, deliberately, so a bundle-only constant
  stays a literal. Meta's `doc_id` is the live example, and this is *strictly
  weaker* than `meta_careers.discover_doc_id`, which re-derives it indefinitely.
- **No `text/x-component` decoder.** Next.js Flight payloads measure as zero
  records.
- **Ranking without a hint is a heuristic.** Supply `expect` when you can.
- **Nothing is blocked on this package.** All ten board clients worked before it
  and work now; it is not wired into any of them. The case for it is the *next*
  board.
