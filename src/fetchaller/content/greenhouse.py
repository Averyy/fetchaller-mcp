"""Greenhouse job board API client and markdown renderer.

Exports URL detection (``is_greenhouse_url``, ``extract_greenhouse_params``),
HTML-embed detection for company career sites
(``is_greenhouse_html``, ``extract_greenhouse_params_from_html``,
``extract_greenhouse_params_guess``), an async API fetcher
(``fetch_greenhouse_job``), and a markdown renderer
(``render_greenhouse_job``).

Fetches ``https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true``
and dumps the JSON as-is — raw field names, raw values, no translation.
Each company exposes different metadata fields and uses different labels,
so the renderer preserves what the company actually sent.
"""

import json
import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_BOARDS_HOSTS = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})

_JOB_PATH_RE = re.compile(r"^/([a-z0-9][a-z0-9_-]*)/jobs/(\d+)/?$", re.I)
_EMBED_PATH_RE = re.compile(r"^/embed/job_app/?$", re.I)
_COMPANY_JOB_PATH_RE = re.compile(r"/(?:[a-z]{2}(?:-[A-Z]{2})?/)?jobs/(\d{4,})(?:/|$)", re.I)

_CAREERS_TLDS = frozenset({"jobs", "careers", "com", "co", "io", "ai"})


def is_greenhouse_url(url: str) -> bool:
    return extract_greenhouse_params(url) is not None


