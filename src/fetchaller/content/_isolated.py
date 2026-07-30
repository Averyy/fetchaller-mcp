"""Reusable disposable-process boundary for parsing untrusted response bodies."""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection

from ._slots import SlotHandle
from .html import _apply_worker_limits, _process_context, _stop_process

_MAX_CONCURRENT_PROCESSES = 2
_MAX_PROCESSING_TIMEOUT = 120.0
_PROCESS_POLL_INTERVAL = 0.01
_SLOT_ATTRIBUTE = "_fetchaller_isolated_process_slots"


class IsolatedProcessingError(RuntimeError):
    """An isolated operation could not safely produce a result."""


def stop_process_and_release(process, handle: SlotHandle) -> None:
    """Reap ``process``, then hand its slot back. Runs on a cleanup thread."""
    try:
        _stop_process(process)
    finally:
        handle.release_from_thread()


def _slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = getattr(loop, _SLOT_ATTRIBUTE, None)
    if slots is None:
        slots = asyncio.Semaphore(_MAX_CONCURRENT_PROCESSES)
        setattr(loop, _SLOT_ATTRIBUTE, slots)
    return slots


def _worker_from_pipe(
    send_connection: Connection,
    task_connection: Connection,
) -> None:
    try:
        task = task_connection.recv()
    except (EOFError, OSError):
        send_connection.close()
        return
    finally:
        task_connection.close()

    function, args, timeout = task
    try:
        _apply_worker_limits(timeout)
        result = function(*args)
        send_connection.send(("result", result))
    except BaseException:
        # Parser/native exception details are deliberately not serialized across
        # the trust boundary. Callers receive one stable fail-closed error.
        try:
            send_connection.send(("error", None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send_connection.close()


def _close_and_stop(
    process: multiprocessing.Process,
    *connections: Connection,
) -> None:
    for connection in connections:
        connection.close()
    _stop_process(process)


def _defer_cleanup(
    process: multiprocessing.Process,
    handle: SlotHandle,
    *connections: Connection,
) -> None:
    def _close_stop_and_release() -> None:
        try:
            _close_and_stop(process, *connections)
        finally:
            handle.release_from_thread()

    cleanup_thread = threading.Thread(
        target=_close_stop_and_release,
        name="fetchaller-isolated-parser-cleanup",
        daemon=True,
    )
    try:
        cleanup_thread.start()
    except RuntimeError:
        # The owning task has not left its finally block yet. Take ownership
        # back before falling back to synchronous cleanup so its release()
        # returns the slot even when no cleanup thread could be created.
        handle.untransfer()
        _close_and_stop(process, *connections)


def _start_process(
    process: multiprocessing.Process,
    connections: tuple[Connection, ...],
    state: dict[str, bool],
    state_lock: threading.Lock,
    handle: SlotHandle,
) -> bool:
    try:
        process.start()
    except BaseException:
        release_slot = False
        try:
            _close_and_stop(process, *connections)
        finally:
            with state_lock:
                state["finished"] = True
                if state["cancelled"] and not state["cleanup_claimed"]:
                    state["cleanup_claimed"] = True
                    release_slot = True
            if release_slot:
                handle.release_from_thread()
        raise
    cleanup = False
    with state_lock:
        state["started"] = True
        state["finished"] = True
        if state["cancelled"] and not state["cleanup_claimed"]:
            state["cleanup_claimed"] = True
            cleanup = True
    if cleanup:
        try:
            _close_and_stop(process, *connections)
        finally:
            handle.release_from_thread()
        return False
    return True


async def _run_started_process[T](
    function: Callable[..., T],
    args: tuple[object, ...],
    timeout: float,
    handle: SlotHandle,
) -> T:
    context = _process_context()
    try:
        receive_connection, send_connection = context.Pipe(duplex=False)
        task_receive_connection, task_send_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=_worker_from_pipe,
            args=(send_connection, task_receive_connection),
            name="fetchaller-isolated-parser",
            daemon=True,
        )
    except (OSError, RuntimeError):
        raise IsolatedProcessingError("An isolated parser process could not be created.") from None

    state = {
        "started": False,
        "finished": False,
        "cancelled": False,
        "cleanup_claimed": False,
    }
    state_lock = threading.Lock()
    connections = (
        receive_connection,
        send_connection,
        task_receive_connection,
        task_send_connection,
    )
    try:
        started = await asyncio.to_thread(
            _start_process,
            process,
            connections,
            state,
            state_lock,
            handle,
        )
    except asyncio.CancelledError:
        cleanup = False
        release_slot = False
        # asyncio cancellation does not stop the thread running process.start().
        # Transfer the slot before publishing cancellation so either that worker
        # or the cleanup thread retains capacity until the child is reaped.
        handle.transfer()
        with state_lock:
            state["cancelled"] = True
            if not state["cleanup_claimed"]:
                if state["started"]:
                    state["cleanup_claimed"] = True
                    cleanup = True
                elif state["finished"]:
                    # process.start() failed and already cleaned up before the
                    # cancellation was delivered; only the slot remains.
                    state["cleanup_claimed"] = True
                    release_slot = True
        if cleanup:
            _defer_cleanup(process, handle, *connections)
        elif release_slot:
            handle.release_from_thread()
        raise
    except (AssertionError, OSError, RuntimeError):
        for connection in connections:
            connection.close()
        await asyncio.to_thread(_stop_process, process)
        raise IsolatedProcessingError("An isolated parser process could not be started.") from None
    if not started:
        raise asyncio.CancelledError
    send_connection.close()
    task_receive_connection.close()

    try:
        await asyncio.to_thread(
            task_send_connection.send,
            (function, args, timeout),
        )
        task_send_connection.close()
        while True:
            if receive_connection.poll():
                try:
                    kind, payload = receive_connection.recv()
                except EOFError:
                    break
                if kind == "result":
                    return payload
                break
            if not process.is_alive():
                break
            await asyncio.sleep(_PROCESS_POLL_INTERVAL)
    finally:
        receive_connection.close()
        task_send_connection.close()
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # Transfer only AFTER the thread is running. Transferring first
            # made a failed Thread.start() permanently leak the permit: the
            # owner's release() had already become a no-op and no thread was
            # alive to release it.
            cleanup_thread = threading.Thread(
                target=stop_process_and_release,
                args=(process, handle),
                name="fetchaller-isolated-parser-cleanup",
                daemon=True,
            )
            handle.transfer()
            try:
                cleanup_thread.start()
            except RuntimeError:
                handle.untransfer()
                await asyncio.to_thread(_stop_process, process)
        else:
            await asyncio.to_thread(_stop_process, process)

    raise IsolatedProcessingError("An isolated parser process exited without a valid result.")


async def run_isolated[T](
    function: Callable[..., T],
    *args: object,
    timeout: float = 20.0,
) -> T:
    """Run a picklable top-level parser with a hard process timeout."""

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = 0.0
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        raise IsolatedProcessingError("Isolated processing timeout must be greater than zero.")
    timeout_value = min(timeout_value, _MAX_PROCESSING_TIMEOUT)

    try:
        async with asyncio.timeout(timeout_value):
            slots = _slots()
            await slots.acquire()
            handle = SlotHandle(slots, asyncio.get_running_loop())
            try:
                return await _run_started_process(
                    function,
                    args,
                    timeout_value,
                    handle,
                )
            finally:
                # No-op when a cleanup thread has taken ownership; that thread
                # releases only after the child is actually gone.
                handle.release()
    except TimeoutError:
        raise IsolatedProcessingError(f"Isolated processing timed out after {timeout_value:g}s.") from None
