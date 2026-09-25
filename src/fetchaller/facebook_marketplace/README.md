# Facebook Marketplace GraphQL Client

Facebook Marketplace's visible markup is CSR with obfuscated CSS and is never scraped. `fetch_url()` intercepts Marketplace URLs and reads structured data: the GraphQL API at `https://www.facebook.com/api/graphql/`, and the Relay data a Marketplace page ships with. No authentication required.

## Request form

Every GraphQL call carries `__a=1` and `__comet_req=15`, the logged-out Comet page's own form fields. Without them the search query still answers 200, with no errors and a cursor that counts its matches, but serves none of them (2026-09-25: "bicycle" near Toronto, 12 matched, 0 edges; with the fields, 12 listings). Geocode, listing detail and images answer either way. No page token (`lsd`, `jazoest`) is needed — checked with and without. `decode_graphql_body()` accepts an XSSI prefix and a deferred multi-line answer (the page's current search doc_id streams one).

`withheld_listings_error()` turns "no listings, but the cursor's `c2c.it`/`b2c.it` counts matches" into an error. A genuinely empty search has `it: 0`.

## Reading the page (`page.py`)

A search URL (`/marketplace/{slug}/search?query=...`) is fetched as the page itself. Its `expectedPreloaders` name `CometMarketplaceSearchContentContainerQuery` (doc_id `27517490627932547` on 2026-09-25) with the slug resolved to `buyLocation` and every URL filter applied, and `RelayPrefetchedStreamCache` streams that query's first page of results in the GraphQL shape — one request, identical to facebook.com. If the page ran the search without streaming it, its own doc_id and variables are replayed.

A city browse page (`/marketplace/{slug}/`) runs `MarketplaceCometBrowseFeedLightContainerQuery` and streams it an edge at a time: a `MarketplaceFeedTopPicksUnit` (20 `marketplace_listings`, with `formatted_price.text`) and `MarketplaceFeedGeneralListingObject` nodes (price only as `data.price.amount_with_offset` + currency). That first screen is all the logged-out feed serves (the query over GraphQL with `count=24` answers the same 1 + 6 edges), so it is rendered from the page. `amount_with_offset` is scaled by 100 only for currencies checked live (2500 on a CAD item titled "$25"); others print unscaled and labelled.

Only when the page gives neither (login wall, failed GET) is the search rebuilt: coordinates from the page if it had them, else the slug geocoded as text — the path that put `vancouver` in Washington — and the URL's `minPrice`/`maxPrice`, which are **whole units**, sent as cents.

## Doc IDs

Discovered from Relay preloader data embedded in page source HTML (`preloaderID`/`queryID`/`queryName` triples).

| Query | doc_id | Variables |
|-------|--------|-----------|
| Geocode (`city_street_search`) | `5585904654783609` | `{params: {caller, page_category[], query}}` |
| Search (`marketplace_search`) | `7111939778879383` | `{count, params: {bqf: {query}, browse_request_params: {lat, lng, radius, price bounds}}}` |
| Listing detail (`MarketplacePDPContainerQuery`) | `34344688261796183` | `{targetId, feedbackSource: 56, feedLocation, scale, __relay_internal__pv__*}` |
| Listing images (`MarketplacePDPC2CMediaViewerWithImagesQuery`) | `10059604367394414` | `{targetId}` |

## Modules

- **`graphql.py`** — Low-level GraphQL client. Shared `graphql_request()` with rate limiting (3s base). Variable builders and response parsers for all query types.
- **`page.py`** — Reads a Marketplace page's preloaded search (`parse_search_page`): resolved location, radius, doc_id/variables, the streamed search result, or a browse page's streamed feed.
- **`search.py`** — Search entry point for a Marketplace URL: page first, then the page's own query, then a rebuilt search (see above). Formats results as numbered markdown.
- **`listing.py`** — Listing detail. Two API calls: detail (title, price, description, condition, category, location, delivery, creation time) + images (URIs with dimensions). Formatted as markdown with photo links.
- **`../content/facebook_marketplace.py`** — URL detection and parameter extraction. Matches `/marketplace/*` paths. Excludes reserved slugs (`item`, `create`, `you`, `categories`, `category`, `directory`, `groups`, `saved`).

## Geocoding Quirk

Facebook's geocode API has US bias — bare city names like "vancouver" return Vancouver, WA (not BC). Users can qualify with country name ("vancouver canada", "london england") for correct international results. Abbreviations like "vancouver, BC" don't work. This matches Facebook's own website behavior.

## Listing Detail Response Structure

```
data.viewer.marketplace_product_details_page.target:
  id, marketplace_listing_title, base_marketplace_listing_title
  listing_price: {formatted_amount_zeros_stripped, amount, currency}
  strikethrough_price: {formatted_amount_zeros_stripped} (nullable)
  redacted_description: {text}
  location_text: {text}
  attribute_data: [{attribute_name, value, label}]  (Condition, etc.)
  delivery_types: ["IN_PERSON", "SHIPPING", ...]
  creation_time: unix_timestamp
  is_pending, is_sold, is_live
  marketplaceListingRenderableIfLoggedOut:
    marketplace_listing_category_name
    seo_virtual_category.taxonomy_path[].seo_info.seo_url
```

## Search Response Structure

```
data.marketplace_search.feed_units.edges[].node:
  __typename: "MarketplaceFeedListingStoryObject"
  listing:
    id, marketplace_listing_title
    listing_price: {formatted_amount}
    strikethrough_price: {formatted_amount} (nullable)
    location.reverse_geocode: {city, city_page.display_name}
    condition_text, is_pending
    primary_listing_photo.image.uri
    marketplace_listing_seller: {name, __typename}
```

## Rate Limiting

Uses `facebook_limiter` (3s base, 0.5-1.5s jitter) shared across all GraphQL calls. Listing detail makes 2 calls (detail + images), so a full listing fetch takes ~7-10s.
