"""Unit tests for AliExpress content module (HTML cleanup for fetch tool).

Tests URL detection, CSS selector dispatch through clean_html(), and
postprocessor dispatch through html_to_markdown().
"""

from fetchaller.content.aliexpress import (
    extract_product_id_from_url,
    extract_search_products,
    is_aliexpress,
    is_aliexpress_search_url,
    postprocess_aliexpress,
)
from fetchaller.content.html import _html_to_markdown_sync, clean_html


def _wrap(body_content: str) -> str:
    """Wrap body content in a full HTML document."""
    return f"<html><head><title>Test</title></head><body>{body_content}</body></html>"


class TestIsAliexpress:
    """URL detection for AliExpress domains."""

    def test_www_aliexpress_com(self):
        assert is_aliexpress("https://www.aliexpress.com/item/123.html")

    def test_aliexpress_com(self):
        assert is_aliexpress("https://aliexpress.com/item/123.html")

    def test_mobile(self):
        assert is_aliexpress("https://m.aliexpress.com/item/123.html")

    def test_regional_subdomain(self):
        assert is_aliexpress("https://ko.aliexpress.com/item/123.html")

    def test_aliexpress_ru(self):
        assert is_aliexpress("https://aliexpress.ru/item/123.html")

    def test_aliexpress_us(self):
        assert is_aliexpress("https://www.aliexpress.us/item/123.html")

    def test_not_aliexpress(self):
        assert not is_aliexpress("https://example.com/aliexpress")

    def test_not_faliexpress(self):
        assert not is_aliexpress("https://faliexpress.com/item/123")

    def test_search_page(self):
        assert is_aliexpress("https://www.aliexpress.com/w/wholesale-esp32.html")


class TestExtractProductIdFromUrl:
    """Product ID extraction from AliExpress URLs (hostname-validated)."""

    def test_standard_product_url(self):
        assert extract_product_id_from_url("https://www.aliexpress.com/item/1005006367324382.html") == "1005006367324382"

    def test_mobile_url(self):
        assert extract_product_id_from_url("https://m.aliexpress.com/item/1005006367324382.html") == "1005006367324382"

    def test_regional_url(self):
        assert extract_product_id_from_url("https://ko.aliexpress.com/item/1005006367324382.html") == "1005006367324382"

    def test_url_with_query_params(self):
        assert extract_product_id_from_url(
            "https://www.aliexpress.com/item/1005006367324382.html?spm=a2g0o.detail"
        ) == "1005006367324382"

    def test_non_aliexpress_url_rejected(self):
        """Even if path matches, non-AliExpress hosts return None."""
        assert extract_product_id_from_url("https://example.com/item/1005006367324382.html") is None

    def test_search_url_returns_none(self):
        """Search pages don't have a product ID."""
        assert extract_product_id_from_url("https://www.aliexpress.com/w/wholesale-esp32.html") is None

    def test_without_html_extension(self):
        assert extract_product_id_from_url("https://www.aliexpress.com/item/1005006367324382") == "1005006367324382"


class TestIsAliexpressSearchUrl:
    """Search URL detection."""

    def test_standard_search(self):
        assert is_aliexpress_search_url("https://www.aliexpress.com/w/wholesale-esp32.html")

    def test_search_with_params(self):
        assert is_aliexpress_search_url("https://www.aliexpress.com/w/wholesale-esp32.html?page=2&sortType=price_asc")

    def test_search_multi_word(self):
        assert is_aliexpress_search_url("https://www.aliexpress.com/w/wholesale-usb-c-hub.html")

    def test_product_url_not_search(self):
        assert not is_aliexpress_search_url("https://www.aliexpress.com/item/123456789.html")

    def test_non_aliexpress_rejected(self):
        assert not is_aliexpress_search_url("https://example.com/w/wholesale-test.html")


class TestExtractSearchProducts:
    """Search product extraction from HTML with embedded _init_data_ JSON."""

    def _make_search_html(self, products: list, total: int = 10, page: int = 1) -> str:
        """Build mock AliExpress search page HTML with _init_data_."""
        import json
        data = {
            "data": {
                "root": {
                    "fields": {
                        "mods": {
                            "itemList": {"content": products},
                        },
                        "pageInfo": {"totalResults": total, "page": page},
                    }
                }
            }
        }
        return (
            "<html><script>"
            "/*!-->init-data-start--*/"
            f"_dida_config_._init_data_= {{ data: {json.dumps(data)} }}"
            "/*!-->init-data-end--*/"
            "</script></html>"
        )

    def test_extracts_products(self):
        products = [
            {
                "productId": "123456789",
                "title": {"displayTitle": "Test Widget"},
                "prices": {"salePrice": {"formattedPrice": "$9.99"}},
                "evaluation": {"starRating": "4.5"},
                "trade": {"tradeDesc": "100+ sold"},
            }
        ]
        result = extract_search_products(
            self._make_search_html(products, total=1),
            "https://www.aliexpress.com/w/wholesale-test-widget.html",
        )
        assert result is not None
        assert "Test Widget" in result
        assert "$9.99" in result
        assert "test widget" in result  # query extracted from URL

    def test_returns_none_without_init_data(self):
        result = extract_search_products(
            "<html><body>No data here</body></html>",
            "https://www.aliexpress.com/w/wholesale-test.html",
        )
        assert result is None

    def test_returns_none_for_empty_products(self):
        result = extract_search_products(
            self._make_search_html([], total=0),
            "https://www.aliexpress.com/w/wholesale-test.html",
        )
        assert result is None


