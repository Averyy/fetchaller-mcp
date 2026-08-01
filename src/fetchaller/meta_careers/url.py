"""metacareers.com URL recognition for the fetch tool."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_JOB_RE = re.compile(r"^/jobs/(\d{6,25})/?$")
_JOBS_INDEX_RE = re.compile(r"^/(?:jobs|jobsearch)/?$")


def is_meta_careers_host(hostname: str) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    return host in ("metacareers.com", "www.metacareers.com")


def _parsed(url: str):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not is_meta_careers_host(parsed.hostname or ""):
        return None
    return parsed


def extract_meta_job_id(url: str) -> str | None:
    parsed = _parsed(url)
    if parsed is None:
        return None
    match = _JOB_RE.match(parsed.path or "")
    return match.group(1) if match else None


def is_meta_job_url(url: str) -> bool:
    return extract_meta_job_id(url) is not None


def is_meta_jobs_index_url(url: str) -> bool:
    parsed = _parsed(url)
    return parsed is not None and bool(_JOBS_INDEX_RE.match(parsed.path or ""))
