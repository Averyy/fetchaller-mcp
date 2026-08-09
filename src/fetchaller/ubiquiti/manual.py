"""Download a Ubiquiti installation guide as a readable document.

Ubiquiti publishes no PDF for current products: the web guide *is* the manual,
and its wording is drawn as vector outlines rather than text (see ``guide.py``).
So "get me the manual" cannot be answered by extracting words — it has to be
answered by rebuilding the pages and handing back a document.

Each page asset is reconstructed into a standalone SVG, and PyMuPDF — already a
dependency for PDF reading — converts those into one combined PDF, or into a PNG
per page for a caller that wants to look at them. No new dependency is involved
and nothing is rasterized upstream: the PDF stays vector, so it prints and zooms
like the original.

Guide pages are one tall scroll rather than paper-sized — page A of the U7 Pro
Wall guide is 320 x 4684 units — so PNG rendering is bounded by total pixels
rather than by DPI, otherwise a single page would allocate hundreds of
megabytes.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import wafer

from . import api, guide

# Bound on one rendered PNG. Guide pages are extremely tall, so a fixed DPI is
# the wrong control; this caps the allocation whatever the aspect ratio.
MAX_PNG_PIXELS = 40_000_000
# Width a page is rendered at when its height allows.
TARGET_PNG_WIDTH = 1000

_SLUG_SAFE_RE = re.compile(r"[^a-z0-9._-]+")


def safe_slug(value: str) -> str:
    """A filename-safe slug. Never returns a path separator or an empty name."""
    cleaned = _SLUG_SAFE_RE.sub("-", value.strip().lower()).strip("-.")
    return cleaned or "guide"


async def fetch_guide(url: str, *, timeout: float = 30.0) -> dict:
    """Fetch a guide shell and every page asset behind it.

    Returns ``{title, description, slug, source, pages: [{id, svg, texts,
    links}], links, dropped_pages}``. Raises ``guide.GuideNotFoundError`` when the
    slug has no published guide.
    """
    session = await api.get_session()
    response = await session.get(url, timeout=timeout)
    if response.status_code != 200:
        raise LookupError(f"installation guide returned HTTP {response.status_code}")

    # Assets resolve against the URL the redirect landed on, but a missing guide
    # must be reported against the URL the caller actually asked for: dl.ui.com
    # bounces an unknown slug to techspecs.ui.com, and naming that in the error
    # would send the reader to a page they never requested.
    try:
        found = guide.discover(response.text, str(response.url) or url)
    except guide.GuideNotFoundError:
        raise guide.GuideNotFoundError(f"no installation guide published at {url}") from None

    async def _one(page_id: str, filename: str) -> dict:
        # One unreachable asset must cost that page, not the guide. Without this
        # a single timeout would cancel its siblings and lose five pages that
        # downloaded perfectly well.
        try:
            asset = guide.asset_url(found["base"], filename)
            result = await session.get(asset, timeout=timeout)
            if result.status_code != 200:
                return {"id": page_id, "svg": None, "texts": [], "links": [], "error": f"HTTP {result.status_code}"}
            tree = guide.parse_asset(result.text)
            texts, links = guide.read_page(result.text, tree)
        except (TimeoutError, OSError, ValueError, wafer.WaferError) as exc:
            return {"id": page_id, "svg": None, "texts": [], "links": [], "error": type(exc).__name__}
        return {
            "id": page_id,
            "svg": guide.page_svg(tree) if tree is not None else None,
            "texts": texts,
            "links": links,
        }

    pages = await asyncio.gather(*(_one(pid, name) for pid, name in found["pages"]))

    merged: list[str] = []
    for page in pages:
        for link in page["links"]:
            if link not in merged:
                merged.append(link)

    return {
        "title": found["title"],
        "description": found["description"],
        "slug": api.guide_slug(url) or safe_slug(found["title"]),
        "source": url,
        "pages": list(pages),
        "links": merged,
        "dropped_pages": found["dropped_pages"],
    }


def _open_svg(svg: str):
    import pymupdf

    return pymupdf.open(stream=svg.encode("utf-8"), filetype="svg")


def build_pdf(svgs: list[str]) -> bytes:
    """One vector PDF holding every page, in order."""
    import pymupdf

    combined = pymupdf.open()
    try:
        for svg in svgs:
            with _open_svg(svg) as page_doc:
                pdf_bytes = page_doc.convert_to_pdf()
            with pymupdf.open("pdf", pdf_bytes) as as_pdf:
                combined.insert_pdf(as_pdf)
        return combined.tobytes()
    finally:
        combined.close()


def build_png(svg: str) -> bytes:
    """One page as a PNG, scaled to fit within ``MAX_PNG_PIXELS``."""
    import pymupdf

    with _open_svg(svg) as page_doc:
        pdf_bytes = page_doc.convert_to_pdf()
    with pymupdf.open("pdf", pdf_bytes) as as_pdf:
        page = as_pdf[0]
        width = max(page.rect.width, 1.0)
        height = max(page.rect.height, 1.0)
        scale = TARGET_PNG_WIDTH / width
        # Tall pages (a guide page can be 15x its own width) would blow past any
        # sane allocation at a fixed width, so clamp on area as well.
        if width * scale * height * scale > MAX_PNG_PIXELS:
            scale = (MAX_PNG_PIXELS / (width * height)) ** 0.5
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
        return pixmap.tobytes("png")


FORMATS = ("pdf", "png", "svg")


def manuals_root() -> Path:
    """Where downloaded manuals are written.

    Under the server's own data directory rather than a caller-supplied path:
    the slug is the only variable part of the destination, so no request can
    steer a write outside this tree.
    """
    from ..config import load_config

    data_dir = load_config().data_dir or str(Path.home() / ".local" / "share" / "fetchaller")
    return Path(data_dir) / "manuals"


def save_manual(data: dict, fmt: str = "pdf") -> dict:
    """Write a fetched guide to disk in ``fmt`` and report what was written."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")

    slug = safe_slug(str(data.get("slug") or "guide"))
    destination = manuals_root() / slug
    destination.mkdir(parents=True, exist_ok=True)

    pages = [p for p in data.get("pages") or [] if p.get("svg")]
    skipped = [str(p.get("id")) for p in data.get("pages") or [] if not p.get("svg")]
    if not pages:
        raise LookupError("none of the guide's pages could be reconstructed")

    written: list[Path] = []
    if fmt == "pdf":
        target = destination / f"{slug}.pdf"
        target.write_bytes(build_pdf([p["svg"] for p in pages]))
        written.append(target)
    else:
        for index, page in enumerate(pages, start=1):
            name = f"{slug}-{index:02d}-{safe_slug(str(page.get('id')))}.{fmt}"
            target = destination / name
            if fmt == "svg":
                target.write_text(page["svg"], encoding="utf-8")
            else:
                target.write_bytes(build_png(page["svg"]))
            written.append(target)

    return {
        "format": fmt,
        "directory": str(destination),
        "files": [str(p) for p in written],
        "pages": len(pages),
        "skipped_pages": skipped,
    }


