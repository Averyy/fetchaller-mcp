"""Unit tests for Work at a Startup (YC) content module.

These tests verify that the content from the Inertia ``data-page`` JSON
is preserved — raw field names, every section, every related job.
"""

import json
from html import escape

from bs4 import BeautifulSoup

from fetchaller.content.html import _detect_site
from fetchaller.content.workatastartup import (
    _MARKER,
    extract_workatastartup_data,
    is_workatastartup,
    postprocess_workatastartup,
)


class TestIsWorkAtAStartup:
    def test_job_detail(self):
        assert is_workatastartup("https://www.workatastartup.com/jobs/93227")

    def test_job_detail_bare_host(self):
        assert is_workatastartup("https://workatastartup.com/jobs/93227")

    def test_listing_pages_are_on_site(self):
        # What a page *is* comes from its Inertia component, not its path:
        # these used to be excluded here and rendered only their <title>.
        assert is_workatastartup("https://www.workatastartup.com/companies/agave")
        assert is_workatastartup("https://www.workatastartup.com/jobs?role=eng")
        assert is_workatastartup("https://www.workatastartup.com/")

    def test_different_host(self):
        assert not is_workatastartup("https://example.com/jobs/93227")


class TestSiteDetection:
    def test_detected(self):
        assert (
            _detect_site("https://www.workatastartup.com/jobs/93227", False)
            == "workatastartup"
        )


def _build_page_html(props: dict, component: str = "jobs/public/pages/JobDetailPage") -> str:
    payload = {"component": component, "props": props}
    # Inertia puts the JSON in an HTML attribute; BeautifulSoup decodes
    # HTML entities on attribute read, so we escape like the real page does.
    blob = escape(json.dumps(payload), quote=True)
    return f"""<html>
        <head><title>Product Designer at Agave | Y Combinator</title></head>
        <body>
          <div data-page="{blob}"></div>
        </body></html>"""


class TestExtractWaasData:
    def test_preserves_raw_fields_and_sections(self):
        props = {
            "job": {
                "id": 93227,
                "title": "Founding Product Designer",
                "salaryRange": "$125K - $170K",
                "equityRange": "0.05% - 0.20%",
                "location": "San Francisco, CA, US",
                "jobType": "Full-time",
                "sponsorsVisa": "Will sponsor",
                "minExperience": "3+ years",
                "skills": ["Figma", "Design Systems"],
                "descriptionHtml": "<h2>About</h2><p>We design things.</p>",
                "interviewProcessHtml": "<ol><li>Intro</li><li>On-site</li></ol>",
            },
            "company": {
                "name": "Agave",
                "slug": "agave",
                "batch": "W22",
                "description": "AI for construction",
                "hiringDescriptionHtml": "<p>We're hiring</p>",
                "techDescriptionHtml": None,
                "url": "https://useagave.com",
                "location": "San Francisco",
                "teamSize": 33,
                "industry": "B2B",
                "founders": [
                    {
                        "name": "Tom Reno",
                        "bio": "Co-founder & CEO.\nPreviously at Amazon.",
                        "linkedin": "https://linkedin.com/in/tomreno/",
                        "avatarUrl": "https://example.com/a.jpg",
                    }
                ],
                "jobs": [
                    {
                        "id": 65427,
                        "title": "Technical PM",
                        "location": "SF",
                        "jobType": "Full-time",
                        "salaryRange": "$130K",
                    }
                ],
            },
            "applyUrl": "https://account.ycombinator.com/apply?job=93227",
            "signupUrl": "https://account.ycombinator.com/signup",
            "otherJobs": [
                {"id": 65427, "title": "Technical PM", "location": "SF",
                 "jobType": "Full-time"}
            ],
        }
        html = _build_page_html(props)
        soup = BeautifulSoup(html, "lxml")
        extract_workatastartup_data(
            soup, "https://www.workatastartup.com/jobs/93227"
        )

        marker = soup.find(id="waas-marker")
        assert marker is not None
        text = marker.string
        assert text.startswith(_MARKER)
        md = postprocess_workatastartup(text)

        # Header: job title @ company name.
        assert "# Founding Product Designer @ Agave" in md

        # Apply URL rendered at the top for easy access.
        assert "**applyUrl**: https://account.ycombinator.com/apply?job=93227" in md
        assert "**signupUrl**: https://account.ycombinator.com/signup" in md

        # Raw field names preserved (not translated into prettier labels).
        assert "**id**: 93227" in md
        assert "**salaryRange**: $125K - $170K" in md
        assert "**equityRange**: 0.05% - 0.20%" in md
        assert "**jobType**: Full-time" in md
        assert "**sponsorsVisa**: Will sponsor" in md
        assert "**minExperience**: 3+ years" in md
        assert "**skills**: Figma; Design Systems" in md

        # Company fields.
        assert "**batch**: W22" in md
        assert "**teamSize**: 33" in md
        assert "**industry**: B2B" in md

        # Description HTML converted.
        assert "## descriptionHtml" in md
        assert "We design things." in md

        # Interview process rendered as its own section.
        assert "## interviewProcessHtml" in md

        # Hiring description rendered.
        assert "## company.hiringDescriptionHtml" in md
        assert "We're hiring" in md

        # Founders.
        assert "## company.founders" in md
        assert "**Tom Reno**" in md
        assert "Co-founder & CEO." in md
        assert "Previously at Amazon." in md
        assert "https://linkedin.com/in/tomreno/" in md

        # Related jobs at the same company.
        assert "## company.jobs" in md
        assert "Technical PM" in md
        assert "https://www.workatastartup.com/jobs/65427" in md

        # otherJobs rendered as its own section.
        assert "## otherJobs" in md

        # Source URL footer.
        assert "**sourceUrl**: https://www.workatastartup.com/jobs/93227" in md

    def test_missing_optional_sections_are_omitted(self):
        props = {
            "job": {
                "id": 1,
                "title": "Engineer",
                "descriptionHtml": "<p>Body</p>",
            },
            "company": {"name": "Acme"},
            "applyUrl": "",
            "signupUrl": "",
            "otherJobs": [],
        }
        html = _build_page_html(props)
        soup = BeautifulSoup(html, "lxml")
        extract_workatastartup_data(
            soup, "https://www.workatastartup.com/jobs/1"
        )

        marker = soup.find(id="waas-marker")
        md = postprocess_workatastartup(marker.string)

        assert "# Engineer @ Acme" in md
        assert "## descriptionHtml" in md
        # These optional sections should not appear at all.
        assert "## interviewProcessHtml" not in md
        assert "## company.hiringDescriptionHtml" not in md
        assert "## company.founders" not in md
        assert "## otherJobs" not in md
        assert "**applyUrl**" not in md

    def test_no_data_page_is_noop(self):
        soup = BeautifulSoup("<html><body><p>hi</p></body></html>", "lxml")
        extract_workatastartup_data(soup, "https://www.workatastartup.com/jobs/1")
        # Body untouched when no data-page present.
        assert soup.find(id="waas-marker") is None
        assert soup.find("p").string == "hi"


