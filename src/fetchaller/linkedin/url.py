"""LinkedIn URL recognition for the fetch tool."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Job permalinks appear as /jobs/view/{id} or /jobs/view/{slug}-{id}, on
# www.linkedin.com and on every country subdomain (ca., uk., in., ...).
_JOB_VIEW_RE = re.compile(r"^/jobs/view/(?:[^/]*?-)?(\d{6,20})/?$")
_CURRENT_JOB_ID_RE = re.compile(r"^\d{6,20}$")


def is_linkedin_host(hostname: str) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def extract_linkedin_job_id(url: str) -> str | None:
    """Return the numeric job ID for a LinkedIn job permalink, else None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not is_linkedin_host(parsed.hostname or ""):
        return None

    match = _JOB_VIEW_RE.match(parsed.path or "")
    if match:
        return match.group(1)

    # /jobs/search/?currentJobId=123 and /jobs/collections/...?currentJobId=123
    if (parsed.path or "").startswith("/jobs/"):
        from urllib.parse import parse_qs

        values = parse_qs(parsed.query or "").get("currentJobId") or []
        if values and _CURRENT_JOB_ID_RE.match(values[0]):
            return values[0]
    return None
