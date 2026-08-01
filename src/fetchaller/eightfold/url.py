"""Eightfold URL recognition and tenant resolution.

Eightfold tenants come in two host shapes:

- ``{tenant}.eightfold.ai`` — the default, recognisable from the host alone.
- A vanity host the employer owns (``apply.careers.microsoft.com``,
  ``explore.jobs.netflix.net``). Nothing in the hostname marks these as
  Eightfold, so they are matched against the registry below.

The registry is a routing convenience, not the source of truth: the group id
each tenant actually needs is read live from the page (see
``api.discover_group_id``). A vanity host missing here still works when the
caller passes its board URL explicitly — it just isn't auto-detected by
``fetch()``.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# Employer aliases accepted by the search tool, mapped to their board URL.
# Group ids are resolved live rather than hardcoded here.
KNOWN_EMPLOYERS: dict[str, str] = {
    "microsoft": "https://apply.careers.microsoft.com/careers",
    "netflix": "https://explore.jobs.netflix.net/careers",
    "paypal": "https://paypal.eightfold.ai/careers",
}

# Vanity hosts that are Eightfold but do not say so in their hostname.
_VANITY_HOSTS = frozenset(
    (urlparse(u).hostname or "").casefold() for u in KNOWN_EMPLOYERS.values()
)

_EIGHTFOLD_HOST_RE = re.compile(r"^(?:[a-z0-9][a-z0-9-]*\.)*eightfold\.ai$")
# /careers, /careers/job/{id}, and the locale-prefixed variants tenants emit.
_CAREERS_PATH_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?/)?careers(?:/.*)?$")
_JOB_PATH_RE = re.compile(
    r"^/(?:[a-z]{2}(?:-[a-zA-Z]{2,4})?/)?careers/job/(\d{6,25})/?$"
)
_POSITION_ID_RE = re.compile(r"^\d{6,25}$")


def is_eightfold_host(hostname: str) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    if not host:
        return False
    return bool(_EIGHTFOLD_HOST_RE.match(host)) or host in _VANITY_HOSTS


def _parsed(url: str):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not is_eightfold_host(parsed.hostname or ""):
        return None
    return parsed


def board_root(url: str) -> str | None:
    """Return ``https://{host}`` for any Eightfold URL, else None."""
    parsed = _parsed(url)
    if parsed is None:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def is_eightfold_board_url(url: str) -> bool:
    """True for a tenant's board/search page (not a single posting)."""
    parsed = _parsed(url)
    if parsed is None:
        return False
    if not _CAREERS_PATH_RE.match(parsed.path or ""):
        return False
    return extract_position_id(url) is None


def extract_position_id(url: str) -> str | None:
    """Return the numeric position id for a posting URL, else None.

    Eightfold links a posting two ways: as a path (``/careers/job/{id}``) and
    as a selection on the board (``/careers?pid={id}``). Both are postings.
    """
    parsed = _parsed(url)
    if parsed is None:
        return None

    match = _JOB_PATH_RE.match(parsed.path or "")
    if match:
        return match.group(1)

    if _CAREERS_PATH_RE.match(parsed.path or ""):
        values = parse_qs(parsed.query or "").get("pid") or []
        if values and _POSITION_ID_RE.match(values[0]):
            return values[0]
    return None


def is_eightfold_job_url(url: str) -> bool:
    return extract_position_id(url) is not None


def resolve_employer(employer: str) -> str | None:
    """Map an employer alias or board URL to a board URL.

    Accepts ``"microsoft"``, a full Eightfold URL, or a bare Eightfold
    hostname. Returns None when the value is neither.
    """
    value = (employer or "").strip()
    if not value:
        return None

    known = KNOWN_EMPLOYERS.get(value.casefold())
    if known:
        return known

    candidate = value if "://" in value else f"https://{value}"
    root = board_root(candidate)
    if root is None:
        return None
    parsed = urlparse(candidate)
    path = parsed.path or ""
    if _CAREERS_PATH_RE.match(path):
        return f"{root}{path}"
    return f"{root}/careers"
