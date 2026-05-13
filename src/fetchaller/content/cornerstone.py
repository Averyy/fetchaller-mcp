"""Cornerstone OnDemand (CSOD) job board client and markdown renderer.

CSOD career sites (``{tenant}.csod.com/ux/ats/careersite/{cid}/home``) are a
~5 KB SPA shell. The interesting state lives in an inline
``csod.context = {...};`` blob: a per-page JWT, the regional cloud API base,
the culture id, and the tenant corp slug. The SPA then calls:

- Posting detail: ``services/x/job-requisition/v2/requisitions/{reqid}/jobDetails?cultureId={n}``
  on the tenant host.
- Board listing: ``rec-job-search/external/jobs`` on the regional cloud
  host (``us.api.csod.com``, ``eu.api.csod.com``, ``uk.api.csod.com``,
  ``au.api.csod.com``).

Both endpoints accept the JWT as a Bearer token plus a
``CSOD-Accept-Language`` header.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_CSOD_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)\.csod\.com$")
_CSOD_JOB_PATH_RE = re.compile(
    r"^/ux/ats/careersite/(\d+)/home/requisition/(\d+)/?$"
)
_CSOD_BOARD_PATH_RE = re.compile(r"^/ux/ats/careersite/(\d+)/home/?$")


def _tenant(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    m = _CSOD_HOST_RE.match(host)
    return m.group(1) if m else None


def extract_cornerstone_params(url: str) -> tuple[str, str, str] | None:
    """Return ``(tenant, career_site_id, requisition_id)`` for a posting URL."""
    tenant = _tenant(url)
    if not tenant:
        return None
    path = urlparse(url).path
    m = _CSOD_JOB_PATH_RE.match(path)
    if not m:
        return None
    return tenant, m.group(1), m.group(2)


def is_cornerstone_url(url: str) -> bool:
    return extract_cornerstone_params(url) is not None


def extract_cornerstone_board_params(url: str) -> tuple[str, str] | None:
    """Return ``(tenant, career_site_id)`` for a board URL."""
    tenant = _tenant(url)
    if not tenant:
        return None
    m = _CSOD_BOARD_PATH_RE.match(urlparse(url).path)
    if not m:
        return None
    return tenant, m.group(1)


def is_cornerstone_board_url(url: str) -> bool:
    return extract_cornerstone_board_params(url) is not None


# ---------------------------------------------------------------------------
# csod.context parsing
# ---------------------------------------------------------------------------

_CSOD_CONTEXT_RE = re.compile(r"csod\.context\s*=\s*(\{.*?\});", re.DOTALL)


def extract_csod_context(html: str) -> dict | None:
    m = _CSOD_CONTEXT_RE.search(html)
    if not m:
        return None
    try:
        ctx = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    return ctx if isinstance(ctx, dict) else None


# ---------------------------------------------------------------------------
# Posting fetch + render
# ---------------------------------------------------------------------------


async def fetch_cornerstone_job(url: str, session) -> dict | None:
    """Fetch a CSOD posting URL.

    Returns ``{"jobDetails": ..., "context": ..., "requisitionId": str}`` or
    ``None``. The full posting markdown can be rendered from ``jobDetails``;
    ``context`` carries tenant/corp metadata used in the header.
    """
    params = extract_cornerstone_params(url)
    if not params:
        return None
    tenant, _cid, reqid = params
    try:
        page = await session.get(url)
    except Exception:
        return None
    if page.status_code != 200:
        return None
    ctx = extract_csod_context(page.text)
    if not ctx:
        return None
    token = ctx.get("token")
    culture_id = ctx.get("cultureID")
    culture_name = (ctx.get("cultureName") or "en-US").strip() or "en-US"
    if not (token and culture_id is not None):
        return None
    api = (
        f"https://{tenant}.csod.com/services/x/job-requisition/v2/"
        f"requisitions/{reqid}/jobDetails?cultureId={culture_id}"
    )
    try:
        resp = await session.get(
            api,
            headers={
                "authorization": f"Bearer {token}",
                "csod-accept-language": culture_name,
                "accept": "application/json",
            },
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return {"jobDetails": data, "context": ctx, "requisitionId": reqid}


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


_JOB_SKIP = frozenset({
    "externalDescription",       # rendered as the description body
    "primaryLocation",           # rendered as its own line
    "additionalLocations",       # rendered as its own line
    # Internal references with no candidate value
    "hiringManagerId", "owners", "reviewers", "positionOUId",
    "requisitionStatusId",
})


def _format_location(loc: dict) -> str:
    if not isinstance(loc, dict):
        return ""
    parts: list[str] = []
    for k in ("title", "city", "state", "country"):
        v = loc.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " · ".join(parts)


def render_cornerstone_job(payload: dict, source_url: str | None = None) -> str:
    job = payload.get("jobDetails") or {}
    ctx = payload.get("context") or {}
    corp = (ctx.get("corp") or "").strip()

    title = (job.get("displayTitle") or "").strip()
    if title and corp:
        header = f"# {title} @ {corp}"
    elif title:
        header = f"# {title}"
    else:
        header = "# Job Posting"
    parts: list[str] = [header, ""]

    meta: list[str] = []
    if corp:
        meta.append(f"- **corp**: {corp}")
    reqid = payload.get("requisitionId")
    if reqid:
        meta.append(f"- **requisitionId**: {reqid}")

    for key, value in job.items():
        if key in _JOB_SKIP or key == "displayTitle":
            continue
        s = _stringify(value)
        if s:
            meta.append(f"- **{key}**: {s}")

    primary = _format_location(job.get("primaryLocation") or {})
    if primary:
        meta.append(f"- **primaryLocation**: {primary}")
    addl = job.get("additionalLocations") or []
    if isinstance(addl, list):
        addl_strs = [s for s in (_format_location(a) for a in addl) if s]
        if addl_strs:
            meta.append(f"- **additionalLocations**: {'; '.join(addl_strs)}")

    if meta:
        parts.extend(meta)
        parts.append("")

    description_md = _html_to_markdown(job.get("externalDescription") or "")
    if description_md:
        parts.append("## externalDescription")
        parts.append("")
        parts.append(description_md)
        parts.append("")

    if source_url:
        parts.append(f"**sourceUrl**: {source_url}")

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Board listing fetch + render
# ---------------------------------------------------------------------------


async def fetch_cornerstone_board(url: str, session) -> dict | None:
    """Fetch a CSOD board listing.

    Returns ``{"requisitions": [...], "totalCount": int, "context": ...}`` or
    ``None``. The SPA shell is fetched first to extract ``csod.context`` (JWT
    + regional cloud base + cultureId), then ``rec-job-search/external/jobs``
    on the regional cloud host is called with that JWT.
    """
    params = extract_cornerstone_board_params(url)
    if not params:
        return None
    _tenant_slug, cid = params
    try:
        page = await session.get(url)
    except Exception:
        return None
    if page.status_code != 200:
        return None
    ctx = extract_csod_context(page.text)
    if not ctx:
        return None
    token = ctx.get("token")
    culture_id = ctx.get("cultureID")
    culture_name = (ctx.get("cultureName") or "en-US").strip() or "en-US"
    cloud = (ctx.get("endpoints") or {}).get("cloud", "").rstrip("/")
    if not (token and culture_id is not None and cloud):
        return None

    body = {
        "careerSiteId": cid,
        "careerSitePageId": cid,
        "pageNumber": 1,
        "pageSize": 200,
        "cultureId": culture_id,
        "cultureName": culture_name,
        "searchText": "",
        "states": [],
        "countryCodes": [],
        "cities": [],
        "placeID": "",
        "radius": "",
        "postingsWithinDays": "",
        "customFieldCheckboxKeys": [],
        "customFieldDropdowns": [],
        "customFieldRadios": [],
    }
    try:
        resp = await session.post(
            f"{cloud}/rec-job-search/external/jobs",
            json=body,
            headers={
                "authorization": f"Bearer {token}",
                "csod-accept-language": culture_name,
                "accept": "application/json",
                "content-type": "application/json",
            },
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        envelope = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return None
    return {
        "requisitions": data.get("requisitions") or [],
        "totalCount": data.get("totalCount") or 0,
        "context": ctx,
    }


def _strip_html(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return BeautifulSoup(value, "lxml").get_text(" ", strip=True)


def render_cornerstone_board(payload: dict, source_url: str | None = None) -> str:
    reqs = payload.get("requisitions") or []
    total = payload.get("totalCount") or len(reqs)
    ctx = payload.get("context") or {}
    corp = (ctx.get("corp") or "").strip()
    culture_name = (ctx.get("cultureName") or "").strip()

    header = f"# {corp} — Job Board ({total} open positions)" if corp else f"# Job Board ({total} open positions)"
    parts: list[str] = [header, ""]
    if corp:
        parts.append(f"- **corp**: {corp}")
    if culture_name:
        parts.append(f"- **cultureName**: {culture_name}")
    if source_url:
        parts.append(f"- **boardUrl**: {source_url}")
    parts.append("")

    base = source_url.rsplit("/home", 1)[0] + "/home" if source_url else ""
    for j in reqs:
        if not isinstance(j, dict):
            continue
        title = (j.get("displayJobTitle") or "").strip() or "(untitled)"
        line = f"- **{title}**"
        details: list[str] = []
        locs = []
        for loc in (j.get("locations") or []):
            if not isinstance(loc, dict):
                continue
            bits = [v for v in (loc.get("city"), loc.get("state"), loc.get("country")) if isinstance(v, str) and v.strip()]
            if bits:
                locs.append(", ".join(bits))
        if locs:
            details.append("; ".join(locs))
        posted = (j.get("postingEffectiveDate") or "").strip()
        if posted and posted != "-":
            details.append(f"posted {posted}")
        if details:
            line += " — " + " · ".join(details)
        parts.append(line)
        rid = j.get("requisitionId")
        if rid is not None and base:
            parts.append(f"  - {base}/requisition/{rid}")
        desc = _strip_html(j.get("externalDescription") or "")
        if desc:
            snippet = desc[:240] + ("…" if len(desc) > 240 else "")
            parts.append(f"  - {snippet}")

    return "\n".join(parts).rstrip() + "\n"
