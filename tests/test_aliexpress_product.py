"""Unit tests for AliExpress product module."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from fetchaller.aliexpress.product import (
    _extract_from_chrome_html,
    _extract_product_data,
    _fetch_browser_product,
    _format_output,
    _format_rating_breakdown,
    _format_reviews,
    _has_useful_product_data,
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

    def test_extracts_product_id_from_supported_global_data_shape(self):
        data = _extract_product_data(
            {
                "data": {
                    "result": {
                        "GLOBAL_DATA": {
                            "globalData": {"itemId": "1005006367324382"}
                        }
                    }
                }
            }
        )

        assert data["product_id"] == "1005006367324382"

    def test_conflicting_embedded_product_ids_fail_closed(self):
        data = _extract_product_data(
            {
                "data": {
                    "result": {
                        "itemId": "1005006367324382",
                        "GLOBAL_DATA": {
                            "globalData": {"productId": "1005006367324383"}
                        },
                    }
                }
            }
        )

        assert data["product_id"] == ""

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

    @pytest.mark.parametrize("raw_price", [12.99, "12.99"])
    def test_normalizes_currency_bound_raw_mtop_prices(self, raw_price):
        result = {
            "data": {
                "result": {
                    "PRICE": {
                        "targetSkuPriceInfo": {"salePrice": raw_price},
                        "skuPriceInfoMap": {
                            "sku-1": {"salePrice": raw_price},
                        },
                    }
                }
            }
        }

        data = _extract_product_data(result)

        assert data["sale_price"] == "USD 12.99"
        assert data["sku_prices"] == "  SKU sku-1: USD 12.99"

    @pytest.mark.parametrize("raw_price", [0, "bad", 12.99])
    def test_valid_local_price_precedes_invalid_or_raw_price(self, raw_price):
        result = {
            "data": {
                "result": {
                    "PRICE": {
                        "targetSkuPriceInfo": {
                            "salePrice": raw_price,
                            "salePriceLocal": "CAD $17.50",
                        },
                    }
                }
            }
        }

        assert _extract_product_data(result)["sale_price"] == "CAD $17.50"

    @pytest.mark.parametrize("raw_shipping", [2.99, "2.99"])
    def test_normalizes_currency_bound_raw_shipping(self, raw_shipping):
        result = {
            "data": {
                "result": {
                    "SHIPPING": {
                        "originalLayoutResultList": [
                            {"bizData": {"shippingFee": raw_shipping}}
                        ]
                    }
                }
            }
        }

        assert _extract_product_data(result)["shipping"] == "USD 2.99"

    @pytest.mark.parametrize(
        "raw_shipping",
        [0, -1, float("inf"), "Infinity", "Minimum order 100 pieces"],
    )
    def test_rejects_invalid_raw_shipping(self, raw_shipping):
        result = {
            "data": {
                "result": {
                    "SHIPPING": {
                        "originalLayoutResultList": [
                            {"bizData": {"shippingFee": raw_shipping}}
                        ]
                    }
                }
            }
        }

        assert _extract_product_data(result)["shipping"] == ""

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
        assert data["review_count"] == "1247"

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
        assert data["positive_rate"] == "98.2"

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

    @pytest.mark.parametrize(
        ("module_name", "value"),
        [
            ("PRODUCT_TITLE", []),
            ("PRICE", "bad"),
            ("PC_RATING", 1),
            ("SHOP_CARD_PC", []),
            ("QUANTITY_PC", "bad"),
            ("TRADE", []),
            ("SHIPPING", {"generalFreightInfo": []}),
            ("SKU", {"skuProperties": [None, "bad", {"skuPropertyValues": {}}]}),
            ("PRODUCT_PROP_PC", {"showedProps": [None, "bad"]}),
        ],
    )
    def test_malformed_nested_modules_do_not_raise(self, module_name, value):
        result = {"data": {"result": {module_name: value}}}

        assert isinstance(_extract_product_data(result), dict)

    @pytest.mark.parametrize(
        "sale_price",
        [
            "Minimum order 100 pieces",
            "US$-5",
            float("inf"),
            "US$0",
            "NaN",
        ],
    )
    def test_invalid_sale_price_is_removed_during_extraction(self, sale_price):
        result = {
            "data": {
                "result": {
                    "PRICE": {
                        "targetSkuPriceInfo": {"salePriceString": sale_price}
                    }
                }
            }
        }

        assert _extract_product_data(result)["sale_price"] == ""


class TestBrowserProductFallback:
    def test_exact_rendered_jsonld_is_substantive_product_detail(self):
        data = _extract_from_chrome_html(
            """
            <html><head>
              <script type="application/ld+json">
                {
                  "@type": "Product",
                  "name": "Braided USB C Cable - AliExpress",
                  "brand": {"@type": "Brand", "name": "CableCo"},
                  "offers": {
                    "@type": "Offer",
                    "priceCurrency": "USD",
                    "price": "9.99",
                    "seller": {"@type": "Organization", "name": "Cable Store"}
                  }
                }
              </script>
            </head><body></body></html>
            """
        )
        data["product_id"] = "1005006367324382"

        assert data["title"] == "Braided USB C Cable"
        assert data["sale_price"] == "USD 9.99"
        assert data["store_name"] == "Cable Store"
        assert data["specs"] == "  Brand: CableCo"
        assert _has_useful_product_data(
            data,
            expected_product_id="1005006367324382",
        )

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.wafer.AsyncSession")
    async def test_browser_fallback_rejects_redirected_product_identity(
        self,
        session_factory,
    ):
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = None
        session.render.return_value.status_code = 200
        session.render.return_value.url = (
            "https://www.aliexpress.com/item/1005006367324383.html"
        )
        session_factory.return_value = session

        result = await _fetch_browser_product(
            "1005006367324382",
            browser_solver=object(),
            deadline=asyncio.get_running_loop().time() + 10,
        )

        assert result is None


class TestUsefulProductData:
    def test_rejects_title_only_shell_data(self):
        assert not _has_useful_product_data(
            {"title": "Cable title", "sale_price": "", "stock": ""}
        )

    def test_accepts_bound_price_with_store(self):
        assert _has_useful_product_data(
            {
                "product_id": "1005006367324382",
                "title": "Cable title",
                "sale_price": "US $9.99",
                "store_name": "Cable Store",
            },
            expected_product_id="1005006367324382",
        )

    def test_accepts_bound_price_with_specs_without_store(self):
        assert _has_useful_product_data(
            {
                "product_id": "1005006367324382",
                "title": "Cable title",
                "sale_price": "US $9.99",
                "specs": "  Length: 1 m",
            },
            expected_product_id="1005006367324382",
        )

    @pytest.mark.parametrize(
        "data",
        [
            {"title": "Cable title", "sale_price": "US $9.99"},
            {
                "product_id": "1005006367324382",
                "title": "Cable title",
                "sale_price": "US $9.99",
            },
            {
                "product_id": "1005006367324382",
                "title": "Cable title",
                "sale_price": "US $9.99",
                "store_name": "  ",
                "variants": "",
                "specs": "",
            },
        ],
    )
    def test_rejects_title_price_shells_without_binding_and_detail(self, data):
        assert not _has_useful_product_data(
            data, expected_product_id="1005006367324382"
        )

    def test_rejects_embedded_product_id_mismatch(self):
        assert not _has_useful_product_data(
            {
                "product_id": "1005006367324383",
                "title": "Cable title",
                "sale_price": "US $9.99",
                "store_name": "Cable Store",
            },
            expected_product_id="1005006367324382",
        )

    @pytest.mark.parametrize(
        "data",
        [
            {"title": "Cable title", "sale_price": "N/A"},
            {"title": "Cable title", "sale_price": "unavailable"},
            {"title": "Cable title", "sale_price": "US $0.00"},
            {"title": "Cable title", "sale_price": 0},
            {"title": "1005006727707575", "sale_price": "US $9.99"},
            {"title": "Cable title", "stock": 12},
            {"title": "Cable title", "stock": "0"},
            {"title": "Cable title", "stock": "out of stock"},
        ],
    )
    def test_rejects_placeholder_price_and_nonpositive_stock(self, data):
        assert not _has_useful_product_data(data)

    @pytest.mark.parametrize(
        "sale_price",
        [
            "Minimum order 100 pieces",
            "US$-5",
            float("inf"),
            "US$0",
            "NaN",
        ],
    )
    def test_rejects_false_sale_prices(self, sale_price):
        assert not _has_useful_product_data(
            {
                "product_id": "1005006367324382",
                "title": "Real Cable",
                "sale_price": sale_price,
                "store_name": "Cable Store",
            },
            expected_product_id="1005006367324382",
        )

    @pytest.mark.parametrize("title", ["1.2", "① ² Ⅻ"])
    def test_rejects_numeric_category_title_shells(self, title):
        assert not _has_useful_product_data(
            {
                "product_id": "1005006367324382",
                "title": title,
                "sale_price": "US $9.99",
                "store_name": "Cable Store",
            },
            expected_product_id="1005006367324382",
        )

    def test_rejects_numeric_only_detail_modules(self):
        assert not _has_useful_product_data(
            {
                "product_id": "1005006367324382",
                "title": "Real Cable",
                "sale_price": "US $9.99",
                "store_name": "1.2",
                "variants": "1: 2",
                "specs": "①: ²",
            },
            expected_product_id="1005006367324382",
        )


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

    def test_invalid_optional_numeric_metadata_is_omitted(self):
        product_data = {
            "title": "USB Hub",
            "sale_price": "US $12.99",
            "original_price": "US $-5",
            "discount": "Infinity",
            "rating": "NaN",
            "review_count": "Infinity",
            "store_name": "Test Store",
            "positive_rate": "Infinity",
            "stock": "NaN",
            "orders": "Infinity",
            "shipping": "Minimum order 100 pieces",
            "delivery_days": "Infinity",
            "sku_prices": "  SKU 1: US $-5",
            "specs": "  Interface: USB-C",
        }

        result = _format_output("1005006367324382", product_data, None)

        assert "Price: US $12.99" in result
        assert "US $-5" not in result
        assert "Infinity" not in result
        assert "NaN" not in result
        assert "SKU Pricing:" not in result

    def test_untrusted_fields_and_total_output_are_bounded(self):
        huge_line = "  Attribute: " + ("x" * 5_000)
        product_data = {
            "title": "x" * 100_000,
            "sale_price": "US $12.99",
            "store_name": "x" * 100_000,
            "variants": "\n".join([huge_line] * 1_000),
            "sku_prices": "\n".join(
                [f"  SKU {index}: US $1.00" for index in range(1_000)]
            ),
            "specs": "\n".join([huge_line] * 1_000),
        }

        result = _format_output("1005006367324382", product_data, None)

        assert len(result) <= 100_000
        assert "x" * 1_000 not in result


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

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product._get_client")
    async def test_returns_only_a_bound_substantive_product(self, mock_get_client):
        from fetchaller.aliexpress.product import _fetch_mtop

        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "result": {
                    "GLOBAL_DATA": {
                        "globalData": {"itemId": "1005006367324382"}
                    },
                    "PRODUCT_TITLE": {"text": "USB C Cable"},
                    "PRICE": {
                        "targetSkuPriceInfo": {"salePriceString": "US $9.99"}
                    },
                    "SHOP_CARD_PC": {"storeName": "Cable Store"},
                }
            },
        }
        mock_get_client.return_value = mock_client

        product = await _fetch_mtop("1005006367324382")

        assert product is not None
        assert product["product_id"] == "1005006367324382"

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product._get_client")
    async def test_rejects_mtop_detail_bound_to_a_different_product(
        self, mock_get_client
    ):
        from fetchaller.aliexpress.product import _fetch_mtop

        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "result": {
                    "GLOBAL_DATA": {
                        "globalData": {"itemId": "1005006367324383"}
                    },
                    "PRODUCT_TITLE": {"text": "Wrong cable"},
                    "PRICE": {
                        "targetSkuPriceInfo": {"salePriceString": "US $9.99"}
                    },
                    "SHOP_CARD_PC": {"storeName": "Cable Store"},
                }
            },
        }
        mock_get_client.return_value = mock_client

        assert await _fetch_mtop("1005006367324382") is None

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product._get_client")
    async def test_title_only_mtop_success_is_not_product_detail(self, mock_get_client):
        """A shell's Open Graph title cannot satisfy the product contract."""
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"result": {"PRODUCT_TITLE": {"text": "Cable"}}},
        }
        mock_get_client.return_value = mock_client

        from fetchaller.aliexpress.product import _fetch_mtop

        assert await _fetch_mtop("1005006367324382") is None


