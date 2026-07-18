"""Ashby job board cleanup and structured extraction.

Exports the standard site interface (SELECTORS_LIST, is_ashby,
extract_ashby_data, postprocess_ashby) plus embed URL resolution
(is_ashby_embed_url, resolve_ashby_embed_url,
extract_ashby_embed_slug_from_html).

Ashby job pages (jobs.ashbyhq.com) are React SPAs — the rendered body
is just a spinner. All posting data lives in ``window.__appData.posting``.
This module dumps that data as-is (raw field names, raw values) so the
LLM can see every field the platform exposes without us translating or
reordering.

Companies also embed the Ashby job board on their own marketing sites
via ``<div id="ashby_embed">`` and a ``?ashby_jid=<uuid>`` deep link.
resolve_ashby_embed_url() turns those URLs into the canonical
``jobs.ashbyhq.com/{org}/{jid}`` form by extracting the org slug from
the page's JS bundle, cached per hostname.
"""

import json
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

from ..security.ssrf import is_private_host

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_ASHBY_HOST_RE = re.compile(r"^jobs\.ashbyhq\.com$")
_ASHBY_BOARD_PATH_RE = re.compile(r"^/([a-z0-9][a-z0-9_-]*)/?$")


def is_ashby(url: str) -> bool:
    """Check if URL is an Ashby job board posting."""
    hostname = (urlparse(url).hostname or "").lower()
    return bool(_ASHBY_HOST_RE.match(hostname))


def is_ashby_board_url(url: str) -> bool:
    return extract_ashby_board_slug(url) is not None


