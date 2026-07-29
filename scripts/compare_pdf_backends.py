#!/usr/bin/env python3
"""Compare pdfplumber vs pymupdf4llm output on sample PDFs.

Usage:
    uv run python scripts/compare_pdf_backends.py
"""

import time
from pathlib import Path

import pymupdf
import pymupdf4llm

try:
    import pdfplumber
except ImportError:
    pdfplumber = None
    print("pdfplumber not installed — showing pymupdf4llm output only\n")

SAMPLE_PDFS = [
    Path("/Users/avery/Code/esphome-custom-components/components/vl53l1x/TOF400C VL53L1X Datasheet.pdf"),
    Path("/Users/avery/Code/esphome-custom-components/components/hlk_ld2413/HLK-LD2413 User Manual.pdf"),
    Path("/Users/avery/Code/esphome-custom-components/components/hlk_ld8001h/HLK-LD8001H Overview.pdf"),
]

PREVIEW_CHARS = 2000


def extract_pdfplumber(content: bytes) -> tuple[str, float]:
    """Extract text with pdfplumber. Returns (text, seconds)."""
    from io import BytesIO

    start = time.perf_counter()
    with pdfplumber.open(BytesIO(content)) as pdf:
        parts = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
        result = "\n\n".join(parts)
    elapsed = time.perf_counter() - start
    return result, elapsed


def extract_pymupdf4llm(content: bytes) -> tuple[str, float]:
    """Extract markdown with pymupdf4llm. Returns (markdown, seconds)."""
    start = time.perf_counter()
    doc = pymupdf.open(stream=content, filetype="pdf")
    result = pymupdf4llm.to_markdown(doc, ignore_images=True, show_progress=False)
    doc.close()
    elapsed = time.perf_counter() - start
    return result, elapsed


def main():
    for pdf_path in SAMPLE_PDFS:
        if not pdf_path.exists():
            print(f"SKIP: {pdf_path.name} (not found)")
            print()
            continue

        content = pdf_path.read_bytes()
        size_mb = len(content) / (1024 * 1024)
        print(f"{'=' * 70}")
        print(f"FILE: {pdf_path.name} ({size_mb:.1f}MB)")
        print(f"{'=' * 70}")

        # pdfplumber
        if pdfplumber:
            plumber_text, plumber_time = extract_pdfplumber(content)
            print(f"\n--- pdfplumber ({plumber_time:.3f}s, {len(plumber_text)} chars) ---")
            print(plumber_text[:PREVIEW_CHARS])
            if len(plumber_text) > PREVIEW_CHARS:
                print(f"\n... ({len(plumber_text) - PREVIEW_CHARS} more chars)")

        # pymupdf4llm
        mupdf_text, mupdf_time = extract_pymupdf4llm(content)
        print(f"\n--- pymupdf4llm ({mupdf_time:.3f}s, {len(mupdf_text)} chars) ---")
        print(mupdf_text[:PREVIEW_CHARS])
        if len(mupdf_text) > PREVIEW_CHARS:
            print(f"\n... ({len(mupdf_text) - PREVIEW_CHARS} more chars)")

        # Summary
        print("\n--- Summary ---")
        if pdfplumber:
            print(f"  pdfplumber:  {len(plumber_text):>8} chars in {plumber_time:.3f}s")
        print(f"  pymupdf4llm: {len(mupdf_text):>8} chars in {mupdf_time:.3f}s")
        if pdfplumber:
            speedup = plumber_time / mupdf_time if mupdf_time > 0 else float("inf")
            print(f"  Speed: pymupdf4llm is {speedup:.1f}x {'faster' if speedup > 1 else 'slower'}")
        print()


if __name__ == "__main__":
    main()
