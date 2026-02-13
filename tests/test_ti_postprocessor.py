"""Tests for TI (ti.com) postprocessor and document viewer."""

from fetchaller.content.html import _detect_site
from fetchaller.content.ti import (
    extract_section_urls,
    extract_ti_part_from_pdf_url,
    is_ti_document_viewer,
    postprocess_ti,
)


def test_detect_ti_urls():
    assert _detect_site("https://www.ti.com/product/BQ25622E", False) == "ti"
    assert _detect_site("https://ti.com/product/BQ25622E", False) == "ti"
    assert _detect_site("https://www.ti.com/product/BQ25622E/part-details/BQ25622ERYKR", False) == "ti"
    assert _detect_site("https://example.com/ti.com", False) is None


def test_removes_inventory_placeholder_blocks():
    markdown = (
        "# BQ25622ERYKR\n\n"
        "Log in to order\nlock\nLog in to view inventory\n\n"
        "- Inventory:\n\n  Limit:\n\n"
        "  This limit is in place to protect sample purchases.\n\n"
        "**Out of stock**\n\n"
        "## Quality information\n\nReal content."
    )
    result = postprocess_ti(markdown)
    assert "Out of stock" not in result
    assert "Log in to order" not in result
    assert "Log in to view inventory" not in result
    assert "Real content" in result


def test_removes_empty_pricing_tables():
    markdown = (
        "## Pricing\n\n"
        "| Qty | Price |\n"
        "| --- | --- |\n"
        "| — |  |\n"
        "| — |  |\n"
        "| — |  |\n"
        "| + |  |\n\n"
        "## Quality information"
    )
    result = postprocess_ti(markdown)
    assert "| — |  |" not in result
    assert "Quality information" in result


def test_preserves_real_content():
    markdown = (
        "Log in to order\nlock\nLog in to view inventory\n\n"
        "- Inventory:\n\n**Out of stock**\n\n"
        "## Features\n\n- High-efficiency charger\n- BATFET control\n\n"
        "## Description\n\nThe BQ25622E is a charger IC."
    )
    result = postprocess_ti(markdown)
    assert "High-efficiency charger" in result
    assert "BATFET control" in result
    assert "BQ25622E is a charger IC" in result


# ---------------------------------------------------------------------------
# Document viewer detection
# ---------------------------------------------------------------------------


class TestDocumentViewerDetection:

    def test_datasheet_url(self):
        assert is_ti_document_viewer("https://www.ti.com/document-viewer/BQ25622E/datasheet")

    def test_datasheet_with_section(self):
        assert is_ti_document_viewer(
            "https://www.ti.com/document-viewer/BQ25622E/datasheet/GUID-94E28C8B-C11F-496C-8151-F7124DE91B11"
        )

    def test_lit_html_url(self):
        assert is_ti_document_viewer("https://www.ti.com/document-viewer/lit/html/SLUSFA3C")

    def test_no_www(self):
        assert is_ti_document_viewer("https://ti.com/document-viewer/BQ25622E/datasheet")

    def test_product_page_not_doc_viewer(self):
        assert not is_ti_document_viewer("https://www.ti.com/product/BQ25622E")

    def test_other_domain(self):
        assert not is_ti_document_viewer("https://example.com/document-viewer/foo")


# ---------------------------------------------------------------------------
# Section URL extraction from TOC
# ---------------------------------------------------------------------------


class TestExtractSectionUrls:

    SAMPLE_TOC = '''
    <yield to="toc">
    <a href="//www.ti.com/document-viewer/BQ25622E/datasheet/GUID-94E28C8B-C11F-496C-8151-F7124DE91B11#TITLE-FEATURES"
       data-chaptertitle="Features">1 Features</a>
    <a href="//www.ti.com/document-viewer/BQ25622E/datasheet/GUID-A4D8AAEF-6674-4D81-89BA-9E3A47B4EF9A#TITLE-APPLICATIONS"
       data-chaptertitle="Applications">2 Applications</a>
    <a href="//www.ti.com/document-viewer/BQ25622E/datasheet/GUID-94E28C8B-C11F-496C-8151-F7124DE91B11#TITLE-DUP"
       data-chaptertitle="Features dup">1 Features (dup)</a>
    </yield>
    '''

    def test_extracts_unique_urls(self):
        urls = extract_section_urls(self.SAMPLE_TOC)
        # Two unique GUIDs (third href is same GUID as first, just different fragment)
        assert len(urls) == 2

    def test_urls_have_raw_param(self):
        urls = extract_section_urls(self.SAMPLE_TOC)
        assert all(url.endswith("?raw=1") for url in urls)

    def test_urls_have_https(self):
        urls = extract_section_urls(self.SAMPLE_TOC)
        assert all(url.startswith("https://") for url in urls)

    def test_fragments_stripped(self):
        urls = extract_section_urls(self.SAMPLE_TOC)
        assert all("#" not in url for url in urls)

    def test_preserves_order(self):
        urls = extract_section_urls(self.SAMPLE_TOC)
        assert "GUID-94E28C8B" in urls[0]
        assert "GUID-A4D8AAEF" in urls[1]

    def test_empty_html_returns_empty(self):
        assert extract_section_urls("<html><body>no toc here</body></html>") == []


# ---------------------------------------------------------------------------
# PDF URL → part number extraction (for PDF→viewer upgrade)
# ---------------------------------------------------------------------------


class TestExtractTiPartFromPdfUrl:

    def test_ds_symlink(self):
        assert extract_ti_part_from_pdf_url("https://www.ti.com/lit/ds/symlink/bq25622e.pdf") == "bq25622e"

    def test_gpn(self):
        assert extract_ti_part_from_pdf_url("https://www.ti.com/lit/gpn/BQ25622E") == "BQ25622E"

    def test_ds_symlink_no_www(self):
        assert extract_ti_part_from_pdf_url("https://ti.com/lit/ds/symlink/lm358.pdf") == "lm358"

    def test_non_ti_domain_returns_none(self):
        assert extract_ti_part_from_pdf_url("https://example.com/lit/ds/symlink/bq25622e.pdf") is None

    def test_non_datasheet_lit_returns_none(self):
        """Application notes (/lit/an/) are not datasheets — don't upgrade."""
        assert extract_ti_part_from_pdf_url("https://www.ti.com/lit/an/slva704/slva704.pdf") is None

    def test_product_page_returns_none(self):
        assert extract_ti_part_from_pdf_url("https://www.ti.com/product/BQ25622E") is None
