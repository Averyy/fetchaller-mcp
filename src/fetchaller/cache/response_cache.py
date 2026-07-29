"""Response caching for fetchaller."""

import hashlib
import math
import re
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
    - Respects origin Cache-Control restrictions and maximum age
    """

    # Content types that should not be cached
    NO_CACHE_TYPES = frozenset({"application/pdf"})

    # URL patterns that should not be cached
    NO_CACHE_PATTERNS = frozenset({"reddit.com/", ".json"})
    _MAX_AGE_RE = re.compile(r"^\s*(?:\"(\d+)\"|(\d+))\s*$")

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
        return hashlib.sha256(url.encode()).hexdigest()

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
        vary: str | None = None,
    ) -> None:
        """
        Cache a response.

        Args:
            url: The URL that was fetched
            content: The response content
            content_type: Content-Type header value
            ttl: Optional TTL override (uses default_ttl if not specified)
            cache_control: Origin Cache-Control header. ``private``,
                ``no-cache``, ``no-store``, and ``max-age=0`` prevent storage;
                a positive ``max-age`` caps the requested/configured TTL.
            vary: Origin Vary header. Responses that vary by request headers
                are not stored because this URL-only cache has no variant keys.
        """
        key = self._key(url)
        effective_ttl = ttl if ttl is not None else self.default_ttl
        try:
            finite_ttl = math.isfinite(effective_ttl)
        except (OverflowError, TypeError):
            finite_ttl = False
        if not finite_ttl:
            self._cache.pop(key, None)
            return
        if vary and vary.strip():
            self._cache.pop(key, None)
            return

        # This is a shared cache and it has no revalidation implementation.
        # A response marked private therefore cannot be stored, and no-cache
        # must be treated as uncacheable rather than served without validation.
        # Invalidate an older entry too: a newly observed restrictive response
        # must not leave stale public content reachable under the same URL.
        cache_allowed, origin_max_age = self._cache_policy(cache_control)
        if not cache_allowed:
            self._cache.pop(key, None)
            return

        if origin_max_age is not None:
            effective_ttl = min(effective_ttl, origin_max_age)
        if effective_ttl <= 0:
            self._cache.pop(key, None)
            return

        # Don't cache certain content types or URLs
        if not self._should_cache(url, content_type):
            self._cache.pop(key, None)
            return

        # Don't cache huge responses
        if len(content.encode("utf-8", errors="replace")) > self.max_entry_size:
            self._cache.pop(key, None)
            return

        if self.max_entries <= 0:
            self._cache.pop(key, None)
            return

        # Replacing an existing URL is an update, not an additional entry. Drop
        # it before capacity enforcement so refreshing a full cache does not
        # evict an unrelated LRU entry as well.
        self._cache.pop(key, None)

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
        self._cache[key] = CacheEntry(
            content=content,
            content_type=content_type,
            fetched_at=now,
            expires_at=now + effective_ttl,
        )

    @classmethod
    def _cache_policy(cls, cache_control: str | None) -> tuple[bool, int | None]:
        """Return whether storage is allowed and the strictest origin max-age.

        Cache-Control field names and directives are case-insensitive. Quoted
        decimal max-age values are accepted; malformed or conflicting max-age
        directives fail closed because guessing a lifetime could retain content
        longer than the origin intended.
        """
        if not cache_control:
            return True, None

        ages: dict[str, list[int]] = {"max-age": [], "s-maxage": []}
        for raw_directive in cache_control.split(","):
            directive = raw_directive.strip()
            if not directive:
                continue

            name, separator, value = directive.partition("=")
            name = name.strip().lower()
            if name in {"private", "no-cache", "no-store"}:
                return False, None
            if name not in ages:
                continue
            if not separator:
                return False, None

            match = cls._MAX_AGE_RE.fullmatch(value)
            if match is None:
                return False, None
            max_age_text = match.group(1) or match.group(2)
            # RFC 9111 requires caches to treat excessive delta-seconds as
            # 2^31 or 2^31-1. Either is far above this cache's configured TTL;
            # clamping also prevents adversarial thousands-digit integers.
            significant = max_age_text.lstrip("0") or "0"
            max_age = (
                2_147_483_648
                if len(significant) > 10
                else min(int(significant), 2_147_483_648)
            )
            if max_age == 0:
                return False, None
            ages[name].append(max_age)

        for values in ages.values():
            if len(set(values)) > 1:
                return False, None

        # This is a shared cache, so s-maxage overrides max-age when present.
        selected = ages["s-maxage"] or ages["max-age"]
        if selected and selected[0] == 0:
            return False, None
        return True, selected[0] if selected else None

    def invalidate(self, url: str) -> None:
        """Remove a URL from cache."""
        self._cache.pop(self._key(url), None)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()

    def size(self) -> int:
        """Return number of cached entries."""
        return len(self._cache)
