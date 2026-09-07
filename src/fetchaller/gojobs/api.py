"""Transport and parsing for gojobs.gov.on.ca (Ontario Public Service Careers).

The board is ASP.NET WebForms, which constrains this module in three ways that
are not obvious from the URLs.

**The listing does not exist in any GET.** ``Search.aspx`` answers a GET with the
search *form* and no postings at all — 65KB of HTML holding zero job links. The
results come only from POSTing the form back with ``__VIEWSTATE``,
``__VIEWSTATEGENERATOR`` and ``__EVENTVALIDATION`` echoed from the page that
issued them. A caller who fetches the URL and reads the HTML sees an empty board
rather than an error, so treating the GET as the listing fails silently.

**Paging is a postback, not a URL.** Page N is ``__EVENTTARGET`` set to
``ctl00$MainContent$lnkButton_Page{N}``, carrying the ``__VIEWSTATE`` of the
*previous* response. Pages therefore cannot be fetched in parallel or out of
order: each one's request body is built from the last one's reply.

**Facet values are JSON arrays, not scalars.** ``ucRegion$hiddenSelected`` set to
``REGION-TRNT`` is accepted and silently ignored — the response comes back with
the full unfiltered count. It has to be ``["REGION-TRNT"]``. Measured: the bare
string returned all 127 postings, the JSON array returned 35. A filter that
looks applied and is not would report the whole board under a narrowed heading.

There is **no keyword or title field** on this board. The only text input is an
exact ``Job ID`` lookup. Title matching is therefore entirely ours to do — see
``search.py``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import timedelta

import wafer

from ..config import get_wafer_cache_dir
from ..content._html_text import strip_markup
from ..ratelimit import gojobs_limiter

logger = logging.getLogger(__name__)

SITE = "https://www.gojobs.gov.on.ca"
SEARCH_URL = f"{SITE}/Search.aspx"
PREVIEW_URL = f"{SITE}/Preview.aspx"

# The board renders a fixed ten result rows per page and offers no page-size
# control, so the page count is entirely a function of how many postings match.
PAGE_SIZE = 10

_MAX_RESPONSE = 12 * 1024 * 1024

_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()

# One search exchange at a time.
#
# A WebForms search is a conversation, not a request: GET the form, POST it
# back, then POST again for each page carrying the previous reply's
# __VIEWSTATE. The server keys that conversation to the session cookie, and the
# session here is shared process-wide — so two concurrent searches interleave
# their GETs and POSTs and the server answers each with the other's state.
#
# Measured, running a Toronto and a Thunder Bay search under asyncio.gather:
# Toronto came back 35 (the count for REGION-TRNT, which it never asked for)
# and Thunder Bay came back 127 (the unfiltered board), where the same two
# searches run one after another correctly return 32 and 17. Both answers were
# well-formed and plausible, and both were wrong — nothing in the response says
# a filter was dropped. Sequentially the shared session is stable; it is only
# overlap that corrupts it, so the exchange is serialised rather than the
# session being made per-call.
_exchange_lock = asyncio.Lock()


class GoJobsBlockedError(Exception):
    """gojobs refused the request."""


class GoJobsUnavailableError(Exception):
    """gojobs failed to answer, as distinct from refusing."""


class GoJobsProtocolError(Exception):
    """The page did not carry the WebForms state a postback needs."""


class GoJobsUnrecognisedPageError(GoJobsProtocolError):
    """A successful postback was not a search-results page."""


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    """One cached session for the whole process.

    Deliberately shared rather than per-call. The board sits behind Radware Bot
    Manager, whose interstitial banks its clearance on the jar; wafer clears it
    transparently, but a fresh jar per search would pay that round trip every
    time. It is also required for correctness here: the WebForms exchange is
    stateful, and ``PHPSESSID`` has to survive from the form GET to the POST.
    """
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
    global _session, _vocab_cache
    _session = None
    _vocab_cache = None


# --------------------------------------------------------------------------
# WebForms state
# --------------------------------------------------------------------------

_STATE_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


def _hidden(body: str, name: str) -> str:
    """Value of a hidden input, tolerating either attribute order."""
    escaped = re.escape(name)
    match = re.search(rf'name="{escaped}"[^>]*?value="([^"]*)"', body)
    if match is None:
        match = re.search(rf'value="([^"]*)"[^>]*?name="{escaped}"', body)
    return html.unescape(match.group(1)) if match else ""


def form_state(body: str) -> dict[str, str]:
    """The three hidden fields a postback must echo back.

    ``__VIEWSTATE`` is mandatory; without it the server answers 200 with the
    bare form again, which would read as "no jobs matched".
    """
    state = {name: _hidden(body, name) for name in _STATE_FIELDS}
    if not state["__VIEWSTATE"]:
        raise GoJobsProtocolError("Search.aspx returned no __VIEWSTATE")
    return state


def build_form(
    body: str,
    *,
    regions: list[str] | None = None,
    cities: list[str] | None = None,
    categories: list[str] | None = None,
    career_levels: list[str] | None = None,
    min_salary: str = "",
    job_id: str = "",
    event_target: str = "",
    submit: bool = False,
) -> dict[str, str]:
    """Build a postback body against the state carried by ``body``.

    Facet lists are JSON-encoded because the control reads them with
    ``JSON.parse``; see the module docstring for what a bare string does.
    """

    def facet(values: list[str] | None) -> str:
        cleaned = [v for v in (values or []) if v]
        return json.dumps(cleaned) if cleaned else ""

    form = dict(form_state(body))
    form.update(
        {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
            "ctl00$MainContent$ucRegion$hiddenField": "True",
            "ctl00$MainContent$ucRegion$hiddenSelected": facet(regions),
            "ctl00$MainContent$ucCity$hiddenField": "False",
            "ctl00$MainContent$ucCity$hiddenSelected": facet(cities),
            "ctl00$MainContent$ucCategory$hiddenField": "False",
            "ctl00$MainContent$ucCategory$hiddenSelected": facet(categories),
            "ctl00$MainContent$ucCareers$hiddenField": "False",
            "ctl00$MainContent$ucCareers$hiddenSelected": facet(career_levels),
            "ctl00$MainContent$txtJobID": job_id,
        }
    )
    if min_salary:
        form["ctl00$MainContent$ddlMinSalary"] = min_salary
    if submit:
        form["ctl00$MainContent$btnSearch"] = "Search"
    return form


def page_event_target(page_index: int) -> str:
    """``__EVENTTARGET`` for a zero-indexed result page."""
    return f"ctl00$MainContent$lnkButton_Page{page_index}"


# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

_OPTIONS_RE = r"var options_%s = (\[.*?\]);"


def parse_options(body: str, facet: str) -> dict[str, str]:
    """``{code: label}`` for one multiselect, from the page's own bootstrap JS.

    The vocabularies ship inline as JSON, so no extra request is needed to learn
    what a facet accepts. The "all" entry has an empty value and is dropped.
    """
    match = re.search(_OPTIONS_RE % re.escape(facet), body, re.S)
    if not match:
        return {}
    try:
        entries = json.loads(match.group(1))
    except (ValueError, TypeError):
        return {}
    out: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = (entry.get("Value") or "").strip()
        text = " ".join((entry.get("Text") or "").split())
        if value and text:
            out[value] = text
    return out


_vocab_cache: dict[str, dict[str, str]] | None = None
_vocab_lock = asyncio.Lock()


async def get_vocabularies(session) -> dict[str, dict[str, str]]:
    """Facet vocabularies, fetched once per process.

    **``Search.aspx`` serves two different pages.** The first GET on a session
    returns ~499KB carrying ``var options_ucCity`` with all 984 cities. Every
    GET after that returns ~65KB with no city declaration at all — the regions,
    categories and career levels are still there, so the page looks complete
    and only the largest vocabulary is missing.

    That is not cosmetic. With no city vocabulary a place name cannot resolve to
    a city code, and ``_resolve_location`` falls through to the region match:
    "Toronto" quietly became REGION-TRNT and the board reported 35 where the
    city filter gives 32, while "Thunder Bay" matched no region at all and the
    board returned the unfiltered 127. The postings shown stayed correct — the
    client-side filter is the guarantee — but the board's own count was
    reported for a scope the caller never asked for.

    So the vocabulary is read once, from the cold page, and an empty city list
    is never cached: better to retry on the next call than to spend the rest of
    the process unable to resolve a city.
    """
    global _vocab_cache
    if _vocab_cache is None:
        async with _vocab_lock:
            if _vocab_cache is None:
                # This GET uses the same process-wide session and Search.aspx
                # conversation as result postbacks. It must not interleave with
                # `_search_all_locked`, even though it only reads vocabulary:
                # the server keys form state to the shared PHP session.
                async with _exchange_lock:
                    parsed = vocabularies(await fetch_search_form(session))
                if parsed.get("city"):
                    _vocab_cache = parsed
                else:
                    logger.debug(
                        "Search.aspx returned no city vocabulary; not caching"
                    )
                    return parsed
    return _vocab_cache


def vocabularies(body: str) -> dict[str, dict[str, str]]:
    """Every facet vocabulary the search form carries."""
    return {
        "region": parse_options(body, "ucRegion"),
        "city": parse_options(body, "ucCity"),
        "category": parse_options(body, "ucCategory"),
        "career_level": parse_options(body, "ucCareers"),
    }


# --------------------------------------------------------------------------
# Result parsing
# --------------------------------------------------------------------------

# "Results 1 - 10 of 127" on a populated page and "Results 0 of 0" on
# a genuine empty one — the board's own figure for the executed query.
_COUNTS_RE = re.compile(
    r"Results\s+(?:(?P<start>\d+)\s*-\s*(?P<end>\d+)|(?P<empty>0))"
    r"\s+of\s+(?P<total>[\d,]+)",
    re.I,
)
# A genuine empty search says so in the result region. A bare Search.aspx form
# has neither this message nor a result-count line, despite carrying perfectly
# valid __VIEWSTATE, so WebForms state alone cannot prove the POST succeeded.
_NO_RESULTS_RE = re.compile(
    r"(?:no\s+matching\s+jobs|no\s+jobs\s+(?:matched|found)|"
    r"your\s+search\s+(?:returned|found)\s+no\s+jobs)",
    re.I,
)
# One row is anchored by its ENGLISH title link and runs to the next one.
#
# Splitting on the repeater id (``rptSearchResult_ctlNN``) instead looks
# equivalent and is not: a bilingual posting carries a second anchor,
# ``lnkJobTitleFR``, whose id contains the same repeater marker. That cut the
# row in half and left every field of the bilingual posting empty while the
# monolingual rows around it parsed perfectly — a per-row silent data loss.
_ROW_ANCHOR_RE = re.compile(
    r'<a[^>]*id="[^"]*_lnkJobTitleEN"[^>]*'
    r'href="(?P<href>[^"]*Preview\.aspx\?[^"]*JobID=(?P<id>\d+)[^"]*)"[^>]*>(?P<title>.{0,4000}?)</a>',
    re.S | re.I,
)

# Where the result list stops. Without this the final row has no following
# anchor to bound it, so its last field ran to the end of the document and
# swallowed the pagination strip, the footer and ~130KB of inline script.
#
# The paging control is listed first and is the one that actually bites: it
# sits between the last row and the footer, so anchoring only on the footer
# still left "…11:59 pm EDT 1 2 3 4 5 6 7 8 9 10 11 Next" as a closing date.
_ROWS_END_RE = re.compile(
    r"(?:lnkButton_Page"
    r'|<div[^>]*class="[^"]*footer'
    r"|Home\s+Accessibility\s+Privacy"
    r"|var\s+startTime)",
    re.I,
)
_WS_RE = re.compile(r"[\s\xa0]+")

_ROW_LABELS = {
    "Organization": "organization",
    "Salary": "salary",
    "Location": "location",
    "Closing Date": "closing_date",
}


def _text(fragment: str) -> str:
    """Tags out, entities decoded, whitespace collapsed."""
    # Result rows are deliberately bounded at a pagination control marker,
    # which can leave the opening pagination tag cut in half.  Discard that
    # one trailing partial tag before the linear markup scan; otherwise its
    # literal ``<a id=...`` becomes part of the closing-date field.
    last_close = fragment.rfind(">")
    partial = fragment.find("<", last_close + 1)
    if partial >= 0:
        fragment = fragment[:partial]
    return _WS_RE.sub(" ", html.unescape(strip_markup(fragment))).strip()


def board_total(body: str) -> int:
    """The board's own match count, or 0 when it reported none."""
    match = _COUNTS_RE.search(_text(body))
    return int(match.group("total").replace(",", "")) if match else 0


