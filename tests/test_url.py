"""Tests for URL handling."""

from fetchaller.content.reddit import transform_reddit_url
from fetchaller.content.url import normalize_url


class TestURLNormalization:
    """Test URL normalization for caching."""

    def test_lowercase_host(self):
        """Host is lowercased."""
        assert "example.com" in normalize_url("https://EXAMPLE.COM/path")

    def test_removes_tracking_params(self):
        """UTM and tracking params are stripped."""
        url = "https://example.com/page?utm_source=twitter&utm_medium=social&real=param"
        normalized = normalize_url(url)

        assert "utm_source" not in normalized
        assert "utm_medium" not in normalized
        assert "real=param" in normalized

    def test_sorts_query_params(self):
        """Query params are sorted for consistency."""
        url1 = normalize_url("https://example.com?b=2&a=1")
        url2 = normalize_url("https://example.com?a=1&b=2")

        assert url1 == url2

    def test_removes_fragment(self):
        """Fragment is removed."""
        url = normalize_url("https://example.com/page#section")
        assert "#" not in url

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


class TestRedditURLTransform:
    """Test Reddit URL transformation."""

    def test_www_reddit_to_old(self):
        """www.reddit.com transforms to old.reddit.com."""
        result = transform_reddit_url("https://www.reddit.com/r/python")

        assert result.is_reddit is True
        assert "old.reddit.com" in result.url

    def test_reddit_to_old(self):
        """reddit.com transforms to old.reddit.com."""
        result = transform_reddit_url("https://reddit.com/r/python")

        assert result.is_reddit is True
        assert "old.reddit.com" in result.url

    def test_adds_trailing_slash(self):
        """Trailing slash is added to avoid redirects."""
        result = transform_reddit_url("https://www.reddit.com/r/python")

        assert result.url.endswith("/")

    def test_json_urls_unchanged(self):
        """JSON API URLs are not transformed."""
        result = transform_reddit_url("https://www.reddit.com/r/python.json")

        assert result.is_reddit is True
        # JSON URLs should keep www for API access
        assert ".json" in result.url

    def test_non_reddit_unchanged(self):
        """Non-Reddit URLs pass through unchanged."""
        result = transform_reddit_url("https://example.com/page")

        assert result.is_reddit is False
        assert result.url == "https://example.com/page"

    def test_old_reddit_adds_slash(self):
        """old.reddit.com URLs just get trailing slash."""
        result = transform_reddit_url("https://old.reddit.com/r/python")

        assert result.is_reddit is True
        assert result.url == "https://old.reddit.com/r/python/"
