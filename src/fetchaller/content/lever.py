"""Lever job board API client and markdown renderer.

Exports URL detection (``is_lever_url``, ``extract_lever_params``), an async
fetch-and-parse entrypoint (``fetch_lever_job``), and a markdown renderer
(``render_lever_job``).

Fetches ``https://api.lever.co/v0/postings/{company}/{id}?mode=json`` for
posting metadata + description, then parses the ``/apply`` page's
``.application-question`` markup for the application form. Dumps the data
using Lever's own key names and section titles.
"""

import json
import re
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_LEVER_HOST_RE = re.compile(r"^jobs\.lever\.co$")
_LEVER_PATH_RE = re.compile(
    r"^/([a-z0-9][a-z0-9_-]*)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:/(?:apply|thanks))?/?$",
    re.I,
)


def is_lever_url(url: str) -> bool:
    return extract_lever_params(url) is not None


def extract_lever_params(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    if not _LEVER_HOST_RE.match(hostname):
        return None
    m = _LEVER_PATH_RE.match(parsed.path)
    if not m:
        return None
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

_API_BASE = "https://api.lever.co/v0/postings"


async def fetch_lever_job(company: str, job_id: str, session) -> dict | None:
    try:
        resp = await session.get(f"{_API_BASE}/{company}/{job_id}?mode=json")
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        posting = json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return None
    form_sections: list[dict] = []
    try:
        apply_resp = await session.get(f"https://jobs.lever.co/{company}/{job_id}/apply")
        if apply_resp.status_code == 200:
            form_sections = parse_lever_apply_form(apply_resp.text)
    except Exception:
        pass
    return {"posting": posting, "form": form_sections}


# ---------------------------------------------------------------------------
# Apply-form parser: dumps each question in document order, grouped by Lever's
# actual section markers (``.section.application-form`` blocks).
# ---------------------------------------------------------------------------


def _clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_lever_apply_form(html: str) -> list[dict]:
    """Parse /apply page into a list of section dicts.

    Each section dict: ``{title, questions: [{label, field_types, required, options, description}]}``
    where ``title`` is taken from any ``<h4>`` / ``.section-header`` inside
    the section block, defaulting to the section's CSS class if present
    (e.g. ``eeo-section``) or an empty string for the main application form.
    """
    soup = BeautifulSoup(html, "lxml")
    sections: list[dict] = []
    seen_labels: set[str] = set()

    section_blocks = soup.select(".application-form")
    if not section_blocks:
        section_blocks = [soup]

    for block in section_blocks:
        classes = set(block.get("class") or [])
        # Title: prefer an explicit header inside the section; fall back to
        # a known class suffix (``eeo-section`` → ``eeo``).
        header_el = block.select_one("h4, .section-header, .application-page-header")
        title = _clean_ws(header_el.get_text(" ")) if header_el else ""
        if not title:
            for cls in classes:
                if cls.endswith("-section"):
                    title = cls[: -len("-section")]
                    break

        questions = []
        for q in block.select(".application-question"):
            q_classes = set(q.get("class") or [])
            label_el = q.select_one(".application-label")
            if label_el is None:
                continue
            label_clone = BeautifulSoup(str(label_el), "lxml").select_one(".application-label")
            for junk in label_clone.select(
                "ul, select, textarea, input, .application-field, .application-answer-alternative"
            ):
                junk.decompose()
            for req_mark in label_clone.select(".required"):
                req_mark.decompose()
            label_text = _clean_ws(label_clone.get_text(" "))
            if not label_text or label_text in seen_labels:
                continue
            seen_labels.add(label_text)

            # Field types: gather from .application-label class hints + actual inputs.
            label_classes = set(label_el.get("class") or [])
            type_hints: list[str] = []
            for cls in label_classes:
                if cls not in {"application-label", "full-width"}:
                    type_hints.append(cls)
            if "resume" in q_classes:
                type_hints.insert(0, "resume")
            # Inputs inside .application-field
            for inp in q.select("input, textarea, select"):
                tag = inp.name
                if tag == "input":
                    t = (inp.get("type") or "").lower()
                    if t in {"hidden", "submit", "button"}:
                        continue
                    type_hints.append(f"input[{t or 'text'}]")
                else:
                    type_hints.append(tag)
            # Dedupe, preserve order
            _seen = set()
            type_hints = [t for t in type_hints if not (t in _seen or _seen.add(t))]
            # `.get("class")` on a bs4 Tag returns the class list (multi-valued
            # attr). The previous code wrapped it in a second `" ".join(...)`,
            # which exploded the joined string into space-separated *characters*,
            # so "required-field" never matched and required fields rendered as
            # optional. Test list membership directly.
            field_el = q.select_one(".application-field")
            field_classes = (field_el.get("class") if field_el is not None else None) or []
            required = (
                label_el.find(class_="required") is not None
                or "required-field" in field_classes
            )

            options: list[str] = []
            for opt in q.select("select option"):
                txt = _clean_ws(opt.get_text(" "))
                if txt and txt.lower() not in {"select ...", "select..."}:
                    options.append(txt)
            for alt in q.select(".application-answer-alternative"):
                txt = _clean_ws(alt.get_text(" "))
                if txt and txt not in options:
                    options.append(txt)

            description = ""
            desc_el = q.select_one(".application-description, .application-label .description")
            if desc_el is not None:
                description = _clean_ws(desc_el.get_text(" "))

            questions.append({
                "label": label_text,
                "field_types": type_hints,
                "required": bool(required),
                "options": options,
                "description": description,
            })

        if questions:
            sections.append({"title": title, "questions": questions})

    return sections


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
        return "; ".join(_stringify(v) for v in value if _stringify(v))
    if isinstance(value, dict):
        if "name" in value:
            return str(value["name"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_lever_job(data: dict, source_url: str | None = None) -> str:
    posting = data.get("posting") or {}
    form_sections = data.get("form") or []

    title = (posting.get("text") or "").strip() or "Job Posting"
    hosted = posting.get("hostedUrl") or ""
    company_slug = ""
    m = re.match(r"https?://jobs\.lever\.co/([^/]+)/", hosted)
    if m:
        company_slug = m.group(1)
    header = f"# {title}"

    # Metadata: dump every non-empty top-level scalar using Lever's own keys.
    _skip = {
        "text", "description", "descriptionBody", "descriptionPlain",
        "descriptionBodyPlain", "opening", "openingPlain", "lists", "additional",
        "additionalPlain",
    }
    meta: list[str] = []
    if company_slug:
        meta.append(f"- **company**: {company_slug}")
    for key in (
        "categories", "workplaceType", "country", "createdAt",
        "hostedUrl", "applyUrl", "id",
    ):
        if key not in posting:
            continue
        val = _stringify(posting[key])
        if val:
            meta.append(f"- **{key}**: {val}")
    # Surface any other top-level fields so new Lever fields aren't silently lost.
    for key, val in posting.items():
        if key in _skip or key in {
            "categories", "workplaceType", "country", "createdAt",
            "hostedUrl", "applyUrl", "id",
        }:
            continue
        s = _stringify(val)
        if s:
            meta.append(f"- **{key}**: {s}")

    # Description blocks — in Lever's natural order.
    body_blocks: list[str] = []
    opening_md = _html_to_markdown(posting.get("opening") or "")
    body_md = _html_to_markdown(posting.get("descriptionBody") or posting.get("description") or "")
    if opening_md:
        body_blocks.append(opening_md)
    if body_md:
        body_blocks.append(body_md)

    for lst in posting.get("lists") or []:
        lst_title = (lst.get("text") or "").strip()
        content_html = lst.get("content") or ""
        if not lst_title and not content_html:
            continue
        block = ""
        if lst_title:
            block = f"### {lst_title}\n\n"
        if content_html.startswith("<li"):
            content_html = f"<ul>{content_html}</ul>"
        rendered = _html_to_markdown(content_html)
        if rendered:
            block += rendered
        if block.strip():
            body_blocks.append(block.strip())

    additional_md = _html_to_markdown(posting.get("additional") or "")
    if additional_md:
        body_blocks.append(additional_md)

    # Form: preserve Lever's own section groupings.
    form_lines: list[str] = []
    if form_sections:
        form_lines.append("## application form")
        form_lines.append("")
        for section in form_sections:
            title_s = (section.get("title") or "").strip()
            if title_s:
                form_lines.append(f"### {title_s}")
                form_lines.append("")
            for q in section.get("questions") or []:
                types_str = ", ".join(q.get("field_types") or []) or "input"
                req = "required" if q.get("required") is True else "optional"
                line = f"- **{q['label']}** ({types_str}, {req})"
                if q.get("description"):
                    line += f"\n  - {q['description']}"
                if q.get("options"):
                    line += f"\n  - Options: {', '.join(q['options'])}"
                form_lines.append(line)
            form_lines.append("")
        while form_lines and not form_lines[-1]:
            form_lines.pop()

    apply_url = source_url or posting.get("applyUrl") or posting.get("hostedUrl") or ""

    parts: list[str] = [header, ""]
    if meta:
        parts.extend(meta)
        parts.append("")
    for block in body_blocks:
        parts.append(block)
        parts.append("")
    if form_lines:
        parts.extend(form_lines)
        parts.append("")
    if apply_url:
        parts.append(f"**sourceUrl**: {apply_url}")
    return "\n".join(parts).rstrip() + "\n"
