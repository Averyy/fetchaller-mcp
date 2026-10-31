"""Unit tests for AliExpress search module."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

import fetchaller.aliexpress.search as search_mod
from fetchaller.aliexpress.search import (
    _build_search_url,
    _clear_product_snapshots,
    _parse_search_html,
    get_recent_product_snapshot,
    search_aliexpress,
)
from fetchaller.content.aliexpress import (
    _format_search_product,
    extract_init_data,
    extract_search_products,
    format_search_results,
    valid_search_product,
)


def _search_html(products: object) -> str:
    init_data = {
        "data": {
            "root": {
                "fields": {
                    "mods": {"itemList": {"content": products}},
                    "pageInfo": {"totalResults": 1, "page": 1},
                }
            }
        }
    }
    return (
        "/*!-->init-data-start--*/"
        f"_dida_config_._init_data_= {{ data: {json.dumps(init_data)} }}"
        "/*!-->init-data-end--*/"
    )


# One item of a live UK-session search page ("ld2450", 2026-10-31), trimmed to
# the fields the parser reads. Every price carries the fullwidth pound sign.
_LIVE_GBP_PRODUCT = {
    "productId": "1005006121923099",
    "title": {"displayTitle": "24G HLK-LD2450 MM Wave Human Body Motion Tracking Radar Sensor Module"},
    "prices": {
        "currencySymbol": "￡",
        "originalPrice": {"currencyCode": "GBP", "minPrice": 3.71, "formattedPrice": "￡3.71", "cent": 371},
        "salePrice": {"discount": 73, "currencyCode": "GBP", "minPrice": 0.99, "formattedPrice": "￡0.99", "cent": 99},
        "taxRate": "0.200000",
    },
    "evaluation": {"starRating": 4.9},
    "trade": {"tradeDesc": "4,000+ sold"},
    "itemType": "productV3",
}


class TestExtractInitData:
    """_init_data_ JSON extraction from HTML."""

    def test_comment_markers(self):
        """Extract JSON using /*!-->init-data-start--*/ markers."""
        data = {"key": "value", "nested": {"a": 1}}
        html = (
            "<html><script>"
            "/*!-->init-data-start--*/"
            f"window._dida_config_._init_data_= {{ data: {json.dumps(data)} }}"
            "/*!-->init-data-end--*/"
            "</script></html>"
        )
        result = extract_init_data(html)
        assert result == data

    def test_direct_assignment(self):
        """Extract JSON using _dida_config_._init_data_= assignment."""
        data = {"products": [1, 2, 3]}
        html = f"<html><script>_dida_config_._init_data_= {{ data: {json.dumps(data)} }}</script></html>"
        result = extract_init_data(html)
        assert result == data

    def test_nested_braces(self):
        """Brace counting correctly handles nested objects."""
        data = {"a": {"b": {"c": {"d": 1}}}, "e": [{"f": 2}]}
        html = (
            f"/*!-->init-data-start--*/_dida_config_._init_data_= {{ data: {json.dumps(data)} }}/*!-->init-data-end--*/"
        )
        result = extract_init_data(html)
        assert result == data

    def test_no_init_data_returns_none(self):
        """HTML without _init_data_ returns None."""
        html = "<html><body>Regular page</body></html>"
        assert extract_init_data(html) is None

    def test_invalid_json_returns_none(self):
        """Malformed JSON inside markers returns None."""
        html = (
            "/*!-->init-data-start--*/_dida_config_._init_data_= { data: {invalid json here} }/*!-->init-data-end--*/"
        )
        assert extract_init_data(html) is None

    def test_ignores_init_data_in_conditionals(self):
        """_init_data_ appearing without markers or assignment should not match."""
        html = "<script>if (window._init_data_) { console.log('exists'); }</script><body>Content</body>"
        assert extract_init_data(html) is None

    @pytest.mark.parametrize(
        "number",
        ["1e400", "-1e400", "NaN", "Infinity", "9" * 5_000],
    )
    def test_rejects_non_finite_or_oversized_json_numbers(self, number):
        html = (
            "/*!-->init-data-start--*/"
            f'_dida_config_._init_data_= {{ data: {{"number": {number}}} }}'
            "/*!-->init-data-end--*/"
        )

        assert extract_init_data(html) is None

    def test_marker_payload_over_two_megabytes_is_rejected_before_scan(self):
        html = (
            "/*!-->init-data-start--*/"
            '_dida_config_._init_data_= { data: {"padding":"' + ("x" * 2_000_001) + '"} }'
            "/*!-->init-data-end--*/"
        )

        started = time.monotonic()
        result = extract_init_data(html)

        assert result is None
        assert time.monotonic() - started < 0.5


class TestProductSearchSnapshots:
    def setup_method(self):
        _clear_product_snapshots()

    def teardown_method(self):
        _clear_product_snapshots()

    def test_parse_retains_isolated_exact_product_snapshot(self):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "USB C Hub 8 in 1"},
            "prices": {
                "salePrice": {
                    "formattedPrice": "US $12.99",
                    "discount": "50",
                },
                "originalPrice": {"formattedPrice": "US $25.98"},
            },
            "evaluation": {"starRating": "4.8"},
            "trade": {"tradeDesc": "5000+ sold"},
        }

        assert _parse_search_html(_search_html([product]), "usb hub") is not None
        snapshot = get_recent_product_snapshot("1005006367324382")

        assert snapshot == {
            "_source": "search_listing",
            "product_id": "1005006367324382",
            "title": "USB C Hub 8 in 1",
            "sale_price": "US $12.99",
            "original_price": "US $25.98",
            "discount": "50",
            "new_shopper_price": False,
            "new_shopper_saving": "",
            "rating": "4.8",
            "orders": "5000+",
        }
        snapshot["title"] = "mutated"
        assert (
            get_recent_product_snapshot("1005006367324382")["title"]
            == "USB C Hub 8 in 1"
        )
        assert get_recent_product_snapshot("1005006367324383") is None

    def test_live_gbp_page_parses_with_standard_pound_sign(self):
        result = _parse_search_html(_search_html([_LIVE_GBP_PRODUCT]), "ld2450")

        assert result is not None
        assert "Price: £0.99 (was £3.71) -73%" in result["content"]
        assert "￡" not in result["content"]
        assert "new-shopper" not in result["content"]
        snapshot = get_recent_product_snapshot("1005006121923099")
        assert snapshot["sale_price"] == "£0.99"
        assert snapshot["original_price"] == "£3.71"
        assert snapshot["new_shopper_price"] is False

    def test_welcome_deal_price_is_labelled_new_shopper(self):
        # The same item's card on another UK session, 2026-10-31: the first-order
        # price, no original price, and only selling-point tags saying so.
        product = {
            **_LIVE_GBP_PRODUCT,
            "prices": {
                "currencySymbol": "￡",
                "salePrice": {"discount": 73, "currencyCode": "GBP", "minPrice": 0.99, "formattedPrice": "￡0.99"},
            },
            "trade": {"tradeDesc": "4K+ sold"},
            "sellingPoints": [
                {"sellingPointTagId": "m0000142", "materials": [{"type": "TEXT", "value": "Free shipping"}]},
                {
                    "sellingPointTagId": "welcomedeal_save_atm",
                    "materials": [
                        {
                            "materialCode": "atm_text",
                            "type": "TEXT",
                            "value": '<span style="color:#F00633;"><strong>￡1.24</strong> saved</span>',
                        },
                        {"materialCode": "background_color", "type": "TEXT", "value": "#FEEBEF"},
                    ],
                },
                {"sellingPointTagId": "welcomedeal_test", "materials": [{"type": "TEXT", "value": "New shoppers - ￡1.24"}]},
            ],
        }

        result = _parse_search_html(_search_html([product]), "ld2450")

        assert "Price: £0.99 -73% (new-shopper price, £1.24 off)" in result["content"]
        assert "4K+ sold" in result["content"]
        snapshot = get_recent_product_snapshot("1005006121923099")
        assert snapshot["new_shopper_price"] is True
        assert snapshot["new_shopper_saving"] == "£1.24"

    @pytest.mark.parametrize(
        "saved_text",
        ["<b>lots</b> saved", "￡0 saved", "-￡1.24 saved", "￡1.24", 7, "￡1.24 saved" + "x" * 1_000],
    )
    def test_unreadable_welcome_deal_saving_keeps_the_bare_label(self, saved_text):
        product = {
            **_LIVE_GBP_PRODUCT,
            "sellingPoints": [
                {"sellingPointTagId": "welcomedeal_save_atm", "materials": [{"type": "TEXT", "value": saved_text}]},
            ],
        }

        result = _parse_search_html(_search_html([product]), "ld2450")

        assert "(new-shopper price)" in result["content"]
        assert " off)" not in result["content"]

    @pytest.mark.parametrize(
        "selling_points",
        [None, "welcomedeal", [None, "welcomedeal_test"], [{"sellingPointTagId": 7}], [{"sellingPointTagId": "m0000377"}]],
    )
    def test_malformed_or_unrelated_selling_points_are_not_a_new_shopper_price(self, selling_points):
        product = {**_LIVE_GBP_PRODUCT, "sellingPoints": selling_points}

        result = _parse_search_html(_search_html([product]), "ld2450")

        assert "new-shopper" not in result["content"]

    def test_zero_result_page_is_an_empty_answer_not_a_failure(self):
        # "AS7343 spectral sensor module", £3-15, sorted by price (2026-10-31):
        # no itemList mod at all, and totalResults counting the unfiltered 54.
        init_data = {
            "data": {
                "root": {
                    "fields": {
                        "mods": {"bigSaleBanner": {}, "searchRefineFilters": {}, "searchTips": {}, "sortBar": {}},
                        "pageInfo": {"searchResultType": "zero_result", "totalResults": 54, "page": 1},
                    }
                }
            }
        }
        html = (
            "/*!-->init-data-start--*/"
            f"_dida_config_._init_data_= {{ data: {json.dumps(init_data)} }}"
            "/*!-->init-data-end--*/"
        )

        result = _parse_search_html(html, "AS7343 spectral sensor module")
        fetched = extract_search_products(html, "https://www.aliexpress.com/w/wholesale-AS7343.html?minPrice=3")

        assert result == {"content": 'Search: "AS7343 spectral sensor module" | page 1 | 0 results\n\nNo products found.'}
        assert fetched == 'Search: "AS7343" | page 1 | 0 results\n\nNo products found.'

    def test_invalid_offer_is_not_retained(self):
        invalid = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "USB C Hub"},
            "prices": {"salePrice": {"formattedPrice": "not a price"}},
        }

        assert _parse_search_html(_search_html([invalid]), "usb hub") is None
        assert get_recent_product_snapshot("1005006367324382") is None

    def test_snapshot_expires_after_bounded_window(self):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "USB C Hub"},
            "prices": {"salePrice": {"formattedPrice": "US $12.99"}},
        }
        with patch.object(search_mod.time, "monotonic", return_value=100.0):
            assert (
                _parse_search_html(_search_html([product]), "usb hub")
                is not None
            )
        with patch.object(
            search_mod.time,
            "monotonic",
            return_value=100.0 + search_mod._PRODUCT_SNAPSHOT_TTL + 1,
        ):
            assert get_recent_product_snapshot("1005006367324382") is None


class TestFormatProduct:
    """Product formatting for search results."""

    def test_formats_complete_product(self):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "USB C Hub 8 in 1"},
            "prices": {
                "salePrice": {"formattedPrice": "US $12.99", "discount": "50"},
                "originalPrice": {"formattedPrice": "US $25.98"},
            },
            "evaluation": {"starRating": "4.8"},
            "trade": {"tradeDesc": "5000+ sold"},
        }
        result = _format_search_product(1, product)
        assert "1. USB C Hub 8 in 1" in result
        assert "US $12.99" in result
        assert "(was US $25.98)" in result
        assert "★4.8" in result
        assert "5000+ sold" in result
        assert "aliexpress.com/item/1005006367324382.html" in result

    def test_formats_minimal_product(self):
        product = {
            "productId": "123456789",
            "title": {"displayTitle": "Basic Widget"},
            "prices": {},
            "evaluation": {},
            "trade": {},
        }
        result = _format_search_product(1, product)
        assert "1. Basic Widget" in result
        # Should not crash on missing fields

    def test_string_title(self):
        """Handle products where title is a plain string instead of dict."""
        product = {"productId": "123", "title": "Plain Title", "prices": {}, "evaluation": {}, "trade": {}}
        result = _format_search_product(1, product)
        assert "Plain Title" in result

    @pytest.mark.parametrize(
        "price",
        [
            float("inf"),
            float("-inf"),
            float("nan"),
            "1e400",
            "-1e400",
            "$-1.00",
            "-$1.00",
            "USD -1",
        ],
    )
    def test_non_finite_price_never_forms_a_valid_product(self, price):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "Real Widget"},
            "prices": {"salePrice": {"minPrice": price}},
        }

        assert valid_search_product(product) is False

    @pytest.mark.parametrize("title", ["²", "Ⅻ", "①", "²Ⅻ①"])
    def test_unicode_numeric_title_is_not_a_valid_product(self, title):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": title},
            "prices": {"salePrice": {"formattedPrice": "US $1.00"}},
        }

        assert valid_search_product(product) is False

    @pytest.mark.parametrize("title", ["Café Widget", "防水开关"])
    def test_unicode_letter_title_is_a_valid_product(self, title):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": title},
            "prices": {"salePrice": {"formattedPrice": "US $1.00"}},
        }

        assert valid_search_product(product) is True

    @pytest.mark.parametrize(
        "price",
        [
            "C$ 6.91",
            "CA $6.91",
            "AU$ 6.91",
            "HK $6.91",
            "NZ$6.91",
            "SG $ 6.91",
            "S$ 6.91",
        ],
    )
    def test_live_regional_dollar_prefix_is_a_valid_price(self, price):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "Real USB Cable"},
            "prices": {"salePrice": {"formattedPrice": price}},
        }

        assert valid_search_product(product) is True

    def test_live_gbp_item_is_a_valid_product(self):
        assert valid_search_product(_LIVE_GBP_PRODUCT) is True

    @pytest.mark.parametrize(
        "price",
        [
            "X$ 6.91",
            "Canada $6.91",
            "C dollars 6.91",
            "Minimum C$ 6.91 pieces",
            "1C$ 6.91",
            "@C$ 6.91",
            "C$ -6.91",
            "-C$ 6.91",
        ],
    )
    def test_malformed_regional_dollar_lookalike_is_not_a_price(self, price):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "Real USB Cable"},
            "prices": {"salePrice": {"formattedPrice": price}},
        }

        assert valid_search_product(product) is False

    @pytest.mark.parametrize(
        ("original_price", "discount"),
        [
            ("US $-5", "-50"),
            ("Minimum order 100 pieces", "50%"),
            ("US $0", float("inf")),
            ("US $NaN", True),
            ("US $-5", 10**3_999),
        ],
    )
    def test_invalid_optional_price_metadata_is_omitted(self, original_price, discount):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "Real Widget"},
            "prices": {
                "salePrice": {
                    "formattedPrice": "US $1",
                    "discount": discount,
                },
                "originalPrice": {"formattedPrice": original_price},
            },
        }

        rendered = _format_search_product(1, product)

        assert "Price: US $1" in rendered
        assert "(was " not in rendered
        assert "%" not in rendered

    def test_valid_optional_price_metadata_is_rendered(self):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "Real Widget"},
            "prices": {
                "salePrice": {
                    "formattedPrice": "US $1",
                    "discount": "12.5",
                },
                "originalPrice": {"formattedPrice": "US $5"},
            },
        }

        rendered = _format_search_product(1, product)

        assert "Price: US $1 (was US $5) -12.5%" in rendered

    @pytest.mark.parametrize("trade_description", ["8K+ sold", "1.5K+ sold", "2M+ sold", "10k sold"])
    def test_abbreviated_sold_count_is_rendered(self, trade_description):
        # A UK session drew "8K+ sold" where another drew "8,000+ sold" (2026-10-31).
        product = {
            "productId": "1005007198006311",
            "title": {"displayTitle": "Real Widget"},
            "prices": {"salePrice": {"formattedPrice": "￡0.99"}},
            "trade": {"tradeDesc": trade_description},
        }

        assert trade_description in _format_search_product(1, product)

    @pytest.mark.parametrize("trade_description", ["NaNK sold", "1e4K sold", "-5K sold", "8KK sold", "K sold", "8B sold"])
    def test_malformed_abbreviated_sold_count_is_omitted(self, trade_description):
        product = {
            "productId": "1005007198006311",
            "title": {"displayTitle": "Real Widget"},
            "prices": {"salePrice": {"formattedPrice": "￡0.99"}},
            "trade": {"tradeDesc": trade_description},
        }

        assert "sold" not in _format_search_product(1, product)

    @pytest.mark.parametrize(
        ("star_rating", "trade_description"),
        [
            ("NaN", "Infinity sold"),
            ("Infinity", "1e400 sold"),
            ("1e400", "-1 sold"),
            ("5.1", "NaN sold"),
        ],
    )
    def test_invalid_numeric_metadata_is_omitted(self, star_rating, trade_description):
        product = {
            "productId": "1005006367324382",
            "title": {"displayTitle": "Real Widget"},
            "prices": {"salePrice": {"formattedPrice": "US $1"}},
            "evaluation": {"starRating": star_rating},
            "trade": {"tradeDesc": trade_description},
        }

        rendered = _format_search_product(1, product)

        assert "★" not in rendered
        assert "sold" not in rendered


class TestFormatSearchResults:
    """Search results list formatting."""

    def test_header_includes_query_and_page(self):
        result = format_search_results([], "ESP32", 1, 0)
        assert '"ESP32"' in result
        assert "page 1" in result

    def test_no_products_message(self):
        result = format_search_results([], "nonexistent", 1, 0)
        assert "No products found" in result

    def test_numbers_products_sequentially(self):
        products = [
            {
                "productId": "1005000000000001",
                "title": {"displayTitle": "Product A"},
                "prices": {"salePrice": {"formattedPrice": "$1.00"}},
            },
            {
                "productId": "1005000000000002",
                "title": {"displayTitle": "Product B"},
                "prices": {"salePrice": {"formattedPrice": "$2.00"}},
            },
        ]
        result = format_search_results(products, "test", 1, 2)
        assert "1. Product A" in result
        assert "2. Product B" in result

    def test_page_2_numbering(self):
        """Page 2 should continue numbering from where page 1 left off."""
        products = [
            {
                "productId": "1005000000000001",
                "title": {"displayTitle": "Product A"},
                "prices": {"salePrice": {"formattedPrice": "$1.00"}},
            },
        ]
        result = format_search_results(products, "test", 2, 100)
        assert "61. Product A" in result


class TestBuildSearchUrl:
    """Search URL construction."""

    def test_basic_query(self):
        url = _build_search_url("esp32 board")
        assert "wholesale-esp32-board.html" in url
        assert "page=1" in url

    def test_sort_and_price_filters(self):
        url = _build_search_url("switch", sort="orders", min_price=1, max_price=10, page=2)
        assert "page=2" in url
        assert "sortType=total_tranpro_desc" in url
        assert "minPrice=1" in url
        assert "maxPrice=10" in url

    def test_scientific_notation_price_filter_round_trips(self):
        url = _build_search_url(
            "switch",
            min_price=1e20,
            max_price=2e20,
        )

        query = parse_qs(urlparse(url).query)

        assert query["minPrice"] == ["1e+20"]
        assert query["maxPrice"] == ["2e+20"]
        assert "1e%2B20" in url


class TestParseSearchHtml:
    """_init_data_ extraction helper."""

    def test_parses_valid_html(self):
        products = [
            {
                "productId": "1005000000000123",
                "title": {"displayTitle": "Widget"},
                "prices": {"salePrice": {"formattedPrice": "$9.99"}},
            },
        ]
        init_data = {
            "data": {
                "root": {
                    "fields": {
                        "mods": {"itemList": {"content": products}},
                        "pageInfo": {"totalResults": 1, "page": 1},
                    }
                }
            }
        }
        html = (
            "/*!-->init-data-start--*/"
            f"_dida_config_._init_data_= {{ data: {json.dumps(init_data)} }}"
            "/*!-->init-data-end--*/"
        )
        result = _parse_search_html(html, "widget")
        assert result is not None
        assert "Widget" in result["content"]

    def test_returns_none_for_empty_html(self):
        assert _parse_search_html("<html></html>", "test") is None

    @pytest.mark.parametrize(
        "placeholder",
        [
            {
                "productId": "1005000000000001",
                "title": {},
                "prices": {},
            },
            {
                "productId": "1005000000000001",
                "title": {"displayTitle": "12345"},
                "prices": {"salePrice": {"formattedPrice": "$9.99"}},
            },
            {
                "productId": "1005000000000001",
                "title": {"displayTitle": "Challenge placeholder"},
                "prices": {"salePrice": {"formattedPrice": "$0.00"}},
            },
            {
                "productId": "1005000000000001",
                "title": {"displayTitle": "Challenge placeholder"},
                "prices": {"salePrice": {"formattedPrice": "Minimum order 100 pieces"}},
            },
            {
                "productId": "1005000000000001",
                "title": {"displayTitle": "Negative price shell"},
                "prices": {"salePrice": {"formattedPrice": "$-1.00"}},
            },
            {
                "productId": [],
                "title": {"displayTitle": "Wrong typed product"},
                "prices": {"salePrice": {"formattedPrice": "$9.99"}},
            },
            "not-an-offer",
        ],
    )
    def test_rejects_placeholder_or_wrong_typed_item_lists(self, placeholder):
        assert _parse_search_html(_search_html([placeholder]), "widget") is None

    def test_valid_oversized_product_array_is_capped_before_formatting(self):
        products = [
            {
                "productId": str(1005000000000000 + index),
                "title": {"displayTitle": f"Bounded Widget {index}"},
                "prices": {"salePrice": {"formattedPrice": "$1.00"}},
            }
            for index in range(10_000)
        ]

        result = _parse_search_html(_search_html(products), "widget")

        assert result is not None
        assert result["content"].count("https://www.aliexpress.com/item/") == 60
        assert "Bounded Widget 59" in result["content"]
        assert "Bounded Widget 60" not in result["content"]
        assert len(result["content"]) <= 100_000

    def test_malformed_nested_fields_do_not_raise(self):
        product = {
            "productId": "1005000000000001",
            "title": {"displayTitle": "Bounded Widget"},
            "prices": {
                "salePrice": {"formattedPrice": "$1.00"},
                "originalPrice": [],
            },
            "evaluation": [],
            "trade": "bad",
        }

        result = _parse_search_html(_search_html([product]), "widget")

        assert result is not None
        assert "Bounded Widget" in result["content"]


def _make_mock_session():
    """Create a mock AsyncSession."""
    return AsyncMock()


class TestSearchAliexpress:
    """End-to-end search via wafer HTTP fetch."""

    @pytest.fixture(autouse=True)
    def _reset_session(self):
        """Reset module-level session between tests."""
        search_mod._session = None
        yield
        search_mod._session = None

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with patch("fetchaller.aliexpress.search.aliexpress_limiter.wait", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    async def test_no_solver_returns_error(self):
        """No browser_solver → error."""
        result = await search_aliexpress("widget", browser_solver=None)
        assert "error" in result
        assert "browser solver" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_search(self):
        """Wafer returns HTML with _init_data_ → products extracted."""
        products = [
            {
                "productId": "1005000000000789",
                "title": {"displayTitle": "Fast Widget"},
                "prices": {"salePrice": {"formattedPrice": "$9.99"}},
            },
        ]
        init_data = {
            "data": {
                "root": {
                    "fields": {
                        "mods": {"itemList": {"content": products}},
                        "pageInfo": {"totalResults": 1, "page": 1},
                    }
                }
            }
        }
        init_html = (
            "<!-->init-data-start--*/\n"
            f"window._dida_config_._init_data_= {{ data: {json.dumps(init_data)} }}"
            "\n<!-->init-data-end--*/"
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = init_html

        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)

        solver = MagicMock()

        with patch("fetchaller.aliexpress.search._get_session", return_value=mock_session):
            result = await search_aliexpress("widget", browser_solver=solver)

        assert "content" in result
        assert "Fast Widget" in result["content"]
        assert 0 < mock_session.get.await_args.kwargs["timeout"] <= 180
        mock_session.add_cookie.assert_not_called()

    @pytest.mark.asyncio
    async def test_shopping_locale_is_pinned_before_every_search(self, monkeypatch):
        # The site re-sets aep_usuc_f from the egress IP (a Frankfurt VPN exit
        # drew EUR and 19% VAT on 2026-10-31), so the pin goes on each request.
        monkeypatch.setenv("SHOPPING_COUNTRY", "GB")
        monkeypatch.setenv("SHOPPING_CURRENCY", "GBP")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _search_html([_LIVE_GBP_PRODUCT])
        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.add_cookie = MagicMock()

        with patch("fetchaller.aliexpress.search._get_session", return_value=mock_session):
            for _ in range(2):
                assert "content" in await search_aliexpress("ld2450", browser_solver=MagicMock())

        assert mock_session.add_cookie.call_count == 2
        cookie, url = mock_session.add_cookie.call_args.args
        assert cookie.startswith("aep_usuc_f=site=glo&c_tp=GBP&region=GB&")
        assert url.startswith("https://www.aliexpress.com/w/wholesale-ld2450.html")

    @pytest.mark.asyncio
    async def test_explicit_timeout_reaches_browser_capable_session(self):
        product = {
            "productId": "1005000000000789",
            "title": {"displayTitle": "Fast Widget"},
            "prices": {"salePrice": {"formattedPrice": "$9.99"}},
        }
        mock_resp = MagicMock(status_code=200, text=_search_html([product]))
        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch(
            "fetchaller.aliexpress.search._get_session",
            return_value=mock_session,
        ):
            result = await search_aliexpress(
                "widget",
                timeout=240,
                browser_solver=MagicMock(),
            )

        assert "Fast Widget" in result["content"]
        assert 0 < mock_session.get.await_args.kwargs["timeout"] <= 240

    @pytest.mark.asyncio
    async def test_limiter_wait_consumes_end_to_end_timeout(self):
        async def delayed_wait(*, extra_delay):
            assert extra_delay == 2.0
            await asyncio.sleep(0.05)

        with patch(
            "fetchaller.aliexpress.search.aliexpress_limiter.wait",
            side_effect=delayed_wait,
        ):
            started = time.monotonic()
            result = await search_aliexpress(
                "widget",
                timeout=0.01,
                browser_solver=MagicMock(),
            )
            elapsed = time.monotonic() - started

        assert "timed out after 0.01s" in result["error"]
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_synchronous_parse_is_inside_end_to_end_timeout(self):
        mock_resp = MagicMock(
            status_code=200,
            text="<html>transport complete</html>",
        )
        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)

        def slow_parse(*_args):
            time.sleep(0.05)
            return {"content": "too late"}

        with (
            patch(
                "fetchaller.aliexpress.search._get_session",
                return_value=mock_session,
            ),
            patch(
                "fetchaller.aliexpress.search._parse_search_html",
                side_effect=slow_parse,
            ),
        ):
            started = time.monotonic()
            result = await search_aliexpress(
                "widget",
                timeout=0.01,
                browser_solver=MagicMock(),
            )
            elapsed = time.monotonic() - started

        assert "timed out after 0.01s" in result["error"]
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_challenge_detected(self):
        """Wafer raises ChallengeDetected → error returned."""
        import wafer

        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(side_effect=wafer.ChallengeDetected("cloudflare", "https://example.com", 403))

        solver = MagicMock()

        with patch("fetchaller.aliexpress.search._get_session", return_value=mock_session):
            result = await search_aliexpress("widget", browser_solver=solver)

        assert "error" in result
        assert "cloudflare" in result["error"]

    @pytest.mark.asyncio
    async def test_tmd_punish_in_html(self):
        """Response contains TMD punish page → error returned."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>_____tmd_____/punish redirect</html>"

        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)

        solver = MagicMock()

        with patch("fetchaller.aliexpress.search._get_session", return_value=mock_session):
            result = await search_aliexpress("widget", browser_solver=solver)

        assert "error" in result
        assert "TMD" in result["error"]

    @pytest.mark.asyncio
    async def test_hydration_placeholder_is_not_a_search_success(self):
        mock_resp = MagicMock(
            status_code=200,
            text=_search_html(
                [
                    {
                        "productId": "1005000000000001",
                        "title": {},
                        "prices": {},
                    }
                ]
            ),
        )
        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch(
            "fetchaller.aliexpress.search._get_session",
            return_value=mock_session,
        ):
            result = await search_aliexpress(
                "widget",
                browser_solver=MagicMock(),
            )

        assert result == {"error": ("AliExpress search failed. Could not extract product data from response.")}

    @pytest.mark.asyncio
    async def test_no_init_data_in_html(self):
        """Response has no _init_data_ → error returned."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Some other page</body></html>"

        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)

        solver = MagicMock()

        with patch("fetchaller.aliexpress.search._get_session", return_value=mock_session):
            result = await search_aliexpress("widget", browser_solver=solver)

        assert "error" in result
        assert "extract" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_http_error(self):
        """HTTP 500 → error returned."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        mock_session = _make_mock_session()
        mock_session.get = AsyncMock(return_value=mock_resp)

        solver = MagicMock()

        with patch("fetchaller.aliexpress.search._get_session", return_value=mock_session):
            result = await search_aliexpress("widget", browser_solver=solver)

        assert "error" in result
        assert "500" in result["error"]
