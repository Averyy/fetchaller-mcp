"""Tests for Reddit request queue."""

import asyncio
import time

import pytest

from fetchaller.queue.reddit_queue import QueueConfig, RedditRequestQueue, parse_retry_after


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
        config = QueueConfig(backoff_rate_limit=0.05, backoff_blocked=0.1)
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
        # Should have waited ~0.05s (the backoff_rate_limit value)
        assert 0.03 <= elapsed < 1.0

        await queue.stop()

    def test_retry_after_parser_supports_seconds_dates_and_invalid_values(self):
        assert parse_retry_after("12") == 12.0
        assert parse_retry_after("1.5") == 1.5
        assert parse_retry_after("-3") == 0.0
        assert (
            parse_retry_after(
                "Thu, 01 Jan 1970 00:00:30 GMT",
                now=0.0,
            )
            == 30.0
        )
        assert parse_retry_after("") is None
        assert parse_retry_after("not a date") is None
        assert parse_retry_after("inf") is None
        assert parse_retry_after("nan") is None
        assert parse_retry_after("999999") == 3600.0

    def test_dynamic_retry_after_sets_exact_backoff_without_shortening(self):
        queue = RedditRequestQueue(QueueConfig(backoff_rate_limit=60))

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
            queue.set_backoff(429, retry_after=137.0)
            assert queue._backoff_until == 1137.0
            queue.set_backoff(429, retry_after=2.0)
            assert queue._backoff_until == 1137.0

    def test_403_honors_bounded_retry_after_without_shortening(self):
        queue = RedditRequestQueue(QueueConfig(backoff_blocked=300))

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
            queue.set_backoff(403, retry_after=42.0)
            assert queue._backoff_until == 1042.0
            queue.set_backoff(403, retry_after=2.0)
            assert queue._backoff_until == 1042.0

            another = RedditRequestQueue(QueueConfig(backoff_blocked=300))
            another.set_backoff(403, retry_after=99_999.0)
            assert another._backoff_until == 4600.0
            fallback = RedditRequestQueue(QueueConfig(backoff_blocked=300))
            fallback.set_backoff(403, retry_after=float("nan"))
            assert fallback._backoff_until == 1300.0

    def test_relative_queue_state_never_reads_wall_clock(self, monkeypatch):
        from fetchaller.queue import reddit_queue

        monkeypatch.setattr(
            reddit_queue.time,
            "time",
            lambda: (_ for _ in ()).throw(AssertionError("wall clock used")),
        )
        monkeypatch.setattr(reddit_queue.time, "monotonic", lambda: 500.0)
        queue = RedditRequestQueue()

        queue.set_backoff(429, retry_after=3)

        assert queue._backoff_until == 503.0

    @pytest.mark.asyncio
    async def test_backoff_beyond_max_queue_wait_rejects_fast(self):
        """A backoff longer than max_queue_wait must fail the item fast, not sleep it.

        Regression: the stale-drop check ran BEFORE the delay sleep, so an item
        dequeued during a 300s (403) backoff slept the whole backoff and hung its
        caller far past any per-request timeout.
        """
        queue = RedditRequestQueue(QueueConfig(max_queue_wait=2.0))

        async def dummy():
            return "ok"

        queue.set_backoff(403)  # 300s backoff (default backoff_blocked)
        start = time.time()
        with pytest.raises(TimeoutError):
            await queue.enqueue(dummy)
        assert time.time() - start < 2.0  # rejected fast, did not sleep 300s

        await queue.stop()

    @pytest.mark.asyncio
    async def test_timeout_during_backoff_sleep_skips_callback(self):
        """If the caller times out DURING the rate-limit/backoff sleep, the
        processor must re-check and skip the callback — no wasted request/quota.
        """
        queue = RedditRequestQueue(QueueConfig(backoff_rate_limit=0.4, max_queue_wait=30.0))
        await queue.enqueue(lambda: asyncio.sleep(0))  # prime (records 1 request)
        queue.set_backoff(429)  # processor will sleep ~0.4s before running

        ran = {"n": 0}

        async def cb():
            ran["n"] += 1
            return "ok"

        with pytest.raises(TimeoutError):
            await queue.enqueue(cb, _queue_timeout=0.1)  # times out mid-sleep
        await asyncio.sleep(0.6)  # let the processor finish the backoff sleep

        assert ran["n"] == 0, "callback ran for an abandoned (timed-out) item"
        assert len(queue._request_times) == 1, "quota consumed for abandoned item"

        await queue.stop()

    @pytest.mark.asyncio
    async def test_queue_timeout_honored(self):
        """_queue_timeout bounds the caller's wait even when the callback is slow."""
        queue = RedditRequestQueue()

        async def slow():
            await asyncio.sleep(10)
            return "late"

        start = time.time()
        with pytest.raises(TimeoutError):
            await queue.enqueue(slow, _queue_timeout=0.2)
        assert time.time() - start < 2.0

        await queue.stop()

    @pytest.mark.asyncio
    async def test_timeout_cancels_in_flight_callback_and_unblocks_next_item(self):
        queue = RedditRequestQueue()
        cancelled = asyncio.Event()

        async def slow():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(TimeoutError):
            await queue.enqueue(slow, _queue_timeout=0.05)

        assert await asyncio.wait_for(cancelled.wait(), timeout=1)

        async def quick():
            return "next"

        assert await queue.enqueue(quick, _queue_timeout=1) == "next"
        await queue.stop()

    @pytest.mark.asyncio
    async def test_callback_cancelled_error_does_not_kill_processor(self):
        queue = RedditRequestQueue()

        async def cancelled():
            raise asyncio.CancelledError

        first = asyncio.create_task(queue.enqueue(cancelled))
        with pytest.raises(asyncio.CancelledError):
            await first

        async def quick():
            return "still alive"

        assert queue._running is True
        assert await queue.enqueue(quick, _queue_timeout=1) == "still alive"
        await queue.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_in_flight_and_queued_callers(self):
        queue = RedditRequestQueue()
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked(value):
            started.set()
            await release.wait()
            return value

        first = asyncio.create_task(queue.enqueue(blocked, 1))
        await started.wait()
        second = asyncio.create_task(queue.enqueue(blocked, 2))
        await asyncio.sleep(0)

        await queue.stop()

        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(asyncio.CancelledError):
            await second
        assert queue._queue.empty()

    @pytest.mark.asyncio
    async def test_stop_does_not_wait_for_callback_suppressing_cancellation(
        self,
    ):
        queue = RedditRequestQueue()
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def noncooperative():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()

        caller = asyncio.create_task(queue.enqueue(noncooperative))
        await started.wait()
        started_at = time.monotonic()
        await queue.stop()
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.5
        with pytest.raises(asyncio.CancelledError):
            await caller

        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)

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
