"""Markdown renderers for wellfound search, company, and job-detail pages."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from markdownify import markdownify

from .api import SITE, connection, connection_nodes, deref, entities

_BLANK_RE = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _money(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"${n / div:.1f}".replace(".0", "") + suffix
    return f"${int(n)}"


def _company_size(value: str | None) -> str:
    if not value or not value.startswith("SIZE_"):
        return ""
    body = value[5:].replace("_PLUS", "+").replace("_", "-")
    return f"{body} employees"


def _date(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%b %d, %Y")
    except (TypeError, ValueError, OSError):
        return ""


def _clean_url(value: str | None) -> str:
    """wellfound sometimes stores junk like ``twitter.com/https://x.com/foo`` or a
    bare ``twitter.com/foo``. Normalize to a single absolute URL."""
    if not value:
        return ""
    value = value.strip()
    # A second scheme embedded mid-string -> keep from there.
    idx = value.find("http", 1)
    if idx != -1:
        value = value[idx:]
    if not value.startswith(("http://", "https://")):
        value = "https://" + value.lstrip("/")
    return value


def _job_url(job: dict) -> str:
    jid, slug = job.get("id"), job.get("slug")
    if jid and slug:
        return f"{SITE}/jobs/{jid}-{slug}"
    if jid:
        return f"{SITE}/jobs/{jid}"
    return ""


def _locations(job: dict) -> str:
    if job.get("remote"):
        accepted = job.get("acceptedRemoteLocationNames") or []
        return f"Remote ({', '.join(accepted)})" if accepted else "Remote"
    locs = job.get("locationNames") or []
    return ", ".join(locs)


def _badges(cache: dict, startup: dict) -> list[str]:
    out = []
    for badge in deref(cache, startup.get("badges") or []):
        label = (badge or {}).get("label")
        if label:
            out.append(label)
    return out


def _job_line(idx: int, job: dict) -> str:
    bits = [f"{idx}. **{job.get('title', 'Untitled role')}**"]
    meta: list[str] = []
    comp = job.get("compensation")
    if comp:
        meta.append(comp)
    loc = _locations(job)
    if loc:
        meta.append(loc)
    jt = job.get("jobType")
    if jt:
        meta.append(jt.replace("_", "-"))
    exp_min = job.get("yearsExperienceMin")
    if exp_min:
        meta.append(f"{exp_min}+ yrs")
    line = bits[0]
    if meta:
        line += "\n   " + " · ".join(meta)
    url = _job_url(job)
    if url:
        line += f"\n   {url}"
    return line


# ---------------------------------------------------------------------------
# Job detail (JSON-LD JobPosting)
# ---------------------------------------------------------------------------


def _to_int(value) -> int | None:
    """JSON-LD numeric fields may be ints, floats, or strings like '120000.0'."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _salary(base: dict) -> str:
    if not isinstance(base, dict):
        return ""
    val = base.get("value") or {}
    cur = base.get("currency", "")
    lo, hi = _to_int(val.get("minValue")), _to_int(val.get("maxValue"))
    unit = (val.get("unitText") or "").lower()
    per = f"/{unit}" if unit and unit != "year" else "/yr"
    if lo and hi:
        return f"{cur} {lo:,}–{hi:,}{per}".strip()
    if lo:
        return f"{cur} {lo:,}+{per}".strip()
    if hi:
        return f"{cur} up to {hi:,}{per}".strip()
    return ""


def _job_location(value) -> str:
    items = value if isinstance(value, list) else [value]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        addr = it.get("address") or {}
        parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
        loc = ", ".join(p for p in parts if p)
        if loc:
            out.append(loc)
    return " / ".join(dict.fromkeys(out))


