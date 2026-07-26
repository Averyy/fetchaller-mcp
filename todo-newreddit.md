# TODO: Retire Old Reddit and use New Reddit's logged-out flow

Status: researched and live-validated on 2026-07-25 and 2026-07-26. No implementation yet.

## Decision

Replace both Old Reddit dependencies with:

1. New Reddit's logged-out JavaScript verification, solved inside wafer using the
   same anonymous browser session Reddit serves to normal logged-out visitors.
2. Reddit's anonymous JSON responses as the content source.
3. A fetchaller-side compact Markdown renderer that selects only useful post and
   comment fields.

Do **not** make Redlib's Android OAuth impersonation the default. It works today,
but it borrows Reddit's first-party Android client identity and undocumented auth
endpoint. That is less defensible, easier for Reddit to revoke, and directly
conflicts with Reddit's current requirement to identify API clients honestly.

This design remains account-free and does not expose a user's Reddit identity.
It is not anonymous to Reddit in the strict sense: Reddit still sees the
fetchaller server's IP/TLS identity and assigns pseudonymous cookies such as
`loid` and `token_v2`.

## Why this is necessary

Reddit announced on June 30, 2026 that logged-out access to Old Reddit will
require login over the following month. Logged-out browsing on `reddit.com`
will remain available:

- <https://www.reddit.com/r/modnews/comments/1ujtebf/logging_in_to_use_old_reddit/>
- <https://support.reddithelp.com/hc/en-us/articles/51137453241492-Changelog-July-9-2026>

The rollout was incomplete from the tested IP on 2026-07-25: anonymous
`old.reddit.com` still returned normal HTML. Other users are already receiving
the wall, so this is not a usable durability signal.

## Current fetchaller and wafer behavior

There are two independent Old Reddit dependencies.

### Normal `fetch` calls

`src/fetchaller/content/reddit.py::transform_reddit_url()` rewrites:

- `www.reddit.com` -> `old.reddit.com`
- `reddit.com` -> `old.reddit.com`

`src/fetchaller/tools/fetch.py` then fetches and cleans Old Reddit HTML using
Old Reddit-specific CSS selectors. Normal post/thread fetching will return a
login page or redirect once logged-out Old Reddit is gated for the deployment
IP.

The generated discussion links in `format_reddit_post()` are also hard-coded to
`https://old.reddit.com`.

### JSON-backed `browse_reddit` and `search_reddit`

These tools already request compact public JSON:

- `https://www.reddit.com/r/{subreddit}/{sort}.json`
- `https://www.reddit.com/[r/{subreddit}/]search.json`

They share a long-lived `wafer.AsyncSession`, which is good. On a cold session,
however, Reddit returns a 403 Shreddit network-security block instead of JSON.
Wafer detects that response as `ChallengeType.REDDIT` and currently visits:

```text
https://old.reddit.com/
```

The Old Reddit response establishes `Domain=.reddit.com` anonymous cookies,
after which wafer retries the original JSON request. The dependency is in:

- `~/code/wafer/wafer/_solvers.py::reddit_warmup_url()`
- `~/code/wafer/wafer/_sync.py`'s `ChallengeType.REDDIT` branch
- `~/code/wafer/wafer/_async.py`'s equivalent branch

When Old Reddit becomes login-only, the warmup can still receive a 2xx response
without receiving the cookies needed by JSON. The current solver only checks
for a 2xx, treats warmup as successful, and retries the blocked JSON request.
Cold `browse_reddit`, `search_reddit`, and explicit `.json` fetches will then
fail.

### Secondary bug found during investigation

Reddit hostname detection currently uses a substring test:

```python
if "reddit.com" not in hostname.lower():
```

This misclassifies names such as `notreddit.com` and
`reddit.com.example.test` as Reddit. Use:

```python
host == "reddit.com" or host.endswith(".reddit.com")
```

This matters for site cleanup, rate limiting, URL behavior, and cookie setup.

## Live comparison

### Current Old Reddit method

With a fresh wafer session:

- `GET https://old.reddit.com/`
  - Status: 200
  - Body: 256,470 bytes
  - Response cookies included `csv`, `edgebucket`, `loid`, and
    `session_tracker`.
- `GET https://www.reddit.com/r/python/hot.json?limit=1`
  - Initially triggered wafer's Reddit challenge handling.
  - Wafer logged `Reddit session warmed via https://old.reddit.com/`.
  - Retry returned JSON 200, 3,045 bytes.
