"""gojobs.gov.on.ca: WebForms protocol, result parsing, and rendering.

The fixtures here are trimmed from real gojobs responses. Each of the parsing
tests corresponds to a defect the live board actually produced — they are
regression tests, not illustrations.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from fetchaller import server
from fetchaller.gojobs import api, render, search

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

FORM_HTML = """
<html><body>
<form name="aspnetForm" method="post" action="./Search.aspx">
<input type="hidden" name="__VIEWSTATE" id="x" value="STATE-1" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="BBBC20B8" />
<input type="hidden" name="__EVENTVALIDATION" value="EV-1" />
<input type="hidden" name="ctl00$MainContent$ucRegion$hiddenField" value="True" />
<script>
var options_ucRegion = [{"Value":"","Text":"All regions"},{"Value":"REGION-TRNT","Text":"Toronto"},{"Value":"REGION-NRTH","Text":"North"}];
var options_ucCategory = [{"Value":"","Text":"All job categories"},{"Value":"JOBCATGY-IT","Text":"Information Technology"}];
var options_ucCareers = [{"Value":"","Text":"All career levels"},{"Value":"CAREERLEVEL-EXEC","Text":"Executive"}];
var options_ucCity = [{"Value":"","Text":"All cities"},{"Value":"CITY-TRNT","Text":"Toronto"},{"Value":"CITY-HMLTN","Text":"Hamilton"}];
</script>
</form></body></html>
"""


def _row(ctl: str, job_id: str, title: str, *, org: str, salary: str, location: str,
         closing: str, counter: str, french: str = "") -> str:
    fr = (
        f'<a id="ctl00_MainContent_rptSearchResult_{ctl}_lnkJobTitleFR" '
        f'href="/Preview.aspx?Language=French&amp;JobID={job_id}">{french}</a>'
        if french
        else ""
    )
    return f"""
      <div>{counter}.</div>
      <a id="ctl00_MainContent_rptSearchResult_{ctl}_lnkJobTitleEN" class="job-link"
         href="/Preview.aspx?Language=English&amp;JobID={job_id}">{title}</a>
      {fr}
      <div>Organization:</div><div>{org}</div>
      <div>Salary:</div><div>{salary}</div>
      <div>Location:</div><div>{location}</div>
      <div>Closing Date:</div><div>{closing}</div>
      <img id="ctl00_MainContent_rptSearchResult_{ctl}_imgNew" src="new.gif" />
    """


RESULTS_HTML = f"""
<html><body><form>
<div>Results 1 - 3 of 42</div>
{_row("ctl01", "111", "Policy Advisor (1)", org="Ministry of Health",
      salary="$80,000.00 - $90,000.00 Per year", location="Toronto, Toronto Region",
      closing="Monday, August 24, 2026 11:59 pm EDT", counter="1")}
{_row("ctl02", "222", "Court and Client Representative - Bilingual (2)",
      org="Ministry of the Attorney General", salary="$29.53 - $34.75 Per hour",
      location="Cochrane, North Region",
      closing="Monday, August 31, 2026 11:59 pm EDT", counter="2",
      french="Préposée Aux Services (2)")}
{_row("ctl03", "333", "Conservation Officer (3)", org="Ministry of Natural Resources",
      salary="$39.08 - $43.56 Per hour", location="Kenora, North Region",
      closing="Tuesday, September 8, 2026 11:59 pm EDT", counter="3")}
<a id="ctl00_MainContent_lnkButton_Page0" title="Result Page 1"
   href="javascript:__doPostBack('ctl00$MainContent$lnkButton_Page0','')">1</a>
<a id="ctl00_MainContent_lnkButton_Page1" title="Result Page 2"
   href="javascript:__doPostBack('ctl00$MainContent$lnkButton_Page1','')">2</a>
