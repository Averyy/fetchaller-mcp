"""PDF to markdown extraction via pymupdf4llm."""

import asyncio
import importlib
import math
import multiprocessing
import re
import sys
import threading
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from multiprocessing.connection import Connection

import pymupdf

# pymupdf4llm currently emits its optional-layout advisory on stdout during
# import. In stdio MCP mode every stdout line is JSON-RPC, including stdout
# inherited by spawned parser workers, so that notice corrupts the protocol.
# Suppress this optional-package advertisement while keeping the wire and
# production logs byte-clean.
with redirect_stdout(StringIO()):
    pymupdf4llm = importlib.import_module("pymupdf4llm")

from ..config import Config
from ._slots import SlotHandle
from .html import _worker_address_space_limit


@dataclass
class PdfResult:
    """Result from PDF extraction."""

    text: str
    page_count: int
    is_empty: bool = False
    error: str | None = None


# Parsing untrusted PDFs in a thread cannot be stopped: cancelling the asyncio
# Future only abandons the thread while MuPDF keeps running. A small per-loop
# process limit plus one disposable process per document gives timeouts real
# cancellation semantics and prevents a run of hostile documents from
# exhausting the server.
_MAX_CONCURRENT_PDF_PROCESSES = 1
_MAX_PDF_INPUT_BYTES = 50 * 1024 * 1024
_MAX_PDF_PAGES = 2_000
_MAX_PDF_OUTPUT_CHARS = 4 * 1024 * 1024
_PDF_PAGE_BATCH_SIZE = 25
_MAX_PROCESSING_TIMEOUT = 120.0
_PROCESS_POLL_INTERVAL = 0.01
_PDF_SLOT_ATTRIBUTE = "_fetchaller_pdf_process_slots"


def _pdf_slots() -> asyncio.Semaphore:
    """Return parser capacity bound to the current event loop."""
    loop = asyncio.get_running_loop()
    slots = getattr(loop, _PDF_SLOT_ATTRIBUTE, None)
    if slots is None:
        slots = asyncio.Semaphore(_MAX_CONCURRENT_PDF_PROCESSES)
        setattr(loop, _PDF_SLOT_ATTRIBUTE, slots)
    return slots


def _process_context() -> multiprocessing.context.BaseContext:
    """Choose a safe context while keeping documented ``python -c`` use working."""
    methods = multiprocessing.get_all_start_methods()
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if (not main_file or str(main_file).startswith("<")) and "fork" in methods:
        # spawn/forkserver cannot re-import a ``-c`` or stdin main module.
        # This fallback is limited to those interactive development entrypoints;
        # the MCP server and normal scripts use an isolated clean-start method.
        return multiprocessing.get_context("fork")
    if sys.platform.startswith("linux") and "forkserver" in methods:
        return multiprocessing.get_context("forkserver")
    return multiprocessing.get_context("spawn")


def _apply_worker_limits(timeout: float) -> None:
    """Bound a parser worker's memory and CPU on platforms that support it."""
    if not sys.platform.startswith("linux"):
        return

    try:
        import resource

        address_soft, address_hard = resource.getrlimit(resource.RLIMIT_AS)
        address_limit = _worker_address_space_limit()
        if address_soft != resource.RLIM_INFINITY:
            address_limit = min(address_limit, address_soft)
        if address_hard != resource.RLIM_INFINITY:
            address_limit = min(address_limit, address_hard)
        resource.setrlimit(resource.RLIMIT_AS, (address_limit, address_hard))

        cpu_soft, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
        cpu_limit = max(1, min(120, math.ceil(timeout) + 1))
        if cpu_soft != resource.RLIM_INFINITY:
            cpu_limit = min(cpu_limit, cpu_soft)
        if cpu_hard != resource.RLIM_INFINITY:
            cpu_limit = min(cpu_limit, cpu_hard)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_hard))
    except (OSError, ValueError):
        # The parent still enforces a hard wall-clock timeout and kills this
        # disposable process. RLIMIT is defense in depth for Linux deployments.
        pass


def _stop_process(process: multiprocessing.Process) -> None:
    """Terminate and synchronously reap a disposable parser process."""
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.25)
    if process.is_alive():
        process.kill()
        process.join()
    else:
        process.join(timeout=0)


def _bounded_text(text: str, max_chars: int, marker: str) -> str:
    """Bound worker output while preserving an explicit truncation marker."""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return marker[:max_chars]
    return text[: max_chars - len(marker)].rstrip() + marker


