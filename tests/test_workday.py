"""URL detection + rendering unit tests for the Workday module.

Network-touching board/posting fetches are exercised by the
``manual_test_*.py`` scripts in the repo; here we cover URL parsing,
HTML→markdown conversion, and end-to-end render of a stubbed payload so
regressions show up in CI.
"""

from fetchaller.content import workday
from fetchaller.workday import search

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestUrlDetection:
    def test_board_with_lang(self):
        u = "https://cae.wd3.myworkdayjobs.com/en-US/career"
        assert workday.is_workday_board_url(u)
        assert not workday.is_workday_url(u)
        assert workday.extract_workday_board_params(u) == ("cae", "en-US", "career")

    def test_board_without_lang(self):
        u = "https://cae.wd3.myworkdayjobs.com/career"
        assert workday.is_workday_board_url(u)
        assert workday.extract_workday_board_params(u) == ("cae", "", "career")

    def test_board_underscored_site(self):
        u = "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site"
        assert workday.is_workday_board_url(u)
        assert workday.extract_workday_board_params(u) == (
            "salesforce", "en-US", "External_Career_Site",
        )

    def test_board_trailing_slash(self):
        u = "https://cae.wd3.myworkdayjobs.com/en-US/career/"
        assert workday.is_workday_board_url(u)

    def test_posting_with_lang(self):
        u = "https://cae.wd3.myworkdayjobs.com/en-US/career/job/Altus/KC-135-Pilot-Instructor_121984"
        assert workday.is_workday_url(u)
        assert workday.extract_workday_params(u) == (
            "cae", "en-US", "career", "/job/Altus/KC-135-Pilot-Instructor_121984",
        )

    def test_posting_without_lang(self):
        u = "https://cae.wd3.myworkdayjobs.com/career/job/Altus/KC-135-Pilot-Instructor_121984"
        assert workday.is_workday_url(u)

    def test_posting_with_nested_path(self):
        u = (
            "https://cae.wd3.myworkdayjobs.com/en-US/career/job/"
            "Montreal---8585-Cote-De-Liesse-QC-Canada/Procurement-Compliance-and-Risk-Officer-Temporary_120266"
        )
        assert workday.is_workday_url(u)

    def test_not_workday(self):
        for u in [
            "https://example.com/careers/123",
            "https://cae.example.com/career",  # not myworkdayjobs.com
            "https://cae.wd3.myworkdayjobs.com/",  # no site segment
            "https://cae.wd3.myworkdayjobs.com/en-US/job/foo/bar",  # missing site
        ]:
            assert not workday.is_workday_url(u)
            assert not workday.is_workday_board_url(u)

    def test_site_named_job_is_rejected(self):
        # A 'job' segment at the board position is actually a path, not a site.
        u = "https://cae.wd3.myworkdayjobs.com/en-US/job/Foo/Bar"
        assert not workday.is_workday_board_url(u)
        assert not workday.is_workday_url(u)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRender:
    def test_render_posting_includes_title_and_description(self):
        payload = {
            "jobPostingInfo": {
                "id": "abc123",
                "title": "Senior Engineer",
                "location": "Toronto, ON",
                "timeType": "Full time",
                "jobDescription": "<p>About the role.</p><ul><li>Build cool things</li></ul>",
            },
        }
        out = workday.render_workday_job(payload, source_url="https://x.wd1.myworkdayjobs.com/career/job/T/Senior")
        assert out.startswith("# Senior Engineer\n")
        assert "- **id**: abc123" in out
        assert "- **location**: Toronto, ON" in out
        assert "- **timeType**: Full time" in out
        assert "## jobDescription" in out
        assert "About the role." in out
        assert "Build cool things" in out
        assert "**sourceUrl**: https://x.wd1.myworkdayjobs.com/career/job/T/Senior" in out

    def test_render_strips_wkq0_layout_spans(self):
        payload = {
            "jobPostingInfo": {
                "title": "T",
                "jobDescription": '<p>Hello <span class="WKQ0">                                            </span> world</p>',
            },
        }
        out = workday.render_workday_job(payload)
        # The 50-space span shouldn't survive into the rendered markdown.
        assert "Hello world" in out
        assert "                  " not in out

    def test_render_board_groups_listings(self):
        payload = {
            "jobPostings": [
                {
                    "title": "KC-135 Pilot Instructor",
                    "externalPath": "/job/Altus/KC-135-Pilot-Instructor_121984",
                    "locationsText": "Altus",
                    "postedOn": "Posted Today",
                    "bulletFields": ["121984"],
                },
                {
                    "title": "Avionics Technician",
                    "externalPath": "/job/Ottawa/Avionics-Technician_117102",
                    "locationsText": "Ottawa",
                    "postedOn": "Posted Today",
                    "bulletFields": ["117102"],
                },
            ],
            "total": 2,
            "tenant": "cae",
            "site": "career",
        }
        out = workday.render_workday_board(
            payload, source_url="https://cae.wd3.myworkdayjobs.com/en-US/career"
        )
        assert out.startswith("# cae — Job Board (2 open positions)\n")
        assert "- **KC-135 Pilot Instructor** — Altus · Posted Today" in out
        assert "- https://cae.wd3.myworkdayjobs.com/en-US/career/job/Altus/KC-135-Pilot-Instructor_121984" in out
        assert "bulletFields: 117102" in out

    def test_render_board_handles_empty(self):
        out = workday.render_workday_board(
            {"jobPostings": [], "total": 0, "tenant": "x", "site": "s"},
            source_url="https://x.wd1.myworkdayjobs.com/s",
        )
        assert "Job Board (0 open positions)" in out


