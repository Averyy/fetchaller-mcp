"""Unit tests for Alibaba.com content module (URL detection, CSS dispatch, postprocessors)."""

from fetchaller.content.alibaba import (
    extract_product_id_from_url,
    is_alibaba,
    is_alibaba_search_url,
    postprocess_alibaba,
)
from fetchaller.content.html import clean_html

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestIsAlibaba:
    """Alibaba.com URL detection."""

    def test_www(self):
        assert is_alibaba("https://www.alibaba.com/product-detail/foo_123.html")

    def test_bare_domain(self):
        assert is_alibaba("https://alibaba.com/trade/search?SearchText=test")

    def test_mobile(self):
        assert is_alibaba("https://m.alibaba.com/product-detail/foo_123.html")

    def test_regional_subdomain(self):
        assert is_alibaba("https://spanish.alibaba.com/product-detail/foo_123.html")
        assert is_alibaba("https://german.alibaba.com/product-detail/foo_123.html")

    def test_not_aliexpress(self):
        assert not is_alibaba("https://www.aliexpress.com/item/123.html")

    def test_not_other(self):
        assert not is_alibaba("https://www.example.com/")


class TestProductIdFromUrl:
    """Product ID extraction from URLs."""

    def test_standard_url(self):
        assert (
            extract_product_id_from_url(
                "https://www.alibaba.com/product-detail/Waterproof-Switch_1600123456789.html"
            )
            == "1600123456789"
        )

    def test_underscore_in_slug(self):
        """Product ID is the last numeric block after underscore."""
        assert (
            extract_product_id_from_url(
                "https://www.alibaba.com/product-detail/USB-C_Hub_8-in-1_1600999888777.html"
            )
            == "1600999888777"
        )

    def test_non_product_url(self):
        assert extract_product_id_from_url("https://www.alibaba.com/trade/search?SearchText=test") is None

    def test_non_alibaba_url(self):
        assert extract_product_id_from_url("https://example.com/product-detail/foo_123.html") is None


class TestSearchUrlDetection:
    """Search URL detection."""

    def test_search_url(self):
        assert is_alibaba_search_url("https://www.alibaba.com/trade/search?SearchText=switch")

    def test_search_url_trailing_slash(self):
        assert is_alibaba_search_url("https://www.alibaba.com/trade/search/?SearchText=switch")

    def test_product_url_not_search(self):
        assert not is_alibaba_search_url("https://www.alibaba.com/product-detail/foo_123.html")

    def test_non_alibaba_not_search(self):
        assert not is_alibaba_search_url("https://example.com/trade/search?SearchText=test")


# ---------------------------------------------------------------------------
# CSS selector dispatch
# ---------------------------------------------------------------------------


class TestCssSelectorDispatch:
    """Alibaba CSS selectors applied through clean_html pipeline."""

    def test_footer_removed(self):
        html = '<html><body><div class="footer">Footer noise</div><p>Content</p></body></html>'
        soup, site = clean_html(html, url="https://www.alibaba.com/product-detail/foo_123.html")
        assert site == "alibaba"
        assert "Footer noise" not in soup.get_text()
        assert "Content" in soup.get_text()

    def test_header_removed(self):
        html = '<html><body><div class="header">Header</div><p>Product</p></body></html>'
        soup, site = clean_html(html, url="https://www.alibaba.com/trade/search?q=test")
        assert site == "alibaba"
        assert "Header" not in soup.get_text()

    def test_hidden_inputs_removed(self):
        html = '<html><body><input type="hidden" value="csrf_token"><p>Product</p></body></html>'
        soup, site = clean_html(html, url="https://www.alibaba.com/product-detail/foo_123.html")
        assert site == "alibaba"
        assert soup.find("input") is None


# ---------------------------------------------------------------------------
# Regex postprocessors
# ---------------------------------------------------------------------------


class TestPostprocessAlibaba:
    """Markdown post-processing for Alibaba.com."""

    def test_strips_sign_in(self):
        assert "Sign in" not in postprocess_alibaba("Content\nSign in\nMore content")

    def test_strips_join_free(self):
        assert "Join Free" not in postprocess_alibaba("Content\nJoin Free\nMore")

    def test_strips_download_app(self):
        result = postprocess_alibaba("Content\nDownload the Alibaba app\nMore")
        assert "Download" not in result

    def test_strips_alibaba_group_footer(self):
        text = "Content\nAlibaba.com Site: International - Espanol\nMore\nIntellectual Property Protection\nEnd"
        result = postprocess_alibaba(text)
        assert "Alibaba.com Site:" not in result

    def test_strips_browse_alphabetically(self):
        text = "Content\nBrowse Alphabetically: A B C D E F\nMore items\n\nReal content"
        result = postprocess_alibaba(text)
        assert "Browse Alphabetically" not in result

    def test_strips_trade_assurance_noise(self):
        result = postprocess_alibaba("Content\nTrade Assurance\nMore")
        assert "Trade Assurance" not in result

    def test_strips_title_prefix(self):
        result = postprocess_alibaba("Alibaba.com - Waterproof Switch")
        assert result == "Waterproof Switch"

    def test_collapses_excessive_newlines(self):
        result = postprocess_alibaba("A\n\n\n\n\nB")
        assert result == "A\n\nB"

    def test_preserves_content(self):
        text = "# Waterproof Switch\n\nPrice: $0.50-1.20\n\nMOQ: 100 pieces"
        assert postprocess_alibaba(text) == text
