"""Unit tests for Gem content module."""

from fetchaller.content.gem import (
    extract_gem_board_slug,
    extract_gem_params,
    is_gem_board_url,
    is_gem_url,
    render_gem_board,
    render_gem_job,
)


class TestIsGemUrl:
    def test_posting(self):
        assert is_gem_url(
            "https://jobs.gem.com/function-health/am9icG9zdDramAtjvaEx5CaiubYzZIC0"
        )

    def test_not_gem(self):
        assert not is_gem_url("https://example.com/jobs/abc")

    def test_extract(self):
        assert extract_gem_params(
            "https://jobs.gem.com/function-health/am9icG9zdDra?x=1"
        ) == ("function-health", "am9icG9zdDra")


class TestIsGemBoardUrl:
    def test_board_root(self):
        assert is_gem_board_url("https://jobs.gem.com/gem")

    def test_board_root_with_slash(self):
        assert is_gem_board_url("https://jobs.gem.com/function-health/")

    def test_board_with_query(self):
        assert is_gem_board_url("https://jobs.gem.com/motion?team=engineering")

    def test_posting_not_board(self):
        # Two-segment URLs are postings, not boards
        assert not is_gem_board_url(
            "https://jobs.gem.com/function-health/am9icG9zdDramAtjvaEx5CaiubYzZIC0"
        )

    def test_different_host(self):
        assert not is_gem_board_url("https://jobs.example.com/abc")

    def test_extract_slug(self):
        assert extract_gem_board_slug("https://jobs.gem.com/function-health") == "function-health"
        assert extract_gem_board_slug("https://jobs.gem.com/abc?x=1") == "abc"


class TestRenderGemBoard:
    def test_groups_by_department_and_renders_links(self):
        jobs = [
            {
                "title": "Founding Engineer",
                "absolute_url": "https://jobs.gem.com/redo/123",
                "departments": [{"name": "Engineering"}],
                "location": {"name": "San Francisco"},
                "location_type": "hybrid",
                "employment_type": "full_time",
            },
            {
                "title": "Account Executive",
                "absolute_url": "https://jobs.gem.com/redo/456",
                "departments": [{"name": "Sales"}],
                "location": {"name": "Remote"},
                "location_type": "remote",
                "employment_type": "full_time",
            },
            {
                "title": "Senior SWE",
                "absolute_url": "https://jobs.gem.com/redo/789",
                "departments": [{"name": "Engineering"}],
                "location": {"name": "New York"},
            },
        ]
        out = render_gem_board(jobs, "redo", source_url="https://jobs.gem.com/redo")
        assert "# redo — Job Board (3 open positions)" in out
        assert "## Engineering (2)" in out
        assert "## Sales (1)" in out
        assert "**Founding Engineer**" in out
        assert "San Francisco" in out
        assert "hybrid" in out
        assert "full_time" in out
        assert "https://jobs.gem.com/redo/123" in out
        # Dept order is alphabetical: Engineering then Sales
        assert out.index("## Engineering") < out.index("## Sales")

    def test_empty_board(self):
        out = render_gem_board([], "fresh-board", source_url="https://jobs.gem.com/fresh-board")
        assert "# fresh-board — Job Board (0 open positions)" in out

    def test_missing_department_defaults_to_other(self):
        jobs = [{"title": "Stealth Role", "absolute_url": "https://jobs.gem.com/x/1"}]
        out = render_gem_board(jobs, "x")
        assert "## Other (1)" in out
        assert "**Stealth Role**" in out


class TestRenderGemJob:
    def test_preserves_raw_fields(self):
        data = {
            "oatsExternalJobPosting": {
                "id": "abc",
                "title": "Principal Product Designer",
                "descriptionHtml": "<p>We build things.</p>",
                "extId": "am9i",
                "firstPublishedTsSec": 1735689600,
                "companyUrl": "https://functionhealth.com",
                "locations": [
                    {"name": "US - Remote", "isRemote": True},
                    {"name": "Canada - Remote", "isRemote": True},
                ],
                "job": {
                    "locationType": "REMOTE",
                    "employmentType": "FULL_TIME",  # raw enum, not translated
                    "requisitionId": "DSN-25-12",
                    "teamDisplayName": "Function Health",
                    "department": {"name": "Product Design"},
                },
                "jobPostSectionHtml": {"introHtml": "<p>Hi.</p>", "outroHtml": ""},
                "compensationHtml": "<p>$150K – $200K</p>",
            },
            "oatsJobPostFieldsAndQuestions": {
                "fields": [
                    {"fieldType": "FIRST_NAME", "isRequired": True},
                    {"fieldType": "EMAIL", "isRequired": True},
                    {"fieldType": "RESUME", "isRequired": True},
                ],
                "questions": [
                    {
                        "text": "Were you referred?",
                        "answerType": "SINGLE_SELECT",
                        "displayType": "DROPDOWN",
                        "isRequired": True,
                        "options": [{"value": "Yes"}, {"value": "No"}],
                        "description": "<div>Please share.</div>",
                    },
                    {
                        "text": "Portfolio link",
                        "answerType": "LONG_TEXT",
                        "displayType": "TEXTAREA",
                        "isRequired": True,
                    },
                ],
                "demographicSurvey": {
                    "surveyType": "Diversity",
                    "questions": [{
                        "text": "Gender?",
                        "answerType": "SINGLE_SELECT",
                        "options": [{"value": "Woman"}, {"value": "Man"}],
                    }],
                },
            },
        }
        md = render_gem_job(
            data,
            source_url="https://jobs.gem.com/function-health/am9icG9zdDra",
        )
        # Title
        assert md.startswith("# Principal Product Designer")
        # Raw Gem keys preserved.
        assert "**companyUrl**: https://functionhealth.com" in md
        # Nested job object flattened but field name kept.
        assert "**firstPublishedTsSec**: 1735689600" in md
        # Raw enum values (no "Full Time" → "Full-time" translation).
        # The job dict is JSON-encoded so it contains the raw strings.
        assert "FULL_TIME" in md
        assert "REMOTE" in md
        assert "DSN-25-12" in md
        # Description body + intro + compensation
        assert "Hi." in md
        assert "We build things." in md
        assert "$150K – $200K" in md
        # Base fields: raw Gem fieldType enum
        assert "**FIRST_NAME** (required)" in md
        assert "**EMAIL** (required)" in md
        assert "**RESUME** (required)" in md
        # Questions: raw answerType/displayType
        assert "Were you referred?" in md
        assert "SINGLE_SELECT/DROPDOWN" in md
        assert "Options: Yes, No" in md
        assert "Please share." in md
        assert "<div>" not in md  # description HTML converted
        # Demographic uses Gem's own surveyType as the section label.
        assert "## Diversity" in md
        assert "Gender?" in md and "Woman" in md and "Man" in md
        # Apply URL
        assert "**sourceUrl**: https://jobs.gem.com/function-health/am9icG9zdDra" in md
