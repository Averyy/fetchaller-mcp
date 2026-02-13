"""Unit tests for Amazon regex postprocessor.

Tests postprocess_amazon() in isolation to verify each pattern works correctly.
"""

from fetchaller.content.amazon import is_amazon, postprocess_amazon


class TestIsAmazon:
    """URL detection for all Amazon TLDs."""

    def test_amazon_ca(self):
        assert is_amazon("https://www.amazon.ca/dp/B0D1XD1ZV3")

    def test_amazon_com(self):
        assert is_amazon("https://www.amazon.com/dp/B123")

    def test_amazon_co_uk(self):
        assert is_amazon("https://www.amazon.co.uk/dp/B123")

    def test_amazon_de(self):
        assert is_amazon("https://www.amazon.de/dp/B123")

    def test_amazon_co_jp(self):
        assert is_amazon("https://www.amazon.co.jp/dp/B123")

    def test_amazon_com_au(self):
        assert is_amazon("https://www.amazon.com.au/dp/B123")

    def test_amazon_no_www(self):
        assert is_amazon("https://amazon.ca/dp/B123")

    def test_not_amazon(self):
        assert not is_amazon("https://example.com/amazon")

    def test_not_famazon(self):
        assert not is_amazon("https://famazon.com/dp/B123")


class TestPostprocessAmazon:
    """Regex postprocessor patterns."""

    def test_removes_sponsored_label(self):
        md = "Product info\n[Sponsored](#sp_detail_feedbackForm)\nMore content"
        result = postprocess_amazon(md)
        assert "Sponsored" not in result
        assert "Product info" in result
        assert "More content" in result

    def test_removes_page_navigation(self):
        md = "Content before\nPage 1 of 1Start over\nContent after"
        result = postprocess_amazon(md)
        assert "Page 1 of" not in result
        assert "Start over" not in result

    def test_removes_previous_next_page(self):
        md = "*Previous page of related Sponsored Products*\nContent\n*Next page of related Sponsored Products*"
        result = postprocess_amazon(md)
        assert "Previous page" not in result
        assert "Next page" not in result

    def test_removes_feedback_labels(self):
        md = "Product A\n   Feedback\nProduct B"
        result = postprocess_amazon(md)
        assert "Feedback" not in result
        assert "Product A" in result

    def test_removes_show_more(self):
        md = "Feature list\nShow More\nSee more details"
        result = postprocess_amazon(md)
        assert "Show More" not in result

    def test_removes_report_issue(self):
        md = 'Content\n[Report an issue with this product or seller](?ref=dp#tellAmazon)\nMore'
        result = postprocess_amazon(md)
        assert "Report an issue" not in result

    def test_removes_product_summary_header(self):
        md = "Reviews\n# Product summary presents key product information --- Keyboard shortcut\nDuplicate content"
        result = postprocess_amazon(md)
        assert "Product summary presents" not in result

    def test_removes_feedback_useful_block(self):
        md = (
            "Description\n\n## Feedback\n\n"
            "Did you find this product summary feature useful?\n"
            "Yes, it is useful\n"
            "No, it is not useful\n"
            "Thank you for your feedback\n"
            "Thank you for your feedback. You selected \"Yes, it is useful\"\n"
            "Thank you for your feedback. You selected \"No, it is not useful\"\n"
            "Change your feedback\n"
            "Next section"
        )
        result = postprocess_amazon(md)
        assert "Did you find" not in result
        assert "Change your feedback" not in result
        assert "Description" in result
        assert "Next section" in result

    def test_removes_customer_mention_counts(self):
        md = "21 customers mention quality, 17 positive, 4 negative\nActual review content"
        result = postprocess_amazon(md)
        assert "21 customers mention" not in result
        assert "Actual review content" in result

    def test_removes_back_to_top(self):
        md = "Last review\nBack to top\nFooter stuff"
        result = postprocess_amazon(md)
        assert "Back to top" not in result

    def test_removes_footer_section_headers(self):
        md = "Content\nGet to Know Us\nMake Money with Us\nAmazon Payment Products\nLet Us Help You"
        result = postprocess_amazon(md)
        assert "Get to Know Us" not in result
        assert "Make Money with Us" not in result

    def test_removes_copyright(self):
        md = "Content\n\n© 1996-2026, Amazon.com, Inc. or its affiliates"
        result = postprocess_amazon(md)
        assert "1996-2026" not in result

    def test_removes_sspa_click_links(self):
        md = "[$109.99$109.99](/sspa/click?ie=UTF8&spc=abcdef)"
        result = postprocess_amazon(md)
        assert "/sspa/click" not in result

    def test_removes_sign_in_prompt(self):
        md = "Product info\nSign in to continue\nMore info"
        result = postprocess_amazon(md)
        assert "Sign in to continue" not in result

    def test_removes_delivery_location(self):
        md = "Product\nDelivering to Balzac T4B 2T – Update location\nPrice"
        result = postprocess_amazon(md)
        assert "Delivering to" not in result
        assert "Update location" not in result

    def test_removes_verified_purchase(self):
        md = "Review title\n  Verified Purchase\nReview content"
        result = postprocess_amazon(md)
        assert "Verified Purchase" not in result
        assert "Review content" in result

    def test_removes_see_more_reviews(self):
        md = "Last review\n[See more reviews](/product-reviews/B123/ref=foo)\nFooter"
        result = postprocess_amazon(md)
        assert "See more reviews" not in result

    def test_removes_video_360_image_tabs(self):
        md = "Images\n- VIDEOS\n- 360° VIEW\n- IMAGES\nProduct title"
        result = postprocess_amazon(md)
        assert "VIDEOS" not in result
        assert "360° VIEW" not in result

    def test_removes_sponsored_products_section(self):
        """Entire 'Products related to this item' sponsored section is removed."""
        md = (
            "## Product Description\n\nGreat product\n\n---\n\n"
            "## Products related to this item\n\n"
            "[Sponsored](#sp_detail_feedbackForm)\n"
            "1. [![Other Product](img.jpg) Other Product](/dp/B123)\n"
            "   $24.99\n"
            "2. [![Another](img.jpg) Another Product](/dp/B456)\n"
            "   $19.99\n\n"
            "---\n\n## Customer reviews\n\nGreat stuff"
        )
        result = postprocess_amazon(md)
        assert "Products related to this item" not in result
        assert "Other Product" not in result
        assert "Product Description" in result
        assert "Customer reviews" in result

    def test_removes_bought_together_section(self):
        """'BRAND products customers bought together' section is removed."""
        md = (
            "Product info\n\n"
            "## BSEEN products customers bought together\n\n"
            "This item: Some collar $14.99\n"
            "+\n"
            "Another collar $16.99\n\n"
            "---\n\n## Product Description"
        )
        result = postprocess_amazon(md)
        assert "products customers bought together" not in result
        assert "Product info" in result
        assert "Product Description" in result

    def test_removes_lower_price_form(self):
        """'Found a lower price?' form section is removed."""
        md = (
            "Product details\n\n"
            "Found a lower price? Let us know.\n\n"
            "## Where did you see a lower price?\n\n"
            "Price Availability\n"
            "Website (Online)\n"
            "URL *:\n"
            "Store (Offline)\n"
            "Submit Feedback\n"
            "More content"
        )
        result = postprocess_amazon(md)
        assert "Found a lower price" not in result
        assert "Submit Feedback" not in result
        assert "Product details" in result
        assert "More content" in result

    def test_deduplicates_price(self):
        """Doubled prices like '$14.99$14.99' become '$14.99'."""
        md = "$14.99$14.99\n\nWas: $15.99$15.99"
        result = postprocess_amazon(md)
        assert result.count("$14.99") == 1
        assert result.count("$15.99") == 1

    def test_deduplicates_ships_from(self):
        """Duplicated 'Ships from' block is collapsed to one."""
        md = (
            "Ships from\n\n"
            "[Amazon](/help)\n\n"
            " Amazon \n\n"
            "Ships from\n\n"
            "[Amazon](/help)\n\n"
            "More info"
        )
        result = postprocess_amazon(md)
        # Should have exactly one "Ships from"
        assert result.count("Ships from") == 1
        assert "More info" in result

    def test_keeps_brief_returns_removes_expanded(self):
        """Keeps brief 'Eligible for Return...' line, removes expanded paragraph."""
        md = (
            "Returns\n\n"
            "Eligible for Return, Refund or Replacement within 30 days of receipt \n\n"
            "Eligible for Return, Refund or Replacement within 30 days of receipt\n\n"
            "This item can be returned in its original condition for a full refund.\n\n"
            "[Read full return policy](/gp/help/customer/display.html)\n"
            "Next section"
        )
        result = postprocess_amazon(md)
        assert "Eligible for Return" in result
        assert "Read full return policy" not in result
        assert "This item can be returned" not in result
        assert "Next section" in result

    def test_removes_payment_security_block(self):
        """Entire Payment/Secure transaction block is removed (just boilerplate)."""
        md = (
            "Payment\n\n"
            "Secure transaction \n\n"
            "Your transaction is secure\n\n"
            "We work hard to protect your security and privacy.\n\n"
            "[Learn more](/gp/help/customer/display.html)\n"
            "Next section"
        )
        result = postprocess_amazon(md)
        assert "Payment" not in result
        assert "Secure transaction" not in result
        assert "We work hard" not in result
        assert "Next section" in result

    def test_removes_review_quote_blocks(self):
        """Review aspect expansion quote blocks with [Read more] links are removed."""
        md = (
            "### Customers say\n\n"
            "Great brightness overall.\n\n"
            '"...so bright and the charge is lasting a long time..." [Read more](/gp/customer-reviews/R1UTM5NUI7BSPC)\n'
            '"Easy to use. Love that it\'s rechargeable." [Read more](/gp/customer-reviews/R1NXVY32IQBCOC)\n'
            "Next section"
        )
        result = postprocess_amazon(md)
        assert "Read more" not in result
        assert "so bright and the charge" not in result
        assert "Great brightness overall" in result
        assert "Next section" in result

    def test_removes_helpful_and_report_links(self):
        md = "Good review content\n[Helpful](/vote)\n[Report](/report)\nAnother review"
        result = postprocess_amazon(md)
        assert "[Helpful]" not in result
        assert "[Report]" not in result
        assert "Good review content" in result

    def test_removes_rating_histogram(self):
        md = "4.6 out of 5\n\n- [5 star4 star3 star2 star1 star5 star\n\n  75%14%8%1%2%75%](/product-reviews/B123)\n\nNext"
        result = postprocess_amazon(md)
        assert "5 star4 star" not in result
        assert "4.6 out of 5" in result

    def test_removes_video_descriptions(self):
        md = "Product description\nThe video showcases the product in use.\nThe video guides you through setup.\nMerchant video\nMore content"
        result = postprocess_amazon(md)
        assert "The video showcases" not in result
        assert "The video guides" not in result
        assert "Merchant video" not in result
        assert "Product description" in result

    def test_removes_cardname_templates(self):
        md = "Price info\n%cardName%\n${cardName} not available for the seller\nMore"
        result = postprocess_amazon(md)
        assert "cardName" not in result
        assert "Price info" in result

    def test_preserves_product_content(self):
        """Core product content must survive all cleanup."""
        md = (
            "# Apple AirPods Pro\n\n"
            "4.6 out of 5 stars\n\n"
            "| Brand | Apple |\n| --- | --- |\n\n"
            "# About this item\n\n"
            "- Great sound quality\n"
            "- Active noise cancellation\n"
        )
        result = postprocess_amazon(md)
        assert "Apple AirPods Pro" in result
        assert "4.6 out of 5 stars" in result
        assert "Brand | Apple" in result
        assert "Great sound quality" in result
        assert "Active noise cancellation" in result

    def test_preserves_delivery_info(self):
        """Delivery dates and stock status must survive."""
        md = (
            "FREE delivery Tuesday, February 17\n\n"
            "Or fastest delivery Tomorrow, February 14\n\n"
            "In Stock\n"
        )
        result = postprocess_amazon(md)
        assert "FREE delivery" in result
        assert "In Stock" in result

    def test_preserves_ships_from_sold_by(self):
        """Single Ships from / Sold by should survive (only duplicates removed)."""
        md = (
            "Ships from\n\n"
            "[Amazon](/help)\n\n"
            " Amazon \n\n"
            "Sold by\n\n"
            "[bseenled](/seller)\n\n"
            " bseenled \n\n"
            "More info"
        )
        result = postprocess_amazon(md)
        assert "Ships from" in result
        assert "Sold by" in result
        assert "Amazon" in result
        assert "bseenled" in result