class TestPostprocessNoMarker:
    def test_passthrough_when_no_marker(self):
        md = "# Something\n\nNo marker here.\n"
        assert postprocess_workatastartup(md) == md


def _render(props: dict, component: str, url: str) -> str:
    soup = BeautifulSoup(_build_page_html(props, component), "html.parser")
    extract_workatastartup_data(soup, url)
    return postprocess_workatastartup(soup.get_text())


class TestListingPages:
    """Measured 2026-10-05: /jobs rendered 86 chars for a page carrying 30 of
    2,893 jobs, a company page 62 chars. Each listing component is read now."""

    def test_job_list_page(self):
        props = {
            "title": "Software Engineer jobs at Y Combinator startups",
            "totalJobsCount": 2893,
            "currentRole": "eng",
            "jobs": [
                {
                    "id": 53662,
                    "title": "Full Stack Developer - Bountiful ",
                    "jobType": "Full-time",
                    "location": "San Francisco, CA, US",
                    "roleType": "Full stack",
                    "salary": "$185K - $210K",
                    "companyName": "Bountiful",
                    "companySlug": "bountiful",
                    "companyBatch": "W17",
                    "companyLogoUrl": "https://example.test/logo.png",
                    "applyUrl": "https://account.ycombinator.com/authenticate?x",
                }
            ],
            "roleLinks": [{"label": "Design", "path": "/jobs/l/designer"}],
            "nav": {"noise": True},
        }
        out = _render(props, "jobs/public/pages/JobsV2Page", "https://www.workatastartup.com/jobs")
        assert out.startswith("# Software Engineer jobs at Y Combinator startups")
        assert "**totalJobsCount**: 2,893" in out
        assert "**shown**: 1" in out
        assert "**Full Stack Developer - Bountiful** — Full-time — San Francisco, CA, US" in out
        assert "$185K - $210K — Bountiful — W17" in out
        assert "https://www.workatastartup.com/jobs/53662 · https://www.workatastartup.com/companies/bountiful" in out
        assert "- Design: https://www.workatastartup.com/jobs/l/designer" in out
        assert "logo.png" not in out and "authenticate" not in out and "noise" not in out

    def test_company_page(self):
        props = {
            "company": {
                "name": "Bountiful",
                "slug": "bountiful",
                "batch": "W17",
                "description": "An Operating System for agriculture.",
                "hiringDescriptionHtml": "<p>We hire builders.</p>",
                "founders": [{"name": "Ada", "bio": "Farmer"}],
                "jobs": [{"id": 53662, "title": "Full Stack Developer", "location": "SF"}],
                "logoUrl": "https://example.test/logo.png",
            },
            "otherCompanies": [{"name": "Albedo", "slug": "albedo"}],
        }
        out = _render(props, "jobs/public/pages/CompanyV2Page", "https://www.workatastartup.com/companies/bountiful")
        assert out.startswith("# Bountiful (W17)")
        assert "- **description**: An Operating System for agriculture." in out
        assert "We hire builders." in out
        assert "**Ada**" in out
        assert "- Full Stack Developer — SF" in out and "/jobs/53662" in out
        assert "- Albedo — https://www.workatastartup.com/companies/albedo" in out

    def test_home_page_lists_jobs_and_hiring_companies_without_marketing(self):
        props = {
            "jobs": [{"id": 1, "title": "Engineer", "companyName": "Acme", "companySlug": "acme"}],
            "hiringCompanies": [{"name": "Albedo", "path": "/companies/albedo"}],
            "slideshow": [{"big": "marketing"}],
            "news": [{"headline": "press"}],
        }
        out = _render(props, "jobs/public/pages/HomeV2Page", "https://www.workatastartup.com/")
        assert "**Engineer** — Acme" in out
        assert "- Albedo — https://www.workatastartup.com/companies/albedo" in out
        assert "marketing" not in out and "press" not in out

    def test_unknown_component_is_left_to_the_html_path(self):
        html = _build_page_html({"something": "else"}, "jobs/public/pages/SomethingNew")
        soup = BeautifulSoup(html, "html.parser")
        extract_workatastartup_data(soup, "https://www.workatastartup.com/x")
        assert _MARKER not in str(soup)
