"""Employer registry for Oracle Recruiting Cloud tenants.

Each entry carries the employer's careers page — used to discover the Fusion
host live, since that hostname is deployment-controlled — and optionally a
pinned host as a fallback plus a non-default site number.

Only verified tenants are listed. Any ORC tenant works without being here:
callers may pass a Fusion host or a careers URL directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .api import DEFAULT_SITE_NUMBER

# A bare hostname, optionally with a path. Guards the "treat it as a careers
# URL" branch: without this, free text like "not a board" becomes
# "https://not a board" and would be fetched as if it were an employer's site.
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)


@dataclass(frozen=True)
class OracleEmployer:
    label: str
    careers_url: str
    # Last-known host. Discovery from `careers_url` wins; this only covers the
    # case where the careers page stops publishing it.
    fallback_host: str | None = None
    site_number: str = DEFAULT_SITE_NUMBER
    # Public posting URL template, keyed by requisition id.
    posting_url: str | None = None


KNOWN_EMPLOYERS: dict[str, OracleEmployer] = {
    # Oracle runs its own product. Note the site number: CX_45001, not the
    # CX_1 nearly every other deployment uses — which is why site_number has
    # to be per-employer rather than a constant.
    "oracle": OracleEmployer(
        label="Oracle",
        careers_url="https://careers.oracle.com/en/sites/jobsearch/jobs",
        fallback_host="https://eeho.fa.us2.oraclecloud.com",
        site_number="CX_45001",
        posting_url="https://careers.oracle.com/en/sites/jobsearch/job/{id}",
    ),
    "uber": OracleEmployer(
        label="Uber",
        careers_url="https://jobs.uber.com/en/jobs/",
        fallback_host="https://iaziqy.fa.ocs.oraclecloud.com",
        posting_url="https://jobs.uber.com/en/jobs/{id}",
    ),
}


def resolve_employer(employer: str) -> OracleEmployer | None:
    """Map an alias, Fusion host, or careers URL to an employer record."""
    value = (employer or "").strip()
    if not value:
        return None

    known = KNOWN_EMPLOYERS.get(value.casefold().replace(" ", "").replace("-", ""))
    if known:
        return known

    if "://" in value:
        candidate = value
    elif _HOSTNAME_RE.match(value):
        candidate = f"https://{value}"
    else:
        # Neither an alias, a URL, nor a hostname — reject rather than
        # fabricating a URL out of arbitrary text.
        return None

    if not candidate.lower().startswith(("http://", "https://")):
        return None

    if ".oraclecloud.com" in candidate:
        # A Fusion host needs no discovery step.
        host = candidate.split("/hcmRestApi", 1)[0].rstrip("/")
        return OracleEmployer(label=host.split("://", 1)[-1], careers_url=host, fallback_host=host)
    # An arbitrary careers page: discover the Fusion host from it.
    return OracleEmployer(label=candidate.split("://", 1)[-1], careers_url=candidate)
