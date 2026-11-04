# Changelog

## 3.8.10 (2026-11-04)

Requires wafer 0.7.12.

- **Large pages no longer time out in the Linux image.** Every parse runs in a
  disposable worker process, and each worker imported BeautifulSoup,
  markdownify, PyMuPDF and wafer for itself before doing any work: about 1.8 s
  per worker in the image, with three workers per HTML page. An amazon.com
  product page took ~10 s and failed `fetch`'s 10 s default. Workers now fork
  from a server that has those imports already loaded and start in ~0.04 s; the
  same page takes ~4.5 s. Across nine page types (Amazon, news, GitHub, a
  Teamtailor board, Vacuum Wars, a PDF, an RSS feed, JSON, Google and DDG
  search) a fetch in the image is 2-7 s faster.
- **A pinned shop currency now survives the browser fallback.** wafer 0.7.11
  carries the currency cookie into the browser, but a challenged request
  rotates and retires its session before failing, which empties the jar, and
  `fetch` pinned the cookie only once per session. The render that follows a
  challenge, and a same-host redirect after a rotation, went out in the IP's
  currency. The pin now goes on before every request. The AliExpress product
  tool's browser fallback (used when MTop fails) sent no pin at all and now
  renders in `SHOPPING_CURRENCY` like MTop does.
- **costco.com searches work again.** Its catalogue is behind Kasada, and wafer
  now has the site's own page sign a refused call in the browser; fetchaller
  never gave its Costco session the browser, so every costco.com search was
  refused. It now does: 3-9 s per search on macOS, 15-25 s in the Linux image,
  so give it `timeout=60` there. Kasada's verdict on the image's browser can
  move, as eBay's does: on 2026-11-04 it passed every cold search there until
  mid-morning, then refused the signed call twice running (wafer 0.7.12) while
  macOS kept passing; the error names the refusal. Kasada refused even the
  page's own signed call from a browser on UTC; wafer now runs a browser that
  would inherit a UTC clock on Europe/London, which it accepts, so the image
  needs no `TZ`. A `TZ` that names UTC is kept, and the error then says to
  change it. A search
  that runs out of time before the browser has signed it now says to raise the
  timeout, and no refusal claims the call was signed. A Costco read's calls
  now share the caller's one timeout instead of taking the whole of it each,
  which overran `fetch`'s own timeout and cut the browser off mid-solve; the
  fallback to Costco's older search service shares it too, so a late refusal
  keeps its reason instead of ending in a bare "timed out".
- **homedepot.com pages read again in the Linux image.** On wafer 0.7.10 and
  0.7.11 the browser's solve there ran to its ~142 s budget and failed:
  wafer set Chrome's languages in a way no stock Chrome does, which
  homedepot.com's Akamai refuses from Linux. wafer 0.7.12 sets them as Chrome
  does, and a cold `/c/` page read in 18.8 s (2026-11-04). Products, search
  and category listings come from the site's gateway and were never affected.
- **Costco's location lookups no longer fail after a costco.com search.** The
  browser that signs a search brings back the page's consent cookie, and
  Costco's location service refuses any request carrying it. On the shared
  session (and the shared cookie cache, across restarts) that broke every
  costco.ca search straight away and every costco.com search once its six-hour
  location cache ran out. The service calls now carry no cookies, as the
  site's own page sends none.
- **The image's browser is Chrome 157 (was Chrome for Testing 154).** wafer
  now presents Chrome 157, and a session that solves anything in the browser
  takes on the browser's version, so the two must match. In alternating runs
  eBay's Akamai refused Linux clients claiming 154 and served ones claiming
  157: with 154, every cold eBay search from the image failed the 10 s default
  (0 of 6); with 157, 5 of 6 passed in 5-8 s (2026-11-03). eBay's verdict
  has moved since for both wafer versions: on 2026-11-04 a cold eBay search
  from the image passed the default 1 of 4 on wafer 0.7.12 (before release)
  and 1 of 4 on 0.7.11 (which twice returned eBay's challenge page as the
  result), and searches the browser cleared took 15-28 s. At `timeout=60`
  0.7.12 passed 3 of 3 cold searches there (6.5-20 s), where 0.7.11 returned
  the challenge page 2 of 2. Give eBay `timeout=60` there; the error says so
  when the default runs out.
- **DuckDuckGo's false "no results" is asked again.** DuckDuckGo sometimes
  answers an ordinary query ("python asyncio tutorial") with its own "No
  results found" page, and fetchaller reported 0 results: 2 of about 25
  searches on 2026-11-03, with 10 results on the next ask. Page 1 is now asked
  once more, with the usual six-second spacing, before an empty answer stands.
- **A challenge that used up the timeout says to raise it.** "Try again" was
  the whole advice, and trying again with the same 10 s fails the same way when
  a browser solve takes 20-45 s. With a browser available and the budget spent,
  the error now says to retry with a larger timeout.

## Earlier releases

- **3.8.9** (2026-11-03): `get_indeed_job` reads postings from the page's own data; Home Depot reviews; Alibaba `/` and `_` queries; costco.com names its Kasada refusal.
- **3.8.8** (2026-11-02): shop currencies (`SHOPPING_COUNTRY`/`SHOPPING_CURRENCY`), UK AliExpress search, Amazon marketplace currency and decimals, reliable Alibaba search; wafer 0.7.11.
- **3.8.7** (2026-10-31): Reddit threads, comments and profiles over the app's GraphQL; wafer 0.7.10.
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
