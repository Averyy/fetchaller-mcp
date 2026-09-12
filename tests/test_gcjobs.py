"""GC Jobs (emploisfp-psjobs.cfp-psc.gc.ca): protocol, parsing, and rendering.

Fixtures are trimmed from real responses captured on 2026-09-11. Each parsing
test corresponds to something the live board actually did.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from fetchaller import server
from fetchaller.gcjobs import api, render, search, url

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _row(poster, title, *, closing, org, location, language, salary, note="",
         session=""):
    note_html = f"<br /><br />{note}" if note else ""
    return f"""
      <li class="searchResult">
        <div><strong>
          <a href="/psrs-srfp/applicant/page1800{session}?poster={poster}">{title}</a>
        </strong></div>
        <div class="tableTable"><div class="tableRow">
          <div class="tableCell">
              Closing date: {closing}
            <br />
              {org}
            <br />
            {location}{note_html}
          </div>
          <div class="tableCell">
              {language}
              <br />{salary}
          </div>
        </div></div>
        <hr class="searchJobHrLine" />
      </li>
    """


def _page(rows, *, total, pages=1, crit_calls="", session=""):
    strip = ""
    if pages > 1:
        links = ", ".join(
            f'<a href="page2440{session}?requestedPage={n}&amp;fromPage=1&amp;tab=1&amp;log=false">{n}</a>'
            for n in range(2, pages + 1)
        )
        strip = (
            f'<p><span class="pagelinks">[First / Previous] Page <strong>1</strong>, {links} '
            f'of {pages} [<a href="page2440?requestedPage=next&amp;fromPage=1&amp;tab=1&amp;log=false">Next</a>]</span></p>'
        )
    return f"""
<html><body>
<form action="page2440" method="get" id="searchForm" name="searchForm">
<input type="hidden" name="tab" value="1" />
<input type="text" id="title" name="title" value="" />
</form>
<script>
  departmentsIdNameAbbreviation[0]={{}};
  departmentsIdNameAbbreviation[0][0]= 1135;
  departmentsIdNameAbbreviation[0][1]="Accessibility Standards Canada";
  departmentsIdNameAbbreviation[0][2]='CASDO';
  departmentsIdNameAbbreviation[1]={{}};
  departmentsIdNameAbbreviation[1][0]= 75;
  departmentsIdNameAbbreviation[1][1]="Transport Canada";
  departmentsIdNameAbbreviation[1][2]='TC';
  {crit_calls}
</script>
<div class="searchResults" id="searchResults">
  <h2 class="lessTopSpace">Search results</h2>
  <div id="criteriaSection" class="mrgn-bttm-sm"></div>
  <div class="searchResultTab"><ul class="searchResultTab">
    <li class="searchResultTabSelected"><a href="page2440?tab=1&tabKeepCriteria=1">Jobs open to the public ({total})</a></li>
  </ul></div>
  <ol class="list-unstyled">{"".join(rows)}</ol>
  {strip}
</div>
</body></html>
"""


ROW_A = _row("2459537", "Administrative and Operational Support", closing="2026-09-11",
             org="Royal Canadian Mounted Police",
             location="Calgary (Alberta), Edmonton (Alberta)",
             language="English essential", salary="$57,217 to $61,761")
ROW_B = _row("2448108", "Non-Insured Health Benefits Analyst", closing="2026-09-15",
             org="Indigenous Services Canada\n - Regional Delivery Sector - Saskatchewan",
             location="Regina (Saskatchewan)", language="English essential",
             salary="$62,533 to $67,699",
             note="⚠️Applicants are encouraged to apply ONLY if they are able to relocate.")
ROW_C = _row("2455748", "Bilingual Sr. Specialist, MU Servicing", closing="2026-09-13",
             org="Canada Mortgage and Housing Corporation", location="Various Locations",
             language="Bilingual - imperative", salary="$104,180 to $130,225")
ROW_D = _row("2383561", "Senior Marine Safety Inspector Inventory (Electrical)",
             closing="2026-12-11", org="Transport Canada - Marine Safety &#38; Security",
             location="Kingston (Ontario), St. Catharines (Ontario), Sarnia (Ontario)",
             language="English essential", salary="$112,823 to $131,504")
ROW_HOURLY = _row("2455700", "2027 Winter - Student Work Placement", closing="2026-09-13",
                  org="Bank of Canada", location="Various Locations",
                  language="English or French essential", salary="$22.84 to $30.94 per hour")

PAGE_ONE = _page([ROW_A, ROW_B, ROW_C], total=84, pages=5,
                 crit_calls='addTopSearchCritButton("wLocation","addedLocation"+"W" + "232",\n'
                            '  "St. Catharines (Ontario)", removeText+" "+"St. Catharines (Ontario)", "hidden");')
PAGE_TWO = _page([ROW_D, ROW_HOURLY], total=84, pages=5)
EMPTY_PAGE = _page([], total=0) + "<p>No jobs found.</p>"
PAST_THE_END = _page([], total=28, pages=2)

# Exactly what the first response of a fresh session looks like: no cookie
# yet, so every path carries ;jsessionid=.
FRESH_SESSION_PAGE = _page(
    [_row("2432896", "Intelligence Case Analyst", closing="2026-09-11",
          org="Financial Transactions and Reports Analysis Centre of Canada",
          location="Ottawa (Ontario)", language="Various language requirements and/or profiles",
          salary="$88,955 to $110,940 (Classification: FC-05)",
          session=";jsessionid=00130B0FB5545CB623F76B9FAD025C89")],
    total=28, pages=2, session=";jsessionid=00130B0FB5545CB623F76B9FAD025C89",
    crit_calls='addTopSearchCritButton("wLocation", "addedLocation"+"P"+ 4,\n'
               '  "Ontario", removeText+" "+"Ontario", "hidden");\n'
               'addTopSearchCritButton("jobTtile", "title", \'Job title: \'+jobTitle2,\n'
               '  removeText+" "+jobTitle2, "text");',
)

SHELL = """
<html><body>
<div class="bodyPart" id="bodyPart" aria-busy="true"><p>The page is being updated. Please wait...</p>
<p>JavaScript must be enabled therefore verify your browser settings if the page does not update.</p></div>
</body></html>
"""

LOST_CONNECTION = """
<html><body><h1>Connexion interrompue</h1><p>Veuillez entrer de nouveau dans le système.</p>
<h1>Lost Connection</h1><p>Please Login again</p></body></html>
"""

POSTING = """
<html><body>
<main property="mainContentOfPage" class="container">
<h1><span class="no-break-word">Intelligence Case Analyst</span></h1>
<h2 class="pst-h2">Financial Transactions and Reports Analysis Centre of Canada</h2>
<h3 class="pst-h3 text-success"><span class="glyphicon glyphicon-time"></span> Closing date:
  September 11, 2099 - 23:59, Pacific Time</h3>