class TestMultiLocationPostings:
    """Workday's list API summarises multi-location postings.

    "11 Locations" and "Maryland, US Offsite, More..." name places the caller
    cannot screen on, and geo eligibility decides whether a posting is worth
    opening at all. The real list is `additionalLocations` on the detail
    endpoint; nothing in the list response carries it.
    """

    def test_a_count_summary_is_recognised(self):
        assert search._is_location_summary({"locationsText": "11 Locations"})
        assert search._is_location_summary({"locationsText": "2 Locations"})
        assert search._is_location_summary({"locationsText": "1 Location"})

    def test_a_truncated_list_is_recognised(self):
        assert search._is_location_summary({"locationsText": "Maryland, US Offsite, More..."})

    def test_a_real_place_is_not_a_summary(self):
        for text in ("Vancouver, Canada", "Chicago, IL", "Allen, TX (TX139)", ""):
            assert not search._is_location_summary({"locationsText": text}), text

    def test_expanded_locations_replace_the_summary(self):
        posting = {
            "locationsText": "3 Locations",
            "allLocations": ["Toronto, ON, CAN", "Montreal, QC, CAN", "Ontario - Offsite/Home"],
        }
        assert search._locations_of(posting) == (
            "Toronto, ON, CAN · Montreal, QC, CAN · Ontario - Offsite/Home"
        )

    def test_a_long_list_is_capped_with_the_remainder_named(self):
        posting = {"locationsText": "12 Locations", "allLocations": [f"City {i}" for i in range(12)]}
        out = search._locations_of(posting)
        assert out.count(" · ") == 8  # 8 shown + the "+4 more" separator
        assert out.endswith("+4 more")

    def test_the_summary_survives_a_failed_expansion(self):
        # A detail fetch that fails must degrade the display, never the result.
        assert search._locations_of({"locationsText": "4 Locations"}) == "4 Locations"