class TestGetProductMTopFailure:
    """Integration: get_product returns error when MTop fails."""

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with (
            patch(
                "fetchaller.aliexpress.product.aliexpress_limiter.wait",
                new_callable=AsyncMock,
            ),
            patch(
                "fetchaller.aliexpress.product._get_recent_search_product",
                return_value=None,
            ),
        ):
            yield

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_mtop_failure_returns_error(self, mock_mtop, mock_reviews):
        """MTop blocked + no browser solver → error."""
        mock_mtop.return_value = None
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382")
        assert "error" in result

    @pytest.mark.asyncio
    @patch(
        "fetchaller.aliexpress.product._fetch_browser_product",
        new_callable=AsyncMock,
    )
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_mtop_failure_uses_exact_browser_product(
        self,
        mock_mtop,
        mock_reviews,
        mock_browser_product,
    ):
        mock_mtop.return_value = None
        mock_reviews.return_value = {"error": "no reviews"}
        mock_browser_product.return_value = {
            "product_id": "1005006367324382",
            "title": "Braided USB C Cable",
            "sale_price": "USD 9.99",
            "specs": "  Brand: CableCo",
        }

        result = await get_product(
            "1005006367324382",
            browser_solver=object(),
        )

        assert result["content"].startswith("Braided USB C Cable")
        assert "Price: USD 9.99" in result["content"]
        assert "Brand: CableCo" in result["content"]

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product._get_recent_search_product")
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_mtop_failure_uses_verified_search_snapshot_before_browser(
        self,
        mock_mtop,
        mock_reviews,
        recent_snapshot,
    ):
        mock_mtop.return_value = None
        mock_reviews.return_value = {"error": "no reviews"}
        recent_snapshot.return_value = {
            "_source": "search_listing",
            "product_id": "1005006367324382",
            "title": "Braided USB C Cable",
            "sale_price": "USD 9.99",
            "rating": "4.8",
            "orders": "5000+",
        }

        result = await get_product(
            "1005006367324382",
            browser_solver=object(),
        )

        assert result["content"].startswith("Braided USB C Cable")
        assert "Price: USD 9.99" in result["content"]
        assert "5000+ sold" in result["content"]
        assert "verified AliExpress search listing snapshot" in result["content"]

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_provider_self_cancellation_is_a_product_failure(
        self, mock_mtop, mock_reviews
    ):
        mock_mtop.side_effect = asyncio.CancelledError
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382")

        assert result == {
            "error": "Could not retrieve product details. MTop API may be blocked."
        }

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_mtop_failure_with_reviews_is_still_an_error(
        self, mock_mtop, mock_reviews
    ):
        """Reviews cannot be reported as a successful product detail result."""
        mock_mtop.return_value = None
        mock_reviews.return_value = {
            "productEvaluationStatistic": {"evarageStar": 4.9, "totalNum": 12},
            "evaViewList": [{"buyerEval": 100, "buyerFeedback": "Great"}],
        }

        result = await get_product("1005006367324382")

        assert "error" in result
        assert "content" not in result

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_title_only_product_shell_is_not_reported_as_detail(
        self, mock_mtop, mock_reviews
    ):
        mock_mtop.return_value = {"title": "Cable"}
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382")

        assert "error" in result
        assert "content" not in result

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_title_price_shell_is_not_reported_as_detail(
        self, mock_mtop, mock_reviews
    ):
        mock_mtop.return_value = {
            "product_id": "1005006367324382",
            "title": "Cable title",
            "sale_price": "US $9.99",
        }
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382")

        assert "error" in result
        assert "content" not in result

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_tool_rejects_a_mismatched_embedded_product_id(
        self, mock_mtop, mock_reviews
    ):
        mock_mtop.return_value = {
            "product_id": "1005006367324383",
            "title": "Wrong cable",
            "sale_price": "US $9.99",
            "store_name": "Cable Store",
        }
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382")

        assert "error" in result
        assert "content" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sale_price",
        [
            "Minimum order 100 pieces",
            "US$-5",
            float("inf"),
        ],
    )
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_tool_rejects_false_product_prices_end_to_end(
        self, mock_mtop, mock_reviews, sale_price
    ):
        mock_mtop.return_value = {
            "product_id": "1005006367324382",
            "title": "Real Cable",
            "sale_price": sale_price,
            "store_name": "Cable Store",
        }
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382")

        assert "error" in result
        assert "content" not in result

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews", new_callable=AsyncMock)
    @patch("fetchaller.aliexpress.product._fetch_mtop", new_callable=AsyncMock)
    async def test_tool_omits_invalid_optional_metadata_end_to_end(
        self, mock_mtop, mock_reviews
    ):
        mock_mtop.return_value = {
            "product_id": "1005006367324382",
            "title": "Real Cable",
            "sale_price": "US $9.99",
            "original_price": "US $-5",
            "discount": "Infinity",
            "rating": "NaN",
            "review_count": "Infinity",
            "store_name": "Cable Store",
            "positive_rate": "Infinity",
            "orders": "Infinity",
            "stock": "Infinity",
        }
        mock_reviews.return_value = {"error": "no reviews"}

        result = await get_product("1005006367324382")

        assert result["content"].startswith("Real Cable")
        assert "Price: US $9.99" in result["content"]
        assert "US $-5" not in result["content"]
        assert "Infinity" not in result["content"]
        assert "NaN" not in result["content"]

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews")
    @patch("fetchaller.aliexpress.product._fetch_mtop")
    async def test_operation_deadline_cancels_and_drains_both_fetches(
        self, mock_mtop, mock_reviews
    ):
        """Timeout is a single operation deadline, not orphaned background work."""
        mtop_cancelled = asyncio.Event()
        reviews_cancelled = asyncio.Event()

        async def wait_for_cancel(event):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                event.set()
                raise

        async def wait_mtop(*_args, **_kwargs):
            return await wait_for_cancel(mtop_cancelled)

        async def wait_reviews(*_args, **_kwargs):
            return await wait_for_cancel(reviews_cancelled)

        mock_mtop.side_effect = wait_mtop
        mock_reviews.side_effect = wait_reviews

        result = await get_product("1005006367324382", timeout=1)

        assert result == {"error": "AliExpress product retrieval timed out."}
        await asyncio.wait_for(mtop_cancelled.wait(), timeout=0.2)
        await asyncio.wait_for(reviews_cancelled.wait(), timeout=0.2)

    @pytest.mark.asyncio
    @patch("fetchaller.aliexpress.product.fetch_reviews")
    @patch("fetchaller.aliexpress.product._fetch_mtop")
    async def test_operation_deadline_detaches_noncooperative_fetches(
        self, mock_mtop, mock_reviews
    ):
        """A child suppressing cancellation cannot extend the tool deadline."""
        release = asyncio.Event()
        started = [asyncio.Event(), asyncio.Event()]
        finished = [asyncio.Event(), asyncio.Event()]

        async def noncooperative(index, *_args, **_kwargs):
            started[index].set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished[index].set()
            return None

        async def noncooperative_mtop(*args, **kwargs):
            return await noncooperative(0, *args, **kwargs)

        async def noncooperative_reviews(*args, **kwargs):
            return await noncooperative(1, *args, **kwargs)

        mock_mtop.side_effect = noncooperative_mtop
        mock_reviews.side_effect = noncooperative_reviews

        caller = asyncio.create_task(
            get_product("1005006367324382", timeout=1)
        )
        await asyncio.gather(*(event.wait() for event in started))
        done, _ = await asyncio.wait({caller}, timeout=1.25)
        try:
            assert caller in done
            assert caller.result() == {
                "error": "AliExpress product retrieval timed out."
            }
            assert not all(event.is_set() for event in finished)
        finally:
            release.set()
            await asyncio.gather(*(event.wait() for event in finished))

    @pytest.mark.asyncio
    async def test_timeout_range_is_enforced_at_product_layer(self):
        assert await get_product("1005006367324382", timeout=0) == {
            "error": "timeout must be an integer from 1 to 180."
        }
