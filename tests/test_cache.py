"""Tests for response caching."""

import time
from unittest.mock import patch

import pytest

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

    def test_expiration(self):
        """Expired entries return None."""
        cache = ResponseCache(default_ttl=0)  # Immediate expiration
        cache.set("http://example.com", "content", "text/html")

        time.sleep(0.01)  # Let it expire
        assert cache.get("http://example.com") is None

    def test_lru_eviction_removes_oldest(self):
        """Least recently used entry is evicted, not just any entry."""
        cache = ResponseCache(max_entries=3)

        cache.set("http://one.com", "1", "text/html")
        cache.set("http://two.com", "2", "text/html")
        cache.set("http://three.com", "3", "text/html")

        # Access "one" to make it recently used — "two" becomes the LRU
        cache.get("http://one.com")

        # Adding a 4th entry should evict "two" (LRU), not "one"
        cache.set("http://four.com", "4", "text/html")

        assert cache.get("http://two.com") is None  # evicted (was LRU)
        assert cache.get("http://one.com").content == "1"  # kept (recently accessed)
        assert cache.get("http://three.com").content == "3"  # kept
        assert cache.get("http://four.com").content == "4"  # kept (just added)

    def test_replacing_entry_at_capacity_does_not_evict_another_url(self):
        """Refreshing one full-cache key keeps every unrelated entry."""
        cache = ResponseCache(max_entries=2)
        cache.set("http://one.com", "old", "text/html")
        cache.set("http://two.com", "two", "text/html")

        cache.set("http://one.com", "new", "text/html")

        assert cache.size() == 2
        assert cache.get("http://one.com").content == "new"
        assert cache.get("http://two.com").content == "two"

    def test_respects_max_entry_size(self):
        """Large entries are not cached."""
        cache = ResponseCache(max_entry_size=10)
        cache.set("http://example.com", "x" * 100, "text/html")

        assert cache.get("http://example.com") is None

    def test_max_entry_size_is_measured_in_utf8_bytes(self):
        """Multibyte text cannot exceed the configured byte budget."""
        cache = ResponseCache(max_entry_size=3)
        cache.set("http://example.com", "éé", "text/html")

        assert len("éé") == 2
        assert len("éé".encode()) == 4
        assert cache.get("http://example.com") is None

    def test_cache_key_uses_full_sha256_digest(self):
        """Cache keys retain the full collision-resistant URL digest."""
        cache = ResponseCache()

        assert len(cache._key("http://example.com")) == 64

    @pytest.mark.parametrize(
        "directive",
        [
            "no-store",
            "NO-STORE",
            "private",
            'private="Set-Cookie"',
            "no-cache",
            'no-cache="Authorization"',
            "public, max-age=0",
            'public, max-age="0"',
        ],
    )
    def test_restrictive_cache_control_prevents_caching(self, directive):
        """Shared cache never stores responses it cannot safely reuse."""
        cache = ResponseCache()
        cache.set(
            "http://example.com",
            "content",
            "text/html",
            cache_control=directive,
        )

        assert cache.get("http://example.com") is None

    @pytest.mark.parametrize(
        "directive",
        [
            "public, max-age=invalid",
            "public, max-age",
            "public, max-age=10, max-age=20",
        ],
    )
    def test_invalid_or_conflicting_max_age_fails_closed(self, directive):
        """An ambiguous origin lifetime is never replaced by a guessed TTL."""
        cache = ResponseCache()
        cache.set(
            "http://example.com",
            "content",
            "text/html",
            cache_control=directive,
        )

        assert cache.get("http://example.com") is None

    def test_restrictive_response_invalidates_prior_entry(self):
        """A later private response cannot leave an older public value usable."""
        cache = ResponseCache()
        url = "http://example.com/account"
        cache.set(url, "old public content", "text/html")
        assert cache.get(url) is not None

        cache.set(
            url,
            "private content",
            "text/html",
            cache_control="private",
        )

        assert cache.get(url) is None

    def test_origin_max_age_caps_configured_ttl_and_expires(self):
        """Origin max-age is an upper bound on the configured cache lifetime."""
        cache = ResponseCache(default_ttl=300)
        url = "http://example.com"
        with patch(
            "fetchaller.cache.response_cache.time.time",
            return_value=1_000.0,
        ):
            cache.set(
                url,
                "content",
                "text/html",
                cache_control="public, max-age=10",
            )
            entry = cache.get(url)

        assert entry is not None
        assert entry.fetched_at == 1_000.0
        assert entry.expires_at == 1_010.0

        with patch(
            "fetchaller.cache.response_cache.time.time",
            return_value=1_009.999,
        ):
            assert cache.get(url) is not None
        with patch(
            "fetchaller.cache.response_cache.time.time",
            return_value=1_010.0,
        ):
            assert cache.get(url) is None

    def test_shorter_requested_ttl_wins_over_origin_max_age(self):
        """A caller can shorten, but never extend, the origin lifetime."""
        cache = ResponseCache(default_ttl=300)
        with patch(
            "fetchaller.cache.response_cache.time.time",
            return_value=100.0,
        ):
            cache.set(
                "http://example.com",
                "content",
                "text/html",
                ttl=5,
                cache_control='public, max-age="60"',
            )
            entry = cache.get("http://example.com")

        assert entry is not None
        assert entry.expires_at == 105.0

    def test_shared_max_age_overrides_and_caps_max_age(self):
        """s-maxage is the authoritative lifetime for this shared cache."""
        cache = ResponseCache(default_ttl=300)
        with patch(
            "fetchaller.cache.response_cache.time.time",
            return_value=100.0,
        ):
            cache.set(
                "http://example.com",
                "content",
                "text/html",
                cache_control="public, max-age=60, s-maxage=5",
            )
            entry = cache.get("http://example.com")

        assert entry is not None
        assert entry.expires_at == 105.0

    def test_shared_max_age_zero_prevents_caching(self):
        """s-maxage=0 forbids reuse by a shared cache."""
        cache = ResponseCache()
        cache.set(
            "http://example.com",
            "content",
            "text/html",
            cache_control="public, max-age=60, s-maxage=0",
        )

        assert cache.get("http://example.com") is None

    @pytest.mark.parametrize("vary", ["*", "Cookie", "Accept-Language"])
    def test_vary_response_is_not_cached_without_variant_keys(self, vary):
        """A URL-only key cannot safely represent header-varying responses."""
        cache = ResponseCache()
        cache.set(
            "http://example.com",
            "content",
            "text/html",
            vary=vary,
        )

        assert cache.get("http://example.com") is None

    def test_vary_response_invalidates_prior_unvaried_entry(self):
        """A newly varying response makes the old URL-only entry unusable."""
        cache = ResponseCache()
        url = "http://example.com"
        cache.set(url, "old", "text/html")

        cache.set(url, "new", "text/html", vary="Cookie")

        assert cache.get(url) is None

    @pytest.mark.parametrize("ttl", [float("nan"), float("inf"), -float("inf")])
    def test_nonfinite_ttl_is_not_cached(self, ttl):
        """A nonfinite expiry must not turn into a permanently live entry."""
        cache = ResponseCache()
        cache.set("http://example.com", "content", "text/html", ttl=ttl)

        assert cache.get("http://example.com") is None

    def test_excessive_origin_max_age_is_safely_bounded(self):
        """Untrusted thousands-digit delta-seconds cannot exhaust integer parsing."""
        cache = ResponseCache(default_ttl=300)
        cache.set(
            "http://example.com",
            "content",
            "text/html",
            cache_control=f"max-age={'9' * 10_000}",
        )

        entry = cache.get("http://example.com")
        assert entry is not None
        assert entry.expires_at - entry.fetched_at == 300

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
