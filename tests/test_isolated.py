"""Disposable parser-process boundary regression tests."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
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


@pytest.mark.asyncio
async def test_cancellation_during_process_start_holds_capacity_until_cleanup(
    monkeypatch,
) -> None:
    """Cancelling the await must not cancel its still-running startup thread."""
    from fetchaller.content import _isolated as isolated

    entered_start = threading.Event()
    continue_start = threading.Event()
    original_start = isolated._start_process

    def _blocked_start(*args, **kwargs):
        entered_start.set()
        if not continue_start.wait(timeout=5):
            raise RuntimeError("test did not release process startup")
        return original_start(*args, **kwargs)

    monkeypatch.setattr(isolated, "_MAX_CONCURRENT_PROCESSES", 1)
    monkeypatch.setattr(isolated, "_start_process", _blocked_start)

    first = asyncio.create_task(run_isolated(_identity, "cancelled", timeout=5))
    second = None
    try:
        assert await asyncio.to_thread(entered_start.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(run_isolated(_identity, "recovered", timeout=5))
        await asyncio.sleep(0.05)
        assert not second.done(), "parser capacity was released before startup cleanup"

        continue_start.set()
        assert await asyncio.wait_for(second, timeout=5) == "recovered"
    finally:
        continue_start.set()
        if not first.done():
            first.cancel()
        if second is not None and not second.done():
            second.cancel()


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


class TestForkserverPreload:
    """Workers must start with the parser stack already imported.

    Without a forkserver preload every worker imported BeautifulSoup,
    markdownify, PyMuPDF and wafer for itself before doing any work: ~1.8 s
    per worker in the Linux image, three workers per HTML page, which took a
    1.8 MB amazon.com product page to ~10 s against fetch's 10 s default.
    """

    # Every function handed to run_isolated, by the name it is called with.
    # A new one must be imported by _FORKSERVER_PRELOAD (add its module to
    # _WORKER_TARGETS below) or each of its workers pays that import again.
    _RUN_ISOLATED_CALLS = {
        "_parse_response",
        "parse_response",
        "json.loads",
        "parse_and_format_feed",
        "inspect_html_preflight",
        "js_render.collect_shell_evidence",
        "_truncate_json_worker",
        "parse_teamtailor_rss",
    }
    _WORKER_TARGETS = (
        ("fetchaller.content.html", "_html_worker_from_pipe"),
        ("fetchaller.content._isolated", "_worker_from_pipe"),
        ("fetchaller.content.pdf", "_pdf_worker_from_pipe"),
        ("fetchaller.content.html_preflight", "inspect_html_preflight"),
        ("fetchaller.content.js_render", "collect_shell_evidence"),
        ("fetchaller.content.forums", "parse_and_format_feed"),
        ("fetchaller.content.teamtailor", "parse_teamtailor_rss"),
        ("fetchaller.search.google", "parse_response"),
        ("fetchaller.search.ddg", "_parse_response"),
        ("fetchaller.search.brave", "_parse_response"),
        ("fetchaller.tools.fetch", "_truncate_json_worker"),
    )

    def test_every_run_isolated_target_is_known(self):
        import pathlib
        import re

        source_root = pathlib.Path(__file__).resolve().parents[1] / "src" / "fetchaller"
        called = set()
        for path in source_root.rglob("*.py"):
            called.update(re.findall(r"\brun_isolated\(\s*([\w.]+)\s*,", path.read_text(encoding="utf-8")))
        assert called == self._RUN_ISOLATED_CALLS

    def test_preload_imports_every_worker_target_and_starts_no_threads(self):
        import importlib
        import json
        import subprocess
        import sys

        from fetchaller.content.html import _FORKSERVER_PRELOAD

        for module, name in self._WORKER_TARGETS:
            assert callable(getattr(importlib.import_module(module), name))
        script = (
            "import json, sys, threading\n"
            f"for module in {list(_FORKSERVER_PRELOAD)!r}:\n"
            "    __import__(module)\n"
            "print(json.dumps({'modules': sorted(sys.modules), 'threads': threading.active_count()}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=True
        )
        state = json.loads(completed.stdout.strip().splitlines()[-1])
        loaded = set(state["modules"])
        missing = [module for module, _name in self._WORKER_TARGETS if module not in loaded]
        assert missing == []
        # Forking a process that has other threads can deadlock its children.
        assert state["threads"] == 1
        # The preload is the parser layer, not the MCP server.
        assert "fetchaller.server" not in loaded

    def test_linux_forkserver_context_sets_the_preload(self, monkeypatch):
        import multiprocessing.forkserver
        import sys

        from fetchaller.content import html

        if "forkserver" not in multiprocessing.get_all_start_methods():
            pytest.skip("no forkserver start method on this platform")
        recorded = []
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(multiprocessing.forkserver, "set_forkserver_preload", recorded.append)
        context = html._process_context()
        assert context.get_start_method() == "forkserver"
        assert recorded == [list(html._FORKSERVER_PRELOAD)]

    def test_pdf_workers_share_the_preloaded_context(self):
        from fetchaller.content import html, pdf

        assert pdf._process_context is html._process_context

    @pytest.mark.asyncio
    async def test_forkserver_worker_starts_with_the_preload_imported(self):
        import sys

        from fetchaller.content.html import _FORKSERVER_PRELOAD, _process_context

        if not sys.platform.startswith("linux") or _process_context().get_start_method() != "forkserver":
            pytest.skip("workers start from a forkserver only on Linux")
        # eval is a builtin, so unpickling it imports nothing in the worker:
        # the module can only be there because the forkserver preloaded it.
        expression = f"{_FORKSERVER_PRELOAD[0]!r} in __import__('sys').modules"
        assert await run_isolated(eval, expression, timeout=30) is True