class TestSelectorDispatch:
    """CSS selectors are applied when clean_html detects AliExpress."""

    def test_detects_aliexpress_site(self):
        html = _wrap("<p>Product info</p>")
        _, site = clean_html(html, url="https://www.aliexpress.com/item/123.html")
        assert site == "aliexpress"

    def test_removes_header(self):
        html = _wrap(
            '<div class="header--header">Header junk</div>'
            "<p>Product content</p>"
        )
        soup, site = clean_html(html, url="https://www.aliexpress.com/item/123.html")
        assert site == "aliexpress"
        assert "Header junk" not in soup.get_text()
        assert "Product content" in soup.get_text()

    def test_removes_footer(self):
        html = _wrap(
            "<p>Product info</p>"
            '<div class="site-footer">Footer links</div>'
        )
        soup, site = clean_html(html, url="https://www.aliexpress.com/item/123.html")
        assert "Footer links" not in soup.get_text()

    def test_removes_app_download(self):
        html = _wrap(
            '<div class="app-download">Get the app</div>'
            "<p>Product</p>"
        )
        soup, site = clean_html(html, url="https://www.aliexpress.com/item/123.html")
        assert "Get the app" not in soup.get_text()

    def test_removes_hidden_inputs(self):
        """Soup cleanup: hidden inputs are removed."""
        html = _wrap(
            '<input type="hidden" name="csrf" value="token123"/>'
            "<p>Product info</p>"
        )
        soup, site = clean_html(html, url="https://www.aliexpress.com/item/123.html")
        assert site == "aliexpress"
        assert soup.find("input", type="hidden") is None


class TestPostprocessDispatch:
    """Postprocessor runs when going through html_to_markdown."""

    def test_removes_sign_in_line(self):
        html = _wrap("<p>Sign in</p><h1>Product Title</h1><p>Description</p>")
        markdown, _ = _html_to_markdown_sync(html, url="https://www.aliexpress.com/item/123.html")
        assert "Sign in" not in markdown
        assert "Product Title" in markdown

    def test_removes_download_app(self):
        html = _wrap("<p>Download the AliExpress app</p><h1>Product</h1>")
        markdown, _ = _html_to_markdown_sync(html, url="https://www.aliexpress.com/item/123.html")
        assert "Download the AliExpress app" not in markdown
        assert "Product" in markdown


class TestPostprocessAliexpress:
    """Regex postprocessor patterns in isolation."""

    def test_removes_sign_in(self):
        md = "Content\nSign in\nMore content"
        result = postprocess_aliexpress(md)
        assert "Sign in" not in result
        assert "Content" in result

    def test_removes_join(self):
        md = "Content\nJoin\nMore content"
        result = postprocess_aliexpress(md)
        assert "\nJoin\n" not in result

    def test_removes_download_app(self):
        md = "Content\nDownload the AliExpress app now\nProduct"
        result = postprocess_aliexpress(md)
        assert "Download the AliExpress app" not in result

    def test_removes_alibaba_group_footer(self):
        md = "Product info\nAlibaba Group\nSome links\nIntellectual Property Protection\nNext"
        result = postprocess_aliexpress(md)
        assert "Alibaba Group" not in result

    def test_removes_ship_to(self):
        md = "Content\nShip to\nUnited States\nMore"
        result = postprocess_aliexpress(md)
        assert "Ship to" not in result

    def test_preserves_product_content(self):
        md = (
            "# USB C Hub\n\n"
            "Price: $12.99\n\n"
            "★4.8 | 5000+ sold\n\n"
            "- Great quality\n"
            "- Fast shipping\n"
        )
        result = postprocess_aliexpress(md)
        assert "USB C Hub" in result
        assert "$12.99" in result
        assert "★4.8" in result
        assert "Great quality" in result

    def test_collapses_excessive_newlines(self):
        md = "A\n\n\n\n\nB"
        result = postprocess_aliexpress(md)
        assert "\n\n\n" not in result
        assert "A" in result
        assert "B" in result


class TestFormatSearchProductNullFields:
    """Regression: search products with explicit JSON null fields must not crash.

    The API sends `null` (not just an absent key) for prices/evaluation/trade on
    some listings; `.get("prices", {})` only defaults on absent keys, so a null
    slipped through and crashed `.get()`, escaping the module's return-None path.
    """

    def test_null_fields_do_not_crash(self):
        from fetchaller.content.aliexpress import _format_search_product

        product = {
            "title": None,
            "prices": None,
            "evaluation": None,
            "trade": None,
            "productId": "1005001234567890",
        }
        out = _format_search_product(1, product)
        assert "1005001234567890" in out

    def test_null_nested_price_fields(self):
        from fetchaller.content.aliexpress import _format_search_product

        product = {
            "title": {"displayTitle": "Widget"},
            "prices": {"salePrice": None, "originalPrice": None},
            "productId": "9",
        }
        out = _format_search_product(2, product)
        assert "Widget" in out
