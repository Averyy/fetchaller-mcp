"""Tests for per-domain rate limiting."""

import asyncio
import time

import pytest

from fetchaller.ratelimit import DomainRateLimiter


class TestDomainRateLimiter:
    """DomainRateLimiter enforces minimum spacing between requests."""

    @pytest.mark.asyncio
    async def test_first_call_applies_jitter_only(self):
        """First call should not wait for the base interval (no prior request)."""
        limiter = DomainRateLimiter(min_interval=5.0, jitter=(0.0, 0.0))
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        # Should be nearly instant (no prior request, zero jitter)
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_enforces_minimum_interval(self):
        """Second call must wait at least min_interval since the first."""
        limiter = DomainRateLimiter(min_interval=0.05, jitter=(0.0, 0.0))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04  # Allow small timing tolerance

    @pytest.mark.asyncio
    async def test_extra_delay_adds_to_interval(self):
        """extra_delay should extend the minimum wait beyond the base interval."""
        limiter = DomainRateLimiter(min_interval=0.03, jitter=(0.0, 0.0))
        await limiter.wait()
        start = time.monotonic()
        await limiter.wait(extra_delay=0.04)
        elapsed = time.monotonic() - start
        # Should wait 0.03 base + 0.04 extra = 0.07s
        assert elapsed >= 0.05
        assert elapsed < 0.5

    @pytest.mark.asyncio
    async def test_no_extra_wait_after_sufficient_time(self):
        """If enough time has passed, wait() should only apply jitter."""
        limiter = DomainRateLimiter(min_interval=0.02, jitter=(0.0, 0.0))
        await limiter.wait()
        await asyncio.sleep(0.04)  # More than min_interval
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        # Should be nearly instant
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_jitter_adds_randomness(self):
        """Jitter should add time beyond the base interval."""
        limiter = DomainRateLimiter(min_interval=0.0, jitter=(0.02, 0.02))
        await limiter.wait()  # First call
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        # Jitter of exactly 0.02s should be applied
        assert elapsed >= 0.01

    @pytest.mark.asyncio
    async def test_serializes_concurrent_calls(self):
        """Concurrent wait() calls should serialize, not overlap."""
        limiter = DomainRateLimiter(min_interval=0.03, jitter=(0.0, 0.0))
        timestamps: list[float] = []

        async def record():
            await limiter.wait()
            timestamps.append(time.monotonic())

        # Fire 3 concurrent calls
        await asyncio.gather(record(), record(), record())
        assert len(timestamps) == 3
        # Each should be spaced by at least min_interval from the previous
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            assert gap >= 0.02, f"Gap {i-1}→{i}: {gap:.3f}s < 0.02s"


class TestCrossModuleCoordination:
    """Shared limiter enforces spacing across different operations."""

    @pytest.mark.asyncio
    async def test_product_after_search_respects_base_interval(self):
        """A 'product' call after 'search' waits at least the base interval."""
        limiter = DomainRateLimiter(min_interval=0.05, jitter=(0.0, 0.0))
        # Simulate search (with extra_delay)
        await limiter.wait(extra_delay=0.03)
        # Simulate product (no extra_delay) — should still wait base interval
        start = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_search_after_product_respects_full_interval(self):
        """A 'search' call after 'product' waits base + extra_delay."""
        limiter = DomainRateLimiter(min_interval=0.03, jitter=(0.0, 0.0))
        # Simulate product (no extra)
        await limiter.wait()
        # Simulate search (with extra_delay) — should wait base + extra
        start = time.monotonic()
        await limiter.wait(extra_delay=0.04)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.05
        assert elapsed < 0.5
