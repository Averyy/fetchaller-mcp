"""Exact-container PDF worker probe using the production process context."""

from __future__ import annotations

import asyncio
import json

from fetchaller.content.html import warm_html_process_runtime
from fetchaller.tools.fetch import fetch_url

_PDF_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


async def _main() -> None:
    await warm_html_process_runtime()
    result = await fetch_url(_PDF_URL, timeout=30)
    if "error" in result:
        raise RuntimeError(result["error"])
    content = result.get("content")
    if (
        result.get("content_type") != "pdf"
        or not isinstance(content, str)
        or "Dummy PDF file" not in content
    ):
        raise RuntimeError(
            "real PDF probe returned an incomplete representation"
        )
    print(
        json.dumps(
            {
                "status": "PASS",
                "content_type": result["content_type"],
                "content_chars": len(content),
                "url": result.get("url"),
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
