"""Transport and parsing for emploisfp-psjobs.cfp-psc.gc.ca (GC Jobs).

The board is a Java/JSF application whose search page is delivered in two
halves, and everything awkward about it follows from that.

**The listing is never in the page a URL returns.** A GET of ``page2440`` is a
149KB shell whose body reads "The page is being updated. Please wait...
JavaScript must be enabled". The page's own ``jobSearch.js`` then fetches the
same URL with ``isSecondPartOfPage=1`` appended and drops the response into
``#bodyPart``. So the results are a *second request*, and a fetch of the URL a
caller holds shows a board with nothing on it rather than an error.

**Two flags, and the first one alone is an error.** ``isSecondPartOfPage=1`` on
its own answers a 2KB "Lost Connection / Connexion interrompue" page. Adding
``isInitialNetworkCheck=1`` — the flag the page passes on its first load — is
what unlocks the results. Measured on 2026-09-11: the shell was 149KB with 0
postings, the single flag 2,355 bytes, both flags 134KB with 20 postings.

**The init flag is also the search itself.** Criteria travel in the query
string exactly as the page's GET form sends them — ``title``,
``addedLocation=<code><id>``, ``department=<id>``, ``jobSalaryRange=<n>``,
``officialLanguage=<n>`` — and are stored in the JSF session only when the
init flag is present. A search without it is an HTTP 500 carrying the same
"Lost Connection" page. A page request *with* it resets the stored search to
the whole board, so paging must omit it: page N is
``requestedPage=N&fromPage=N-1&tab=1&log=false&isSecondPartOfPage=1``, which
walks whatever search the session holds. That makes a search a conversation
keyed to ``JSESSIONID``, and two cannot share a session at once — hence
``_exchange_lock``.

An earlier probe sent ``wLocation=232`` (the *criteria-button* index from
``critMap`` in the page's script, not an input name) and POSTed the criteria,
and concluded the location filter could not be applied over GET. Both were
the wrong request. ``addedLocation=W232`` narrowed the board from 414 to 84;
the page echoes every applied criterion back as an ``addTopSearchCritButton``
call, and ``applied_criteria`` reads that echo so a filter the board dropped
is never reported as one it applied.

**The page is 20 rows and the count is on it.** "Jobs open to the public (N)"
is the board's own figure for the executed query; the pagination strip says
"of M" pages. A genuine empty search says "No jobs found" with a count of 0,
and a page past the end renders the strip with no rows.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import urllib.parse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import wafer

from ..config import get_wafer_cache_dir
from ..content._html_text import strip_markup
from ..ratelimit import gcjobs_limiter

logger = logging.getLogger(__name__)

SITE = "https://emploisfp-psjobs.cfp-psc.gc.ca"
SEARCH_URL = f"{SITE}/psrs-srfp/applicant/page2440"
POSTING_URL = f"{SITE}/psrs-srfp/applicant/page1800"

# The board renders twenty rows per page and offers no page-size control.
PAGE_SIZE = 20

_MAX_RESPONSE = 12 * 1024 * 1024

# Closing times on this board are stated in Pacific time ("23:59, Pacific
# Time"), so "has this closed" is decided against the Pacific calendar day.
_BOARD_TZ = ZoneInfo("America/Vancouver")

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()

# One search exchange at a time. Paging reads the search the session stores,
# so two overlapping searches on the shared session would page each other's
# results — and the page that came back would be well-formed and plausible.
_exchange_lock = asyncio.Lock()


class GCJobsBlockedError(Exception):
    """GC Jobs refused the request."""


class GCJobsUnavailableError(Exception):
    """GC Jobs failed to answer, as distinct from refusing."""


class GCJobsSessionError(Exception):
    """The board answered its "Lost Connection" page: the search state was gone."""


class GCJobsUnrecognisedPageError(Exception):
    """A successful response was not a GC Jobs result page."""


class GCJobsNotFoundError(Exception):
    """The board answered 404: an unknown poster id, measured for 0 and 999999999."""


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    """One cached session for the whole process; the search state lives on it."""
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    timeout=timedelta(seconds=60),
                    cache_dir=get_wafer_cache_dir(),
                    max_response_size=_MAX_RESPONSE,
                )
    return _session


async def close_session() -> None:
    global _session, _department_cache
    _session = None
    _department_cache = None


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------

# Both languages of the board's session-loss page, which it serves as a 200
# (bare second-part flag) and as a 500 (a search without the init flag).
_LOST_CONNECTION_RE = re.compile(r"Lost Connection|Connexion interrompue", re.I)


async def _get(session, url: str) -> str:
    await gcjobs_limiter.wait()
    try:
        resp = await session.get(url)
    except wafer.ChallengeDetected as exc:
        raise GCJobsBlockedError(str(exc)) from exc
    text = resp.text or ""
    # Checked before the status: the board spells a lost search state as a
    # 500 when criteria are sent, and that is a protocol fault, not an outage.
    if len(text) < 20_000 and _LOST_CONNECTION_RE.search(text):
        raise GCJobsSessionError(f"{url} answered the Lost Connection page")
    if resp.status_code >= 500:
        raise GCJobsUnavailableError(f"{url} returned {resp.status_code}")
    if resp.status_code == 404:
        raise GCJobsNotFoundError(f"{url} returned 404")
    if resp.status_code >= 400:
        raise GCJobsBlockedError(f"{url} returned {resp.status_code}")
    return text


# --------------------------------------------------------------------------
# Search URLs
# --------------------------------------------------------------------------


def search_params(
    *,
    title_term: str = "",
    locations: list[str] | tuple[str, ...] = (),
    departments: list[str] | tuple[str, ...] = (),
    salary_bands: list[int] | tuple[int, ...] = (),
    language: str = "",
    exclude_various: bool = False,
) -> list[tuple[str, str]]:
    """The query the board's own search form would send, plus the two flags.

    Names are the form's input names, verified against the page: ``title`` is
    a case-insensitive substring match in the given word order; each
    ``addedLocation`` is ``LOCATION_CD + LOCATION_ID`` from the autocomplete;
    ``jobSalaryRange`` repeats once per band; ``officialLanguage`` is 1/2/3
    for English/French/Bilingual; ``variousLocation`` is the "Exclude various
    locations" checkbox.
    """
    params: list[tuple[str, str]] = [("toggleLanguage", "en"), ("tab", "1")]
    if title_term:
        params.append(("title", title_term))
    for code in locations:
        params.append(("addedLocation", code))
    for department in departments:
        params.append(("department", str(department)))
    for band in salary_bands:
        params.append(("jobSalaryRange", str(band)))
    if language:
        params.append(("officialLanguage", language))
    if exclude_various:
        params.append(("variousLocation", "variousLocation"))
    params.append(("search", "Search jobs"))
    params.extend([("isSecondPartOfPage", "1"), ("isInitialNetworkCheck", "1")])
    return params


def search_url(params: list[tuple[str, str]]) -> str:
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"


def page_url(page: int) -> str:
    """Page ``page`` (1-based) of the search the session holds.

    Deliberately without ``isInitialNetworkCheck``: with it, the board resets
    the stored search to the whole board and pages *that* — measured, page 2
    of a 13-page Ontario search came back as page 2 of the 21-page national
    listing, with nothing in the response to say so.
    """
    return (
        f"{SEARCH_URL}?requestedPage={page}&fromPage={page - 1}"
        "&tab=1&log=false&isSecondPartOfPage=1"
    )


# --------------------------------------------------------------------------
# Result parsing
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"[\s\xa0]+")
_TOTAL_RE = re.compile(r"Jobs open to the public\s*\(\s*([\d,]+)\s*\)", re.I)
_NO_JOBS_RE = re.compile(r"No jobs found", re.I)
_PAGELINKS_RE = re.compile(r'<span class="pagelinks">(.*?)</span>', re.S | re.I)
_PAGE_COUNT_RE = re.compile(r"\bof\s+(\d+)\s*\[", re.I)
_ROW_RE = re.compile(r'<li class="searchResult">(.*?)</li>', re.S | re.I)
# The first response of a fresh session — before the cookie is set — carries
# ``;jsessionid=…`` on every href, so the very first search a process runs is
# the one that parsed to zero rows when this anchored on ``page1800?``.
_ROW_TITLE_RE = re.compile(
    r'<a[^>]*href="[^"]*page1800(?:;jsessionid=[^"?]*)?\?poster=(?P<id>\d+)"[^>]*>'
    r"(?P<title>.*?)</a>",
    re.S | re.I,
)
_CELL_RE = re.compile(r'<div class="tableCell">(.*?)</div>', re.S | re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_CLOSING_PREFIX_RE = re.compile(r"^Closing date\s*:\s*", re.I)


def _text(fragment: str) -> str:
    return _WS_RE.sub(" ", html.unescape(strip_markup(fragment))).strip()


def _lines(cell: str) -> list[str]:
    """A table cell's ``<br>``-separated lines, each flattened, empties dropped."""
    out = []
    for part in _BR_RE.split(cell):
        text = _text(part)
        if text:
            out.append(text)
    return out


