"""DuckDuckGo HTML search endpoint scraper."""

import sys
from datetime import UTC, datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .models import SearchResult


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


def extract_results(html: str) -> list[SearchResult]:
    """Extract search results from DDG HTML response."""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for result_div in soup.select(".result"):
        link = result_div.select_one(".result__a")
        if not link or not link.get("href"):
            continue

        title = link.get_text(strip=True)
        if not title:
            continue

        # Extract URL from DDG redirect wrapper
        href = link["href"]
        if "uddg=" in href:
            url = unquote(href.split("uddg=")[1].split("&")[0])
        else:
            url = href

        # Skip non-HTTP URLs
        if not url.startswith(("http://", "https://")):
            continue

        # Extract snippet
        snippet_el = result_div.select_one(".result__snippet")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""

        results.append(SearchResult(title=title, url=url, snippet=snippet))

    return results


async def search_ddg(session, query: str) -> list[SearchResult]:
    """
    Search DuckDuckGo via HTML endpoint.

    Only called on page 1 — DDG pagination is fragile.

    Returns:
        List of results (empty on error).
    """
    params = {"q": query, "kp": "-2"}

    try:
        response = await session.get(
            "https://html.duckduckgo.com/html/",
            params=params,
            headers={"Accept": "text/html"},
            timeout=10,
        )
    except Exception as e:
        _log(f"ddg request error: {type(e).__name__}: {e}")
        return []

    if response.status_code != 200:
        _log(f"ddg non-200 status: {response.status_code}")
        return []

    results = extract_results(response.text)

    if not results:
        _log(f"ddg 200 but zero results extracted. HTML prefix: {response.text[:500]}")

    return results
