"""jobs.uber.com URL recognition for the fetch tool."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Postings live on both hosts: uber.com/{region}/{lang}/careers/list/{id}
# and jobs.uber.com/{lang}/jobs/{id}.
_UBER_JOB_RE = re.compile(r"^(?:/[a-z]{2,6}){0,2}/careers/list/(\d{3,12})/?$")
_JOBS_UBER_RE = re.compile(r"^(?:/[a-z]{2,6})?/jobs/(\d{3,12})/?$")
# Both list forms: uber.com/{region}/{lang}/careers/list and the
# jobs.uber.com/{lang}/jobs form the board actually redirects to. Without the
# second, `fetch()` silently declined to dispatch the very URL a user lands on
# after following uber.com/us/en/careers/list/.
_LIST_RE = re.compile(r"^(?:/[a-z]{2,6}){0,2}/careers/list/?$|^(?:/[a-z]{2,6})?/jobs/?$")


def is_uber_jobs_host(hostname: str) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    return host in ("uber.com", "www.uber.com", "jobs.uber.com")


def _parsed(url: str):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not is_uber_jobs_host(parsed.hostname or ""):
        return None
    return parsed


def extract_uber_job_id(url: str) -> str | None:
    parsed = _parsed(url)
    if parsed is None:
        return None
    path = parsed.path or ""
    for pattern in (_UBER_JOB_RE, _JOBS_UBER_RE):
        match = pattern.match(path)
        if match:
            return match.group(1)
    return None


def is_uber_job_url(url: str) -> bool:
    return extract_uber_job_id(url) is not None


def is_uber_jobs_list_url(url: str) -> bool:
    parsed = _parsed(url)
    return parsed is not None and bool(_LIST_RE.match(parsed.path or ""))