- `GET https://api.reddit.com/r/python/hot?limit=1`
  - Had the same cold-session behavior and returned the same listing after the
    Old Reddit warmup.

The internal wafer warmup does not normally consume the Old Reddit HTML body:
wreq returns the response object after headers, wafer caches `Set-Cookie`, and
the response is dropped. The body size is therefore not the main problem; the
login wall and cookie loss are.

### New Reddit logged-out method

A cold request to `https://www.reddit.com/` returned:

- Status: 200
- Body: 8,424 bytes
- Title: `Reddit - Please wait for verification`
- A hidden GET form containing:
  - `solution`
  - `js_challenge=1`
  - `token`
  - `jsc_orig_r`
- A small nonce-bearing inline script that calculated the current solution.

The tested script doubled a server-provided seed. The solver must derive the
operation from the served challenge and must not hard-code the observed seed or
challenge token.

Submitting the form in the same wafer session returned:

- Status: 200
- A normal New Reddit homepage.
- Body size if consumed: approximately 513-536 KB.
- `Set-Cookie` values:
  - `loid` — persistent
  - `token_v2` — persistent
  - `csv` — persistent
  - `session_tracker` — session cookie
  - `csrf_token` — session cookie

After this, the original anonymous JSON request returned 200 immediately.

The large post-verification homepage does **not** need to be downloaded.
wreq exposes status and headers before the response body is consumed. Wafer can:

1. Issue the solved GET through its internal wreq client.
2. Validate the 2xx status.
3. cache all `Set-Cookie` headers.
4. Drop the response without calling `.bytes()`/`.text()`.
5. Retry the original JSON request.

A separate test used wafer's `max_response_size=12000`. The solved request
raised `ResponseTooLarge`, but wreq's in-memory cookie jar already contained the
cookies and the next JSON request succeeded. Directly caching headers before
body consumption is cleaner because it also persists the cookies.

Persistence was validated:

1. Solve New Reddit verification.
2. Write the persistent cookies through wafer's `CookieCache`.
3. Construct a completely fresh `wafer.SyncSession` using the same cache
   directory.
4. Request `https://www.reddit.com/r/python/hot.json?limit=1`.
5. Receive JSON 200 without another challenge or any Old Reddit request.

This is the preferred warmup.

## Content and token comparison

Test thread:

```text
https://www.reddit.com/r/Python/comments/1v6gbps/
```

It had one self-post and six comments during testing. Fetchaller's configured
estimate is four characters per token.

| Representation | Characters | Estimated tokens | Notes |
|---|---:|---:|---|
| Current cleaned Old Reddit Markdown | 3,291 | 823 | Included duplicated UI and `save`/`hide`/`report` noise |
| Raw Reddit JSON | 19,251 | 4,813 | Contains every API metadata field |
| Prototype selected-field Markdown | 2,348 | 587 | Post plus all six nested comments |

The prototype renderer was:

- approximately 29% smaller than current cleaned Old Reddit Markdown;
- approximately 88% smaller than returning raw JSON;
- structurally cleaner because Reddit's `selftext` and comment `body` are
  already Markdown and need no HTML-to-Markdown pass.

The serialized raw Old Reddit HTML tool result for the same thread was roughly
83 KB. New Reddit's verified homepage was roughly 513 KB. Neither HTML document
should enter the model context.

### JSON response growth on a larger thread

For a thread with roughly 109 comments, using `depth=2`:

| API `limit` | Returned comments | `more` nodes | Raw JSON characters | Estimated raw tokens |
|---:|---:|---:|---:|---:|
| 5 | 5 | 6 | 16,937 | 4,235 |
| 10 | 10 | 9 | 28,740 | 7,185 |
| 25 | 24 | 12 | 62,762 | 15,691 |
| 50 | 33 | 17 | 83,166 | 20,792 |

The raw response can be large, but it stays inside fetchaller. Only selected
fields should be rendered and passed to the caller. `limit` and `depth` should
still be bounded to control transfer, parsing cost, and worst-case comment
bodies.

## New Reddit versus Android OAuth

| Property | New Reddit logged-out verification | Redlib-style Android OAuth |
|---|---|---|
| Reddit account required | No | No |
| Uses the public logged-out web flow | Yes | No |
| Borrows a first-party client ID | No | Yes |
| Claims to be the Android Reddit app | No | Yes |
| Persistent identifier | `loid`/`token_v2` cookies | Device UUID, bearer token, `x-reddit-loid`, `x-reddit-session` |
| Content endpoint | Anonymous `www.reddit.com/*.json` | `oauth.reddit.com` |
| Live status | Working | Working |
| Primary risk | Anonymous JSON can be withdrawn | First-party impersonation can be detected/revoked |
| Recommended role | Default | At most an explicit, documented fallback |

