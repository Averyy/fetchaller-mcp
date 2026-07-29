"""Disposable parser-process boundary regression tests."""

from __future__ import annotations

import asyncio
import multiprocessing
import time

import pytest

from fetchaller.content._isolated import (
    IsolatedProcessingError,
    run_isolated,
)
from fetchaller.content.html_preflight import inspect_html_preflight


def _sleep_then_return(delay: float, value: str) -> str:
    time.sleep(delay)
    return value


def _identity(value: str) -> str:
    return value


async def _event_loop_ticks(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        ticks += 1
        await asyncio.sleep(0.005)
    return ticks


@pytest.mark.asyncio
async def test_timeout_does_not_block_loop_and_next_worker_recovers() -> None:
    baseline = {child.pid for child in multiprocessing.active_children()}
    stop = asyncio.Event()
    ticker = asyncio.create_task(_event_loop_ticks(stop))
    try:
        with pytest.raises(IsolatedProcessingError, match="timed out"):
            await run_isolated(
                _sleep_then_return,
                2.0,
                "late",
                timeout=0.1,
            )
        assert await run_isolated(_identity, "recovered", timeout=5) == "recovered"
    finally:
        stop.set()
    assert await ticker >= 5

    leaked: set[int | None] = set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        leaked = {child.pid for child in multiprocessing.active_children() if child.pid not in baseline}
        if not leaked:
            break
        await asyncio.sleep(0.02)
    assert not leaked


@pytest.mark.asyncio
async def test_hostile_marker_html_is_inspected_off_event_loop() -> None:
    html = '<html><body><div id="grnhse_app">' + ("<div>" * 180_000) + ("</div>" * 180_000) + "</body></html>"
    stop = asyncio.Event()
    ticker = asyncio.create_task(_event_loop_ticks(stop))
    try:
        result = await run_isolated(
            inspect_html_preflight,
            html,
            "https://example.com/careers",
            False,
            False,
            False,
            False,
            timeout=10,
        )
    finally:
        stop.set()
    assert result.greenhouse_detected is True
    assert await ticker >= 2


class TestSlotHandleOwnership:
    """A cancelled parser must keep its slot until the child is really gone.

    ``async with semaphore:`` released the slot the instant the block exited,
    while a daemon thread was still terminating the child. Under repeated
    cancellation that let more parser processes run at once than the cap
    allows, and nothing bounded how many cleanup threads piled up.

    Ownership is explicit rather than first-caller-wins: the awaiting task's
    ``finally`` always runs before the cleanup thread finishes, so a
    first-wins race would resolve the wrong way every single time.
    """

    @staticmethod
    def _handle():
        import asyncio as _asyncio

        from fetchaller.content._slots import SlotHandle

        loop = _asyncio.new_event_loop()
        semaphore = _asyncio.Semaphore(1)
        return SlotHandle(semaphore, loop), semaphore, loop

    def test_plain_release_returns_the_slot(self):
        handle, semaphore, loop = self._handle()
        try:
            assert semaphore.locked() is False
            loop.run_until_complete(semaphore.acquire())
            assert semaphore.locked() is True
            handle.release()
            assert semaphore.locked() is False
        finally:
            loop.close()

    def test_transferred_slot_is_not_released_by_the_owning_task(self):
        """The exact ordering that made a first-wins design wrong."""

        handle, semaphore, loop = self._handle()
        try:
            loop.run_until_complete(semaphore.acquire())
            handle.transfer()          # cleanup thread now owns it
            handle.release()           # owning task's finally -- must no-op
            assert semaphore.locked() is True, "slot freed while child still alive"
        finally:
            loop.close()

    def test_cleanup_thread_release_frees_it_exactly_once(self):
        handle, semaphore, loop = self._handle()
        try:
            loop.run_until_complete(semaphore.acquire())
            handle.transfer()
            handle.release()
            handle.release_from_thread()
            loop.call_soon(loop.stop)
            loop.run_forever()          # drain call_soon_threadsafe
            assert semaphore.locked() is False
            # A second release must not over-credit the pool.
            handle.release_from_thread()
            handle.release()
            loop.call_soon(loop.stop)
            loop.run_forever()
            assert semaphore._value == 1
        finally:
            loop.close()

    def test_release_after_loop_close_does_not_raise(self):
        """Interpreter shutdown: nothing waits on the slot, so dropping is fine."""

        handle, semaphore, loop = self._handle()
        loop.run_until_complete(semaphore.acquire())
        handle.transfer()
        loop.close()
        handle.release_from_thread()  # must not raise

    def test_every_parser_module_holds_its_slot_through_cleanup(self):
        """All three boundaries must transfer ownership, not just one."""

        import pathlib

        for name in ("_isolated.py", "html.py", "pdf.py"):
            source = pathlib.Path("src/fetchaller/content", name).read_text()
            # Every deferred-cleanup thread must take ownership, give it back if
            # the handoff fails, and release once the child is actually reaped.
            for token in (
                "handle.transfer()",
                "handle.untransfer()",
                "handle.release_from_thread()",
            ):
                assert token in source, f"{name}: missing {token}"
            # A cleanup thread must never be started before ownership moves.
            assert "cleanup_thread.start()" in source, (
                f"{name}: cleanup thread is not started transactionally"
            )