# Rows are numbered "1.", "2." … in the markup, and that counter sits *before*
# the next row's title link. Whatever bounds a row, its last field therefore
# ends up holding the following row's number — "…11:59 pm EDT 17." — so the
# counter is trimmed explicitly rather than hoped away by the boundary.
_TRAILING_ROW_COUNTER_RE = re.compile(r"\s+\d{1,3}\.\s*$")


def _row_fields(row_text: str) -> dict[str, str]:
    """Pull ``Label: value`` pairs, each running to the next known label."""
    labels = sorted(_ROW_LABELS, key=len, reverse=True)
    alternation = "|".join(re.escape(label) for label in labels)
    out: dict[str, str] = {}
    for match in re.finditer(rf"({alternation})\s*:\s*(.*?)(?=(?:{alternation})\s*:|$)", row_text):
        key = _ROW_LABELS.get(match.group(1))
        if key and key not in out:
            value = _TRAILING_ROW_COUNTER_RE.sub("", match.group(2).strip(" ·|"))
            out[key] = value.strip(" ·|")
    return out


def parse_results(body: str) -> list[dict]:
    """Postings on one result page, in the order the board listed them."""
    anchors = list(_ROW_ANCHOR_RE.finditer(body))
    if not anchors:
        return []

    end_match = _ROWS_END_RE.search(body, anchors[-1].end())
    list_end = end_match.start() if end_match else len(body)

    jobs: list[dict] = []
    seen: set[str] = set()
    for index, link in enumerate(anchors):
        job_id = link.group("id")
        if job_id in seen:
            continue
        title = _text(link.group("title"))
        if not title:
            continue
        seen.add(job_id)
        stop = anchors[index + 1].start() if index + 1 < len(anchors) else list_end
        record = {
            "job_id": job_id,
            "title": title,
            # Rebuilt from the fixed origin and the numeric id, never from the
            # captured href. Accepting any href starting with "http" let the
            # board's own markup steer a caller off-site, and preserved every
            # trailing query value on the way.
            "url": f"{PREVIEW_URL}?JobID={job_id}",
        }
        record.update(_row_fields(_text(body[link.end() : stop])))
        jobs.append(record)
    return jobs


