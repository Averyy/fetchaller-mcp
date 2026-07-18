"""Dayforce HCM job board client and markdown renderer.

Dayforce job boards (``jobs.dayforcehcm.com``) are a Next.js SPA, but the
job-detail page server-side-renders the full posting into the
``__NEXT_DATA__`` script blob. The board-listing page does not — it makes a
CSRF-protected POST to ``/api/geo/{clientNamespace}/jobposting/search`` after
hydration.

This module exposes:

- ``is_dayforce_url`` / ``extract_dayforce_params`` — posting URL detection
  (``/{lang}/{namespace}/{board}/jobs/{id}``).
- ``is_dayforce_board_url`` / ``extract_dayforce_board_params`` — board URL
  detection (``/{lang}/{namespace}/{board}``).
- ``fetch_dayforce_job`` — pulls the posting HTML, returns the parsed
  ``jobData`` + ``site-info`` dehydrated query.
- ``fetch_dayforce_board`` — pulls the board HTML for cookies + ``site-info``,
  fetches the NextAuth CSRF token, and POSTs the listings search.
- ``render_dayforce_job`` / ``render_dayforce_board`` — markdown rendering
  using Dayforce's own field names.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_DAYFORCE_HOST_RE = re.compile(r"^jobs\.dayforcehcm\.com$")
_DAYFORCE_JOB_PATH_RE = re.compile(
    r"^/([a-z]{2}-[A-Za-z]{2,4})/([A-Za-z0-9][A-Za-z0-9_-]*)/([A-Za-z0-9][A-Za-z0-9_-]*)/jobs/(\d+)/?$"
)
_DAYFORCE_BOARD_PATH_RE = re.compile(
    r"^/([a-z]{2}-[A-Za-z]{2,4})/([A-Za-z0-9][A-Za-z0-9_-]*)/([A-Za-z0-9][A-Za-z0-9_-]*)/?$"
)


def extract_dayforce_params(url: str) -> tuple[str, str, str, str] | None:
    """Return ``(language, client_namespace, board_code, job_id)`` for a posting URL."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if not _DAYFORCE_HOST_RE.match((parsed.hostname or "").lower()):
        return None
    m = _DAYFORCE_JOB_PATH_RE.match(parsed.path)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def is_dayforce_url(url: str) -> bool:
    return extract_dayforce_params(url) is not None


