"""Google search via Opera Mini SSR endpoint."""

import re
import sys
from datetime import UTC, datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .models import SearchResult

# Google internal URLs to filter out
_GOOGLE_INTERNAL_PREFIXES = (
    "https://www.google.com/",
    "http://www.google.com/",
    "https://google.com/",
    "http://google.com/",
    "https://accounts.google.com/",
    "https://support.google.com/",
    "https://maps.google.com/",
    "https://play.google.com/",
)

# Snippet cleanup regexes
_BREADCRUMB_RE = re.compile(r"^[\w.-]+\.(?:com|org|net|io|dev|edu|gov|co)\s*(?:›\s*\S+\s*)*")
_MORE_RESULTS_RE = re.compile(r"\s*More results from\s+\S+\s*$")
_STACKED_TITLES_RE = re.compile(r"\s*\.\.\.(?:\s+[A-Z].*)$")
_FEATURED_SNIPPET_RE = re.compile(r"\s*About Featured Snippets\s*$")
_TRAILING_SEPARATORS_RE = re.compile(r"[\s·|—–-]+$")


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


def is_captcha(response) -> bool:
    """Check if Google returned a CAPTCHA page."""
    from urllib.parse import urlparse

    parsed = urlparse(str(response.url))
    return (
        "sorry.google.com" in parsed.hostname
        or parsed.path.startswith("/sorry")
        or "unusual traffic" in response.text.lower()
        or response.status_code == 429
    )


def extract_results(html: str) -> list[SearchResult]:
    """Extract search results from Google SSR HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/url?q="):
            continue

        # Extract actual URL
        url = unquote(href.split("/url?q=")[1].split("&")[0])

        # Skip non-HTTP URLs (e.g., "#", "javascript:", relative paths)
        if not url.startswith(("http://", "https://")):
            continue

        # Filter Google internal URLs
        if any(url.startswith(prefix) for prefix in _GOOGLE_INTERNAL_PREFIXES):
            continue

        # Skip news carousel items — walk up 3 levels looking for carousel markers
        is_carousel = False
        p = a.parent
        for _ in range(3):
            if p is None:
                break
            classes = p.get("class", [])
            # pcitem = Google's carousel item class
            if "pcitem" in classes:
                is_carousel = True
                break
            p = p.parent
        if is_carousel:
            continue

        # Dedup anchor fragments — strip #fragment for dedup key
        base_url = url.split("#")[0]
        if base_url in seen_urls:
            continue
        seen_urls.add(base_url)

        # Extract title — prefer <h3> text (Google SSR wraps main results in h3)
        h3 = a.find("h3")
        if h3:
            title = h3.get_text(strip=True)
        elif a.find("div"):
            # Secondary result with div structure but no h3 — still extract
            cite = a.find("cite")
            if cite:
                cite.decompose()
            # Find the innermost div containing › (breadcrumb) and remove it
            # Must target the leaf div, not a parent that also contains ›
            breadcrumb_div = None
            for div in a.find_all("div"):
                text = div.get_text(strip=True)
                if "›" in text and not div.find("h3"):
                    # Check if any child div also has › (if so, skip this parent)
                    child_has_arrow = any(
                        "›" in d.get_text(strip=True) for d in div.find_all("div")
                    )
                    if not child_has_arrow:
                        breadcrumb_div = div
                        break
            if breadcrumb_div:
                breadcrumb_div.decompose()
            else:
                # No › breadcrumb — check for bare domain div (e.g. "justpy.io")
                url_domain = url.split("//")[-1].split("/")[0].replace("www.", "")
                for div in a.find_all("div"):
                    text = div.get_text(strip=True)
                    if text == url_domain or text == "www." + url_domain:
                        div.decompose()
                        break
            title = a.get_text(strip=True)
        else:
            # Skip stacked site links (bare <span> with no snippet value)
            continue
        if not title:
            continue

        # Extract snippet — look for sibling divs of the link's parent
        # Google SSR structure: grandparent > parent(link) + sibling(snippet)
        snippet = ""
        link_parent = a.parent
        if link_parent and link_parent.parent:
            for sibling in link_parent.parent.children:
                if sibling is link_parent or not hasattr(sibling, "get_text"):
                    continue
                if not hasattr(sibling, "find_all"):
                    continue
                # Decompose stacked result links before extracting text
                from copy import copy
                sib_copy = copy(sibling)
                for stacked_link in sib_copy.find_all("a", href=lambda h: h and h.startswith("/url?q=")):
                    stacked_link.decompose()
                text = sib_copy.get_text(separator=" ", strip=True)
                if text and len(text) > 20:
                    snippet = text[:500]
                    break

        # Fallback: walk up from <a> tag if sibling approach found nothing
        if not snippet:
            parent = a.parent
            for _ in range(5):
                if parent is None:
                    break
                text = parent.get_text(separator=" ", strip=True)
                if len(text) > len(title) + 40:
                    snippet = text.replace(title, "", 1).strip()[:500]
                    break
                parent = parent.parent

        # Clean up snippet noise
        if snippet:
            snippet = _BREADCRUMB_RE.sub("", snippet).strip()
            snippet = _MORE_RESULTS_RE.sub("", snippet).strip()
            # Truncate stacked result titles that follow "..." (adjacent results leaking in)
            snippet = _STACKED_TITLES_RE.sub(" ...", snippet).strip()
            # Remove Google's "About Featured Snippets" UI text
            snippet = _FEATURED_SNIPPET_RE.sub("", snippet).strip()
            # Strip trailing separator chars (· | — etc. from Google metadata)
            snippet = _TRAILING_SEPARATORS_RE.sub("", snippet)

        results.append(SearchResult(title=title, url=url, snippet=snippet))

    return results


async def search_google(
    session, query: str, page: int = 1
) -> tuple[list[SearchResult], bool, str | None]:
    """
    Search Google via Opera Mini SSR.

    Returns:
        Tuple of (results, is_captcha, error). If is_captcha is True, results is
        empty. ``error`` is a short description when the request failed at the
        transport/HTTP layer — distinct from an honest empty result set, and the
        caller surfaces it so a network failure never reads as "nothing exists".
    """
    params = {
        "q": query,
        "hl": "en",
        "safe": "off",
        "client": "ms-opera-mini-android",
        "channel": "new",
    }
    if page > 1:
        params["start"] = str((page - 1) * 10)

    try:
        response = await session.get(
            "https://www.google.com/search",
            params=params,
            timeout=10,
        )
    except Exception as e:
        _log(f"google request error: {type(e).__name__}: {e}")
        return [], False, f"{type(e).__name__}: {e}"

    if is_captcha(response):
        _log("google captcha detected")
        return [], True, None

    if response.status_code != 200:
        _log(f"google non-200 status: {response.status_code}")
        return [], False, f"HTTP {response.status_code}"

    results = extract_results(response.text)

    # Defensive: 200 OK but zero results and not empty query — possible structural change
    if not results:
        _log(f"google 200 but zero results extracted. HTML prefix: {response.text[:500]}")

    return results, False, None