def parse_search_response(body: str) -> tuple[list[dict], int]:
    """Postings and total from a recognized WebForms result response."""
    text = _text(body)
    count_match = _COUNTS_RE.search(text)
    jobs = parse_results(body)
    if count_match is None and not jobs and _NO_RESULTS_RE.search(text) is None:
        raise GoJobsUnrecognisedPageError(
            "Search.aspx postback returned neither results nor an empty-result marker"
        )
    total = int(count_match.group("total").replace(",", "")) if count_match else 0
    return jobs, total


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


async def _get(session, url: str) -> str:
    await gojobs_limiter.wait()
    try:
        resp = await session.get(url)
    except wafer.ChallengeDetected as exc:
        raise GoJobsBlockedError(str(exc)) from exc
    if resp.status_code >= 500:
        raise GoJobsUnavailableError(f"{url} returned {resp.status_code}")
    if resp.status_code >= 400:
        raise GoJobsBlockedError(f"{url} returned {resp.status_code}")
    return resp.text or ""


async def _post(session, url: str, form: dict[str, str]) -> str:
    await gojobs_limiter.wait()
    try:
        # wafer spells a urlencoded body `form=`; `data=` is silently dropped
        # and the postback would go out empty.
        resp = await session.post(url, form=form)
    except wafer.ChallengeDetected as exc:
        raise GoJobsBlockedError(str(exc)) from exc
    if resp.status_code >= 500:
        raise GoJobsUnavailableError(f"{url} returned {resp.status_code}")
    if resp.status_code >= 400:
        raise GoJobsBlockedError(f"{url} returned {resp.status_code}")
    return resp.text or ""


