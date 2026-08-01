"""Oracle Recruiting Cloud URL recognition for the fetch tool.

ORC candidate sites are SPAs: a posting page returns ~42 KB of HTML with about
eight characters of readable text, so the generic HTML path yields an empty
shell. These patterns route those URLs to the API client instead.

Tenants host their candidate site on their own domain, so there is no single
hostname pattern to match. Recognition is therefore driven by the employer
registry plus the Fusion host shape, which is stable across every deployment.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .employers import KNOWN_EMPLOYERS

# .../job/338925 and .../job/338925/some-slug, with or without a locale segment.
_JOB_PATH_RE = re.compile(r"/job/(\d{4,20})(?:/[^/?#]*)?/?$")
# The candidate-site search page, e.g. /en/sites/jobsearch/jobs
_SEARCH_PATH_RE = re.compile(r"/sites/[^/]+/jobs/?$")
_FUSION_HOST_RE = re.compile(r"^[a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com$", re.IGNORECASE)


def _known_hosts() -> set[str]:
    hosts: set[str] = set()
    for record in KNOWN_EMPLOYERS.values():
        for candidate in (record.careers_url, record.fallback_host, record.posting_url):
            if not candidate:
                continue
            host = (urlparse(candidate).hostname or "").casefold()
            if host:
                hosts.add(host)
    return hosts


def is_oracle_recruiting_host(hostname: str) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    if not host:
        return False
    return bool(_FUSION_HOST_RE.match(host)) or host in _known_hosts()


def _parsed(url: str):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not is_oracle_recruiting_host(parsed.hostname or ""):
        return None
    return parsed


def employer_for_url(url: str) -> str | None:
    """Return the registry alias whose site this URL belongs to, else the host."""
    parsed = _parsed(url)
    if parsed is None:
        return None
    host = (parsed.hostname or "").casefold()
    for alias, record in KNOWN_EMPLOYERS.items():
        for candidate in (record.careers_url, record.fallback_host, record.posting_url):
            if candidate and (urlparse(candidate).hostname or "").casefold() == host:
                return alias
    return f"{parsed.scheme}://{parsed.netloc}"


def extract_requisition_id(url: str) -> str | None:
    parsed = _parsed(url)
    if parsed is None:
        return None
    match = _JOB_PATH_RE.search(parsed.path or "")
    return match.group(1) if match else None


def is_oracle_job_url(url: str) -> bool:
    return extract_requisition_id(url) is not None


def is_oracle_search_url(url: str) -> bool:
    parsed = _parsed(url)
    if parsed is None or extract_requisition_id(url) is not None:
        return False
    return bool(_SEARCH_PATH_RE.search(parsed.path or ""))
