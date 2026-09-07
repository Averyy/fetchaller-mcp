"""Transport and parsing for jobbank.gc.ca (federal Job Bank).

**A location only filters when it carries the city's numeric id.** The obvious
request — ``?locationstring=St.+Catharines%2C+ON`` — is answered with HTTP 200
and the *entire national* result set. Measured: that URL returned **64,017**,
and so did ``locationstring=Toronto%2C+ON`` and ``locationstring=nonsensexyz``
and omitting the parameter altogether. The string is not validated, not
honoured, and not complained about.

That is the dangerous shape of failure: the page renders a normal, successful
search under a heading naming the city, so anything reading it concludes "these
are the St. Catharines jobs" and is wrong by two orders of magnitude.

The filter is ``locationparam`` (the page's own pagination spells the same value
``mid``), carrying the numeric ``city_id`` from Job Bank's Solr autocomplete.
With it, St. Catharines returns 1,036 at the default 50km radius and Toronto
8,630.

Read those counts from ``id="results-count"`` and nowhere else — see
``_COUNT_RE``. Taking them from the page text instead returns a distance-facet
badge, which is how an earlier pass recorded 8,963 / 148 / 2,162 here and had to
be redone.

So this module never sends a location it could not resolve to an id. Falling
back to the bare string would look like a filter and be none.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import urllib.parse
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..content._html_text import strip_markup
from ..ratelimit import jobbank_limiter

logger = logging.getLogger(__name__)

SITE = "https://www.jobbank.gc.ca"
SEARCH_URL = f"{SITE}/jobsearch/jobsearch"
SUGGEST_URL = f"{SITE}/core/ta-cityprovsuggest_en/select"
POSTING_URL = f"{SITE}/jobsearch/jobposting"

# Job Bank renders 25 result cards per page and offers no page-size control.
PAGE_SIZE = 25

# The board is slow — a filtered search page routinely takes 30-60s and a 90s
# ceiling produced real timeouts during development.
_REQUEST_TIMEOUT = 180
_MAX_RESPONSE = 12 * 1024 * 1024

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()

# Deliberately no exchange lock, unlike gojobs.
#
# Every parameter of a Job Bank search lives in the URL, so the server keeps no
# per-session search state and overlapping requests cannot cross. Verified:
# St. Catharines and Toronto searches run sequentially return 439 and 5,920, and
# the same two under asyncio.gather return 439 and 5,920. gojobs needs a lock
# because its WebForms exchange is a stateful conversation keyed to a cookie;
# copying that here would serialise a board that is already slow, for nothing.


class JobBankBlockedError(Exception):
    """Job Bank refused the request."""


class JobBankUnavailableError(Exception):
    """Job Bank failed to answer, as distinct from refusing."""


class JobBankUnrecognisedPageError(Exception):
    """A successful response was not a Job Bank search-results page."""


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
    await jobbank_limiter.wait()
    try:
        resp = await session.get(url)
    except wafer.ChallengeDetected as exc:
        raise JobBankBlockedError(str(exc)) from exc
    if resp.status_code >= 500:
        raise JobBankUnavailableError(f"{url} returned {resp.status_code}")
    if resp.status_code >= 400:
        raise JobBankBlockedError(f"{url} returned {resp.status_code}")
    return resp.text or ""


# --------------------------------------------------------------------------
# Location resolution
# --------------------------------------------------------------------------

_PROVINCES = {
    "ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk", "yt",
}


def _split_place(value: str) -> tuple[str, str]:
    """``"St. Catharines, ON"`` -> ``("St. Catharines", "ON")``."""
    text = " ".join((value or "").split())
    if "," in text:
        city, _, prov = text.rpartition(",")
        prov = prov.strip()
        if prov.casefold() in _PROVINCES:
            return city.strip(), prov.upper()
    return text, ""


async def resolve_city(session, location: str) -> dict | None:
    """Job Bank's own record for a place name, or None.

    Queries the Solr autocomplete the search box uses. ``fq=NOT
    postalcode_cnt:0`` mirrors the site's own filter, which drops entries with
    no postal coverage.
    """
    city, province = _split_place(location)
    if not city:
        return None
    query = urllib.parse.urlencode(
        {"q": city, "fq": "NOT postalcode_cnt:0", "wt": "json", "rows": 25}
    )
    body = await _get(session, f"{SUGGEST_URL}?{query}")
    try:
        docs = json.loads(body)["response"]["docs"]
    except (ValueError, KeyError, TypeError):
        logger.debug("Job Bank city suggest returned no parseable docs")
        return None

    candidates = [d for d in docs if d.get("city_id")]
    if province:
        scoped = [d for d in candidates if (d.get("province_cd") or "").upper() == province]
        # A province that matches nothing is a contradiction, not a hint to
        # widen: "Toronto, BC" must not quietly return Toronto, Ontario.
        candidates = scoped
    folded = city.casefold()
    exact = [d for d in candidates if (d.get("name") or "").casefold() == folded]
    return (exact or candidates or [None])[0]


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"[\s\xa0]+")
# Screen-reader-only labels ("Location", "Salary", "Job number:") share the
# markup with the values they label, so they have to go before the text is
# flattened or every field arrives with its own label glued on.
_INVISIBLE_RE = re.compile(r'<span class="wb-inv">.*?</span>', re.S | re.I)
# The result total, and only this. The page is littered with other counts that
# read exactly like it: every distance facet renders
# ``<span class="badge">150 <span class="wb-inv">jobs found in</span></span>
# 10km``, so a "N jobs found" text match silently returns whichever facet
# happened to come first — 152 for the 10km option on a search whose real
# total was 1,036. Every figure in this module is a snapshot: the board's
# counts move daily as postings open and close, so treat them as orders of
# magnitude, not fixtures.
_COUNT_RE = re.compile(r'id="results-count"[^>]*>\s*([\d,]+)\s*<', re.I)
_ARTICLE_OPEN_RE = re.compile(r'<article id="article-(?P<id>\d+)"')
_ARTICLE_CLOSE = "</article>"
_MAX_ARTICLE_CHARS = 60_000
# The posting id as the *link* spells it. Preferred over the id on the
# surrounding <article> element: they agree today, and if they ever stop
# agreeing the link is the one that resolves.
_HREF_ID_RE = re.compile(r'href="/jobsearch/jobposting/(?P<id>\d+)')


def _text(fragment: str) -> str:
    fragment = _INVISIBLE_RE.sub(" ", fragment)
    return _WS_RE.sub(" ", html.unescape(strip_markup(fragment))).strip()


def _articles(body: str):
    """Yield bounded article fragments in one forward pass."""

    cursor = 0
    while True:
        opener = _ARTICLE_OPEN_RE.search(body, cursor)
        if opener is None:
            return
        scan_end = min(len(body), opener.start() + _MAX_ARTICLE_CHARS)
        next_opener = _ARTICLE_OPEN_RE.search(body, opener.end(), scan_end)
        if next_opener is not None:
            scan_end = next_opener.start()
        close = body.find(_ARTICLE_CLOSE, opener.end(), scan_end)
        if close >= 0:
            end = close + len(_ARTICLE_CLOSE)
            yield opener, body[opener.start() : end]
            cursor = end
        elif next_opener is not None:
            cursor = next_opener.start()
        else:
            cursor = scan_end


def _field(article: str, css_class: str) -> str:
    match = re.search(rf'<li class="{css_class}"[^>]*>(.*?)</li>', article, re.S)
    return _text(match.group(1)) if match else ""


def _span(article: str, css_class: str) -> str:
    match = re.search(rf'<span class="{css_class}"[^>]*>(.*?)</span>', article, re.S)
    return _text(match.group(1)) if match else ""


def board_total(body: str) -> int:
    """Job Bank's own match count for the executed query.

    Read from the markup, not the flattened text: see ``_COUNT_RE``.
    """
    match = _COUNT_RE.search(body)
    return int(match.group(1).replace(",", "")) if match else 0


def parse_results(body: str) -> list[dict]:
    """Postings on one result page."""
    jobs: list[dict] = []
    for match, article in _articles(body):
        job_id = match.group("id")
        title = ""
        title_match = re.search(r'<span class="noctitle">(.*?)</span>', article, re.S)
        if title_match:
            title = _text(title_match.group(1))
        if not title:
            continue
        # Rebuilt from the id rather than taken from the href, which carries a
        # ;jsessionid= path parameter and a source= query — session scratch, not
        # identity. A link handed to someone else must not carry a session.
        href_match = _HREF_ID_RE.search(article)
        posting_id = href_match.group("id") if href_match else job_id
        url = f"{POSTING_URL}/{posting_id}"
        salary = _field(article, "salary")
        jobs.append(
            {
                "job_id": job_id,
                "title": title,
                "url": url,
                "employer": _field(article, "business"),
                "location": _field(article, "location"),
                "salary": re.sub(r"^Salary\s*", "", salary).strip(),
                "posted": _field(article, "date"),
                "workplace": _span(article, "telework"),
            }
        )
    return jobs


def parse_search_page(body: str) -> tuple[list[dict], int]:
    """Postings and total from a recognized search page.

    Job Bank uses HTTP 200 for pages that are not search results. Missing the
    result-count element cannot therefore mean zero: a genuine empty search
    still renders ``results-count`` with a value of 0. Treating its absence as
    zero turned sign-in shells and markup changes into "No matching postings."
    """
    match = _COUNT_RE.search(body)
    if match is None:
        raise JobBankUnrecognisedPageError(
            "Job Bank returned a page with no results-count element"
        )
    total = int(match.group(1).replace(",", ""))
    jobs = parse_results(body)
    if total > 0 and not jobs:
        raise JobBankUnrecognisedPageError(
            "Job Bank reported matching postings but rendered no result cards"
        )
    return jobs, total


async def search_page(
    *,
    session,
    keywords: str = "",
    city_id: str = "",
    location_label: str = "",
    radius_km: int = 0,
    page: int = 1,
    sort: str = "M",
) -> str:
    """One page of results.

    ``city_id`` is the only thing that scopes the search geographically;
    ``location_label`` is cosmetic and is sent only to keep the request
    recognisable, never as the filter. See the module docstring.

    ``radius_km`` is the ``d`` parameter and is a real filter — measured from
    St. Catharines: 10 km gives 152 postings, 25 km 424, the default 50 km
    1,036 and 100 km 10,327.
    """
    params: dict[str, str] = {"searchstring": keywords or "", "sort": sort}
    if city_id:
        params["locationparam"] = str(city_id)
        if location_label:
            params["locationstring"] = location_label
        if radius_km:
            params["d"] = str(radius_km)
    if page > 1:
        params["page"] = str(page)
    return await _get(session, f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")


async def unfiltered_total(
    *, session, city_id: str, location_label: str = "", radius_km: int = 0
) -> int:
    """The board's count for this location with no keyword at all.

    Used to catch a keyword the board dropped. ``searchstring`` filters for most
    terms — measured within 25 km of St. Catharines, "driver" gives 11, "nurse"
    6, "welder" 3 — but "assistant" returned 424, byte-identical to the
    unfiltered page down to the first five titles. There is no error and no
    marker; the query heading simply sits above the whole location slice.
    """
    body = await search_page(
        session=session,
        keywords="",
        city_id=city_id,
        location_label=location_label,
        radius_km=radius_km,
    )
    _, total = parse_search_page(body)
    return total


async def search_all(
    *,
    session,
    keywords: str = "",
    city_id: str = "",
    location_label: str = "",
    radius_km: int = 0,
    sort: str = "M",
    max_records: int,
) -> tuple[list[dict], int, bool]:
    """Page through results up to ``max_records``.

    Returns ``(jobs, board_total, complete)``. The first page's board total is
    the pagination target. Once that many records have been collected there is
    no reason to make one more request merely to receive an empty page. A page
    that stops yielding new postings still terminates the loop defensively.
    """
    body = await search_page(
        session=session,
        keywords=keywords,
        city_id=city_id,
        location_label=location_label,
        radius_km=radius_km,
        sort=sort,
    )
    jobs, total = parse_search_page(body)
    seen = {job["job_id"] for job in jobs}

    reachable = min(max_records, total)
    page = 2
    while len(jobs) < reachable and jobs:
        body = await search_page(
            session=session,
            keywords=keywords,
            city_id=city_id,
            location_label=location_label,
            radius_km=radius_km,
            page=page,
            sort=sort,
        )
        page_jobs, _ = parse_search_page(body)
        fresh = [job for job in page_jobs if job["job_id"] not in seen]
        if not fresh:
            break
        seen.update(job["job_id"] for job in fresh)
        jobs.extend(fresh)
        page += 1

    complete = len(jobs) >= total if total else True
    return jobs[:max_records], total, complete