### Redlib live results

Current Redlib source:

- <https://github.com/redlib-org/redlib/blob/main/src/oauth.rs>
- <https://github.com/redlib-org/redlib/blob/main/src/client.rs>

Redlib's current primary flow is not the old documented installed-client grant:

1. It builds an Android Reddit User-Agent and device headers.
2. It authenticates to:

   ```text
   https://www.reddit.com/auth/v2/oauth/access-token/loid
   ```

3. It sends Reddit's first-party Android OAuth client ID using HTTP Basic auth.
4. It requests `["*", "email", "pii"]`.
5. It uses the bearer token against `https://oauth.reddit.com`.

Live validation:

- Token request: 200.
- Lifetime: 86,399 seconds, approximately 24 hours.
- First listing request: 200.
- `x-ratelimit-remaining`: 99.0 after that request.
- A token requested with only `["read"]` also worked, proving the broad Redlib
  scopes are unnecessary for fetchaller.

Redlib also has a `GenericWebAuth` fallback using the documented
`installed_client` grant. Reproducing its exact client credential, form body,
and headers returned:

```text
401 Unauthorized
```

The fallback only runs after repeated Android failures, so Android currently
masks the broken fallback.

### Policy implications

Reddit's current documentation says:

- API access requires explicit approval.
- Clients must use registered OAuth.
- User-Agents must be unique and honest.
- Clients must not mask or misrepresent how they access Reddit data.
- Clients must not circumvent limits.
- Unauthenticated/unidentified API traffic may be blocked.

Sources:

- <https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy>
- <https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki>
- <https://redditinc.com/policies/developer-terms>
- <https://redditinc.com/policies/data-api-terms>

Therefore:

- New Reddit verification plus anonymous JSON is technically working and does
  not falsely claim to be Reddit's Android app.
- It is still automated access and is not an officially approved Data API
  integration.
- Android OAuth is technically robust today but is materially riskier because
  the request deliberately misidentifies the client.
- A fully supported long-term API integration requires a registered app,
  approval, and therefore a Reddit account. There is no currently documented
  route that is simultaneously official, durable, account-free, and anonymous.

## Proposed wafer implementation

All verification, cookie handling, and anti-detection work belongs in wafer.
Fetchaller must not implement the solver.

### Replace `reddit_warmup_url()`

Replace the fixed Old Reddit URL with a New Reddit warmup routine used by both
`SyncSession` and `AsyncSession`.

Proposed behavior after a cold JSON request is recognized as
`ChallengeType.REDDIT`:

1. Validate that the challenged URL's host is exactly `reddit.com` or a
   subdomain of `reddit.com`.
2. Fetch `https://www.reddit.com/` with the current wreq client and remaining
   request deadline.
3. If the response already carries usable persistent Reddit cookies, cache them
   and retry without solving.
4. Otherwise, consume only the small verification body.
5. Structurally validate:
   - expected verification title/markers;
   - hidden GET form;
   - same-origin `/` action;
   - required hidden fields;
   - recognizable solution calculation.
6. Derive the solution from the served script.
7. Submit the form with the same wreq client and browser identity.
8. Require a 2xx response and at least the expected cookie evidence
   (`loid` plus `csv` and/or `token_v2`).
9. Persist the raw `Set-Cookie` headers using wafer's existing cookie cache.
10. Do not consume the normal New Reddit page body.
11. Retry the original JSON request once.

### Solver safety and robustness

- Do not hard-code the dynamic challenge token or seed.
- Do not accept arbitrary form destinations; submission must stay on
  `https://www.reddit.com`.
- Do not log the challenge token, solution, cookies, `loid`, `token_v2`, or any
  bearer values.
- Preserve the original request timeout/deadline across both warmup legs.
- Fail closed when the challenge structure changes.
- Attempt the New Reddit warmup at most once per original request.
- Do not rotate identity merely to obtain more Reddit rate-limit budget.
- Keep a long-lived shared Reddit session instead of creating a fingerprint and
  pseudonymous identity per fetch or per end user.
- Prefer the persistent cookie jar: it reduces verification traffic and avoids
  repeatedly presenting as a new anonymous visitor.

### Wafer tests

Add sync and async coverage for:

