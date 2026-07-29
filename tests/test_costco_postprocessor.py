"""Tests for Costco content module: URL detection and postprocessor."""

import pytest

from fetchaller.content.costco import (
    is_costco,
    is_costco_category_url,
    is_costco_product_url,
    is_costco_search_url,
    postprocess_costco,
)

# ---------------------------------------------------------------------------
# URL detection (content module)
# ---------------------------------------------------------------------------


class TestIsCostco:
    def test_www_com(self):
        assert is_costco("https://www.costco.com/anything")

    def test_www_ca(self):
        assert is_costco("https://www.costco.ca/anything")

    def test_bare_com(self):
        assert is_costco("https://costco.com/anything")

    def test_bare_ca(self):
        assert is_costco("https://costco.ca/anything")

    def test_non_costco(self):
        assert not is_costco("https://www.walmart.com/")

    def test_search_subdomain(self):
        assert not is_costco("https://search.costco.com/api/apps")


class TestIsCostcoProductUrl:
    def test_product_dot_pattern(self):
        assert is_costco_product_url(
            "https://www.costco.com/kirkland-paper-towels.product.100012345.html"
        )

    def test_p_path_pattern(self):
        assert is_costco_product_url("https://www.costco.com/p/kirkland-paper-towels")

    def test_category_not_product(self):
        assert not is_costco_product_url("https://www.costco.com/dog-food.html")

    def test_search_not_product(self):
        assert not is_costco_product_url("https://www.costco.com/s?keyword=towels")

    def test_non_costco(self):
        assert not is_costco_product_url("https://example.com/thing.product.123.html")


class TestIsCostcoSearchUrlContent:
    """Tests for the content module's is_costco_search_url (distinct from costco.search)."""

    def test_s_path(self):
        assert is_costco_search_url("https://www.costco.com/s?keyword=towels")

    def test_keyword_in_query(self):
        assert is_costco_search_url("https://www.costco.com/something?keyword=test")

    def test_product_not_search(self):
        assert not is_costco_search_url(
            "https://www.costco.com/kirkland.product.123.html"
        )

    def test_non_costco(self):
        assert not is_costco_search_url("https://example.com/s?keyword=test")


class TestIsCostcoCategoryUrlContent:
    def test_c_path(self):
        assert is_costco_category_url("https://www.costco.com/c/grocery")

    def test_non_category(self):
        assert not is_costco_category_url("https://www.costco.com/s?keyword=food")

    def test_non_costco(self):
        assert not is_costco_category_url("https://example.com/c/grocery")

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.costco.com/laptops.html",
            "https://www.costco.com/dog-food.html",
            "https://www.costco.ca/computers-tablets.html",
        ],
    )
    def test_legacy_html_category_paths(self, url):
        """``/<slug>.html`` is Costco's long-standing category shape.

        Only ``/c/<slug>`` was recognized, so these skipped the working search
        API and fell through to HTML that Akamai challenges.
        """

        assert is_costco_category_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            # Product pages also end in .html -- they must not be treated as
            # categories just because of the suffix.
            "https://www.costco.com/kirkland-signature-olive-oil.product.100334841.html",
            "https://www.costco.com/p/12345",
            # Nested paths are not category slugs.
            "https://www.costco.com/warehouse-locations/foo.html",
            "https://www.costco.com/",
        ],
    )
    def test_html_suffix_alone_is_not_a_category(self, url):
        assert not is_costco_category_url(url)


# ---------------------------------------------------------------------------
# Postprocessor
# ---------------------------------------------------------------------------


