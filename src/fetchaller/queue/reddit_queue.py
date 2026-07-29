"""Reddit request queue with proactive rate limiting."""

import asyncio
import math
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

from ..config import Config

T = TypeVar("T")
MAX_RETRY_AFTER_SECONDS = 3600.0
_QUEUE_STOP_GRACE_SECONDS = 1.0


def _consume_background_task(task: asyncio.Task) -> None:
    """Retrieve a detached callback/processor outcome without blocking."""

    try:
        task.exception()
    except BaseException:
        pass


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Parse an HTTP Retry-After value into non-negative delay seconds."""

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = retry_at.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(seconds):
        return None
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


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
    enqueued_at: float = field(default_factory=time.monotonic)


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
            processor = self._task
            processor.cancel()
            done, pending = await asyncio.wait(
                {processor},
                timeout=_QUEUE_STOP_GRACE_SECONDS,
            )
            if pending:
                processor.add_done_callback(_consume_background_task)
            elif done:
                _consume_background_task(processor)
            self._task = None
        while True:
            try:
                pending = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not pending.future.done():
                pending.future.cancel()

    def _count_recent_requests(self) -> int:
        """Count requests in the last minute."""
        now = time.monotonic()
        cutoff = now - 60

        # Remove old entries
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()

        return len(self._request_times)

    def _calculate_delay(self) -> float:
        """Calculate delay before next request."""
        now = time.monotonic()

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

    def set_backoff(
        self,
        status_code: int,
        retry_after: float | None = None,
        *,
        default_delay: float | None = None,
    ) -> None:
        """Set backoff from Retry-After when supplied, otherwise use defaults.

        ``default_delay`` lets a caller that has identified a *specific*,
        known-transient block substitute a shorter fallback than the blanket
        403 configuration. Reddit's anonymous-session gate clears in about two
        seconds, so holding every queued request for the configured five
        minutes turned a self-healing blip into an outage. An unrecognised 403
        still gets the configured delay. A ``Retry-After`` header always wins,
        and the ``max()`` below means this can only ever *lengthen* an
        already-imposed backoff, never cut one short.
        """

        now = time.monotonic()

        bounded_retry_after = (
            min(MAX_RETRY_AFTER_SECONDS, max(0.0, retry_after))
            if retry_after is not None and math.isfinite(retry_after)
            else None
        )
        bounded_default = (
            min(MAX_RETRY_AFTER_SECONDS, max(0.0, default_delay))
            if default_delay is not None and math.isfinite(default_delay)
            else None
        )
        if status_code == 429:
            fallback = (
                self.config.backoff_rate_limit
                if bounded_default is None
                else bounded_default
            )
            delay = fallback if bounded_retry_after is None else bounded_retry_after
        elif status_code == 403:
            fallback = (
                self.config.backoff_blocked
                if bounded_default is None
                else bounded_default
            )
            delay = fallback if bounded_retry_after is None else bounded_retry_after
        else:
            return
        # A later response with a shorter Retry-After must not shorten a backoff
        # already imposed by an earlier response.
        self._backoff_until = max(self._backoff_until, now + delay)

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

                # Skip items whose caller already timed out and abandoned them —
                # don't spend a rate-limited request on a result nobody awaits.
                if item.future.done():
                    item = None
                    continue

                # Compute the pending rate-limit/backoff delay, then enforce a
                # single total-wait bound: time already spent in queue PLUS this
                # delay must not exceed max_queue_wait. The old check looked only at
                # time-already-waited and ran BEFORE the sleep, so an item dequeued
                # during a 300s (403) backoff slept the whole backoff and hung its
                # caller far past any per-request timeout.
                delay = self._calculate_delay()
                total_wait = (time.monotonic() - item.enqueued_at) + delay
                if total_wait > self.config.max_queue_wait:
                    if not item.future.done():
                        item.future.set_exception(
                            TimeoutError(
                                f"Reddit request expired: total wait {total_wait:.1f}s exceeds "
                                f"max {self.config.max_queue_wait:.0f}s (backoff/delay {delay:.0f}s)"
                            )
                        )
                    item = None
                    continue

                if delay > 0:
                    await asyncio.sleep(delay)
                    # The caller's _queue_timeout may have fired DURING the sleep,
                    # cancelling item.future. Re-check before spending a request so
                    # a timed-out caller doesn't still consume rate-limit quota /
                    # hit the network for a result nobody will read.
                    if item.future.done():
                        item = None
                        continue

                # Record request time
                self._request_times.append(time.monotonic())

                # Execute the callback in its own task. If the caller abandons
                # the future while the request is in flight, cancel the
                # network operation so it cannot monopolize this serial queue.
                callback_task = asyncio.create_task(item.callback(*item.args, **item.kwargs))
                try:
                    done, _ = await asyncio.wait(
                        {callback_task, item.future},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if item.future in done and not callback_task.done():
                        callback_task.cancel()
                        callback_task.add_done_callback(_consume_background_task)
                    elif callback_task in done:
                        if callback_task.cancelled():
                            # A callback may cancel itself or propagate
                            # CancelledError from its own dependency. That
                            # cancels this item, not the long-lived processor.
                            if not item.future.done():
                                item.future.cancel()
                        else:
                            result = callback_task.result()
                            if not item.future.done():
                                item.future.set_result(result)
                except asyncio.CancelledError:
                    callback_task.cancel()
                    callback_task.add_done_callback(_consume_background_task)
                    raise
                except Exception as e:
                    if not item.future.done():
                        item.future.set_exception(e)
                finally:
                    if not callback_task.done():
                        callback_task.cancel()
                        callback_task.add_done_callback(_consume_background_task)
                    if callback_task.done() and not callback_task.cancelled():
                        # Retrieve a late exception if the caller disappeared
                        # exactly as the callback completed.
                        try:
                            callback_task.exception()
                        except asyncio.CancelledError:
                            pass
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
        _queue_timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        """
        Enqueue a request and wait for result.

        Args:
            callback: Async function to call
            *args: Positional arguments for callback
            _queue_timeout: Max seconds to wait for the result before giving up.
                Bounds the caller's wait so a long rate-limit backoff can't hang it
                past its own request timeout. None waits indefinitely (the queue's
                own max_queue_wait still applies on the processor side).
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
        if _queue_timeout is None:
            return await item.future
        try:
            return await asyncio.wait_for(item.future, timeout=_queue_timeout)
        except TimeoutError:
            # wait_for cancels item.future; the processor skips cancelled items.
            raise TimeoutError(f"Reddit request timed out after {_queue_timeout:.0f}s waiting in queue") from None