def _pdf_size_error(size: int, max_size: int) -> PdfResult:
    """Build a precise size-limit error for both tiny tests and real PDFs."""
    if size < 1024 * 1024:
        size_label = f"{size} bytes"
    else:
        size_label = f"{size / (1024 * 1024):.1f}MB"
    if max_size < 1024 * 1024:
        max_label = f"{max_size} bytes"
    else:
        max_label = f"{max_size // (1024 * 1024)}MB"
    return PdfResult(
        text="",
        page_count=0,
        error=f"PDF too large: {size_label} (max {max_label})",
    )


def _postprocess_markdown(text: str) -> str:
    """Clean up pymupdf4llm markdown output."""

    # Collapse table rows where merged cells cause duplication.
    # pymupdf4llm repeats the same text across all columns for merged rows.
    def _dedup_table_row(match: re.Match) -> str:
        row = match.group(0)
        cells = row.split("|")
        # Split gives empty strings at start/end from leading/trailing |
        inner = [c.strip() for c in cells[1:-1]]
        if len(inner) < 2:
            return row
        # Skip separator rows (---|---|...)
        if all(re.match(r":?-+:?$", c) for c in inner if c):
            return row
        # All cells identical (full-width merged header): keep first, empty rest
        first = inner[0]
        if first and all(c == first for c in inner[1:]):
            return "|" + first + "|" + "|".join("" for _ in inner[1:]) + "|"
        # First cell differs but cells 2+ are all identical (label + spanning value):
        # keep first two, empty the rest
        if len(inner) > 2 and inner[1]:
            rest = inner[1:]
            if all(c == rest[0] for c in rest[1:]):
                return "|" + inner[0] + "|" + rest[0] + "|" + "|".join("" for _ in rest[1:]) + "|"
        return row

    text = re.sub(r"^\|.+\|$", _dedup_table_row, text, flags=re.MULTILINE)

    # Strip excessive leading whitespace from list items
    text = re.sub(r"^ {4,}(- )", r"\1", text, flags=re.MULTILINE)

    # Collapse dot leaders (TOC fill dots, form fill lines)
    # ". . . . . . . . 14" → "... 14", ".............. 14" → "... 14"
    text = re.sub(r"(?:\. ){3,}\.?", "... ", text)
    text = re.sub(r"\.{4,}", "...", text)

    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    # Strip bare page numbers (standalone digit lines, typically at page boundaries)
    text = re.sub(r"(?:^|\n)\n\d{1,4}\n(?=\n)", "\n", text)

    # Strip Chinese page footers (第 N 页共 M 页 = Page N of M)
    text = re.sub(r"(?:^|\n)第 ?\d+ ?页共 ?\d+ ?页(?=\n|$)", "", text)

    return text.strip() + "\n"


