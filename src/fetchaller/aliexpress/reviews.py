"""AliExpress reviews API client.

Fetches product reviews from feedback.aliexpress.com — no auth required.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

import wafer

from ..config import get_wafer_cache_dir
from ..security.xss import safe_log_text


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] aliexpress reviews: "
        f"{safe_log_text(msg)}",
        file=sys.stderr,
    )


# Shared session for feedback.aliexpress.com (reuses TLS connections).
_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    max_rotations=0,
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=5 * 1024 * 1024,
                )
    return _session


async def close_session() -> None:
    """Release the shared session (for shutdown cleanup)."""
    global _session
    _session = None


async def fetch_reviews(
    product_id: str,
    page: int = 1,
    page_size: int = 10,
    timeout: float = 10,
) -> dict:
    """Fetch product reviews from feedback.aliexpress.com.

    Args:
        product_id: Numeric product ID.
        page: Page number (1-indexed).
        page_size: Reviews per page (default 10).

    Returns:
        Parsed ``data`` field from the API response, or ``{"error": "..."}``.
    """
    url = "https://feedback.aliexpress.com/pc/searchEvaluation.do"
    params = {
        "productId": product_id,
        "lang": "en_US",
        "country": "US",
        "page": str(page),
        "pageSize": str(page_size),
        "filter": "all",
        "sort": "complex_default",
    }

    session = await _get_session()
    try:
        resp = await session.get(
            url,
            params=params,
            headers={"Referer": f"https://www.aliexpress.com/item/{product_id}.html"},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            _log(f"reviews API HTTP {resp.status_code} for product {product_id}")
            return {"error": f"Reviews API returned HTTP {resp.status_code}"}

        data = resp.json()
        if "data" in data:
            return data["data"]
        return {"error": "No data field in reviews response"}
    except Exception as e:
        _log(
            f"reviews API error for product {product_id}: "
            f"{type(e).__name__}"
        )
        return {"error": f"Reviews API error: {e}"}
