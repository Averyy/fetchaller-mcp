"""Tests for eBay search result extraction from HTML DOM."""


from bs4 import BeautifulSoup

from fetchaller.content.ebay import (
    extract_ebay_search_results,
    is_ebay_search_url,
    postprocess_ebay,
)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestIsEbaySearchUrl:
    def test_standard_search(self):
        assert is_ebay_search_url("https://www.ebay.com/sch/i.html?_nkw=bicycle")

    def test_category_search(self):
        assert is_ebay_search_url("https://www.ebay.com/sch/Bikes/177831/bn_1865546")

    def test_ebay_ca(self):
        assert is_ebay_search_url("https://www.ebay.ca/sch/i.html?_nkw=bicycle")

    def test_product_page_not_search(self):
        assert not is_ebay_search_url("https://www.ebay.com/itm/123456789")

    def test_non_ebay(self):
        assert not is_ebay_search_url("https://example.com/sch/i.html?_nkw=bicycle")


# ---------------------------------------------------------------------------
# Search result extraction from HTML
# ---------------------------------------------------------------------------


_SEARCH_HTML = """
<html><body>
<div class="srp-controls__count-heading">
    <span class="BOLD">1,234</span> results for bicycle
</div>
<ul class="srp-results">
    <li class="s-item">
        <div class="s-item__title"><span>Trek Marlin 5</span></div>
        <span class="s-item__price">$349.99</span>
        <span class="SECONDARY_INFO">New</span>
        <span class="s-item__shipping">Free shipping</span>
        <a class="s-item__link" href="https://www.ebay.com/itm/111?_trksid=abc"></a>
    </li>
    <li class="s-item">
        <div class="s-item__title"><span>Specialized Allez</span></div>
        <span class="s-item__price">$275.00</span>
        <span class="SECONDARY_INFO">Used</span>
        <span class="s-item__shipping">+$25.00 shipping</span>
        <a class="s-item__link" href="https://www.ebay.com/itm/222"></a>
    </li>
    <li class="s-item">
        <div class="s-item__title"><span>Shop on eBay</span></div>
    </li>
</ul>
</body></html>
"""


