"""URL detection + rendering unit tests for the Workday module.

Network-touching board/posting fetches are exercised by the
``manual_test_*.py`` scripts in the repo; here we cover URL parsing,
HTML→markdown conversion, and end-to-end render of a stubbed payload so
regressions show up in CI.
"""

from fetchaller.content import workday

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
