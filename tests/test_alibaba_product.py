"""Unit tests for Alibaba.com product module."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from fetchaller.alibaba.product import (
    _extract_details,
    _format_output,
    _has_usable_product_data,
    extract_product_id,
    get_product,
)
from fetchaller.content.alibaba import extract_product_data

# ---------------------------------------------------------------------------
# Product ID extraction
# ---------------------------------------------------------------------------


class TestExtractProductId:
    """Product ID extraction from URLs and bare IDs."""

    def test_bare_numeric_id(self):
        assert extract_product_id("1600123456789") == "1600123456789"

    def test_full_url(self):
        assert (
            extract_product_id("https://www.alibaba.com/product-detail/Waterproof-Switch_1600123456789.html")
            == "1600123456789"
        )

    def test_url_with_params(self):
        assert (
            extract_product_id("https://www.alibaba.com/product-detail/Switch_1600123456789.html?spm=abc")
            == "1600123456789"
        )

    def test_mobile_url(self):
        assert extract_product_id("https://m.alibaba.com/product-detail/Widget_1600123456789.html") == "1600123456789"

    def test_regional_subdomain(self):
        assert (
            extract_product_id("https://spanish.alibaba.com/product-detail/Cosa_1600123456789.html") == "1600123456789"
        )

    def test_invalid_url(self):
        assert extract_product_id("https://example.com/page") is None

    @pytest.mark.parametrize(
        "value",
        [
            "http://www.alibaba.com/product-detail/Widget_1600123456789.html",
            "https://user@www.alibaba.com/product-detail/Widget_1600123456789.html",
            "https://www.alibaba.com.evil.example/product-detail/Widget_1600123456789.html",
            "https://www.alibaba.com/product-detail/Widget_1600123456789.html/extra",
            "https://www.alibaba.com/foo/product-detail/Widget_1600123456789.html",
            " 1600123456789 ",
        ],
    )
    def test_rejects_noncanonical_or_ambiguous_inputs(self, value):
        assert extract_product_id(value) is None

    def test_short_number_rejected(self):
        assert extract_product_id("1234") is None

    def test_too_long_number_rejected(self):
        assert extract_product_id("123456789012345678901") is None

    def test_non_product_alibaba_url(self):
        assert extract_product_id("https://www.alibaba.com/trade/search?SearchText=switch") is None


# ---------------------------------------------------------------------------
# JSON extraction from product HTML
# ---------------------------------------------------------------------------


class TestExtractProductData:
    """window.detailData extraction."""

    def test_extracts_global_data(self):
        detail = {
            "globalData": {
                "product": {"subject": "Toggle Switch"},
                "seller": {"companyName": "Acme Co"},
            },
            "nodeMap": {},
        }
        html = f"<script>window.detailData = {json.dumps(detail)}</script>"
        result = extract_product_data(html)
        assert result is not None
        assert result["product"]["subject"] == "Toggle Switch"

    def test_no_detail_data_returns_none(self):
        html = "<html><body>Regular page</body></html>"
        assert extract_product_data(html) is None

    def test_no_global_data_returns_none(self):
        detail = {"nodeMap": {}}
        html = f"<script>window.detailData = {json.dumps(detail)}</script>"
        assert extract_product_data(html) is None

    @pytest.mark.parametrize(
        "number",
        ["1e400", "-1e400", "NaN", "Infinity", "9" * 5_000],
    )
    def test_rejects_non_finite_or_oversized_json_numbers(self, number):
        html = (
            "<script>window.detailData = "
            f'{{"globalData": {{"number": {number}}}}}'
            "</script>"
        )

        assert extract_product_data(html) is None


# ---------------------------------------------------------------------------
# Data extraction from globalData
# ---------------------------------------------------------------------------


class TestExtractDetails:
    """Structured data extraction from globalData."""

    def _make_global_data(self, **overrides):
        """Create a globalData dict with defaults."""
        base = {
            "product": {
                "subject": "Waterproof Switch IP67",
                "productId": "1600123456789",
                "price": {
                    "productRangePrices": {"priceRangeText": "$0.50-1.20"},
                    "unitEven": "pieces",
                    "productLadderPrices": [
                        {"minQuantity": 100, "maxQuantity": 499, "price": "1.20"},
                        {"minQuantity": 500, "maxQuantity": 0, "price": "0.50"},
                    ],
                },
                "moq": 100,
                "productKeyIndustryProperties": [
                    {"attrName": "Max Current", "attrNameId": 1, "attrValue": "15A"},
                    {"attrName": "Voltage", "attrNameId": 2, "attrValue": "250V AC"},
                ],
                "sku": {
                    "skuAttrs": [
                        {
                            "name": "Color",
                            "values": [{"name": "Black"}, {"name": "Red"}],
                        }
                    ]
                },
            },
            "seller": {
                "companyName": "Shenzhen Electronics Co.",
                "companyId": "987654",
                "companyProfileUrl": "//shenzhen.alibaba.com/",
            },
            "trade": {
                "salesVolume": "1580 sold",
                "tradeInfo": {"tradePriceType": "FOB"},
                "leadTimeInfo": {
                    "ladderPeriodList": [
                        {"minQuantity": 1, "maxQuantity": 500, "processPeriod": 7},
                        {"minQuantity": 501, "maxQuantity": 0, "processPeriod": 15},
                    ]
                },
            },
            "review": {
                "productReview": {
                    "averageStar": 4.9,
                    "totalReviewCount": 42,
                }
            },
        }
        for k, v in overrides.items():
            base[k] = v
        return base

    def test_extracts_all_fields(self):
        data = _extract_details(self._make_global_data())
        assert data["title"] == "Waterproof Switch IP67"
        assert data["product_id"] == "1600123456789"
        assert data["price_range"] == "$0.50-1.20"
        assert len(data["price_tiers"]) == 2
        assert data["price_tiers"][0]["qty_str"] == "100-499"
        assert data["price_tiers"][0]["price"] == "$1.20"
        assert data["price_tiers"][1]["qty_str"] == "500+"
        assert data["price_tiers"][1]["price"] == "$0.50"
        assert data["moq"] == "100"
        assert data["unit"] == "pieces"
        assert data["company_name"] == "Shenzhen Electronics Co."
        assert data["sales_volume"] == "1580 sold"
        assert data["trade_type"] == "FOB"
        assert len(data["lead_times"]) == 2
        assert "1-500 units: 7 days" in data["lead_times"]
        assert "501+ units: 15 days" in data["lead_times"]
        assert "Max Current: 15A" in data["key_properties"]
        assert "Color: Black, Red" in data["variants"]
        assert data["avg_star"] == "4.9"
        assert data["review_count"] == 42

    def test_handles_empty_global_data(self):
        """Empty globalData produces empty fields without crashing."""
        data = _extract_details({})
        assert data["title"] == ""
        assert data["price_range"] == ""
        assert data["price_tiers"] == []

    def test_drops_each_invalid_price_from_mixed_product_data(self):
        global_data = self._make_global_data()
        global_data["product"]["price"] = {
            "formatLadderPrice": "Minimum order 100 pieces",
            "productRangePrices": {"priceRangeText": "US$2.00-3.00"},
            "productLadderPrices": [
                {"minQuantity": 1, "formatPrice": "US$1.00"},
                {"minQuantity": 2, "formatPrice": "US$-5.00"},
                {"minQuantity": 3, "formatPrice": "Minimum order 100 pieces"},
            ],
        }

        data = _extract_details(global_data)
        rendered = _format_output("1600123456789", data)

        assert data["price_range"] == "US$2.00-3.00"
        assert [tier["price"] for tier in data["price_tiers"]] == ["US$1.00"]
        assert "Minimum order 100 pieces" not in rendered
        assert "US$-5.00" not in rendered

    def test_drops_invalid_and_overlapping_price_tiers(self):
        global_data = self._make_global_data()
        global_data["product"]["price"]["productLadderPrices"] = [
            {"min": -1, "max": 4, "formatPrice": "US$9"},
            {"min": 0, "max": 4, "formatPrice": "US$8"},
            {"min": 5, "max": 4, "formatPrice": "US$7"},
            {"min": 2, "max": 10, "formatPrice": "US$2"},
            {"min": 1, "max": 5, "formatPrice": "US$3"},
            {"min": 11, "max": 0, "formatPrice": "US$1"},
            {"min": 20, "max": 0, "formatPrice": "US$0.50"},
        ]

        data = _extract_details(global_data)

        assert data["price_tiers"] == [
            {"qty_str": "1-5", "min": 1, "max": 5, "price": "US$3"},
            {"qty_str": "11+", "min": 11, "max": None, "price": "US$1"},
        ]

    @pytest.mark.parametrize(
        ("average_star", "review_count"),
        [
            ("NaN", "Infinity"),
            ("Infinity", "1e400"),
            ("1e400", "-1"),
            ("5.1", "1.5"),
        ],
    )
    def test_invalid_string_review_metadata_is_omitted(
        self, average_star, review_count
    ):
        global_data = self._make_global_data()
        global_data["review"]["productReview"] = {
            "averageStar": average_star,
            "totalReviewCount": review_count,
        }

        data = _extract_details(global_data)
        rendered = _format_output("1600123456789", data)

        assert data["avg_star"] == ""
        assert data["review_count"] == 0
        assert "**Rating:**" not in rendered

    def test_malformed_and_oversized_embedded_arrays_are_bounded(self):
        global_data = self._make_global_data()
        global_data["product"]["price"]["productLadderPrices"] = [
            {
                "min": index + 1,
                "max": index + 1,
                "formatPrice": "$1.00",
            }
            for index in range(10_000)
        ]
        global_data["product"]["productKeyIndustryProperties"] = [
            {"attrName": f"Property {index}", "attrValue": "value"}
            for index in range(10_000)
        ]
        global_data["product"]["sku"]["skuAttrs"] = [
            {
                "name": f"Variant {index}",
                "values": [
                    {"name": f"Value {value_index}"}
                    for value_index in range(1_000)
                ],
            }
            for index in range(1_000)
        ]

        data = _extract_details(global_data)

        assert len(data["price_tiers"]) == 20
        assert len(data["key_properties"]) == 40
        assert len(data["variants"]) == 20
        assert "Value 19" in data["variants"][0]
        assert "Value 20" not in data["variants"][0]

    @pytest.mark.parametrize(
        "global_data",
        [
            {"product": [], "seller": "bad", "trade": 1, "review": None},
            {"product": {"price": [], "sku": "bad"}},
            {"product": {"price": {"productLadderPrices": [None, "bad"]}}},
        ],
    )
    def test_malformed_embedded_types_do_not_raise(self, global_data):
        assert isinstance(_extract_details(global_data), dict)

    def test_non_finite_moq_and_tier_quantities_are_rejected(self):
        global_data = self._make_global_data()
        global_data["product"]["moq"] = float("inf")
        global_data["product"]["price"]["productLadderPrices"] = [
            {
                "min": float("inf"),
                "max": 100,
                "formatPrice": "$1.00",
            },
            {
                "min": 1,
                "max": float("inf"),
                "formatPrice": "$2.00",
            },
        ]

        data = _extract_details(global_data)

        assert data["moq"] == ""
        assert data["price_tiers"] == []
        assert "inf" not in _format_output("1600123456789", data).lower()

    @pytest.mark.parametrize("moq", ["Infinity", "NaN", "1e400", "-1", "0"])
    def test_invalid_string_moq_is_omitted(self, moq):
        global_data = self._make_global_data()
        global_data["product"]["moq"] = moq

        data = _extract_details(global_data)

        assert data["moq"] == ""
        assert "**MOQ:**" not in _format_output("1600123456789", data)

    @pytest.mark.parametrize(
        "period",
        [
            {"minQuantity": "NaN", "maxQuantity": 10, "day": 2},
            {"minQuantity": 1, "maxQuantity": "Infinity", "day": 2},
            {"minQuantity": 1, "maxQuantity": 10, "day": "Infinity"},
            {"minQuantity": -1, "maxQuantity": 10, "day": 2},
            {"minQuantity": 5, "maxQuantity": 4, "day": 2},
            {"minQuantity": 1, "maxQuantity": 10, "day": 0},
        ],
    )
    def test_invalid_lead_time_numbers_are_omitted(self, period):
        global_data = self._make_global_data()
        global_data["trade"]["leadTimeInfo"]["ladderPeriodList"] = [period]

        assert _extract_details(global_data)["lead_times"] == []

    def test_zero_max_qty_shows_plus(self):
        """maxQuantity=0 means open-ended (500+)."""
        gd = self._make_global_data()
        data = _extract_details(gd)
        assert any("500+" in t["qty_str"] for t in data["price_tiers"])

    def test_format_price_preferred_over_raw(self):
        """formatPrice (with currency symbol) is preferred over raw price."""
        gd = self._make_global_data()
        gd["product"]["price"]["productLadderPrices"] = [
            {"min": 100, "max": 499, "formatPrice": "$4.12", "price": 4.12},
        ]
        data = _extract_details(gd)
        assert data["price_tiers"][0]["price"] == "$4.12"


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


class TestFormatOutput:
    """Product output formatting."""

    def test_full_output(self):
        data = {
            "title": "Waterproof Switch",
            "product_id": "1600123456789",
            "price_range": "$0.50-1.20",
            "price_tiers": [
                {"qty_str": "100-499", "min": 100, "max": 499, "price": "$1.20"},
                {"qty_str": "500+", "min": 500, "max": None, "price": "$0.50"},
            ],
            "unit": "pieces",
            "moq": 100,
            "company_name": "Acme Co",
            "company_id": "123",
            "company_url": "//acme.alibaba.com/",
            "sales_volume": "1580 sold",
            "trade_type": "FOB",
            "lead_times": ["1-500 units: 7 days"],
            "variants": ["Color: Black, Red"],
            "key_properties": ["Max Current: 15A"],
            "avg_star": "4.9",
            "review_count": 42,
        }
        result = _format_output("1600123456789", data)
        assert "# Waterproof Switch" in result
        assert "$0.50-1.20" in result
        assert "per pieces" in result
        assert "100-499: $1.20" in result
        assert "500+: $0.50" in result
        assert "MOQ:** 100 pieces" in result
        assert "MOQ Price:** $1.20/piece" in result
        assert "FOB" in result
        assert "Acme Co" in result
        assert "1580 sold" in result
        assert "★4.9" in result
        assert "42 reviews" in result
        assert "1-500 units: 7 days" in result
        assert "Color: Black, Red" in result
        assert "Max Current: 15A" in result
        assert "alibaba.com/product-detail/_1600123456789.html" in result

    def test_fractional_moq_does_not_select_an_incorrect_tier(self):
        data = {
            "title": "Weighted Product",
            "product_id": "1600123456789",
            "price_range": "$5-100",
            "price_tiers": [
                {"min": 1, "max": 1, "price": "$100"},
                {"min": 2, "max": None, "price": "$5"},
            ],
            "unit": "pieces",
            "moq": 1.9,
            "company_name": "Acme Co",
            "company_url": "",
            "sales_volume": "",
            "trade_type": "",
            "lead_times": [],
            "variants": [],
            "key_properties": ["Weight: 1kg"],
            "avg_star": "",
            "review_count": 0,
        }

        result = _format_output("1600123456789", data)

        assert "**MOQ:** 1.9 pieces" in result
        assert "**MOQ Price:**" not in result

    def test_moq_outside_explicit_tiers_has_no_derived_price(self):
        data = {
            "title": "Tiered Product",
            "product_id": "1600123456789",
            "price_range": "$5-100",
            "price_tiers": [{"min": 2, "max": 5, "price": "$5"}],
            "unit": "pieces",
            "moq": 10,
            "company_name": "Acme Co",
            "company_url": "",
            "sales_volume": "",
            "trade_type": "",
            "lead_times": [],
            "variants": [],
            "key_properties": ["Weight: 1kg"],
            "avg_star": "",
            "review_count": 0,
        }

        result = _format_output("1600123456789", data)

        assert "**MOQ Price:**" not in result

    def test_empty_data_returns_empty(self):
        data = {
            "title": "",
            "product_id": "",
            "price_range": "",
            "price_tiers": [],
            "unit": "",
            "moq": "",
            "company_name": "",
            "company_id": "",
            "company_url": "",
            "sales_volume": "",
            "trade_type": "",
            "lead_times": [],
            "variants": [],
            "key_properties": [],
            "avg_star": "",
            "review_count": 0,
        }
        result = _format_output("", data)
        assert result == ""

    def test_company_url_gets_protocol(self):
        """Protocol-relative URLs get https: prefix."""
        data = {
            "title": "Widget",
            "product_id": "",
            "price_range": "",
            "price_tiers": [],
            "unit": "",
            "moq": "",
            "company_name": "",
            "company_id": "",
            "company_url": "//acme.alibaba.com/",
            "sales_volume": "",
            "trade_type": "",
            "lead_times": [],
            "variants": [],
            "key_properties": [],
            "avg_star": "",
            "review_count": 0,
        }
        result = _format_output("123", data)
        assert "https://acme.alibaba.com/" in result

    def test_rejects_non_alibaba_supplier_profile_url(self):
        data = {
            "title": "Waterproof Switch",
            "company_url": "https://evil.example/supplier",
        }

        result = _format_output("1600123456789", data)

        assert "evil.example" not in result

    def test_output_cap_keeps_whole_lines_and_names_omission(self):
        data = {
            "title": "Waterproof Switch",
            "variants": [
                f"Variant {index}: {'x' * 10_000}"
                for index in range(20)
            ],
            "key_properties": [
                f"Property {index}: {'y' * 10_000}"
                for index in range(40)
            ],
        }

        result = _format_output("1600123456789", data)

        assert len(result) <= 100_000
        assert "Additional product fields omitted" in result


class TestUsableProductData:
    def test_requires_title_numeric_price_supplier_and_specification(self):
        data = {
            "title": "Waterproof Switch",
            "product_id": "1600123456789",
            "price_range": "$0.50-1.20",
            "price_tiers": [],
            "company_name": "Acme Co",
            "key_properties": ["Ingress protection: IP67"],
        }

        assert _has_usable_product_data(data) is True

    @pytest.mark.parametrize(
        "override",
        [
            {"title": ""},
            {"title": "1600123456789"},
            {"title": "²Ⅻ①"},
            {"price_range": "Contact supplier"},
            {"price_range": "$0.00"},
            {"price_range": "Minimum order 100 pieces"},
            {"price_range": "$-1.00"},
            {"company_name": ""},
            {"company_name": "²Ⅻ①"},
            {"key_properties": []},
            {"key_properties": ["²Ⅻ①"]},
        ],
    )
    def test_rejects_partial_detail_data_shells(self, override):
        data = {
            "title": "Waterproof Switch",
            "product_id": "1600123456789",
            "price_range": "$0.50-1.20",
            "price_tiers": [],
            "company_name": "Acme Co",
            "key_properties": ["Ingress protection: IP67"],
        }
        data.update(override)

        assert _has_usable_product_data(data) is False

    @pytest.mark.parametrize(
        ("title", "company_name", "key_properties"),
        [
            ("Café Switch", "Société Acme", ["Étanchéité: IP67"]),
            ("防水开关", "深圳开关公司", ["防护等级: IP67"]),
        ],
    )
    def test_accepts_unicode_letter_product_data(
        self, title, company_name, key_properties
    ):
        assert _has_usable_product_data(
            {
                "title": title,
                "product_id": "1600123456789",
                "price_range": "$0.50-1.20",
                "price_tiers": [],
                "company_name": company_name,
                "key_properties": key_properties,
            }
        )

    def test_rejects_embedded_product_id_mismatch(self):
        data = {
            "title": "Waterproof Switch",
            "product_id": "1600999999999",
            "price_range": "$0.50-1.20",
            "price_tiers": [],
            "company_name": "Acme Co",
            "key_properties": ["Ingress protection: IP67"],
        }

        assert _has_usable_product_data(data, expected_product_id="1600123456789") is False

    def test_rejects_numeric_scalar_hydration_shell(self):
        data = _extract_details(
            {
                "product": {
                    "subject": 1.2,
                    "productId": "1600123456789",
                    "price": {"formatLadderPrice": "$1.00"},
                    "productKeyIndustryProperties": [
                        {"attrName": 2.3, "attrValue": 4.5}
                    ],
                },
                "seller": {"companyName": 6.7},
            }
        )

        assert (
            _has_usable_product_data(
                data,
                expected_product_id="1600123456789",
            )
            is False
        )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestGetProductIntegration:
    """End-to-end get_product with mocked fetch_url."""

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with patch("fetchaller.alibaba.product.alibaba_limiter.wait", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_success(self, mock_fetch):
        """Full flow: fetch HTML → extract JSON → format output."""
        detail = {
            "globalData": {
                "product": {
                    "subject": "Toggle Switch IP67",
                    "productId": "1600123456789",
                    "price": {"formatLadderPrice": "$0.50"},
                    "moq": 100,
                    "productKeyIndustryProperties": [{"attrName": "Ingress protection", "attrValue": "IP67"}],
                },
                "seller": {"companyName": "Test Supplier"},
                "trade": {},
                "review": {},
            }
        }
        html = f"<script>window.detailData = {json.dumps(detail)}</script>"
        mock_fetch.return_value = {"content": html}

        result = await get_product("1600123456789")
        assert "Toggle Switch IP67" in result["content"]
        assert "$0.50" in result["content"]
        assert "Test Supplier" in result["content"]
        assert 0 < mock_fetch.await_args.kwargs["timeout"] <= 180

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_explicit_timeout_reaches_browser_capable_fetch(
        self,
        mock_fetch,
    ):
        detail = {
            "globalData": {
                "product": {
                    "subject": "Toggle Switch IP67",
                    "productId": "1600123456789",
                    "price": {"formatLadderPrice": "$0.50"},
                    "productKeyIndustryProperties": [
                        {
                            "attrName": "Ingress protection",
                            "attrValue": "IP67",
                        }
                    ],
                },
                "seller": {"companyName": "Test Supplier"},
            }
        }
        mock_fetch.return_value = {
            "content": (
                f"<script>window.detailData = {json.dumps(detail)}</script>"
            )
        }

        result = await get_product("1600123456789", timeout=240)

        assert "Toggle Switch IP67" in result["content"]
        assert 0 < mock_fetch.await_args.kwargs["timeout"] <= 240

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_limiter_wait_consumes_end_to_end_timeout(self, mock_fetch):
        async def delayed_wait():
            await asyncio.sleep(0.05)

        with patch(
            "fetchaller.alibaba.product.alibaba_limiter.wait",
            side_effect=delayed_wait,
        ):
            started = time.monotonic()
            result = await get_product("1600123456789", timeout=0.01)
            elapsed = time.monotonic() - started

        assert "timed out after 0.01s" in result["error"]
        assert elapsed < 0.1
        mock_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_synchronous_parse_is_inside_end_to_end_timeout(
        self,
        mock_fetch,
    ):
        mock_fetch.return_value = {"content": "<html>transport complete</html>"}

        def slow_parse(*_args):
            time.sleep(0.05)
            return {"content": "too late"}

        with patch(
            "fetchaller.alibaba.product._parse_product_html",
            side_effect=slow_parse,
        ):
            started = time.monotonic()
            result = await get_product("1600123456789", timeout=0.01)
            elapsed = time.monotonic() - started

        assert "timed out after 0.01s" in result["error"]
        assert elapsed < 0.1

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_fetch_error_propagates(self, mock_fetch):
        mock_fetch.return_value = {"error": "Connection timeout"}

        result = await get_product("1600123456789")
        assert "error" in result

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_embedded_product_id_must_match_request(self, mock_fetch):
        detail = {
            "globalData": {
                "product": {
                    "subject": "Wrong product",
                    "productId": "1600999999999",
                    "price": {"formatLadderPrice": "$1.00"},
                    "productKeyIndustryProperties": [{"attrName": "Material", "attrValue": "Steel"}],
                },
                "seller": {"companyName": "Supplier"},
            }
        }
        mock_fetch.return_value = {"content": (f"<script>window.detailData = {json.dumps(detail)}</script>")}

        result = await get_product("1600123456789")

        assert "error" in result

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_no_detail_data_returns_error(self, mock_fetch):
        mock_fetch.return_value = {"content": "<html><body>Empty</body></html>"}

        result = await get_product("1600123456789")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_product_id_returns_error(self):
        result = await get_product("abc")
        assert "error" in result
        assert "Invalid" in result["error"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_accepts_full_url(self, mock_fetch):
        """Partial detailData from a full URL is rejected, not formatted."""
        detail = {
            "globalData": {
                "product": {"subject": "From URL", "productId": "1600123456789"},
                "seller": {},
                "trade": {},
                "review": {},
            }
        }
        html = f"<script>window.detailData = {json.dumps(detail)}</script>"
        mock_fetch.return_value = {"content": html}

        result = await get_product("https://www.alibaba.com/product-detail/Waterproof-Switch_1600123456789.html")
        assert "error" in result
        assert "complete product data" in result["error"]