def render_job(job: dict, url: str) -> str:
    title = (job.get("title") or "Job Posting").strip()
    org = job.get("hiringOrganization") or {}
    company = org.get("name", "")

    lines = [f"# {title}"]
    if company:
        lines.append(f"**{company}**")
    lines.append("")

    meta: list[str] = []
    emp = job.get("employmentType")
    if emp:
        meta.append(emp.replace("_", "-").title())
    loc = _job_location(job.get("jobLocation"))
    jlt = job.get("jobLocationType")
    if jlt == "TELECOMMUTE":
        alr = job.get("applicantLocationRequirements") or {}
        name = alr.get("name") if isinstance(alr, dict) else ""
        loc = f"Remote ({name})" if name else "Remote"
    if loc:
        meta.append(loc)
    sal = _salary(job.get("baseSalary"))
    if sal:
        meta.append(sal)
    posted = job.get("datePosted")
    if posted:
        meta.append(f"Posted {posted[:10]}")
    if meta:
        lines.append(" · ".join(meta))
        lines.append("")

    industry = job.get("industry")
    if industry:
        lines.append(f"_Industry: {industry}_")
        lines.append("")

    desc = job.get("description") or ""
    if desc:
        md = markdownify(desc, heading_style="ATX", bullets="-",
                         escape_asterisks=False, escape_underscores=False)
        lines.append("## Description")
        lines.append("")
        lines.append(_BLANK_RE.sub("\n\n", md).strip())
        lines.append("")

    if org.get("sameAs"):
        lines.append(f"Company site: {org['sameAs']}")
    lines.append(url)
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Company page (Apollo Startup)
# ---------------------------------------------------------------------------


def render_company(cache: dict, startup: dict, url: str) -> str:
    name = startup.get("name", "Company")
    lines = [f"# {name}"]
    concept = startup.get("highConcept")
    if concept:
        lines.append(f"_{concept}_")
    lines.append("")

    meta: list[str] = []
    size = _company_size(startup.get("companySize"))
    if size:
        meta.append(size)
    raised = _money(startup.get("totalRaisedAmount"))
    if raised:
        meta.append(f"{raised} raised")
    badges = _badges(cache, startup)
    if badges:
        meta.append(" / ".join(badges))
    if meta:
        lines.append(" · ".join(meta))
        lines.append("")

    # Links
    link_fields = [
        ("Website", "companyUrl"), ("LinkedIn", "linkedInUrl"), ("Twitter", "twitterUrl"),
        ("Blog", "blogUrl"), ("Product Hunt", "productHuntUrl"),
    ]
    links = [f"[{label}]({_clean_url(startup[key])})" for label, key in link_fields if startup.get(key)]
    if links:
        lines.append(" · ".join(links))
        lines.append("")

    desc = startup.get("productDescription")
    if desc:
        lines.append("## About")
        lines.append("")
        lines.append(desc.strip())
        lines.append("")

    # Markets
    markets = [t.get("displayName") or t.get("name") for t in deref(cache, startup.get("marketTaggings") or [])]
    markets = [m for m in markets if m]
    if markets:
        lines.append(f"**Markets:** {', '.join(markets)}")
        lines.append("")

    # Funding rounds
    rounds = connection_nodes(cache, startup, "startupRounds")
    if rounds:
        lines.append("## Funding")
        lines.append("")
        for rnd in rounds:
            rt = rnd.get("roundType", "Round")
            when = _date(rnd.get("closedAt"))
            val = _money(rnd.get("valuation"))
            seg = f"- **{rt}**"
            extra = " · ".join(x for x in (val and f"valuation {val}", when) if x)
            if extra:
                seg += f" — {extra}"
            lines.append(seg)
        lines.append("")

    # Perks
    perks = deref(cache, startup.get("culturePerks") or [])
    if perks:
        lines.append("## Perks")
        lines.append("")
        for perk in perks:
            title = (perk or {}).get("title")
            pdesc = (perk or {}).get("description")
            if title and pdesc:
                lines.append(f"- **{title}:** {pdesc}")
            elif title:
                lines.append(f"- {title}")
        lines.append("")

    # Team
    team = connection_nodes(cache, startup, "currentTeamMemberRoles")
    members = []
    for role in team:
        user = deref(cache, role.get("user")) if role.get("user") else {}
        person = user.get("name") or role.get("invitedName")
        title = role.get("title") or role.get("roleDisplayName")
        if person:
            members.append(f"{person} ({title})" if title else person)
    if members:
        lines.append(f"**Team:** {', '.join(members)}")
        lines.append("")

    # Open jobs. Read totalCount from the RESOLVED connection — Apollo may store
    # it under an arg-qualified key (jobListingsConnection({"first":10})), so the
    # bare startup.get("jobListingsConnection") would miss it and underreport.
    jobs = connection_nodes(cache, startup, "jobListingsConnection")
    if jobs:
        conn = connection(startup, "jobListingsConnection")
        total = conn.get("totalCount", len(jobs))
        lines.append(f"## Open Jobs ({total})")
        lines.append("")
        for i, job in enumerate(jobs, 1):
            lines.append(_job_line(i, job))
            lines.append("")

    lines.append(url)
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Search pages (Apollo: company-grouped or flat job list)
# ---------------------------------------------------------------------------