def board_total(body: str) -> int | None:
    """The board's own count, or None when the page carries none."""
    match = _TOTAL_RE.search(body)
    return int(match.group(1).replace(",", "")) if match else None


def page_count(body: str) -> int:
    """How many pages the strip says there are; 1 when there is no strip."""
    strip = _PAGELINKS_RE.search(body)
    if not strip:
        return 1
    match = _PAGE_COUNT_RE.search(_text(strip.group(1)))
    return int(match.group(1)) if match else 1


def parse_results(body: str) -> list[dict]:
    """Postings on one result page, in the order the board listed them.

    Each row is two cells. The first is ``Closing date: YYYY-MM-DD``, the
    organization (with its branch on a continuation line), the location, and
    sometimes a note after an extra ``<br>``; the second is the language
    requirement and the salary. A row's fields are taken positionally from
    those lines rather than by label, because only the closing date has one.
    """
    jobs: list[dict] = []
    seen: set[str] = set()
    for row_match in _ROW_RE.finditer(body):
        row = row_match.group(1)
        link = _ROW_TITLE_RE.search(row)
        if not link:
            continue
        poster = link.group("id")
        title = _text(link.group("title"))
        if not title or poster in seen:
            continue
        seen.add(poster)
        cells = [_lines(c) for c in _CELL_RE.findall(row)]
        first = cells[0] if cells else []
        second = cells[1] if len(cells) > 1 else []

        closing = ""
        if first and _CLOSING_PREFIX_RE.match(first[0]):
            closing = _CLOSING_PREFIX_RE.sub("", first[0]).strip()
            first = first[1:]
        organization = first[0] if first else ""
        location = first[1] if len(first) > 1 else ""
        note = " ".join(first[2:]) if len(first) > 2 else ""

        language = ""
        salary = ""
        if len(second) >= 2:
            language, salary = second[0], " ".join(second[1:])
        elif len(second) == 1:
            if second[0].startswith("$"):
                salary = second[0]
            else:
                language = second[0]

        jobs.append(
            {
                "poster_id": poster,
                "title": title,
                # Rebuilt from the id, never from the href, so the board's
                # markup cannot steer a caller elsewhere.
                "url": f"{POSTING_URL}?poster={poster}",
                "closing_date": closing,
                "organization": organization,
                "location": location,
                "language": language,
                "salary": salary,
                "note": note,
            }
        )
    return jobs


