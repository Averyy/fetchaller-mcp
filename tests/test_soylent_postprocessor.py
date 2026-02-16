"""Unit tests for Soylent regex postprocessor.

Tests postprocess_soylent() in isolation to verify each pattern works correctly,
and is_soylent() for URL detection.
"""

from fetchaller.content.soylent import is_soylent, postprocess_soylent


class TestIsSoylent:
    """URL detection for Soylent domains."""

    def test_soylent_ca_www(self):
        assert is_soylent("https://www.soylent.ca/products/soylent-drink-creamy-chocolate")

    def test_soylent_com_www(self):
        assert is_soylent("https://www.soylent.com/products/soylent-drink")

    def test_soylent_ca_bare(self):
        assert is_soylent("https://soylent.ca/collections/all")

    def test_soylent_com_bare(self):
        assert is_soylent("https://soylent.com/collections/all")

    def test_not_soylent(self):
        assert not is_soylent("https://example.com/soylent")

    def test_not_fake_soylent(self):
        assert not is_soylent("https://notsoylent.com/products")


class TestPostprocessSoylent:
    """Regex postprocessor patterns."""

    def test_removes_subscribe_save_noise(self):
        md = "Product info\nSubscribe & Save 25%\nOne-Time Purchase\nPrepaid Subscription\nMore content"
        result = postprocess_soylent(md)
        assert "Subscribe & Save" not in result
        assert "One-Time Purchase" not in result
        assert "Prepaid Subscription" not in result
        assert "Product info" in result
        assert "More content" in result

    def test_removes_duplicate_benefits_sections(self):
        md = (
            "# Product\n\n"
            "## The Benefits of Drinking Soylent\n\n"
            "- Complete nutrition\n- 400 calories\n\n"
            "---\n\n"
            "## The Benefits of Drinking Soylent\n\n"
            "- Complete nutrition\n- 400 calories\n\n"
            "---\n\n"
            "## Reviews"
        )
        result = postprocess_soylent(md)
        assert result.count("Benefits of Drinking Soylent") == 1
        assert "Complete nutrition" in result
        assert "Reviews" in result

    def test_removes_review_voting_ui(self):
        md = "Great taste!\nWas this helpful?\nYes (5)\nNo (0)\nNext review"
        result = postprocess_soylent(md)
        assert "Was this helpful" not in result
        assert "Great taste" in result
        assert "Next review" in result

    def test_removes_radio_button_artifacts(self):
        md = "(/) Subscribe & Save\n( ) One-Time Purchase\nProduct details"
        result = postprocess_soylent(md)
        assert "(/) " not in result
        assert "( ) " not in result
        assert "Product details" in result

    def test_preserves_product_info(self):
        md = (
            "# Soylent Drink Creamy Chocolate\n\n"
            "$63.00 CAD\n\n"
            "Each bottle of Soylent contains complete nutrition.\n\n"
            "## FAQ\n\nWhat does it taste like?\n\nSmooth chocolate."
        )
        result = postprocess_soylent(md)
        assert "Soylent Drink Creamy Chocolate" in result
        assert "$63.00 CAD" in result
        assert "complete nutrition" in result
        assert "FAQ" in result
        assert "Smooth chocolate" in result

    def test_preserves_reviews_content(self):
        md = (
            "## Customer Reviews\n\n"
            "4.6 out of 5 stars\n\n"
            "Great meal replacement, tastes good!\n\n"
            "Easy to drink on the go."
        )
        result = postprocess_soylent(md)
        assert "4.6 out of 5 stars" in result
        assert "Great meal replacement" in result
        assert "Easy to drink on the go" in result

    def test_inventory_extraction(self):
        md = "# Soylent Drink\n\nProduct description\n\n__SOYLENT_INVENTORY_42__"
        result = postprocess_soylent(md)
        assert "**Stock: 42 units**" in result
        assert "__SOYLENT_INVENTORY_" not in result
        assert "Product description" in result

    def test_inventory_out_of_stock(self):
        md = "# Soylent Drink\n\nProduct description\n\n__SOYLENT_INVENTORY_0__"
        result = postprocess_soylent(md)
        assert "**Out of stock**" in result
        assert "__SOYLENT_INVENTORY_" not in result

    def test_available_in_stock(self):
        md = "# Soylent Drink\n\nProduct description\n\n__SOYLENT_AVAILABLE_yes__"
        result = postprocess_soylent(md)
        assert "**In stock**" in result
        assert "__SOYLENT_AVAILABLE_" not in result
        assert "Product description" in result

    def test_available_out_of_stock(self):
        md = "# Soylent Drink\n\nProduct description\n\n__SOYLENT_AVAILABLE_no__"
        result = postprocess_soylent(md)
        assert "**Out of stock**" in result
        assert "__SOYLENT_AVAILABLE_" not in result

    def test_inventory_takes_priority_over_available(self):
        """When both gsf_conversion_data quantity and Shopify available exist, quantity wins."""
        md = "# Soylent Drink\n\nProduct description\n\n__SOYLENT_INVENTORY_42__\n__SOYLENT_AVAILABLE_no__"
        result = postprocess_soylent(md)
        assert "**Stock: 42 units**" in result
        assert "**Out of stock**" not in result
        assert "__SOYLENT_" not in result

    def test_removes_button_text(self):
        md = "Product info\nSold Out\nAdd to Cart\nShop Now\nMore content"
        result = postprocess_soylent(md)
        assert "Sold Out" not in result
        assert "Add to Cart" not in result
        assert "Shop Now" not in result
        assert "Product info" in result
        assert "More content" in result

    def test_removes_us_vs_them(self):
        md = (
            "# Product\n\n"
            "## Us vs. Them\n\n"
            "| Feature | Soylent | Others |\n"
            "| --- | --- | --- |\n"
            "| Protein | 20g | 10g |\n\n"
            "---\n\n## Reviews"
        )
        result = postprocess_soylent(md)
        assert "Us vs. Them" not in result
        assert "Reviews" in result

    def test_removes_write_a_review_and_sort(self):
        md = "Reviews section\nWrite a Review\nSort Most Recent\nActual review"
        result = postprocess_soylent(md)
        assert "Write a Review" not in result
        assert "Sort Most Recent" not in result
        assert "Actual review" in result

    def test_removes_why_subscribe_heading(self):
        md = (
            "# Product\n\n"
            "## Why Subscribe?\n\n"
            "## Product Details\n\nGreat product."
        )
        result = postprocess_soylent(md)
        assert "Why Subscribe" not in result
        assert "Product Details" in result
        assert "Great product" in result

    def test_removes_5_things_to_consider(self):
        md = (
            "# Product\n\n"
            "## 5 Things to Consider Before Buying Soylent\n\n"
            "1. Nutrition\n2. Taste\n3. Convenience\n\n"
            "---\n\n## FAQ"
        )
        result = postprocess_soylent(md)
        assert "5 Things to Consider" not in result
        assert "FAQ" in result

    def test_removes_loading_text(self):
        md = "Reviews\nLoading...\nMore content"
        result = postprocess_soylent(md)
        assert "Loading..." not in result
        assert "More content" in result

    def test_removes_read_more_links(self):
        md = "Review text here.\nRead More\nNext review"
        result = postprocess_soylent(md)
        assert "Read More" not in result
        assert "Review text here" in result

    def test_removes_view_details(self):
        md = "Product card\n[View Details](/products/soylent-drink)\nNext card"
        result = postprocess_soylent(md)
        assert "View Details" not in result
        assert "Product card" in result

    def test_removes_badge_text(self):
        md = "Best Seller\n# Soylent Drink\nBundle & Save\nProduct info"
        result = postprocess_soylent(md)
        assert "Best Seller" not in result
        assert "Bundle & Save" not in result
        assert "Soylent Drink" in result

    def test_removes_form_labels(self):
        md = "Duration:\nTitle\nProduct content"
        result = postprocess_soylent(md)
        assert "Duration:" not in result
        # "Title" on its own line is removed
        assert "\nTitle\n" not in result
        assert "Product content" in result
