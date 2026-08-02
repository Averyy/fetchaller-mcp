"""Workday job board client and markdown renderer.

Workday "myworkdayjobs" sites are SPAs that render board lists + posting
details via two REST endpoints rooted at ``/wday/cxs/{tenant}/{site}``:

- Board listing: ``POST /wday/cxs/{tenant}/{site}/jobs`` with body
  ``{appliedFacets, limit, offset, searchText}``. Returns
  ``{total, jobPostings: [{title, externalPath, locationsText, postedOn,
  bulletFields}]}``.
- Posting detail: ``GET /wday/cxs/{tenant}/{site}/job{externalPath}``.
  Returns ``{jobPostingInfo: {id, title, jobDescription (HTML), location,
  postedOn, jobReqId, timeType, remoteType, ...}}``.

This module exposes:

- ``is_workday_url`` / ``extract_workday_params`` — posting URL detection
  (``/{lang?}/{site}/job/{externalPath}``).
- ``is_workday_board_url`` / ``extract_workday_board_params`` — board URL
  detection (``/{lang?}/{site}``).
- ``fetch_workday_job`` — pulls posting JSON.
- ``fetch_workday_board`` — pulls a single page of postings.
- ``render_workday_job`` / ``render_workday_board`` — markdown rendering
  using Workday's own field names.
"""

import json
import re
from urllib.parse import urlparse

from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_WORKDAY_HOST_RE = re.compile(
    r"^([a-z0-9][a-z0-9-]*)\.wd\d{1,3}\.myworkdayjobs\.com$"
)
_LANG_RE = r"[a-zA-Z]{2}(?:-[A-Za-z]{2,4})?"
_LANG_FULL_RE = re.compile(rf"^{_LANG_RE}$")
_SITE_RE = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_WORKDAY_BOARD_PATH_RE = re.compile(
    rf"^(?:/({_LANG_RE}))?/({_SITE_RE})/?$"
)
_WORKDAY_JOB_PATH_RE = re.compile(
    rf"^(?:/({_LANG_RE}))?/({_SITE_RE})(/job/[^?#]+)$"
)


def _looks_like_lang(s: str) -> bool:
    return bool(_LANG_FULL_RE.match(s or ""))


def _tenant(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    m = _WORKDAY_HOST_RE.match(host)
    return m.group(1) if m else None


def extract_workday_params(url: str) -> tuple[str, str, str, str] | None:
    """Return ``(tenant, lang, site, external_path)`` for a Workday posting URL.

    ``lang`` may be an empty string if the URL has no language segment.
    ``external_path`` always starts with ``/job/`` and matches the value
    Workday returns in its API as ``externalPath``.
    """
    tenant = _tenant(url)
    if not tenant:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        return None
    m = _WORKDAY_JOB_PATH_RE.match(path)
    if not m:
        return None
    lang = m.group(1) or ""
    site = m.group(2)
    if site == "job":
        return None
    # When the URL has no language segment, reject site values that look like
    # language codes (e.g. `en-US`) — they're almost certainly a stripped lang
    # prefix rather than a real site slug.
    if not lang and _looks_like_lang(site):
        return None
    return tenant, lang, site, m.group(3)


def is_workday_url(url: str) -> bool:
    return extract_workday_params(url) is not None


def extract_workday_board_params(url: str) -> tuple[str, str, str] | None:
    """Return ``(tenant, lang, site)`` for a Workday board URL."""
    tenant = _tenant(url)
    if not tenant:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        return None
    m = _WORKDAY_BOARD_PATH_RE.match(path)
    if not m:
        return None
    lang = m.group(1) or ""
    site = m.group(2)
    if site == "job":
        return None
    if not lang and _looks_like_lang(site):
        return None
    return tenant, lang, site


def is_workday_board_url(url: str) -> bool:
    return extract_workday_board_params(url) is not None


# ---------------------------------------------------------------------------
# Posting fetch + render
# ---------------------------------------------------------------------------


def _api_base(url: str, tenant: str, site: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return f"https://{host}/wday/cxs/{tenant}/{site}"


async def fetch_workday_job(url: str, session) -> dict | None:
    """Fetch a Workday posting URL. Returns the raw ``{"jobPostingInfo": ...}``
    payload, or ``None`` on any error so callers can fall through.
    """
    params = extract_workday_params(url)
    if not params:
        return None
    tenant, _lang, site, external_path = params
    api = _api_base(url, tenant, site) + external_path
    try:
        resp = await session.get(api, headers={"accept": "application/json"})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("jobPostingInfo"), dict):
        return None
    return data


_HTML_NBSP_P_RE = re.compile(r"<p[^>]*>\s*(?:&nbsp;|\xa0)?\s*</p>")
_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")
# Workday wraps blocks of single spaces in <span class="WKQ0"> as a layout hack.
_WKQ0_RE = re.compile(r'<span[^>]*class="WKQ0"[^>]*>[^<]*</span>')


def _html_to_markdown(html: str) -> str:
    if not html:
        return ""
    cleaned = _WKQ0_RE.sub("", html)
    cleaned = _HTML_NBSP_P_RE.sub("", cleaned)
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
    "jobDescription",  # rendered as the description body
    "title",           # rendered as the H1
})