class TestPostprocessCostco:
    def test_removes_sign_in_prompt(self):
        md = "# Product\n\nSign In / Register\n\nGood stuff here"
        result = postprocess_costco(md)
        assert "Sign In" not in result
        assert "Good stuff here" in result

    def test_removes_warehouse_text(self):
        md = "# Product\n\nMy Warehouse\n\nSet My Warehouse\n\nOpen until 8:30pm\n\nContent"
        result = postprocess_costco(md)
        assert "My Warehouse" not in result
        assert "Set My Warehouse" not in result
        assert "Open until" not in result
        assert "Content" in result

    def test_removes_delivery_location(self):
        md = "# Product\n\nDelivery Location: 98101\n\nEnter your ZIP code\n\n98101\n\nContent"
        result = postprocess_costco(md)
        assert "Delivery Location" not in result
        assert "Enter your ZIP" not in result
        assert "Content" in result

    def test_removes_add_to_cart(self):
        md = "# Product\n\nAdd to Cart\n\nAdd to List\n\nContent"
        result = postprocess_costco(md)
        assert "Add to Cart" not in result
        assert "Add to List" not in result
        assert "Content" in result

    def test_removes_membership_prompts(self):
        md = "# Product\n\nJoin Now\n\nRenew Membership\n\nMembers also bought\n\nContent"
        result = postprocess_costco(md)
        assert "Join Now" not in result
        assert "Renew Membership" not in result
        assert "Members also bought" not in result
        assert "Content" in result

    def test_removes_footer_headings(self):
        md = "# Product\n\nContent\n\n## About Us\n\n## Customer Service"
        result = postprocess_costco(md)
        assert "About Us" not in result
        assert "Customer Service" not in result
        assert "Content" in result

    def test_removes_copyright(self):
        md = "# Product\n\nContent\n\n\u00a9 2025 Costco Wholesale Corporation. All rights reserved."
        result = postprocess_costco(md)
        assert "Costco Wholesale" not in result
        assert "Content" in result

    def test_strips_title_suffix(self):
        md = "# Kirkland Paper Towels | Costco\n\nContent"
        result = postprocess_costco(md)
        assert "# Kirkland Paper Towels" in result
        assert "| Costco" not in result

    def test_removes_cookie_consent(self):
        md = "# Product\n\nWe use cookies to improve your experience.\n\nAccept All Cookies\n\nContent"
        result = postprocess_costco(md)
        assert "cookies" not in result.lower()
        assert "Content" in result

    def test_removes_write_a_review(self):
        md = "# Product\n\nWrite a Review\n\nContent"
        result = postprocess_costco(md)
        assert "Write a Review" not in result
        assert "Content" in result

    def test_removes_empty_headings(self):
        md = "# Product\n\n##\n\n###\n\nContent"
        result = postprocess_costco(md)
        assert "##" not in result
        assert "Content" in result

    def test_collapses_excessive_newlines(self):
        md = "# Product\n\n\n\n\n\nContent"
        result = postprocess_costco(md)
        assert "\n\n\n" not in result
        assert "Content" in result

    def test_deduplicates_costco_images(self):
        md = (
            "# Product\n\n"
            "![img](https://richmedia.ca-richcontent.com/costco-static.com/4000399008-847__1?w=800)\n"
            "![img](https://richmedia.ca-richcontent.com/costco-static.com/4000399008-847__1?w=200)\n"
            "Content"
        )
        result = postprocess_costco(md)
        # First image kept, duplicate removed
        assert result.count("4000399008-847__1") == 1
        assert "Content" in result

    def test_removes_shop_link(self):
        # [Shop Grocery](/s?...) links from mega-menu are removed
        md = "# Product\n\n[Shop Grocery](/s?keyword=grocery)\n\nKirkland bath tissue"
        result = postprocess_costco(md)
        assert "Shop Grocery" not in result
        assert "Kirkland bath tissue" in result

    def test_removes_empty_member_reviews_section(self):
        md = "# Product\n\nContent\n\n## Member Reviews\n\n0"
        result = postprocess_costco(md)
        assert "Member Reviews" not in result
        assert "Content" in result

    def test_removes_json_ld_placeholder_price(self):
        md = "# Product\n\n**Price:** USD 1\n\n**Availability:** OutOfStock\n\nContent"
        result = postprocess_costco(md)
        assert "USD 1" not in result
        assert "OutOfStock" not in result
        assert "Content" in result

    def test_preserves_real_content(self):
        """Postprocessor should not strip legitimate product information."""
        md = (
            "# Kirkland Signature Bath Tissue\n\n"
            "**Price:** $24.99\n\n"
            "30 rolls, 2-ply, 380 sheets per roll\n\n"
            "**Rating:** 4.7 out of 5 stars (2,345 reviews)\n\n"
            "- Ultra soft and strong\n"
            "- Septic-safe\n"
        )
        result = postprocess_costco(md)
        assert "Kirkland Signature Bath Tissue" in result
        assert "$24.99" in result
        assert "380 sheets per roll" in result
        assert "Ultra soft and strong" in result

    def test_removes_skip_to_content(self):
        md = "Skip to Main Content\n\n# Product\n\nContent"
        result = postprocess_costco(md)
        assert "Skip to Main Content" not in result
        assert "Content" in result

    def test_removes_social_media_links(self):
        md = "# Product\n\nContent\n\nCostco Facebook\n\nCostco Instagram"
        result = postprocess_costco(md)
        assert "Costco Facebook" not in result
        assert "Costco Instagram" not in result
        assert "Content" in result

    def test_removes_view_cart(self):
        md = "# Product\n\nView Cart\n\nGo to Cart\n\nShopping Cart\n\nContent"
        result = postprocess_costco(md)
        assert "Cart" not in result
        assert "Content" in result

    def test_non_breaking_spaces_handled(self):
        """Patterns should match \xa0 non-breaking spaces."""
        md = "# Product\n\nSign\xa0In / Register\n\nContent"
        result = postprocess_costco(md)
        assert "Sign" not in result or "Register" not in result
        assert "Content" in result
