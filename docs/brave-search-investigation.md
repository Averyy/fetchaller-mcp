# Brave Search pacing investigation — 2026-10-25

## Current integration status

Wafer’s Brave handler is published in version **0.7.9**, including approved commit `d01c811`. Fetchaller
now passes its existing guarded, audited shared solver through the unified
tool into Brave for explicit, all-provider and Google-fallback searches.
The default solver budget is 120s, the total Brave HTTP request 150s, and
the provider budget 180s including queueing, pacing and parsing. Solver
identity is checked before reusing Brave sessions. Google/DDG never receive
the solver. Shared pacing, no hidden retries/rotations, body caps and
cooldowns remain enforced. A wafer-identified gate is an error even at 200.

Fetchaller 3.8.6 requires published wafer 0.7.9 and locks its PyPI artifact. See [the reproducible MCP probe](testing.md#brave-search-with-local-wafer).
The earlier investigation below records the original state and evidence.

## Published wafer 0.7.9 / fetchaller 3.8.6 verification

Fetchaller requires `wafer-py[browser]>=0.7.9`; the lock uses the published
PyPI wheel, not an editable checkout. All **49 Python/JavaScript source files**
in the installed package match the independently approved source. The only new
locked transitive dependency is wafer's browser-extra `quickjs-ng`; Google's
Docs search does not use it or the ordinary Google SearchGuard route.

- Full fetchaller suite with the published package: **4,603 passed, 5 skipped**.
  Ruff (`src/`, `tests/`, all `scripts/`), lock and diff checks passed.
  Log: `/tmp/fetchaller-wafer-079-full.out`.
- Fresh stdio MCP using the installed PyPI package recovered a real challenge
  in **36.92s**, returned Brave **20 + 20** with zero URL overlap, and `all`
  returned **Google 10 / DDG 10 / Brave 20**. Audit: **23 allowed / 0 denied**.
  Log: `/tmp/fetchaller-wafer-079-mcp.out`.
- The exact new Linux amd64 image was built from the frozen lockfile. Its
  complete stdio smoke suite passed **20/20 gates without live waivers**,
  including all four web-search modes, Reddit, AliExpress, Alibaba, marketplace,
  LinkedIn, the UniFi manual and realtor. Brave's real challenge recovered in
  **32.72s**. Log: `/tmp/fetchaller-386-container-smoke.out`.
- The untouched candidate image also passed its HTTP suite: **28 registered
  tools**, **19 semantic gates**, healthy readiness and OAuth token rotation.
  Brave recovered a real challenge there too. Log:
  `/tmp/fetchaller-386-http-smoke-clean.out`.
- Actual guarded-Chrome egress/security tests in the candidate image:
  **57 passed**. Log: `/tmp/fetchaller-386-browser-canary.out`.
- Graceful HTTP shutdown exited **0**; after restart, healthy readiness,
  protocol/version/tool discovery, fetch dispatch and persisted OAuth token
  rotation passed. Log: `/tmp/fetchaller-386-http-restart.out`.

## Release validation: APPROVED at d01c811

Independent verification completed on 2026-10-25 (EDT). Wafer's accepted-reload
stall and fast spare-token classification race are resolved in this candidate.
Release approval is written in `../wafer/todo-brave-search.md`. No wafer
implementation files were changed during verification. Fetchaller now requires
wafer 0.7.9, and its lockfile uses the published PyPI artifact. All 49 installed
Python/JavaScript files match the independently approved source bytes.

- Fetchaller full suite: **4,603 passed, 5 skipped**. Wafer full suite:
  **2,856 passed, 25 skipped**, including **75 Brave regressions**. Ruff,
  lockfile and diff checks passed in both repositories.
- Native **headless and headed** acceptance probes passed unchanged. Fresh
  PoW returned real search content in **30.89 / 31.01s**. Every spare recovery
  in both runs was correctly classified `cleared=True`, `pow_accepted=False`,
  `challenge_absent=False`: **12.56 / 12.66s** headless and **12.71 / 12.67s**
  headed. Browser-free HTTP token consumption and cache restart passed.
  Logs: `/tmp/wafer-brave-d01c811-headless.out` and
  `/tmp/wafer-brave-d01c811-headed.out`.
- A fresh headless solve persisted an unspent token. A **separate process
  without a BrowserSolver** loaded it, returned **216,787 bytes of real search
  content**, and confirmed token consumption. Logs:
  `/tmp/wafer-brave-d01c811-process-prime.out` and
  `/tmp/wafer-brave-d01c811-process-replay.out`.
- Actual guarded stdio MCP, **macOS**: Brave **20 + 20**, zero URL overlap;
  `all` **Google 10 / DDG 10 / Brave 20**. Two recoveries were correctly
  classified. Fresh recovery **31.50s**; **37 allowed / 0 denied** egress.
  Log: `/tmp/fetchaller-brave-d01c811-mcp-all.out`.
- Same candidate source mounted read-only into the existing **Linux amd64
  runtime image**: identical result counts, zero overlap, two correctly
  classified recoveries; fresh recovery **34.02s**, **36 allowed / 0 denied**.
  This verifies the local source in that runtime, not a newly published image.
  Log: `/tmp/fetchaller-brave-d01c811-linux-mcp.out`.
- Real headless pacing: awaited concurrent token-key fetches inside
  `evaluate()` and paced `reload()` completed without an outer flush loop.
  Actual send gaps **6.473 / 6.437 / 6.274s**, both keys 200, clean cleanup.
  This is progress/pacing evidence, separate from the PoW solve proof.
  Log: `/tmp/wafer-brave-d01c811-pacing.out`.

Wafer retains structural gate evidence from earlier exact-target network
documents before classifying the latest document. The new tests cover a real
gate and final response arriving before the first DOM poll, including HTTP 200
gates and normal solver listeners. Plain 429, wrong origin/query/page,
non-HTML/prose and oversized-response controls remain enforced. Shared guards,
six-second pacing and 120s / 150s / 180s budgets were not relaxed. No remaining
Brave release blocker was observed in this matrix.

## Historical validation at 8e9844b

Wafer commit `8e9844b` fixes the circular wait described below. Paced routes now
drain independently on the driver's event loop while synchronous protocol calls
wait. Required key, PoW and document exchanges take precedence over optional
feedback, while every first-party send keeps the same six-second limiter.
Cookie handoff accounts for Brave consuming token cookies during HTTP reads.
No fetchaller timeout increase or guard exception was needed.

Independent verification against that exact local source:

- Fetchaller: **4,603 passed, 5 skipped**; wafer: **2,845 passed, 25 skipped**,
  including all **64 Brave regressions**. Lint and lock checks passed in both
  repositories. Fetchaller's wheel built successfully.
- Actual local stdio MCP: page 1 returned 20 results, then page 2 recovered a
  real challenge and returned **20 attributed Brave results in 37.09s**, with
  zero overlapping URLs. Audit: `response_status=200 challenge_absent=False`,
  one browser dispatch, **26 allowed / 0 denied** egress connections.
  Log: `/tmp/fetchaller-brave-8e9844b-mcp-recovery.out`.
- Repeated MCP run: another page-2 challenge recovered in **36.83s**, both
  Brave pages returned 20 results with zero overlap, and `all` returned
  **Google 10 / DDG 10 / Brave 20** in 5.61s. Audit: one solve, **22 allowed /
  0 denied**. The verifier now completes its requested merged check after a
  recovery rather than stopping before `--include-all` executes.
  Log: `/tmp/fetchaller-brave-8e9844b-all.out`.
- Linux amd64: the existing `fetchaller-mcp:test` image ran with the exact
  local fetchaller and wafer source mounted read-only. The guarded stdio MCP
  recovered a challenge and returned **20 Brave results in 32.69s**, one solve,
  **23 allowed / 0 denied**. This tests the local candidate source in that
  runtime, not a newly built published image.
  Log: `/tmp/fetchaller-brave-8e9844b-linux-mcp.out`.
- Native headless: fresh PoW returned 200 at **23:42:55 UTC**; the correct
  `_rr=1` search returned 200 at **23:43:07**, delivering **227,377 bytes** in
  **30.89s**. A later HTTP read used and consumed the token with no browser.
  Subsequent gated pages returned actual search content without another PoW.
  Log: `/tmp/wafer-brave-8e9844b-native.out`.
- Native headed: the complete acceptance probe **passed**: fresh PoW and real
  search content (31.00s), browser-free HTTP token consumption, spare-token
  recovery without new PoW, and a new-session cache read **without a browser**
  (219,282 bytes, 200, consumed token). Log:
  `/tmp/wafer-brave-8e9844b-headed.out`. One earlier spare recovery in that same
  run was mislabelled, so this passing run does not remove the audit race.
- Independent process restart: a fresh headless solve prepared and persisted
  an unspent cookie. A separate Python process, with **no BrowserSolver**,
  loaded it and returned a **219,283-byte real search document**, then confirmed
  the token had been consumed. Logs:
  `/tmp/wafer-brave-8e9844b-process-prime.out` and
  `/tmp/wafer-brave-8e9844b-process-replay.out`; observation harness:
  `/tmp/verify_wafer_brave_process_replay.py`.
- Native headless pacing: concurrent awaited key fetches inside `evaluate()`
  and a paced `reload()` completed without an outer flush loop. Actual send
  gaps were **6.281 / 6.297 / 6.258 seconds**; both key responses were 200.
  The search/reload were issued 429 gates; this verifies scheduling and driver
  progress, not a PoW solve. Cleanup completed without errors.
  Log: `/tmp/wafer-brave-8e9844b-pacing.out`.

### Historical classification race: resolved in d01c811

The headless native run observed browser main documents 429 then 200 twice,
without new PoW, but labelled both `cleared=False` / `challenge_absent=True`.
Search content was returned successfully. The native probe consequently counted
zero spare-token recoveries and exited **2** before checking cache restart.
That headless run is not a passed complete native acceptance probe. A later
headed run passed the complete flow after one correctly labelled spare recovery;
its earlier spare recovery was still mislabelled. The race was intermittent.

`wait_for_brave` retains accepted-new-PoW evidence but starts with no observed
gate and examines only the latest network document. A fast spare-token reload
can finish before the first DOM poll, losing the earlier gate evidence.
The follow-up was documented in `../wafer/todo-brave-search.md`, then resolved
by `d01c811`; that document now records release approval. No wafer code was
changed during verification. Its previous reload-stall handoff was incorporated into wafer's
`docs/ref-brave.md` and removed by `8e9844b`. A recovered flag alone never proves
fresh PoW or cookie persistence.

## Historical fresh-challenge failure before 8e9844b

The following records the failure on `5ce977c`, superseded for retrieval by the
successful recovered responses above.

The integration is implemented and its local suite passes (4,603 passed,
5 skipped). Lint, lock validation, and packaging also pass. Real stdio MCP
searches returned Brave 20 + 20 with no overlap, and `all` returned Google 10,
DDG 10, Brave 20. A deliberate real browser dispatch through the actual guarded
session returned 20 results, with an audited dispatch and no denied egress;
that navigation was challenge-free and was not a solve proof.

A sustained, normally paced MCP run then encountered a real challenge on its
16th search: page 2 of `Python asyncio documentation queues`. The user observed
that the browser displayed verified. The browser solve nevertheless returned
None, and MCP honestly returned the original identified HTTP 429 with 60s
cooldown. No further request was sent through the blocked session. Normal
budgets were 120s solve, 150s request, 180s provider. Egress: 27 allowed, 0 denied.

An observation-only native wafer reproduction then allowed a **240s total HTTP
budget**. Its fresh challenge returned a PoW HTTP 200 about 18 seconds after
detection, followed by another successful key fetch. No completed search was
captured. The browser timed out after **234.9s**, returning the original 429.
A pending reload retained the original query and added `_rr=1`; cleanup aborted
it with `net::ERR_ABORTED`. Egress: 26 allowed, 0 denied. Increasing the outer
budget did not fix this reproduction. A DOM-read / queued-navigation event-pump
stall is being investigated; no relaxed redirect validator or extra retries
were added to fetchaller.

A further reproduction removed fetchaller's proxy entirely and used native wafer
only. It hit the same failure. At 22:26:54 UTC PoW returned 200; at 22:27:00.888
wafer entered `_bounded_page_content`. That read did not return for about 210s,
until the browser deadline interrupted the driver at 22:30:31. The pending
exact-query `_rr=1` reload was aborted during cleanup, and native wafer returned
429. This isolates the stall to wafer's browser handling rather than the MCP
budget or fetchaller egress proxy. The exact DOM-read sub-call and its dependency
on queued route flushing remain for wafer to investigate. The detailed no-proxy
trace is `/tmp/brave-recovery-direct.out`; run the same harness with `--direct`.

The original detailed handoff was `../wafer/todo-brave-search.md` (incorporated
into wafer's `docs/ref-brave.md` by `8e9844b`). Historical evidence:
`/tmp/fetchaller-brave-soak-1.out`, `/tmp/brave-recovery-diagnostic.out`, and the
observation harness `/tmp/diagnose_brave_recovery.py`. No wafer code was edited.

The first 429 capture was Brave's HTML CAPTCHA page: "Your request has been
flagged as being suspicious" and "schedule a captcha". The status alone did
not establish that the caller had exceeded a documented request quota.

## Fetchaller issues found and fixed

- Application pacing ran once before `session.get()`, while wafer's limiter was
  disabled. A controlled local HTTP replay using the original session options
  returned a 503 then a real captured HTML page: wafer retried after **1.082s**,
  below fetchaller's advertised three-second search interval.
- The application released its pacing lock before the HTTP exchange. A slow
  transport wait or retry could let overlapping queries bunch up on the wire.
- Brave had no provider-wide 429/403 cooldown. A new explicit search, `all`, or
  Google fallback could request the blocked provider again after three seconds.
- Standalone diagnostic scripts need a `__main__` guard: on macOS, isolated
  parser workers import the calling script. An unguarded live-test script can
  repeat requests in independent processes, each with a fresh limiter. This
  occurred in the initial direct-Python paced diagnostic and was corrected.
  The MCP server's normal entry point is already guarded. Use the real stdio
  MCP path for final acceptance checks.

DDG and Brave now match Reddit's default **ten requests per minute per provider**
with a smooth six-second floor plus 0.3–1.0 seconds of jitter. Their clients hold
a shared lock through each HTTP exchange, set wafer's own limiter to six seconds,
and disable hidden retries (`max_retries=0`, `max_rotations=0`). Google retains
its explicitly paced retry budget and six-second minimum. DDG/Brave cooldowns
match Reddit's defaults: 60 seconds for 429, 300 seconds for 403, extended by
longer `Retry-After`; DDG's blocking 202 homepage also gets 300 seconds. All
search modes use the same provider gates. These limits are process-local, like
the existing Reddit queue; multiple server processes have independent budgets.

Wafer's source confirms that `max_rotations=0` already returns 429 without
retrying or rotating. The reproduced short retry was a **503 retry**, not a
hidden 429 retry. No claim is made that this alone caused the earlier CAPTCHA.

## Ownership and live evidence

After more than ten minutes without Brave requests, the ordinary wafer profile
returned 20 results on page 1 and 20 on page 2, with zero overlap and no challenge.
No browser solver, custom impersonation, account cookie, API key or wafer change
was needed. The first direct diagnostic also exposed the script-guard issue
above. A subsequent guarded stdio MCP run returned 20 Brave results on page 1,
then HTTP 429 on page 2 despite the enforced pacing (the second tool call took
6.0 seconds). The live run stopped at that block. This is not a clean proof of
steady-state rejection: the earlier diagnostic's duplicate-process traffic
could still have influenced it. It does establish that successful pages alone
are insufficient to claim reliable CAPTCHA avoidance. The cooldown and
no-network behavior are covered by replay/concurrency tests.

The MCP cache configuration was also checked: no persisted Brave cookie file
was present, so there is no evidence for a poisoned shared Brave cookie cache.
The original caller used wafer's ordinary default Chrome profile, no browser solver,
no identity rotations and no custom User-Agent. The successful and blocked
calls used the same URL/parameter/header construction. Remaining hypotheses
include residual throttling or transport/browser-identity challenge handling;
these have not been isolated, so no specific anti-detection cause is claimed.

Request scheduling, avoiding bursts, and holding a blocked provider belong in
fetchaller and are fixed here. If correctly paced requests still encounter a
CAPTCHA, detecting or solving that active challenge belongs in wafer. Do not add
CAPTCHA solving, cookie manipulation or fingerprint rotation to fetchaller.
Spacing reduces avoidable triggers; it does not prove that a third party will
never challenge a request.

## Follow-up: confirmed challenge-handling gap

The saved 429 is Brave's first-party **Argon2 proof-of-work and blind-token**
challenge. Its Svelte script carries internal `page:"/captcha"` and an issued
`challengeSet` with 100 tokens, work parameters, and a solution limit. Inspecting
the site's own current JavaScript through wafer confirmed that sets above ten
tokens require pressing **Verify**; smaller sets auto-start. The client then
validates a blind-token batch, consumes a token, sets a first-party anonymous
cookie, and reloads the original search.

Wafer 0.7.7's `detect_challenge()` returns **None** for the real 73,896-character
capture. No dedicated detector recognizes this protocol, and the generic-JS
fallback excludes bodies above 50,000 characters. Consequently this challenge
never reaches a browser solver. Attaching a solver alone or simply increasing
the generic size limit would not implement its required verification action.
The browser passthrough validator also needs to reject this interstitial rather
than treating a quiet page as successful content.

At investigation time the search session did not receive the server's guarded
shared BrowserSolver, and Brave was limited to a 10-second request / 15-second
provider budget. Those integration gaps are now fixed while preserving the
Google Docs provider's browser-free path and the shared Brave pacing gate.

At Avery's request, no wafer code was changed. The original implementation and
verification handoff was written in the sibling checkout at
`/Users/avery/Code/wafer/todo-brave-search.md` and later incorporated into wafer's
`docs/ref-brave.md`. It included exact capture paths,
the site's own script URLs and protocol, anonymous-state reuse concerns,
deadline/pacing requirements, and live acceptance tests. No live search probes
were added in this follow-up; the detector failure was reproduced offline on
the saved real response, and only the static protocol scripts were fetched.
The reason the first challenge is issued so early remains unisolated; detecting
and answering that challenge is a confirmed missing transport capability.
That missing transport capability has since been implemented in local wafer,
and the fetchaller integration described at the top is now implemented.
Live challenge recovery is now verified separately from clean searches, as
recorded above; the fast spare-token classification issue was fixed in `d01c811`.
