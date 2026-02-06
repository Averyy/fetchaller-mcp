"""Response caching for fetchaller."""

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from ..config import Config


@dataclass
class CacheEntry:
    """A cached response."""

    content: str
    content_type: str
    fetched_at: float
    expires_at: float


class ResponseCache:
    """
    In-memory URL response cache with TTL.

    Features:
    - Default 5-minute TTL
    - Max 1000 entries (LRU eviction)
    - Max 1MB per entry
    - Don't cache PDFs (too large, rarely re-fetched)
    - Don't cache Reddit API responses (stale quickly)
    - Respects Cache-Control: no-store
    """

    # Content types that should not be cached
    NO_CACHE_TYPES = frozenset({"application/pdf"})

    # URL patterns that should not be cached
    NO_CACHE_PATTERNS = frozenset({"reddit.com/", ".json"})

    def __init__(
        self,
        default_ttl: int = 300,
        max_entries: int = 1000,
        max_entry_size: int = 1_000_000,
    ):
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self.max_entry_size = max_entry_size
        # OrderedDict maintains insertion order for O(1) LRU eviction
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()

    @classmethod
    def from_config(cls, config: Config) -> "ResponseCache":
        return cls(
            default_ttl=config.cache_default_ttl,
            max_entries=config.cache_max_entries,
            max_entry_size=config.cache_max_entry_size,
        )

    def _key(self, url: str) -> str:
        """Generate cache key from URL."""
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def _should_cache(self, url: str, content_type: str) -> bool:
        """Check if this URL/content type should be cached."""
        # Don't cache PDFs
        if any(ct in content_type.lower() for ct in self.NO_CACHE_TYPES):
            return False

        # Don't cache Reddit API responses
        if any(pattern in url.lower() for pattern in self.NO_CACHE_PATTERNS):
            return False

        return True

    def get(self, url: str) -> CacheEntry | None:
        """
        Get cached entry for URL.

        Returns None if not cached or expired.
        Promotes accessed entries for true LRU behavior.
        """
        key = self._key(url)
        entry = self._cache.get(key)

        if entry is None:
            return None

        # Check expiration
        if entry.expires_at <= time.time():
            del self._cache[key]
            return None

        # Promote to most-recently-used (true LRU)
        self._cache.move_to_end(key)
        return entry

    def set(
        self,
        url: str,
        content: str,
        content_type: str,
        ttl: float | None = None,
        cache_control: str | None = None,
    ) -> None:
        """
        Cache a response.

        Args:
            url: The URL that was fetched
            content: The response content
            content_type: Content-Type header value
            ttl: Optional TTL override (uses default_ttl if not specified)
            cache_control: Cache-Control header (respects no-store)
        """
        # Respect Cache-Control: no-store
        if cache_control and "no-store" in cache_control.lower():
            return

        # Don't cache certain content types or URLs
        if not self._should_cache(url, content_type):
            return

        # Don't cache huge responses
        if len(content) > self.max_entry_size:
            return

        # Evict if at limit: first purge expired, then LRU
        if len(self._cache) >= self.max_entries:
            now = time.time()
            expired = [k for k, v in self._cache.items() if v.expires_at <= now]
            for k in expired:
                del self._cache[k]
            # If still at limit, evict least-recently-used
            if len(self._cache) >= self.max_entries:
                self._cache.popitem(last=False)

        now = time.time()
        self._cache[self._key(url)] = CacheEntry(
            content=content,
            content_type=content_type,
            fetched_at=now,
            expires_at=now + (ttl if ttl is not None else self.default_ttl),
        )

    def invalidate(self, url: str) -> None:
        """Remove a URL from cache."""
        self._cache.pop(self._key(url), None)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()

    def size(self) -> int:
        """Return number of cached entries."""
        return len(self._cache)