class TestExtractEbaySearchResults:
    def test_extracts_items(self):
        soup = BeautifulSoup(_SEARCH_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=bicycle")

        # Should have injected a marker div
        marker = soup.find("div", id="ebay-search-marker")
        assert marker is not None
        text = marker.string
        assert "Trek Marlin 5" in text
        assert "$349.99" in text
        assert "New" in text
        assert "Free shipping" in text
        assert "https://www.ebay.com/itm/111" in text
        # Tracking params should be stripped
        assert "_trksid" not in text

    def test_excludes_shop_on_ebay(self):
        soup = BeautifulSoup(_SEARCH_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=bicycle")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "Shop on eBay" not in text

    def test_numbered_correctly(self):
        soup = BeautifulSoup(_SEARCH_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=bicycle")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "1. Trek Marlin 5" in text
        assert "2. Specialized Allez" in text

    def test_result_count_in_header(self):
        soup = BeautifulSoup(_SEARCH_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=bicycle")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "1,234 results" in text

    def test_no_items_no_marker(self):
        soup = BeautifulSoup("<html><body><p>empty</p></body></html>", "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=nothing")
        assert soup.find("div", id="ebay-search-marker") is None

    def test_items_without_itm_links_skipped(self):
        html = """
        <html><body>
        <ul class="srp-results">
            <li><span>$10</span></li>
            <li><a href="https://ebay.com/other/123">No itm link</a></li>
        </ul>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html")
        assert soup.find("div", id="ebay-search-marker") is None

    def test_no_srp_results_container(self):
        html = """
        <html><body>
        <ul><li><a href="https://ebay.com/itm/123">Item</a></li></ul>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html")
        assert soup.find("div", id="ebay-search-marker") is None


# ---------------------------------------------------------------------------
# .s-card layout extraction (2025+)
# ---------------------------------------------------------------------------

_SCARD_HTML = """
<html><body>
<ul class="srp-results">
    <li class="s-card s-card--horizontal">
        <div class="su-card-container su-card-container--horizontal">
            <a class="s-card__link" href="https://www.ebay.com/itm/111?_trksid=abc">
                <div class="s-card__title">
                    <span class="su-styled-text primary default">Raspberry Pi 5 8GB</span>
                    <span class="clipped">Opens in a new window or tab</span>
                </div>
            </a>
            <div class="s-card__subtitle"><span class="su-styled-text secondary default">Brand New</span></div>
            <div class="s-card__attribute-row"><span class="s-card__price">$69.99</span></div>
            <div class="s-card__attribute-row"><span>+$5.00 delivery</span></div>
        </div>
    </li>
    <li class="s-card s-card--horizontal">
        <div class="su-card-container su-card-container--horizontal">
            <a class="s-card__link" href="https://www.ebay.com/itm/222?foo=bar">
                <div class="s-card__title">
                    <span class="su-styled-text primary default">Arduino Mega 2560</span>
                    <span class="clipped">Opens in a new window or tab</span>
                </div>
            </a>
            <div class="s-card__subtitle"><span>Pre-Owned</span></div>
            <div class="s-card__attribute-row"><span class="s-card__price">$19.50</span></div>
            <div class="s-card__attribute-row"><span>Free shipping</span></div>
        </div>
    </li>
    <li class="s-card s-card--horizontal">
        <div class="su-card-container su-card-container--horizontal">
            <a class="s-card__link" href="https://ebay.com/itm/333">
                <div class="s-card__title"><span>Shop on eBay</span></div>
            </a>
        </div>
    </li>
</ul>
</body></html>
"""


class TestExtractEbaySCardLayout:
    def test_extracts_scard_items(self):
        soup = BeautifulSoup(_SCARD_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=pi")
        marker = soup.find("div", id="ebay-search-marker")
        assert marker is not None
        text = marker.string
        assert "1. Raspberry Pi 5 8GB" in text
        assert "$69.99" in text
        assert "Brand New" in text
        assert "+$5.00 delivery" in text
        assert "https://www.ebay.com/itm/111" in text
        assert "_trksid" not in text

    def test_scard_accessibility_text_stripped(self):
        soup = BeautifulSoup(_SCARD_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=pi")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "Opens in a new window" not in text

    def test_scard_shop_on_ebay_excluded(self):
        soup = BeautifulSoup(_SCARD_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=pi")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "Shop on eBay" not in text

    def test_scard_numbered_correctly(self):
        soup = BeautifulSoup(_SCARD_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=pi")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "1. Raspberry Pi 5 8GB" in text
        assert "2. Arduino Mega 2560" in text
        # Only 2 real items (Shop on eBay excluded)
        assert "3." not in text

    def test_scard_shipping_extracted(self):
        soup = BeautifulSoup(_SCARD_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=pi")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "Free shipping" in text


# ---------------------------------------------------------------------------
# Class-agnostic heuristic extraction (future-proofing)
# ---------------------------------------------------------------------------

_UNKNOWN_CLASS_HTML = """
<html><body>
<ul class="srp-results">
    <li class="x-result x-result--card">
        <a class="x-result__link" href="https://www.ebay.com/itm/999?hash=abc">
            <h3 class="x-result__heading">Mystery Widget</h3>
        </a>
        <span class="x-result__cost">$42.00</span>
        <span>New</span>
        <span>Free shipping</span>
    </li>
    <li class="x-result x-result--card">
        <a class="x-result__link" href="https://www.ebay.com/itm/888">
            <h3 class="x-result__heading">Gadget Pro</h3>
        </a>
        <span class="x-result__cost">$99.99</span>
        <span>Used</span>
    </li>
</ul>
</body></html>
"""


class TestExtractEbayHeuristicFallback:
    """Items with unknown CSS classes should still extract via /itm/ link + heuristics."""

    def test_extracts_with_unknown_classes(self):
        soup = BeautifulSoup(_UNKNOWN_CLASS_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=widget")
        marker = soup.find("div", id="ebay-search-marker")
        assert marker is not None
        text = marker.string
        assert "1. Mystery Widget" in text
        assert "$42.00" in text
        assert "https://www.ebay.com/itm/999" in text
        assert "hash" not in text  # query params stripped
        assert "2. Gadget Pro" in text
        assert "$99.99" in text

    def test_heuristic_condition_detected(self):
        soup = BeautifulSoup(_UNKNOWN_CLASS_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=widget")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "New" in text
        assert "Used" in text

    def test_heuristic_shipping_detected(self):
        soup = BeautifulSoup(_UNKNOWN_CLASS_HTML, "lxml")
        extract_ebay_search_results(soup, "https://www.ebay.com/sch/i.html?_nkw=widget")
        marker = soup.find("div", id="ebay-search-marker")
        text = marker.string
        assert "Free shipping" in text


# ---------------------------------------------------------------------------
# Postprocessor: search marker → clean output
# ---------------------------------------------------------------------------


class TestPostprocessEbaySearch:
    def test_search_marker_replaces_all_noise(self):
        """When search marker exists, postprocessor returns only the extracted results."""
        markdown = (
            "# eBay search noise\n\n"
            "Some noise here\n\n"
            "__EBAY_SEARCH__"
            "eBay Search Results | 50 results\n\n"
            "1. Item A\n   $100 | New\n\n"
            "2. Item B\n   $200 | Used"
            "__EBAY_SEARCH__"
            "\n\nMore noise\n"
        )
        result = postprocess_ebay(markdown)
        assert result == "eBay Search Results | 50 results\n\n1. Item A\n   $100 | New\n\n2. Item B\n   $200 | Used"

    def test_no_marker_falls_through(self):
        """Without search marker, normal postprocessing applies."""
        markdown = "# Test Product\n\nSome content\n\nSponsored\n\nMore content"
        result = postprocess_ebay(markdown)
        assert "Sponsored" not in result
        assert "Some content" in result