</form>
<div class="footer">Home Accessibility Privacy Terms of use</div>
<script>var startTime; // lots of script</script>
</body></html>
"""

JOB_HTML = """
<html><head><title>Ontario Public Service Careers - Job Preview</title></head>
<body>
<h1>Conservation Officer</h1>
<div>Apply By:</div><div>Tuesday, September 2, 2025 11:59 pm EDT</div>
<div>Competition Status:</div><div>Position Filled</div>
<div>Job ID:</div><div>232882</div>
<div>Posting status:</div><div>Open</div>
<div>Organization:</div><div>Ministry of Natural Resources</div>
<div>Division:</div><div>Enforcement Branch</div>
<div>City:</div><div>Kenora, Nipigon</div>
<div>Salary:</div><div>$39.08 - $43.56 Per hour</div>
<div>Category:</div><div>Corrections and Enforcement</div>
<div>Posted on:</div><div>Monday, August 18, 2025</div>
<a class="btn">Apply now</a><a class="btn">Accessibility support</a>
<!--</div>-->
<h2>Conservation Officer</h2>
<hr /><b>The Job</b><br/>Protect Ontario's natural resources.
<div class="footer">Home Accessibility Privacy</div>
</body></html>
"""

# A posting whose copy opens on a bare text node rather than a tag. Measured:
# 232882 opens on <b>, 247849 on plain text, and anchoring the body on a tag
# lookahead silently produced an empty body for the second.
JOB_HTML_PLAIN_BODY = """
<html><head><title>Ontario Public Service Careers - Job Preview</title></head>
<body>
<h1>Forensic Analyst</h1>
<div>Job ID:</div><div>247849</div>
<div>Posting status:</div><div>Open</div>
<div>Salary:</div><div>$1,171.91 - $1,393.37 Per week</div>
<hr /> Do you have experience working in a routine, high throughput environment?
<div class="footer">Home Accessibility Privacy</div>
</body></html>
"""


# --------------------------------------------------------------------------
# WebForms protocol
# --------------------------------------------------------------------------


class TestFormState:
    def test_reads_the_three_state_fields(self):
        state = api.form_state(FORM_HTML)
        assert state["__VIEWSTATE"] == "STATE-1"
        assert state["__VIEWSTATEGENERATOR"] == "BBBC20B8"
        assert state["__EVENTVALIDATION"] == "EV-1"

    def test_missing_viewstate_is_an_error_not_an_empty_search(self):
        # Posting without __VIEWSTATE returns the bare form, which parses as
        # zero postings. Failing loudly is the difference between "the layout
        # changed" and "no jobs matched".
        with pytest.raises(api.GoJobsProtocolError):
            api.form_state("<html><body>no form here</body></html>")

    def test_facets_are_json_arrays_not_bare_strings(self):
        # A bare string is accepted and silently ignored by the board: the
        # response comes back with the full unfiltered count.
        form = api.build_form(FORM_HTML, regions=["REGION-TRNT"], submit=True)
        assert form["ctl00$MainContent$ucRegion$hiddenSelected"] == '["REGION-TRNT"]'
        assert json.loads(form["ctl00$MainContent$ucRegion$hiddenSelected"]) == ["REGION-TRNT"]

    def test_empty_facet_is_empty_string_not_empty_array(self):
        form = api.build_form(FORM_HTML, submit=True)
        assert form["ctl00$MainContent$ucCity$hiddenSelected"] == ""

    def test_submit_button_only_on_a_search(self):
        assert "ctl00$MainContent$btnSearch" in api.build_form(FORM_HTML, submit=True)
        assert "ctl00$MainContent$btnSearch" not in api.build_form(FORM_HTML)

    def test_paging_is_an_event_target(self):
        form = api.build_form(FORM_HTML, event_target=api.page_event_target(2))
        assert form["__EVENTTARGET"] == "ctl00$MainContent$lnkButton_Page2"


class TestVocabularies:
    def test_parses_each_facet(self):
        vocab = api.vocabularies(FORM_HTML)
        assert vocab["region"]["REGION-TRNT"] == "Toronto"
        assert vocab["category"]["JOBCATGY-IT"] == "Information Technology"
        assert vocab["career_level"]["CAREERLEVEL-EXEC"] == "Executive"
        assert vocab["city"]["CITY-HMLTN"] == "Hamilton"

    def test_drops_the_all_entry(self):
        # The "All regions" option has an empty value and is not a filter.
        assert "" not in api.vocabularies(FORM_HTML)["region"]


# --------------------------------------------------------------------------
# Result parsing
# --------------------------------------------------------------------------


class TestParseResults:
    def test_board_total(self):
        assert api.board_total(RESULTS_HTML) == 42

    def test_every_row_is_found(self):
        jobs = api.parse_results(RESULTS_HTML)
        assert [job["job_id"] for job in jobs] == ["111", "222", "333"]

    def test_bilingual_row_keeps_its_fields(self):
        # Regression: splitting on the repeater id cut this row at its French
        # title anchor, leaving every field empty while its neighbours parsed.
        row = next(j for j in api.parse_results(RESULTS_HTML) if j["job_id"] == "222")
        assert row["organization"] == "Ministry of the Attorney General"
        assert row["location"] == "Cochrane, North Region"
        assert row["closing_date"] == "Monday, August 31, 2026 11:59 pm EDT"

    def test_bilingual_row_reports_the_english_title(self):
        row = next(j for j in api.parse_results(RESULTS_HTML) if j["job_id"] == "222")
        assert row["title"] == "Court and Client Representative - Bilingual (2)"

    def test_no_field_carries_the_next_rows_counter(self):
        # Regression: the row number sits before the next title, so the last
        # field ended "...11:59 pm EDT 17."
        for job in api.parse_results(RESULTS_HTML):
            assert not job["closing_date"].rstrip().endswith(".")

    def test_last_row_does_not_swallow_the_page_footer(self):
        # Regression: with no following anchor, the final row ran to the end of
        # the document and absorbed the pagination strip, footer and ~130KB of
        # inline script into its closing date.
        last = api.parse_results(RESULTS_HTML)[-1]
        assert last["closing_date"] == "Tuesday, September 8, 2026 11:59 pm EDT"
        assert "Accessibility" not in last["closing_date"]
        assert "startTime" not in last["closing_date"]

    def test_urls_are_absolute(self):
        for job in api.parse_results(RESULTS_HTML):
            assert job["url"].startswith("https://www.gojobs.gov.on.ca/Preview.aspx")

    def test_a_page_with_no_rows_is_empty_not_an_error(self):
        assert api.parse_results("<html><body>No matching jobs</body></html>") == []

    def test_a_recognised_empty_result_page_is_empty(self):
        jobs, total = api.parse_search_response(
            "<html><body>No matching jobs</body></html>"
        )
        assert jobs == []
        assert total == 0

    def test_the_boards_live_zero_count_is_a_recognised_empty_result(self):
        # Live response for Hamilton + Executive. Empty pages omit the usual
        # "1 - 10" range and render this shorter count instead.
        jobs, total = api.parse_search_response(
            "<html><body><div>Results 0 of 0</div></body></html>"
        )
        assert jobs == []
        assert total == 0

    def test_a_bare_200_form_is_not_an_empty_result_page(self):
        with pytest.raises(api.GoJobsUnrecognisedPageError):
            api.parse_search_response(FORM_HTML)

    def test_unterminated_comments_do_not_stall(self):
        # Remote pages are parsed synchronously.  The former lazy comment
        # regex rescanned the remainder for every opener and took seconds on
        # an 80KB malformed response.
        import time

        started = time.monotonic()
        assert api._text("<!--" * 20_000) == ""
        assert time.monotonic() - started < 2.0


class TestParseJob:
    def test_extracts_the_fields(self):
        job = api.parse_job(JOB_HTML)
        assert job["job_id"] == "232882"
        assert job["title"] == "Conservation Officer"
        assert job["fields"]["Organization"] == "Ministry of Natural Resources"
        assert job["fields"]["Division"] == "Enforcement Branch"

    def test_apply_by_stops_at_the_competition_block(self):
        # Regression: "Competition Status" was not a boundary label, so Apply By
        # swallowed the entire competition block.
        assert api.parse_job(JOB_HTML)["fields"]["Apply By"] == (
            "Tuesday, September 2, 2025 11:59 pm EDT"
        )

    def test_category_stops_at_posted_on(self):
        assert api.parse_job(JOB_HTML)["fields"]["Category"] == "Corrections and Enforcement"

    def test_competition_status_is_captured_separately(self):
        # The board reports "Posting status: Open" on a filled competition, so
        # the status field alone would present a closed job as live.
        job = api.parse_job(JOB_HTML)
        assert job["competition_status"] == "Position Filled"
        assert job["fields"]["Posting status"] == "Open"

    def test_a_page_that_is_not_a_posting_is_none(self):
        # Preview.aspx answers 200 and renders its shell for an unknown id.
        assert api.parse_job("<html><body><h1>Search</h1></body></html>") is None

    def test_last_fact_does_not_run_into_the_job_description(self):
        # Regression: Salary is the last labelled fact and nothing follows it,
        # so it absorbed 600 characters of the posting body.
        salary = api.parse_job(JOB_HTML)["fields"]["Salary"]
        assert salary == "$39.08 - $43.56 Per hour"
        assert "The Job" not in salary

    def test_button_text_is_not_part_of_a_field(self):
        # Regression: "Apply now" / "Accessibility support" carry no label of
        # their own and landed on whichever field parsed last.
        for value in api.parse_job(JOB_HTML)["fields"].values():
            assert "Apply now" not in value
            assert "Accessibility support" not in value

    def test_comment_close_marker_is_not_left_in_text(self):
        # Regression: the page contains `<!--</div>-->`; the tag stripper ate
        # `<!--</div>` and left a bare `-->` glued to the previous field.
        for value in api.parse_job(JOB_HTML)["fields"].values():
            assert "--&gt;" not in value
            assert "-->" not in value

    def test_repeated_title_heading_is_not_part_of_a_field(self):
        # The page repeats the job title as an unlabelled heading inside the
        # facts block, which landed on the preceding field.
        for value in api.parse_job(JOB_HTML)["fields"].values():
            assert not value.endswith("Conservation Officer")


class TestSplitJobPage:
    def test_body_is_found_when_copy_opens_on_a_tag(self):
        job = api.parse_job(JOB_HTML)
        assert "Protect Ontario's natural resources." in render.job_body_markdown(
            job["body_html"]
        )

    def test_body_is_found_when_copy_opens_on_plain_text(self):
        # Regression: a `<hr>` lookahead requiring <b>/<strong>/<p> produced an
        # empty body for this shape while working for the other.
        job = api.parse_job(JOB_HTML_PLAIN_BODY)
        body = render.job_body_markdown(job["body_html"])
        assert "high throughput environment" in body

    def test_body_stops_at_the_footer(self):
        job = api.parse_job(JOB_HTML)
        assert "Accessibility Privacy" not in render.job_body_markdown(job["body_html"])

    def test_a_page_with_no_rule_yields_no_body(self):
        facts, body = api.split_job_page("<html><body>Job ID: 1</body></html>")
        assert body == ""
        assert "Job ID" in facts

    def test_unsafe_description_links_are_unwrapped(self):
        body = (
            '<p>Apply <a href="javascript:alert(1)">here</a> or '
            '<a href="https://jobs.example/apply">on the employer site</a>.</p>'
        )

        out = render.job_body_markdown(body)

        assert "javascript:" not in out
        assert "Apply here" in out
        assert "[on the employer site](https://jobs.example/apply)" in out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


class TestRender:
    def test_filled_competition_is_stated_before_the_fields(self):
        out = render.render_job(api.parse_job(JOB_HTML))
        assert "Competition status:" in out
        assert out.index("Competition status:") < out.index("Posting status:")

    def test_job_body_is_included(self):
        out = render.render_job(api.parse_job(JOB_HTML))
        assert "Protect Ontario's natural resources." in out

    def test_search_counts_reconcile(self):
        out = render.render_search_results(
            api.parse_results(RESULTS_HTML)[:2],
            title="advisor",
            board_total=42,
            title_filtered=5,
            examined=7,
            truncated_by_limit=0,
        )
        assert "2 jobs shown" in out
        assert "dropped 5 by title" in out

    def test_empty_result_says_so(self):
        out = render.render_search_results([], title="nothing")
        assert "No matching postings." in out

    def test_notes_are_surfaced(self):
        out = render.render_search_results([], notes=["the filter was not applied."])
        assert "the filter was not applied." in out


# --------------------------------------------------------------------------
# Filter resolution
# --------------------------------------------------------------------------


class TestLocationResolution:
    def setup_method(self):
        self.vocab = api.vocabularies(FORM_HTML)

    def test_city_wins_over_region(self):
        cities, regions, board_wide = search._resolve_location("Toronto", self.vocab)
        assert cities == ["CITY-TRNT"]
        assert regions == []
        assert board_wide is False

    def test_region_name_resolves_when_no_city_matches(self):
        cities, regions, board_wide = search._resolve_location("North", self.vocab)
        assert regions == ["REGION-NRTH"]
        assert cities == []

    def test_a_region_name_is_not_hijacked_by_similarly_named_cities(self):
        # Measured: "North" fuzzily matched North Bay / North York /
        # Northbrook, so the board was asked for ~10 postings when the North
        # *region* has 39 — and the 29 the client filter would have kept were
        # never fetched. A board filter that can EXCLUDE a valid posting is not
        # an optimisation, it is a wrong answer.
        vocab = {
            "region": {"REGION-NRTH": "North"},
            "city": {
                "CITY-NB": "North Bay",
                "CITY-NY": "North York",
                "CITY-NBRK": "Northbrook",
            },
        }
        cities, regions, _ = search._resolve_location("North", vocab)
        assert regions == ["REGION-NRTH"]
        assert cities == []

    def test_unrecognised_place_applies_no_board_filter(self):
        # Falling back to a loose match could narrow the pool below what the
        # client filter would keep. No filter is the safe answer; the client
        # filter still guarantees the result.
        cities, regions, board_wide = search._resolve_location("Nowheresville", self.vocab)
        assert cities == [] and regions == [] and board_wide is False

    def test_city_wins_over_an_identically_named_region(self):
        # "Toronto" is both. The city is the more specific reading: 32 postings
        # against the region's 35.
        cities, regions, _ = search._resolve_location("Toronto", self.vocab)
        assert cities == ["CITY-TRNT"] and regions == []

    def test_ontario_is_the_whole_board_not_a_filter(self):
        # Every posting is in Ontario and no row names the province, so
        # filtering on it matched nothing and emptied the result set.
        cities, regions, board_wide = search._resolve_location("Ontario", self.vocab)
        assert board_wide is True
        assert not cities and not regions

    def test_canada_is_also_board_wide(self):
        assert search._resolve_location("Canada", self.vocab)[2] is True

    @pytest.mark.parametrize("value", ["Ontario, Canada", "Canada, Ontario"])
    def test_punctuated_province_country_is_board_wide(self, value):
        assert search._resolve_location(value, self.vocab) == ([], [], True)

    def test_blank_location_is_not_board_wide(self):
        assert search._resolve_location("", self.vocab) == ([], [], False)

    @pytest.mark.asyncio
    async def test_unresolved_place_does_not_scope_the_board_total(
        self, monkeypatch
    ):
        async def fake_session(_browser_solver=None):
            return object()

        async def fake_vocabularies(_session):
            return {"region": {}, "city": {}, "category": {}, "career_level": {}}

        async def fake_search_all(**_kwargs):
            jobs = [
                {
                    "job_id": str(index),
                    "title": "Nurse",
                    "location": "Niagara Falls, West Region"
                    if index == 0
                    else "Toronto, Toronto Region",
                    "url": f"https://example.test/{index}",
                }
                for index in range(15)
            ]
            return jobs, 15, True

        monkeypatch.setattr(api, "_get_session", fake_session)
        monkeypatch.setattr(api, "get_vocabularies", fake_vocabularies)
        monkeypatch.setattr(api, "search_all", fake_search_all)

        result = await search.search_gojobs(location="Niagara")
        content = result["content"]
        assert "ranked in Niagara" not in content
        assert "has 15 in Niagara" not in content
        assert "no location filter was applied" in content


class TestFacetResolution:
    def setup_method(self):
        self.vocab = api.vocabularies(FORM_HTML)

    def test_matches_a_human_label(self):
        assert search._resolve_facet("Information Technology", self.vocab["category"]) == [
            "JOBCATGY-IT"
        ]

    def test_accepts_a_raw_code(self):
        assert search._resolve_facet("JOBCATGY-IT", self.vocab["category"]) == ["JOBCATGY-IT"]

    def test_unknown_value_resolves_to_nothing(self):
        # The caller is told the filter was not applied rather than silently
        # getting the whole board under a narrowed heading.
        assert search._resolve_facet("Underwater Basket Weaving", self.vocab["category"]) == []

    def test_blank_is_no_filter(self):
        assert search._resolve_facet("", self.vocab["category"]) == []

    def test_partial_label_does_not_narrow_to_an_arbitrary_subset(self):
        vocabulary = {
            f"CATEGORY-{index}": f"Information Services {index}"
            for index in range(8)
        }
        assert search._resolve_facet("Information", vocabulary) == []


class TestFacetScope:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("arguments", "expected", "api_field", "api_value"),
        [
            (
                {"category": "Information Technology"},
                "category: Information Technology",
                "categories",
                ["JOBCATGY-IT"],
            ),
            (
                {"career_level": "Executive"},
                "career level: Executive",
                "career_levels",
                ["CAREERLEVEL-EXEC"],
            ),
            (
                {"min_salary": "80000"},
                "minimum salary: $80,000+",
                "min_salary",
                "80000",
            ),
        ],
    )
    async def test_board_only_filter_is_visible_and_applied(
        self, monkeypatch, arguments, expected, api_field, api_value
    ):
        async def fake_session(_browser_solver=None):
            return object()

        async def fake_vocabularies(_session):
            return {
                "region": {},
                "city": {},
                "category": {"JOBCATGY-IT": "Information Technology"},
                "career_level": {"CAREERLEVEL-EXEC": "Executive"},
            }

        captured = {}

        async def fake_search_all(**kwargs):
            captured.update(kwargs)
            return [], 12, True

        monkeypatch.setattr(api, "_get_session", fake_session)
        monkeypatch.setattr(api, "get_vocabularies", fake_vocabularies)
        monkeypatch.setattr(api, "search_all", fake_search_all)

        result = await search.search_gojobs(**arguments)

        assert expected in result["content"]
        assert captured[api_field] == api_value

    def test_category_is_also_part_of_the_board_count_scope(self):
        out = render.render_search_results(
            [],
            category="Information Technology",
            board_total=12,
            examined=12,
        )
        assert "has 12 in category “Information Technology”" in out


class TestExamineCeiling:
    def test_is_a_constant_not_a_multiple_of_limit(self):
        # Deriving the examined pool from `limit` makes the answer depend on
        # how many rows the caller asked to see.
        assert isinstance(search._EXAMINE_CEILING, int)
        assert search._EXAMINE_CEILING >= 100


class TestSearchErrors:
    @pytest.mark.asyncio
    async def test_unrecognised_200_is_not_reported_as_an_empty_search(
        self, monkeypatch
    ):
        async def fake_session(_browser_solver=None):
            return object()

        async def fake_vocabularies(_session):
            return {"region": {}, "city": {}, "category": {}, "career_level": {}}

        async def fake_search_all(**_kwargs):
            raise api.GoJobsUnrecognisedPageError("search shell")

        monkeypatch.setattr(api, "_get_session", fake_session)
        monkeypatch.setattr(api, "get_vocabularies", fake_vocabularies)
        monkeypatch.setattr(api, "search_all", fake_search_all)

        result = await search.search_gojobs(title="nurse")

        assert "error" in result
        assert "answered 200" in result["error"]
        assert "No empty-search conclusion" in result["error"]


class TestGetJobValidation:
    @pytest.mark.asyncio
    async def test_non_numeric_job_id_is_rejected_before_any_request(self):
        result = await search.get_gojobs_job("not-a-number")
        assert "error" in result
        assert "numeric" in result["error"]


class TestJobIdBoundaryRules:
    """`job_id` is validated per board, not by one shared LinkedIn rule."""

    def test_short_ops_job_ids_are_accepted(self):
        # Regression: the shared rule required 6-20 digits, so real OPS
        # postings — 2799 and 13473 are both live — were rejected at the MCP
        # boundary before the tool ran.
        for job_id in ("1", "2799", "13473", "232882"):
            assert server._validate_tool_arguments("get_gojobs_job", {"job_id": job_id}) is None

    def test_error_names_the_right_board(self):
        error = server._validate_tool_arguments("get_gojobs_job", {"job_id": "abc"})
        assert "Ontario Public Service" in error
        assert "LinkedIn" not in error

    def test_linkedin_rule_is_unchanged(self):
        assert server._validate_tool_arguments("get_linkedin_job", {"job_id": "4171234567"}) is None
        # LinkedIn ids really are long; a short one is still a mistake there.
        assert server._validate_tool_arguments("get_linkedin_job", {"job_id": "2799"}) is not None


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Skip the 2s inter-request spacing; these tests issue no real requests."""

    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(api.gojobs_limiter, "wait", instant)


