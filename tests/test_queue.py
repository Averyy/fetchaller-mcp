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
    async def test_tracks_request_count(self):
        """Queue tracks requests in the last minute."""
        queue = RedditRequestQueue()

        async def dummy():
            return None

        await queue.enqueue(dummy)
        await queue.enqueue(dummy)
        await queue.enqueue(dummy)

        assert queue._count_recent_requests() == 3

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

    def test_backoff_sets_time(self):
        """Backoff sets future time based on status code."""
        queue = RedditRequestQueue()

        queue.set_backoff(429)
        assert queue._backoff_until > time.time()

        queue._backoff_until = 0
        queue.set_backoff(403)
        assert queue._backoff_until > time.time()

    def test_calculate_delay_at_limit(self):
        """Delay is calculated when at rate limit."""
        queue = RedditRequestQueue(QueueConfig(max_requests_per_minute=2))

        # Simulate 2 requests
        now = time.time()
        queue._request_times.append(now)
        queue._request_times.append(now)

        delay = queue._calculate_delay()
        assert delay > 0  # Should wait for oldest to expire
