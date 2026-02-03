"""Tests for response caching."""

import time

from fetchaller.cache.response_cache import ResponseCache


class TestResponseCache:
    """Test response cache functionality."""

    def test_set_and_get(self):
        """Cache stores and retrieves entries."""
        cache = ResponseCache()
        cache.set("http://example.com", "content", "text/html")

        entry = cache.get("http://example.com")
        assert entry is not None
        assert entry.content == "content"
        assert entry.content_type == "text/html"

    def test_cache_miss(self):
        """Returns None for uncached URLs."""
        cache = ResponseCache()
        assert cache.get("http://not-cached.com") is None

    def test_expiration(self):
        """Expired entries return None."""
        cache = ResponseCache(default_ttl=0)  # Immediate expiration
        cache.set("http://example.com", "content", "text/html")

        time.sleep(0.01)  # Let it expire
        assert cache.get("http://example.com") is None

    def test_lru_eviction(self):
        """Oldest entry is evicted when at capacity."""
        cache = ResponseCache(max_entries=3)

        cache.set("http://one.com", "1", "text/html")
        cache.set("http://two.com", "2", "text/html")
        cache.set("http://three.com", "3", "text/html")

        # This should evict "one"
        cache.set("http://four.com", "4", "text/html")

        assert cache.get("http://one.com") is None
        assert cache.get("http://two.com") is not None
        assert cache.get("http://four.com") is not None

    def test_respects_max_entry_size(self):
        """Large entries are not cached."""
        cache = ResponseCache(max_entry_size=10)
        cache.set("http://example.com", "x" * 100, "text/html")

        assert cache.get("http://example.com") is None

    def test_respects_no_store(self):
        """Cache-Control: no-store prevents caching."""
        cache = ResponseCache()
        cache.set("http://example.com", "content", "text/html",
                  cache_control="no-store")

        assert cache.get("http://example.com") is None

    def test_invalidate(self):
        """Invalidate removes entry."""
        cache = ResponseCache()
        cache.set("http://example.com", "content", "text/html")
        cache.invalidate("http://example.com")

        assert cache.get("http://example.com") is None

    def test_clear(self):
        """Clear removes all entries."""
        cache = ResponseCache()
        cache.set("http://one.com", "1", "text/html")
        cache.set("http://two.com", "2", "text/html")
        cache.clear()

        assert cache.size() == 0

    def test_no_cache_pdf(self):
        """PDFs are not cached."""
        cache = ResponseCache()
        cache.set("http://example.com/doc.pdf", "content", "application/pdf")

        assert cache.get("http://example.com/doc.pdf") is None

    def test_no_cache_reddit_json(self):
        """Reddit API responses are not cached."""
        cache = ResponseCache()
        cache.set("http://reddit.com/r/test.json", "content", "application/json")

        assert cache.get("http://reddit.com/r/test.json") is None
