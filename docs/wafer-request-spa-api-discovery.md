# Wafer request: discovering the API call an SPA makes

**Status:** request for wafer. Not implemented in fetchaller, and not
implementable there — fetchaller owns content processing, wafer owns transport.

**Raised from:** building nine job-board clients in fetchaller. Eight were
solved with plain HTTP. The ninth exposed a class of problem that plain HTTP
cannot solve, and the same class cost significant time on four of the other
eight.

## The problem

A growing number of sites serve no content in their HTML and no discoverable
API. The data is fetched by JavaScript at runtime, and the exact shape of that
fetch — its URL, its required headers, its request body, its query grammar — is
knowable only by executing the page's own code.

Finding it currently means one of two things, both bad:

1. **Downloading and reading the site's JS bundles.** This works but is slow
   (tens of multi-hundred-KB files), brittle (the value lives in minified,
   deploy-specific output), and has to be redone whenever the site ships.
2. **Guessing.** Trying candidate endpoint names, parameter names, and body
   shapes until something answers.

Both failed badly in practice, and the failures were expensive because **the
sites do not report the failure**. Four concrete cases from this work:

- A search API returned `HTTP 200` with `totalRecords: 0` because the request
  body omitted a field that only controls *date formatting*. That reads as
  "this employer has no jobs", not "your request was wrong". It was recorded as
  an unsolvable, token-gated API for hours.
- A GraphQL endpoint requires a persisted-query id that rotates on every
  deploy. Observed churn: four distinct ids across roughly nine months. A wrong
  id returns a *valid JSON error*, not a transport failure.
- A REST search endpoint returns `HTTP 200` and a correct total count but omits
  the results array entirely unless an `expand` parameter is present.
- A board's location filter accepts a city name, returns `HTTP 200`, and
  silently ignores it — returning the entire global result set while appearing
  to have filtered.

In each case the site's own front end was making a correct request, in a
browser, a few metres away. We could not see it.

## Why this belongs in wafer, not in each client

Every one of these is a *transport* question: what request does this origin
actually accept? It is not content processing. Solving it per-site means every
new SPA repeats the same bundle archaeology, and the result decays with the
next deploy.

Wafer already owns the browser (`wafer[browser]` ships a real Chrome for
challenge solving). The capability described here is adjacent to something
wafer can already do, but is not currently exposed.

## What success looks like

A caller that has a URL and knows what data the page displays can obtain a
**reproducible plain-HTTP request** that returns that data, without the caller
reading any JavaScript.

Concretely, success means all of the following:

1. **Observation, not guessing.** Given a page URL, a caller can learn which
   requests that page issued while rendering — method, full URL, request
   headers, request body, response status, response content type — and filter
   them to the ones that returned data rather than assets or telemetry.

2. **Replayable without a browser.** Whatever is learned can be re-issued as an
   ordinary wafer request later, in a fresh process, with no browser running.
   The browser is a discovery tool, not a runtime dependency. If a request
   genuinely cannot be replayed headlessly, that fact is reported clearly
   rather than surfacing as an empty result.

3. **Cheap enough to run on failure, not on every call.** The expected usage is
   "our pinned request stopped returning data, re-derive it once and cache the
   result", not "do this per search". A discovery pass taking seconds is fine;
   it must not be on the hot path.

4. **Distinguishes empty from wrong.** The single most damaging failure in this
   domain is a well-formed `HTTP 200` that means "your request was malformed"
   while looking like "there is no data". Success includes being able to tell
   those apart — for example by comparing what the page actually rendered
   against what the replayed request returns, so a silent-empty can be detected
   rather than believed.

5. **Survives a redeploy.** Re-running discovery after the site ships new
   bundles yields the new working request without any code change in the
   caller. This is the property that makes it worth building: it converts a
   recurring manual investigation into a runtime capability.

## What is explicitly not being asked for

- Not a scraper, and not a per-site adapter. No site-specific knowledge should
  live in wafer.
- Not authentication or login. Everything above concerns endpoints that are
  already public and anonymous; the difficulty is the request *shape*, not
  access.
- Not a general browser-automation API. The narrow question is "what request
  does this page make", not "drive this page".

## How we would use it

fetchaller pins a known-good request per board and treats it as a fast path.
When a board's response looks empty, the client would re-derive the request once
via this capability, compare, and either self-heal or report honestly that the
board really is empty. Today it can only do the second half, and only because
each silent-empty trap was found by hand.

## Current workarounds, for context

- Apple: send the required `format` field always; pinned by a test so it cannot
  be removed. Found by reading bundles.
- Meta: rediscover the persisted-query id from JS bundles when the pinned one
  fails; prefer schema.org JSON-LD on posting pages, which is SEO-facing and so
  materially less build-coupled. Search still has no doc_id-free surface — the
  robots-advertised sitemap is complete (its ids matched the API's inventory
  exactly) but costs one page fetch per posting, which cannot back an
  interactive search.
- Oracle Recruiting, Workday, Amazon: send the required parameter, and re-apply
  every filter client-side because the board's own filters cannot be trusted to
  have applied.
