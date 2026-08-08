"""Tests for aartech.ca URL detection, page dispatch, and rendering.

aartech ships no prices in its HTML — listings mount a React app over an empty
container and product pages fill empty <template> elements from a script. Every
assertion about a price below is therefore an assertion that we read the site's
data rather than its markup.
"""

import pytest

from fetchaller.aartech.api import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    _normalize_query,
    extract_listing_ids,
    extract_product,
    is_aartech,
    is_aartech_catalogue_url,
    resolve_window,
)
from fetchaller.aartech.search import render_listing, render_product

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestIsAartech:
    def test_www(self):
        assert is_aartech("https://www.aartech.ca/network-cable")

    def test_bare_domain(self):
        assert is_aartech("https://aartech.ca/network-cable")

    def test_other_site(self):
        assert not is_aartech("https://www.infinitecables.com/products/cat6")

    def test_lookalike_domain_is_not_matched(self):
        assert not is_aartech("https://aartech.ca.evil.example/network-cable")


class TestIsAartechCatalogueUrl:
    def test_category(self):
        assert is_aartech_catalogue_url("https://www.aartech.ca/network-cable?limit=24")

    def test_search(self):
        assert is_aartech_catalogue_url("https://www.aartech.ca/search?q=cat6")

    def test_product_with_html_suffix(self):
        assert is_aartech_catalogue_url("https://www.aartech.ca/gen-50881102/genesis-cat5e.html")

    def test_bare_slug_product_is_still_a_candidate(self):
        """A product and a category are the same URL shape here.

        ``/intermatic-dt200lt`` is a product and ``/network-cable`` a category,
        so neither may be excluded by shape — reading the page decides.
        """
        assert is_aartech_catalogue_url("https://www.aartech.ca/intermatic-dt200lt")

    def test_home_page_excluded(self):
        assert not is_aartech_catalogue_url("https://www.aartech.ca/")

    def test_content_page_excluded(self):
        assert not is_aartech_catalogue_url("https://www.aartech.ca/page/shipping")

    def test_cart_excluded(self):
        assert not is_aartech_catalogue_url("https://www.aartech.ca/cart")

    def test_exclusions_match_whole_segments_not_bare_prefixes(self):
        """`/cart` must not swallow a real category whose slug starts with it.

        A wrongly excluded category falls to the HTML path, which has no prices
        — silently, which is the whole failure this module exists to prevent.
        """
        assert is_aartech_catalogue_url("https://www.aartech.ca/cartridge-heaters")
        assert is_aartech_catalogue_url("https://www.aartech.ca/accountancy-supplies")

    def test_trailing_slash_does_not_defeat_an_exclusion(self):
        assert not is_aartech_catalogue_url("https://www.aartech.ca/page/")


# ---------------------------------------------------------------------------
# Page dispatch
# ---------------------------------------------------------------------------


class TestExtractListingIds:
    def test_category_page_ids(self):
        html = "<div id='search'></div><script>var searchId = '3';var systemSearchFilterId = '198';</script>"
        assert extract_listing_ids(html) == ("3", "198")

    def test_keyword_search_has_no_filter(self):
        html = "<script>var searchId = '2';</script>"
        assert extract_listing_ids(html) == ("2", None)

    def test_product_page_has_no_search_app(self):
        assert extract_listing_ids("<html><body>no search app</body></html>") is None


class TestExtractProduct:
    def test_reads_the_embedded_blob(self):
        html = (
            "<script>var detailProduct = new window.catalog.ProductPreview("
            '{"part_number":"GEN-1","title":"Cable","price":"9.99"}'
            ");</script>"
        )
        blob = extract_product(html)
        assert blob is not None
        assert blob["part_number"] == "GEN-1"
        assert blob["price"] == "9.99"

    def test_listing_page_has_no_product_blob(self):
        assert extract_product("<script>var searchId = '3';</script>") is None

    def test_blob_without_a_part_number_is_rejected(self):
        html = "<script>new window.catalog.ProductPreview({\"exists\":false});</script>"
        assert extract_product(html) is None


class TestNormalizeQuery:
    def test_absent_limit_is_supplied(self):
        assert _normalize_query("", "1") == f"limit={DEFAULT_LIMIT}"

    def test_callers_limit_is_honoured(self):
        assert _normalize_query("limit=5", "1") == "limit=5"

    def test_non_term_params_pass_through_untouched(self):
        # The search app pushes its own parameters onto the address bar, so the
        # rest of the query string is already in the API's vocabulary.
        assert _normalize_query("page=2", "1") == "page=2&limit=24"

    def test_oversized_limit_is_capped(self):
        assert _normalize_query(f"limit={MAX_LIMIT * 10}", "1") == f"limit={MAX_LIMIT}"

    def test_junk_limit_falls_back_to_the_default(self):
        assert _normalize_query("limit=all", "1") == f"limit={DEFAULT_LIMIT}"