def render_search(cache: dict, url: str, title: str = "") -> str:
    # Role/location pages carry rich StartupResults that link their jobs; the
    # /jobs feed carries only StartupResult stubs (no jobs) plus flat JobListings.
    startups = [s for s in entities(cache, "StartupResult") if s.get("highlightedJobListings")]
    if startups:
        return _render_company_grouped(cache, startups, url, title)
    return _render_job_list(cache, url, title)


def _render_company_grouped(cache: dict, startups: list[dict], url: str, title: str) -> str:
    jobs_total = len(entities(cache, "JobListingSearchResult"))
    head = title or "Startup job search"
    lines = [f"# {head}", "", f"_{jobs_total} roles across {len(startups)} startups (first page)_", ""]

    # Most jobs first.
    def njobs(s):
        return len(s.get("highlightedJobListings") or [])

    for startup in sorted(startups, key=njobs, reverse=True):
        name = startup.get("name", "Startup")
        slug = startup.get("slug")
        company_url = f"{SITE}/company/{slug}" if slug else ""
        header = f"## {name}"
        lines.append(header)
        sub: list[str] = []
        if startup.get("highConcept"):
            sub.append(startup["highConcept"])
        size = _company_size(startup.get("companySize"))
        if size:
            sub.append(size)
        badges = _badges(cache, startup)
        if badges:
            sub.append(" / ".join(badges))
        if sub:
            lines.append("_" + " · ".join(sub) + "_")
        if company_url:
            lines.append(company_url)
        lines.append("")
        jobs = deref(cache, startup.get("highlightedJobListings") or [])
        for i, job in enumerate(jobs, 1):
            if job:
                lines.append(_job_line(i, job))
        lines.append("")

    lines.append(f"Source: {url}")
    return "\n".join(lines).rstrip()


def _render_job_list(cache: dict, url: str, title: str) -> str:
    jobs = entities(cache, "JobListing")
    head = title or "Jobs"
    lines = [f"# {head}", "", f"_{len(jobs)} roles (first page)_", ""]
    for i, job in enumerate(jobs, 1):
        startup = deref(cache, job.get("startup")) if job.get("startup") else {}
        company = startup.get("name", "")
        line = f"{i}. **{job.get('title', 'Untitled role')}**"
        if company:
            line += f" — {company}"
        meta: list[str] = []
        if job.get("compensation"):
            meta.append(job["compensation"])
        loc = _locations(job)
        if loc:
            meta.append(loc)
        if meta:
            line += "\n   " + " · ".join(meta)
        ju = _job_url(job)
        if ju:
            line += f"\n   {ju}"
        lines.append(line)
        lines.append("")
    lines.append(f"Source: {url}")
    return "\n".join(lines).rstrip()
