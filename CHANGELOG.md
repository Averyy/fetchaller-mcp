# Changelog

## 3.8.7 (2026-10-31)

Requires wafer 0.7.10.

Since about 2026-10-20 Reddit refuses a thread's JSON, comment streams,
`/api/info`, `/api/morechildren`, `/user/<name>/about`, `/r/all` and
multi-post `/by_id` to every logged-out client. fetchaller now reads them the
way the Reddit app does, over its anonymous GraphQL.

- **Threads:** post from `/by_id`, comments from `PostComments`. Permalinks
  keep the selected comment and its parents; "load more" links carry Reddit's
  own continuation cursor.
- **Profiles:** `UserProfile` (post/comment/total karma, created date, employee,
  avatar, title, description, post and comment counts).
- **Several posts or comments at once** (`/by_id` lists, related-post details,
  `morechildren`): one `PostsByIds` / `LiveCommentsByIds` batch, with missing
  IDs named.
- **New `fetch` options:** `redditComments` (`fast` default: one read of up to
  200 comments; `more` follows the continuations, up to 5 extra reads within
  `maxTokens` and the timeout) and `redditSort` (`best` default, `top`, `new`,
  `old`, `controversial`, `qa`).
- **Refused reads are final:** reported as Reddit's answer at once, with no
  retry, no backoff, and never the browser. Comment streams and `/r/all` say
  Reddit refuses them; global random-rising reads `/rising`.
- No longer available from Reddit to logged-out clients: a comment's
  "controversial" marker, and an account's email-verified, Premium and
  suspended flags.
- **Fixes:** deleted comments (which Reddit sends without an ID) no longer
  reject a page; the parity gate's multireddit discovery passes the required
  search `provider`.

## Earlier releases

- **3.8.6** (2026-10-25): unified `search` tool; wafer 0.7.9.
- **3.8.5** (2026-10-09): wafer 0.7.7.
- **3.8.4** (2026-10-08): wafer 0.7.6; Home Depot pages in the Linux image; Home Depot gateway refusals fixed.
- **3.8.3** (2026-10-07): Home Depot without a browser; Costco catalogue search; wafer 0.7.3.
- **3.8.2** (2026-09-28): wafer 0.7.0 Reddit app route; strict Reddit gate passable again.
- **3.8.1** (2026-09-26): wafer 0.6.2; FCC filings render again.
- **3.8.0** (2026-09-25): Teamtailor boards; Facebook Marketplace search restored; wafer 0.6.1.
- **3.7.0** (2026-09-12): GC Jobs; Ashby hosted-board fallback; Vacuum Wars routing.
- **3.6.4** (2026-09-11): Vacuum Wars verified against the live site.
- **3.6.3** (2026-09-07): RedFlagDeals cleanup fixes.
- **3.6.2** (2026-09-07): RedFlagDeals threads readable again; wafer 0.6.0.
- **3.6.1** (2026-09-07): realtor.ca loads its front page first.
- **3.6.0** (2026-09-07): gojobs, Job Bank and Indeed boards; wafer 0.5.0.
- **3.5.4** (2026-08-08): UniFi specs from the page payload.
- **3.5.3** (2026-08-08): truncated listings count what they dropped; aartech.ca.
- **3.5.2** (2026-08-03): job location subdivision implies its country.
- **3.5.1** (2026-08-03): job tools report the pool examined.
- **3.5.0** (2026-08-02): job tools' `limit` sizes output only; scoping fixes.
- **3.4.1** (2026-08-02): job-board reporting fixes.
- **3.4.0** (2026-08-02): eight tech-careers tools; wafer 0.4.8.
- **3.3.4** (2026-07-31): wafer 0.4.6.
- **3.3.3** (2026-07-31): version bump.
- **3.3.2** (2026-07-31): MCP endpoint URL accepted as OAuth resource indicator.
- **3.3.1** (2026-07-30): Reddit read anonymously only; credential path removed.
- **3.3.0** (2026-07-29): request-handling hardening.
- **3.2.4** (2026-07-26): DNS false blocks, silent search failures, Docker solver fixed.
- **3.2.3** (2026-07-19): wafer 0.3.3 for the Reddit cold-session fix.
- **3.2.2** (2026-07-18): SSRF hardening; cache and queue fixes; faster HTML path.
- **3.2.1** (2026-07-18): wafer 0.3.2 APIs; secondary-fetch SSRF hardening.
- **3.2.0** (2026-05-11): Ashby and Gem board-index APIs; dependency upgrades.
- **3.1.1** (2026-04-05): wafer 0.2.0.
- **3.1.0** (2026-02-26): wafer replaces curl_cffi.
- **3.0.0** (2026-02-10): generic forum support and site-specific cleanup.
- **2.1.0** (2026-02-03): rewritten from Node.js to Python.