- Reddit hostname validation, including hostile suffix/prefix cases.
- Verification challenge parsing.
- Missing/malformed fields.
- Changed or unrecognized script calculation.
- Same-origin form enforcement.
- Successful cookie extraction and caching.
- Required-cookie validation rather than accepting any 2xx.
- Post-verification response body not being consumed.
- Retry of the original JSON request.
- Warmup attempted only once.
- Cookie persistence across a fresh session.
- Timeout exhaustion during either leg.
- No request to `old.reddit.com`.

Add a live test with an empty temporary cookie cache:

1. Request a one-item Reddit JSON listing.
2. Assert JSON 200.
3. Assert the warmup log/mocked request history contains `www.reddit.com`.
4. Assert it contains no `old.reddit.com`.
5. Recreate the session with the same cache.
6. Assert the second JSON request succeeds without verification.

## Proposed fetchaller implementation

### Stop rewriting normal URLs to Old Reddit

`transform_reddit_url()` should become strict Reddit URL recognition and
canonicalization, not an Old Reddit transform.

For rendered, non-raw requests:

- Thread URLs should use the Reddit JSON thread endpoint.
- Subreddit listing URLs should use the existing listing JSON behavior.
- Search URLs should use the existing search JSON behavior.
- Generated links should use `https://www.reddit.com`.

Explicit `.json` URLs requested by a caller should continue returning raw JSON,
because the caller explicitly selected that representation.

For `raw=true`, preserve clear semantics. A normal Reddit URL should mean raw
New Reddit HTML at the requested/canonical URL, not a hidden rewrite to Old
Reddit. This path will be large and should remain opt-in.

### Thread URL mapping

Recognize at least:

```text
/r/{subreddit}/comments/{post_id}/
/r/{subreddit}/comments/{post_id}/{slug}/
/r/{subreddit}/comments/{post_id}/{slug}/{comment_id}/
/comments/{post_id}/
```

Fetch:

```text
https://www.reddit.com/r/{subreddit}/comments/{post_id}.json
    ?raw_json=1
    &sort=confidence
    &limit={bounded_limit}
    &depth={bounded_depth}
```

For a comment permalink, preserve enough context to return the selected comment
and its parent chain rather than silently turning it into an unrelated top-level
thread view.

### Compact Markdown renderer

Post fields:

- `title`
- `subreddit`
- `author`
- `score`
- `num_comments`
- `created_utc`, formatted compactly
- `selftext`
- external URL for link posts
- canonical `www.reddit.com` discussion URL
- useful media URL/metadata where applicable

Comment fields:

- `author`
- `score`
- `created_utc`
- `body`
- nesting depth
- submitter/mod/admin distinction only when useful

Omit:

- IDs unless needed for pagination/follow-up.
- flair internals.
- awards/gilding payloads.
- moderation fields irrelevant to a public reader.
- tracking fields.
- HTML duplicates (`selftext_html`, `body_html`).
- action UI (`save`, `hide`, `report`, `reply`, `share`).
- empty fields.

Use Reddit's Markdown bodies directly. Do not render `body_html` and then run
Markdownify.

Render `more` objects as compact markers, for example:

```text
[42 more replies omitted]
```

Do not automatically issue `morechildren` requests unless a future tool
explicitly asks for expansion. Automatic expansion makes request counts and
model context unpredictable.

### Token budgeting

Derive conservative API limits from `maxTokens`, then apply the existing final
character cap after rendering.

The limit cannot guarantee output size because one comment can be arbitrarily
long. It is a transfer/parsing guard, not the final token guard.

Suggested initial defaults:

- `depth`: 4 or lower for small token budgets.
- `limit`: bounded between 5 and 50, derived from `maxTokens`.
- preserve `sort=confidence` unless the original URL specifies a supported
  comment sort.

Render the post first, then comments in API order until the output budget is
nearly exhausted. End at a comment boundary and append an omission marker
rather than cutting a Markdown comment in the middle.

### Session, queue, and caching

- Reuse one long-lived Reddit session and cookie jar for fetch, browse, and
  search where the SSRF-pinning architecture permits it.
- Continue proactive Reddit request queuing.
- Honor `Retry-After` and any rate-limit headers.
- Do not rotate cookies/tokens to evade rate limits.
- Consider a short 30-60 second response cache for identical listing/search
  requests. Current Reddit API responses are deliberately not cached, but a
  very short cache would reduce duplicate anonymous traffic without materially
  staling results.
- Never cache deleted Reddit content long term. Reddit's current policy asks
  clients to remove deleted content and recommends short retention.

