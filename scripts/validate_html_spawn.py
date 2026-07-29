"""Live spawned-parser timeout probe for the production container runtime."""

from __future__ import annotations

import asyncio
import multiprocessing
import time

from fetchaller.content.html import (
    HtmlProcessingError,
    html_to_markdown,
    warm_html_process_runtime,
)


async def _run() -> None:
    await warm_html_process_runtime()
    html = "<html><body>" + ("<i>x</i>" * 175_000) + "</body></html>"
    loop_progressed = asyncio.Event()

    async def mark_progress() -> None:
        await asyncio.sleep(0.1)
        loop_progressed.set()

    marker = asyncio.create_task(mark_progress())
    started = time.monotonic()
    try:
        await html_to_markdown(html, timeout=1)
    except HtmlProcessingError as exc:
        if "timed out" not in str(exc):
            raise
    else:
        raise RuntimeError("spawn probe unexpectedly completed before timeout")
    response_elapsed = time.monotonic() - started
    await marker
    if not loop_progressed.is_set():
        raise RuntimeError("spawn startup blocked the event loop")
    if response_elapsed > 1.5:
        raise RuntimeError(
            f"spawn timeout exceeded wall bound: {response_elapsed:.3f}s"
        )

    cleanup_deadline = time.monotonic() + 5
    while time.monotonic() < cleanup_deadline:
        children = [
            child
            for child in multiprocessing.active_children()
            if child.name == "fetchaller-html-parser"
        ]
        if not children:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("spawn timeout left a parser child running")
    print(
        "PASS spawned HTML parser timeout "
        f"response={response_elapsed:.3f}s children=0"
    )


if __name__ == "__main__":
    asyncio.run(_run())
