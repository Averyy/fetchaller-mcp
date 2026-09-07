"""Transport and parsing for ca.indeed.com.

Two structured surfaces, both in the served HTML — no browser, no API key.

**One request per search, because the second one stopped paying.** A probe run
found the default page was 100% sponsored while ``start=1`` returned a disjoint
all-organic 15 — two requests for thirty postings. On re-test hours later
``start=1`` and ``start=2`` both returned the login wall ("To see more than one
page of jobs"), 131,902 bytes and zero cards, every time.

So the offset is not a stable surface. The client fetches the one page that
reliably answers and reports that it stopped there. If a future probe shows an
offset working again, verify it in the same session as the default page before
trusting it — the difference between the two runs was not the URL.

``page``, ``p``, ``offset`` and ``pagenum`` are silently ignored and return the
default page byte-for-byte. ``filter=0`` and ``limit=50`` are fingerprinted: on
a *fresh* session ``&limit=50`` drew a deterministic Cloudflare 403 while the
byte-identical URL without it returned 200 eight seconds later on that same
session, so it is the parameter, not reputation or rate.

**Search** embeds its result set as JSON in
``window.mosaic.providerData["mosaic-provider-jobcards"]``. Each card already
carries the salary band as numbers (``extractedSalary: {min, max, type}``), so a
search does not need to open 25 postings to report pay.

**A posting** carries ``schema.org/JobPosting`` JSON-LD: the full description,
the band again, and ``validThrough`` — an expiry date no other board here
publishes, which is the direct answer to indexes that list filled requisitions
as open.

Parse the JSON-LD rather than the markup. It is a published schema, so it
survives the redesigns that break CSS selectors, and a posting page renders to
0.7% readable text — the visible HTML is mostly chrome.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import re
import urllib.parse
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..content._html_text import strip_markup
from ..content._json_extract import extract_json_object
from ..ratelimit import indeed_limiter

logger = logging.getLogger(__name__)

SITE = "https://ca.indeed.com"
SEARCH_URL = f"{SITE}/jobs"
VIEWJOB_URL = f"{SITE}/viewjob"

PAGE_SIZE = 15
_MAX_RESPONSE = 16 * 1024 * 1024
_REQUEST_TIMEOUT = 90

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


class IndeedBlockedError(Exception):
    """Indeed refused the request or served a challenge."""


class IndeedUnavailableError(Exception):
    """Indeed failed to answer, as distinct from refusing."""


# A challenge, not merely the presence of Cloudflare's script. `challenge-
# platform` appears on every ordinary Indeed page and is NOT evidence of a
# block — keying on it would report a working board as blocked.
_CHALLENGE_MARKERS = (
    "just a moment",
    "verify you are human",
    "additional verification required",
    "px-captcha",
    "/cdn-cgi/challenge-platform/h/b/orchestrate/managed/",
)


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    timeout=timedelta(seconds=_REQUEST_TIMEOUT),
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=_MAX_RESPONSE,
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


async def _get(session, url: str) -> str:
    await indeed_limiter.wait()
    try:
        resp = await session.get(url)
    except wafer.ChallengeDetected as exc:
        raise IndeedBlockedError(str(exc)) from exc
    if resp.status_code >= 500:
        raise IndeedUnavailableError(f"Indeed returned {resp.status_code}")
    if resp.status_code >= 400:
        raise IndeedBlockedError(f"Indeed returned {resp.status_code}")
    body = resp.text or ""
    lowered = body[:200_000].lower()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS):
        raise IndeedBlockedError("Indeed served a bot challenge")
    return body


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

_MOSAIC_MARKER = 'mosaic-provider-jobcards'
_MOSAIC_ASSIGNMENT = 'window.mosaic.providerData["mosaic-provider-jobcards"]'
_MAX_MOSAIC_CHARS = 4_000_000
_JOBKEY_RE = re.compile(r'data-jk="([a-z0-9]+)"')
_TOTAL_RE = re.compile(r'"totalJobCount"\s*:\s*(\d+)')


def search_url(
    *, query: str = "", location: str = "", start: int | None = None, radius: int = 0
) -> str:
    params: dict[str, str] = {}
    if query:
        params["q"] = query
    if location:
        params["l"] = location
    if radius:
        params["radius"] = str(radius)
    if start is not None:
        params["start"] = str(start)
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"


def _salary(card: dict) -> str:
    """Human-readable band from the numeric fields, not the display string.

    ``extractedSalary`` is numbers; ``salarySnippet.text`` is a pre-formatted
    string that is sometimes an estimate rather than the employer's own figure.
    Preferring the numbers keeps a reported band traceable to the posting.
    """
    extracted = card.get("extractedSalary") or {}
    low, high = extracted.get("min"), extracted.get("max")

    def amount(value):
        # Indeed uses -1 as a missing range endpoint. Treat only finite positive
        # numbers as pay; otherwise a one-sided band rendered as "$35 - $-1".
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if math.isfinite(value) and value > 0 else None

    low, high = amount(low), amount(high)
    unit = str(extracted.get("type") or "").lower()
    suffix = {"hourly": "hourly", "yearly": "annually", "monthly": "monthly"}.get(unit, unit)
    if low and high and low != high:
        return f"${low:,.0f} - ${high:,.0f} {suffix}".strip()
    single = low or high
    if single:
        return f"${single:,.0f} {suffix}".strip()
    snippet = (card.get("salarySnippet") or {}).get("text") or ""
    return " ".join(html.unescape(snippet).split())


class IndeedUnrecognisedPageError(Exception):
    """A 200 whose structure is not a search page."""


def parse_search(body: str) -> tuple[list[dict], int]:
    """``(jobs, board_total)`` from one search page.

    Raises ``IndeedUnrecognisedPageError`` when the page carries no result
    container at all. A login shell, a soft 404 and a markup change all arrive
    as HTTP 200 and all previously parsed to ``jobs=[], total=0`` — reported to
    the caller as a confident "No matching postings" for a search that never
    ran. A genuinely empty result set still HAS the container; its absence means
    this is not the page we asked for.
    """
    if _MOSAIC_MARKER not in body:
        raise IndeedUnrecognisedPageError(
            "Indeed returned a page with no job-card container"
        )
    results: list[dict] = []
    assignment = body.find(_MOSAIC_ASSIGNMENT)
    blob = None
    if assignment >= 0:
        equals = body.find("=", assignment + len(_MOSAIC_ASSIGNMENT))
        if equals >= 0:
            start = equals + 1
            while start < len(body) and body[start].isspace():
                start += 1
            if start < len(body) and body[start] == "{":
                blob = extract_json_object(body, start, _MAX_MOSAIC_CHARS)
    if blob is not None:
        results = (
            ((blob.get("metaData") or {}).get("mosaicProviderJobCardsModel") or {}).get(
                "results"
            )
            or []
        )
    elif assignment >= 0:
        logger.debug("Indeed mosaic blob did not parse")

    jobs: list[dict] = []
    seen: set[str] = set()
    for card in results:
        if not isinstance(card, dict):
            continue
        key = str(card.get("jobkey") or "")
        title = " ".join(html.unescape(str(card.get("title") or "")).split())
        # The key is remote JSON and becomes a URL that is rendered as a
        # markdown link. Unvalidated, a key like `x) [click](javascript:...)`
        # breaks out of the link, and a multi-megabyte one bypasses every field
        # cap. Indeed's keys are short hex; anything else is not one.
        if not key.isalnum() or len(key) > 32:
            continue
        if not title or key in seen:
            continue
        seen.add(key)
        jobs.append(
            {
                "job_key": key,
                "title": title,
                "employer": " ".join(html.unescape(str(card.get("company") or "")).split()),
                "location": " ".join(
                    html.unescape(str(card.get("formattedLocation") or "")).split()
                ),
                "salary": _salary(card),
                "posted": str(card.get("formattedRelativeTime") or "").strip(),
                "url": f"{VIEWJOB_URL}?jk={key}",
            }
        )

    total_match = _TOTAL_RE.search(body)
    total = int(total_match.group(1)) if total_match else 0
    if total > 0 and not jobs:
        raise IndeedUnrecognisedPageError(
            "Indeed reported matching postings but rendered no valid job cards"
        )
    return jobs, total


async def search_page(
    *, session, query: str = "", location: str = "", start: int | None = None, radius: int = 0
) -> str:
    return await _get(
        session, search_url(query=query, location=location, start=start, radius=radius)
    )


async def search_all(
    *, session, query: str = "", location: str = "", radius: int = 0, max_records: int
) -> tuple[list[dict], int, bool]:
    """The one page that reliably answers.

    Deliberately a single request: every offset now returns the login wall, so a
    second fetch would cost a request per search and return nothing. See the
    module docstring for what was measured.

    Returns ``(jobs, board_total, complete)``.
    """
    body = await search_page(session=session, query=query, location=location, radius=radius)
    jobs, total = parse_search(body)
    complete = len(jobs) >= total if total else True
    return jobs[:max_records], total, complete


# --------------------------------------------------------------------------
# Posting detail
# --------------------------------------------------------------------------

# Located by a linear scan, not a regex.
#
# A lazy `<script ...>(.*?)</script>` over remote HTML is quadratic: 20,000
# unterminated openers made each candidate rescan the body, costing 35s of
# synchronous CPU and stalling the event loop for every concurrent request.
# Bounding the window helped but not enough — 20,000 candidates times any useful
# window is still quadratic. str.find() walks the document once.
_LD_OPEN_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>', re.I)
_LD_CLOSE = "</script>"
_MAX_LD_BLOCKS = 40
_MAX_LD_BLOCK_CHARS = 400_000


def _iter_ld_blocks(body: str):
    """Each JSON-LD payload, found in one pass."""
    pos = 0
    seen = 0
    while seen < _MAX_LD_BLOCKS:
        opener = _LD_OPEN_RE.search(body, pos)
        if not opener:
            return
        end = body.find(_LD_CLOSE, opener.end())
        if end == -1:
            return
        if end - opener.end() <= _MAX_LD_BLOCK_CHARS:
            seen += 1
            yield body[opener.end() : end]
        pos = end + len(_LD_CLOSE)


_WS_RE = re.compile(r"[\s\xa0]+")
_MAX_DESCRIPTION_CHARS = 20_000


def _plain(fragment: str) -> str:
    return _WS_RE.sub(" ", html.unescape(strip_markup(fragment or ""))).strip()


def parse_job(body: str) -> dict | None:
    """The JobPosting JSON-LD, flattened. None when the page is not a posting."""
    for block in _iter_ld_blocks(body):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if not isinstance(entry, dict) or entry.get("@type") != "JobPosting":
                continue
            location = entry.get("jobLocation")
            if isinstance(location, list):
                location = location[0] if location else {}
            address = (location or {}).get("address") or {}
            salary = entry.get("baseSalary") or {}
            value = salary.get("value") or {}
            employment = entry.get("employmentType")
            if isinstance(employment, list):
                employment = ", ".join(str(item) for item in employment)
            return {
                "title": _plain(str(entry.get("title") or "")),
                "employer": _plain(str((entry.get("hiringOrganization") or {}).get("name") or "")),
                "location": " ".join(
                    part
                    for part in (
                        address.get("addressLocality"),
                        address.get("addressRegion"),
                    )
                    if part
                ),
                "salary_min": value.get("minValue"),
                "salary_max": value.get("maxValue"),
                "salary_unit": value.get("unitText"),
                "currency": salary.get("currency"),
                "employment_type": _plain(str(employment or "")),
                "posted": str(entry.get("datePosted") or "")[:10],
                # No other board indexed here publishes an expiry. It is the
                # direct answer to indexes that list filled requisitions as open.
                "valid_through": str(entry.get("validThrough") or "")[:10],
                "description": _plain(str(entry.get("description") or ""))[
                    :_MAX_DESCRIPTION_CHARS
                ],
            }
    return None


async def fetch_job(job_key: str, *, session) -> dict | None:
    """One posting, from its JSON-LD.

    This is the mirror the whole client exists for: UKG and PeopleSoft serve a
    list fine and gate the posting body, and the same body is here in plain
    HTML with a salary band and an expiry attached.
    """
    body = await _get(session, f"{VIEWJOB_URL}?jk={job_key}")
    job = parse_job(body)
    if job is None:
        return None
    job["job_key"] = job_key
    job["url"] = f"{VIEWJOB_URL}?jk={job_key}"
    return job
