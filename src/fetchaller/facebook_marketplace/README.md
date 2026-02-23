# Facebook Marketplace GraphQL Client

Facebook Marketplace is 100% CSR with obfuscated CSS — HTML scraping is not viable. `fetch_url()` intercepts Marketplace URLs and routes to the GraphQL API at `https://www.facebook.com/api/graphql/`. No authentication required.

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
- **`search.py`** — Search entry point. Extracts query, location slug, and price filters (`minPrice`/`maxPrice`) from URL. Geocodes location to lat/lng. Formats results as numbered markdown.
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
