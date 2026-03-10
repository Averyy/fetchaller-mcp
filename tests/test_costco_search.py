"""Tests for Costco search URL detection, param extraction, and result formatting."""

from fetchaller.costco.search import (
    extract_category_params,
    extract_search_params,
    format_search_results,
    is_costco_category_url,
    is_costco_search_url,
    is_costco_url,
)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestIsCostcoUrl:
    def test_www_com(self):
        assert is_costco_url("https://www.costco.com/kirkland-paper-towels.product.100012345.html")

    def test_www_ca(self):
        assert is_costco_url("https://www.costco.ca/s?keyword=chips")

    def test_bare_domain(self):
        assert is_costco_url("https://costco.com/dog-food.html")

    def test_non_costco(self):
        assert not is_costco_url("https://www.walmart.com/search?q=towels")

    def test_subdomain_not_matched(self):
        assert not is_costco_url("https://search.costco.com/api/apps")


class TestIsCostcoSearchUrl:
    def test_keyword_search(self):
        assert is_costco_search_url("https://www.costco.com/s?keyword=paper+towels")

    def test_search_with_pagination(self):
        assert is_costco_search_url("https://www.costco.ca/s?keyword=chips&currentPage=2")

    def test_product_page_not_search(self):
        assert not is_costco_search_url("https://www.costco.com/kirkland-paper-towels.product.100012345.html")

    def test_category_page_not_search(self):
        assert not is_costco_search_url("https://www.costco.com/dog-food.html")

    def test_non_costco(self):
        assert not is_costco_search_url("https://example.com/s?keyword=test")


class TestIsCostcoCategoryUrl:
    def test_category_url(self):
        assert is_costco_category_url("https://www.costco.com/dog-food.html")

    def test_category_ca(self):
        assert is_costco_category_url("https://www.costco.ca/snacks.html")

    def test_product_url_not_category(self):
        assert not is_costco_category_url(
            "https://www.costco.com/kirkland-paper-towels.product.100012345.html"
        )

    def test_product_with_p_path_not_category(self):
        assert not is_costco_category_url("https://www.costco.com/p/kirkland-towels")

    def test_nested_path_not_category(self):
        # Category URLs should be single-segment paths (no slashes)
        assert not is_costco_category_url("https://www.costco.com/grocery/snacks.html")

    def test_search_url_not_category(self):
        assert not is_costco_category_url("https://www.costco.com/s?keyword=food")

    def test_non_costco(self):
        assert not is_costco_category_url("https://example.com/dog-food.html")


# ---------------------------------------------------------------------------
# Search param extraction
# ---------------------------------------------------------------------------


class TestExtractSearchParams:
    def test_basic_search(self):
        params = extract_search_params("https://www.costco.com/s?keyword=paper+towels")
        assert params["query"] == "paper towels"
        assert params["domain"] == "com"
        assert params["start"] == 0

    def test_ca_domain(self):
        params = extract_search_params("https://www.costco.ca/s?keyword=chips")
        assert params["domain"] == "ca"

    def test_offset_pagination(self):
        params = extract_search_params("https://www.costco.com/s?keyword=towels&offset=48")
        assert params["start"] == 48

    def test_current_page_pagination(self):
        params = extract_search_params("https://www.costco.com/s?keyword=towels&currentPage=3")
        # Page 3 → start = (3-1)*24 = 48
        assert params["start"] == 48

    def test_offset_takes_precedence_over_page(self):
        params = extract_search_params(
            "https://www.costco.com/s?keyword=towels&offset=24&currentPage=5"
        )
        assert params["start"] == 24

    def test_no_keyword_returns_none(self):
        assert extract_search_params("https://www.costco.com/s") is None

    def test_empty_keyword_returns_none(self):
        assert extract_search_params("https://www.costco.com/s?keyword=") is None

    def test_non_costco_returns_none(self):
        assert extract_search_params("https://example.com/s?keyword=test") is None

    def test_invalid_offset_ignored(self):
        params = extract_search_params("https://www.costco.com/s?keyword=test&offset=abc")
        assert params["start"] == 0


