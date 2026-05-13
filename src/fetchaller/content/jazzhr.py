"""JazzHR job board client and markdown renderer.

JazzHR career pages live at ``{tenant}.applytojob.com/apply``. The
listing page is SSR'd with one ``<li class="list-group-item">`` per
posting (optionally preceded by a ``<div class="department-heading">``
for department groupings). The posting-detail page carries a full
``schema.org/JobPosting`` JSON-LD block with the description, salary,
location, employment type, and date posted — that's what we render.

This module exposes:

- ``is_jazzhr_url`` / ``extract_jazzhr_params`` — posting URL detection
  (``/apply/{8-char-id}/{slug}``).
- ``is_jazzhr_board_url`` / ``extract_jazzhr_tenant`` — board URL
  detection (``/apply`` or ``/apply/``).
- ``extract_jazzhr_embed_tenants`` — pull JazzHR tenant slugs from any
  HTML page (for white-label company sites that pull jobs from
  ``*.applytojob.com``).
- ``fetch_jazzhr_board`` — pulls the listing HTML and parses it.
- ``fetch_jazzhr_job`` — pulls the posting HTML and parses the JSON-LD
  block.
- ``render_jazzhr_board`` / ``render_jazzhr_job`` — markdown rendering
  using JobPosting's schema.org keys.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_JAZZHR_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)\.applytojob\.com$")
# Posting IDs are 8-12 char mixed-case alphanumerics; we accept 6-16 to be safe.
_JAZZHR_JOB_PATH_RE = re.compile(r"^/apply/([A-Za-z0-9]{6,16})(?:/([^/]+))?/?$")
_JAZZHR_BOARD_PATH_RE = re.compile(r"^/apply/?$")


def _tenant(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    m = _JAZZHR_HOST_RE.match(host)
    return m.group(1) if m else None


def extract_jazzhr_params(url: str) -> tuple[str, str] | None:
    """Return ``(tenant, posting_id)`` for a JazzHR posting URL."""
    tenant = _tenant(url)
    if not tenant:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        return None
    m = _JAZZHR_JOB_PATH_RE.match(path)
    if not m:
        return None
    return tenant, m.group(1)


def is_jazzhr_url(url: str) -> bool:
    return extract_jazzhr_params(url) is not None


def extract_jazzhr_tenant(url: str) -> str | None:
    """Return ``tenant`` for a JazzHR board URL."""
    tenant = _tenant(url)
    if not tenant:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        return None
    if not _JAZZHR_BOARD_PATH_RE.match(path):
        return None
    return tenant


def is_jazzhr_board_url(url: str) -> bool:
    return extract_jazzhr_tenant(url) is not None


# ---------------------------------------------------------------------------
# Embed detection (company sites that pull JazzHR job listings via JS)
# ---------------------------------------------------------------------------

_JAZZHR_EMBED_URL_RE = re.compile(
    r"https?://([a-z0-9][a-z0-9-]*)\.applytojob\.com/apply",
    re.IGNORECASE,
)


def extract_jazzhr_embed_tenants(html: str) -> list[str]:
    """Return JazzHR tenant slugs referenced in any HTML page, deduped/ordered.

    Used to detect white-label company career pages that load jobs from a
    JazzHR ``*.applytojob.com`` board via JS (e.g. EarthDaily, which pulls
    from both ``earthdaily.applytojob.com`` and
    ``earthdailyagro.applytojob.com``).
    """
    seen: dict[str, None] = {}
    for m in _JAZZHR_EMBED_URL_RE.finditer(html):
        tenant = m.group(1).lower()
        seen.setdefault(tenant, None)
    return list(seen)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


async def fetch_jazzhr_board(tenant: str, session) -> list[dict] | None:
    """Fetch the JazzHR listing page and parse it into job dicts.

    Returns a list of ``{title, posting_id, slug, location, department, url}``
    dicts or ``None`` on any error.
    """
    url = f"https://{tenant}.applytojob.com/apply"
    try:
        resp = await session.get(url)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    items: list[dict] = []
    current_dept = ""
    for li in soup.select(".list-group .list-group-item"):
        dept_h3 = li.select_one(".department-heading h3")
        if dept_h3:
            current_dept = dept_h3.get_text(strip=True)
        a = li.select_one("h3.list-group-item-heading a")
        if not a:
            continue
        href = (a.get("href") or "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        # Extract posting id + slug from href
        path = urlparse(href).path
        m = _JAZZHR_JOB_PATH_RE.match(path)
        posting_id = m.group(1) if m else ""
        slug = (m.group(2) or "") if m else ""

        loc_el = li.select_one(".list-group-item-text")
        location = ""
        if loc_el:
            loc_text = loc_el.get_text(" ", strip=True)
            # Trim trailing department name (JazzHR templates append it)
            if current_dept and loc_text.endswith(current_dept):
                loc_text = loc_text[: -len(current_dept)].strip()
            location = loc_text

        items.append({
            "title": title,
            "posting_id": posting_id,
            "slug": slug,
            "department": current_dept,
            "location": location,
            "url": href,
        })
    return items


async def fetch_jazzhr_job(tenant: str, posting_id: str, session) -> dict | None:
    """Fetch a JazzHR posting page and return its JSON-LD JobPosting.

    Returns a dict with ``{jobPosting: <schema.org dict>, pageTitle, tenant,
    postingId}`` or ``None`` on any error.
    """
    # We don't know the slug from posting id alone; JazzHR happily 302s to the
    # canonical slug URL when we request /apply/{id} with no slug. The slug is
    # cosmetic in the URL but the page works the same either way.
    url = f"https://{tenant}.applytojob.com/apply/{posting_id}"
    try:
        resp = await session.get(url)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    job = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                job = item
                break
        if job:
            break
    if not job:
        return None
    title_el = soup.find("title")
    page_title = title_el.get_text(strip=True) if title_el else ""
    return {
        "jobPosting": job,
        "pageTitle": page_title,
        "tenant": tenant,
        "postingId": posting_id,
    }


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


# Keys whose values are rendered specially elsewhere.
_POSTING_SKIP = frozenset({
    "@context", "@type",
    "description",  # rendered as the body
    "title",        # rendered as the H1
})


def render_jazzhr_job(payload: dict, source_url: str | None = None) -> str:
    job = payload.get("jobPosting") or {}
    title = (job.get("title") or "").strip()
    parts: list[str] = [f"# {title}" if title else "# Job Posting", ""]

    meta: list[str] = []
    for key, value in job.items():
        if key in _POSTING_SKIP:
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

    if source_url:
        parts.append(f"**sourceUrl**: {source_url}")

    return "\n".join(parts).rstrip() + "\n"


def render_jazzhr_board(
    jobs: list[dict], tenant: str, source_url: str | None = None
) -> str:
    header = f"# {tenant} — Job Board ({len(jobs)} open positions)"
    parts: list[str] = [header, ""]
    parts.append(f"- **tenant**: {tenant}")
    if source_url:
        parts.append(f"- **boardUrl**: {source_url}")
    parts.append("")

    by_dept: dict[str, list[dict]] = {}
    for j in jobs:
        dept = (j.get("department") or "").strip() or "Other"
        by_dept.setdefault(dept, []).append(j)

    for dept in sorted(by_dept):
        parts.append(f"## {dept}")
        parts.append("")
        for j in by_dept[dept]:
            title = (j.get("title") or "").strip() or "(untitled)"
            line = f"- **{title}**"
            loc = (j.get("location") or "").strip()
            if loc:
                line += f" — {loc}"
            parts.append(line)
            jurl = (j.get("url") or "").strip()
            if jurl:
                parts.append(f"  - {jurl}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def render_jazzhr_boards(boards: list[tuple[str, list[dict]]], source_url: str | None = None) -> str:
    """Render multiple JazzHR boards as a single document.

    Used when a white-label page (e.g. earthdaily.com/job-openings) pulls
    jobs from more than one tenant.
    """
    total = sum(len(jobs) for _, jobs in boards)
    parts: list[str] = [
        f"# Job Board ({total} open positions across {len(boards)} JazzHR sites)",
        "",
    ]
    if source_url:
        parts.append(f"- **boardUrl**: {source_url}")
    parts.append("")

    for tenant, jobs in boards:
        sub = render_jazzhr_board(jobs, tenant)
        # Drop the duplicate H1 from the sub-render; demote second-level
        # headers to H3 inside the combined doc.
        sub_lines = sub.splitlines()
        if sub_lines and sub_lines[0].startswith("# "):
            sub_lines = sub_lines[1:]
        rebuilt = []
        for line in sub_lines:
            if line.startswith("## "):
                rebuilt.append("### " + line[3:])
            else:
                rebuilt.append(line)
        parts.append(f"## {tenant} ({len(jobs)} positions)")
        parts.append("")
        parts.extend(rebuilt)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