class TestReqId:
    """`bulletFields` is a tenant-configured column list, not an id field.

    Motorola puts the location code first and the requisition second, so
    joining them rendered "Req ID: British Columbia Remote Work, R65471".
    """

    def test_the_location_column_is_not_part_of_the_id(self):
        posting = {
            "externalPath": "/job/Vancouver-Canada/DevOps-Engineer-II_R65291",
            "bulletFields": ["Vancouver on site (BRC06)", "R65291"],
        }
        assert search._req_id(posting) == "R65291"

    def test_a_tenant_that_publishes_only_the_id_is_unchanged(self):
        posting = {
            "externalPath": "/job/Toronto-ON-CAN/Software-Manager_26WD97217-2",
            "bulletFields": ["26WD97217"],
        }
        assert search._req_id(posting) == "26WD97217"

    def test_a_repost_suffix_on_the_path_still_resolves(self):
        posting = {
            "externalPath": "/job/Maryland-US-Offsite/Senior-System-Engineer_R65468-1",
            "bulletFields": ["Maryland, US Offsite (MD999)", "R65468"],
        }
        assert search._req_id(posting) == "R65468"

    def test_a_one_word_location_in_the_path_is_not_mistaken_for_the_id(self):
        # Matching anywhere in the path would take "Remote" here.
        posting = {
            "externalPath": "/job/Remote/Engineer_R123",
            "bulletFields": ["Remote", "R123"],
        }
        assert search._req_id(posting) == "R123"

    def test_an_id_absent_from_the_path_still_renders(self):
        # Better a noisy id than none; the fallback is the old behaviour.
        posting = {"externalPath": "/job/Somewhere/Role", "bulletFields": ["Somewhere", "R1"]}
        assert search._req_id(posting) == "Somewhere, R1"

    def test_no_bullets_is_empty_not_an_error(self):
        assert search._req_id({"externalPath": "/job/x/y_R1"}) == ""
        assert search._req_id({"bulletFields": [], "externalPath": "/x"}) == ""


class TestRequestedLocationOrdering:
    def test_matching_places_come_first(self):
        # Measured on Motorola R66106: 60 locations, the four Canadian ones
        # ranked 53rd onward, so a Canada search hid them behind "+52 more".
        posting = {
            "locationsText": "Colorado Remote Work, More...",
            "allLocations": [f"State {i} Remote Work" for i in range(56)]
            + ["Alberta Remote Work", "British Columbia Remote Work",
               "Ontario Remote Work", "Quebec Remote Work"],
        }
        out = search._locations_of(posting, ["ontario"])
        assert out.startswith("Ontario Remote Work · ")
        assert "+52 more" in out

    def test_order_is_untouched_without_a_requested_location(self):
        posting = {"locationsText": "3 Locations", "allLocations": ["A", "B", "C"]}
        assert search._locations_of(posting, None) == "A · B · C"


class TestBoardDiagnosis:
    """Three different failures must not read as one.

    Measured: Intuit answers 401 (gated), Visa 422 (wrong site id), Thomson
    Reuters 404 (wrong host). All three rendered as "did not answer", and only
    one of them is something the caller can fix.
    """

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    class FakeSession:
        def __init__(self, status_code=None, error=None):
            self.status_code, self.error = status_code, error

        async def post(self, *_args, **_kwargs):
            if self.error:
                raise self.error
            return TestBoardDiagnosis.FakeResponse(self.status_code)

    async def _diagnose(self, url, **kwargs):
        return await search._diagnose_board(url, self.FakeSession(**kwargs))

    async def test_a_gated_board_says_so(self):
        out = await self._diagnose(
            "https://intuit.wd1.myworkdayjobs.com/IntuitCareers", status_code=401
        )
        assert "requires a login (HTTP 401)" in out
        assert "does not publish its jobs anonymously" in out

    async def test_a_wrong_site_id_names_the_segment(self):
        out = await self._diagnose("https://visa.wd1.myworkdayjobs.com/Visa", status_code=422)
        assert "rejected the site id 'Visa' on tenant 'visa'" in out
        assert "The host is right" in out

    async def test_a_wrong_host_says_host(self):
        out = await self._diagnose(
            "https://thomsonreuters.wd5.myworkdayjobs.com/External", status_code=404
        )
        assert "No Workday board" in out and "HTTP 404" in out

    async def test_a_server_error_is_transient(self):
        out = await self._diagnose("https://x.wd1.myworkdayjobs.com/s", status_code=503)
        assert "HTTP 503" in out and "Try again shortly" in out

    async def test_an_unreachable_host_names_the_exception(self):
        out = await self._diagnose(
            "https://x.wd1.myworkdayjobs.com/s", error=OSError("no route")
        )
        assert "Could not reach x.wd1.myworkdayjobs.com (OSError)" in out

    async def test_a_url_that_is_not_a_board_never_makes_a_request(self):
        # No session call at all — the URL is wrong before the network matters.
        out = await search._diagnose_board("https://example.com/careers", None)
        assert "is not a Workday board URL" in out
