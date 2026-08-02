"""jobs.apple.com URL recognition for the fetch tool."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_LOCALE = r"[a-z]{2}-[a-z]{2}"
# /en-ca/details/200674861/staff-machine-learning-engineer
_DETAILS_RE = re.compile(rf"^/(?:{_LOCALE}/)?details/(\d{{4,20}})(?:/([^/?#]*))?/?$")
_SEARCH_RE = re.compile(rf"^/(?:{_LOCALE}/)?search/?$")
_LOCALE_RE = re.compile(rf"^/({_LOCALE})/")


def is_apple_jobs_host(hostname: str) -> bool:
    return (hostname or "").rstrip(".").casefold() == "jobs.apple.com"


def _parsed(url: str):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not is_apple_jobs_host(parsed.hostname or ""):
        return None
    return parsed


def extract_locale(url: str) -> str:
    parsed = _parsed(url)
    if parsed is None:
        return ""
    match = _LOCALE_RE.match(parsed.path or "")
    return match.group(1) if match else ""


def extract_apple_job(url: str) -> tuple[str, str] | None:
    """Return ``(position_id, slug)`` for a posting URL, else None."""
    parsed = _parsed(url)
    if parsed is None:
        return None
    match = _DETAILS_RE.match(parsed.path or "")
    if not match:
        return None
    return match.group(1), match.group(2) or ""


def is_apple_job_url(url: str) -> bool:
    return extract_apple_job(url) is not None


def extract_apple_search(url: str) -> dict | None:
    """Return ``{search, location}`` for a search URL, else None."""
    parsed = _parsed(url)
    if parsed is None or not _SEARCH_RE.match(parsed.path or ""):
        return None
    query = parse_qs(parsed.query or "")
    return {
        "search": (query.get("search") or [""])[0],
        "location": (query.get("location") or [""])[0],
    }


def is_apple_search_url(url: str) -> bool:
    return extract_apple_search(url) is not None
