"""Unit tests for AliExpress product module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fetchaller.aliexpress.product import (
    _extract_product_data,
    _fetch_via_chrome,
    _format_output,
    _format_rating_breakdown,
    _format_reviews,
    extract_product_id,
    get_product,
)


class TestExtractProductId:
    """Product ID extraction from URLs and bare IDs."""

    def test_bare_numeric_id(self):
        assert extract_product_id("1005006367324382") == "1005006367324382"

    def test_full_url(self):
        assert extract_product_id("https://www.aliexpress.com/item/1005006367324382.html") == "1005006367324382"

    def test_url_with_params(self):
        assert extract_product_id(
            "https://www.aliexpress.com/item/1005006367324382.html?spm=a2g0o.detail.1000060.1"
        ) == "1005006367324382"

    def test_mobile_url(self):
        assert extract_product_id("https://m.aliexpress.com/item/1005006367324382.html") == "1005006367324382"

    def test_regional_url(self):
        assert extract_product_id("https://ko.aliexpress.com/item/1005006367324382.html") == "1005006367324382"

    def test_invalid_url(self):
        assert extract_product_id("https://example.com/page") is None

    def test_short_number_rejected(self):
        """Numbers shorter than 8 digits are not valid product IDs."""
        assert extract_product_id("1234567") is None

    def test_too_long_number_rejected(self):
        """Numbers longer than 20 digits are not valid product IDs."""
        assert extract_product_id("123456789012345678901") is None


class TestExtractProductData:
    """Product data extraction from MTop response."""

    def test_extracts_title(self):
        result = {
            "data": {
                "result": {
                    "PRODUCT_TITLE": {"text": "USB C Hub 8 in 1"},
                }
            }
        }
        data = _extract_product_data(result)
        assert data["title"] == "USB C Hub 8 in 1"

    def test_extracts_price(self):
        result = {
            "data": {
                "result": {
                    "PRICE": {
                        "targetSkuPriceInfo": {
                            "salePriceString": "$12.99",
                            "originalPriceString": "$25.98",
                            "discount": "50",
                        }
                    }
                }
            }
        }
        data = _extract_product_data(result)
        assert data["sale_price"] == "$12.99"
        assert data["original_price"] == "$25.98"
        assert data["discount"] == "50"

    def test_extracts_rating(self):
        result = {
            "data": {
                "result": {
                    "PC_RATING": {"rating": "4.8", "totalValidNum": 1247},
                }
            }
        }
        data = _extract_product_data(result)
        assert data["rating"] == "4.8"
        assert data["review_count"] == 1247

    def test_extracts_store(self):
        result = {
            "data": {
                "result": {
                    "SHOP_CARD_PC": {
                        "storeName": "TechGadgets Official",
                        "positiveRate": "98.2%",
                    }
                }
            }
        }
        data = _extract_product_data(result)
        assert data["store_name"] == "TechGadgets Official"
        assert data["positive_rate"] == "98.2%"

    def test_extracts_variants(self):
        result = {
            "data": {
                "result": {
                    "SKU": {
                        "skuProperties": [
                            {
                                "skuPropertyName": "Color",
                                "skuPropertyValues": [
                                    {"propertyValueDisplayName": "Silver"},
                                    {"propertyValueDisplayName": "Black"},
                                ],
                            }
                        ]
                    }
                }
            }
        }
        data = _extract_product_data(result)
        assert "Color: Silver, Black" in data["variants"]

    def test_extracts_specs(self):
        result = {
            "data": {
                "result": {
                    "PRODUCT_PROP_PC": {
                        "showedProps": [
                            {"name": "Brand", "value": "Generic"},
                            {"name": "Interface", "value": "USB-C"},
                        ]
                    }
                }
            }
        }
        data = _extract_product_data(result)
        assert "Brand: Generic" in data["specs"]
        assert "Interface: USB-C" in data["specs"]

    def test_handles_empty_response(self):
        """Empty response should produce empty fields, not crash."""
        data = _extract_product_data({"data": {"result": {}}})
        assert data["title"] == ""
        assert data["sale_price"] == ""


class TestFormatRatingBreakdown:
    """Rating breakdown formatting."""

    def test_formats_all_stars(self):
        stats = {
            "fiveStarRate": 76.8,
            "fourStarRate": 9.7,
            "threeStarRate": 3.6,
            "twoStarRate": 1.8,
            "oneStarRate": 8.1,
        }
        result = _format_rating_breakdown(stats)
        assert "★5: 76.8%" in result
        assert "★4: 9.7%" in result
        assert "★1: 8.1%" in result


class TestFormatReviews:
    """Review list formatting."""

    def test_formats_review_with_translation(self):
        reviews = [
            {
                "buyerEval": 100,
                "buyerCountry": "RU",
                "evalDate": "22 Nov 2025",
                "skuInfo": "Color:Silver",
                "buyerTranslationFeedback": "Great quality product",
                "buyerFeedback": "Отличный товар",
            }
        ]
        result = _format_reviews(reviews)
        assert "★5" in result
        assert "RU" in result
        assert "Great quality product" in result
        # Should use translation, not original
        assert "Отличный товар" not in result

    def test_formats_review_with_photos(self):
        reviews = [
            {
                "buyerEval": 80,
                "buyerCountry": "US",
                "evalDate": "10 Jan 2026",
                "buyerFeedback": "Good",
                "images": ["img1.jpg", "img2.jpg"],
            }
        ]
        result = _format_reviews(reviews)
        assert "2 photos" in result

    def test_truncates_long_reviews(self):
        reviews = [
            {
                "buyerEval": 60,
                "buyerCountry": "UK",
                "evalDate": "5 Feb 2026",
                "buyerFeedback": "x" * 300,
            }
        ]
        result = _format_reviews(reviews)
        assert "..." in result

    def test_limits_to_5_reviews(self):
        reviews = [
            {"buyerEval": 100, "buyerCountry": f"C{i}", "evalDate": "", "buyerFeedback": f"Review {i}"}
            for i in range(10)
        ]
        result = _format_reviews(reviews)
        assert "Review 0" in result
        assert "Review 4" in result
        assert "Review 5" not in result


class TestFormatOutput:
    """Merged output formatting."""

    def test_full_output_with_product_and_reviews(self):
        product_data = {
            "title": "USB Hub",
            "sale_price": "$12.99",
            "original_price": "$25.98",
            "discount": "50",
            "rating": "4.8",
            "review_count": 100,
            "store_name": "TestStore",
            "positive_rate": "98%",
            "stock": "500",
            "orders": "1000+",
        }
        reviews_data = {
            "productEvaluationStatistic": {
                "evarageStar": 4.8,
                "totalNum": 100,
                "fiveStarRate": 90,
                "fourStarRate": 5,
                "threeStarRate": 3,
                "twoStarRate": 1,
                "oneStarRate": 1,
            },
            "evaViewList": [
                {"buyerEval": 100, "buyerCountry": "US", "evalDate": "1 Jan 2026", "buyerFeedback": "Great"},
            ],
        }
        result = _format_output("1005006367324382", product_data, reviews_data)
        assert "USB Hub" in result
        assert "$12.99" in result
        assert "TestStore" in result
        assert "★4.8" in result
        assert "Recent reviews:" in result

    def test_reviews_only_returns_reviews_section(self):
        """When no product data, format reviews standalone."""
        reviews_data = {
            "productEvaluationStatistic": {
                "evarageStar": 4.5,
                "totalNum": 50,
                "fiveStarRate": 80,
                "fourStarRate": 10,
                "threeStarRate": 5,
                "twoStarRate": 3,
                "oneStarRate": 2,
            },
            "evaViewList": [],
        }
        result = _format_output("123456789", None, reviews_data)
        assert "★4.5" in result

    def test_both_none_returns_empty(self):
        result = _format_output("123456789", None, None)
        assert result == ""

    def test_reviews_error_returns_empty(self):
        result = _format_output("123456789", None, {"error": "API error"})
        assert result == ""


# ── Chrome Fallback Tests ────────────────────────────────────────────────────


def _mock_execute_script(return_map):
    """Create a mock tab.execute_script that returns different values per JS snippet.

    return_map: list of (substring_match, value) — first match wins.
    """
    async def execute_script(js, **kwargs):
        for substring, value in return_map:
            if substring in js:
                return {"result": {"result": {"value": value}}}
        return {"result": {"result": {"value": None}}}
    return execute_script


def _mtop_success_json(product_id="1005006367324382"):
    """Return a valid MTop JSON response string."""
    return json.dumps({
        "ret": ["SUCCESS::调用成功"],
        "data": {
            "result": {
                "PRODUCT_TITLE": {"text": "USB-C Hub from MTop"},
                "PRICE": {"targetSkuPriceInfo": {"salePriceString": "$19.99"}},
            }
        },
    })


class TestFetchViaChrome:
    """Chrome-based fallback extraction strategies."""

    @pytest.mark.asyncio
    async def test_strategy_a_runparams(self):
        """Strategy A: populated window.runParams extracted from Chrome."""
        run_params = json.dumps({
            "data": {
                "result": {
                    "PRODUCT_TITLE": {"text": "USB Hub from runParams"},
                    "PRICE": {"targetSkuPriceInfo": {"salePriceString": "$14.99"}},
                }
            }
        })
        tab = MagicMock()
        tab.execute_script = _mock_execute_script([
            ("runParams", run_params),
        ])
        tab.get_cookies = AsyncMock(return_value=[])

        solver = MagicMock()
        # with_page calls callback(tab) — simulate that
        async def fake_with_page(url, callback, wait=5, timeout=20):
            return await callback(tab)
        solver.with_page = AsyncMock(side_effect=fake_with_page)

        result = await _fetch_via_chrome("1005006367324382", solver)
        assert result is not None
        assert result["title"] == "USB Hub from runParams"
        assert result["sale_price"] == "$14.99"

    @pytest.mark.asyncio
    async def test_strategy_b_in_chrome_mtop(self):
        """Strategy B: runParams empty, in-Chrome MTop fetch() succeeds."""
        tab = MagicMock()
        tab.execute_script = _mock_execute_script([
            ("runParams", None),  # Strategy A fails
            ("fetch(", _mtop_success_json()),  # Strategy B succeeds
        ])
        tab.get_cookies = AsyncMock(return_value=[
            {"name": "_m_h5_tk", "value": "abc123def_1234567890"},
        ])

        solver = MagicMock()
        async def fake_with_page(url, callback, wait=5, timeout=20):
            return await callback(tab)
        solver.with_page = AsyncMock(side_effect=fake_with_page)

        result = await _fetch_via_chrome("1005006367324382", solver)
        assert result is not None
        assert result["title"] == "USB-C Hub from MTop"

    @pytest.mark.asyncio
    async def test_strategy_c_dom_fallback(self):
        """Strategy C: both A and B fail, DOM extraction from outerHTML."""
        # Build minimal HTML with JSON-LD
        json_ld = json.dumps({
            "@type": "Product",
            "name": "Widget from DOM",
            "offers": {"priceCurrency": "USD", "price": "9.99"},
        })
        html = f'<html><head><script type="application/ld+json">{json_ld}</script></head><body>{"x" * 5000}</body></html>'

        tab = MagicMock()
        tab.execute_script = _mock_execute_script([
            ("runParams", None),  # Strategy A fails
            ("fetch(", ""),  # Strategy B: empty response
            ("outerHTML", html),  # Strategy C
        ])
        tab.get_cookies = AsyncMock(return_value=[])  # No token → skip B

        solver = MagicMock()
        async def fake_with_page(url, callback, wait=5, timeout=20):
            return await callback(tab)
        solver.with_page = AsyncMock(side_effect=fake_with_page)

        result = await _fetch_via_chrome("1005006367324382", solver)
        assert result is not None
        assert result["title"] == "Widget from DOM"
        assert result["sale_price"] == "USD 9.99"

    @pytest.mark.asyncio
    async def test_chrome_busy_returns_none(self):
        """When Chrome lock is held, with_page returns None."""
        solver = MagicMock()
        solver.with_page = AsyncMock(return_value=None)

        result = await _fetch_via_chrome("1005006367324382", solver)
        assert result is None


class TestFetchMtop:
    """MTop fetch integration with error detection."""

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product._get_client")
    async def test_sitem_not_exist_returns_product_not_found(self, mock_get_client):
        """Delisted product returns SITEM_NOT_EXIST in GLOBAL_DATA."""
        from fetchaller.aliexpress.product import _fetch_mtop

        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "result": {
                    "GLOBAL_DATA": {
                        "globalData": {"errorCode": "SITEM_NOT_EXIST"}
                    }
                }
            },
        }
        mock_get_client.return_value = mock_client

        result = await _fetch_mtop("1005006367324382")
        assert result is not None
        assert result.get("_error") == "product_not_found"

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.aliexpress_limiter.wait", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._get_client")
    async def test_delisted_product_returns_clear_error(self, mock_get_client, _mock_limiter):
        """get_product with delisted product returns user-friendly error."""
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "result": {
                    "GLOBAL_DATA": {
                        "globalData": {"errorCode": "SITEM_NOT_EXIST"}
                    }
                }
            },
        }
        mock_get_client.return_value = mock_client

        with patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock) as mock_reviews:
            mock_reviews.return_value = {"error": "no reviews"}
            result = await get_product("1005006367324382")
            assert "error" in result
            assert "not found" in result["error"]

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product._get_client")
    async def test_locale_params_sent_in_request(self, mock_get_client):
        """MTop request includes _lang, _currency, country, clientType."""
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "result": {
                    "PRODUCT_TITLE": {"text": "Widget"},
                    "PRICE": {"targetSkuPriceInfo": {"salePriceString": "$9.99"}},
                }
            },
        }
        mock_get_client.return_value = mock_client

        from fetchaller.aliexpress.product import _fetch_mtop
        await _fetch_mtop("1005006367324382")

        # Check the data dict passed to client.request
        call_args = mock_client.request.call_args
        data = call_args[0][2]  # Third positional arg is data_dict
        assert data["_lang"] == "en_US"
        assert data["_currency"] == "USD"
        assert data["country"] == "US"
        assert data["clientType"] == "pc"


class TestGetProductChromeFallback:
    """Integration: get_product falls through to Chrome when MTop fails."""

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with patch("fetchaller.aliexpress.product.aliexpress_limiter.wait", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_chrome_fallback_on_mtop_failure(self, mock_mtop, mock_reviews):
        """MTop blocked → Chrome fallback produces product content."""
        mock_mtop.return_value = None
        mock_reviews.return_value = {"error": "no reviews"}

        run_params = json.dumps({
            "data": {
                "result": {
                    "PRODUCT_TITLE": {"text": "Chrome Fallback Product"},
                    "PRICE": {"targetSkuPriceInfo": {"salePriceString": "$29.99"}},
                }
            }
        })
        tab = MagicMock()
        tab.execute_script = _mock_execute_script([("runParams", run_params)])
        tab.get_cookies = AsyncMock(return_value=[])

        solver = MagicMock()
        async def fake_with_page(url, callback, wait=5, timeout=20):
            return await callback(tab)
        solver.with_page = AsyncMock(side_effect=fake_with_page)

        result = await get_product("1005006367324382", challenge_solver=solver)
        assert "error" not in result
        assert "Chrome Fallback Product" in result["content"]
        assert "$29.99" in result["content"]

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_no_solver_returns_error(self, mock_mtop, mock_reviews):
        """MTop blocked + no challenge_solver → error."""
        mock_mtop.return_value = None
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382", challenge_solver=None)
        assert "error" in result