def _extract_pdf_sync(
    content: bytes,
    max_size: int,
    max_pages: int = _MAX_PDF_PAGES,
    max_output_chars: int = _MAX_PDF_OUTPUT_CHARS,
    absolute_max_size: int = _MAX_PDF_INPUT_BYTES,
) -> PdfResult:
    """
    Synchronous PDF extraction (runs inside a disposable worker process).

    Args:
        content: PDF file content
        max_size: Maximum allowed PDF size in bytes
        max_pages: Maximum number of pages accepted
        max_output_chars: Maximum extracted markdown returned to the parent
        absolute_max_size: Non-configurable safety ceiling for input bytes

    Returns:
        PdfResult with text, page count, and any errors
    """
    effective_max_size = max(0, min(max_size, absolute_max_size))
    if len(content) > effective_max_size:
        return _pdf_size_error(len(content), effective_max_size)

    doc = None
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")

        # Password-protected PDF
        if doc.needs_pass:
            return PdfResult(
                text="",
                page_count=0,
                error="PDF is password-protected and cannot be read.",
            )

        page_count = len(doc)
        if page_count > max_pages:
            return PdfResult(
                text="",
                page_count=page_count,
                error=f"PDF has too many pages: {page_count} (max {max_pages}).",
            )

        # pymupdf4llm's non-chunked mode repeatedly concatenates the complete
        # document string. Use its page chunks and a shared header detector so
        # normal output stays identical while large documents avoid quadratic
        # string growth and stop once the useful output bound is reached.
        # Bake forms/annotations before header detection, matching
        # pymupdf4llm.to_markdown()'s ordering for ordinary whole-document use.
        if doc.is_form_pdf or (doc.is_pdf and doc.has_annots()):
            doc.bake()
        header_info = pymupdf4llm.IdentifyHeaders(doc)
        raw_parts: list[str] = []
        raw_size = 0
        truncated = False
        for first_page in range(0, page_count, _PDF_PAGE_BATCH_SIZE):
            pages = list(
                range(
                    first_page,
                    min(first_page + _PDF_PAGE_BATCH_SIZE, page_count),
                )
            )
            chunks = pymupdf4llm.to_markdown(
                doc,
                pages=pages,
                hdr_info=header_info,
                ignore_images=True,
                ignore_graphics=True,
                page_chunks=True,
                show_progress=False,
            )
            for chunk in chunks:
                page_text = chunk.get("text", "")
                remaining = max_output_chars - raw_size
                if len(page_text) > remaining:
                    raw_parts.append(page_text[: max(0, remaining)])
                    truncated = True
                    break
                raw_parts.append(page_text)
                raw_size += len(page_text)
            if truncated:
                break
        raw = "".join(raw_parts)

        # Check if PDF is empty/scanned
        if not raw or not raw.strip():
            return PdfResult(
                text="",
                page_count=page_count,
                is_empty=True,
            )

        text = _postprocess_markdown(raw)
        if truncated or len(text) > max_output_chars:
            text = _bounded_text(
                text,
                max_output_chars,
                "\n\n[PDF extraction truncated at the safe processing limit]\n",
            )

        return PdfResult(
            text=text,
            page_count=page_count,
        )

    except Exception as e:
        error_msg = str(e).lower()

        # Password-protected PDF (shouldn't reach here, but just in case)
        if "password" in error_msg or "encrypted" in error_msg:
            return PdfResult(
                text="",
                page_count=0,
                error="PDF is password-protected and cannot be read.",
            )

        # Generic error
        return PdfResult(
            text="",
            page_count=0,
            error="PDF parsing failed. The file may be corrupted, invalid, or use unsupported features.",
        )

    finally:
        if doc:
            doc.close()