<div class="templates-container"><div class="left-box">
<a href="/psrs-srfp/applicant/page1710?careerChoiceId=2432896&amp;psrsMode=1" class="btn btn-primary btn-lg">Apply</a>
<div class="well well-sub-section">
  <div class="bottomSpace"><b>Reference number</b><br> CFC26J-094095-000007 </div>
  <div class="bottomSpace"><b>Selection process number</b><br> CFC-INTEL-2026-02 </div>
  <div class="bottomSpace"><b>Location</b><br> Ottawa&nbsp;(Ontario) </div>
  <div class="bottomSpace"><b>Salary</b><br> $88,955 to $110,940 -&nbsp;Classification: FC-05 </div>
  <div><b>Who can apply</b><br> Persons residing within a 125 km radius of the National Capital Region </div>
  <div><section class="mrgn-tp-md"><b>Organization information</b><br> For further information, visit
    <a href="https://fintrac-canafe.canada.ca/intro-eng">FINTRAC</a>.</section></div>
</div></div>
<div class="right-box">
  <div id="backToTop"> </div>
  <h2 class="h2NoSpace">On this page</h2>
  <ul class="list-unstyled"><li><a href="#aboutPosition">About the position</a></li></ul>
  <div id="aboutPosition"><hr class="hr-empty-line"><h2>About the position</h2>
    <div class="bottomSpace"><b>Duties</b><br>Analyse things.<div class="spacer"></div>Report on them.</div>
    <div class="bottomSpace"><a href="#backToTop" class="wb-back-to-top back-to-top-btn" title="Return to top">
      <span class="glyphicon glyphicon-circle-arrow-up"></span></a><hr class="hr-empty-line"></div>
  </div>
  <div id="youNeed"><h2>You need (essential for the job)</h2>
    <a id="somcAnchor5969561" ></a><div id="somcID5969561" class="bottomSpace">EDUCATION: a degree.</div>
  </div>
  <div class="bottom-space-before-apply"></div>
  <a href="/psrs-srfp/applicant/page1710?careerChoiceId=2432896&amp;psrsMode=1" class="btn btn-primary btn-lg">Apply</a>
</div></div>
<div data-gc-analytics-pageid="CFC26J-094095-000007"></div>
<dl id="wb-dtmd"><dt>Date modified:</dt><dd>2026-08-18</dd></dl>
</main></body></html>
"""

EXTERNAL = """
<html><body><main property="mainContentOfPage" class="container">
<h1>You will leave the <abbr title='Government of Canada'>GC</abbr> Jobs Web site</h1>
<p>The job opportunity you have selected requires the Public Service Commission (PSC) to transfer you
to the hiring organization's Web site.</p>
<p><a href="https://www.canada.ca/en/security-intelligence-service/corporate/csis-jobs/multimedia-en.html">Head of Multimedia (L08), Multimedia Producer (L07)</a></p>
<div data-gc-analytics-pageid="CSI26D-018356-002018"></div>
</main></body></html>
"""

LEGACY = """
<html><body><main property="mainContentOfPage" class="container">
<table>
  <tr><td>Fisheries and Oceans Canada </td><td>DFO1-COA1</td></tr>
  <tr><td colspan="2"><p>RESOURCE MANAGEMENT FISHERIES ADVISOR</p></td></tr>
  <tr><td colspan="2"><p><strong>Positions:</strong> 3<br />
    <strong>Location:</strong> Dartmouth, Nova Scotia<br />
    <strong>Salary:</strong> $37,999 to $50,555 (under revision)<br />
    <strong>Deadline:</strong> October 12, 2001 - 18:00 Pacific time (6:00 p.m.)</p></td></tr>
  <tr><td colspan="2"><strong>OUR REQUIREMENTS:</strong><br /><ul><li>A degree.</li></ul></td></tr>
  <tr><td colspan="2"><strong>DOCUMENTS TO BE SUBMITTED</strong><br />Your résumé.</td></tr>
