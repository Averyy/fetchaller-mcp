"""Unit tests for Alibaba.com product module."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from fetchaller.alibaba.product import (
    _extract_details,
    _format_output,
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
            extract_product_id(
                "https://www.alibaba.com/product-detail/Waterproof-Switch_1600123456789.html"
            )
            == "1600123456789"
        )

    def test_url_with_params(self):
        assert (
            extract_product_id(
                "https://www.alibaba.com/product-detail/Switch_1600123456789.html?spm=abc"
            )
            == "1600123456789"
        )

    def test_mobile_url(self):
        assert (
            extract_product_id(
                "https://m.alibaba.com/product-detail/Widget_1600123456789.html"
            )
            == "1600123456789"
        )

    def test_regional_subdomain(self):
        assert (
            extract_product_id(
                "https://spanish.alibaba.com/product-detail/Cosa_1600123456789.html"
            )
            == "1600123456789"
        )

    def test_invalid_url(self):
        assert extract_product_id("https://example.com/page") is None

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
        html = f'<script>window.detailData = {json.dumps(detail)}</script>'
        result = extract_product_data(html)
        assert result is not None
        assert result["product"]["subject"] == "Toggle Switch"

    def test_no_detail_data_returns_none(self):
        html = "<html><body>Regular page</body></html>"
        assert extract_product_data(html) is None

    def test_no_global_data_returns_none(self):
        detail = {"nodeMap": {}}
        html = f'<script>window.detailData = {json.dumps(detail)}</script>'
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
        assert data["moq"] == 100
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
                },
                "seller": {"companyName": "Test Supplier"},
                "trade": {},
                "review": {},
            }
        }
        html = f'<script>window.detailData = {json.dumps(detail)}</script>'
        mock_fetch.return_value = {"content": html}

        result = await get_product("1600123456789")
        assert "Toggle Switch IP67" in result["content"]
        assert "$0.50" in result["content"]
        assert "Test Supplier" in result["content"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.product.fetch_url", new_callable=AsyncMock)
    async def test_fetch_error_propagates(self, mock_fetch):
        mock_fetch.return_value = {"error": "Connection timeout"}

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
        """Accepts full Alibaba URL and extracts product ID."""
        detail = {
            "globalData": {
                "product": {"subject": "From URL", "productId": "1600123456789"},
                "seller": {},
                "trade": {},
                "review": {},
            }
        }
        html = f'<script>window.detailData = {json.dumps(detail)}</script>'
        mock_fetch.return_value = {"content": html}

        result = await get_product(
            "https://www.alibaba.com/product-detail/Waterproof-Switch_1600123456789.html"
        )
        assert "From URL" in result["content"]
