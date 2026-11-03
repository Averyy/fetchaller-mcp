# Changelog

## 3.8.8 (2026-11-02)

Requires wafer 0.7.11. Started from Ribetzer's PR #2.

- **AliExpress search works for UK sessions.** A UK session prices every item
  with the fullwidth pound sign (`￡0.99`, U+FFE1), which the price check did
  not recognise, so every search answered "Could not extract product data".
  Fullwidth `＄￡￥￦` are now read and shown as `$£¥₩`. A GBP product read also
  stops labelling its raw amount "USD".
- **New optional `SHOPPING_COUNTRY` / `SHOPPING_CURRENCY`** (e.g. `GB`/`GBP`)
  pin AliExpress search and product prices (and their VAT) and Alibaba product
  pages to a ship-to country and currency, instead of whatever the server's IP
  suggests. Unset, nothing changes. Alibaba search is not affected. With
  wafer 0.7.11 the setting also holds on pages read through the
  challenge-solving browser, which had come back in the IP's currency.
- **Amazon is read in each marketplace's own currency.** amazon.com now
  quotes USD wherever the server is (it had quoted a Canadian IP in converted
  CAD), amazon.co.uk GBP, and so on.
- **Amazon prices missing their decimal point are fixed.** Carousel cards
  shipped $82.49 as `$8249`, which rendered as `$8249$8249`, 100 times too high.
  Every Amazon price now renders once, with its decimal.
- **AliExpress first-order "welcome deal" prices are labelled**
  `(new-shopper price, £X off)`, and abbreviated sold counts (`8K+ sold`) are
  kept.
- **Alibaba search works reliably.** About half its searches failed with
  "Could not extract product data": Alibaba sends `/trade/search` requests at
  random to an app shell with no results. Search now reads Alibaba's keyword
  listing page, one request with no redirects (8 of 8 live), and falls back
  to `/trade/search` with retries only for queries the listing page cannot
  carry.
- **Alibaba `min_price`/`max_price` now filter.** They were sent under names
  Alibaba ignores. **Alibaba has no price sort**, so `price_asc`/`price_desc`
  now order the returned page by lowest price and say so, and the new
  `orders` sort uses Alibaba's own "most sold".
- **An AliExpress "No search results" page is an empty result only when it
  counts no matches.** Price-sorted searches with a price filter can answer it
  while counting dozens of matches; that is now an error naming the count and
  the sorts that list them, not "No products found".

## Earlier releases

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