async def fetch_search_form(session) -> str:
    """The search page, which carries both the WebForms state and the facets."""
    return await _get(session, SEARCH_URL)


async def search_all(
    *,
    session,
    regions: list[str] | None = None,
    cities: list[str] | None = None,
    categories: list[str] | None = None,
    career_levels: list[str] | None = None,
    min_salary: str = "",
    max_records: int,
) -> tuple[list[dict], int, bool]:
    """Page through results up to ``max_records``.

    Returns ``(jobs, board_total, complete)``. ``complete`` is False when the
    board reported more matches than the window pulled, so the caller can say
    what it did not look at instead of implying it saw everything.

    Paging is sequential by necessity: each request is signed by the previous
    response's ``__VIEWSTATE``. The whole exchange runs under
    ``_exchange_lock`` — see there for what overlapping searches did.
    """
    async with _exchange_lock:
        return await _search_all_locked(
            session=session,
            regions=regions,
            cities=cities,
            categories=categories,
            career_levels=career_levels,
            min_salary=min_salary,
            max_records=max_records,
        )


async def _search_all_locked(
    *,
    session,
    regions: list[str] | None,
    cities: list[str] | None,
    categories: list[str] | None,
    career_levels: list[str] | None,
    min_salary: str,
    max_records: int,
) -> tuple[list[dict], int, bool]:
    body = await fetch_search_form(session)
    form = build_form(
        body,
        regions=regions,
        cities=cities,
        categories=categories,
        career_levels=career_levels,
        min_salary=min_salary,
        submit=True,
    )
    body = await _post(session, SEARCH_URL, form)

    jobs, total = parse_search_response(body)
    seen = {job["job_id"] for job in jobs}

    # A recognized empty-result marker is conclusive.  With ``total == 0``
    # the generic "count missing" fallback below would otherwise assume the
    # full examination ceiling was reachable and issue a pointless page-2
    # postback.
    if total == 0 and not jobs:
        return [], 0, True

    reachable = min(max_records, total) if total else max_records
    page = 1
    while len(jobs) < reachable:
        # Guard against a board that keeps answering but stops advancing.
        next_body = await _post(
            session,
            SEARCH_URL,
            build_form(body, event_target=page_event_target(page)),
        )
        parsed, _ = parse_search_response(next_body)
        page_jobs = [job for job in parsed if job["job_id"] not in seen]
        if not page_jobs:
            break
        seen.update(job["job_id"] for job in page_jobs)
        jobs.extend(page_jobs)
        body = next_body
        page += 1

    complete = not total or len(jobs) >= total
    return jobs[:max_records], total, complete