def extract_greenhouse_params(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if hostname in _BOARDS_HOSTS:
        if _EMBED_PATH_RE.match(parsed.path):
            token = (query.get("for") or [""])[0].strip()
            job_id = (query.get("token") or [""])[0].strip()
            if token and job_id.isdigit():
                return token, job_id
            return None
        m = _JOB_PATH_RE.match(parsed.path)
        if m:
            return m.group(1), m.group(2)
        return None
    gh_jid = (query.get("gh_jid") or [""])[0].strip()
    gh_src = (query.get("gh_src") or [""])[0].strip()
    if gh_jid.isdigit() and gh_src:
        return gh_src, gh_jid
    return None


def extract_greenhouse_params_guess(url: str) -> tuple[str, str] | None:
    """Hostname-derived guess for company-site URLs like ``dropbox.jobs/en/jobs/{id}?gh_src=X``."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in _BOARDS_HOSTS:
        return None
    query = parse_qs(parsed.query)
    if not ("gh_src" in query or "gh_jid" in query or "gh_aid" in query):
        return None
    m = _COMPANY_JOB_PATH_RE.search(parsed.path)
    if not m:
        return None
    job_id = m.group(1)
    labels = hostname.split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    while len(labels) > 1 and labels[-1] in _CAREERS_TLDS:
        labels.pop()
    if len(labels) >= 2 and labels[0] in {"careers", "jobs"}:
        labels = labels[1:]
    if not labels:
        return None
    token = labels[-1].lower()
    return (token, job_id) if token else None


# ---------------------------------------------------------------------------
# HTML-embed detection
# ---------------------------------------------------------------------------


def is_greenhouse_html(soup: BeautifulSoup) -> bool:
    for iframe in soup.find_all("iframe"):
        if "boards.greenhouse.io/embed" in (iframe.get("src") or ""):
            return True
    if soup.find(id="grnhse_app") is not None:
        return True
    for script in soup.find_all("script", src=True):
        if "boards.greenhouse.io/embed" in script["src"]:
            return True
    return False


def extract_greenhouse_params_from_html(
    soup: BeautifulSoup, page_url: str | None = None
) -> tuple[str, str] | None:
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        if "boards.greenhouse.io/embed" not in src:
            continue
        try:
            q = parse_qs(urlparse(src).query)
        except Exception:
            continue
        token = (q.get("for") or [""])[0].strip()
        job_id = (q.get("token") or [""])[0].strip()
        if token and job_id.isdigit():
            return token, job_id
    gh_div = soup.find(id="grnhse_app")
    if gh_div is not None:
        token = (gh_div.get("data-src") or gh_div.get("data-token-for") or "").strip()
        job_id = (gh_div.get("data-token") or gh_div.get("data-job-id") or "").strip()
        if token and job_id.isdigit():
            return token, job_id
    if page_url:
        return extract_greenhouse_params(page_url)
    return None


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

_API_BASE = "https://boards-api.greenhouse.io/v1/boards"


async def fetch_greenhouse_job(board_token: str, job_id: str, session) -> dict | None:
    url = f"{_API_BASE}/{board_token}/jobs/{job_id}?questions=true"
    try:
        resp = await session.get(url)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        return json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
    # Greenhouse double-encodes content; unescape once if we see entity refs.
    if "&lt;" in html or "&gt;" in html:
        html = unescape(html)
    html = re.sub(r"<p[^>]*>\s*(?:&nbsp;|\xa0)?\s*</p>", "", html)
    md = markdownify(
        html, heading_style="ATX", bullets="-",
        escape_asterisks=False, escape_underscores=False,
    )
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _stringify(value) -> str:
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        parts: list[str] = []
        for v in value:
            if isinstance(v, dict) and "name" in v:
                parts.append(str(v["name"]))
            elif isinstance(v, dict):
                parts.append(json.dumps(v, ensure_ascii=False))
            else:
                parts.append(str(v))
        return "; ".join(parts)
    if isinstance(value, dict):
        if "name" in value:
            return str(value["name"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_question(q: dict) -> str | None:
    label = (q.get("label") or q.get("title") or "").strip()
    if not label:
        return None
    req = "required" if q.get("required") is True else "optional"
    fields = q.get("fields") or []
    # Gather field types
    types = [f.get("type") for f in fields if f.get("type")]
    if not types and q.get("type"):
        types = [q["type"]]
    types_str = ", ".join(t for t in types if t) or "input"

    line = f"- **{label}** ({types_str}, {req})"
    desc = q.get("description")
    if desc:
        desc_md = _html_to_markdown(str(desc)).strip()
        if desc_md:
            line += f"\n  - {desc_md}"
    # Options across any select fields + top-level answer_options
    opts: list[str] = []
    for f in fields:
        for o in f.get("values") or []:
            if isinstance(o, dict) and o.get("label") is not None:
                opts.append(str(o["label"]))
    for o in q.get("answer_options") or []:
        if isinstance(o, dict) and o.get("label") is not None:
            opts.append(str(o["label"]))
    if opts:
        line += f"\n  - Options: {', '.join(opts)}"
    return line


def render_greenhouse_job(data: dict, source_url: str | None = None) -> str:
    title = (data.get("title") or "").strip() or "Job Posting"
    company = (data.get("company_name") or "").strip()
    header = f"# {title} @ {company}" if company else f"# {title}"

    # Metadata: dump every scalar / simple field from the API response with
    # Greenhouse's exact key names. Questions, content, and nested form data
    # are rendered as their own sections below.
    _section_keys = {
        "content", "questions", "location_questions",
        "demographic_questions", "data_compliance",
    }
    meta: list[str] = []
    for key in (
        "company_name", "title", "absolute_url", "id", "internal_job_id",
        "requisition_id", "first_published", "updated_at", "language",
        "location", "offices", "departments", "education", "employment",
        "compliance",
    ):
        if key == "title":
            continue
        if key not in data:
            continue
        val = _stringify(data.get(key))
        if val:
            meta.append(f"- **{key}**: {val}")

    # metadata array — Greenhouse's own key:value pairs (per-company custom fields)
    for item in data.get("metadata") or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        value = item.get("value")
        # Skip only true null / empty; preserve False for yes/no fields.
        if value is None or value == "" or value == [] or value == "None":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = _stringify(value)
        if rendered:
            meta.append(f"- **metadata.{name}**: {rendered}")

    # Any other top-level keys we haven't rendered — dump them so nothing is
    # silently dropped as the API evolves.
    rendered_keys = _section_keys | {
        "title", "company_name", "absolute_url", "id", "internal_job_id",
        "requisition_id", "first_published", "updated_at", "language",
        "location", "offices", "departments", "education", "employment",
        "compliance", "metadata",
    }
    for key, val in data.items():
        if key in rendered_keys:
            continue
        s = _stringify(val)
        if s:
            meta.append(f"- **{key}**: {s}")

    # Description HTML → markdown (preserve the company's own heading levels).
    description_md = _html_to_markdown(data.get("content") or "")

    # Application questions — flat, in source order.
    question_lines: list[str] = []
    for q in data.get("questions") or []:
        rendered = _render_question(q)
        if rendered:
            question_lines.append(rendered)
    for q in data.get("location_questions") or []:
        rendered = _render_question(q)
        if rendered:
            question_lines.append(rendered)

    # Demographic block — preserve Greenhouse's own header / description HTML.
    demo = data.get("demographic_questions") or {}
    demo_header_md = _html_to_markdown(demo.get("header") or "")
    demo_desc_md = _html_to_markdown(demo.get("description") or "")
    demo_questions = demo.get("questions") or []
    demo_lines: list[str] = []
    if demo_questions:
        if demo_header_md:
            demo_lines.append(demo_header_md)
        if demo_desc_md:
            demo_lines.append(demo_desc_md)
        for q in demo_questions:
            rendered = _render_question(q)
            if rendered:
                demo_lines.append(rendered)

    # Data compliance — render each entry raw.
    compliance_lines: list[str] = []
    for item in data.get("data_compliance") or []:
        if not isinstance(item, dict):
            continue
        compliance_lines.append(f"- {json.dumps(item, ensure_ascii=False)}")

    apply_url = source_url or data.get("absolute_url") or ""

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
    if demo_lines:
        parts.append("## demographic_questions")
        parts.append("")
        parts.extend(demo_lines)
        parts.append("")
    if compliance_lines:
        parts.append("## data_compliance")
        parts.append("")
        parts.extend(compliance_lines)
        parts.append("")
    if apply_url:
        parts.append(f"**sourceUrl**: {apply_url}")

    return "\n".join(parts).rstrip() + "\n"