def extract_ashby_board_slug(url: str) -> str | None:
    """Match Ashby board-index URLs like ``jobs.ashbyhq.com/{org}`` (one path segment only).

    Individual postings have two path segments (``/{org}/{uuid}``) and won't match.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    if not _ASHBY_HOST_RE.match(hostname):
        return None
    m = _ASHBY_BOARD_PATH_RE.match(parsed.path)
    return m.group(1) if m else None


SELECTORS_LIST: list[str] = []


# ---------------------------------------------------------------------------
# Structured data extraction (runs BEFORE scripts are decomposed)
# ---------------------------------------------------------------------------

_MARKER = "__ASHBY_MARKER__"
_MARKER_RE = re.compile(r"__ASHBY_MARKER__([\s\S]*?)__ASHBY_MARKER__")
_NL_TOKEN = "__ASHBY_NL__"
_APP_DATA_ASSIGN_RE = re.compile(r"window\.__appData\s*=\s*")


def _parse_app_data(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script"):
        text = script.string
        if not text or "__appData" not in text:
            continue
        m = _APP_DATA_ASSIGN_RE.search(text)
        if not m:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[m.end():].lstrip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _parse_jsonld_jobposting(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _html_to_markdown(html: str) -> str:
    """Convert HTML to markdown, preserving the original heading levels."""
    if not html:
        return ""
    html = re.sub(r"<p[^>]*>\s*(?:&nbsp;|\xa0)?\s*</p>", "", html)
    md = markdownify(
        html, heading_style="ATX", bullets="-",
        escape_asterisks=False, escape_underscores=False,
    )
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _stringify(value) -> str:
    """Render a JSON scalar/list/dict value as a compact inline string."""
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        # Join simple scalar lists with "; "
        rendered = []
        for v in value:
            s = _stringify(v)
            if s:
                rendered.append(s)
        return "; ".join(rendered)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# Fields in ``posting`` that are either internal IDs or content we render
# separately as their own section. Everything else is dumped as-is.
_POSTING_METADATA_SKIP = frozenset({
    "applicationForm", "surveyForms",
    "descriptionHtml", "descriptionPlainText", "descriptionParts",
    "linkedData", "userRoles", "structuredMetadataOverride",
    "applicationFormDefinitionId", "surveyFormDefinitionIds",
    "compensationPhilosophyHtml",  # HTML duplicate of plainText
    "compensationTiers",  # verbose; compensationTierSummary covers it
    "summaryComponents",  # redundant with compensationTiers
    "applicationLimitCalloutHtml",  # rendered via markdown separately
    "automatedProcessingLegalNotice",  # just a rule-ID reference
})

_ORG_METADATA_SKIP = frozenset({
    "theme", "activeFeatureFlags", "appConfirmationTrackingPixelHtml",
})


def _render_form(form: dict, label: str | None = None) -> list[str]:
    """Render an applicationForm or surveyForm, preserving its own sections."""
    if not isinstance(form, dict):
        return []
    sections = form.get("sections") or []
    lines: list[str] = []
    if label:
        lines.append(f"## {label}")
        lines.append("")

    def render_entry(entry: dict) -> str | None:
        field = entry.get("field")
        if not isinstance(field, dict):
            return None
        title = (field.get("title") or "").strip()
        if not title:
            return None
        ftype = field.get("type") or ""
        req = "required" if entry.get("isRequired") else "optional"
        out = f"- **{title}** ({ftype}, {req})"
        options = field.get("selectableValues") or []
        if isinstance(options, list) and options:
            opt_labels = []
            for o in options:
                if isinstance(o, dict):
                    lab = o.get("label") or o.get("value")
                    if lab:
                        opt_labels.append(str(lab))
                elif isinstance(o, str):
                    opt_labels.append(o)
            if opt_labels:
                out += f"\n  - Options: {', '.join(opt_labels)}"
        return out

    if sections:
        for section in sections:
            if not isinstance(section, dict):
                continue
            entries = section.get("fieldEntries") or []
            rendered = [r for r in (render_entry(e) for e in entries) if r]
            if not rendered:
                continue
            title = (section.get("title") or "").strip()
            if title:
                lines.append(f"### {title}")
            desc_html = section.get("descriptionHtml") or ""
            desc_md = _html_to_markdown(desc_html)
            if desc_md:
                lines.append(desc_md)
                lines.append("")
            lines.extend(rendered)
            lines.append("")
    else:
        entries = form.get("fieldEntries") or []
        rendered = [r for r in (render_entry(e) for e in entries) if r]
        lines.extend(rendered)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _build_markdown(app_data: dict, url: str | None) -> str | None:
    posting = app_data.get("posting")
    if not isinstance(posting, dict):
        return None
    organization = app_data.get("organization") or {}

    title = (posting.get("title") or "").strip()
    org_name = (organization.get("name") or "").strip()
    header = f"# {title} @ {org_name}" if (title and org_name) else f"# {title or org_name or 'Job Posting'}"

    # Metadata: dump every non-empty posting field (and a few useful org
    # fields) using Ashby's own key names and values.
    meta: list[str] = []
    # Company is the most useful top-of-page anchor; include it first.
    if org_name:
        site = (organization.get("publicWebsite") or "").strip()
        meta.append(f"- **company**: {org_name}" + (f" ({site})" if site else ""))
    for key in (
        "publicWebsite", "customJobsPageUrl", "hostedJobsPageSlug",
        "recruitingPrivacyPolicyUrl", "timezone",
    ):
        if key in _ORG_METADATA_SKIP:
            continue
        val = _stringify(organization.get(key))
        if val and key != "publicWebsite":  # already rendered with company name
            meta.append(f"- **{key}**: {val}")

    for key, value in posting.items():
        if key == "title":
            continue
        if key in _POSTING_METADATA_SKIP:
            continue
        s = _stringify(value)
        if s:
            meta.append(f"- **{key}**: {s}")

    # Posting description HTML, preserved verbatim as markdown.
    description_md = _html_to_markdown(posting.get("descriptionHtml") or "")

    # Compensation philosophy text as an explicit block, labelled with its
    # source field name.
    comp_philosophy = (posting.get("compensationPhilosophyPlainText") or "").strip()

    # Application limit callout as HTML → markdown (Ashby's own wording).
    limit_md = _html_to_markdown(posting.get("applicationLimitCalloutHtml") or "")

    # Application form
    form_lines = _render_form(posting.get("applicationForm") or {}, label="applicationForm")

    # Survey forms — preserve each in order.
    survey_lines: list[str] = []
    for i, sv in enumerate(posting.get("surveyForms") or []):
        survey_lines.extend(_render_form(sv, label=f"surveyForms[{i}]"))
        if survey_lines and survey_lines[-1] != "":
            survey_lines.append("")

    parts: list[str] = [header, ""]
    if meta:
        parts.extend(meta)
        parts.append("")
    if description_md:
        parts.append(description_md)
        parts.append("")
    if comp_philosophy:
        parts.append("**compensationPhilosophyPlainText**:")
        parts.append(comp_philosophy)
        parts.append("")
    if limit_md:
        parts.append("**applicationLimitCalloutHtml**:")
        parts.append(limit_md)
        parts.append("")
    if form_lines:
        parts.extend(form_lines)
        parts.append("")
    if survey_lines:
        parts.extend(survey_lines)
        parts.append("")
    if url:
        parts.append(f"**sourceUrl**: {url}")

    return "\n".join(parts).rstrip() + "\n"


def _build_markdown_from_jsonld(jp: dict, url: str | None) -> str | None:
    """Fallback when __appData isn't available — dump the JSON-LD verbatim."""
    title = (jp.get("title") or "").strip()
    header = f"# {title}" if title else "# Job Posting"

    meta: list[str] = []
    for key, value in jp.items():
        if key in ("@context", "@type", "title", "description"):
            continue
        s = _stringify(value)
        if s:
            meta.append(f"- **{key}**: {s}")

    description_html = jp.get("description") or ""
    # Only unescape when the value is actually entity-escaped (&lt;p&gt;…). The
    # old check also fired on raw "<" and would over-decode already-plain HTML
    # (e.g. turning a literal &amp; into &); markdownify handles raw HTML fine.
    if isinstance(description_html, str) and "&lt;" in description_html:
        from html import unescape as _un
        description_html = _un(description_html)
    description_md = _html_to_markdown(description_html)

    parts: list[str] = [header, ""]
    if meta:
        parts.extend(meta)
        parts.append("")
    if description_md:
        parts.append(description_md)
        parts.append("")
    if url:
        parts.append(f"**sourceUrl**: {url}")
    return "\n".join(parts).rstrip() + "\n"