class TestExchangeSerialisation:
    """A WebForms search is a conversation; two cannot share a session at once."""

    @pytest.mark.asyncio
    async def test_concurrent_searches_do_not_interleave(self, no_rate_limit):
        # Measured against the live board: a Toronto and a Thunder Bay search
        # run concurrently came back 35 and 127, where run one after another
        # they return 32 and 17. Both wrong answers were well-formed and
        # plausible, and nothing in either said a filter had been dropped.
        events: list[str] = []

        class FakeSession:
            def __init__(self, tag):
                self.tag = tag

            async def get(self, url, **kwargs):
                events.append(f"{self.tag}:get")
                await asyncio.sleep(0)
                return SimpleNamespace(status_code=200, text=FORM_HTML)

            async def post(self, url, **kwargs):
                events.append(f"{self.tag}:post")
                await asyncio.sleep(0)
                return SimpleNamespace(status_code=200, text=RESULTS_HTML)

        await asyncio.gather(
            api.search_all(session=FakeSession("A"), max_records=3),
            api.search_all(session=FakeSession("B"), max_records=3),
        )

        # Whichever ran first, its whole exchange must complete before the
        # other's begins.
        first = events[0].split(":")[0]
        boundary = next(i for i, e in enumerate(events) if not e.startswith(first))
        assert all(e.startswith(first) for e in events[:boundary]), events
        assert all(not e.startswith(first) for e in events[boundary:]), events

    @pytest.mark.asyncio
    async def test_uncached_vocabulary_get_waits_for_an_active_exchange(
        self, monkeypatch
    ):
        await api.close_session()
        # pytest creates a fresh event loop for this test; use locks owned by it
        # instead of module locks exercised by the preceding async test.
        monkeypatch.setattr(api, "_exchange_lock", asyncio.Lock())
        monkeypatch.setattr(api, "_vocab_lock", asyncio.Lock())
        fetch_started = asyncio.Event()

        async def fake_fetch(_session):
            fetch_started.set()
            return FORM_HTML

        monkeypatch.setattr(api, "fetch_search_form", fake_fetch)

        await api._exchange_lock.acquire()
        try:
            task = asyncio.create_task(api.get_vocabularies(object()))
            await asyncio.sleep(0)
            assert not fetch_started.is_set()
        finally:
            api._exchange_lock.release()

        vocab = await task
        assert fetch_started.is_set()
        assert vocab["city"]["CITY-TRNT"] == "Toronto"
        await api.close_session()

    @pytest.mark.asyncio
    async def test_recognised_zero_stops_after_the_search_postback(
        self, monkeypatch, no_rate_limit
    ):
        monkeypatch.setattr(api, "_exchange_lock", asyncio.Lock())
        zero_page = FORM_HTML.replace(
            "</form>", "<div>Results 0 of 0</div></form>"
        )

        class Session:
            def __init__(self):
                self.gets = 0
                self.posts = 0

            async def get(self, url, **kwargs):
                self.gets += 1
                return SimpleNamespace(status_code=200, text=FORM_HTML)

            async def post(self, url, **kwargs):
                self.posts += 1
                return SimpleNamespace(status_code=200, text=zero_page)

        session = Session()
        assert await api.search_all(session=session, max_records=200) == ([], 0, True)
        assert session.gets == 1
        assert session.posts == 1