def _pdf_worker(
    send_connection: Connection,
    content: bytes,
    max_size: int,
    max_pages: int,
    max_output_chars: int,
    timeout: float,
) -> None:
    """Run one PDF parse in an OS-isolated worker."""
    try:
        _apply_worker_limits(timeout)
        result = _extract_pdf_sync(
            content,
            max_size,
            max_pages=max_pages,
            max_output_chars=max_output_chars,
        )
        send_connection.send(("result", result))
    except BaseException:
        # Never serialize exception details from a native parser boundary.
        try:
            send_connection.send(("error", None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send_connection.close()


def _pdf_worker_from_pipe(
    send_connection: Connection,
    task_connection: Connection,
) -> None:
    """Receive PDF bytes after clean worker startup."""

    try:
        task = task_connection.recv()
    except (EOFError, OSError):
        send_connection.close()
        return
    finally:
        task_connection.close()
    _pdf_worker(send_connection, *task)


def _close_and_stop_pdf_process(
    process: multiprocessing.Process,
    *connections: Connection,
) -> None:
    for connection in connections:
        connection.close()
    _stop_process(process)


def _defer_pdf_process_cleanup(
    process: multiprocessing.Process,
    handle: SlotHandle,
    *connections: Connection,
) -> None:
    """Reap after cancellation, then hand the parser slot back.

    Without the handle the caller's ``finally`` released the slot while this
    thread was still reaping, so a cancellation storm could run more children
    than the cap allows.
    """

    def _close_stop_and_release() -> None:
        try:
            _close_and_stop_pdf_process(process, *connections)
        finally:
            handle.release_from_thread()

    cleanup_thread = threading.Thread(
        target=_close_stop_and_release,
        name="fetchaller-pdf-parser-cleanup",
        daemon=True,
    )
    handle.transfer()
    try:
        cleanup_thread.start()
    except RuntimeError:
        handle.untransfer()
        _close_and_stop_pdf_process(process, *connections)


def _start_pdf_process(
    process: multiprocessing.Process,
    connections: tuple[Connection, ...],
    state: dict[str, bool],
    state_lock: threading.Lock,
) -> bool:
    try:
        process.start()
    except BaseException:
        _close_and_stop_pdf_process(process, *connections)
        raise
    cleanup = False
    with state_lock:
        state["started"] = True
        if state["cancelled"] and not state["cleanup_claimed"]:
            state["cleanup_claimed"] = True
            cleanup = True
    if cleanup:
        _close_and_stop_pdf_process(process, *connections)
        return False
    return True


def _stop_pdf_process_and_release(process, handle: SlotHandle) -> None:
    """Reap ``process``, then hand its slot back. Runs on a cleanup thread."""
    try:
        _stop_process(process)
    finally:
        handle.release_from_thread()


async def _extract_pdf_in_process(
    content: bytes,
    max_size: int,
    timeout: float,
    handle: SlotHandle,
) -> PdfResult:
    """Start, monitor, and always reap one disposable parser process."""
    context = _process_context()
    try:
        receive_connection, send_connection = context.Pipe(duplex=False)
        task_receive_connection, task_send_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=_pdf_worker_from_pipe,
            args=(
                send_connection,
                task_receive_connection,
            ),
            name="fetchaller-pdf-parser",
            daemon=True,
        )
    except (OSError, RuntimeError):
        return PdfResult(
            text="",
            page_count=0,
            error="PDF parsing failed because an isolated parser could not be started.",
        )

    state = {
        "started": False,
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
            _start_pdf_process,
            process,
            connections,
            state,
            state_lock,
        )
    except asyncio.CancelledError:
        cleanup = False
        with state_lock:
            state["cancelled"] = True
            if state["started"] and not state["cleanup_claimed"]:
                state["cleanup_claimed"] = True
                cleanup = True
        if cleanup:
            _defer_pdf_process_cleanup(process, handle, *connections)
        raise
    except (AssertionError, OSError, RuntimeError):
        send_connection.close()
        receive_connection.close()
        task_receive_connection.close()
        task_send_connection.close()
        await asyncio.to_thread(_stop_process, process)
        return PdfResult(
            text="",
            page_count=0,
            error="PDF parsing failed because an isolated parser could not be started.",
        )
    if not started:
        raise asyncio.CancelledError
    send_connection.close()
    task_receive_connection.close()

    try:
        await asyncio.to_thread(
            task_send_connection.send,
            (
                content,
                max_size,
                _MAX_PDF_PAGES,
                _MAX_PDF_OUTPUT_CHARS,
                timeout,
            ),
        )
        task_send_connection.close()
        while True:
            if receive_connection.poll():
                try:
                    kind, payload = receive_connection.recv()
                except EOFError:
                    break
                if kind == "result" and isinstance(payload, PdfResult):
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
            # Hold the slot until the child is actually reaped; releasing here
            # let a cancellation storm run more parsers than the cap allows.
            # Transfer only AFTER the thread is running. Transferring first
            # made a failed Thread.start() permanently leak the permit: the
            # owner's release() had already become a no-op and no thread was
            # alive to release it.
            cleanup_thread = threading.Thread(
                target=_stop_pdf_process_and_release,
                args=(process, handle),
                name="fetchaller-pdf-parser-cleanup",
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

    return PdfResult(
        text="",
        page_count=0,
        error="PDF parsing failed. The parser process exited unexpectedly.",
    )


async def extract_pdf(
    content: bytes,
    config: Config | None = None,
) -> PdfResult:
    """
    Extract markdown from a PDF file.

    Args:
        content: PDF file content as bytes
        config: Optional configuration (uses defaults if not provided)

    Returns:
        PdfResult with extracted markdown or error
    """
    max_size = config.max_pdf_size if config else 50 * 1024 * 1024
    timeout = config.pdf_processing_timeout if config else 30
    effective_max_size = max(0, min(max_size, _MAX_PDF_INPUT_BYTES))

    if len(content) > effective_max_size:
        return _pdf_size_error(len(content), effective_max_size)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = 0
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        return PdfResult(
            text="",
            page_count=0,
            error="PDF processing timeout must be greater than zero.",
        )
    timeout_value = min(timeout_value, _MAX_PROCESSING_TIMEOUT)

    try:
        async with asyncio.timeout(timeout_value):
            slots = _pdf_slots()
            await slots.acquire()
            handle = SlotHandle(slots, asyncio.get_running_loop())
            try:
                return await _extract_pdf_in_process(
                    content,
                    effective_max_size,
                    timeout_value,
                    handle,
                )
            finally:
                # No-op when a cleanup thread has taken ownership; that thread
                # releases only after the child is actually gone.
                handle.release()

    except TimeoutError:
        return PdfResult(
            text="",
            page_count=0,
            error=(f"PDF parsing timed out after {timeout_value:g}s. The PDF may be too complex or large to process."),
        )
