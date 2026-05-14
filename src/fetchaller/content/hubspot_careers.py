"""HubSpot careers GraphQL client and markdown renderer.

HubSpot's career site (``https://www.hubspot.com/careers/jobs/{id}``) is a
JavaScript SPA — the rendered HTML contains only chrome and zero job content.
The page issues a single POST to ``https://wtcfns.hubspot.com/careers/graphql``
with operation ``Job(id: ID!)`` to fetch title, content, department, office,
application questions, and location questions. We hit that endpoint directly.

The ``?gh_jid=`` query parameter on these URLs is a vestigial Greenhouse
artifact: it carries HubSpot's own job id, not a Greenhouse one. The HubSpot
greenhouse board (token ``hubspot``) is empty, so the existing greenhouse
fallback would 404 and drop us into the empty-SPA HTML path. This module must
intercept BEFORE the greenhouse guess in the dispatcher.

Exports:
- ``is_hubspot_careers_url(url)``: URL detection
- ``extract_hubspot_job_id(url)``: parse job id from the path
- ``fetch_hubspot_job(job_id, session)``: GraphQL fetch
- ``render_hubspot_job(data, source_url)``: markdown renderer
"""

import json
import re
from html import unescape
from urllib.parse import urlparse

from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_JOB_PATH_RE = re.compile(r"^/careers/jobs/(\d+)/?$", re.I)


def is_hubspot_careers_url(url: str) -> bool:
    return extract_hubspot_job_id(url) is not None


def extract_hubspot_job_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    if hostname != "www.hubspot.com" and hostname != "hubspot.com":
        return None
    m = _JOB_PATH_RE.match(parsed.path)
    if not m:
        return None
    return m.group(1)


# ---------------------------------------------------------------------------
# GraphQL fetch
# ---------------------------------------------------------------------------

_API_URL = "https://wtcfns.hubspot.com/careers/graphql"

_QUERY = """query Job($id: ID!) {
  job(id: $id) {
    title
    id
    content
    department { id name }
    office { location id }
    questions {
      label
      description
      required
      fields {
        name
        type
        values { value label }
      }
    }
    location_questions {
      label
      required
      fields { name type }
    }
  }
}"""


async def fetch_hubspot_job(job_id: str, session) -> dict | None:
    body = {
        "operationName": "Job",
        "variables": {"id": str(job_id)},
        "query": _QUERY,
    }
    try:
        resp = await session.post(
            _API_URL,
            json=body,
            headers={
                "content-type": "application/json",
                "accept": "*/*",
                "referer": f"https://www.hubspot.com/careers/jobs/{job_id}",
            },
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    job = (payload.get("data") or {}).get("job")
    if not isinstance(job, dict):
        return None
    return job


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
    # HubSpot double-encodes the ``content`` field (``&lt;p&gt;`` etc.). Other
    # fields (``description`` on questions) are single-encoded. ``unescape`` is
    # idempotent for already-decoded HTML, so always run it.
    if "&lt;" in html or "&gt;" in html or "&amp;" in html or "&#" in html:
        html = unescape(html)
    html = re.sub(r"<p[^>]*>\s*(?:&nbsp;|\xa0)?\s*</p>", "", html)
    md = markdownify(
        html, heading_style="ATX", bullets="-",
        escape_asterisks=False, escape_underscores=False,
    )
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _render_question(q: dict) -> str | None:
    label = (q.get("label") or "").strip()
    if not label:
        return None
    req = "required" if q.get("required") else "optional"
    fields = q.get("fields") or []
    types = [f.get("type") for f in fields if f.get("type")]
    types_str = ", ".join(t for t in types if t) or "input"

    line = f"- **{label}** ({types_str}, {req})"
    desc = q.get("description")
    if desc:
        desc_md = _html_to_markdown(str(desc)).strip()
        if desc_md:
            line += f"\n  - {desc_md}"
    opts: list[str] = []
    for f in fields:
        for o in f.get("values") or []:
            if isinstance(o, dict) and o.get("label") is not None:
                opts.append(str(o["label"]))
    if opts:
        line += f"\n  - Options: {', '.join(opts)}"
    return line


def render_hubspot_job(data: dict, source_url: str | None = None) -> str:
    title = (data.get("title") or "").strip() or "Job Posting"
    header = f"# {title}"

    meta: list[str] = []
    job_id = data.get("id")
    if job_id is not None:
        meta.append(f"- **id**: {job_id}")
    dept = data.get("department") or {}
    if isinstance(dept, dict):
        dept_name = (dept.get("name") or "").strip()
        if dept_name:
            meta.append(f"- **department**: {dept_name}")
    office = data.get("office") or {}
    if isinstance(office, dict):
        loc = (office.get("location") or "").strip()
        if loc:
            meta.append(f"- **location**: {loc}")

    description_md = _html_to_markdown(data.get("content") or "")

    question_lines: list[str] = []
    for q in data.get("questions") or []:
        rendered = _render_question(q)
        if rendered:
            question_lines.append(rendered)

    location_question_lines: list[str] = []
    for q in data.get("location_questions") or []:
        rendered = _render_question(q)
        if rendered:
            location_question_lines.append(rendered)

    apply_url = source_url or (
        f"https://www.hubspot.com/careers/jobs/{job_id}" if job_id else ""
    )

    parts: list[str] = [header, ""]
    if meta:
        parts.extend(meta)
        parts.append("")
    if description_md:
        parts.append(description_md)
        parts.append("")
    if question_lines:
        parts.append("## questions")
        parts.append("")
        parts.extend(question_lines)
        parts.append("")
    if location_question_lines:
        parts.append("## location_questions")
        parts.append("")
        parts.extend(location_question_lines)
        parts.append("")
    if apply_url:
        parts.append(f"**sourceUrl**: {apply_url}")

    return "\n".join(parts).rstrip() + "\n"
