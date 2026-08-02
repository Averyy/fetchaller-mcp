"""Google careers URL recognition for the fetch tool.

Scoped tightly to ``/about/careers/applications`` — google.com is mostly not a
job board, and nothing outside that prefix should ever route here.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_BASE = "/about/careers/applications"
# /jobs/results/92025237427626694-product-design-developer-xr — the slug is
# cosmetic and the id alone resolves, so only the id is captured.
_JOB_RE = re.compile(rf"^{re.escape(_BASE)}/jobs/results/(\d{{6,25}})(?:-[^/?#]*)?/?$")
_SEARCH_RE = re.compile(rf"^{re.escape(_BASE)}/jobs/results/?$")


def is_google_careers_host(hostname: str) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    return host in ("www.google.com", "google.com", "careers.google.com")


def _parsed(url: str):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not is_google_careers_host(parsed.hostname or ""):
        return None
    return parsed


def extract_google_job_id(url: str) -> str | None:
    parsed = _parsed(url)
    if parsed is None:
        return None
    match = _JOB_RE.match(parsed.path or "")
    return match.group(1) if match else None


def is_google_job_url(url: str) -> bool:
    return extract_google_job_id(url) is not None


def extract_google_search(url: str) -> dict | None:
    """Return ``{title, location}`` for a careers search URL, else None."""
    parsed = _parsed(url)
    if parsed is None or not _SEARCH_RE.match(parsed.path or ""):
        return None
    query = parse_qs(parsed.query or "")
    return {
        "title": (query.get("q") or [""])[0],
        # `location` repeats for a multi-location search; the first is enough
        # to reproduce the caller's intent here.
        "location": (query.get("location") or [""])[0],
    }


def is_google_search_url(url: str) -> bool:
    return extract_google_search(url) is not None
