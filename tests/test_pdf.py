"""Tests for PDF extraction via pymupdf4llm."""

import asyncio
import multiprocessing
import subprocess
import sys
import time
from unittest.mock import patch

import pymupdf
import pymupdf4llm
import pytest

from fetchaller.config import Config
from fetchaller.content.pdf import _extract_pdf_sync, _postprocess_markdown, extract_pdf


async def _wait_for_no_pdf_children() -> None:
    deadline = time.monotonic() + 2
    while (
        any(
            child.name == "fetchaller-pdf-parser"
            for child in multiprocessing.active_children()
        )
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(0.01)


def test_pdf_module_import_never_contaminates_stdio_mcp_stdout():
    result = subprocess.run(
        [sys.executable, "-c", "import fetchaller.content.pdf"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "pymupdf_layout" not in result.stderr


def _make_pdf(texts: list[str]) -> bytes:
    """Create a PDF with one page per text string."""
    doc = pymupdf.open()
    for text in texts:
        page = doc.new_page()
        tw = pymupdf.TextWriter(page.rect)
        tw.append((72, 100), text, fontsize=12)
        tw.write_text(page)
    content = doc.tobytes()
    doc.close()
    return content


def _make_encrypted_pdf() -> bytes:
    """Create an AES-256 encrypted PDF."""
    doc = pymupdf.open()
    page = doc.new_page()
    tw = pymupdf.TextWriter(page.rect)
    tw.append((72, 100), "Secret content", fontsize=12)
    tw.write_text(page)
    content = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="test123")
    doc.close()
    return content


def _make_blank_pdf(num_pages: int = 1) -> bytes:
    """Create a PDF with blank pages (no text)."""
    doc = pymupdf.open()
    for _ in range(num_pages):
        doc.new_page()
    content = doc.tobytes()
    doc.close()
    return content


class TestExtractPdfSync:
    def test_basic_text_extraction(self):
        pdf = _make_pdf(["Hello from pymupdf4llm"])
        result = _extract_pdf_sync(pdf, 50 * 1024 * 1024)
        assert result.page_count == 1
        assert "Hello from pymupdf4llm" in result.text
        assert not result.is_empty
        assert result.error is None

    def test_multi_page(self):
        pdf = _make_pdf(["Page one content", "Page two content", "Page three content"])
        result = _extract_pdf_sync(pdf, 50 * 1024 * 1024)
        assert result.page_count == 3
        assert "Page one content" in result.text
        assert "Page two content" in result.text
        assert "Page three content" in result.text

    def test_chunked_extraction_preserves_normal_output(self):
        texts = [f"Page {page} content" for page in range(30)]
        pdf = _make_pdf(texts)
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            previous_output = pymupdf4llm.to_markdown(
                doc,
                ignore_images=True,
                ignore_graphics=True,
                show_progress=False,
            )

        result = _extract_pdf_sync(pdf, 50 * 1024 * 1024)
        assert result.text == _postprocess_markdown(previous_output)
        assert "Page 29 content" in result.text

    def test_annotated_pdf_preserves_whole_document_output(self):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Annotated document content")
        page.add_text_annot((120, 120), "review note")
        pdf = doc.tobytes()
        doc.close()

        with pymupdf.open(stream=pdf, filetype="pdf") as expected_doc:
            previous_output = pymupdf4llm.to_markdown(
                expected_doc,
                ignore_images=True,
                ignore_graphics=True,
                show_progress=False,
            )

        result = _extract_pdf_sync(pdf, 50 * 1024 * 1024)
        assert result.text == _postprocess_markdown(previous_output)
        assert "Annotated document content" in result.text

    def test_blank_pdf_is_empty(self):
        pdf = _make_blank_pdf(2)
        result = _extract_pdf_sync(pdf, 50 * 1024 * 1024)
        assert result.is_empty
        assert result.page_count == 2
        assert result.text == ""

    def test_encrypted_pdf_error(self):
        pdf = _make_encrypted_pdf()
        result = _extract_pdf_sync(pdf, 50 * 1024 * 1024)
        assert result.error is not None
        assert "password" in result.error.lower()
        assert result.page_count == 0

    def test_corrupted_input_error(self):
        result = _extract_pdf_sync(b"this is not a pdf", 50 * 1024 * 1024)
        assert result.error is not None
        assert "parsing failed" in result.error.lower()

    def test_size_limit_exceeded(self):
        pdf = _make_pdf(["Small content"])
        # Set max_size smaller than the PDF
        result = _extract_pdf_sync(pdf, 10)
        assert result.error is not None
        assert "too large" in result.error.lower()
        assert result.page_count == 0

    def test_absolute_size_ceiling_cannot_be_disabled_by_configured_limit(self):
        result = _extract_pdf_sync(
            b"x" * 11,
            max_size=1_000,
            absolute_max_size=10,
        )
        assert result.error == "PDF too large: 11 bytes (max 10 bytes)"
        assert result.page_count == 0

    def test_page_count_bound_rejects_before_page_extraction(self):
        pdf = _make_blank_pdf(3)
        result = _extract_pdf_sync(
            pdf,
            50 * 1024 * 1024,
            max_pages=2,
        )
        assert result.error == "PDF has too many pages: 3 (max 2)."
        assert result.page_count == 3

    def test_output_bound_is_explicit(self):
        pdf = _make_pdf([f"Page {page}: " + "content " * 50 for page in range(10)])
        result = _extract_pdf_sync(
            pdf,
            50 * 1024 * 1024,
            max_output_chars=160,
        )
        assert len(result.text) <= 160
        assert result.text.endswith(
            "[PDF extraction truncated at the safe processing limit]\n"
        )

    def test_output_is_markdown(self):
        """pymupdf4llm should produce markdown with headers for larger font sizes."""
        doc = pymupdf.open()
        page = doc.new_page()
        tw = pymupdf.TextWriter(page.rect)
        tw.append((72, 80), "Title Text", fontsize=24)
        tw.write_text(page)
        tw2 = pymupdf.TextWriter(page.rect)
        tw2.append((72, 150), "Body paragraph text here.", fontsize=12)
        tw2.write_text(page)
        content = doc.tobytes()
        doc.close()

        result = _extract_pdf_sync(content, 50 * 1024 * 1024)
        assert "Title Text" in result.text
        assert "Body paragraph text here." in result.text
        # pymupdf4llm detects larger fonts as headers
        assert "#" in result.text


class TestPostprocessMarkdown:
    def test_collapses_all_identical_cells(self):
        """Full-width merged row: all cells identical → keep first, empty rest."""
        row = "|**Transmitters**|**Transmitters**|**Transmitters**|"
        result = _postprocess_markdown(row)
        assert result.strip() == "|**Transmitters**|||"

    def test_collapses_spanning_value_cells(self):
        """Label + spanning value: first cell differs, cells 2+ identical → keep first two, empty rest."""
        row = "|**Exports**|UART text|UART text|UART text|"
        result = _postprocess_markdown(row)
        assert result.strip() == "|**Exports**|UART text|||"

    def test_preserves_separator_row(self):
        row = "|---|---|---|---|"
        result = _postprocess_markdown(row)
        assert result.strip() == "|---|---|---|---|"

    def test_preserves_normal_data_row(self):
        row = "|**Voltage**|VCC|3.5|3.7|5|V|"
        result = _postprocess_markdown(row)
        assert result.strip() == "|**Voltage**|VCC|3.5|3.7|5|V|"

    def test_collapses_excessive_blank_lines(self):
        text = "line1\n\n\n\n\n\nline2"
        result = _postprocess_markdown(text)
        assert result == "line1\n\n\nline2\n"

    def test_strips_page_numbers(self):
        text = "end of page content\n\n42\n\nstart of next page"
        result = _postprocess_markdown(text)
        assert "42" not in result
        assert "end of page content" in result
        assert "start of next page" in result

    def test_collapses_spaced_dot_leaders(self):
        """TOC dot leaders with spaces: '. . . . . . 14' → '... 14'."""
        text = "Section Name . . . . . . . . . . . . . . . 14"
        result = _postprocess_markdown(text)
        assert ". . . ." not in result
        assert "Section Name" in result
        assert "14" in result

    def test_collapses_consecutive_dot_leaders(self):
        """TOC dot leaders without spaces: '.............. 14' → '... 14'."""
        text = "Section Name.............. 14"
        result = _postprocess_markdown(text)
        assert "......" not in result
        assert "Section Name" in result
        assert "14" in result

    def test_strips_leading_whitespace_from_list_items(self):
        text = "                       - Item with too much indent"
        result = _postprocess_markdown(text)
        assert result.strip() == "- Item with too much indent"

    def test_preserves_nested_list_indentation(self):
        """3-space indent (nested list) should be preserved."""
        text = "- Parent item\n   - Nested item"
        result = _postprocess_markdown(text)
        assert "   - Nested item" in result

    def test_strips_chinese_page_footers(self):
        text = "content here\n\n第 1 页共 21 页\n\nmore content"
        result = _postprocess_markdown(text)
        assert "第" not in result
        assert "content here" in result
        assert "more content" in result


class TestExtractPdfAsync:
    async def test_async_returns_same_as_sync(self):
        pdf = _make_pdf(["Async test content"])
        result = await extract_pdf(pdf)
        assert result.page_count == 1
        assert "Async test content" in result.text
        assert result.error is None

    async def test_config_max_size_respected(self):
        pdf = _make_pdf(["Content"])
        config = Config(max_pdf_size=10)
        result = await extract_pdf(pdf, config)
        assert result.error is not None
        assert "too large" in result.error.lower()

    async def test_nonpositive_timeout_is_rejected_before_worker_spawn(self):
        result = await extract_pdf(
            _make_pdf(["Content"]),
            Config(pdf_processing_timeout=0),
        )
        assert result.error == "PDF processing timeout must be greater than zero."
        assert not any(
            child.name == "fetchaller-pdf-parser"
            for child in multiprocessing.active_children()
        )

    async def test_timeout_releases_capacity_and_reaps_worker(self):
        config = Config(pdf_processing_timeout=0.001)  # 1ms timeout
        pdf = _make_pdf(["Content"])
        result = await extract_pdf(pdf, config)
        assert result.error is not None
        assert "timed out" in result.error.lower()
        await _wait_for_no_pdf_children()
        assert not any(
            child.name == "fetchaller-pdf-parser"
            for child in multiprocessing.active_children()
        )

        subsequent = await extract_pdf(
            pdf,
            Config(pdf_processing_timeout=10),
        )
        assert subsequent.error is None
        assert "Content" in subsequent.text

    async def test_cancellation_reaps_worker_and_releases_capacity(self):
        task = asyncio.create_task(
            extract_pdf(
                _make_blank_pdf(100),
                Config(pdf_processing_timeout=30),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _wait_for_no_pdf_children()
        assert not any(
            child.name == "fetchaller-pdf-parser"
            for child in multiprocessing.active_children()
        )

        subsequent = await extract_pdf(
            _make_pdf(["After cancellation"]),
            Config(pdf_processing_timeout=10),
        )
        assert subsequent.error is None
        assert "After cancellation" in subsequent.text

    async def test_slow_process_start_does_not_block_timeout_or_event_loop(self):
        loop_progressed = asyncio.Event()

        async def mark_progress():
            await asyncio.sleep(0.005)
            loop_progressed.set()

        def slow_start(*_args):
            time.sleep(0.2)
            return True

        marker = asyncio.create_task(mark_progress())
        started = time.monotonic()
        with patch(
            "fetchaller.content.pdf._start_pdf_process",
            side_effect=slow_start,
        ):
            result = await extract_pdf(
                _make_pdf(["Slow start"]),
                Config(pdf_processing_timeout=0.02),
            )
        elapsed = time.monotonic() - started
        await marker

        assert "timed out" in (result.error or "").lower()
        assert elapsed < 0.1
        assert loop_progressed.is_set()

    async def test_encrypted_via_async(self):
        pdf = _make_encrypted_pdf()
        result = await extract_pdf(pdf)
        assert result.error is not None
        assert "password" in result.error.lower()
