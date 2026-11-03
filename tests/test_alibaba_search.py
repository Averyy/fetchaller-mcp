"""Unit tests for Alibaba.com search module."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from fetchaller.alibaba.search import (
    _format_offer,
    _format_search_results,
    _has_usable_offer,
    _offer_product_url,
    _offer_title,
    _parse_search_html,
    _products_url,
    _trade_search_url,
    search_alibaba,
)
from fetchaller.content.alibaba import extract_search_data, is_pro_search_shell, is_zero_result_page

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


class TestSearchUrls:
    """Search URL construction: the keyword listing page first, /trade/search as fallback."""

    def test_listing_page_is_the_slug(self):
        assert _products_url("usb c cable") == "https://www.alibaba.com/products/usb_c_cable.html"

    def test_listing_page_encodes_what_the_slug_can_carry(self):
        url = _products_url("wireless & power")
        assert url == "https://www.alibaba.com/products/wireless_%26_power.html"
        assert _products_url("c++ book") == "https://www.alibaba.com/products/c%2B%2B_book.html"

    @pytest.mark.parametrize("query", ["50/50 mix", "usb_c", "   "])
    def test_queries_the_slug_cannot_carry_have_no_listing_page(self, query):
        # "50/50 mix" came back as the keyword "50" even percent-encoded, and an
        # underscore reads as a space (2026-11-02).
        assert _products_url(query) is None

    def test_page_sort_and_price_band_use_the_sites_names(self):
        url = _products_url("switch", page=2, sort="orders", min_price=1, max_price=10)
        query = parse_qs(urlparse(url).query)

        assert query == {"page": ["2"], "sortType": ["prodSold180"], "pricef": ["1"], "pricet": ["10"]}

    def test_page_1_and_relevance_sort_are_omitted(self):
        assert _products_url("switch", page=1, sort="default") == "https://www.alibaba.com/products/switch.html"

    @pytest.mark.parametrize("sort", ["price_asc", "price_desc"])
    def test_price_sorts_send_no_sort_type(self, sort):
        # sortType=PRICE_ASC and PRICE_DESC answered the same unsorted list.
        assert "sortType" not in _products_url("switch", sort=sort)
        assert "sortType" not in _trade_search_url("switch", sort=sort)

    def test_price_bounds_never_use_exponent_notation(self):
        url = _products_url("switch", min_price=1e20, max_price=2.5)
        query = parse_qs(urlparse(url).query)

        assert query["pricef"] == ["100000000000000000000"]
        assert query["pricet"] == ["2.5"]

    def test_trade_search_is_the_sites_own_form(self):
        url = _trade_search_url("50/50  mix", page=3, min_price=2)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        assert parsed.path == "/trade/search"
        assert query["keywords"] == ["50/50 mix"]
        assert query["originKeywords"] == ["50/50 mix"]
        assert query["IndexArea"] == ["product_en"]
        assert query["page"] == ["3"]
        assert query["pricef"] == ["2"]
        assert "minPrice" not in query


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


def _sse10_page(**sections: object) -> str:
    """A search page as Alibaba ships it: one assignment per section."""
    scripts = "".join(
        f"<script>window.__page__data_sse10.{name} = {json.dumps(data)};</script>"
        for name, data in sections.items()
    )
    return f"<html><script>window.__page__data_sse10={{}};</script>{scripts}</html>"


_ZERO_RESULT = {"offerResultData": {"firstScreen": True, "offers": [], "totalCount": 0}}


class TestZeroResultPage:
    """Alibaba's own "did not match any products" answer."""

    def test_zero_section(self):
        assert is_zero_result_page(_sse10_page(_header={}, _zero=_ZERO_RESULT))

    def test_empty_offer_list_with_zero_total(self):
        assert is_zero_result_page(_sse10_page(_offer_list=_ZERO_RESULT))

    @pytest.mark.parametrize(
        "offer_result",
        [
            {"offers": [{"title": "LED strip"}], "totalCount": 120000},
            {"offers": [], "totalCount": 48},
            {"offers": [], "totalCount": False},
            {"offers": []},
            {"totalCount": 0},
        ],
    )
    def test_anything_else_is_not_a_zero_result(self, offer_result):
        html = _sse10_page(_offer_list={"offerResultData": offer_result})
        assert not is_zero_result_page(html)

    def test_page_without_search_data(self):
        assert not is_zero_result_page("<html><body>Captcha Interception</body></html>")


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
        assert "Could not extract product data" in result["error"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_zero_result_page_reports_alibabas_answer(self, mock_fetch):
        """Alibaba's "did not match" page is its answer, not an extraction failure."""
        mock_fetch.return_value = {"content": _sse10_page(_header={}, _zero=_ZERO_RESULT)}

        result = await search_alibaba("led strip")

        assert "content" not in result
        assert 'no products match "led strip"' in result["error"]
        assert "retrying may return them" in result["error"]
        assert "Could not extract" not in result["error"]


def _offer(title: str, price: str, product_id: str) -> dict:
    return {"enPureTitle": title, "price": price, "productId": product_id}


def _results_page(offers: list[dict], keyword: str | None = None, total: int = 100) -> str:
    offer_list: dict = {"offerResultData": {"offers": offers, "totalCount": total}}
    if keyword is not None:
        offer_list["keyword"] = keyword
    return _sse10_page(_offer_list=offer_list)


# Trimmed from the live shell /trade/search redirected to (2026-11-02).
_PRO_SEARCH_SHELL = (
    '<!doctype html><script>window.__icbu_search_intention_id = "64edde3c79a6427faec5e09d369ea0c4";</script>'
    "<html><head><title>Alibaba.com</title></head><body><div id=\"root\"></div>"
    '<script src="//s.alicdn.com/@g/icbu-search-assets/pro-search/0.0.117/index.js"></script></body></html>'
)


class TestProSearchShell:
    def test_live_shell_is_recognised(self):
        assert is_pro_search_shell(_PRO_SEARCH_SHELL)

    def test_results_page_is_not_the_shell(self):
        page = _results_page([_offer("Widget cable", "US$1.00", "1600123456789")])
        assert not is_pro_search_shell(page)
        assert not is_pro_search_shell(page + "icbu-search-assets/pro-search/")


class TestSearchRouting:
    """Listing page first; /trade/search only when the slug cannot carry the query."""

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with patch("fetchaller.alibaba.search.alibaba_limiter.wait", new_callable=AsyncMock):
            yield

    @staticmethod
    def _urls(mock_fetch) -> list[str]:
        return [call.kwargs["url"] for call in mock_fetch.await_args_list]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_listing_page_is_one_request(self, mock_fetch):
        mock_fetch.return_value = {
            "content": _results_page([_offer("USB C cable", "$2.40-3.39", "1601833304780")], keyword="usb c cable")
        }

        result = await search_alibaba("usb c cable", min_price=1, max_price=2)

        assert "USB C cable" in result["content"]
        assert self._urls(mock_fetch) == ["https://www.alibaba.com/products/usb_c_cable.html?pricef=1&pricet=2"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_listing_page_that_searched_another_keyword_falls_back(self, mock_fetch):
        mock_fetch.side_effect = [
            {"content": _results_page([_offer("Motor oil 50:1", "$1.00", "1601000000001")], keyword="50")},
            {"content": _results_page([_offer("50/50 mix powder", "$3.00", "1601000000002")], keyword="50 50 mix")},
        ]

        # A query the slug can carry, but the page reports a different keyword.
        result = await search_alibaba("50 mix")

        urls = self._urls(mock_fetch)
        assert urls[0].startswith("https://www.alibaba.com/products/")
        assert urls[1].startswith("https://www.alibaba.com/trade/search?")
        assert "50/50 mix powder" in result["content"]
        assert "Motor oil" not in result["content"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_slash_query_goes_straight_to_trade_search(self, mock_fetch):
        mock_fetch.return_value = {"content": _results_page([_offer("50/50 mix powder", "$3.00", "1601000000002")])}

        await search_alibaba("50/50 mix")

        urls = self._urls(mock_fetch)
        assert len(urls) == 1
        assert parse_qs(urlparse(urls[0]).query)["keywords"] == ["50/50 mix"]

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_listing_page_404_falls_back(self, mock_fetch):
        mock_fetch.side_effect = [
            {"error": "HTTP 404", "body": "Not found"},
            {"content": _results_page([_offer("Widget cable", "$1.00", "1601000000003")])},
        ]

        result = await search_alibaba("widget")

        assert "Widget cable" in result["content"]
        assert self._urls(mock_fetch)[1].startswith("https://www.alibaba.com/trade/search?")

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_trade_search_retries_past_the_pro_search_shell(self, mock_fetch):
        mock_fetch.side_effect = [
            {"content": _PRO_SEARCH_SHELL},
            {"content": _results_page([_offer("50/50 mix powder", "$3.00", "1601000000002")])},
        ]

        result = await search_alibaba("50/50 mix")

        assert "50/50 mix powder" in result["content"]
        assert mock_fetch.await_count == 2

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_pro_search_shell_every_time_is_named(self, mock_fetch):
        mock_fetch.return_value = {"content": _PRO_SEARCH_SHELL}

        result = await search_alibaba("50/50 mix")

        # Three /trade/search tries, then the spaced listing page, which is a
        # shell too (no keyword), so it is not used.
        assert mock_fetch.await_count == 4
        assert "content" not in result
        assert '"pro search" app page' in result["error"]
        assert "Could not extract" not in result["error"]


class TestSlashQueryLastResort:
    """A / query whose /trade/search tries all get the shell reads the spaced listing page, disclosed."""

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with patch("fetchaller.alibaba.search.alibaba_limiter.wait", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_spaced_listing_page_is_used_and_disclosed(self, mock_fetch):
        mock_fetch.side_effect = [{"content": _PRO_SEARCH_SHELL}] * 3 + [
            {"content": _results_page([_offer("50/50 mix powder", "$3.00", "1601000000002")], keyword="50 50 mix")}
        ]

        result = await search_alibaba("50/50 mix")

        urls = [call.kwargs["url"] for call in mock_fetch.await_args_list]
        assert urls[-1] == "https://www.alibaba.com/products/50_50_mix.html"
        assert "50/50 mix powder" in result["content"]
        assert 'its listing page for "50 50 mix"' in result["content"]
        assert result["content"].startswith('Search: "50/50 mix"')

    @pytest.mark.asyncio
    @patch("fetchaller.alibaba.search.fetch_url", new_callable=AsyncMock)
    async def test_spaced_page_that_searched_something_else_is_not_used(self, mock_fetch):
        mock_fetch.side_effect = [{"content": _PRO_SEARCH_SHELL}] * 3 + [
            {"content": _results_page([_offer("Motor oil 50:1", "$1.00", "1601000000001")], keyword="50")}
        ]

        result = await search_alibaba("50/50 mix")

        assert "content" not in result
        assert '"pro search" app page' in result["error"]


class TestLocalPriceOrder:
    """Alibaba has no price sort; the price sorts order the returned page here."""

    _OFFERS = [
        _offer("Middle widget", "$2.40-3.39", "1601000000011"),
        _offer("Cheap widget", "US$0.7904-3.74", "1601000000012"),
        _offer("Dear widget", "$1,234.00", "1601000000013"),
        _offer("Mid-cheap widget", "$1.22", "1601000000014"),
    ]

    def _titles(self, content: str) -> list[str]:
        return [line.split(". ", 1)[1] for line in content.splitlines() if line[:1].isdigit() and ". " in line]

    def test_price_asc_orders_the_page_and_says_so(self):
        result = _parse_search_html(_results_page(self._OFFERS), "widget", 1, "price_asc")

        assert self._titles(result["content"]) == ["Cheap widget", "Mid-cheap widget", "Middle widget", "Dear widget"]
        assert "Alibaba.com has no price sort" in result["content"]
        assert "lowest first" in result["content"]

    def test_price_desc(self):
        result = _parse_search_html(_results_page(self._OFFERS), "widget", 1, "price_desc")

        assert self._titles(result["content"]) == ["Dear widget", "Middle widget", "Mid-cheap widget", "Cheap widget"]
        assert "highest first" in result["content"]

    @pytest.mark.parametrize("sort", ["default", "orders"])
    def test_other_sorts_keep_alibabas_order_without_a_note(self, sort):
        result = _parse_search_html(_results_page(self._OFFERS), "widget", 1, sort)

        assert self._titles(result["content"])[0] == "Middle widget"
        assert "no price sort" not in result["content"]