async def download_manual(url: str, *, fmt: str = "pdf", timeout: float = 30.0) -> dict:
    """Fetch a guide and write it out. Returns ``{"content": markdown}``."""
    from . import render

    try:
        data = await fetch_guide(url, timeout=timeout)
    except guide.GuideNotFoundError as exc:
        return {"error": str(exc)}
    except LookupError as exc:
        return {"error": f"Installation guide could not be read: {exc}"}
    except (TimeoutError, OSError, ValueError, wafer.WaferError) as exc:
        # WaferError subclasses Exception directly, so it is not covered by any
        # of the above; letting it escape loses the URL from the error message.
        return {"error": f"Installation guide request failed: {type(exc).__name__}: {exc}"}

    try:
        saved = await asyncio.to_thread(save_manual, data, fmt)
    except (LookupError, ValueError) as exc:
        return {"error": f"Manual could not be written: {exc}"}
    except OSError as exc:
        return {"error": f"Manual could not be written: {type(exc).__name__}: {exc}"}

    lines = [
        render.render_guide(data, manual_hint=False).rstrip(),
        "",
        "## Downloaded",
        f"Format: {saved['format']} | Pages written: {saved['pages']}",
        f"Directory: {saved['directory']}",
        *[f"- {path}" for path in saved["files"]],
    ]
    if saved["skipped_pages"]:
        lines.append(f"Not reproduced: {', '.join(saved['skipped_pages'])}")
    lines.append(
        "\nThe guide's wording is vector artwork, so these files are the manual "
        "itself; open the PDF, or read the PNGs as images."
    )
    return {"content": "\n".join(lines)}
