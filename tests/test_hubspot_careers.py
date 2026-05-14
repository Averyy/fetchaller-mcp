"""Unit tests for HubSpot careers content module.

The HubSpot career site is a JS SPA backed by a single GraphQL endpoint at
``wtcfns.hubspot.com/careers/graphql``. URLs carry a ``?gh_jid=`` parameter
that's HubSpot's own job id (not Greenhouse's), so dispatch must intercept
before the greenhouse hostname guess.
"""

from fetchaller.content.hubspot_careers import (
    extract_hubspot_job_id,
    is_hubspot_careers_url,
    render_hubspot_job,
)


class TestIsHubspotCareersUrl:
    def test_canonical_path(self):
        assert is_hubspot_careers_url("https://www.hubspot.com/careers/jobs/7530523")

    def test_with_vestigial_gh_jid(self):
        assert is_hubspot_careers_url(
            "https://www.hubspot.com/careers/jobs/7530523?gh_jid=7530523"
        )

    def test_apex_domain(self):
        assert is_hubspot_careers_url("https://hubspot.com/careers/jobs/7530523")

    def test_trailing_slash(self):
        assert is_hubspot_careers_url("https://www.hubspot.com/careers/jobs/7530523/")

    def test_careers_index_not_a_job(self):
        assert not is_hubspot_careers_url("https://www.hubspot.com/careers/")

    def test_non_careers_path(self):
        assert not is_hubspot_careers_url("https://www.hubspot.com/products")

    def test_other_host(self):
        assert not is_hubspot_careers_url("https://example.com/careers/jobs/7530523")

    def test_non_numeric_id(self):
        assert not is_hubspot_careers_url("https://www.hubspot.com/careers/jobs/abc")


class TestExtractHubspotJobId:
    def test_basic(self):
        assert extract_hubspot_job_id(
            "https://www.hubspot.com/careers/jobs/7530523?gh_jid=7530523"
        ) == "7530523"

    def test_no_query(self):
        assert extract_hubspot_job_id(
            "https://www.hubspot.com/careers/jobs/12345"
        ) == "12345"

    def test_other_host_returns_none(self):
        assert extract_hubspot_job_id("https://example.com/careers/jobs/12345") is None


class TestRenderHubspotJob:
    def _sample(self) -> dict:
        return {
            "title": "Staff Product Designer",
            "id": 7530523,
            # GraphQL ``content`` is double-encoded — entity refs inside an
            # HTML string. The renderer must unescape before markdownify.
            "content": (
                "&lt;p&gt;&lt;strong&gt;POS-29684&lt;/strong&gt;&lt;/p&gt;\n"
                "&lt;h2&gt;Why Flywheel?&lt;/h2&gt;\n"
                "&lt;ul&gt;&lt;li&gt;Shape AI.&lt;/li&gt;&lt;/ul&gt;"
            ),
            "department": {"id": 64162, "name": "User Experience"},
            "office": {"location": "Remote - Ontario, Canada", "id": 80765},
            "questions": [
                {
                    "label": "First Name",
                    "description": None,
                    "required": True,
                    "fields": [{"name": "first_name", "type": "input_text", "values": []}],
                },
                {
                    "label": "Do you have authorization?",
                    "description": None,
                    "required": True,
                    "fields": [{
                        "name": "q1",
                        "type": "multi_value_single_select",
                        "values": [{"value": 1, "label": "Yes"}, {"value": 0, "label": "No"}],
                    }],
                },
                {
                    # ``description`` here is single-encoded HTML (not double).
                    "label": "Voluntary Equal Opportunity Employment",
                    "description": "<p>Completion is voluntary.</p>",
                    "required": True,
                    "fields": [{
                        "name": "q2",
                        "type": "multi_value_single_select",
                        "values": [{"value": 1, "label": "Decline"}],
                    }],
                },
            ],
            "location_questions": [
                {
                    "label": "Location",
                    "required": True,
                    "fields": [{"name": "location", "type": "input_text"}],
                },
            ],
        }

    def test_renders_title_and_metadata(self):
        md = render_hubspot_job(
            self._sample(),
            source_url="https://www.hubspot.com/careers/jobs/7530523?gh_jid=7530523",
        )
        assert md.startswith("# Staff Product Designer")
        assert "**id**: 7530523" in md
        assert "**department**: User Experience" in md
        assert "**location**: Remote - Ontario, Canada" in md

    def test_decodes_double_encoded_content(self):
        md = render_hubspot_job(self._sample(), source_url=None)
        # Should not contain leftover entity refs from the double-encoded payload.
        assert "&lt;" not in md
        assert "&gt;" not in md
        # Markdownified content present.
        assert "**POS-29684**" in md
        assert "## Why Flywheel?" in md
        assert "- Shape AI." in md

    def test_decodes_single_encoded_question_description(self):
        md = render_hubspot_job(self._sample(), source_url=None)
        assert "Completion is voluntary." in md

    def test_renders_questions_with_types_and_options(self):
        md = render_hubspot_job(self._sample(), source_url=None)
        assert "## questions" in md
        assert "**First Name**" in md and "input_text" in md
        assert "**Do you have authorization?**" in md
        assert "multi_value_single_select" in md
        assert "Options: Yes, No" in md

    def test_renders_location_questions(self):
        md = render_hubspot_job(self._sample(), source_url=None)
        assert "## location_questions" in md
        assert "**Location**" in md

    def test_source_url_prefers_passed_value(self):
        md = render_hubspot_job(
            self._sample(),
            source_url="https://www.hubspot.com/careers/jobs/7530523?gh_jid=7530523",
        )
        assert (
            "**sourceUrl**: https://www.hubspot.com/careers/jobs/7530523?gh_jid=7530523"
            in md
        )

    def test_source_url_falls_back_to_canonical(self):
        md = render_hubspot_job(self._sample(), source_url=None)
        assert "**sourceUrl**: https://www.hubspot.com/careers/jobs/7530523" in md

    def test_handles_missing_optional_fields(self):
        data = {"title": "Bare Posting"}
        md = render_hubspot_job(data, source_url=None)
        assert md.startswith("# Bare Posting")
        # No metadata/sections should be emitted, but it shouldn't crash.
        assert "## questions" not in md
        assert "## location_questions" not in md
