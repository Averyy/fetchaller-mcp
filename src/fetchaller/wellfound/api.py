"""wellfound.com client — SSR fetch + Next.js/Apollo + JSON-LD extraction.

wellfound is behind DataDome; wafer 0.2.4 returns the real SSR document via its
browser passthrough, so this module never touches challenges — it just fetches
and parses. All structured data is in the initial document:

- search + company pages: ``__NEXT_DATA__`` -> ``props.pageProps.apolloState``
  (a normalized Apollo cache keyed ``Type:id`` with ``{"__ref": ...}`` links)
- job-detail pages: a ``schema.org/JobPosting`` JSON-LD block (no Apollo)
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from urllib.parse import urlparse

import wafer
from bs4 import BeautifulSoup

from ..config import get_wafer_cache_dir

SITE = "https://wellfound.com"
_HOSTS = ("wellfound.com", "www.wellfound.com")


# ---------------------------------------------------------------------------
# Shared session
# ---------------------------------------------------------------------------

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    """Shared session. browser_solver lets wafer pass DataDome; cache_dir
    persists the earned DataDome cookie so we re-solve rarely; rate_limit keeps
    us polite (DataDome escalates on bursts)."""
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    timeout=timedelta(seconds=90),
                    rate_limit=2.5,
                    rate_jitter=0.5,
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=10 * 1024 * 1024,
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_JOB_RE = re.compile(r"^/jobs/(\d+)(?:[-/]|$)")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_wellfound(url: str) -> bool:
    return _host(url) in _HOSTS


def is_wellfound_job(url: str) -> bool:
    return _host(url) in _HOSTS and bool(_JOB_RE.match(urlparse(url).path))


def is_wellfound_company(url: str) -> bool:
    return _host(url) in _HOSTS and urlparse(url).path.startswith("/company/")


def is_wellfound_search(url: str) -> bool:
    if _host(url) not in _HOSTS:
        return False
    path = urlparse(url).path.rstrip("/")
    return path == "/jobs" or path.startswith("/role/") or path.startswith("/location/")


def extract_job_id(url: str) -> str | None:
    m = _JOB_RE.match(urlparse(url).path)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Fetch + document parsing
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


async def fetch_html(url: str, browser_solver=None, timeout: float | None = None) -> str:
    """Fetch a wellfound page, bounded by the caller's remaining budget.

    Without ``timeout`` this inherits the shared session's 90s total — which is
    the budget for solving a DataDome challenge, not one a caller asking for 10s
    ever agreed to. The fetch tool's outer deadline stops the caller waiting on
    it either way, but only passing the real budget down keeps the request from
    running on past the answer it can no longer be used for.
    """
    s = await _get_session(browser_solver)
    kwargs = {}
    if timeout is not None and timeout > 0:
        kwargs["timeout"] = timedelta(seconds=timeout)
    r = await s.get(url, **kwargs)
    r.raise_for_status()
    return r.text


def parse_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def apollo_cache(next_data: dict) -> dict:
    """The normalized Apollo cache (``{"Type:id": {...}}``)."""
    pp = (next_data.get("props") or {}).get("pageProps") or {}
    state = pp.get("apolloState") or {}
    return state.get("data", state) or {}


def jsonld_jobposting(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# wellfound's 404 <title> is "Page not found - 404 | Wellfound". Match only that
# specific phrase — a bare "404" or "not found" substring would false-positive on
# real job titles (e.g. "404 Response Engineer", "Lost & Found Lead").
_NOT_FOUND_RE = re.compile(r"page not found", re.IGNORECASE)


def is_not_found_page(html: str) -> bool:
    """wellfound serves a 200 'Page not found - 404' shell for unresolvable URLs
    (e.g. a bare ``/jobs/{id}`` with no slug, or an expired job). Detect it so we
    can return an actionable error instead of a generic 'no data' message."""
    m = _TITLE_RE.search(html)
    return bool(m and _NOT_FOUND_RE.search(m.group(1)))


# ---------------------------------------------------------------------------
# Apollo cache navigation
# ---------------------------------------------------------------------------


def deref(cache: dict, value):
    """Resolve a ``{"__ref": "Key"}`` (or list of them) against the cache."""
    if isinstance(value, dict):
        if "__ref" in value:
            return cache.get(value["__ref"], {})
        return value
    if isinstance(value, list):
        return [deref(cache, v) for v in value]
    return value


def entities(cache: dict, typename: str) -> list[dict]:
    """All cache objects of a given ``__typename``."""
    return [v for v in cache.values() if isinstance(v, dict) and v.get("__typename") == typename]


def connection(obj: dict, prefix: str) -> dict:
    """Apollo stores field-with-args as ``field({"first":10})``. Return the first
    value whose key starts with ``prefix`` and looks like a connection/object."""
    best = None
    for key, val in obj.items():
        if key == prefix or key.startswith(prefix + "("):
            if isinstance(val, dict):
                if "edges" in val or "totalCount" in val:
                    return val
                best = best or val
    return best or {}


def connection_nodes(cache: dict, obj: dict, prefix: str) -> list[dict]:
    conn = connection(obj, prefix)
    nodes = []
    for edge in conn.get("edges") or []:
        node = deref(cache, edge.get("node")) if isinstance(edge, dict) else {}
        if node:
            nodes.append(node)
    return nodes
