"""Unit tests for Greenhouse content module.

Tests preserve Greenhouse's raw field names and values — we don't translate
enum values or impose our own section labels on the rendered output.
"""

from bs4 import BeautifulSoup

from fetchaller.content.greenhouse import (
    extract_greenhouse_params,
    extract_greenhouse_params_from_html,
    extract_greenhouse_params_guess,
    is_greenhouse_html,
    is_greenhouse_url,
    render_greenhouse_job,
)


class TestIsGreenhouseUrl:
    def test_direct_boards_url(self):
        assert is_greenhouse_url("https://boards.greenhouse.io/instacart/jobs/7627450")

    def test_new_job_boards_url(self):
        assert is_greenhouse_url("https://job-boards.greenhouse.io/affirm/jobs/7615046003")

    def test_embed_iframe_url(self):
        assert is_greenhouse_url(
            "https://boards.greenhouse.io/embed/job_app?for=instacart&token=7627450"
        )

    def test_gh_jid_and_src(self):
        assert is_greenhouse_url("https://careers.example.com/jobs?gh_jid=123&gh_src=acme")

    def test_non_greenhouse(self):
        assert not is_greenhouse_url("https://example.com/jobs/123")


class TestExtractGreenhouseParams:
    def test_direct(self):
        assert extract_greenhouse_params(
            "https://boards.greenhouse.io/instacart/jobs/7627450"
        ) == ("instacart", "7627450")

    def test_embed(self):
        assert extract_greenhouse_params(
            "https://boards.greenhouse.io/embed/job_app?for=instacart&token=7627450"
        ) == ("instacart", "7627450")

    def test_gh_jid(self):
        assert extract_greenhouse_params(
            "https://careers.example.com/?gh_jid=999&gh_src=acme"
        ) == ("acme", "999")


class TestExtractGreenhouseParamsGuess:
    def test_dropbox_jobs_with_gh_src(self):
        assert extract_greenhouse_params_guess(
            "https://www.dropbox.jobs/en/jobs/7416012/staff-pd/?gh_src=aonhf1"
        ) == ("dropbox", "7416012")

    def test_requires_greenhouse_hint(self):
        assert extract_greenhouse_params_guess(
            "https://www.dropbox.jobs/en/jobs/7416012/"
        ) is None

    def test_skips_greenhouse_hosts(self):
        assert extract_greenhouse_params_guess(
            "https://boards.greenhouse.io/instacart/jobs/12345?gh_src=x"
        ) is None


class TestGreenhouseHtmlDetection:
    def test_iframe_embed(self):
        html = (
            '<html><body><iframe src="https://boards.greenhouse.io/embed/job_app'
            '?for=instacart&amp;token=7627450"></iframe></body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        assert is_greenhouse_html(soup)
        assert extract_greenhouse_params_from_html(soup) == ("instacart", "7627450")

    def test_div_embed(self):
        html = '<html><body><div id="grnhse_app" data-src="acme" data-token="42"></div></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert is_greenhouse_html(soup)
        assert extract_greenhouse_params_from_html(soup) == ("acme", "42")

    def test_no_markers(self):
        html = "<html><body><p>plain</p></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert not is_greenhouse_html(soup)


class TestRenderGreenhouseJob:
    def _sample(self) -> dict:
        return {
            "title": "Staff Engineer",
            "company_name": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
            "id": "42",
            "requisition_id": "R-42",
            "first_published": "2026-01-15T00:00:00-05:00",
            "updated_at": "2026-02-20T00:00:00-05:00",
            "location": {"name": "San Francisco"},
            "offices": [{"name": "San Francisco"}, {"name": "Remote"}],
            "departments": [{"name": "Engineering"}],
            "education": "education_required",
            "employment": "employment_optional",
            "metadata": [
                {"name": "Employment Type", "value": "Regular", "value_type": "single_select"},
                {"name": "Time Type", "value": "Full time", "value_type": "single_select"},
                {"name": "PERM Job?", "value": False, "value_type": "yes_no"},
            ],
            "content": "&lt;p&gt;&lt;strong&gt;We build things.&lt;/strong&gt;&lt;/p&gt;",
            "questions": [
                {
                    "label": "First Name", "required": True,
                    "fields": [{"name": "first_name", "type": "input_text", "values": []}],
                },
                {
                    "label": "Are you legally authorized?", "required": True,
                    "fields": [{
                        "name": "q1",
                        "type": "multi_value_single_select",
                        "values": [{"label": "Yes"}, {"label": "No"}],
                    }],
                },
            ],
            "location_questions": [
                {"label": "Longitude", "required": True,
                 "fields": [{"name": "longitude", "type": "input_hidden", "values": []}]},
                {"label": "Location", "required": True,
                 "fields": [{"name": "location", "type": "input_text", "values": []}]},
            ],
            "demographic_questions": {
                "header": "<p>Voluntary Self-ID</p>",
                "description": "<p>Optional.</p>",
                "questions": [
                    {"label": "Gender?", "required": True,
                     "type": "multi_value_single_select",
                     "answer_options": [{"label": "Woman"}, {"label": "Man"}]},
                ],
            },
            "data_compliance": [{"type": "gdpr", "requires_consent": True}],
        }

    def test_preserves_raw_fields_and_values(self):
        md = render_greenhouse_job(self._sample(), source_url="https://acme.com/job?id=42")
        # Title/header
        assert md.startswith("# Staff Engineer @ Acme")
        # Raw Greenhouse keys (not renamed).
        assert "**first_published**: 2026-01-15T00:00:00-05:00" in md
        assert "**updated_at**: 2026-02-20T00:00:00-05:00" in md
        assert "**requisition_id**: R-42" in md
        assert "**location**: San Francisco" in md
        # Raw metadata entries preserved using their company-provided names.
        assert "**metadata.Employment Type**: Regular" in md
        assert "**metadata.Time Type**: Full time" in md
        # Boolean false rendered, not dropped.
        assert "**metadata.PERM Job?**: false" in md
        # Raw enum flag preserved.
        assert "**education**: education_required" in md
        assert "**employment**: employment_optional" in md
        # Description HTML rendered
        assert "**We build things.**" in md
        # Questions with raw types
        assert "**First Name**" in md and "input_text" in md
        assert "**Are you legally authorized?**" in md
        assert "multi_value_single_select" in md
        assert "Yes" in md and "No" in md
        # Location field: visible one kept (we include all questions; hidden
        # ones still appear with their type label).
        assert "**Location**" in md
        # Demographic: Greenhouse's own header preserved.
        assert "Voluntary Self-ID" in md
        assert "Optional." in md
        assert "Gender?" in md and "Woman" in md and "Man" in md
        # Compliance: raw dict dump
        assert "gdpr" in md and "requires_consent" in md
        # Apply URL
        assert "**sourceUrl**: https://acme.com/job?id=42" in md

    def test_falls_back_to_absolute_url(self):
        md = render_greenhouse_job(self._sample(), source_url=None)
        assert "**sourceUrl**: https://boards.greenhouse.io/acme/jobs/42" in md
