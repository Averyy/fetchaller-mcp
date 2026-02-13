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

    def test_removes_tracking_images(self):
        md = "Content\n![](https://m.media-amazon.com/images/G/15/gno/sprites/abc.png)\nMore"
        result = postprocess_amazon(md)
        assert "sprites" not in result
        assert "Content" in result

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
