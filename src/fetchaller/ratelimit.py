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
        self._backoff_until: float = 0.0

    def defer(self, seconds: float) -> None:
        """Prevent future callers from passing until a server-requested delay ends."""

        self._backoff_until = max(
            self._backoff_until,
            time.monotonic() + max(0.0, seconds),
        )

    async def wait(self, extra_delay: float = 0.0) -> None:
        """Wait until it's safe to make a request.

        Args:
            extra_delay: Additional seconds on top of the base interval
                for heavier operations (e.g., search pages).
        """
        async with self._lock:
            spacing_target = 0.0
            if self._last_time > 0:
                spacing_target = (
                    self._last_time
                    + self._min_interval
                    + extra_delay
                    + random.uniform(self._jitter_min, self._jitter_max)
                )
            while True:
                now = time.monotonic()
                target = max(spacing_target, self._backoff_until)
                if target <= now:
                    break
                await asyncio.sleep(target - now)
                # Recheck: another request can receive Retry-After and call
                # defer() while this caller is already asleep.
            self._last_time = time.monotonic()


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

# Reddit anonymous JSON. MCP tool calls share RedditRequestQueue; this limiter
# covers direct/library fetch_url calls where the server queue is not injected.
# Six seconds plus small jitter stays below the documented ~10 req/min budget.
reddit_limiter = DomainRateLimiter(min_interval=6.0, jitter=(0.1, 0.4))

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

# Costco: search.costco.com / search.costco.ca (API key-authed JSON search)
# Conservative rate limit — API key rotation on 401 means we want to be gentle.
costco_limiter = DomainRateLimiter(min_interval=2.0, jitter=(0.3, 1.0))

# Facebook: www.facebook.com/api/graphql/ (unauthenticated GraphQL)
# IP reputation is a concern — conservative rate limiting.
facebook_limiter = DomainRateLimiter(min_interval=3.0, jitter=(0.5, 1.5))

# Google careers: www.google.com/about/careers/applications (BOQ batchexecute)
# An internal RPC on google.com proper, so this stays deliberately gentle even
# though it answers anonymously; a filtered search pages 20 at a time.
google_jobs_limiter = DomainRateLimiter(min_interval=2.0, jitter=(0.3, 0.8))

# Oracle Recruiting Cloud: {tenant}.fa.{region}.oraclecloud.com/hcmRestApi
# Fusion hosts are shared infrastructure serving many tenants, so this stays
# conservative even though the endpoints are public and unauthenticated.
oracle_recruiting_limiter = DomainRateLimiter(min_interval=1.5, jitter=(0.2, 0.6))

# Uber: www.uber.com/api/loadSearchJobsResults (anonymous board JSON)
# The whole global board is a few hundred reqs, so a full pull is 2-7 calls.
uber_jobs_limiter = DomainRateLimiter(min_interval=1.5, jitter=(0.2, 0.6))

# Meta: www.metacareers.com/graphql (anonymous persisted queries)
# Same IP-reputation caution as the facebook.com limiter, and doc_id discovery
# can walk several multi-hundred-KB bundles in a row.
meta_careers_limiter = DomainRateLimiter(min_interval=2.0, jitter=(0.3, 0.8))

# Apple: jobs.apple.com/{locale}/search (server-rendered HTML, ~190KB a page)
# Each call is a full page render rather than a JSON row set, so pages are
# spaced further apart than the JSON boards.
apple_jobs_limiter = DomainRateLimiter(min_interval=2.0, jitter=(0.3, 0.8))

# amazon.jobs: www.amazon.jobs/en/search.json (anonymous board JSON)
# Amazon's own site polls this route freely, but a filtered search pages 100 at
# a time; 1.5s keeps a multi-page walk unremarkable.
amazon_jobs_limiter = DomainRateLimiter(min_interval=1.5, jitter=(0.2, 0.6))

# Eightfold: {tenant}/api/pcsx/* (anonymous career-site JSON)
# One limiter covers every tenant. The endpoints are the ones the tenant's own
# SPA calls and answered every probe without a 429, but a job search pages in
# tens of results, so 1s keeps a multi-page walk from looking like a scrape.
eightfold_limiter = DomainRateLimiter(min_interval=1.0, jitter=(0.2, 0.5))

# LinkedIn: www.linkedin.com/jobs-guest/* (logged-out public job endpoints)
# 3.2s was the measured safe operating point — 46 probes at that spacing drew
# no 403, 429, Retry-After, or challenge. The blocking threshold was
# deliberately never probed, so treat this as a floor, not a target.
linkedin_limiter = DomainRateLimiter(min_interval=3.2, jitter=(0.1, 0.4))