class TestVocabularyCache:
    @pytest.mark.asyncio
    async def test_form_is_fetched_once_per_process(self, no_rate_limit):
        # The lists change when Ontario reorganises its ministries. Fetching
        # them on every search costs a request per call for nothing, and put a
        # second GET of Search.aspx alongside the locked exchange.
        await api.close_session()
        calls = {"n": 0}

        class CountingSession:
            async def get(self, url, **kwargs):
                calls["n"] += 1
                return SimpleNamespace(status_code=200, text=FORM_HTML)

        session = CountingSession()
        first = await api.get_vocabularies(session)
        second = await api.get_vocabularies(session)
        assert calls["n"] == 1
        assert first is second
        assert first["region"]["REGION-TRNT"] == "Toronto"
        await api.close_session()

    @pytest.mark.asyncio
    async def test_close_session_clears_the_cache(self):
        await api.close_session()
        assert api._vocab_cache is None

    @pytest.mark.asyncio
    async def test_a_page_without_cities_is_not_cached(self, no_rate_limit):
        # Search.aspx serves ~499KB with all 984 cities on a session's FIRST
        # GET and ~65KB with no city declaration on every one after. Caching
        # the light page leaves the process permanently unable to resolve a
        # city, which silently degrades "Toronto" to the region filter.
        await api.close_session()
        light = FORM_HTML.replace("var options_ucCity", "var options_ucNothing")
        pages = [light, FORM_HTML]

        class Session:
            async def get(self, url, **kwargs):
                return SimpleNamespace(status_code=200, text=pages.pop(0))

        session = Session()
        first = await api.get_vocabularies(session)
        assert first["city"] == {}
        assert api._vocab_cache is None, "the city-less page must not be cached"

        second = await api.get_vocabularies(session)
        assert second["city"], "a later call must be able to recover the cities"
        assert api._vocab_cache is not None
        await api.close_session()


