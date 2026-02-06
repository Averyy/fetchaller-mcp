"""Reddit request queue with proactive rate limiting."""

import asyncio
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from ..config import Config

T = TypeVar("T")


@dataclass
class QueueConfig:
    """Configuration for Reddit request queue."""

    max_requests_per_minute: int = 10
    proactive_threshold: int = 8  # Start slowing at 8/10
    backoff_rate_limit: int = 60  # Backoff after 429
    backoff_blocked: int = 300  # Backoff after 403
    max_queue_wait: float = 30.0  # Max seconds an item can wait in queue

    @classmethod
    def from_config(cls, config: Config) -> "QueueConfig":
        return cls(
            max_requests_per_minute=config.reddit_max_requests_per_minute,
            proactive_threshold=config.reddit_proactive_threshold,
            backoff_rate_limit=config.reddit_backoff_rate_limit,
            backoff_blocked=config.reddit_backoff_blocked,
        )


@dataclass
class QueueItem:
    """An item in the request queue."""

    callback: Callable[..., Awaitable[Any]]
    args: tuple
    kwargs: dict
    # Note: future is created in enqueue() using the running loop, not here
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]
    enqueued_at: float = field(default_factory=time.time)


class RedditRequestQueue:
    """
    Request queue for Reddit API with proactive rate limiting.

    Features:
    - Tracks requests per minute (max 10)
    - Proactive slowdown at 8/10 requests
    - Exponential backoff on 429 (60s) or 403 (300s)
    - Async queue processing
    """

    def __init__(self, config: QueueConfig | None = None):
        self.config = config or QueueConfig()
        self._request_times: deque[float] = deque()
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None
        self._backoff_until: float = 0

    def start(self) -> None:
        """Start the queue processor."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._process_queue())

    async def stop(self) -> None:
        """Stop the queue processor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _count_recent_requests(self) -> int:
        """Count requests in the last minute."""
        now = time.time()
        cutoff = now - 60

        # Remove old entries
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()

        return len(self._request_times)

    def _calculate_delay(self) -> float:
        """Calculate delay before next request."""
        now = time.time()

        # Check backoff
        if self._backoff_until > now:
            return self._backoff_until - now

        recent = self._count_recent_requests()

        # At or over limit
        if recent >= self.config.max_requests_per_minute:
            # Wait for oldest request to expire
            oldest = self._request_times[0]
            return (oldest + 60) - now + 0.1  # Small buffer

        # Proactive slowdown
        if recent >= self.config.proactive_threshold:
            remaining_budget = self.config.max_requests_per_minute - recent
            # Spread remaining budget over remaining time
            if remaining_budget > 0:
                oldest = self._request_times[0]
                time_until_reset = (oldest + 60) - now
                if time_until_reset > 0:
                    return time_until_reset / (remaining_budget * 2)

        return 0.0

    def set_backoff(self, status_code: int) -> None:
        """Set backoff based on response status code."""
        now = time.time()

        if status_code == 429:
            self._backoff_until = now + self.config.backoff_rate_limit
        elif status_code == 403:
            self._backoff_until = now + self.config.backoff_blocked

    async def _process_queue(self) -> None:
        """Process queued requests with rate limiting."""
        item = None
        while self._running:
            try:
                # Get next item (with timeout to allow checking _running)
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except TimeoutError:
                    item = None
                    continue

                # Drop items that have been waiting too long
                wait_time = time.time() - item.enqueued_at
                if wait_time > self.config.max_queue_wait:
                    if not item.future.done():
                        item.future.set_exception(
                            TimeoutError(f"Request expired after {wait_time:.1f}s in queue")
                        )
                    item = None
                    continue

                # Calculate and apply delay
                delay = self._calculate_delay()
                if delay > 0:
                    await asyncio.sleep(delay)

                # Record request time
                self._request_times.append(time.time())

                # Execute the callback
                try:
                    result = await item.callback(*item.args, **item.kwargs)
                    item.future.set_result(result)
                except Exception as e:
                    item.future.set_exception(e)
                item = None

            except asyncio.CancelledError:
                # Cancel any in-flight item's future so callers don't hang
                if item and not item.future.done():
                    item.future.cancel()
                break
            except Exception as e:
                print(f"[{datetime.now(UTC).isoformat()}] Reddit queue error: {e}", file=sys.stderr)
                # Ensure the dequeued item's future is resolved
                if item and not item.future.done():
                    item.future.set_exception(e)
                item = None

    async def enqueue(
        self,
        callback: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """
        Enqueue a request and wait for result.

        Args:
            callback: Async function to call
            *args: Positional arguments for callback
            **kwargs: Keyword arguments for callback

        Returns:
            Result from callback
        """
        # Auto-start if not running
        if not self._running:
            self.start()

        loop = asyncio.get_running_loop()
        item = QueueItem(
            callback=callback,
            args=args,
            kwargs=kwargs,
            future=loop.create_future(),
        )

        await self._queue.put(item)
        return await item.future