class TestExtractCategoryParams:
    def test_basic_category(self):
        params = extract_category_params("https://www.costco.com/dog-food.html")
        assert params["query"] == "dog food"
        assert params["domain"] == "com"
        assert params["start"] == 0

    def test_ca_category(self):
        params = extract_category_params("https://www.costco.ca/snacks.html")
        assert params["query"] == "snacks"
        assert params["domain"] == "ca"

    def test_product_url_returns_none(self):
        assert extract_category_params(
            "https://www.costco.com/kirkland-towels.product.100012345.html"
        ) is None

    def test_nested_path_returns_none(self):
        assert extract_category_params("https://www.costco.com/grocery/snacks.html") is None

    def test_non_costco_returns_none(self):
        assert extract_category_params("https://example.com/dog-food.html") is None


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    def test_basic_formatting(self):
        items = [
            {
                "title": "Kirkland Paper Towels",
                "item_number": "1234567",
                "price": 19.99,
                "sale_price": None,
                "rating": 4.5,
                "reviews": 128,
                "brand": "Kirkland Signature",
                "stock": "In Stock",
                "url": "https://www.costco.com/kirkland-paper-towels.html",
                "image": "",
                "description": "Premium paper towels",
                "features": ["12 rolls", "2-ply"],
            },
        ]
        result = format_search_results(items, "paper towels", 1, "com")
        assert "Costco.com" in result
        assert '"paper towels"' in result
        assert "1 results" in result
        assert "1. **Kirkland Paper Towels**" in result
        assert "Item# 1234567" in result
        assert "Kirkland Signature" in result
        assert "$19.99" in result
        assert "Rating: 4.5 (128 reviews)" in result
        assert "In Stock" in result
        assert "- 12 rolls" in result
        assert "- 2-ply" in result
        assert "https://www.costco.com/kirkland-paper-towels.html" in result

    def test_sale_price_with_strikethrough(self):
        items = [
            {
                "title": "Sale Item",
                "item_number": "",
                "price": 29.99,
                "sale_price": 19.99,
                "rating": None,
                "reviews": None,
                "brand": "",
                "stock": "",
                "url": "",
                "image": "",
                "description": "",
                "features": [],
            },
        ]
        result = format_search_results(items, "sale", 1, "com")
        assert "~~$29.99~~ **$19.99**" in result

    def test_showing_x_of_y(self):
        items = [
            {
                "title": f"Item {i}",
                "item_number": "",
                "price": 10.0,
                "sale_price": None,
                "rating": None,
                "reviews": None,
                "brand": "",
                "stock": "",
                "url": "",
                "image": "",
                "description": "",
                "features": [],
            }
            for i in range(24)
        ]
        result = format_search_results(items, "test", 500, "com")
        assert "showing 24 of 500" in result

    def test_empty_results(self):
        result = format_search_results([], "towels", 0, "com")
        assert "No products found" in result
        assert "Costco.com" in result

    def test_ca_domain(self):
        result = format_search_results([], "chips", 0, "ca")
        assert "Costco.ca" in result

    def test_no_query(self):
        result = format_search_results([], "", 0, "com")
        assert "Costco.com" in result
        # Should not have empty quotes
        assert '""' not in result

    def test_multiple_items_numbered(self):
        items = [
            {
                "title": f"Product {c}",
                "item_number": "",
                "price": None,
                "sale_price": None,
                "rating": None,
                "reviews": None,
                "brand": "",
                "stock": "",
                "url": "",
                "image": "",
                "description": "",
                "features": [],
            }
            for c in ("A", "B", "C")
        ]
        result = format_search_results(items, "test", 3, "com")
        assert "1. **Product A**" in result
        assert "2. **Product B**" in result
        assert "3. **Product C**" in result
