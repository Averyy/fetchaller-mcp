"""Unit tests for Ashby job board content module.

These tests verify that the content from ``__appData.posting`` is
preserved — raw field names, every form field, every option.
"""

import json

from bs4 import BeautifulSoup

from fetchaller.content.ashby import (
    _MARKER,
    _extract_ashby_jid,
    _extract_org_slug_from_js,
    _find_careers_chunk_url,
    extract_ashby_data,
    is_ashby,
    is_ashby_embed_url,
    postprocess_ashby,
)
from fetchaller.content.html import _detect_site


class TestIsAshby:
    def test_ashby_posting(self):
        assert is_ashby(
            "https://jobs.ashbyhq.com/ramp/eca54d0e-232a-4c3e-bfcc-d6c6add393f5"
        )

    def test_ashby_org_root(self):
        assert is_ashby("https://jobs.ashbyhq.com/ramp")

    def test_not_ashby_main_site(self):
        assert not is_ashby("https://www.ashbyhq.com/")

    def test_not_ashby_different_host(self):
        assert not is_ashby("https://example.com/jobs/ashbyhq")


class TestSiteDetection:
    def test_ashby_detected(self):
        assert _detect_site("https://jobs.ashbyhq.com/ramp/abc", False) == "ashby"


def _build_app_data_html(posting: dict, organization: dict | None = None) -> str:
    payload = {
        "organization": organization or {"name": "Ramp", "publicWebsite": "https://ramp.com"},
        "posting": posting,
    }
    blob = json.dumps(payload)
    return f"""<html>
        <head><title>Product Designer @ Ramp</title></head>
        <body>
          <div id="root"></div>
          <script>window.__appData = {blob};</script>
        </body></html>"""


class TestExtractAshbyData:
    def test_preserves_raw_field_names_and_all_form_fields(self):
        posting = {
            "title": "Product Designer",
            "departmentExternalName": "Design",
            "teamNames": ["Design"],
            "locationExternalName": "New York, NY (HQ)",
            "secondaryLocationNames": ["San Francisco, CA", "Remote (US)"],
            "workplaceType": "Hybrid",
            "isRemote": True,
            "employmentType": "FullTime",
            "publishedDate": "2026-02-09",
            "descriptionHtml": "<h1>About</h1><p>We build things.</p>",
            "compensationTierSummary": "$172K – $440K",
            "applicationForm": {
                "sections": [
                    {
                        "title": "Your Information",
                        "fieldEntries": [
                            {"field": {"title": "Legal Name", "type": "String"},
                             "isRequired": True},
                            {"field": {"title": "Cover Letter", "type": "File"},
                             "isRequired": False},
                        ],
                    }
                ]
            },
        }
        html = _build_app_data_html(posting)
        soup = BeautifulSoup(html, "lxml")
        extract_ashby_data(soup, "https://jobs.ashbyhq.com/ramp/abc")

        marker = soup.find(id="ashby-marker")
        assert marker is not None
        text = marker.string
        assert text.startswith(_MARKER)
        md = postprocess_ashby(text)

        # Header: title + org.
        assert "# Product Designer @ Ramp" in md
        # Raw field names preserved (not translated into my prettier labels).
        assert "**departmentExternalName**: Design" in md
        assert "**workplaceType**: Hybrid" in md
        assert "**employmentType**: FullTime" in md  # NOT translated to "Full-time"
        assert "**publishedDate**: 2026-02-09" in md
        assert "**compensationTierSummary**: $172K – $440K" in md
        # Company anchor line uses the canonical name from the org object.
        assert "**company**: Ramp" in md
        # Secondary locations rendered (all preserved).
        assert "New York, NY (HQ)" in md
        assert "San Francisco, CA" in md
        assert "Remote (US)" in md
        # Description HTML kept (heading preserved at original level).
        assert "# About" in md or "About" in md
        assert "We build things." in md
        # Form section title preserved.
        assert "### Your Information" in md
        # Every field + its raw type preserved.
        assert "Legal Name" in md and "String" in md
        assert "Cover Letter" in md and "File" in md
        # Source URL.
        assert "**sourceUrl**: https://jobs.ashbyhq.com/ramp/abc" in md

    def test_renders_survey_forms_and_limit_callout(self):
        posting = {
            "title": "Designer",
            "descriptionHtml": "<p>Desc</p>",
            "applicationLimitCalloutHtml": (
                "<p>Candidates may not apply more than 3 times.</p>"
            ),
            "shouldAskForTextingConsent": True,
            "applicationForm": {
                "sections": [{"fieldEntries": [
                    {"field": {"title": "Name", "type": "String"}, "isRequired": True},
                ]}]
            },
            "surveyForms": [{
                "sections": [{
                    "title": "Diversity Survey",
                    "fieldEntries": [{
                        "field": {
                            "title": "Age?",
                            "type": "ValueSelect",
                            "selectableValues": [{"label": "Under 30"}, {"label": "30-39"}],
                        },
                        "isRequired": False,
                    }],
                }],
            }],
        }
        html = _build_app_data_html(posting)
        soup = BeautifulSoup(html, "lxml")
        extract_ashby_data(soup, "https://jobs.ashbyhq.com/ramp/abc")
        md = postprocess_ashby(soup.find(id="ashby-marker").string)

        assert "Candidates may not apply more than 3 times." in md
        assert "**shouldAskForTextingConsent**: true" in md  # raw value
        assert "### Diversity Survey" in md  # company-provided section title
        assert "Age?" in md
        assert "ValueSelect" in md  # raw type label
        assert "Under 30" in md and "30-39" in md

    def test_falls_back_to_jsonld(self):
        html = """<html><head><title>Job</title></head><body>
          <script type="application/ld+json">{
            "@type": "JobPosting",
            "title": "Engineer",
            "description": "<p>Build stuff</p>",
            "hiringOrganization": {"@type": "Organization", "name": "Acme"},
            "employmentType": "FULL_TIME",
            "datePosted": "2026-01-01"
          }</script>
        </body></html>"""
        soup = BeautifulSoup(html, "lxml")
        extract_ashby_data(soup, "https://jobs.ashbyhq.com/acme/x")
        marker = soup.find(id="ashby-marker")
        assert marker is not None
        md = postprocess_ashby(marker.string)
        assert "# Engineer" in md
        # Raw JSON-LD field names preserved.
        assert "**employmentType**: FULL_TIME" in md
        assert "**datePosted**: 2026-01-01" in md
        assert "Build stuff" in md

    def test_no_data_leaves_soup_alone(self):
        html = "<html><body><p>hello</p></body></html>"
        soup = BeautifulSoup(html, "lxml")
        extract_ashby_data(soup, "https://jobs.ashbyhq.com/x/y")
        assert soup.find(id="ashby-marker") is None
        assert soup.find("p") is not None


