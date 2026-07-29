"""Tests for URL handling."""

from fetchaller.content.reddit import transform_reddit_url
from fetchaller.content.url import normalize_url


class TestURLNormalization:
    """Test URL normalization for caching."""

    def test_lowercase_host(self):
        """Host is lowercased while preserving the rest of the URL."""
        result = normalize_url("https://EXAMPLE.COM/path")
        assert result.startswith("https://example.com/path")
        assert "EXAMPLE" not in result

    def test_removes_tracking_params(self):
        """UTM and tracking params are stripped."""
        url = "https://example.com/page?utm_source=twitter&utm_medium=social&real=param"
        normalized = normalize_url(url)

        assert "utm_source" not in normalized
        assert "utm_medium" not in normalized
        assert "real=param" in normalized

    def test_preserves_query_param_order_as_resource_identity(self):
        url1 = normalize_url("https://example.com?b=2&a=1")
        url2 = normalize_url("https://example.com?a=1&b=2")

        assert url1 == "https://example.com?b=2&a=1"
        assert url2 == "https://example.com?a=1&b=2"
        assert url1 != url2

    def test_removes_fragment(self):
        """Fragment is removed but path is preserved."""
        url = normalize_url("https://example.com/page#section")
        assert "/page" in url
        assert "#section" not in url

    def test_removes_default_https_port(self):
        """Default HTTPS port 443 is removed."""
        url = normalize_url("https://example.com:443/path")
        assert ":443" not in url

    def test_removes_default_http_port(self):
        """Default HTTP port 80 is removed."""
        url = normalize_url("http://example.com:80/path")
        assert ":80" not in url

    def test_preserves_path_case(self):
        """Path case is preserved (servers may be case-sensitive)."""
        url = normalize_url("https://example.com/Path/To/Page")
        assert "/Path/To/Page" in url

    def test_preserves_case_sensitive_userinfo(self):
        upper = normalize_url("https://User:Secret@EXAMPLE.com/private")
        lower = normalize_url("https://user:secret@example.com/private")

        assert upper == "https://User:Secret@example.com/private"
        assert lower == "https://user:secret@example.com/private"
        assert upper != lower

    def test_preserves_path_parameters_as_resource_identity(self):
        english = normalize_url("https://example.com/item;lang=en")
        french = normalize_url("https://example.com/item;lang=fr")

        assert english == "https://example.com/item;lang=en"
        assert french == "https://example.com/item;lang=fr"
        assert english != french

    def test_preserves_order_of_repeated_query_values(self):
        first = normalize_url("https://example.com/?tag=red&x=1&tag=blue")
        second = normalize_url("https://example.com/?tag=blue&x=1&tag=red")

        assert first == "https://example.com/?tag=red&x=1&tag=blue"
        assert second == "https://example.com/?tag=blue&x=1&tag=red"
        assert first != second

    def test_preserves_raw_query_spelling_and_empty_delimiter(self):
        variants = {
            normalize_url("https://example.com/?flag"),
            normalize_url("https://example.com/?flag="),
            normalize_url("https://example.com/?q=%20"),
            normalize_url("https://example.com/?q=+"),
            normalize_url("https://example.com/?q=%2f"),
            normalize_url("https://example.com/?q=%2F"),
            normalize_url("https://example.com/?"),
            normalize_url("https://example.com/"),
        }

        assert len(variants) == 8

    def test_filters_encoded_tracking_key_without_reencoding_retained_segments(self):
        normalized = normalize_url(
            "https://example.com/?q=%2f&uTm%5Fsource=x&flag&tag=a+b"
        )

        assert normalized == "https://example.com/?q=%2f&flag&tag=a+b"


class TestRedditURLTransform:
    """Test strict Reddit recognition and New Reddit canonicalization."""

    def test_www_reddit_stays_new(self):
        result = transform_reddit_url("https://www.reddit.com/r/python")

        assert result.is_reddit is True
        assert result.url == "https://www.reddit.com/r/python"

    def test_bare_reddit_to_www(self):
        result = transform_reddit_url("https://reddit.com/r/python")

        assert result.is_reddit is True
        assert result.url == "https://www.reddit.com/r/python"

    def test_old_reddit_to_www(self):
        result = transform_reddit_url("https://old.reddit.com/r/python/")

        assert result.is_reddit is True
        assert result.url == "https://www.reddit.com/r/python/"

    def test_json_representation_is_preserved_on_new_host(self):
        result = transform_reddit_url("https://www.reddit.com/r/python.json")

        assert result.is_reddit is True
        assert result.url == "https://www.reddit.com/r/python.json"

    def test_non_reddit_unchanged(self):
        result = transform_reddit_url("https://example.com/page")

        assert result.is_reddit is False
        assert result.url == "https://example.com/page"

    def test_non_content_reddit_subdomains_keep_their_representation(self):
        for url in (
            "https://oauth.reddit.com/api/v1/me",
            "https://mod.reddit.com/mail/all",
            "https://chat.reddit.com/room/example",
            "https://api.reddit.com/r/python",
        ):
            result = transform_reddit_url(url)
            assert result.is_reddit is True
            assert result.url == url

    def test_reddit_path_params_and_nonstandard_ports_are_not_erased(self):
        result = transform_reddit_url(
            "https://old.reddit.com:8443/r/python;alternate?q=1"
        )

        assert result.is_reddit is True
        assert (
            result.url
            == "https://www.reddit.com:8443/r/python;alternate?q=1"
        )

    def test_hostile_reddit_substrings_are_not_reddit(self):
        for url in (
            "https://notreddit.com/r/python/",
            "https://reddit.com.example.test/r/python/",
            "https://redditcommunity.com/r/python/",
        ):
            result = transform_reddit_url(url)
            assert result.is_reddit is False
            assert result.url == url
