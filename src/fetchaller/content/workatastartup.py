"""Work at a Startup (YC) job board cleanup and structured extraction.

Exports the standard site interface (SELECTORS_LIST, is_workatastartup,
extract_workatastartup_data, postprocess_workatastartup).

workatastartup.com pages are Inertia.js-style React SPAs — the rendered
body is just a spinner. All page data lives inline as
``<div data-page="<json>">`` with HTML-entity-encoded JSON. This module
dumps that data as-is (raw field names, raw values) so the LLM sees every
field the platform exposes without translation or reordering.

Dispatch is on the Inertia ``component``, never the URL: a job posting
(``props.job``), the public job list (``JobsV2Page``), a company page
(``CompanyV2Page``) and the home page (``HomeV2Page``). Only postings were
handled at first, so the other three rendered their ``<title>`` and nothing
else (measured 2026-10-05: ``/jobs`` 86 chars for a page carrying 30 of 2,893
jobs; a company page 62 chars) — an empty board that read as a real one.
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

_WAAS_HOST_RE = re.compile(r"^(?:www\.)?workatastartup\.com$")
_ORIGIN = "https://www.workatastartup.com"


def is_workatastartup(url: str) -> bool:
    """Check if URL is on Work at a Startup.

    Any path qualifies; what the page is gets decided by its Inertia
    component, and a page this module does not render is left untouched.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    hostname = (parsed.hostname or "").lower()
    return bool(_WAAS_HOST_RE.match(hostname))


SELECTORS_LIST: list[str] = []


# ---------------------------------------------------------------------------
# Structured data extraction (runs BEFORE scripts are decomposed)
# ---------------------------------------------------------------------------

_MARKER = "__WAAS_MARKER__"
_MARKER_RE = re.compile(r"__WAAS_MARKER__([\s\S]*?)__WAAS_MARKER__")
_NL_TOKEN = "__WAAS_NL__"


