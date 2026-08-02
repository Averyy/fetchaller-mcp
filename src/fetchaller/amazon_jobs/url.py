"""amazon.jobs URL recognition for the fetch tool.

Kept separate from ``content.amazon``, which matches the retail storefront's
country TLDs (amazon.com, amazon.ca, ...). ``amazon.jobs`` is a different
host and must not be routed through the shopping post-processor.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_JOB_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?/)?jobs/(\d{4,20})(?:/[^/?#]*)?/?$")
_SEARCH_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?/)?search(?:\.json)?/?$")


def is_amazon_jobs_host(hostname: str) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    return host in ("amazon.jobs", "www.amazon.jobs")


def _parsed(url: str):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not is_amazon_jobs_host(parsed.hostname or ""):
        return None
    return parsed


def extract_amazon_job_path(url: str) -> str | None:
    """Return the ``/en/jobs/{id}/{slug}`` path for a posting URL, else None."""
    parsed = _parsed(url)
    if parsed is None:
        return None
    path = parsed.path or ""
    return path if _JOB_RE.match(path) else None


def is_amazon_job_url(url: str) -> bool:
    return extract_amazon_job_path(url) is not None


def is_amazon_jobs_search_url(url: str) -> bool:
    parsed = _parsed(url)
    return parsed is not None and bool(_SEARCH_RE.match(parsed.path or ""))