def parse_search_response(body: str) -> tuple[list[dict], int, int]:
    """``(jobs, board_total, pages)`` from a recognised result page.

    A page with no count and no rows is not an empty search: the shell, the
    Lost Connection page and a changed layout all look like that.
    """
    total = board_total(body)
    jobs = parse_results(body)
    if total is None:
        if jobs:
            return jobs, len(jobs), page_count(body)
        raise GCJobsUnrecognisedPageError(
            "GC Jobs answered without a result count or any postings"
        )
    if total == 0 and not jobs and not _NO_JOBS_RE.search(body):
        raise GCJobsUnrecognisedPageError(
            "GC Jobs reported zero postings without its empty-result marker"
        )
    if total > 0 and not jobs and page_count(body) <= 1:
        raise GCJobsUnrecognisedPageError(
            "GC Jobs reported matching postings but rendered no result rows"
        )
    return jobs, total, page_count(body)


# The page echoes every criterion the server applied as one of these calls,
# e.g.  addTopSearchCritButton("wLocation", "addedLocation"+"W"+ 232, "St. …
# That echo is the only proof a filter was honoured; an ignored parameter
# produces a normal page with the whole board on it.
_CRIT_CALL_RE = re.compile(r"addTopSearchCritButton\(([^)]*)\)", re.S)
_CRIT_TYPE_RE = re.compile(r'^\s*"(\w+)"')
# A province is echoed as  "addedLocation"+"P"+ 4  and a city, differently,
# as  "addedLocation"+"W" + "232"  — the id quoted. Both are the same value.
_CRIT_LOCATION_RE = re.compile(r'"addedLocation"\s*\+\s*"(\w)"\s*\+\s*"?(\d+)"?')
_CRIT_SALARY_RE = re.compile(r'"jobSalaryRange(\d+)"')