def _parse_inertia_data(soup: BeautifulSoup) -> dict | None:
    """Pull the JSON from the Inertia ``<div data-page>`` root node."""
    node = soup.find(attrs={"data-page": True})
    if node is None:
        return None
    raw = node.get("data-page") or ""
    if not raw:
        return None
    # BeautifulSoup already decodes HTML entities when reading attributes,
    # but some servers double-encode. unescape is a no-op on decoded text.
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            return json.loads(unescape(raw))
        except (json.JSONDecodeError, ValueError):
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
        rendered = []
        for v in value:
            s = _stringify(v)
            if s:
                rendered.append(s)
        return "; ".join(rendered)
    if isinstance(value, dict):
        if "name" in value and isinstance(value["name"], str):
            return str(value["name"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# Fields in ``job`` rendered as their own section (HTML bodies) — skipped in meta.
_JOB_METADATA_SKIP = frozenset({
    "descriptionHtml", "interviewProcessHtml",
})

# Fields in ``company`` rendered as their own section — skipped in meta.
_COMPANY_METADATA_SKIP = frozenset({
    "hiringDescriptionHtml", "techDescriptionHtml",
    "founders", "jobs", "logoUrl",
})


def _render_founder(founder: dict) -> list[str]:
    if not isinstance(founder, dict):
        return []
    lines: list[str] = []
    name = (founder.get("name") or "").strip()
    linkedin = (founder.get("linkedin") or "").strip()
    header = f"- **{name}**" if name else "- **founder**"
    if linkedin:
        header += f" ({linkedin})"
    lines.append(header)
    bio = (founder.get("bio") or "").strip()
    if bio:
        for bio_line in bio.splitlines():
            bio_line = bio_line.strip()
            if bio_line:
                lines.append(f"  - {bio_line}")
    # Preserve any other fields the platform adds later (avatarUrl is noisy;
    # stringify everything else we haven't already rendered).
    for k, v in founder.items():
        if k in ("name", "bio", "linkedin", "avatarUrl"):
            continue
        s = _stringify(v)
        if s:
            lines.append(f"  - **{k}**: {s}")
    return lines


def _render_job_stub(job: dict) -> str | None:
    """One-line summary for entries in company.jobs / otherJobs."""
    if not isinstance(job, dict):
        return None
    title = (job.get("title") or "").strip()
    if not title:
        return None
    parts = [title]
    for k in ("location", "jobType", "salaryRange", "equityRange",
              "sponsorsVisa", "minExperience"):
        v = job.get(k)
        s = _stringify(v)
        if s:
            parts.append(s)
    job_id = job.get("id")
    line = f"- {' — '.join(parts)}"
    if job_id:
        line += f"  \n  https://www.workatastartup.com/jobs/{job_id}"
    return line


# Page chrome and marketing modules on the listing pages. Everything else a
# listing page carries is rendered or dumped.
_LISTING_CHROME = frozenset({
    "nav", "flash", "flashAlertsHtml", "footerLogoPath", "variantChrome",
    "icons", "heroVideo", "slideshow", "people", "news", "founderHighlights",
    "founderPlaceholders", "learnEvents", "signupUrl", "loginUrl",
    "metaDescription", "jobsPath",
})

# The row fields of a public job-list entry, in the order they are printed.
# companyLogoUrl and applyUrl (a sign-up redirect) are noise per row.
_LISTING_ROW_SKIP = frozenset({"id", "title", "companyLogoUrl", "applyUrl", "companySlug"})


def _render_listing_job(job: dict) -> list[str]:
    """One public job-list entry: title, then its raw fields, then links."""
    if not isinstance(job, dict) or not (job.get("title") or "").strip():
        return []
    values = [
        _stringify(v)
        for k, v in job.items()
        if k not in _LISTING_ROW_SKIP and _stringify(v)
    ]
    lines = [f"- **{job['title'].strip()}**" + (f" — {' — '.join(values)}" if values else "")]
    links = []
    if job.get("id"):
        links.append(f"{_ORIGIN}/jobs/{job['id']}")
    if job.get("companySlug"):
        links.append(f"{_ORIGIN}/companies/{job['companySlug']}")
    if links:
        lines.append(f"  {' · '.join(links)}")
    return lines


def _dump_other_props(props: dict, rendered: set[str]) -> list[str]:
    lines = []
    for key, value in props.items():
        if key in rendered or key in _LISTING_CHROME:
            continue
        s = _stringify(value)
        if s:
            lines.append(f"- **{key}**: {s}")
    return lines


def _build_job_list_markdown(props: dict, url: str | None) -> str:
    """``JobsV2Page`` and ``HomeV2Page``: the jobs the page carries."""
    jobs = [j for j in props.get("jobs") or [] if isinstance(j, dict)]
    title = (props.get("title") or "").strip() or "Work at a Startup jobs"
    parts = [f"# {title}", ""]
    total = props.get("totalJobsCount")
    summary = []
    if isinstance(total, int):
        summary.append(f"**totalJobsCount**: {total:,}")
    summary.append(f"**shown**: {len(jobs)} (the jobs this page carries)")
    for key in ("currentRole", "initialQuery"):
        s = _stringify(props.get(key))
        if s:
            summary.append(f"**{key}**: {s}")
    parts.append(" · ".join(summary))
    parts.append("")

    if jobs:
        parts.append("## jobs")
        parts.append("")
        for job in jobs:
            parts.extend(_render_listing_job(job))
        parts.append("")
    else:
        parts.append("No jobs on this page.")
        parts.append("")

    role_links = [r for r in props.get("roleLinks") or [] if isinstance(r, dict) and r.get("path")]
    if role_links:
        parts.append("## roleLinks")
        parts.append("")
        parts.extend(f"- {r.get('label')}: {_ORIGIN}{r['path']}" for r in role_links)
        parts.append("")

    companies = [c for c in props.get("hiringCompanies") or [] if isinstance(c, dict) and c.get("name")]
    if companies:
        parts.append("## hiringCompanies")
        parts.append("")
        parts.extend(
            f"- {c['name']}" + (f" — {_ORIGIN}{c['path']}" if c.get("path") else "") for c in companies
        )
        parts.append("")

    other = _dump_other_props(
        props,
        {"title", "jobs", "totalJobsCount", "currentRole", "initialQuery", "roleLinks", "roles", "hiringCompanies"},
    )
    if other:
        parts.append("## props (other)")
        parts.append("")
        parts.extend(other)
        parts.append("")
    if url:
        parts.append(f"**sourceUrl**: {url}")
    return "\n".join(parts).rstrip() + "\n"


def _build_company_markdown(props: dict, url: str | None) -> str:
    """``CompanyV2Page``: the company record, its founders and its open jobs."""
    company = props.get("company") if isinstance(props.get("company"), dict) else {}
    name = (company.get("name") or "").strip() or "Company"
    batch = (company.get("batch") or "").strip()
    parts = [f"# {name}" + (f" ({batch})" if batch else ""), ""]

    meta = [
        f"- **{k}**: {_stringify(v)}"
        for k, v in company.items()
        if k not in _COMPANY_METADATA_SKIP and k not in ("name", "batch") and _stringify(v)
    ]
    if meta:
        parts.append("## company")
        parts.append("")
        parts.extend(meta)
        parts.append("")

    for key in ("hiringDescriptionHtml", "techDescriptionHtml"):
        md = _html_to_markdown(company.get(key) or "")
        if md:
            parts.append(f"## company.{key}")
            parts.append("")
            parts.append(md)
            parts.append("")

    founders = [line for f in company.get("founders") or [] for line in _render_founder(f)]
    if founders:
        parts.append("## company.founders")
        parts.append("")
        parts.extend(founders)
        parts.append("")

    jobs = [line for j in company.get("jobs") or [] if (line := _render_job_stub(j))]
    parts.append("## company.jobs")
    parts.append("")
    parts.extend(jobs or ["No open jobs listed."])
    parts.append("")

    others = [c for c in props.get("otherCompanies") or [] if isinstance(c, dict)]
    if others:
        parts.append("## otherCompanies")
        parts.append("")
        for other in others:
            label = other.get("name") or other.get("slug") or ""
            path = other.get("path") or (f"/companies/{other['slug']}" if other.get("slug") else "")
            if label:
                parts.append(f"- {label}" + (f" — {_ORIGIN}{path}" if path else ""))
        parts.append("")

    other = _dump_other_props(props, {"company", "otherCompanies"})
    if other:
        parts.append("## props (other)")
        parts.append("")
        parts.extend(other)
        parts.append("")
    if url:
        parts.append(f"**sourceUrl**: {url}")
    return "\n".join(parts).rstrip() + "\n"


def _build_markdown(inertia: dict, url: str | None) -> str | None:
    props = inertia.get("props")
    if not isinstance(props, dict):
        return None
    job = props.get("job")
    if not isinstance(job, dict):
        component = str(inertia.get("component") or "")
        if component.endswith("CompanyV2Page") and isinstance(props.get("company"), dict):
            return _build_company_markdown(props, url)
        if component.endswith(("JobsV2Page", "HomeV2Page")) and isinstance(props.get("jobs"), list):
            return _build_job_list_markdown(props, url)
        return None
    company = props.get("company") or {}
    if not isinstance(company, dict):
        company = {}

    title = (job.get("title") or "").strip()
    org_name = (company.get("name") or "").strip()
    if title and org_name:
        header = f"# {title} @ {org_name}"
    else:
        header = f"# {title or org_name or 'Job Posting'}"

    # ---- Job metadata ----
    job_meta: list[str] = []
    for key, value in job.items():
        if key == "title" or key in _JOB_METADATA_SKIP:
            continue
        s = _stringify(value)
        if s:
            job_meta.append(f"- **{key}**: {s}")

    # ---- Company metadata ----
    company_meta: list[str] = []
    if org_name:
        company_meta.append(f"- **name**: {org_name}")
    for key, value in company.items():
        if key in _COMPANY_METADATA_SKIP or key == "name":
            continue
        s = _stringify(value)
        if s:
            company_meta.append(f"- **{key}**: {s}")

    # ---- Apply / signup URLs (top-level props) ----
    apply_url = (props.get("applyUrl") or "").strip()
    signup_url = (props.get("signupUrl") or "").strip()

    # ---- HTML sections ----
    description_md = _html_to_markdown(job.get("descriptionHtml") or "")
    interview_md = _html_to_markdown(job.get("interviewProcessHtml") or "")
    hiring_md = _html_to_markdown(company.get("hiringDescriptionHtml") or "")
    tech_md = _html_to_markdown(company.get("techDescriptionHtml") or "")

    # ---- Founders ----
    founder_lines: list[str] = []
    for founder in company.get("founders") or []:
        founder_lines.extend(_render_founder(founder))

    # ---- Other jobs at the same company ----
    company_jobs_lines: list[str] = []
    for stub in company.get("jobs") or []:
        rendered = _render_job_stub(stub)
        if rendered:
            company_jobs_lines.append(rendered)

    # ---- otherJobs (platform-suggested, usually cross-company) ----
    other_jobs_lines: list[str] = []
    for stub in props.get("otherJobs") or []:
        rendered = _render_job_stub(stub)
        if rendered:
            other_jobs_lines.append(rendered)

    # ---- Any top-level props we haven't rendered — dump them raw so nothing
    # is silently dropped as the schema evolves. ----
    rendered_props = {
        "job", "company", "applyUrl", "signupUrl", "otherJobs",
        "nav", "flash", "footerLogoPath",
    }
    extra_meta: list[str] = []
    for key, value in props.items():
        if key in rendered_props:
            continue
        s = _stringify(value)
        if s:
            extra_meta.append(f"- **{key}**: {s}")

    parts: list[str] = [header, ""]

    if apply_url:
        parts.append(f"**applyUrl**: {apply_url}")
    if signup_url and signup_url != apply_url:
        parts.append(f"**signupUrl**: {signup_url}")
    if apply_url or signup_url:
        parts.append("")

    if job_meta:
        parts.append("## job")
        parts.append("")
        parts.extend(job_meta)
        parts.append("")

    if company_meta:
        parts.append("## company")
        parts.append("")
        parts.extend(company_meta)
        parts.append("")

    if description_md:
        parts.append("## descriptionHtml")
        parts.append("")
        parts.append(description_md)
        parts.append("")

    if interview_md:
        parts.append("## interviewProcessHtml")
        parts.append("")
        parts.append(interview_md)
        parts.append("")

    if hiring_md:
        parts.append("## company.hiringDescriptionHtml")
        parts.append("")
        parts.append(hiring_md)
        parts.append("")

    if tech_md:
        parts.append("## company.techDescriptionHtml")
        parts.append("")
        parts.append(tech_md)
        parts.append("")

    if founder_lines:
        parts.append("## company.founders")
        parts.append("")
        parts.extend(founder_lines)
        parts.append("")

    if company_jobs_lines:
        parts.append("## company.jobs")
        parts.append("")
        parts.extend(company_jobs_lines)
        parts.append("")

    if other_jobs_lines:
        parts.append("## otherJobs")
        parts.append("")
        parts.extend(other_jobs_lines)
        parts.append("")

    if extra_meta:
        parts.append("## props (other)")
        parts.append("")
        parts.extend(extra_meta)
        parts.append("")

    if url:
        parts.append(f"**sourceUrl**: {url}")

    return "\n".join(parts).rstrip() + "\n"


def extract_workatastartup_data(soup: BeautifulSoup, url: str | None = None) -> None:
    inertia = _parse_inertia_data(soup)
    if not inertia:
        return
    rendered = _build_markdown(inertia, url)
    if not rendered:
        return

    body = soup.find("body")
    if body is None:
        return
    body.clear()
    marker = soup.new_tag("div", id="waas-marker")
    marker.string = _MARKER + rendered.replace("\n", _NL_TOKEN) + _MARKER
    body.append(marker)


def postprocess_workatastartup(markdown: str) -> str:
    m = _MARKER_RE.search(markdown)
    if not m:
        return markdown
    content = m.group(1).replace(_NL_TOKEN, "\n")
    return content.strip() + "\n"
