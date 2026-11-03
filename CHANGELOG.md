# Changelog

## 3.8.9 (2026-11-03)

Requires wafer 0.7.11.

- **`get_indeed_job` works again.** Indeed stopped putting the posting's
  JSON-LD on its job pages, so every posting answered "carried no JobPosting
  data". Postings are now read from the page's own data: title, employer,
  location, employment type, pay band, description and posting age.
- **Indeed no longer publishes an expiry date** on posting pages, so none is
  shown unless a page carries one; a posting Indeed marks expired now says so.
- **Home Depot review pages work again.** The gateway stopped answering the
  reviews query for the experience fetchaller named; it now asks as the site's
  own reviews page does.
- **Alibaba searches containing `/` or `_`** fall back, as a last resort, to
  Alibaba's listing page with those characters read as spaces, and say so.
- **costco.com search names its refusal.** costco.com's catalogue now sits
  behind Kasada bot protection and refuses requests without its per-request
  tokens. The error says so instead of "neither service answered". costco.ca
  is unaffected; the costco.com fix is wafer's.

## Earlier releases

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