def applied_criteria(body: str) -> dict[str, list[str]]:
    """``{criterion_type: [values]}`` the board says it applied.

    Location values are ``W232``-style codes and salary values band numbers;
    the other criteria (``jobTtile`` — the board's own spelling —
    ``organization``, ``lang``, ``datePosted``) are echoed by presence only,
    with an empty string as their value.
    """
    out: dict[str, list[str]] = {}
    for call in _CRIT_CALL_RE.finditer(body):
        args = call.group(1)
        kind = _CRIT_TYPE_RE.match(args)
        if not kind:
            continue
        values = out.setdefault(kind.group(1), [])
        location = _CRIT_LOCATION_RE.search(args)
        salary = _CRIT_SALARY_RE.search(args)
        if location:
            values.append(f"{location.group(1)}{location.group(2)}")
        elif salary:
            values.append(salary.group(1))
        else:
            values.append("")
    return out


# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

# The organization list ships inline on every result page as three parallel
# assignments per entry: id, name, abbreviation.
_DEPARTMENT_RE = re.compile(
    r"departmentsIdNameAbbreviation\[(\d+)\]\[0\]\s*=\s*(\d+);\s*"
    r"departmentsIdNameAbbreviation\[\1\]\[1\]\s*=\s*\"([^\"]*)\";\s*"
    r"departmentsIdNameAbbreviation\[\1\]\[2\]\s*=\s*'([^']*)';"
)


def parse_departments(body: str) -> dict[str, tuple[str, str]]:
    """``{id: (name, abbreviation)}`` from a result page's inline script."""
    out: dict[str, tuple[str, str]] = {}
    for _index, dept_id, name, abbreviation in _DEPARTMENT_RE.findall(body):
        name = " ".join(html.unescape(name).split())
        abbreviation = " ".join(html.unescape(abbreviation).split())
        if name:
            out[dept_id] = (name, abbreviation)
    return out


_department_cache: dict[str, tuple[str, str]] | None = None
_department_lock = asyncio.Lock()


async def get_departments(session) -> dict[str, tuple[str, str]]:
    """The organization vocabulary, read once per process.

    It is on every result page and nowhere else — the shell a plain GET
    returns has no form at all — so learning it costs one unfiltered search.
    That request replaces the session's stored search, which is why it runs
    under the exchange lock.
    """
    global _department_cache
    if _department_cache is None:
        async with _department_lock:
            if _department_cache is None:
                async with _exchange_lock:
                    body = await _get(session, search_url(search_params()))
                parsed = parse_departments(body)
                if parsed:
                    _department_cache = parsed
                else:
                    logger.debug("GC Jobs result page carried no organization list")
                    return parsed
    return _department_cache


async def lookup_locations(session, text: str) -> list[dict]:
    """The autocomplete's entries for ``text``: LOCATION_CD, LOCATION_DESC, LOCATION_ID.

    ``LOCATION_CD`` is ``P`` for a province and ``W`` for a place. The match
    is a substring anywhere in the name, so the caller decides what is exact.
    """
    query = urllib.parse.urlencode({"ajaxFilter": text})
    body = await _get(session, f"{SEARCH_URL}?{query}")
    try:
        entries = json.loads(body)
    except ValueError:
        logger.debug("GC Jobs location filter returned no JSON")
        return []
    if not isinstance(entries, list):
        return []
    return [
        e
        for e in entries
        if isinstance(e, dict)
        and e.get("ERROR_MESSAGE") != "ERROR"
        and e.get("LOCATION_CD")
        and e.get("LOCATION_ID") is not None
    ]


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


