"""Tests for PDF extraction via pymupdf4llm."""

from unittest.mock import patch

import pymupdf

from fetchaller.config import Config
from fetchaller.content.pdf import _extract_pdf_sync, _postprocess_markdown, extract_pdf


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

    async def test_timeout_error(self):
        config = Config(pdf_processing_timeout=0.001)  # 1ms timeout

        # Create a valid PDF so it enters processing
        pdf = _make_pdf(["Content"])

        # Patch _extract_pdf_sync to be slow
        original_fn = _extract_pdf_sync

        def slow_extract(*args, **kwargs):
            import time
            time.sleep(1)
            return original_fn(*args, **kwargs)

        with patch("fetchaller.content.pdf._extract_pdf_sync", side_effect=slow_extract):
            result = await extract_pdf(pdf, config)

        assert result.error is not None
        assert "timed out" in result.error.lower()

    async def test_encrypted_via_async(self):
        pdf = _make_encrypted_pdf()
        result = await extract_pdf(pdf)
        assert result.error is not None
        assert "password" in result.error.lower()