# --------------------------------------------------------------------------
# Job detail
# --------------------------------------------------------------------------

_DETAIL_LABELS = (
    "Job ID",
    "Posting status",
    "Organization",
    "Division",
    "City",
    "Position(s) language",
    "Job term",
    "Job code",
    "Salary",
    "Address",
    "Category",
    "Apply By",
)

# Labels that must terminate a value without being reported themselves. Without
# these, "Apply By" swallowed the whole competition block and "Category" ran on
# into "Posted on" — each field silently absorbing the next one's text.
_DETAIL_BOUNDARIES = _DETAIL_LABELS + (
    "Competition Status",
    "Posted on",
    "Note",
    "How to apply",
)

# The facts block ends, and the posting's own copy begins, at the first rule
# after the Job ID. Splitting there does two jobs at once: it stops the last
# fact (Salary, which no label follows) from running on into 600 characters of
# job description, and it locates the body without having to guess which tag
# the copy happens to start with — measured, one posting opens on <b> and
# another on a bare text node.
_RULE_RE = re.compile(r"<hr\s*/?>", re.I)


# Button and link text that sits inside the facts block with no label of its
# own, so it lands on whichever field was parsed last.
_CHROME_RE = re.compile(
    r"(?:\s|^)(?:Apply now|Accessibility support|Back to search results|Print)\b",
    re.I,
)