async def search_all(
    *, session, params: list[tuple[str, str]], max_records: int
) -> tuple[list[dict], int, bool, dict[str, list[str]]]:
    """Run a search and page through it up to ``max_records``.

    Returns ``(jobs, board_total, complete, applied)``. ``applied`` is what the
    board echoed back as the criteria it honoured. Paging is sequential and
    stateful — see ``page_url`` — so the whole exchange runs under the lock.

    ``complete`` is False only when ``max_records`` stopped the walk with
    pages still unread. Reaching the strip's last page is complete even when
    the board's count is higher than the distinct rows its pages served —
    measured, St. Catharines counted 84 and served 83 — because the gap is
    the board's arithmetic, and nothing a narrower query would recover.
    """
    async with _exchange_lock:
        body = await _get(session, search_url(params))
        jobs, total, pages = parse_search_response(body)
        applied = applied_criteria(body)
        seen = {job["poster_id"] for job in jobs}

        reachable = min(max_records, total)
        page = 2
        while len(jobs) < reachable and page <= pages:
            page_jobs, _, _ = parse_search_response(await _get(session, page_url(page)))
            fresh = [job for job in page_jobs if job["poster_id"] not in seen]
            if not fresh:
                break
            seen.update(job["poster_id"] for job in fresh)
            jobs.extend(fresh)
            page += 1

    cut_by_ceiling = len(jobs) >= max_records and len(jobs) < total and page <= pages
    return jobs[:max_records], total, not cut_by_ceiling, applied


# --------------------------------------------------------------------------
# Job detail
# --------------------------------------------------------------------------

_MAIN_RE = re.compile(r"<main\b.*?</main>", re.S | re.I)
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_ORG_RE = re.compile(r'<h2 class="pst-h2">(.*?)</h2>', re.S | re.I)
_CLOSING_RE = re.compile(r'<h3 class="pst-h3[^"]*">(.*?)</h3>', re.S | re.I)
_LEAVE_RE = re.compile(
    r"You will leave the\s*(?:<abbr[^>]*>\s*GC\s*</abbr>|GC)\s*Jobs Web site", re.S | re.I
)
_LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_FACT_RE = re.compile(r"<b>\s*([^<]{1,60}?)\s*</b>\s*<br\s*/?>(.*?)(?=<b>|</div>)", re.S | re.I)
_CLASSIFICATION_RE = re.compile(r"\s*-\s*Classification\s*:\s*(.+)$", re.I)
# Only the labelled facts of the 2001 template. A looser label pattern also
# captured "OUR REQUIREMENTS:" and ran it to the next bold heading, turning
# the whole requirements list into a fact.
_LEGACY_FACT_RE = re.compile(
    r"<strong>\s*(Positions|Location|Salary|Deadline)\s*:\s*</strong>\s*(.*?)(?=<strong>|</p>)",
    re.S | re.I,
)
_MONTH_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})",
    re.I,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# Facts read from the left column of a modern posting, in the order rendered.
_FACT_LABELS = (
    "Reference number",
    "Selection process number",
    "Location",
    "Salary",
    "Who can apply",
    "Positions to be filled",
    "Language requirements",
)


def parse_closing_date(text: str) -> date | None:
    """A date from either spelling the board uses, or None."""
    match = _MONTH_DATE_RE.search(text or "")
    if match:
        try:
            return datetime.strptime(
                f"{match.group(1).title()} {match.group(2)} {match.group(3)}", "%B %d %Y"
            ).date()
        except ValueError:
            return None
    match = _ISO_DATE_RE.search(text or "")
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def board_today() -> date:
    return datetime.now(_BOARD_TZ).date()


def _closed(closing: date | None) -> bool:
    return closing is not None and closing < board_today()


def _main(body: str) -> str:
    match = _MAIN_RE.search(body)
    return _SCRIPT_RE.sub("", match.group(0) if match else body)


