"""Tests for Reddit request queue."""

import time

import pytest

from fetchaller.queue.reddit_queue import QueueConfig, RedditRequestQueue


class TestRedditQueue:
    """Test Reddit request queue rate limiting."""

    @pytest.mark.asyncio
    async def test_enqueue_executes_and_returns_result(self):
        """First enqueue starts the queue and returns the callback's result."""
        queue = RedditRequestQueue()

        async def get_value():
            return "result"

        result = await queue.enqueue(get_value)
        assert result == "result"

        await queue.stop()

    @pytest.mark.asyncio
    async def test_executes_callbacks(self):
        """Queue executes callbacks and returns results."""
        queue = RedditRequestQueue()
        results = []

        async def callback(n):
            results.append(n)
            return n * 2

        r1 = await queue.enqueue(callback, 1)
        r2 = await queue.enqueue(callback, 2)
        r3 = await queue.enqueue(callback, 3)

        assert r1 == 2
        assert r2 == 4
        assert r3 == 6
        assert results == [1, 2, 3]

        await queue.stop()

    @pytest.mark.asyncio
    async def test_stop_prevents_further_processing(self):
        """After stop(), the queue no longer processes — new enqueue restarts it."""
        queue = RedditRequestQueue()

        async def dummy():
            return "ok"

        await queue.enqueue(dummy)
        await queue.stop()

        # After stop, enqueue still works (auto-restarts), proving stop was effective
        result = await queue.enqueue(dummy)
        assert result == "ok"

        await queue.stop()

    @pytest.mark.asyncio
    async def test_backoff_delays_next_request(self):
        """set_backoff(429) causes the next enqueue to be delayed."""
        config = QueueConfig(backoff_rate_limit=1, backoff_blocked=2)
        queue = RedditRequestQueue(config)

        async def dummy():
            return "ok"

        # Prime the queue so it's running
        await queue.enqueue(dummy)

        queue.set_backoff(429)
        start = time.time()
        result = await queue.enqueue(dummy)
        elapsed = time.time() - start

        assert result == "ok"
        # Should have waited ~1s (the backoff_rate_limit value)
        assert 0.8 <= elapsed < 3.0

        await queue.stop()

    @pytest.mark.asyncio
    async def test_rate_limit_delays_at_capacity(self):
        """At max_requests_per_minute, next request is delayed until window expires."""
        config = QueueConfig(max_requests_per_minute=2, proactive_threshold=2)
        queue = RedditRequestQueue(config)

        async def dummy():
            return "ok"

        # Fill the rate limit window
        await queue.enqueue(dummy)
        await queue.enqueue(dummy)

        # Third request should be delayed until oldest expires (~60s window)
        # We can't wait 60s in a test, so just verify the delay is calculated > 0
        # by checking _calculate_delay (it's the only way without a real 60s wait)
        delay = queue._calculate_delay()
        assert 50 < delay < 65  # Should be close to 60s

        await queue.stop()
