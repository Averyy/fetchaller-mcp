"""Unit tests for Kijiji regex postprocessor.

Tests postprocess_kijiji() in isolation to verify each pattern works correctly,
and is_kijiji() for URL detection.
"""

from fetchaller.content.kijiji import is_kijiji, postprocess_kijiji


class TestIsKijiji:
    """URL detection for Kijiji domains."""

    def test_kijiji_ca_www(self):
        assert is_kijiji("https://www.kijiji.ca/b-buy-sell/ottawa/c10l1700185")

    def test_kijiji_ca_bare(self):
        assert is_kijiji("https://kijiji.ca/v-cell-phone/ottawa/iphone/1701831111")

    def test_not_kijiji(self):
        assert not is_kijiji("https://example.com/kijiji")

    def test_not_fake_kijiji(self):
        assert not is_kijiji("https://notkijiji.ca/page")


class TestPostprocessKijiji:
    """Regex postprocessor patterns."""

    def test_removes_register_sign_in(self):
        md = "Register\nSign In\nPost\nActual listing content"
        result = postprocess_kijiji(md)
        assert "\nRegister\n" not in result
        assert "\nSign In\n" not in result
        assert "\nPost\n" not in result
        assert "Actual listing content" in result

    def test_removes_joined_register_sign_in(self):
        md = "RegisterorSign In\nContent"
        result = postprocess_kijiji(md)
        assert "RegisterorSign In" not in result
        assert "Content" in result

    def test_removes_language_toggle(self):
        md = "FR\nContent"
        result = postprocess_kijiji(md)
        assert "\nFR\n" not in result
        assert "Content" in result

    def test_removes_search_standalone(self):
        md = "Search\nOttawa\nSearch\nContent"
        result = postprocess_kijiji(md)
        assert "\nSearch\n" not in result
        assert "Content" in result

    def test_removes_notify_me(self):
        md = "Content\nNotify me when new ads are posted\nListings"
        result = postprocess_kijiji(md)
        assert "Notify me" not in result
        assert "Listings" in result

    def test_removes_filter_labels(self):
        md = "Price\nFor Sale By\nPrice type\nAll Filters\nContent"
        result = postprocess_kijiji(md)
        assert "\nPrice\n" not in result
        assert "\nAll Filters\n" not in result
        assert "Content" in result

    def test_removes_list_view(self):
        md = "List View\nList View\nContent"
        result = postprocess_kijiji(md)
        assert "List View" not in result
        assert "Content" in result

    def test_removes_sponsored(self):
        md = "### Great Product\nSponsored\nProduct description"
        result = postprocess_kijiji(md)
        assert "Sponsored" not in result
        assert "Great Product" in result

    def test_removes_view_more(self):
        md = "Product description\nView more\nNext listing"
        result = postprocess_kijiji(md)
        assert "View more" not in result
        assert "Product description" in result

    def test_removes_results_count(self):
        md = "## Results 1 - 40 of 147,779\n\n## 147,779 results\nListings"
        result = postprocess_kijiji(md)
        assert "Results 1 - 40" not in result
        assert "147,779 results" not in result
        assert "Listings" in result

    def test_removes_popular_header(self):
        md = "Popular:\n- Acoustic guitar\n- Gaming laptop\nListings"
        result = postprocess_kijiji(md)
        assert "Popular:" not in result
        assert "Listings" in result

    def test_preserves_listing_content(self):
        md = (
            "### Men's Teva Sandals\n\n"
            "$30.00\n\n"
            "Ottawa\n\n"
            "These are size 9/EU 42. Grey sole with black straps."
        )
        result = postprocess_kijiji(md)
        assert "Teva Sandals" in result
        assert "$30.00" in result
        assert "size 9/EU 42" in result
