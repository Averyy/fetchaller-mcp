"""PDF text extraction with pdfplumber."""

import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO

import pdfplumber

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

    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            page_count = len(pdf.pages)
            text_parts = []

            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

            text = "\n\n".join(text_parts)

            # Check if PDF is empty/scanned
            if not text or not text.strip():
                return PdfResult(
                    text="",
                    page_count=page_count,
                    is_empty=True,
                )

            return PdfResult(
                text=text,
                page_count=page_count,
            )

    except Exception as e:
        error_msg = str(e).lower()

        # Password-protected PDF
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


async def extract_pdf(
    content: bytes,
    config: Config | None = None,
) -> PdfResult:
    """
    Extract text from a PDF file.

    Args:
        content: PDF file content as bytes
        config: Optional configuration (uses defaults if not provided)

    Returns:
        PdfResult with extracted text or error
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