def render_workday_job(payload: dict, source_url: str | None = None) -> str:
    info = payload.get("jobPostingInfo") or {}
    title = (info.get("title") or "").strip()
    parts: list[str] = [f"# {title}" if title else "# Job Posting", ""]

    meta: list[str] = []
    for key, value in info.items():
        if key in _POSTING_SKIP:
            continue
        s = _stringify(value)
        if s:
            meta.append(f"- **{key}**: {s}")

    if meta:
        parts.extend(meta)
        parts.append("")

    description_md = _html_to_markdown(info.get("jobDescription") or "")
    if description_md:
        parts.append("## jobDescription")
        parts.append("")
        parts.append(description_md)
        parts.append("")

    if source_url:
        parts.append(f"**sourceUrl**: {source_url}")

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Board listing fetch + render
# ---------------------------------------------------------------------------

# Workday paginates at 20; we ask for a large batch and only follow more pages
# when the board is truly huge. 200 covers ~99% of company boards in one call.
_BOARD_PAGE_SIZE = 20
_BOARD_MAX_PAGES = 10


async def fetch_workday_board(url: str, session) -> dict | None:
    """Fetch a Workday board listing.

    Returns ``{"jobPostings": [...], "total": int}`` or ``None`` on any error.
    Pages through results until either ``total`` jobs are collected or
    ``_BOARD_MAX_PAGES`` pages have been fetched.
    """
    params = extract_workday_board_params(url)
    if not params:
        return None
    tenant, _lang, site = params
    api = _api_base(url, tenant, site) + "/jobs"

    all_jobs: list[dict] = []
    total: int | None = None
    for page in range(_BOARD_MAX_PAGES):
        body = {
            "appliedFacets": {},
            "limit": _BOARD_PAGE_SIZE,
            "offset": page * _BOARD_PAGE_SIZE,
            "searchText": "",
        }
        try:
            resp = await session.post(
                api,
                json=body,
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
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
        postings = data.get("jobPostings") or []
        if not isinstance(postings, list):
            return None
        # Some tenants (e.g. salesforce.wd12) only return "total" on the first
        # page and zero it out on subsequent pages, so lock the value in once.
        if total is None:
            page_total = data.get("total")
            total = int(page_total) if isinstance(page_total, int) else len(postings)
        all_jobs.extend(p for p in postings if isinstance(p, dict))
        if len(postings) < _BOARD_PAGE_SIZE or len(all_jobs) >= total:
            break

    return {
        "jobPostings": all_jobs,
        "total": total or len(all_jobs),
        "tenant": tenant,
        "site": site,
    }


# ---------------------------------------------------------------------------
# Facets — location and category filtering
# ---------------------------------------------------------------------------
#
# Every tenant exposes its filters as a `facets` array on the /jobs response,
# but they agree on almost nothing. The location facet is called
# `locationCountry` on Adobe and CrowdStrike, `locationHierarchy1` on NVIDIA,
# `locations` on ServiceTitan, and
# `CF_-_REC_-_LRV_-_Job_Posting_Anchor_-_Country_from_Job_Posting_Location_Extended`
# on Salesforce. The values disagree too, for the same city: "Canada, Toronto"
# (NVIDIA), "Canada - Toronto" (Salesforce), "Toronto" (Adobe).
#
# So nothing here is keyed by facet name. Location facets are found
# structurally (the children of `locationMainGroup`) plus any top-level facet
# whose human descriptor reads like a place, and values are matched by token
# containment rather than equality.

_LOCATION_DESCRIPTOR_RE = re.compile(
    r"countr|state|region|location|site|city|province|metro", re.IGNORECASE
)
_LOCATION_PARAM_RE = re.compile(r"location", re.IGNORECASE)
_LOCATION_GROUP = "locationMainGroup"


def _is_nested(facet: dict) -> bool:
    values = facet.get("values") or []
    return bool(values) and isinstance(values[0], dict) and "facetParameter" in values[0]


def flatten_facets(facets) -> list[dict]:
    """Return every leaf facet, including those nested under a group.

    Each entry is ``{facetParameter, descriptor, values, isLocation}``.
    """
    out: list[dict] = []

    def walk(items, in_location_group: bool) -> None:
        for facet in items or []:
            if not isinstance(facet, dict):
                continue
            parameter = facet.get("facetParameter") or ""
            if _is_nested(facet):
                walk(facet.get("values"), in_location_group or parameter == _LOCATION_GROUP)
                continue
            descriptor = facet.get("descriptor") or ""
            is_location = (
                in_location_group
                or bool(_LOCATION_PARAM_RE.search(parameter))
                or bool(_LOCATION_DESCRIPTOR_RE.search(descriptor))
            )
            out.append(
                {
                    "facetParameter": parameter,
                    "descriptor": descriptor,
                    "values": [v for v in (facet.get("values") or []) if isinstance(v, dict)],
                    "isLocation": is_location,
                }
            )

    walk(facets, False)
    return out


def resolve_location_facet(facets, location: str) -> tuple[str, list[str]] | None:
    """Pick the facet and value ids that best express ``location``.

    Returns ``(facetParameter, [ids])`` or None when nothing matches. When
    several facets match, the one with the fewest matching values wins: asking
    for "Canada" should apply the single country value rather than the twelve
    city values that also contain the word.
    """
    from ..jobfilter import location_matches, tokens

    wanted = tokens(location)
    if not wanted:
        return None

    best: tuple[str, list[str]] | None = None
    for facet in flatten_facets(facets):
        if not facet["isLocation"] or not facet["facetParameter"]:
            continue
        ids = [
            v["id"]
            for v in facet["values"]
            if v.get("id") and location_matches(str(v.get("descriptor") or ""), wanted)
        ]
        if not ids:
            continue
        if best is None or len(ids) < len(best[1]):
            best = (facet["facetParameter"], ids)
    return best


async def fetch_workday_facets(url: str, session) -> list | None:
    """Fetch just the facet definitions for a board."""
    params = extract_workday_board_params(url)
    if not params:
        return None
    tenant, _lang, site = params
    try:
        resp = await session.post(
            _api_base(url, tenant, site) + "/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"accept": "application/json", "content-type": "application/json"},
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
    return data.get("facets") or []


async def search_workday_board(
    url: str,
    session,
    *,
    search_text: str = "",
    applied_facets: dict | None = None,
    limit: int = 200,
) -> dict | None:
    """Page a board with an optional search string and applied facets."""
    params = extract_workday_board_params(url)
    if not params:
        return None
    tenant, _lang, site = params
    api = _api_base(url, tenant, site) + "/jobs"

    all_jobs: list[dict] = []
    total: int | None = None
    max_pages = max(1, min(_BOARD_MAX_PAGES, -(-limit // _BOARD_PAGE_SIZE)))

    for page in range(max_pages):
        body = {
            "appliedFacets": applied_facets or {},
            "limit": _BOARD_PAGE_SIZE,
            "offset": page * _BOARD_PAGE_SIZE,
            "searchText": search_text or "",
        }
        try:
            resp = await session.post(
                api,
                json=body,
                headers={"accept": "application/json", "content-type": "application/json"},
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
        postings = data.get("jobPostings") or []
        if not isinstance(postings, list):
            return None
        if total is None:
            page_total = data.get("total")
            total = int(page_total) if isinstance(page_total, int) else len(postings)
        all_jobs.extend(p for p in postings if isinstance(p, dict))
        if len(postings) < _BOARD_PAGE_SIZE or len(all_jobs) >= total:
            break

    return {
        "jobPostings": all_jobs[:limit],
        "total": total or len(all_jobs),
        "tenant": tenant,
        "site": site,
    }


def render_workday_board(payload: dict, source_url: str | None = None) -> str:
    jobs = payload.get("jobPostings") or []
    total = payload.get("total") or len(jobs)
    tenant = (payload.get("tenant") or "").strip()
    site = (payload.get("site") or "").strip()

    header = f"# {tenant or 'workday'} — Job Board ({total} open positions)"
    parts: list[str] = [header, ""]
    # Pagination caps at _BOARD_MAX_PAGES * _BOARD_PAGE_SIZE (200); flag when the
    # tenant's true total exceeds what we actually listed.
    if total > len(jobs):
        parts.append(f"_Showing {len(jobs)} of {total} positions (listing capped at {len(jobs)})._")
        parts.append("")
    if tenant:
        parts.append(f"- **tenant**: {tenant}")
    if site:
        parts.append(f"- **site**: {site}")
    if source_url:
        parts.append(f"- **boardUrl**: {source_url}")
    parts.append("")

    if not source_url:
        host_prefix = ""
    else:
        parsed = urlparse(source_url)
        # Strip trailing /{lang}/{site} segments — externalPath already
        # carries the full /job/{...} suffix below.
        # We use the board URL up to (and including) the {site} segment as
        # the base for posting links.
        host_prefix = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

    for j in jobs:
        title = (j.get("title") or "").strip() or "(untitled)"
        line = f"- **{title}**"
        details: list[str] = []
        loc = (j.get("locationsText") or "").strip()
        if loc:
            details.append(loc)
        posted = (j.get("postedOn") or "").strip()
        if posted:
            details.append(posted)
        if details:
            line += " — " + " · ".join(details)
        parts.append(line)

        external = (j.get("externalPath") or "").strip()
        if external and host_prefix:
            parts.append(f"  - {host_prefix}{external}")

        # bulletFields holds job-req IDs and similar small fields.
        bullets = j.get("bulletFields") or []
        if isinstance(bullets, list):
            shown = [str(b) for b in bullets if b not in (None, "")]
            if shown:
                parts.append(f"  - bulletFields: {', '.join(shown)}")

    return "\n".join(parts).rstrip() + "\n"