class TestToolSurface:
    def test_search_requires_at_least_one_filter(self):
        # An unfiltered listing is a dozen sequential postbacks.
        error = server._validate_tool_arguments("search_gojobs", {})
        assert error and "at least one of" in error

    def test_category_is_spelled_job_category(self):
        # `category` is bound to the marketplace enum by a global by-name
        # fallback, which would reject every real OPS category.
        assert server._validate_tool_arguments("search_gojobs", {"category": "cars"}) is not None
        assert (
            server._validate_tool_arguments(
                "search_gojobs", {"job_category": "Information Technology"}
            )
            is None
        )


class TestProvinceWidePostings:
    """A posting open across Ontario is available in every Ontario city."""

    def test_province_wide_location_is_recognised(self):
        for value in (
            "Any City, Anywhere in Ontario",
            "any city",
            "Anywhere in Ontario",
            "Province-wide",
        ):
            assert search._location_is_open_to_anywhere(value), value

    def test_an_ordinary_location_is_not(self):
        for value in ("Toronto, Toronto Region", "Kenora, North Region", ""):
            assert not search._location_is_open_to_anywhere(value), value

    def test_a_city_filter_keeps_a_province_wide_posting(self):
        # Regression: every Toronto search reported an unexplained "dropped 1 by
        # location". It was "Any City, Anywhere in Ontario" — a real match for
        # Toronto being thrown away by the strict city check.
        from fetchaller.jobfilter import location_matches, tokens

        wanted = tokens("Toronto")
        row = "Any City, Anywhere in Ontario"
        assert not location_matches(row, wanted)
        assert search._location_is_open_to_anywhere(row)