class TestSearchTermIsActuallyApplied:
    """`?q=` is ignored by the API and must never be forwarded as-is.

    Every spelling of a plain keyword param returns the full 3,432-product
    catalogue, which renders as a perfectly plausible "Search Results" page for
    a term nobody searched. The site's own autocomplete navigates to
    `search?controls[{searchControlId}]=term`, which really does filter.
    """

    def test_plain_term_is_rewritten_to_the_control(self):
        assert _normalize_query("q=cat6", "1") == "controls[1]=cat6&limit=24"

    def test_every_natural_spelling_is_rewritten(self):
        for key in ("q", "query", "search", "keyword", "keywords", "term"):
            assert _normalize_query(f"{key}=cat6", "1") == "controls[1]=cat6&limit=24"

    def test_control_id_from_the_page_is_used(self):
        assert _normalize_query("q=cat6", "7") == "controls[7]=cat6&limit=24"

    def test_an_explicit_control_param_is_left_alone(self):
        assert _normalize_query("controls[1]=cat6", "1") == "controls[1]=cat6&limit=24"

    def test_a_term_that_cannot_be_applied_raises_rather_than_lying(self):
        with pytest.raises(LookupError):
            _normalize_query("q=cat6", None)

    def test_a_termless_listing_needs_no_control(self):
        # `/search` with no term is a legitimate browse-everything page.
        assert _normalize_query("page=2", None) == "page=2&limit=24"


class TestResolveWindow:
    def test_defaults(self):
        assert resolve_window("") == (1, DEFAULT_LIMIT)

    def test_page_and_limit(self):
        assert resolve_window("page=3&limit=5") == (3, 5)

    def test_limit_is_clamped_like_the_request(self):
        assert resolve_window(f"limit={MAX_LIMIT * 10}") == (1, MAX_LIMIT)

    def test_junk_values_fall_back(self):
        assert resolve_window("page=abc&limit=xyz") == (1, DEFAULT_LIMIT)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _item(**overrides):
    item = {
        "shortDescription": "Genesis Cat5e UTP 1000FT",
        "partNumber": "GEN-50881102",
        "manufacturer": {"title": "Genesis"},
        "price": {"amount": 479.95, "amountText": "$479.95 CAD", "listPrice": 479.95, "listPriceText": "$479.95 CAD"},
        "stock": {"display": {"message": "In Stock"}},
        "url": "https://www.aartech.ca/gen-50881102",
    }
    item.update(overrides)
    return item


class TestRenderListing:
    def test_price_and_stock_are_shown(self):
        out = render_listing({"items": [_item()], "totalHits": 1}, "https://www.aartech.ca/network-cable")
        assert "$479.95 CAD" in out
        assert "In Stock" in out
        assert "GEN-50881102" in out

    def test_sale_price_strikes_through_the_list_price(self):
        out = render_listing(
            {
                "items": [
                    _item(
                        price={
                            "amount": 159.99,
                            "amountText": "$159.99 CAD",
                            "listPrice": 325.95,
                            "listPriceText": "$325.95 CAD",
                        }
                    )
                ],
                "totalHits": 1,
            },
            "https://www.aartech.ca/network-cable",
        )
        assert "~~$325.95 CAD~~ **$159.99 CAD**" in out

    def test_bounded_window_states_what_it_could_not_reach(self):
        """A page of 3 out of 3,432 must never read as the whole catalogue."""
        out = render_listing({"items": [_item()] * 3, "totalHits": 3432}, "https://www.aartech.ca/search?q=cat6")
        assert "showing 3 of 3,432" in out
        assert "3,429 further products not shown" in out

    def test_remaining_count_accounts_for_the_page_offset(self):
        """The last page must not report the rows behind it as still to come."""
        out = render_listing(
            {"items": [_item()] * 4, "totalHits": 14},
            "https://www.aartech.ca/network-cable?page=3&limit=5",
        )
        assert "showing 11-14 of 14" in out
        assert "not shown" not in out

    def test_middle_page_counts_only_what_is_ahead(self):
        out = render_listing(
            {"items": [_item()] * 5, "totalHits": 14},
            "https://www.aartech.ca/network-cable?page=2&limit=5",
        )
        assert "showing 6-10 of 14" in out
        assert "4 further products not shown" in out
        assert "page=3" in out

    def test_pagination_hint_is_a_usable_url(self):
        """A query-less URL needs `?page=2`, not `&page=2`."""
        out = render_listing({"items": [_item()] * 2, "totalHits": 50}, "https://www.aartech.ca/network-cable")
        assert "add ?page=2 to this URL" in out
        assert "&page=" not in out

    def test_pagination_hint_appends_when_a_query_exists(self):
        out = render_listing({"items": [_item()] * 2, "totalHits": 50}, "https://www.aartech.ca/network-cable?limit=2")
        assert "add &page=2 to this URL" in out

    def test_a_scalar_where_a_field_should_be_does_not_crash(self):
        out = render_listing(
            {"items": [_item(manufacturer="Genesis", price="oops", stock=None)], "totalHits": 1},
            "https://www.aartech.ca/x",
        )
        assert "GEN-50881102" in out

    def test_complete_listing_claims_no_missing_rows(self):
        out = render_listing({"items": [_item()] * 2, "totalHits": 2}, "https://www.aartech.ca/network-cable")
        assert "2 products" in out
        assert "not shown" not in out

    def test_quote_only_item_does_not_invent_a_price(self):
        out = render_listing({"items": [_item(is_quote_only=True)], "totalHits": 1}, "https://www.aartech.ca/x")
        assert "Price on request" in out
        assert "$479.95" not in out

    def test_volume_breaks_are_listed(self):
        out = render_listing(
            {
                "items": [
                    _item(
                        quantityBreaks={
                            "EACH": [
                                {"quantityBreak": 3, "price": 34.99, "formattedPrice": "$34.99 CAD"},
                                {"quantityBreak": 20, "price": 32.99, "formattedPrice": "$32.99 CAD"},
                            ]
                        }
                    )
                ],
                "totalHits": 1,
            },
            "https://www.aartech.ca/x",
        )
        assert "Volume: 3+ $34.99 CAD, 20+ $32.99 CAD" in out

    def test_empty_listing_says_so(self):
        out = render_listing({"items": [], "totalHits": 0}, "https://www.aartech.ca/search?q=zzz")
        assert "No products found." in out


