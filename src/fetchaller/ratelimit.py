"""Per-domain rate limiting shared across all modules.

Ensures minimum spacing between HTTP requests to a domain, preventing
bot detection from rapid-fire requests across different operations
(e.g., search followed immediately by product fetch).

All modules hitting the same domain share one limiter instance.
Modules pass extra_delay for heavier operations like search pages.
"""

from __future__ import annotations

import asyncio
import random
import time


class DomainRateLimiter:
    """Enforces minimum time between requests to a domain.

    Uses asyncio.Lock to serialize concurrent requests, ensuring they
    fire sequentially with proper spacing. Only one request passes
    through wait() at a time.
    """

    def __init__(
        self,
        min_interval: float,
        jitter: tuple[float, float] = (0.3, 1.0),
    ):
        self._lock = asyncio.Lock()
        self._last_time: float = 0.0
        self._min_interval = min_interval
        self._jitter_min, self._jitter_max = jitter

    async def wait(self, extra_delay: float = 0.0) -> None:
        """Wait until it's safe to make a request.

        Args:
            extra_delay: Additional seconds on top of the base interval
                for heavier operations (e.g., search pages).
        """
        async with self._lock:
            if self._last_time > 0:
                now = time.time()
                elapsed = now - self._last_time
                required = self._min_interval + extra_delay
                if elapsed < required:
                    await asyncio.sleep(required - elapsed)
                await asyncio.sleep(random.uniform(self._jitter_min, self._jitter_max))
            self._last_time = time.time()


# Shared instances — one per domain family.
#
# Alibaba: www.alibaba.com (search + product pages, both SSR HTML)
# Base 4s ensures product-after-search waits. Search adds 2s extra (6s total).
alibaba_limiter = DomainRateLimiter(min_interval=4.0, jitter=(0.5, 1.5))

# AliExpress: www/acs.aliexpress.com (search + MTop API)
# Reviews at feedback.aliexpress.com are exempt (separate service, no bot detection).
# Base 3s ensures product-after-search waits. Search adds 2s extra (5s total).
aliexpress_limiter = DomainRateLimiter(min_interval=3.0, jitter=(0.5, 1.5))

# Soylent: soylent.com / soylent.ca (Shopify stores)
# Shopify rate-limits cart/add.js and products.json aggressively (429 after ~15 fast requests).
# 2s base is conservative enough for sequential product page fetches.
soylent_limiter = DomainRateLimiter(min_interval=2.0, jitter=(0.3, 1.0))

# Reddit: old.reddit.com (HTML fetches via fetch tool)
# Reddit allows ~10 req/min. browse_reddit/search_reddit use RedditRequestQueue,
# but fetch tool bypasses it. 3s base keeps us well under the limit.
reddit_limiter = DomainRateLimiter(min_interval=3.0, jitter=(0.5, 1.5))

# Mouser: api.mouser.com (official API)
# 30 req/min API limit → 2s base interval.
mouser_limiter = DomainRateLimiter(min_interval=2.0, jitter=(0.3, 1.0))

# DigiKey: api.digikey.com (official API)
# 120 req/min burst, 1000/day → 0.5s base interval.
digikey_limiter = DomainRateLimiter(min_interval=0.5, jitter=(0.1, 0.3))

# Kijiji: www.kijiji.ca/anvil/api (unauthenticated GraphQL)
# No known rate limit, but 1s base interval to be polite.
kijiji_limiter = DomainRateLimiter(min_interval=1.0, jitter=(0.2, 0.5))

# Craigslist: sapi.craigslist.org (unauthenticated JSON search API)
# SAPI is the same endpoint the CL frontend uses. Rate limit to be polite.
craigslist_limiter = DomainRateLimiter(min_interval=2.0, jitter=(0.3, 1.0))

# Facebook: www.facebook.com/api/graphql/ (unauthenticated GraphQL)
# IP reputation is a concern — conservative rate limiting.
facebook_limiter = DomainRateLimiter(min_interval=3.0, jitter=(0.5, 1.5))