def extract_ashby_data(soup: BeautifulSoup, url: str | None = None) -> None:
    app_data = _parse_app_data(soup)
    rendered: str | None = None
    if app_data:
        rendered = _build_markdown(app_data, url)
    if rendered is None:
        jp = _parse_jsonld_jobposting(soup)
        if jp is not None:
            rendered = _build_markdown_from_jsonld(jp, url)
    if not rendered:
        return

    body = soup.find("body")
    if body is None:
        return
    body.clear()
    marker = soup.new_tag("div", id="ashby-marker")
    marker.string = _MARKER + rendered.replace("\n", _NL_TOKEN) + _MARKER
    body.append(marker)


def postprocess_ashby(markdown: str) -> str:
    m = _MARKER_RE.search(markdown)
    if not m:
        return markdown
    content = m.group(1).replace(_NL_TOKEN, "\n")
    return content.strip() + "\n"


# ---------------------------------------------------------------------------
# Embed URL resolution
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_JOBS_HOST_RE = re.compile(r"jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_-]*)", re.I)
_ORG_SLUG_CACHE: dict[str, str] = {}


def _extract_ashby_jid(url: str) -> str | None:
    try:
        query = urlparse(url).query
    except Exception:
        return None
    if not query:
        return None
    values = parse_qs(query).get("ashby_jid")
    if not values:
        return None
    jid = values[0].strip().lower()
    return jid if _UUID_RE.match(jid) else None


def is_ashby_embed_url(url: str) -> bool:
    return _extract_ashby_jid(url) is not None


_ASHBY_EMBED_SCRIPT_RE = re.compile(
    r'<script[^>]+src\s*=\s*["\']https?://jobs\.ashbyhq\.com/([a-z0-9][a-z0-9_-]*)/embed[^"\']*["\']',
    re.IGNORECASE,
)


def extract_ashby_embed_slug_from_html(html: str) -> str | None:
    """Return the Ashby org slug if the page embeds a ``jobs.ashbyhq.com/{org}/embed`` script.

    Used to detect company career pages that pull job listings from Ashby via
    the embed script tag (e.g. ``skywatch.com/careers/``).
    """
    m = _ASHBY_EMBED_SCRIPT_RE.search(html)
    if not m:
        return None
    slug = m.group(1).lower()
    if slug in {"api", "embed", "assets", "_next", "static"}:
        return None
    return slug


def _find_careers_chunk_url(html: str, base_url: str) -> str | None:
    preferred = re.search(r'src="([^"]*/_next/static/chunks/pages/careers[^"]*\.js)"', html)
    if preferred:
        return urljoin(base_url, preferred.group(1))
    generic = re.search(r'src="([^"]*/_next/static/chunks/pages/[^"]*\.js)"', html)
    if generic:
        return urljoin(base_url, generic.group(1))
    return None