class TestRenderProduct:
    def _blob(self, **overrides):
        blob = {
            "title": "Genesis 24/4pr Cat5e UTP Wire",
            "part_number": "GEN-50881102",
            "manufacturer": {"name": "Genesis"},
            "uoms": [{"code": "EACH", "text": "$479.95 CAD", "listPrice": "$479.95 CAD", "packSize": 1}],
            "stock": {"display": {"message": "In Stock"}},
            "upc": ["619172054928"],
            "default_category": {"name": "Network Cable"},
            "HTML_description": "<p>When quality counts, choose Genesis&nbsp;wire.</p>",
            "url": "https://www.aartech.ca/gen-50881102",
        }
        blob.update(overrides)
        return blob

    def test_price_is_rendered_from_the_blob(self):
        out = render_product(self._blob(), "https://www.aartech.ca/gen-50881102")
        assert "**$479.95 CAD**" in out
        assert "Availability: In Stock" in out
        assert "Part #: GEN-50881102" in out
        assert "Brand: Genesis" in out

    def test_description_html_is_flattened(self):
        out = render_product(self._blob(), "https://www.aartech.ca/gen-50881102")
        assert "When quality counts, choose Genesis wire." in out
        assert "<p>" not in out

    def test_quote_only_product_shows_no_price(self):
        out = render_product(self._blob(is_quote_only=True), "https://www.aartech.ca/x")
        assert "Price on request" in out
        assert "479.95" not in out

    def test_hidden_price_is_reported_not_guessed(self):
        out = render_product(self._blob(isPriceVisible=False), "https://www.aartech.ca/x")
        assert "Price not shown" in out
        assert "479.95" not in out

    def test_volume_breaks_are_listed(self):
        out = render_product(
            self._blob(
                quantity_breaks=[
                    {"quantityBreak": 10, "formattedPrice": "$13.99 CAD"},
                    {"quantityBreak": 25, "formattedPrice": "$13.49 CAD"},
                ]
            ),
            "https://www.aartech.ca/x",
        )
        assert "Volume: 10+ $13.99 CAD, 25+ $13.49 CAD" in out

    def test_noise_specs_are_dropped_and_real_ones_kept(self):
        out = render_product(
            self._blob(
                custom_fields=[
                    {"display_name": "Case Qty", "value": "0.00"},
                    {"display_name": "Product Short Description", "value": "duplicate"},
                    {"display_name": "Conductor Gauge", "value": "24 AWG"},
                ]
            ),
            "https://www.aartech.ca/x",
        )
        assert "Conductor Gauge: 24 AWG" in out
        assert "Case Qty" not in out
        assert "Product Short Description" not in out

    def test_falls_back_to_the_raw_price_when_no_uom_row_exists(self):
        out = render_product(self._blob(uoms=[], price="42.50"), "https://www.aartech.ca/x")
        assert "$42.50 CAD" in out