</table>
<dl id="wb-dtmd"><dt>Date modified:</dt><dd>2026-08-18</dd></dl>
</main></body></html>
"""


@pytest.fixture
def no_rate_limit(monkeypatch):
    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(api.gcjobs_limiter, "wait", instant)


@pytest.fixture
def fresh_locks(monkeypatch):
    monkeypatch.setattr(api, "_exchange_lock", asyncio.Lock())
    monkeypatch.setattr(api, "_department_lock", asyncio.Lock())
    monkeypatch.setattr(api, "_department_cache", None)


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------


class TestSearchUrls:
    def test_a_search_carries_both_flags_and_the_forms_own_names(self):
        params = api.search_params(
            title_term="anal",
            locations=["W232", "P4"],
            departments=["75"],
            salary_bands=[7, 8],
            language="1",
            exclude_various=True,
        )
        assert ("isSecondPartOfPage", "1") in params
        assert ("isInitialNetworkCheck", "1") in params
        assert ("title", "anal") in params
        assert params.count(("addedLocation", "W232")) == 1
        assert ("addedLocation", "P4") in params
        assert ("department", "75") in params
        assert ("jobSalaryRange", "7") in params and ("jobSalaryRange", "8") in params
        assert ("officialLanguage", "1") in params
        assert ("variousLocation", "variousLocation") in params
        # The submit button's name is what makes the query a search.
        assert ("search", "Search jobs") in params
        assert "wLocation" not in dict(params)

    def test_a_page_request_omits_the_init_flag(self):
        # With it, the board resets the stored search to the whole board and
        # pages that: page 2 of a 13-page Ontario search came back as page 2
        # of the 21-page national listing.
        page = api.page_url(3)
        assert "requestedPage=3" in page and "fromPage=2" in page
        assert "isSecondPartOfPage=1" in page
        assert "isInitialNetworkCheck" not in page


class TestLostConnection:
    @pytest.mark.asyncio
    async def test_the_lost_connection_page_is_a_session_error_at_any_status(
        self, no_rate_limit
    ):
        # The board serves it as a 200 for a bare second-part request and as a
        # 500 for a search without the init flag; neither is an outage.
        for status in (200, 500):
            session = SimpleNamespace(
                get=lambda url, _s=status: _coro(SimpleNamespace(status_code=_s, text=LOST_CONNECTION))
            )
            with pytest.raises(api.GCJobsSessionError):
                await api._get(session, "https://example/")

    def test_the_shell_is_not_an_empty_search(self):
        with pytest.raises(api.GCJobsUnrecognisedPageError):
            api.parse_search_response(SHELL)


def _coro(value):
    async def inner():
        return value

    return inner()


# --------------------------------------------------------------------------
# Result parsing
# --------------------------------------------------------------------------


class TestParseResults:
    def test_fields_are_read_positionally_from_the_two_cells(self):
        jobs = api.parse_results(PAGE_ONE)
        assert [j["poster_id"] for j in jobs] == ["2459537", "2448108", "2455748"]
        first = jobs[0]
        assert first["closing_date"] == "2026-09-11"
        assert first["organization"] == "Royal Canadian Mounted Police"
        assert first["location"] == "Calgary (Alberta), Edmonton (Alberta)"
        assert first["language"] == "English essential"
        assert first["salary"] == "$57,217 to $61,761"
        assert first["url"] == f"{api.POSTING_URL}?poster=2459537"

    def test_a_branch_continuation_line_stays_with_its_organization(self):
        row = api.parse_results(PAGE_ONE)[1]
        assert row["organization"] == (
            "Indigenous Services Canada - Regional Delivery Sector - Saskatchewan"
        )
        assert row["location"] == "Regina (Saskatchewan)"

    def test_a_note_after_the_location_is_kept_as_a_note_not_a_location(self):
        row = api.parse_results(PAGE_ONE)[1]
        assert row["note"].startswith("⚠️Applicants are encouraged")
        assert "Applicants" not in row["location"]

    def test_the_first_response_of_a_fresh_session_still_parses(self):
        # Regression: before a cookie exists the board rewrites every href
        # with ;jsessionid=, and anchoring on "page1800?" found zero rows on
        # the first search a process ever ran.
        jobs, total, pages = api.parse_search_response(FRESH_SESSION_PAGE)
        assert [j["poster_id"] for j in jobs] == ["2432896"]
        assert total == 28 and pages == 2
        assert "jsessionid" not in jobs[0]["url"]

    def test_count_and_page_count(self):
        jobs, total, pages = api.parse_search_response(PAGE_ONE)
        assert (len(jobs), total, pages) == (3, 84, 5)
        _jobs, total, pages = api.parse_search_response(PAGE_TWO)
        assert (total, pages) == (84, 5)

    def test_a_genuine_empty_search_is_recognised(self):
        assert api.parse_search_response(EMPTY_PAGE) == ([], 0, 1)

    def test_a_zero_count_without_the_marker_is_not_trusted(self):
        with pytest.raises(api.GCJobsUnrecognisedPageError):
            api.parse_search_response(_page([], total=0))

    def test_a_page_past_the_end_is_empty_but_recognised(self):
        jobs, total, pages = api.parse_search_response(PAST_THE_END)
        assert jobs == [] and total == 28 and pages == 2

    def test_a_positive_count_with_no_rows_on_a_single_page_is_an_error(self):
        with pytest.raises(api.GCJobsUnrecognisedPageError):
            api.parse_search_response(_page([], total=5))

    def test_the_lost_connection_page_is_not_a_result_page(self):
        with pytest.raises(api.GCJobsUnrecognisedPageError):
            api.parse_search_response(LOST_CONNECTION)


class TestAppliedCriteria:
    def test_a_city_is_echoed_with_a_quoted_id_and_a_province_bare(self):
        # Two spellings of the same echo; both must resolve to the code sent.
        assert api.applied_criteria(PAGE_ONE)["wLocation"] == ["W232"]
        assert api.applied_criteria(FRESH_SESSION_PAGE)["wLocation"] == ["P4"]

    def test_title_salary_organization_and_language_echoes(self):
        body = _page([], total=0, crit_calls=(
            'addTopSearchCritButton("salary","jobSalaryRange7", \'$90,000 - $99,999\', removeText, "checkbox");'
            'addTopSearchCritButton("salary","jobSalaryRange8", \'$100,000 +\', removeText, "checkbox");'
            'addTopSearchCritButton("organization","department" + departmentsIdNameAbbreviation[departmentIndex][0], x, y, "hidden");'
            'addTopSearchCritButton("lang","officialLanguage", \'English\', removeText, "select");'
        ))
        applied = api.applied_criteria(body)
        assert applied["salary"] == ["7", "8"]
        assert "organization" in applied and "lang" in applied
        assert "wLocation" not in applied

    def test_an_unfiltered_page_echoes_nothing(self):
        assert api.applied_criteria(PAGE_TWO) == {}


class TestDepartments:
    def test_parses_id_name_and_abbreviation(self):
        vocab = api.parse_departments(PAGE_ONE)
        assert vocab["75"] == ("Transport Canada", "TC")
        assert vocab["1135"] == ("Accessibility Standards Canada", "CASDO")

    def test_the_shell_carries_no_vocabulary(self):
        assert api.parse_departments(SHELL) == {}


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------


class RecordingSession:
    def __init__(self, pages: dict[int, str], first: str):
        self.pages = pages
        self.first = first
        self.urls: list[str] = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        await asyncio.sleep(0)
        if "isInitialNetworkCheck=1" in url:
            return SimpleNamespace(status_code=200, text=self.first)
        for number, body in self.pages.items():
            if f"requestedPage={number}&" in url:
                return SimpleNamespace(status_code=200, text=body)
        return SimpleNamespace(status_code=200, text=PAST_THE_END)


class TestSearchAll:
    @pytest.mark.asyncio
    async def test_pages_follow_the_strip_and_stop_at_its_count(
        self, no_rate_limit, fresh_locks
    ):
        session = RecordingSession({2: PAGE_TWO}, first=PAGE_ONE)
        jobs, total, complete, applied = await api.search_all(
            session=session, params=api.search_params(locations=["W232"]), max_records=200
        )
        assert [j["poster_id"] for j in jobs] == [
            "2459537", "2448108", "2455748", "2383561", "2455700"
        ]
        # The walk ended because page 3 served nothing new, so it is
        # complete: nothing a narrower query would recover.
        assert total == 84 and complete is True
        assert applied["wLocation"] == ["W232"]
        # Only the first request carries the init flag; a page that yields
        # nothing new ends the walk even though the strip promised five.
        assert sum("isInitialNetworkCheck=1" in u for u in session.urls) == 1
        assert "requestedPage=2&fromPage=1" in session.urls[1]
        assert "requestedPage=3&fromPage=2" in session.urls[2]
        assert len(session.urls) == 3

    @pytest.mark.asyncio
    async def test_the_ceiling_stops_paging_once_reached(self, no_rate_limit, fresh_locks):
        session = RecordingSession({2: PAGE_TWO}, first=PAGE_ONE)
        jobs, _total, complete, _applied = await api.search_all(
            session=session, params=api.search_params(), max_records=2
        )
        assert len(jobs) == 2
        assert len(session.urls) == 1
        # This one *was* cut by the ceiling with pages unread.
        assert complete is False

    @pytest.mark.asyncio
    async def test_concurrent_searches_do_not_interleave(self, no_rate_limit, fresh_locks):
        # Paging reads the search the session stores, so an overlapping
        # search would page the other's results — and get a well-formed page.
        events: list[str] = []

        class Tagged(RecordingSession):
            def __init__(self, tag):
                super().__init__({2: PAGE_TWO}, first=PAGE_ONE)
                self.tag = tag

            async def get(self, url, **kwargs):
                events.append(self.tag)
                return await super().get(url, **kwargs)

        await asyncio.gather(
            api.search_all(session=Tagged("A"), params=api.search_params(), max_records=200),
            api.search_all(session=Tagged("B"), params=api.search_params(), max_records=200),
        )
        first = events[0]
        boundary = next(i for i, e in enumerate(events) if e != first)
        assert all(e == first for e in events[:boundary])
        assert all(e != first for e in events[boundary:])


# --------------------------------------------------------------------------
# Job detail
# --------------------------------------------------------------------------


class TestParseJob:
    def test_a_posting_yields_its_facts_and_eligibility(self):
        job = api.parse_job(POSTING, "2432896")
        assert job["kind"] == "posting"
        assert job["title"] == "Intelligence Case Analyst"
        assert job["organization"] == "Financial Transactions and Reports Analysis Centre of Canada"
        assert job["closing_date"] == date(2099, 9, 11)
        assert job["closed"] is False
        assert job["facts"]["Location"] == "Ottawa (Ontario)"
        assert job["facts"]["Who can apply"].startswith("Persons residing within a 125 km")
        assert job["facts"]["Reference number"] == "CFC26J-094095-000007"
        assert "Organization information" not in job["facts"]

    def test_classification_is_split_out_of_the_salary(self):
        job = api.parse_job(POSTING, "2432896")
        assert job["facts"]["Salary"] == "$88,955 to $110,940"
        assert job["classification"] == "FC-05"

    def test_the_body_keeps_sections_and_drops_page_furniture(self):
        body = render.job_body_markdown(api.parse_job(POSTING, "1")["body_html"])
        assert body.startswith("## About the position")
        assert "Analyse things." in body and "EDUCATION: a degree." in body
        assert "On this page" not in body
        assert "Return to top" not in body
        assert "Apply" not in body
        assert "somcAnchor" not in body
        assert "Date modified" not in body

    def test_an_interstitial_is_external_with_its_outbound_link(self):
        # Eleven of twenty postings enumerated on 2026-09-11 were this page;
        # parsed as a posting it yields a row with every field empty.
        job = api.parse_job(EXTERNAL, "2457298")
        assert job["kind"] == "external"
        assert job["title"] == "Head of Multimedia (L08), Multimedia Producer (L07)"
        assert job["external_url"].startswith("https://www.canada.ca/")

    def test_a_2001_posting_is_read_and_marked_closed(self):
        # poster=1 is a Fisheries and Oceans ad from 2001, served as a normal
        # live page.
        job = api.parse_job(LEGACY, "1")
        assert job["kind"] == "legacy"
        assert job["title"] == "RESOURCE MANAGEMENT FISHERIES ADVISOR"
        assert job["organization"] == "Fisheries and Oceans Canada"
        assert job["closing_date"] == date(2001, 10, 12)
        assert job["closed"] is True
        assert job["facts"] == {
            "Positions": "3",
            "Location": "Dartmouth, Nova Scotia",
            "Salary": "$37,999 to $50,555 (under revision)",
        }

    def test_a_past_closing_date_marks_a_modern_posting_closed(self):
        past = POSTING.replace("September 11, 2099", "September 11, 2020")
        assert api.parse_job(past, "1")["closed"] is True

    def test_a_page_that_is_none_of_the_three_shapes_is_none(self):
        assert api.parse_job(SHELL, "1") is None
        assert api.parse_job(LOST_CONNECTION, "1") is None

    def test_closing_dates_in_both_spellings(self):
        assert api.parse_closing_date("September 11, 2026 - 23:59, Pacific Time") == date(2026, 9, 11)
        assert api.parse_closing_date("2026-09-11") == date(2026, 9, 11)
        assert api.parse_closing_date("no date here") is None


class TestRenderJob:
    def test_eligibility_comes_first_and_is_flagged_when_restricted(self):
        md = render.render_job(api.parse_job(POSTING, "2432896"))
        lines = md.splitlines()
        assert lines[0] == "# Intelligence Case Analyst"
        assert lines[2].startswith("> **Who can apply:** ⚠ Persons residing within")
        assert "- **Classification:** FC-05" in md
        assert "Closed" not in md

    def test_a_closed_posting_says_so_before_anything_else(self):
        md = render.render_job(api.parse_job(LEGACY, "1"))
        assert md.splitlines()[2].startswith("> **Closed.** This posting's closing date was October 12, 2001")

    def test_an_external_posting_gives_the_outbound_url_not_empty_fields(self):
        md = render.render_external(api.parse_job(EXTERNAL, "2457298"))
        assert "Hosted outside GC Jobs" in md
        assert "https://www.canada.ca/en/security-intelligence-service" in md
        assert "Closing date" not in md


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------


class TestBoardTitleTerm:
    def test_a_multi_word_title_is_never_sent_as_typed(self):
        # The board matches a substring in the given order: "policy analyst"
        # returned 3 and "analyst policy" returned 0.
        assert search.board_title_term("policy analyst") == "anal"

    @pytest.mark.parametrize(
        ("query", "titles"),
        [
            ("engineering", ["Engine Mechanic", "Engineer", "Engineering Technologist"]),
            ("designer", ["Design Lead", "Designers", "Senior Designer"]),
            ("nurses", ["Nurse Practitioner", "Senior Nurse"]),
            ("policy analyst", ["Junior Policy Analyst EC-02", "Analyst, Policy"]),
        ],
    )
    def test_the_term_is_contained_in_every_title_the_client_filter_accepts(
        self, query, titles
    ):
        # The board filter may only ever be an optimisation over the pool the
        # client filter then checks, so nothing the client would keep may be
        # excluded server-side.
        from fetchaller.jobfilter import title_matches, tokens

        term = search.board_title_term(query)
        for title in titles:
            assert title_matches(title, tokens(query)), title
            assert term in title.casefold(), (term, title)

    def test_a_short_word_is_sent_whole(self):
        assert search.board_title_term("IT") == "it"
        assert search.board_title_term("") == ""


class TestSalary:
    def test_bands_are_those_whose_top_reaches_the_minimum(self):
        assert search.salary_bands_for(70_000) == [5, 6, 7, 8]
        assert search.salary_bands_for(75_000) == [5, 6, 7, 8]
        assert search.salary_bands_for(100_000) == [8]
        assert search.salary_bands_for(1) == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_the_floor_of_an_annual_range_and_none_for_hourly(self):
        assert search.annual_floor("$88,955 to $110,940 (Classification: FC-05)") == 88_955
        assert search.annual_floor("$22.84 to $30.94 per hour") is None
        assert search.annual_floor("") is None


class TestLocationResolution:
    @pytest.mark.asyncio
    async def test_exact_name_only_and_province_scoping(self, monkeypatch):
        async def fake_lookup(_session, text):
            return [
                {"LOCATION_CD": "W", "LOCATION_DESC": "Toronto (Ontario)", "LOCATION_ID": 244},
                {"LOCATION_CD": "W", "LOCATION_DESC": "Toronto Pearson International Airport (Ontario)", "LOCATION_ID": 1116},
                {"LOCATION_CD": "W", "LOCATION_DESC": "Springfield (Ontario)", "LOCATION_ID": 900},
                {"LOCATION_CD": "W", "LOCATION_DESC": "Springfield (Manitoba)", "LOCATION_ID": 901},
                {"LOCATION_CD": "W", "LOCATION_DESC": "St. Catharines (Ontario)", "LOCATION_ID": 232},
                {"LOCATION_CD": "P", "LOCATION_DESC": "Ontario", "LOCATION_ID": 4},
            ]

        monkeypatch.setattr(api, "lookup_locations", fake_lookup)
        # The autocomplete is a substring search; the airport must not ride along.
        assert (await search._resolve_location(None, "Toronto"))[0] == ["W244"]
        # A punctuation difference is not a different place.
        assert (await search._resolve_location(None, "st catharines"))[0] == ["W232"]
        # Ambiguous without a province: both are sent, a superset the client re-checks.
        assert (await search._resolve_location(None, "Springfield"))[0] == ["W900", "W901"]
        assert (await search._resolve_location(None, "Springfield, MB"))[0] == ["W901"]
        # A province that contradicts the name is a contradiction, not a hint.
        assert (await search._resolve_location(None, "Springfield, BC"))[0] == []
        assert (await search._resolve_location(None, "Ontario"))[0] == ["P4"]
        assert (await search._resolve_location(None, "Niagara"))[0] == []

    @pytest.mark.asyncio
    async def test_canada_is_the_whole_board(self):
        assert await search._resolve_location(None, "Canada") == ([], [], True)


class TestOrganizationResolution:
    VOCAB = {"75": ("Transport Canada", "TC"), "1135": ("Accessibility Standards Canada", "CASDO")}

    def test_name_abbreviation_and_id_resolve_exactly(self):
        assert search._resolve_organization("transport canada", self.VOCAB) == (["75"], "Transport Canada")
        assert search._resolve_organization("TC", self.VOCAB) == (["75"], "Transport Canada")
        assert search._resolve_organization("75", self.VOCAB) == (["75"], "Transport Canada")

    def test_a_partial_name_does_not_resolve(self):
        # A loose match would filter to an organization the caller never named.
        assert search._resolve_organization("Transport", self.VOCAB) == ([], "")


# --------------------------------------------------------------------------
# Search flow
# --------------------------------------------------------------------------


@pytest.fixture
def stubbed_board(monkeypatch):
    """Drive search_gcjobs with canned board answers; captures what was sent."""
    sent: dict = {}

    async def fake_session(_browser_solver=None):
        return object()

    async def fake_lookup(_session, text):
        if "catharines" in text.casefold():
            return [{"LOCATION_CD": "W", "LOCATION_DESC": "St. Catharines (Ontario)", "LOCATION_ID": 232}]
        return []

    async def fake_departments(_session):
        return {"75": ("Transport Canada", "TC")}

    def install(*, jobs, total=None, applied=None, complete=True):
        async def fake_search_all(*, session, params, max_records):
            sent["params"] = params
            sent["max_records"] = max_records
            return list(jobs), total if total is not None else len(jobs), complete, dict(applied or {})

        monkeypatch.setattr(api, "search_all", fake_search_all)
        return sent

    monkeypatch.setattr(api, "_get_session", fake_session)
    monkeypatch.setattr(api, "lookup_locations", fake_lookup)
    monkeypatch.setattr(api, "get_departments", fake_departments)
    return install


ROWS = api.parse_results(PAGE_ONE) + api.parse_results(PAGE_TWO)


class TestSearchFlow:
    @pytest.mark.asyncio
    async def test_an_echoed_location_scopes_the_boards_count(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=84, applied={"wLocation": ["W232"]}, complete=False)
        result = await search.search_gcjobs(location="St. Catharines, ON")
        content = result["content"]
        assert ("addedLocation", "W232") in sent["params"]
        assert "in St. Catharines \\(Ontario\\)" in content
        # Various Locations rows are kept and counted, not silently included.
        assert "2 of the matches list “Various Locations”" in content
        assert "Senior Marine Safety Inspector" in content
        assert "Administrative and Operational Support" not in content
        assert "reported more postings than this window pulled" in content

    @pytest.mark.asyncio
    async def test_a_location_the_board_did_not_echo_is_reported_unscoped(self, stubbed_board):
        # The failure mode to design against: a parameter accepted and
        # ignored looks exactly like one that matched everything.
        stubbed_board(jobs=ROWS, total=414, applied={})
        content = (await search.search_gcjobs(location="St. Catharines, ON"))["content"]
        assert "did not echo the location filter back" in content
        assert "in St. Catharines" not in content.split("\n")[2]
        assert "dropped 2 by location" in content

    @pytest.mark.asyncio
    async def test_exclude_various_locations_drops_them_and_is_sent(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=4, applied={"wLocation": ["W232", ""]})
        content = (
            await search.search_gcjobs(location="St. Catharines, ON", exclude_various_locations=True)
        )["content"]
        assert ("variousLocation", "variousLocation") in sent["params"]
        assert "Various Locations" not in content.split("### ")[1:][0]
        assert "excluding" in content

    @pytest.mark.asyncio
    async def test_an_unknown_place_sends_no_board_filter_and_filters_here(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=414)
        content = (await search.search_gcjobs(location="Niagara"))["content"]
        assert not any(k == "addedLocation" for k, _ in sent["params"])
        assert "is not a place GC Jobs knows" in content
        # Nothing names Niagara; the two "Various Locations" rows survive
        # with a note that says why, and the named-elsewhere rows are dropped.
        assert "2 jobs shown; dropped 3 by location" in content
        assert "a posting open in various places may include it" in content
        assert "Regina" not in content.split("### ", 1)[1]

    @pytest.mark.asyncio
    async def test_title_is_sent_as_a_prefix_and_rechecked_here(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=32, applied={"jobTtile": [""]})
        content = (await search.search_gcjobs(title="benefits analyst"))["content"]
        assert ("title", "bene") in sent["params"]
        assert "Non-Insured Health Benefits Analyst" in content
        assert "dropped 4 by title" in content

    @pytest.mark.asyncio
    async def test_min_salary_selects_bands_and_rechecks_the_floor(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=5, applied={"salary": ["5", "6", "7", "8"]})
        content = (await search.search_gcjobs(min_salary=75_000))["content"]
        assert [v for k, v in sent["params"] if k == "jobSalaryRange"] == ["5", "6", "7", "8"]
        # $57,217 and $62,533 floors are dropped; the hourly student row is
        # not annual and is left as the board classed it.
        assert "dropped 2 by salary" in content
        assert "Student Work Placement" in content
        assert "paying from $75,000" in content

    @pytest.mark.asyncio
    async def test_an_unresolved_organization_is_matched_on_the_row(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=414)
        content = (await search.search_gcjobs(organization="Marine Safety"))["content"]
        assert not any(k == "department" for k, _ in sent["params"])
        assert "did not match an organization name" in content
        assert "Senior Marine Safety Inspector" in content
        assert "1 job shown" in content

    @pytest.mark.asyncio
    async def test_a_resolved_organization_is_sent_by_id(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS[3:4], total=1, applied={"organization": [""]})
        content = (await search.search_gcjobs(organization="TC"))["content"]
        assert ("department", "75") in sent["params"]
        assert content.startswith("# GC Jobs (federal public service): organization: Transport Canada")

    @pytest.mark.asyncio
    async def test_language_is_a_board_facet(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=302, applied={"lang": [""]})
        content = (await search.search_gcjobs(language="English"))["content"]
        assert ("officialLanguage", "1") in sent["params"]
        assert "english language requirement" in content
        assert "not one of the board's language requirements" not in content

    @pytest.mark.asyncio
    async def test_limit_sizes_the_output_not_the_pool(self, stubbed_board):
        sent = stubbed_board(jobs=ROWS, total=5)
        content = (await search.search_gcjobs(title="", location="", language="english", limit=2))["content"]
        assert sent["max_records"] == search._EXAMINE_CEILING
        assert "2 jobs shown of 5 matched" in content

    @pytest.mark.asyncio
    async def test_the_eligibility_caveat_is_always_printed_with_results(self, stubbed_board):
        stubbed_board(jobs=ROWS, total=5)
        content = (await search.search_gcjobs(language="english"))["content"]
        assert "Who can apply" in content
        assert "soonest closing first" in content

    @pytest.mark.asyncio
    async def test_session_and_unrecognised_errors_are_not_empty_searches(
        self, stubbed_board, monkeypatch
    ):
        for exc, phrase in (
            (api.GCJobsSessionError("x"), "Lost Connection"),
            (api.GCJobsUnrecognisedPageError("x"), "answered 200"),
        ):
            async def boom(_exc=exc, **_kwargs):
                raise _exc

            monkeypatch.setattr(api, "search_all", boom)
            result = await search.search_gcjobs(title="nurse")
            assert phrase in result["error"]
            assert "No empty-search conclusion" in result["error"]


class TestExamineCeiling:
    def test_is_a_constant_not_a_multiple_of_limit(self):
        assert isinstance(search._EXAMINE_CEILING, int)
        assert search._EXAMINE_CEILING >= 100


class TestGetJob:
    @pytest.mark.asyncio
    async def test_non_numeric_id_is_rejected_before_any_request(self):
        result = await search.get_gcjobs_job("not-a-number")
        assert "numeric poster ID" in result["error"]

    @pytest.mark.asyncio
    async def test_each_page_shape_renders(self, monkeypatch):
        async def fake_session(_browser_solver=None):
            return object()

        monkeypatch.setattr(api, "_get_session", fake_session)
        for body, phrase in ((POSTING, "Who can apply"), (EXTERNAL, "Hosted outside GC Jobs"), (LEGACY, "**Closed.**")):
            async def fake_fetch(poster_id, *, session, _body=body):
                return api.parse_job(_body, poster_id)

            monkeypatch.setattr(api, "fetch_job", fake_fetch)
            assert phrase in (await search.get_gcjobs_job("2432896"))["content"]

    @pytest.mark.asyncio
    async def test_an_unrecognised_page_is_not_found(self, monkeypatch):
        async def fake_session(_browser_solver=None):
            return object()

        async def fake_fetch(poster_id, *, session):
            return None

        monkeypatch.setattr(api, "_get_session", fake_session)
        monkeypatch.setattr(api, "fetch_job", fake_fetch)
        assert "was not found" in (await search.get_gcjobs_job("999999999"))["error"]


# --------------------------------------------------------------------------
# URL routing and the tool boundary
# --------------------------------------------------------------------------


class TestUrls:
    def test_poster_ids_survive_a_session_path_parameter(self):
        assert url.extract_gcjobs_poster(
            "https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/page1800;jsessionid=ABC?poster=2432896"
        ) == "2432896"
        assert url.extract_gcjobs_poster(
            "https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/page1800?toggleLanguage=en"
        ) == ""
        assert url.extract_gcjobs_poster("https://example.com/page1800?poster=1") == ""

    def test_search_url_and_its_title(self):
        board = "https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/page2440?tab=1&title=policy+analyst"
        assert url.is_gcjobs_search_url(board)
        assert url.gcjobs_search_criteria(board) == {"title": "policy analyst"}
        assert not url.is_gcjobs_search_url("https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/page1800?poster=1")


class TestToolSurface:
    def test_search_requires_at_least_one_filter(self):
        error = server._validate_tool_arguments("search_gcjobs", {})
        assert error and "at least one of" in error
        assert server._validate_tool_arguments("search_gcjobs", {"title": "", "location": ""})

    def test_each_filter_has_its_published_type(self):
        ok = server._validate_tool_arguments
        assert ok("search_gcjobs", {"min_salary": 70000}) is None
        assert ok("search_gcjobs", {"min_salary": "70000"}) is not None
        assert ok("search_gcjobs", {"language": "english"}) is None
        assert ok("search_gcjobs", {"language": "German"}) is not None
        assert ok("search_gcjobs", {"organization": "CRA"}) is None
        assert ok("search_gcjobs", {"title": "x", "exclude_various_locations": "yes"}) is not None
        assert ok("search_gcjobs", {"title": "x", "exclude_various_locations": True}) is None

    def test_poster_ids_of_any_realistic_length_are_accepted(self):
        for job_id in ("1", "2432896"):
            assert server._validate_tool_arguments("get_gcjobs_job", {"job_id": job_id}) is None
        error = server._validate_tool_arguments("get_gcjobs_job", {"job_id": "abc"})
        assert "GC Jobs poster ID" in error


class TestRenderSearch:
    def test_header_counts_and_rows(self):
        md = render.render_search_results(
            ROWS[:2], title="analyst", location="Regina", board_total=2, examined=2, board_scope="in Regina"
        )
        assert md.startswith("# GC Jobs (federal public service): “analyst” · Regina")
        assert "_2 jobs shown_" in md
        assert "### [Administrative and Operational Support](https://emploisfp-psjobs.cfp-psc.gc.ca/psrs-srfp/applicant/page1800?poster=2459537)" in md
        assert "English essential · $57,217 to $61,761 · closes 2026-09-11" in md
        assert "_⚠️Applicants are encouraged" in md
        assert "Poster ID: 2448108" in md

    def test_no_results(self):
        md = render.render_search_results([], title="zzqxjv", board_total=0)
        assert md.endswith("No matching postings.")


class TestCompleteness:
    @pytest.mark.asyncio
    async def test_reaching_the_last_page_is_complete_even_when_the_board_overcounts(
        self, no_rate_limit, fresh_locks
    ):
        # Live: St. Catharines counted 84 and its five pages served 83
        # distinct rows. That is the board's arithmetic, not a window.
        session = RecordingSession({2: PAGE_TWO}, first=PAGE_ONE)
        _jobs, _total, complete, _applied = await api.search_all(
            session=session, params=api.search_params(), max_records=200
        )
        assert complete is True

    def test_an_exhausted_walk_does_not_tell_the_reader_to_narrow_the_query(self):
        from fetchaller.jobfilter import counts_line

        lines = counts_line(5, dropped_by_location=78, board_total=84, examined=83, exhausted=True)
        text = "\n".join(lines)
        assert "counted 84 for this query but its pages served 83 distinct postings" in text
        assert "not examined" not in text
        # Without the flag the same numbers still describe a real window cut.
        text = "\n".join(counts_line(5, dropped_by_location=78, board_total=84, examined=83))
        assert "The remaining 1 were not examined" in text


class TestNotFound:
    @pytest.mark.asyncio
    async def test_a_404_is_not_found_rather_than_declined(self, no_rate_limit):
        # Measured: poster=0 and poster=999999999 both answer a plain 404
        # page, unlike gojobs, which answers 200 with its shell.
        session = SimpleNamespace(
            get=lambda url: _coro(SimpleNamespace(status_code=404, text="File not found"))
        )
        assert await api.fetch_job("999999999", session=session) is None