def parse_job(body: str, poster_id: str = "") -> dict | None:
    """One posting page, or None when the page is not one.

    Three shapes come back for ``page1800?poster=N``:

    - **external**: "You will leave the GC Jobs Web site" — the organization
      hosts the posting itself and GC Jobs serves only a departure notice, with
      no closing date, location, salary or eligibility. Eleven of twenty
      postings enumerated on 2026-09-11 were this. Parsing it as a posting
      yields a row with every field empty.
    - **posting**: the current template — title, organization, closing date,
      a facts column, and the body sections.
    - **legacy**: a 2001-era table ("Deadline: October 12, 2001"), still served
      for low poster ids as though live. The closing date is parsed from it so
      the renderer can say it closed two decades ago.
    """
    main = _main(body)

    if _LEAVE_RE.search(main):
        link = None
        for href, text in _LINK_RE.findall(main):
            if href.startswith(("http://", "https://")):
                link = (href, _text(text))
                break
        return {
            "kind": "external",
            "poster_id": poster_id,
            "title": link[1] if link else "",
            "external_url": html.unescape(link[0]) if link else "",
            "url": f"{POSTING_URL}?poster={poster_id}",
        }

    heading = _H1_RE.search(main)
    title = _text(heading.group(1)) if heading else ""
    # A heading alone proves nothing — the Lost Connection page has two. The
    # posting template is recognised by its closing-date heading or its
    # facts box.
    is_posting = bool(_CLOSING_RE.search(main)) or "well-sub-section" in main
    if title and is_posting:
        org = _ORG_RE.search(main)
        closing_match = _CLOSING_RE.search(main)
        closing_text = ""
        if closing_match:
            closing_text = _CLOSING_PREFIX_RE.sub("", _text(closing_match.group(1)))
        facts: dict[str, str] = {}
        right = main.find('<div class="right-box"')
        left = main[:right] if right >= 0 else main
        for label, value in _FACT_RE.findall(left):
            label = _text(label)
            value = _text(value)
            if label in _FACT_LABELS and value and label not in facts:
                facts[label] = value[:600]
        classification = ""
        salary = facts.get("Salary", "")
        if salary:
            match = _CLASSIFICATION_RE.search(salary)
            if match:
                classification = match.group(1).strip()
                facts["Salary"] = salary[: match.start()].strip()
        closing = parse_closing_date(closing_text)
        return {
            "kind": "posting",
            "poster_id": poster_id,
            "title": title,
            "organization": _text(org.group(1)) if org else "",
            "closing_text": closing_text,
            "closing_date": closing,
            "closed": _closed(closing),
            "facts": facts,
            "classification": classification,
            "url": f"{POSTING_URL}?poster={poster_id}",
            "body_html": main[right:] if right >= 0 else "",
        }

    legacy = _LEGACY_FACT_RE.findall(main)
    if legacy and "<table" in main.casefold():
        facts = {}
        for label, value in legacy:
            label = _text(label).title()
            value = _text(value)
            if value and label not in facts:
                facts[label] = value[:600]
        title_match = re.search(r"<p>\s*([^<]{3,200}?)\s*</p>", main, re.S)
        org_match = re.search(r"<td>\s*([^<]{2,200}?)\s*</td>", main, re.S)
        closing_text = facts.pop("Deadline", "")
        closing = parse_closing_date(closing_text)
        return {
            "kind": "legacy",
            "poster_id": poster_id,
            "title": _text(title_match.group(1)) if title_match else "",
            "organization": _text(org_match.group(1)) if org_match else "",
            "closing_text": closing_text,
            "closing_date": closing,
            "closed": _closed(closing),
            "facts": facts,
            "classification": "",
            "url": f"{POSTING_URL}?poster={poster_id}",
            "body_html": main,
        }

    return None


async def fetch_job(poster_id: str, *, session) -> dict | None:
    """One posting by poster id, in whichever shape the board serves it.

    None for a 404, which is what an unknown id gets — unlike gojobs, the
    board does not answer 200 with a shell here.
    """
    try:
        body = await _get(session, f"{POSTING_URL}?toggleLanguage=en&poster={poster_id}")
    except GCJobsNotFoundError:
        return None
    return parse_job(body, poster_id)
