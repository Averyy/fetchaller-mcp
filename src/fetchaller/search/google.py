"""Google search via Opera Mini SSR endpoint."""

import random
import re
import sys
from datetime import UTC, datetime
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .models import SearchResult

# Opera Mini Presto UAs — Google serves full SSR HTML to these
OPERA_MINI_UAS = [
    "Opera/9.80 (Android; Opera Mini/7.5.33361/191.243; U; en) Presto/2.12.423 Version/12.16",
    "Opera/9.80 (Android; Opera Mini/36.2.2254/191.306; U; en) Presto/2.12.423 Version/12.16",
    "Opera/9.80 (Android; Opera Mini/20.0.2254/191.291; U; en) Presto/2.12.423 Version/12.16",
    "Opera/9.80 (Android 4.1.2; Opera Mini/7.6.40234/191.257; U; en) Presto/2.12.423 Version/12.16",
    "Opera/9.80 (iPhone; Opera Mini/14.0.0/37.7452; U; en) Presto/2.12.423 Version/12.16",
    "Opera/9.80 (iPhone; Opera Mini/7.1.32694/27.1407; U; en) Presto/2.8.119 Version/11.10",
    "Opera/9.80 (iPad; Opera Mini/7.1.32694/27.1407; U; en) Presto/2.8.119 Version/11.10",
    "Opera/9.80 (J2ME/MIDP; Opera Mini/9.80 (S60; SymbOS; Opera Mobi/23.348; U; en)) Presto/2.5.25 Version/10.54",
    "Opera/9.80 (S60; SymbOS; Opera Mobi/SYB-1107071606; U; en) Presto/2.8.149 Version/11.10",
    "Opera/9.80 (SpreadTrum; Opera Mini/4.4.33961/191.302; U; en) Presto/2.12.423 Version/12.16",
    "Opera/12.02 (Android 4.1; Linux; Opera Mobi/ADR-1111101157; U; en) Presto/2.9.201 Version/12.02",
    "Opera/12.00 (Android 4.0; Linux; Opera Mobi/ADR-1205181138; U; en) Presto/2.10.254 Version/12.00",
    "Opera/9.80 (Android; Opera Mini/7.5/191.243; U; en) Presto/2.12.423 Version/12.16",
    "Opera/9.80 (iPhone; Opera Mini/8.0/37.7452; U; en) Presto/2.12.423 Version/12.16",
]

# Pool of realistic devices (popular in Opera Mini markets)
PHONE_POOL = [
    ("Samsung # SM-A515F", "Mozilla/5.0 (Linux; Android 11; SM-A515F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.104 Mobile Safari/537.36"),
    ("Samsung # SM-A127F", "Mozilla/5.0 (Linux; Android 12; SM-A127F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Mobile Safari/537.36"),
    ("Samsung # SM-G960F", "Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36"),
    ("Xiaomi # Redmi Note 9", "Mozilla/5.0 (Linux; Android 10; Redmi Note 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.210 Mobile Safari/537.36"),
    ("Xiaomi # Redmi 9A", "Mozilla/5.0 (Linux; Android 10; Redmi 9A) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.105 Mobile Safari/537.36"),
    ("TECNO # TECNO KC8", "Mozilla/5.0 (Linux; Android 10; TECNO KC8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.105 Mobile Safari/537.36"),
    ("TECNO # TECNO KE5", "Mozilla/5.0 (Linux; Android 11; TECNO KE5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.104 Mobile Safari/537.36"),
    ("Infinix # Infinix X680", "Mozilla/5.0 (Linux; Android 10; Infinix X680) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.181 Mobile Safari/537.36"),
    ("Nokia # Nokia 2.3", "Mozilla/5.0 (Linux; Android 10; Nokia 2.3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.181 Mobile Safari/537.36"),
    ("itel # itel P36", "Mozilla/5.0 (Linux; Android 10; itel P36) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.141 Mobile Safari/537.36"),
]

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


def build_google_headers() -> dict[str, str]:
    """Build Opera Mini proxy request headers with random UA and device."""
    ua = random.choice(OPERA_MINI_UAS)
    phone, stock_ua = random.choice(PHONE_POOL)
    return {
        "User-Agent": ua,
        "Accept": "text/html, application/xml;q=0.9, application/xhtml+xml, image/png, image/webp, image/jpeg, image/gif, image/x-xbitmap, */*;q=0.1",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "Keep-Alive",
        "X-OperaMini-Features": "advanced, file_system, secure, touch",
        "X-OperaMini-Phone": phone,
        "X-OperaMini-Phone-UA": stock_ua,
        "Device-Stock-UA": stock_ua,
    }


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
) -> tuple[list[SearchResult], bool]:
    """
    Search Google via Opera Mini SSR.

    Returns:
        Tuple of (results, is_captcha). If is_captcha is True, results is empty.
    """
    params = {
        "q": query,
        "hl": "en",
        "safe": "off",
    }
    if page > 1:
        params["start"] = str((page - 1) * 10)

    headers = build_google_headers()

    try:
        response = await session.get(
            "https://www.google.com/search",
            params=params,
            headers=headers,
            timeout=10,
            allow_redirects=True,
        )
    except Exception as e:
        _log(f"google request error: {type(e).__name__}: {e}")
        return [], False

    if is_captcha(response):
        _log("google captcha detected")
        return [], True

    if response.status_code != 200:
        _log(f"google non-200 status: {response.status_code}")
        return [], False

    results = extract_results(response.text)

    # Defensive: 200 OK but zero results and not empty query — possible structural change
    if not results:
        _log(f"google 200 but zero results extracted. HTML prefix: {response.text[:500]}")

    return results, False
