"""AliExpress reviews API client.

Fetches product reviews from feedback.aliexpress.com — no auth required.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from curl_cffi.requests import AsyncSession


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] aliexpress reviews: {msg}", file=sys.stderr)


async def fetch_reviews(
    product_id: str,
    page: int = 1,
    page_size: int = 10,
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

    async with AsyncSession(impersonate="chrome") as session:
        try:
            resp = await session.get(
                url,
                params=params,
                headers={"Referer": f"https://www.aliexpress.com/item/{product_id}.html"},
                timeout=10,
            )
            if resp.status_code >= 400:
                _log(f"reviews API HTTP {resp.status_code} for product {product_id}")
                return {"error": f"Reviews API returned HTTP {resp.status_code}"}

            data = resp.json()
            if "data" in data:
                return data["data"]
            return {"error": "No data field in reviews response"}
        except Exception as e:
            _log(f"reviews API error for product {product_id}: {e}")
            return {"error": f"Reviews API error: {e}"}
