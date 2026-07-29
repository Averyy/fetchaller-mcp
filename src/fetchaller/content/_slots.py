"""Parser-process slot ownership shared by the isolated/HTML/PDF boundaries.

Kept dependency-free on purpose: ``_isolated`` imports process helpers from
``html``, so anything the three modules share has to live outside both.
"""

from __future__ import annotations

import asyncio
import threading


class SlotHandle:
    """Holds one parser-process slot until the child is actually reaped.

    ``async with semaphore:`` releases the moment the block exits. On the
    cancellation path the block exits while a daemon thread is still stopping
    the child, so the slot returned to the pool with the process still alive.
    Under repeated cancellation that let more children run at once than the
    semaphore permits, and every cancellation started another cleanup thread
    with nothing bounding how many could pile up.

    Ownership is explicit rather than first-caller-wins: the awaiting task's
    ``finally`` always runs before a cleanup thread finishes, so a race would
    resolve the wrong way every time. ``transfer()`` hands the release to the
    thread; the slot is released exactly once on either path.
    """

    __slots__ = ("_semaphore", "_loop", "_lock", "_released", "_transferred")

    def __init__(
        self,
        semaphore: asyncio.Semaphore,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._semaphore = semaphore
        self._loop = loop
        self._lock = threading.Lock()
        self._released = False
        self._transferred = False

    def transfer(self) -> None:
        """Make a cleanup thread responsible for releasing this slot."""
        with self._lock:
            self._transferred = True

    def untransfer(self) -> None:
        """Take ownership back when the cleanup handoff failed."""
        with self._lock:
            if not self._released:
                self._transferred = False

    def release(self) -> None:
        """Release from the owning task; a no-op once transferred."""
        with self._lock:
            if self._released or self._transferred:
                return
            self._released = True
        self._semaphore.release()

    def release_from_thread(self) -> None:
        """Release from a cleanup thread once the child is gone."""
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            self._loop.call_soon_threadsafe(self._semaphore.release)
        except RuntimeError:
            # Loop already closed (interpreter shutdown): nothing is waiting on
            # the slot any more, so dropping it is correct.
            pass