### Fetchaller tests

Add coverage for:

- Strict Reddit hostname matching.
- No Old Reddit URL rewrites.
- No Old Reddit links in browse/search output.
- Thread URL parsing, with and without slug/comment ID.
- Query preservation and supported sort validation.
- Self-post and link-post rendering.
- Nested comments.
- Deleted authors/comments.
- `more` nodes.
- Empty threads.
- Markdown bodies containing code fences, links, lists, and blockquotes.
- Media/gallery/video posts.
- NSFW, quarantined, gated, private, and banned responses.
- Comment-boundary token truncation.
- Explicit `.json` remaining raw.
- `raw=true` fetching the requested New Reddit representation.
- Live cold and warm session behavior using the local MCP server.

Update:

- `docs/architecture.md`
- `CLAUDE.md` Reddit content-module description if it still claims Old Reddit.
- `landing/llms.txt` if externally visible behavior/tool descriptions change.

## Rollout plan

### Phase 1: wafer

- Implement and unit-test the New Reddit verification warmup in the wafer repo.
- Run wafer lint/tests.
- Run a cold-cache live JSON test.
- Confirm persistent-cookie reuse after session recreation.
- Publish/update wafer and update fetchaller's lockfile as appropriate.

### Phase 2: fetchaller

- Add the Reddit JSON thread client/renderer.
- Stop Old Reddit transforms and links.
- Reuse the Reddit request queue.
- Add token-budget-aware comment limits.
- Run lint and full tests.
- Manually test browse, search, thread fetch, comment permalink, media, and an
  NSFW thread using the local MCP server.

### Phase 3: production verification

- Deploy without Android OAuth.
- Monitor:
  - New Reddit verification attempts/successes/failures.
  - Cold versus cached-cookie JSON success.
  - Reddit 403/429 rates.
  - JSON response sizes.
  - Rendered output sizes.
- Logs must identify the solver stage without including cookie or token values.
- Keep any temporary Old Reddit fallback feature-flagged and time-bounded. The
  rollout means it cannot be the durable rollback path.

## Acceptance criteria

- No normal fetchaller or wafer path requests `old.reddit.com`.
- Fresh anonymous sessions can fetch Reddit JSON using New Reddit verification.
- Persistent cookies survive session/process recreation.
- `browse_reddit` and `search_reddit` continue to work.
- Normal Reddit thread URLs return post and nested comment Markdown.
- Output contains no Old Reddit links or Old Reddit UI noise.
- Representative thread output is no larger than current output and should
  normally be materially smaller.
- No Android OAuth client ID, OAuth bearer, or Reddit account is required.
- Rate limits are respected without identity/token rotation.
- No cookie, challenge token, solution, or pseudonymous Reddit identifier is
  logged.

## Open risks and questions

1. Anonymous `.json` is operational today but Reddit's current Data API
   documentation says unidentified traffic may be blocked. It can be withdrawn
   independently of logged-out New Reddit HTML.
2. The New Reddit JavaScript challenge may change. The solver must fail closed
   and be easy to update.
3. Some Reddit page types may not map cleanly to legacy JSON endpoints:
   wiki pages, certain user/profile pages, galleries, live threads, and new
   Shreddit-only features need explicit testing.
4. Quarantined/NSFW content may require `_options`/`over18` cookies or may be
   intentionally unavailable to logged-out users.
5. A shared persistent cookie improves reliability and anonymity-set size but
   lets Reddit correlate fetchaller activity over time. Rotating every request
   would be less linkable but more abusive-looking, more expensive, and less
   reliable; do not do it by default.
6. The only documented durable/compliant API route requires registered OAuth
   and explicit approval, which conflicts with the account-free requirement.
7. If anonymous JSON is removed, the remaining account-free fallback is
   rendering/extracting New Reddit itself. That can still produce compact model
   output after server-side extraction, but network/CPU cost and frontend
   fragility will be substantially higher.

## Summary

New Reddit verification is safer than Android OAuth impersonation because it
uses the public logged-out web security flow Reddit says will remain available,
does not borrow a first-party app identity, and needs no OAuth scopes.

The best architecture is:

```text
Reddit URL
  -> wafer anonymous New Reddit verification/cookie jar
  -> Reddit JSON request
  -> fetchaller selected-field Markdown renderer
  -> maxTokens-aware response
```

This removes Old Reddit, keeps requests account-free, and should reduce model
tokens by roughly 29% on the measured representative thread compared with the
current cleaned Old Reddit output.