class TestPostprocessAshby:
    def test_extracts_marker_content(self):
        md = f"# Stale\n\n{_MARKER}# Clean\n\nbody{_MARKER}\n\ntrailing"
        out = postprocess_ashby(md)
        assert out.startswith("# Clean")
        assert "Stale" not in out
        assert "trailing" not in out

    def test_no_marker_returns_unchanged(self):
        md = "# Regular\n\nbody"
        assert postprocess_ashby(md) == md


class TestIsAshbyEmbedUrl:
    def test_marketing_site_with_jid(self):
        url = (
            "https://www.ashbyhq.com/careers"
            "?ashby_jid=2373fcd5-144b-4d66-a98b-dd0efb4eb9d1"
        )
        assert is_ashby_embed_url(url)

    def test_without_jid(self):
        assert not is_ashby_embed_url("https://www.ashbyhq.com/careers")

    def test_invalid_jid(self):
        assert not is_ashby_embed_url("https://www.ashbyhq.com/careers?ashby_jid=nope")

    def test_extract_jid(self):
        assert _extract_ashby_jid(
            "https://x.com/?ashby_jid=2373fcd5-144b-4d66-a98b-dd0efb4eb9d1"
        ) == "2373fcd5-144b-4d66-a98b-dd0efb4eb9d1"


class TestEmbedResolutionHelpers:
    def test_finds_careers_chunk(self):
        html = '<script src="/_next/static/chunks/pages/careers-abc.js"></script>'
        assert _find_careers_chunk_url(html, "https://www.ashbyhq.com/careers") == (
            "https://www.ashbyhq.com/_next/static/chunks/pages/careers-abc.js"
        )

    def test_extracts_org_slug(self):
        js = 'var x="https://jobs.ashbyhq.com/ashby/embed";'
        assert _extract_org_slug_from_js(js) == "ashby"

    def test_skips_non_slug_paths(self):
        js = 'fetch("https://jobs.ashbyhq.com/api/foo"); var y="jobs.ashbyhq.com/linear-inc/embed";'
        assert _extract_org_slug_from_js(js) == "linear-inc"

    def test_no_slug_returns_none(self):
        assert _extract_org_slug_from_js("unrelated JS") is None