def _strip_chrome(value: str, *, title: str = "") -> str:
    """Drop unlabelled UI text and tidy the edges of a field value."""
    cleaned = _CHROME_RE.sub(" ", value)
    if title:
        cleaned = re.sub(rf"\s*{re.escape(title)}\s*$", " ", cleaned)
    return _WS_RE.sub(" ", cleaned).strip(" ·|-")


def split_job_page(body: str) -> tuple[str, str]:
    """``(facts_html, body_html)`` for a posting page."""
    anchor = body.find("Job ID")
    rule = _RULE_RE.search(body, anchor if anchor >= 0 else 0)
    if not rule:
        return body, ""
    return body[: rule.start()], body[rule.end() :]


def parse_job(body: str) -> dict | None:
    """Fields and body copy for one posting, or None if the page is not one.

    ``Preview.aspx`` answers 200 for an unknown id and renders the shell, so the
    title is what proves a posting was actually served.
    """
    facts_html, body_html = split_job_page(body)
    text = _text(facts_html)
    if "Job Preview" not in body and "Job ID" not in text:
        return None

    title = ""
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
    if heading:
        title = _text(heading.group(1))
    if not title:
        match = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
        title = _text(match.group(1)) if match else ""

    fields: dict[str, str] = {}
    wanted = "|".join(re.escape(label) for label in sorted(_DETAIL_LABELS, key=len, reverse=True))
    stops = "|".join(re.escape(label) for label in sorted(_DETAIL_BOUNDARIES, key=len, reverse=True))
    for match in re.finditer(rf"({wanted})\s*:\s*(.*?)(?=(?:{stops})\s*:|$)", text):
        key = match.group(1)
        if key not in fields:
            # The page repeats the job title as an unlabelled heading inside
            # the facts block, so it lands on whichever field precedes it.
            fields[key] = _strip_chrome(match.group(2), title=title)[:600]

    if not fields.get("Job ID"):
        return None

    # A filled competition still reports "Posting status: Open". Reporting only
    # that would present a closed job as live, so the competition block is
    # picked out separately and rendered first.
    status = ""
    # `_text` deliberately collapses all whitespace, so whitespace cannot bound
    # this value. Stop at the next labelled fact just like `_row_fields` does;
    # otherwise the promoted status absorbs the entire facts block.
    status_stops = "|".join(
        re.escape(label) for label in sorted(_DETAIL_BOUNDARIES, key=len, reverse=True)
        if label != "Competition Status"
    )
    match = re.search(
        rf"Competition Status\s*:\s*(.*?)(?=(?:{status_stops})\s*:|$)",
        text,
    )
    if match:
        status = match.group(1).strip()[:200]

    return {
        "job_id": fields.get("Job ID", ""),
        "title": title,
        "fields": fields,
        "competition_status": status,
        "url": f"{PREVIEW_URL}?JobID={fields.get('Job ID', '')}",
        "body_html": body_html,
    }


async def fetch_job(job_id: str, *, session) -> dict | None:
    """One posting by numeric id."""
    body = await _get(session, f"{PREVIEW_URL}?JobID={job_id}")
    job = parse_job(body)
    if job is None or job.get("job_id") != str(job_id):
        return None
    return job
