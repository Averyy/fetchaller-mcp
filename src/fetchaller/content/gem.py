"""Gem job board GraphQL client and markdown renderer.

Exports URL detection (``is_gem_url``, ``extract_gem_params``), an async
GraphQL fetcher (``fetch_gem_job``), and a markdown renderer
(``render_gem_job``).

Gem (``jobs.gem.com/{board-slug}/{ext-id}``) is a pure SPA. Content loads
via an unauthenticated Apollo GraphQL endpoint at
``https://jobs.gem.com/api/public/graphql``. Dumps the response as-is
using Gem's own field names and enum values so we don't lose anything to
translation.
"""

import json
import re
from html import unescape
from urllib.parse import urlparse

from markdownify import markdownify

_GEM_HOST_RE = re.compile(r"^jobs\.gem\.com$")
_GEM_PATH_RE = re.compile(r"^/([a-z0-9][a-z0-9_-]*)/([A-Za-z0-9_-]{8,})/?$")
_GEM_BOARD_PATH_RE = re.compile(r"^/([a-z0-9][a-z0-9_-]*)/?$")


def is_gem_url(url: str) -> bool:
    return extract_gem_params(url) is not None


def extract_gem_params(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    if not _GEM_HOST_RE.match(hostname):
        return None
    m = _GEM_PATH_RE.match(parsed.path)
    if not m:
        return None
    return m.group(1), m.group(2)


def is_gem_board_url(url: str) -> bool:
    return extract_gem_board_slug(url) is not None


def extract_gem_board_slug(url: str) -> str | None:
    """Match board-index URLs like ``jobs.gem.com/{board-slug}`` (one path segment only)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    if not _GEM_HOST_RE.match(hostname):
        return None
    m = _GEM_BOARD_PATH_RE.match(parsed.path)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# GraphQL fetch
# ---------------------------------------------------------------------------

_GRAPHQL_URL = "https://jobs.gem.com/api/public/graphql"

_QUERY = """
query ExternalJobPostingQuery($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    id
    title
    descriptionHtml
    extId
    startDateTs
    firstPublishedTsSec
    companyUrl
    companyLogo
    isApplicationFormHidden
    isUnlistedExternally
    locations { id extId name city isoCountry isRemote }
    job {
      id locationType employmentType requisitionId teamDisplayName
      department { id extId name }
      locations { id extId name city isoCountry isRemote }
    }
    jobPostSectionHtml { introHtml outroHtml }
    compensationHtml
  }
  oatsJobPostFieldsAndQuestions(
    jobBoardVanityPath: $boardId
    jobPostExtId: $extId
  ) {
    fields { fieldType isRequired }
    questions {
      extId
      answerType
      displayType
      fileType
      text
      description
      isRequired
      options { extId value }
      numericRatingRange
    }
    demographicSurvey {
      extId
      surveyType
      questions {
        extId
        answerType
        displayType
        text
        description
        options { extId value }
      }
    }
  }
}
"""


async def fetch_gem_job(board_slug: str, ext_id: str, session) -> dict | None:
    payload = {
        "operationName": "ExternalJobPostingQuery",
        "variables": {"boardId": board_slug, "extId": ext_id},
        "query": _QUERY,
    }
    try:
        resp = await session.post(
            _GRAPHQL_URL,
            json=payload,
            headers={
                "content-type": "application/json",
                "apollographql-client-name": "jobBoards",
                "apollographql-client-version": "v2",
            },
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("errors"):
        return None
    return data.get("data") or None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
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
    text = (q.get("text") or "").strip()
    if not text:
        return None
    answer_type = q.get("answerType") or ""
    display_type = q.get("displayType") or ""
    # Show both when they differ, else whichever is present.
    if answer_type and display_type and answer_type != display_type:
        types = f"{answer_type}/{display_type}"
    else:
        types = answer_type or display_type or "input"
    if q.get("fileType"):
        types += f" ({q['fileType']})"
    req = "required" if q.get("isRequired") is True else "optional"
    line = f"- **{text}** ({types}, {req})"
    desc_html = q.get("description") or ""
    if desc_html:
        desc_md = _html_to_markdown(desc_html).strip()
        if desc_md:
            line += f"\n  - {desc_md}"
    opts = q.get("options") or []
    if opts:
        values = [o.get("value") for o in opts if o.get("value")]
        if values:
            line += f"\n  - Options: {', '.join(values)}"
    rng = q.get("numericRatingRange")
    if isinstance(rng, dict):
        mn, mx = rng.get("min"), rng.get("max")
        if mn is not None and mx is not None:
            line += f"\n  - Range: {mn} – {mx}"
    return line


def render_gem_job(data: dict, source_url: str | None = None) -> str:
    posting = data.get("oatsExternalJobPosting") or {}
    fq = data.get("oatsJobPostFieldsAndQuestions") or {}

    title = (posting.get("title") or "").strip() or "Job Posting"
    header = f"# {title}"

    # Metadata: dump every non-empty posting field with Gem's own key names.
    _skip = {"title", "descriptionHtml", "jobPostSectionHtml", "compensationHtml"}
    meta: list[str] = []
    for key, val in posting.items():
        if key in _skip:
            continue
        s = _stringify(val)
        if s:
            meta.append(f"- **{key}**: {s}")

    # Description: intro → body → outro → compensation, in Gem's natural order.
    description_blocks: list[str] = []
    section_html = posting.get("jobPostSectionHtml") or {}
    intro_md = _html_to_markdown(section_html.get("introHtml") or "")
    body_md = _html_to_markdown(posting.get("descriptionHtml") or "")
    outro_md = _html_to_markdown(section_html.get("outroHtml") or "")
    comp_md = _html_to_markdown(posting.get("compensationHtml") or "")
    if intro_md:
        description_blocks.append(intro_md)
    if body_md:
        description_blocks.append(body_md)
    if outro_md:
        description_blocks.append(outro_md)
    if comp_md:
        description_blocks.append("**compensationHtml**:\n\n" + comp_md)

    # Application fields: render the raw field-type list verbatim.
    base_fields = fq.get("fields") or []
    base_lines: list[str] = []
    for f in base_fields:
        ft = (f.get("fieldType") or "").strip()
        if not ft:
            continue
        req = "required" if f.get("isRequired") is True else "optional"
        base_lines.append(f"- **{ft}** ({req})")

    # Custom questions + demographic survey — each rendered in source order,
    # preserving Gem's surveyType label for the demographic block.
    question_lines: list[str] = []
    for q in fq.get("questions") or []:
        rendered = _render_question(q)
        if rendered:
            question_lines.append(rendered)

    demo_block: list[str] = []
    demographic = fq.get("demographicSurvey")
    if isinstance(demographic, dict):
        demo_questions = demographic.get("questions") or []
        if demo_questions:
            survey_type = (demographic.get("surveyType") or "demographicSurvey").strip()
            demo_block.append(f"## {survey_type}")
            demo_block.append("")
            for q in demo_questions:
                rendered = _render_question(q)
                if rendered:
                    demo_block.append(rendered)

    apply_url = source_url or ""
    parts: list[str] = [header, ""]
    if meta:
        parts.extend(meta)
        parts.append("")
    for block in description_blocks:
        parts.append(block)
        parts.append("")
    if base_lines:
        parts.append("## fields")
        parts.append("")
        parts.extend(base_lines)
        parts.append("")
    if question_lines:
        parts.append("## questions")
        parts.append("")
        parts.extend(question_lines)
        parts.append("")
    if demo_block:
        parts.extend(demo_block)
        parts.append("")
    if apply_url:
        parts.append(f"**sourceUrl**: {apply_url}")
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Board-index (Greenhouse-style REST API)
# ---------------------------------------------------------------------------

_BOARD_API_BASE = "https://api.gem.com/job_board/v0"


async def fetch_gem_board(board_slug: str, session) -> list[dict] | None:
    """Fetch the full job-board listing for ``jobs.gem.com/{board_slug}``.

    Returns the raw list of job posts (Greenhouse-shaped) or ``None`` on any
    error so callers can fall through to the normal HTML fetch.
    """
    try:
        resp = await session.get(f"{_BOARD_API_BASE}/{board_slug}/job_posts/")
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return data


def _gem_job_location(job: dict) -> str:
    loc = job.get("location")
    if isinstance(loc, dict):
        name = (loc.get("name") or "").strip()
        if name:
            return name
    offices = job.get("offices") or []
    if isinstance(offices, list):
        names = []
        for o in offices:
            if isinstance(o, dict):
                n = (o.get("name") or "").strip()
                if n:
                    names.append(n)
        if names:
            return ", ".join(names)
    return ""


def render_gem_board(jobs: list[dict], board_slug: str, source_url: str | None = None) -> str:
    """Render a Gem board listing as markdown grouped by department."""
    header = f"# {board_slug} — Job Board ({len(jobs)} open positions)"
    parts: list[str] = [header, ""]
    parts.append(f"**source**: {_BOARD_API_BASE}/{board_slug}/job_posts/")
    if source_url:
        parts.append(f"**boardUrl**: {source_url}")
    parts.append("")

    # Group by department name (using the first listed department per job).
    by_dept: dict[str, list[dict]] = {}
    for j in jobs:
        depts = j.get("departments") or []
        dept_name = "Other"
        if isinstance(depts, list) and depts and isinstance(depts[0], dict):
            dept_name = (depts[0].get("name") or "").strip() or "Other"
        by_dept.setdefault(dept_name, []).append(j)

    for dept in sorted(by_dept.keys()):
        dept_jobs = by_dept[dept]
        parts.append(f"## {dept} ({len(dept_jobs)})")
        parts.append("")
        for j in dept_jobs:
            title = (j.get("title") or "").strip() or "(untitled)"
            line = f"- **{title}**"
            details: list[str] = []
            loc = _gem_job_location(j)
            if loc:
                details.append(loc)
            ltype = (j.get("location_type") or "").strip()
            if ltype:
                details.append(ltype)
            etype = (j.get("employment_type") or "").strip()
            if etype:
                details.append(etype)
            if details:
                line += " — " + " · ".join(details)
            parts.append(line)
            url = (j.get("absolute_url") or "").strip()
            if url:
                parts.append(f"  - {url}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
