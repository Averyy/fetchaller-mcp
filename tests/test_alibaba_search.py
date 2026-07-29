"""Unit tests for Alibaba.com search module."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from fetchaller.alibaba.search import (
    _build_search_url,
    _format_offer,
    _format_search_results,
    _has_usable_offer,
    _offer_product_url,
    _offer_title,
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
        html = f"<html><script>window.__page__data_sse10 = {json.dumps(data)}</script></html>"
        result = extract_search_data(html)
        assert result is not None
        assert result["offerResultData"]["totalCount"] == 100

    def test_no_page_data_returns_none(self):
        html = "<html><body>Regular page</body></html>"
        assert extract_search_data(html) is None

    def test_no_offer_list_returns_none(self):
        data = {"_left_filters": {}, "_header": {}}
        html = f"<html><script>window.__page__data_sse10 = {json.dumps(data)}</script></html>"
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
        html = f"<script>window.__page__data_sse10 = {json.dumps(data)}</script>"
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

    def test_current_live_title_field_strips_embedded_markup(self):
        offer = {
            "title": (
                "<img src='https://cdn.example/badge.png'></img>"
                "<span> </span>EAST New Waterproof Momentary "
                "Self Locking 22mm Push Button Switches"
            ),
            "productId": "1601715160125",
        }

        assert _offer_title(offer) == ("EAST New Waterproof Momentary Self Locking 22mm Push Button Switches")
        assert _format_offer(1, offer).startswith("1. EAST New Waterproof Momentary Self Locking")

    def test_structured_title_fallback(self):
        assert _offer_title({"title": {"displayTitle": "Structured Widget"}}) == "Structured Widget"

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
            "productUrl": ("//www.alibaba.com/product-detail/Custom-Widget_1600123456789.html"),
            "productId": "1600123456789",
        }
        result = _format_offer(1, offer)
        assert ("https://www.alibaba.com/product-detail/Custom-Widget_1600123456789.html") in result

    @pytest.mark.parametrize(
        "offer",
        [
            {
                "enPureTitle": "Widget",
                "price": "US$1.00",
                "productUrl": "http://www.alibaba.com/product-detail/X_1600123456789.html",
            },
            {
                "enPureTitle": "Widget",
                "price": "US$1.00",
                "productUrl": "https://alibaba.com.evil.test/product-detail/X_1600123456789.html",
            },
            {
                "enPureTitle": "Widget",
                "price": "US$1.00",
                "productUrl": "https://user@www.alibaba.com/product-detail/X_1600123456789.html",
            },
            {
                "enPureTitle": "Widget",
                "price": "US$1.00",
                "productUrl": "https://www.alibaba.com/trade/search_1600123456789.html",
            },
            {
                "enPureTitle": "Widget",
                "price": "US$1.00",
                "productId": "1600123456789",
                "productUrl": "https://www.alibaba.com/product-detail/X_1600123456790.html",
            },
        ],
    )
    def test_rejects_untrusted_or_mismatched_product_url(self, offer):
        assert _offer_product_url(offer) is None

    def test_accepts_strict_bare_product_id(self):
        assert _offer_product_url({"productId": "1600123456789"}) == (
            "https://www.alibaba.com/product-detail/_1600123456789.html"
        )

    def test_non_finite_optional_metadata_is_omitted(self):
        offer = {
            "enPureTitle": "Substantive Widget",
            "price": "US$1.00",
            "productId": "1600123456789",
            "companyName": float("inf"),
            "reviewScore": float("nan"),
            "reviewCount": float("-inf"),
        }

        result = _format_offer(1, offer)

        assert "inf" not in result.lower()
        assert "nan" not in result.lower()

    @pytest.mark.parametrize(
        "value",
        ["NaN", "Infinity", "1e400", "-1", "5.1"],
    )
    def test_invalid_string_numeric_metadata_is_omitted(self, value):
        offer = {
            "enPureTitle": "Substantive Widget",
            "price": "US$1.00",
            "productId": "1600123456789",
            "reviewScore": value,
            "reviewCount": value,
            "supplierService": value,
        }

        result = _format_offer(1, offer)

        assert "review" not in result
        assert "★" not in result

    @pytest.mark.parametrize(
        "override",
        [
            {"enPureTitle": ""},
            {"enPureTitle": "1600123456789"},
            {"enPureTitle": "1.2"},
            {"enPureTitle": "²Ⅻ①"},
            {"enPureTitle": "..."},
            {"price": "Contact supplier"},
            {"price": "US$0.00"},
            {"price": "Minimum order 100 pieces"},
            {"price": "US$-1.00"},
            {"productId": "1234"},
            {"productUrl": ("https://evil.test/product-detail/Widget_1600123456789.html")},
        ],
    )
    def test_rejects_nonsemantic_offer_shells(self, override):
        offer = {
            "enPureTitle": "Substantive Widget",
            "price": "US$1.00",
            "productId": "1600123456789",
        }
        offer.update(override)
        assert not _has_usable_offer(offer)

    @pytest.mark.parametrize("title", ["Café Widget", "防水开关"])
    def test_accepts_unicode_letter_titles(self, title):
        assert _has_usable_offer(
            {
                "enPureTitle": title,
                "price": "US$1.00",
                "productId": "1600123456789",
            }
        )


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
            {
                "enPureTitle": "A",
                "price": "US$1.00",
                "productId": "1600123456789",
            },
            {
                "enPureTitle": "B",
                "price": "US$2.00",
                "productId": "1600123456790",
            },
        ]
        result = _format_search_results(offers, "test", 1, 2)
        assert "1. A" in result
        assert "2. B" in result

    def test_page_2_numbering(self):
        """Page 2 continues numbering from page 1 (48 per page)."""
        offers = [
            {
                "enPureTitle": "A",
                "price": "US$1.00",
                "productId": "1600123456789",
            }
        ]
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

    def test_default_sort_omitted(self):
        url = _build_search_url("switch", sort="default")
        assert "sortType=" not in url


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class TestParseSearchHtml:
    """Full parse flow from HTML to formatted content."""

    def test_parses_valid_html(self):
        offers = [
            {
                "enPureTitle": "Switch",
                "price": "US$1.00",
                "productId": "1600123456789",
            }
        ]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": 1,
                }
            }
        }
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"
        result = _parse_search_html(html, "switch", 1)
        assert result is not None
        assert "Switch" in result["content"]

    def test_filters_invalid_offers_but_keeps_valid_results(self):
        offers = [
            {
                "enPureTitle": "Challenge Shell",
                "price": "Contact supplier",
                "productId": "1600123456788",
            },
            {
                "enPureTitle": "Real Switch",
                "price": "US$1.00",
                "productId": "1600123456789",
            },
            {
                "enPureTitle": "Wrong Host",
                "price": "US$2.00",
                "productUrl": ("https://evil.test/product-detail/Wrong_1600123456790.html"),
            },
        ]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": 3,
                }
            }
        }
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"

        result = _parse_search_html(html, "switch", 1)

        assert result is not None
        assert "Real Switch" in result["content"]
        assert "Challenge Shell" not in result["content"]
        assert "Wrong Host" not in result["content"]

    def test_returns_none_when_only_nonsemantic_offers_exist(self):
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": [
                        {
                            "enPureTitle": "1600123456789",
                            "price": "US$0.00",
                            "productId": "1600123456789",
                        }
                    ],
                    "totalCount": 1,
                }
            }
        }
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"
        assert _parse_search_html(html, "switch", 1) is None

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
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"
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
        offers = [
            {
                "enPureTitle": "Fast Widget",
                "price": "US$1.00",
                "productId": "1600123456789",
            }
        ]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": 1,
                }
            }
        }
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"
        mock_fetch.return_value = {"content": html}

        result = await search_alibaba("widget")
        assert "Fast Widget" in result["content"]
        assert 0 < mock_fetch.await_args.kwargs["timeout"] <= 180

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_explicit_timeout_reaches_browser_capable_fetch(self, mock_fetch):
        """Callers can budget enough time for repeated live Baxia attempts."""
        offers = [
            {
                "enPureTitle": "Fast Widget",
                "price": "US$1.00",
                "productId": "1600123456789",
            }
        ]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": 1,
                }
            }
        }
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"
        mock_fetch.return_value = {"content": html}

        result = await search_alibaba("widget", timeout=240)

        assert "Fast Widget" in result["content"]
        assert 0 < mock_fetch.await_args.kwargs["timeout"] <= 240

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_limiter_wait_consumes_the_end_to_end_timeout(self, mock_fetch):
        async def delayed_wait(*, extra_delay):
            assert extra_delay == 2.0
            await asyncio.sleep(0.05)

        with patch(
            "fetchaller.alibaba.search.alibaba_limiter.wait",
            side_effect=delayed_wait,
        ):
            started = time.monotonic()
            result = await search_alibaba("widget", timeout=0.01)
            elapsed = time.monotonic() - started

        assert result == {
            "error": (
                "Request timed out after 0.01s. "
                "Try increasing the timeout parameter for slow servers."
            )
        }
        assert elapsed < 0.1
        mock_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_queued_limiter_caller_expires_without_late_fetch(self, mock_fetch):
        offers = [
            {
                "enPureTitle": "Fast Widget",
                "price": "US$1.00",
                "productId": "1600123456789",
            }
        ]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": 1,
                }
            }
        }
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"
        mock_fetch.return_value = {"content": html}
        lock = asyncio.Lock()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        wait_count = 0

        async def queued_wait(*, extra_delay):
            nonlocal wait_count
            assert extra_delay == 2.0
            async with lock:
                wait_count += 1
                if wait_count == 1:
                    first_entered.set()
                    await release_first.wait()

        with patch(
            "fetchaller.alibaba.search.alibaba_limiter.wait",
            side_effect=queued_wait,
        ):
            first = asyncio.create_task(search_alibaba("first", timeout=0.2))
            await first_entered.wait()
            second = await search_alibaba("second", timeout=0.01)
            release_first.set()
            first_result = await first

        assert "Fast Widget" in first_result["content"]
        assert "timed out after 0.01s" in second["error"]
        assert mock_fetch.await_count == 1

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_synchronous_parse_is_inside_end_to_end_timeout(self, mock_fetch):
        mock_fetch.return_value = {"content": "<html>valid transport</html>"}

        def slow_parse(*_args):
            time.sleep(0.05)
            return {"content": "too late"}

        with patch(
            "fetchaller.alibaba.search._parse_search_html",
            side_effect=slow_parse,
        ):
            started = time.monotonic()
            result = await search_alibaba("widget", timeout=0.01)
            elapsed = time.monotonic() - started

        assert "timed out after 0.01s" in result["error"]
        assert elapsed < 0.1

    def test_valid_oversized_offer_array_is_capped_before_formatting(self):
        offers = [
            {
                "enPureTitle": f"Bounded Widget {index}",
                "price": "US$1.00",
                "productId": str(1600000000000 + index),
            }
            for index in range(10_000)
        ]
        page_data = {
            "_offer_list": {
                "offerResultData": {
                    "offers": offers,
                    "totalCount": len(offers),
                }
            }
        }
        html = f"<script>window.__page__data_sse10 = {json.dumps(page_data)}</script>"

        result = _parse_search_html(html, "widget", 1)

        assert result is not None
        assert result["content"].count("https://www.alibaba.com/product-detail/") == 48
        assert "Bounded Widget 47" in result["content"]
        assert "Bounded Widget 48" not in result["content"]
        assert len(result["content"]) < 100_000

    def test_search_output_cap_names_any_omitted_products(self):
        slug = "s" * 3000
        offers = [
            {
                "enPureTitle": f"Large URL Widget {index}",
                "price": "US$1.00",
                "productId": str(1600000000000 + index),
                "productUrl": (
                    "https://www.alibaba.com/product-detail/"
                    f"{slug}_{1600000000000 + index}.html"
                ),
            }
            for index in range(48)
        ]

        content = _format_search_results(offers, "widget", 1, 48)

        assert len(content) <= 100_000
        assert "Additional products omitted" in content

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
