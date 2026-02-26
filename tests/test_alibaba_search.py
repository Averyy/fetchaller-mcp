"""Unit tests for Alibaba.com search module."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from fetchaller.alibaba.search import (
    _build_search_url,
    _format_offer,
    _format_search_results,
    _parse_search_html,
    search_alibaba,
)
from fetchaller.content.alibaba import extract_search_data

# ---------------------------------------------------------------------------
# JSON extraction from SSR HTML
# ---------------------------------------------------------------------------


class TestExtractSearchData:
    """window.__page__data_sse10 extraction."""

    def test_extracts_offer_list(self):
        """Extract _offer_list from embedded JSON."""
        data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": [{"enPureTitle": "Widget"}],
                    "totalCount": 100,
                }
            },
            "_left_filters": {},
        }
        html = f'<html><script>window.__page__data_sse10 = {json.dumps(data)}</script></html>'
        result = extract_search_data(html)
        assert result is not None
        assert result["offerResultData"]["totalCount"] == 100

    def test_no_page_data_returns_none(self):
        html = "<html><body>Regular page</body></html>"
        assert extract_search_data(html) is None

    def test_no_offer_list_returns_none(self):
        data = {"_left_filters": {}, "_header": {}}
        html = f'<html><script>window.__page__data_sse10 = {json.dumps(data)}</script></html>'
        assert extract_search_data(html) is None

    def test_nested_json_with_special_chars(self):
        """Handles deeply nested JSON with escaped strings."""
        data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": [{"enPureTitle": 'Widget "Pro" {v2}'}],
                    "totalCount": 1,
                }
            }
        }
        html = f'<script>window.__page__data_sse10 = {json.dumps(data)}</script>'
        result = extract_search_data(html)
        assert result["offerResultData"]["offers"][0]["enPureTitle"] == 'Widget "Pro" {v2}'


# ---------------------------------------------------------------------------
# Offer formatting
# ---------------------------------------------------------------------------


class TestFormatOffer:
    """Individual offer formatting."""

    def test_formats_complete_offer(self):
        offer = {
            "enPureTitle": "Waterproof Toggle Switch IP67",
            "price": "US$0.50-1.20",
            "moqV2": "100 Pieces",
            "companyName": "Shenzhen Electronics Co.",
            "productId": "1600123456789",
            "reviewCount": "42",
            "reviewScore": "4.7",
            "supplierService": "4.8",
            "countryCode": "CN",
            "customizable": True,
            "goldSupplierYears": "5 yrs",
            "shippingTime": "3-7 days",
        }
        result = _format_offer(1, offer)
        assert "1. Waterproof Toggle Switch IP67" in result
        assert "US$0.50-1.20" in result
        assert "MOQ: 100 Pieces" in result
        assert "Shenzhen Electronics Co." in result
        assert "CN" in result
        # Product rating: score + count combined
        assert "★4.7 (42 reviews)" in result
        # Supplier line: name, country, years, service rating
        assert "service ★4.8" in result
        assert "5 yrs" in result
        assert "Customizable" in result
        assert "Ships: 3-7 days" in result
        assert "alibaba.com/product-detail/_1600123456789.html" in result

    def test_review_count_without_score(self):
        """Review count shown alone when no reviewScore."""
        offer = {"enPureTitle": "Widget", "reviewCount": 10}
        result = _format_offer(1, offer)
        assert "10 reviews" in result

    def test_no_reviews_no_line(self):
        """No review line when both score and count are missing."""
        offer = {"enPureTitle": "Widget"}
        result = _format_offer(1, offer)
        assert "review" not in result.lower()
        assert "★" not in result

    def test_formats_minimal_offer(self):
        """Missing fields don't crash."""
        offer = {"enPureTitle": "Basic Widget"}
        result = _format_offer(1, offer)
        assert "1. Basic Widget" in result

    def test_product_url_used_when_present(self):
        """productUrl field is preferred over building from productId."""
        offer = {
            "enPureTitle": "Widget",
            "productUrl": "//www.alibaba.com/product-detail/Custom-Widget_123.html",
            "productId": "123",
        }
        result = _format_offer(1, offer)
        assert "https://www.alibaba.com/product-detail/Custom-Widget_123.html" in result


