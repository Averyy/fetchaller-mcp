"""Unit tests for Ashby job board content module.

These tests verify that the content from ``__appData.posting`` is
preserved — raw field names, every form field, every option.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import wafer
from bs4 import BeautifulSoup

from fetchaller.content.ashby import (
    _MARKER,
    BOARD_MAX_RESPONSE_BYTES,
    AshbyBoardTooLargeError,
    _extract_ashby_jid,
    _extract_org_slug_from_js,
    _find_careers_chunk_url,
    extract_ashby_board_slug,
    extract_ashby_data,
    fetch_ashby_board,
    is_ashby,
    is_ashby_board_url,
    is_ashby_embed_url,
    postprocess_ashby,
    render_ashby_board,
)
from fetchaller.content.html import _detect_site
from fetchaller.tools.fetch import fetch_url


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


class TestIsAshbyBoardUrl:
    def test_org_root(self):
        assert is_ashby_board_url("https://jobs.ashbyhq.com/openai")

    def test_org_root_with_slash(self):
        assert is_ashby_board_url("https://jobs.ashbyhq.com/openai/")

    def test_org_root_with_query(self):
        # Filtered board view (departmentId filter) is still a board URL
        assert is_ashby_board_url("https://jobs.ashbyhq.com/openai?departmentId=abc")

    def test_posting_not_board(self):
        # Two-segment URLs are postings, not boards
        assert not is_ashby_board_url(
            "https://jobs.ashbyhq.com/openai/eca54d0e-232a-4c3e-bfcc-d6c6add393f5"
        )

    def test_different_host(self):
        assert not is_ashby_board_url("https://example.com/openai")

    def test_extract_slug(self):
        assert extract_ashby_board_slug("https://jobs.ashbyhq.com/openai") == "openai"
        assert extract_ashby_board_slug("https://jobs.ashbyhq.com/ramp/") == "ramp"
        assert extract_ashby_board_slug("https://jobs.ashbyhq.com/openai?x=1") == "openai"
        assert extract_ashby_board_slug("https://jobs.ashbyhq.com/openai/abc") is None


class TestRenderAshbyBoard:
    def test_groups_and_filters_unlisted(self):
        data = {
            "apiVersion": "1",
            "jobs": [
                {
                    "id": "j1",
                    "title": "Research Engineer",
                    "department": "Research",
                    "team": "Alignment",
                    "employmentType": "FullTime",
                    "location": "San Francisco",
                    "isRemote": False,
                    "isListed": True,
                    "jobUrl": "https://jobs.ashbyhq.com/openai/j1",
                },
                {
                    "id": "j2",
                    "title": "Designer",
                    "department": "Design",
                    "team": "Design",
                    "employmentType": "FullTime",
                    "location": "Remote",
                    "isRemote": True,
                    "isListed": True,
                    "jobUrl": "https://jobs.ashbyhq.com/openai/j2",
                },
                {
                    "id": "j3",
                    "title": "Hidden Role",
                    "department": "Research",
                    "isListed": False,  # should be filtered out
                    "jobUrl": "https://jobs.ashbyhq.com/openai/j3",
                },
            ],
        }
        out = render_ashby_board(data, "openai", source_url="https://jobs.ashbyhq.com/openai")
        assert "# openai — Job Board (2 open positions)" in out
        assert "**apiVersion**: 1" in out
        assert "## Research (1)" in out
        assert "## Design (1)" in out
        assert "**Research Engineer**" in out
        assert "San Francisco" in out
        assert "team: Alignment" in out  # team differs from dept
        assert "Remote" in out
        assert "Hidden Role" not in out  # isListed=False filtered
        # Alphabetical: Design < Research
        assert out.index("## Design") < out.index("## Research")

    def test_empty_board(self):
        out = render_ashby_board({"jobs": []}, "freshstart")
        assert "# freshstart — Job Board (0 open positions)" in out

    def test_missing_department(self):
        data = {
            "jobs": [
                {"title": "Stealth", "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/x/1"},
            ],
        }
        out = render_ashby_board(data, "x")
        assert "## Other (1)" in out
        assert "**Stealth**" in out


class TestFetchAshbyBoardBudget:
    """A real board must never degrade into the SPA's title.

    The board endpoint returns every posting's description inline and has no
    pagination, so large boards (openai is ~11.5MB) blow past the 10MB budget
    the other job-board interceptors share. Swallowing that as ``None`` sent the
    caller to the board's HTML, which renders as nothing but ``# <org> Jobs``.
    """

    def test_budget_exceeds_the_shared_interceptor_limit(self):
        assert BOARD_MAX_RESPONSE_BYTES > 10 * 1024 * 1024

    async def test_oversized_board_raises_instead_of_falling_through(self):
        class TooLargeSession:
            async def get(self, url):
                raise wafer.ResponseTooLarge(url, 12_071_757, 10_485_760)

        with pytest.raises(AshbyBoardTooLargeError):
            await fetch_ashby_board("openai", TooLargeSession())

    async def test_other_transport_errors_still_fall_through(self):
        class FailingSession:
            async def get(self, url):
                raise ConnectionError("boom")

        assert await fetch_ashby_board("openai", FailingSession()) is None

    async def test_non_200_still_falls_through(self):
        class NotFoundSession:
            async def get(self, url):
                return SimpleNamespace(status_code=404, text="")

        assert await fetch_ashby_board("nobody", NotFoundSession()) is None

    async def test_fetch_url_reports_oversized_board_instead_of_empty_page(self):
        async def raise_too_large(org, session):
            raise AshbyBoardTooLargeError("too big")

        with patch(
            "fetchaller.tools.fetch.fetch_ashby_board",
            side_effect=raise_too_large,
        ):
            result = await fetch_url(
                "https://jobs.ashbyhq.com/openai",
                timeout=10,
            )

        assert "content" not in result
        assert "openai" in result["error"]
        assert "read limit" in result["error"]


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


def test_every_ashby_board_call_site_handles_the_oversize_error():
    """All ``fetch_ashby_board`` callers must catch ``AshbyBoardTooLargeError``.

    ``fetch_ashby_board`` raises rather than returning ``None`` for an oversized
    board, precisely so the caller cannot fall through and render the SPA
    spinner as an empty job list. There are two call sites -- the direct
    ``jobs.ashbyhq.com/<org>`` route and the ``<script src=".../embed">``
    detection on a company career page -- and only the direct one was guarded,
    so an oversized embedded board escaped as an unhandled exception.

    The embed branch runs behind ``run_isolated`` (a subprocess), so it cannot
    be reached by patching from a test; assert the structural invariant instead.
    """

    import ast
    import pathlib

    source = pathlib.Path("src/fetchaller/tools/fetch.py").read_text()
    tree = ast.parse(source)

    def _calls_board_fetch(node: ast.AST) -> bool:
        return any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "fetch_ashby_board"
            for inner in ast.walk(node)
        )

    guarded_lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_oversize = any(
            handler.type is not None
            and "AshbyBoardTooLargeError" in ast.unparse(handler.type)
            for handler in node.handlers
        )
        if not catches_oversize:
            continue
        for stmt in node.body:
            if _calls_board_fetch(stmt):
                for inner in ast.walk(stmt):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "fetch_ashby_board"
                    ):
                        guarded_lines.add(inner.lineno)

    all_lines = {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "fetch_ashby_board"
    }

    assert len(all_lines) >= 2, "expected the direct and embed call sites"
    assert all_lines == guarded_lines, (
        "unguarded fetch_ashby_board call site(s) at line(s) "
        f"{sorted(all_lines - guarded_lines)}"
    )