def extract_dayforce_board_params(url: str) -> tuple[str, str, str] | None:
    """Return ``(language, client_namespace, board_code)`` for a board URL."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if not _DAYFORCE_HOST_RE.match((parsed.hostname or "").lower()):
        return None
    m = _DAYFORCE_BOARD_PATH_RE.match(parsed.path)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def is_dayforce_board_url(url: str) -> bool:
    return extract_dayforce_board_params(url) is not None


# ---------------------------------------------------------------------------
# White-label deployment detection
# ---------------------------------------------------------------------------
#
# Some companies host the Dayforce Next.js candidate portal on their own
# domain (e.g. ``www.synaptivemedical.com/job-openings``). The SSR'd
# ``__NEXT_DATA__`` carries the canonical clientNamespace + careerSiteXRefCode
# in ``query`` and exposes ``runtimeConfig.BASE_URL = 'https://jobs.dayforcehcm.com/'``
# — enough to rewrite the URL to the canonical Dayforce-hosted board.


def extract_dayforce_canonical_board_url(html: str) -> str | None:
    """If ``html`` is a white-label Dayforce candidate portal, return the
    canonical ``jobs.dayforcehcm.com`` board URL it corresponds to.

    Returns ``None`` if the HTML is not a Dayforce-hosted page.
    """
    next_data = extract_next_data(html)
    if not isinstance(next_data, dict):
        return None
    rt = next_data.get("runtimeConfig") or {}
    base_url = (rt.get("BASE_URL") or "").strip().rstrip("/")
    if base_url != "https://jobs.dayforcehcm.com":
        return None
    query = next_data.get("query") or {}
    namespace = (query.get("clientNamespace") or "").strip()
    board = (query.get("careerSiteXRefCode") or "").strip()
    locale = (next_data.get("locale") or "en-US").strip() or "en-US"
    if not (namespace and board):
        return None
    return f"https://jobs.dayforcehcm.com/{locale}/{namespace}/{board}"


# ---------------------------------------------------------------------------
# __NEXT_DATA__ parsing
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def extract_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def _site_info_from_next_data(next_data: dict) -> dict | None:
    """Pull the ``site-info`` dehydrated query (board metadata)."""
    try:
        queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError):
        return None
    for q in queries:
        key = q.get("queryKey") or []
        if isinstance(key, list) and key and key[0] == "site-info":
            data = q.get("state", {}).get("data")
            if isinstance(data, dict):
                return data
    return None


def _job_data_from_next_data(next_data: dict) -> dict | None:
    """Pull the ``jobData`` prop (posting detail)."""
    try:
        jd = next_data["props"]["pageProps"]["jobData"]
    except (KeyError, TypeError):
        return None
    return jd if isinstance(jd, dict) else None


# ---------------------------------------------------------------------------
# Posting fetch + render
# ---------------------------------------------------------------------------


async def fetch_dayforce_job(url: str, session) -> dict | None:
    """Fetch a Dayforce posting URL and return the parsed posting payload.

    Returns ``{"jobData": ..., "siteInfo": ...}`` (siteInfo may be ``None``)
    or ``None`` on any error so callers can fall through.
    """
    try:
        resp = await session.get(url)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    next_data = extract_next_data(resp.text)
    if not next_data:
        return None
    job = _job_data_from_next_data(next_data)
    if not job:
        return None
    return {"jobData": job, "siteInfo": _site_info_from_next_data(next_data)}


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
    "jobPostingContent",  # rendered as the description body
    "postingLocations",   # rendered as its own section
    "jobPostingAttributes",  # rendered as its own section
    # Internal handles with no candidate-facing value
    "assessmentId", "assessmentSentType", "jobApplicationTemplateId",
    "postingStatus", "postingThemeOverrideBoardCode",
})


def render_dayforce_job(payload: dict, source_url: str | None = None) -> str:
    job = payload.get("jobData") or {}
    site = payload.get("siteInfo") or {}

    title = (job.get("jobTitle") or "").strip()
    parts: list[str] = [f"# {title}" if title else "# Job Posting", ""]

    meta: list[str] = []
    board_code = (site.get("jobBoardCode") or "").strip()
    namespace = (site.get("clientNamespace") or "").strip()
    if namespace:
        meta.append(f"- **clientNamespace**: {namespace}")
    if board_code:
        meta.append(f"- **jobBoardCode**: {board_code}")

    for key, value in job.items():
        if key in _POSTING_SKIP or key == "jobTitle":
            continue
        s = _stringify(value)
        if s:
            meta.append(f"- **{key}**: {s}")

    # postingLocations as its own block
    loc_lines: list[str] = []
    for loc in (job.get("postingLocations") or []):
        if not isinstance(loc, dict):
            continue
        addr = (loc.get("formattedAddress") or "").strip()
        bits = []
        for k in ("cityName", "stateCode", "isoCountryCode"):
            v = (loc.get(k) or "").strip() if isinstance(loc.get(k), str) else loc.get(k)
            if v:
                bits.append(f"{k}={v}")
        if addr:
            loc_lines.append(f"- {addr}")
        elif bits:
            loc_lines.append("- " + ", ".join(bits))

    attr_lines: list[str] = []
    for attr in (job.get("jobPostingAttributes") or []):
        if not isinstance(attr, dict):
            continue
        name = (attr.get("name") or "").strip()
        val = attr.get("value")
        if name and val not in (None, ""):
            attr_lines.append(f"- **{name}**: {val}")

    if meta:
        parts.extend(meta)
        parts.append("")
    if loc_lines:
        parts.append("## postingLocations")
        parts.append("")
        parts.extend(loc_lines)
        parts.append("")
    if attr_lines:
        parts.append("## jobPostingAttributes")
        parts.append("")
        parts.extend(attr_lines)
        parts.append("")

    # jobPostingContent: header → body → footer (Dayforce's own ordering)
    content = job.get("jobPostingContent") or {}
    for label in ("jobDescriptionHeader", "jobDescription", "jobDescriptionFooter"):
        md = _html_to_markdown(content.get(label) or "")
        if md:
            parts.append(f"## {label}")
            parts.append("")
            parts.append(md)
            parts.append("")

    if source_url:
        parts.append(f"**sourceUrl**: {source_url}")

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Board listing fetch + render
# ---------------------------------------------------------------------------


async def fetch_dayforce_board(url: str, session) -> dict | None:
    """Fetch a Dayforce board listing.

    Returns ``{"siteInfo": ..., "jobPostings": [...], "totalCount": int}`` or
    ``None`` on any error.

    The board page only ships ``site-info`` in the SSR'd ``__NEXT_DATA__``;
    the actual posting list is loaded client-side via a CSRF-protected POST
    to ``/api/geo/{clientNamespace}/jobposting/search``. We replicate that
    call here: GET the page (for cookies + ``site-info``), GET
    ``/api/auth/csrf`` for the NextAuth token, then POST the search.
    """
    try:
        page = await session.get(url)
    except Exception:
        return None
    if page.status_code != 200:
        return None
    next_data = extract_next_data(page.text)
    if not next_data:
        return None
    site = _site_info_from_next_data(next_data)
    if not site:
        return None
    namespace = (site.get("clientNamespace") or "").strip()
    board_code = (site.get("jobBoardCode") or "").strip()
    culture = (site.get("cultureCode") or "").strip()
    if not (namespace and board_code and culture):
        return None

    try:
        csrf_resp = await session.get("https://jobs.dayforcehcm.com/api/auth/csrf")
        csrf = (csrf_resp.json() or {}).get("csrfToken")
    except Exception:
        csrf = None
    if not csrf:
        return None

    api_url = f"https://jobs.dayforcehcm.com/api/geo/{namespace}/jobposting/search"
    body = {
        "clientNamespace": namespace,
        "jobBoardCode": board_code,
        "cultureCode": culture,
        "pageNumber": 1,
        "pageSize": 200,
    }
    try:
        resp = await session.post(
            api_url,
            json=body,
            headers={
                "content-type": "application/json",
                "X-CSRF-TOKEN": csrf,
                "referer": url,
            },
        )
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
    return {
        "siteInfo": site,
        "jobPostings": data.get("jobPostings") or [],
        "totalCount": data.get("count") or data.get("maxCount") or 0,
    }


def _strip_html(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return BeautifulSoup(value, "lxml").get_text(" ", strip=True)


def render_dayforce_board(payload: dict, source_url: str | None = None) -> str:
    site = payload.get("siteInfo") or {}
    jobs = payload.get("jobPostings") or []
    namespace = (site.get("clientNamespace") or "").strip()
    board_code = (site.get("jobBoardCode") or "").strip()
    culture = (site.get("cultureCode") or "").strip()

    total = payload.get("totalCount") or len(jobs)
    header = f"# {board_code or namespace} — Job Board ({len(jobs)} open positions)"
    parts: list[str] = [header, ""]
    # Board is fetched as a single page (pageSize 200); flag truncation so the
    # count above isn't read as the full set when the tenant has more openings.
    if total > len(jobs):
        parts.append(f"_Showing {len(jobs)} of {total} positions (listing capped at {len(jobs)})._")
        parts.append("")
    if namespace:
        parts.append(f"- **clientNamespace**: {namespace}")
    if board_code:
        parts.append(f"- **jobBoardCode**: {board_code}")
    if culture:
        parts.append(f"- **cultureCode**: {culture}")
    if source_url:
        parts.append(f"- **boardUrl**: {source_url}")
    parts.append("")

    for j in jobs:
        if not isinstance(j, dict):
            continue
        title = (j.get("jobTitle") or "").strip() or "(untitled)"
        jpid = j.get("jobPostingId")
        line = f"- **{title}**"
        details: list[str] = []
        loc = (j.get("location") or "").strip() if isinstance(j.get("location"), str) else ""
        if not loc:
            for ploc in (j.get("postingLocations") or []):
                if isinstance(ploc, dict):
                    addr = (ploc.get("formattedAddress") or "").strip()
                    city = (ploc.get("cityName") or "").strip() if isinstance(ploc.get("cityName"), str) else ""
                    if addr:
                        loc = addr
                        break
                    if city:
                        loc = city
                        break
        if loc:
            details.append(loc)
        if details:
            line += " — " + " · ".join(details)
        parts.append(line)
        if jpid is not None and namespace and board_code:
            parts.append(
                f"  - https://jobs.dayforcehcm.com/{culture or 'en-US'}/{namespace}/"
                f"{board_code.upper()}/jobs/{jpid}"
            )
        # Short snippet
        desc = _strip_html(j.get("jobDescription") or "")
        if desc:
            snippet = desc[:240] + ("…" if len(desc) > 240 else "")
            parts.append(f"  - {snippet}")

    return "\n".join(parts).rstrip() + "\n"