class TestPunctuationInsensitiveLocations:
    def test_a_full_stop_does_not_defeat_the_match(self):
        # Regression: the board spells it "St Catharines"; a caller naturally
        # types "St. Catharines". A plain casefold comparison missed it, so no
        # board filter was applied and the search silently degraded to a text
        # match over the whole board.
        vocab = {"city": {"CITY-STCT": "St Catharines"}, "region": {}}
        for typed in ("St. Catharines", "St Catharines", "st. catharines"):
            cities, regions, _ = search._resolve_location(typed, vocab)
            assert cities == ["CITY-STCT"], typed

    def test_an_apostrophe_is_folded_too(self):
        vocab = {"city": {"CITY-X": "L'Orignal"}, "region": {}}
        assert search._resolve_location("LOrignal", vocab)[0] == ["CITY-X"]

    def test_a_genuinely_unknown_place_still_resolves_to_nothing(self):
        vocab = {"city": {"CITY-STCT": "St Catharines"}, "region": {}}
        assert search._resolve_location("Niagara", vocab) == ([], [], False)


class TestResultUrlIsRebuilt:
    def test_url_never_comes_from_the_remote_href(self):
        # Accepting any href starting with "http" let the board's own markup
        # steer a caller off-site and preserved its query string.
        hostile = (
            '<a id="ctl00_MainContent_rptSearchResult_ctl01_lnkJobTitleEN" '
            'href="https://evil.example/Preview.aspx?JobID=999&csrf=SECRET">Job</a>'
        )
        jobs = api.parse_results(hostile)
        assert jobs, "row should still parse"
        assert jobs[0]["url"] == "https://www.gojobs.gov.on.ca/Preview.aspx?JobID=999"
        assert "evil.example" not in jobs[0]["url"]
        assert "SECRET" not in jobs[0]["url"]
