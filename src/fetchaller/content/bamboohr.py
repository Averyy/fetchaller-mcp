"""BambooHR job board client and markdown renderer.

BambooHR careers pages (``{tenant}.bamboohr.com/careers``) are SPAs, but
two JSON endpoints expose everything we need without auth:

- Board listing: ``GET /careers/list`` returns
  ``{meta:{totalCount}, result:[{id, jobOpeningName, departmentId,
  departmentLabel, employmentStatusLabel, location, atsLocation,
  isRemote, locationType}]}``.
- Posting detail: ``GET /careers/{id}/detail`` returns
  ``{meta:{}, result:{jobOpening:{...full posting with description...}}}``.

Companies also embed the BambooHR board on their own marketing sites via
``<div id="BambooHR" data-domain="{tenant}.bamboohr.com">``. This module
exposes detection helpers for both forms.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_BAMBOO_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)\.bamboohr\.com$")
_BAMBOO_JOB_PATH_RE = re.compile(r"^/careers/(\d+)(?:/[^/]*)?/?$")
_BAMBOO_BOARD_PATH_RE = re.compile(r"^/careers/?$")


def _tenant(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    m = _BAMBOO_HOST_RE.match(host)
    return m.group(1) if m else None


def extract_bamboohr_params(url: str) -> tuple[str, str] | None:
    """Return ``(tenant, job_id)`` for a BambooHR posting URL."""
    tenant = _tenant(url)
    if not tenant:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        return None
    m = _BAMBOO_JOB_PATH_RE.match(path)
    if not m:
        return None
    return tenant, m.group(1)


def is_bamboohr_url(url: str) -> bool:
    return extract_bamboohr_params(url) is not None


def extract_bamboohr_board_params(url: str) -> str | None:
    """Return ``tenant`` for a BambooHR board URL."""
    tenant = _tenant(url)
    if not tenant:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        return None
    if not _BAMBOO_BOARD_PATH_RE.match(path):
        return None
    return tenant


def is_bamboohr_board_url(url: str) -> bool:
    return extract_bamboohr_board_params(url) is not None


# ---------------------------------------------------------------------------
# HTML embed detection (company sites carrying <div id="BambooHR" data-domain=...>)
# ---------------------------------------------------------------------------

_BAMBOO_EMBED_DOMAIN_RE = re.compile(
    r'<div[^>]+id\s*=\s*"BambooHR"[^>]+data-domain\s*=\s*"([a-z0-9][a-z0-9-]*\.bamboohr\.com)"',
    re.IGNORECASE,
)


def extract_bamboohr_embed_tenant(html: str) -> str | None:
    """Return the BambooHR tenant slug if the page embeds the BambooHR widget."""
    m = _BAMBOO_EMBED_DOMAIN_RE.search(html)
    if not m:
        return None
    host = m.group(1).lower()
    sub = host.split(".bamboohr.com")[0]
    return sub or None


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


async def fetch_bamboohr_board(tenant: str, session) -> dict | None:
    """Fetch the full board listing for ``{tenant}.bamboohr.com/careers``.

    Returns the raw API payload ``{"meta": {...}, "result": [...]}`` or
    ``None`` on any error.
    """
    url = f"https://{tenant}.bamboohr.com/careers/list"
    try:
        resp = await session.get(url, headers={"accept": "application/json"})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        return None
    return data


async def fetch_bamboohr_job(tenant: str, job_id: str, session) -> dict | None:
    """Fetch a single posting via ``/careers/{id}/detail``.

    Returns the raw ``{"meta": {...}, "result": {"jobOpening": {...}}}``
    payload or ``None`` on any error.
    """
    url = f"https://{tenant}.bamboohr.com/careers/{job_id}/detail"
    try:
        resp = await session.get(url, headers={"accept": "application/json"})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("jobOpening"), dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_HTML_NBSP_P_RE = re.compile(r"<p[^>]*>\s*(?:&nbsp;|\xa0)?\s*</p>")
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
    cleaned = _HTML_NBSP_P_RE.sub("", html)
    md = markdownify(
        cleaned, heading_style="ATX", bullets="-",
        escape_asterisks=False, escape_underscores=False,
    )
    return _BLANK_LINE_COLLAPSE_RE.sub("\n\n", md).strip()


def _stringify(value) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        rendered = []
        for v in value:
            s = _stringify(v)
            if s:
                rendered.append(s)
        return "; ".join(rendered)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_POSTING_SKIP = frozenset({
    "description",          # rendered as the description body
    "jobOpeningName",       # rendered as the H1
    "additionalInformation",  # rendered as its own section
})


def _format_location(loc) -> str:
    if not isinstance(loc, dict):
        return ""
    bits = []
    for k in ("city", "state", "province", "postalCode", "addressCountry", "country"):
        v = loc.get(k)
        if isinstance(v, str) and v.strip():
            bits.append(v.strip())
    return ", ".join(bits)


def render_bamboohr_job(payload: dict, source_url: str | None = None) -> str:
    job = (payload.get("result") or {}).get("jobOpening") or {}
    title = (job.get("jobOpeningName") or "").strip()
    parts: list[str] = [f"# {title}" if title else "# Job Posting", ""]

    meta: list[str] = []
    for key, value in job.items():
        if key in _POSTING_SKIP:
            continue
        if key in ("location", "atsLocation"):
            formatted = _format_location(value)
            if formatted:
                meta.append(f"- **{key}**: {formatted}")
            continue
        s = _stringify(value)
        if s:
            meta.append(f"- **{key}**: {s}")
    if meta:
        parts.extend(meta)
        parts.append("")

    desc_md = _html_to_markdown(job.get("description") or "")
    if desc_md:
        parts.append("## description")
        parts.append("")
        parts.append(desc_md)
        parts.append("")

    addl_md = _html_to_markdown(job.get("additionalInformation") or "")
    if addl_md:
        parts.append("## additionalInformation")
        parts.append("")
        parts.append(addl_md)
        parts.append("")

    if source_url:
        parts.append(f"**sourceUrl**: {source_url}")

    return "\n".join(parts).rstrip() + "\n"


def _strip_html(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return BeautifulSoup(value, "lxml").get_text(" ", strip=True)


def render_bamboohr_board(payload: dict, tenant: str, source_url: str | None = None) -> str:
    jobs = payload.get("result") or []
    total = (payload.get("meta") or {}).get("totalCount") or len(jobs)

    header = f"# {tenant} — Job Board ({total} open positions)"
    parts: list[str] = [header, ""]
    parts.append(f"- **tenant**: {tenant}")
    if source_url:
        parts.append(f"- **boardUrl**: {source_url}")
    parts.append("")

    # Group by departmentLabel
    by_dept: dict[str, list[dict]] = {}
    for j in jobs:
        if not isinstance(j, dict):
            continue
        dept = (j.get("departmentLabel") or "").strip() or "Other"
        by_dept.setdefault(dept, []).append(j)

    for dept in sorted(by_dept):
        parts.append(f"## {dept}")
        parts.append("")
        for j in by_dept[dept]:
            title = (j.get("jobOpeningName") or "").strip() or "(untitled)"
            jid = j.get("id")
            line = f"- **{title}**"
            details: list[str] = []
            loc = _format_location(j.get("atsLocation") or {}) or _format_location(j.get("location") or {})
            if loc:
                details.append(loc)
            emp = (j.get("employmentStatusLabel") or "").strip()
            if emp:
                details.append(emp)
            if details:
                line += " — " + " · ".join(details)
            parts.append(line)
            if jid is not None:
                parts.append(f"  - https://{tenant}.bamboohr.com/careers/{jid}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
