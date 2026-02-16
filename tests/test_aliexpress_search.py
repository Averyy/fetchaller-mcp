"""Unit tests for AliExpress search module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fetchaller.aliexpress.search import (
    _build_search_url,
    _parse_search_html,
    _search_via_chrome,
    search_aliexpress,
)
from fetchaller.content.aliexpress import (
    _format_search_product,
    extract_init_data,
    format_search_results,
)


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
        html = (
            "<html><script>"
            f"_dida_config_._init_data_= {{ data: {json.dumps(data)} }}"
            "</script></html>"
        )
        result = extract_init_data(html)
        assert result == data

    def test_nested_braces(self):
        """Brace counting correctly handles nested objects."""
        data = {"a": {"b": {"c": {"d": 1}}}, "e": [{"f": 2}]}
        html = (
            "/*!-->init-data-start--*/"
            f"_dida_config_._init_data_= {{ data: {json.dumps(data)} }}"
            "/*!-->init-data-end--*/"
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
            "/*!-->init-data-start--*/"
            "_dida_config_._init_data_= { data: {invalid json here} }"
            "/*!-->init-data-end--*/"
        )
        assert extract_init_data(html) is None

    def test_ignores_init_data_in_conditionals(self):
        """_init_data_ appearing without markers or assignment should not match."""
        html = (
            "<script>if (window._init_data_) { console.log('exists'); }</script>"
            "<body>Content</body>"
        )
        assert extract_init_data(html) is None


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
            {"productId": "1", "title": {"displayTitle": "A"}, "prices": {}, "evaluation": {}, "trade": {}},
            {"productId": "2", "title": {"displayTitle": "B"}, "prices": {}, "evaluation": {}, "trade": {}},
        ]
        result = format_search_results(products, "test", 1, 2)
        assert "1. A" in result
        assert "2. B" in result

    def test_page_2_numbering(self):
        """Page 2 should continue numbering from where page 1 left off."""
        products = [
            {"productId": "1", "title": {"displayTitle": "A"}, "prices": {}, "evaluation": {}, "trade": {}},
        ]
        result = format_search_results(products, "test", 2, 100)
        assert "61. A" in result


class TestRateLimiting:
    """Rate limiting uses shared domain limiter."""

    def test_uses_shared_aliexpress_limiter(self):
        """Verify search uses the shared aliexpress domain limiter."""
        from fetchaller.aliexpress import search as search_mod
        from fetchaller.ratelimit import aliexpress_limiter

        assert hasattr(search_mod, "aliexpress_limiter")
        assert search_mod.aliexpress_limiter is aliexpress_limiter


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


class TestParseSearchHtml:
    """_init_data_ extraction helper."""

    def test_parses_valid_html(self):
        products = [
            {"productId": "123", "title": {"displayTitle": "Widget"}, "prices": {}, "evaluation": {}, "trade": {}},
        ]
        init_data = {
            "data": {"root": {"fields": {
                "mods": {"itemList": {"content": products}},
                "pageInfo": {"totalResults": 1, "page": 1},
            }}}
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


class TestSearchViaChrome:
    """Chrome-based search with session reuse."""

    @pytest.mark.asyncio
    async def test_direct_search_with_existing_session(self):
        """If Chrome already has a session, search works without homepage warming."""
        products = [
            {"productId": "456", "title": {"displayTitle": "Chrome Widget"}, "prices": {}, "evaluation": {}, "trade": {}},
        ]
        init_data = {
            "data": {"root": {"fields": {
                "mods": {"itemList": {"content": products}},
                "pageInfo": {"totalResults": 1, "page": 1},
            }}}
        }
        init_html = (
            "<!-->init-data-start--*/\n"
            f"window._dida_config_._init_data_= {{ data: {json.dumps(init_data)} }}"
            "\n<!-->init-data-end--*/"
        )

        tab = MagicMock()
        tab.go_to = AsyncMock()
        async def mock_execute(js, **kwargs):
            if "window.location.href" in js:
                return {"result": {"result": {"value": "https://www.aliexpress.com/w/wholesale-test.html?page=1"}}}
            if "_dida_config_" in js and "outerHTML" not in js:
                return {"result": {"result": {"value": True}}}
            if "outerHTML" in js:
                return {"result": {"result": {"value": init_html}}}
            return {"result": {"result": {"value": None}}}
        tab.execute_script = mock_execute

        solver = MagicMock()
        async def fake_with_page(url, callback, wait=1, timeout=30):
            return await callback(tab)
        solver.with_page = AsyncMock(side_effect=fake_with_page)

        result = await _search_via_chrome(
            "https://www.aliexpress.com/w/wholesale-test.html", "test", solver
        )
        assert result is not None
        assert "Chrome Widget" in result["content"]
        # with_page called with the SEARCH URL directly (not homepage)
        solver.with_page.assert_called_once()
        assert "wholesale-test" in solver.with_page.call_args[0][0]
        # No extra navigation needed — session was already valid
        assert tab.go_to.call_count == 0

    @pytest.mark.asyncio
    async def test_tmd_triggers_homepage_warming_then_retry(self):
        """First attempt gets TMD-blocked → warms from homepage → retries search."""
        products = [
            {"productId": "789", "title": {"displayTitle": "Warmed Widget"}, "prices": {}, "evaluation": {}, "trade": {}},
        ]
        init_data = {
            "data": {"root": {"fields": {
                "mods": {"itemList": {"content": products}},
                "pageInfo": {"totalResults": 1, "page": 1},
            }}}
        }
        init_html = (
            "<!-->init-data-start--*/\n"
            f"window._dida_config_._init_data_= {{ data: {json.dumps(init_data)} }}"
            "\n<!-->init-data-end--*/"
        )

        tab = MagicMock()
        tab.go_to = AsyncMock()
        call_count = 0
        async def mock_execute(js, **kwargs):
            nonlocal call_count
            if "window.location.href" in js:
                call_count += 1
                if call_count <= 1:
                    # First poll cycle: TMD block (before session warming)
                    return {"result": {"result": {"value": "https://www.aliexpress.com/_____tmd_____/punish"}}}
                # After warming: normal search URL
                return {"result": {"result": {"value": "https://www.aliexpress.com/w/wholesale-test.html"}}}
            if "_dida_config_" in js and "outerHTML" not in js:
                return {"result": {"result": {"value": True}}}
            if "outerHTML" in js:
                return {"result": {"result": {"value": init_html}}}
            return {"result": {"result": {"value": None}}}
        tab.execute_script = mock_execute

        solver = MagicMock()
        async def fake_with_page(url, callback, wait=1, timeout=30):
            return await callback(tab)
        solver.with_page = AsyncMock(side_effect=fake_with_page)

        result = await _search_via_chrome(
            "https://www.aliexpress.com/w/wholesale-test.html", "test", solver
        )
        assert result is not None
        assert "Warmed Widget" in result["content"]
        # Should have navigated: homepage (warming) then search URL (retry)
        go_to_urls = [str(c) for c in tab.go_to.call_args_list]
        assert any("aliexpress.com/" in u and "wholesale" not in u for u in go_to_urls), "Should visit homepage"
        assert any("wholesale-test" in u for u in go_to_urls), "Should retry search"

    @pytest.mark.asyncio
    async def test_chrome_busy_returns_none(self):
        solver = MagicMock()
        solver.with_page = AsyncMock(return_value=None)

        result = await _search_via_chrome(
            "https://www.aliexpress.com/w/wholesale-test.html", "test", solver
        )
        assert result is None


class TestSearchIntegration:
    """End-to-end search — always Chrome, no curl_cffi."""

    @pytest.fixture(autouse=True)
    def _skip_rate_limit(self):
        with patch("fetchaller.aliexpress.search.aliexpress_limiter.wait", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    async def test_chrome_success(self):
        """Chrome session warming + search page → products extracted."""
        products = [
            {"productId": "789", "title": {"displayTitle": "Fast Widget"}, "prices": {}, "evaluation": {}, "trade": {}},
        ]
        init_data = {
            "data": {"root": {"fields": {
                "mods": {"itemList": {"content": products}},
                "pageInfo": {"totalResults": 1, "page": 1},
            }}}
        }
        init_html = (
            "<!-->init-data-start--*/\n"
            f"window._dida_config_._init_data_= {{ data: {json.dumps(init_data)} }}"
            "\n<!-->init-data-end--*/"
        )

        tab = MagicMock()
        tab.go_to = AsyncMock()
        async def mock_execute(js, **kwargs):
            if "window.location.href" in js:
                return {"result": {"result": {"value": "https://www.aliexpress.com/w/wholesale-widget.html"}}}
            if "_dida_config_" in js and "outerHTML" not in js:
                return {"result": {"result": {"value": True}}}
            if "outerHTML" in js:
                return {"result": {"result": {"value": init_html}}}
            return {"result": {"result": {"value": None}}}
        tab.execute_script = mock_execute

        solver = MagicMock()
        async def fake_with_page(url, callback, wait=1, timeout=30):
            return await callback(tab)
        solver.with_page = AsyncMock(side_effect=fake_with_page)

        result = await search_aliexpress("widget", challenge_solver=solver)
        assert "Fast Widget" in result["content"]
        # with_page called with search URL directly
        solver.with_page.assert_called_once()
        assert "wholesale-widget" in solver.with_page.call_args[0][0]

    @pytest.mark.asyncio
    async def test_no_solver_returns_error_immediately(self):
        """No challenge_solver → error without waiting for rate limit."""
        result = await search_aliexpress("widget", challenge_solver=None)
        assert "error" in result
        assert "Chrome" in result["error"]