# ---------------------------------------------------------------------------
# Search results formatting
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    """Search results list formatting."""

    def test_header_includes_query_and_page(self):
        result = _format_search_results([], "waterproof switch", 1, 0)
        assert '"waterproof switch"' in result
        assert "page 1" in result

    def test_no_products_message(self):
        result = _format_search_results([], "nonexistent", 1, 0)
        assert "No products found" in result

    def test_numbers_sequentially(self):
        offers = [
            {"enPureTitle": "A", "productId": "1"},
            {"enPureTitle": "B", "productId": "2"},
        ]
        result = _format_search_results(offers, "test", 1, 2)
        assert "1. A" in result
        assert "2. B" in result

    def test_page_2_numbering(self):
        """Page 2 continues numbering from page 1 (48 per page)."""
        offers = [{"enPureTitle": "A", "productId": "1"}]
        result = _format_search_results(offers, "test", 2, 100)
        assert "49. A" in result


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


class TestBuildSearchUrl:
    """Search URL construction."""

    def test_basic_query(self):
        url = _build_search_url("waterproof switch")
        assert "SearchText=waterproof%20switch" in url
        assert "alibaba.com/trade/search" in url

    def test_special_chars_encoded(self):
        url = _build_search_url("wireless & power")
        assert "SearchText=wireless%20%26%20power" in url
        # No bare & that would split the parameter
        assert "SearchText=" in url

    def test_page_2(self):
        url = _build_search_url("switch", page=2)
        assert "page=2" in url

    def test_page_1_omitted(self):
        url = _build_search_url("switch", page=1)
        assert "page=" not in url

    def test_sort_and_price_filters(self):
        url = _build_search_url("switch", sort="price_asc", min_price=1, max_price=10)
        assert "sortType=PRICE_ASC" in url
        assert "minPrice=1" in url
        assert "maxPrice=10" in url

    def test_default_sort_omitted(self):
        url = _build_search_url("switch", sort="default")
        assert "sortType=" not in url


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class TestParseSearchHtml:
    """Full parse flow from HTML to formatted content."""

    def test_parses_valid_html(self):
        offers = [{"enPureTitle": "Switch", "productId": "123"}]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": 1,
                }
            }
        }
        html = f'<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>'
        result = _parse_search_html(html, "switch", 1)
        assert result is not None
        assert "Switch" in result["content"]

    def test_returns_none_for_empty_html(self):
        assert _parse_search_html("<html></html>", "test", 1) is None

    def test_returns_none_for_empty_offers(self):
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": [],
                    "totalCount": 0,
                }
            }
        }
        html = f'<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>'
        assert _parse_search_html(html, "test", 1) is None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestSearchIntegration:
    """End-to-end search with mocked fetch_url."""

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with patch("fetchaller.alibaba.search.alibaba_limiter.wait", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_fetch_success(self, mock_fetch):
        """Fetch returns valid SSR HTML with embedded JSON."""
        offers = [{"enPureTitle": "Fast Widget", "productId": "789"}]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": 1,
                }
            }
        }
        html = f'<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>'
        mock_fetch.return_value = {"content": html}

        result = await search_alibaba("widget")
        assert "Fast Widget" in result["content"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_fetch_error_propagates(self, mock_fetch):
        """fetch_url error is returned directly."""
        mock_fetch.return_value = {"error": "Connection failed"}

        result = await search_alibaba("widget")
        assert "error" in result
        assert "Connection failed" in result["error"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_no_data_returns_error(self, mock_fetch):
        """HTML without embedded JSON returns error."""
        mock_fetch.return_value = {"content": "<html><body>Empty</body></html>"}

        result = await search_alibaba("widget")
        assert "error" in result