def _extract_org_slug_from_js(js: str) -> str | None:
    for m in _JOBS_HOST_RE.finditer(js):
        slug = m.group(1).lower()
        if slug in {"api", "embed", "assets", "_next", "static"}:
            continue
        return slug
    return None


async def resolve_ashby_embed_url(url: str, session) -> str | None:
    jid = _extract_ashby_jid(url)
    if not jid:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname == "jobs.ashbyhq.com":
        return None
    slug = _ORG_SLUG_CACHE.get(hostname)
    if slug is None:
        try:
            page = await session.get(url)
            chunk_url = _find_careers_chunk_url(page.text, str(page.url))
            if not chunk_url:
                return None
            # SSRF: chunk_url is a <script src> from attacker-controlled page HTML
            # and may be an absolute URL to any host. Refuse private/internal (or
            # unresolvable) hosts before fetching it (fail closed).
            if await is_private_host(urlparse(chunk_url).hostname or ""):
                return None
            chunk = await session.get(chunk_url)
            slug = _extract_org_slug_from_js(chunk.text)
        except Exception:
            return None
        if not slug:
            return None
        _ORG_SLUG_CACHE[hostname] = slug
    return f"https://jobs.ashbyhq.com/{slug}/{jid}"


# ---------------------------------------------------------------------------
# Board-index (public posting-api REST endpoint)
# ---------------------------------------------------------------------------

_BOARD_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


async def fetch_ashby_board(org: str, session) -> dict | None:
    """Fetch the full job-board listing for ``jobs.ashbyhq.com/{org}``.

    Returns the raw API payload (``{"jobs": [...], "apiVersion": "..."}``) or
    ``None`` on any error so callers can fall through to the normal HTML fetch.
    """
    try:
        resp = await session.get(f"{_BOARD_API_BASE}/{org}")
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = json.loads(resp.text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def render_ashby_board(data: dict, org: str, source_url: str | None = None) -> str:
    """Render an Ashby board listing as markdown grouped by department."""
    jobs = data.get("jobs") or []
    listed = [j for j in jobs if isinstance(j, dict) and j.get("isListed", True)]

    header = f"# {org} — Job Board ({len(listed)} open positions)"
    parts: list[str] = [header, ""]
    api_version = (data.get("apiVersion") or "").strip()
    if api_version:
        parts.append(f"**apiVersion**: {api_version}")
    parts.append(f"**source**: {_BOARD_API_BASE}/{org}")
    if source_url:
        parts.append(f"**boardUrl**: {source_url}")
    parts.append("")

    # Group by department for readability.
    by_dept: dict[str, list[dict]] = {}
    for j in listed:
        dept = (j.get("department") or "").strip() or "Other"
        by_dept.setdefault(dept, []).append(j)

    for dept in sorted(by_dept.keys()):
        dept_jobs = by_dept[dept]
        parts.append(f"## {dept} ({len(dept_jobs)})")
        parts.append("")
        for j in dept_jobs:
            title = (j.get("title") or "").strip() or "(untitled)"
            line = f"- **{title}**"
            details: list[str] = []
            loc = (j.get("location") or "").strip()
            if loc:
                details.append(loc)
            secondary = j.get("secondaryLocations") or []
            if isinstance(secondary, list) and secondary:
                more_locs = []
                for s in secondary:
                    if isinstance(s, dict):
                        n = (s.get("location") or "").strip()
                        if n:
                            more_locs.append(n)
                    elif isinstance(s, str):
                        more_locs.append(s)
                if more_locs:
                    details.append("+ " + ", ".join(more_locs))
            if j.get("isRemote"):
                details.append("Remote")
            wtype = (j.get("workplaceType") or "").strip()
            if wtype:
                details.append(wtype)
            etype = (j.get("employmentType") or "").strip()
            if etype:
                details.append(etype)
            team = (j.get("team") or "").strip()
            if team and team != dept:
                details.append(f"team: {team}")
            if details:
                line += " — " + " · ".join(details)
            parts.append(line)
            url = (j.get("jobUrl") or "").strip()
            if url:
                parts.append(f"  - {url}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"
