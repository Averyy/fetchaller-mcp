"""PDF to markdown extraction via pymupdf4llm."""

import asyncio
import atexit
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pymupdf
import pymupdf4llm

from ..config import Config


@dataclass
class PdfResult:
    """Result from PDF extraction."""

    text: str
    page_count: int
    is_empty: bool = False
    error: str | None = None


# Thread pool for blocking PDF operations
_executor = ThreadPoolExecutor(max_workers=2)

# Register cleanup on exit to prevent thread leaks
atexit.register(_executor.shutdown, wait=False)


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
) -> PdfResult:
    """
    Synchronous PDF extraction (runs in thread pool).

    Note: Timeout is enforced at the async level via asyncio.wait_for,
    not within this sync function.

    Args:
        content: PDF file content
        max_size: Maximum allowed PDF size in bytes

    Returns:
        PdfResult with text, page count, and any errors
    """
    # Check size
    if len(content) > max_size:
        size_mb = len(content) / (1024 * 1024)
        return PdfResult(
            text="",
            page_count=0,
            error=f"PDF too large: {size_mb:.1f}MB (max {max_size // (1024 * 1024)}MB)",
        )

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
        raw = pymupdf4llm.to_markdown(
            doc, ignore_images=True, ignore_graphics=True, show_progress=False
        )

        # Check if PDF is empty/scanned
        if not raw or not raw.strip():
            return PdfResult(
                text="",
                page_count=page_count,
                is_empty=True,
            )

        text = _postprocess_markdown(raw)

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

    loop = asyncio.get_running_loop()

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, _extract_pdf_sync, content, max_size),
            timeout=timeout,
        )
        return result

    except TimeoutError:
        return PdfResult(
            text="",
            page_count=0,
            error=f"PDF parsing timed out after {timeout}s. The PDF may be too complex or large to process.",
        )
