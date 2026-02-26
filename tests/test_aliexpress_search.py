"""Unit tests for AliExpress search module."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import fetchaller.aliexpress.search as search_mod
from fetchaller.aliexpress.search import (
    _build_search_url,
    _parse_search_html,
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
