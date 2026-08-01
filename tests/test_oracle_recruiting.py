"""Oracle Recruiting Cloud: finder construction, location semantics, rendering.

Two ORC behaviours are pinned here because both fail *silently* rather than
erroring, which is the dangerous kind:

- Omitting ``expand=requisitionList`` still returns HTTP 200 with a correct
  ``TotalJobsCount`` but no postings at all.
- The ``location`` finder filters on country names and ignores city names, so
  ``location="Toronto"`` returns the entire global board while looking like a
  filter.
"""

from fetchaller.oracle_recruiting import api, search
from fetchaller.oracle_recruiting.employers import KNOWN_EMPLOYERS, resolve_employer


class TestFinder:
    def test_minimal(self):
        assert api._finder("CX_1", limit=20, offset=0, keyword="", location="") == (
            "findReqs;siteNumber=CX_1,limit=20"
        )

    def test_offset_only_when_nonzero(self):
        assert "offset" not in api._finder("CX_1", limit=20, offset=0, keyword="", location="")
        assert "offset=200" in api._finder("CX_1", limit=20, offset=200, keyword="", location="")

    def test_keyword_and_location_are_quoted(self):
        finder = api._finder("CX_1", limit=5, offset=0, keyword="designer", location="Canada")
        assert 'keyword="designer"' in finder
        assert 'location="Canada"' in finder

    def test_values_are_url_encoded_but_syntax_is_not(self):
        finder = api._finder(
            "CX_1", limit=5, offset=0, keyword="product designer", location="Toronto, ON"
        )
        # The finder's own ; and , must survive as syntax.
        assert finder.startswith("findReqs;siteNumber=CX_1,")
        assert "product%20designer" in finder
        assert "%2C" in finder  # the comma inside the location value is encoded

    def test_custom_site_number(self):
        assert "siteNumber=CX_2" in api._finder(
            "CX_2", limit=5, offset=0, keyword="", location=""
        )


class TestServerLocation:
    def test_country_is_passed_through(self):
        assert search._server_location("Canada") == "Canada"

    def test_city_is_withheld_because_orc_ignores_it(self):
        # Sending it would look like a filter and silently return everything.
        assert search._server_location("Toronto") == ""

    def test_country_extracted_from_a_composite_location(self):
        assert search._server_location("Toronto, Canada") == "Canada"
        assert search._server_location("Vancouver, British Columbia, Canada") == "Canada"

    def test_unknown_country_withheld(self):
        assert search._server_location("Narnia") == ""

    def test_empty(self):
        assert search._server_location("") == ""


class TestEmployerResolution:
    def test_alias(self):
        record = resolve_employer("uber")
        assert record is KNOWN_EMPLOYERS["uber"]
        assert record.careers_url.startswith("https://jobs.uber.com")

    def test_fusion_host(self):
        record = resolve_employer("https://acme.fa.us2.oraclecloud.com")
        assert record is not None
        assert record.fallback_host == "https://acme.fa.us2.oraclecloud.com"

    def test_fusion_host_strips_rest_path(self):
        record = resolve_employer(
            "https://acme.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/x"
        )
        assert record.fallback_host == "https://acme.fa.us2.oraclecloud.com"

    def test_arbitrary_careers_url_defers_to_discovery(self):
        record = resolve_employer("https://careers.example.com/jobs")
        assert record is not None
        assert record.fallback_host is None

    def test_unknown_returns_none(self):
        assert resolve_employer("") is None
        assert resolve_employer("not a board") is None

    def test_default_site_number(self):
        assert KNOWN_EMPLOYERS["uber"].site_number == api.DEFAULT_SITE_NUMBER


class TestHostDiscovery:
    def test_regex_matches_a_fusion_host(self):
        html = 'x = "https://iaziqy.fa.ocs.oraclecloud.com/hcmRestApi/resources";'
        assert api._FUSION_HOST_RE.search(html).group(0) == (
            "https://iaziqy.fa.ocs.oraclecloud.com"
        )

    def test_regex_ignores_unrelated_oracle_urls(self):
        assert api._FUSION_HOST_RE.search("https://www.oracle.com/careers") is None


_JOB = {
    "Id": "301041",
    "Title": "Senior Product Designer - Uber Direct & Connect",
    "PrimaryLocation": "New York City, NY, United States",
    "Department": "Design",
    "JobFamily": "Product Design",
    "PostedDate": "2026-07-29T20:02:19+00:00",
    "ShortDescriptionStr": "Design for on-demand delivery.",
}


class TestRender:
    def test_search_results_link_to_the_public_posting(self):
        out = search._render_results(
            [_JOB],
            employer=KNOWN_EMPLOYERS["uber"],
            title="product designer",
            location="",
            total=203,
            title_filtered=13,
            location_filtered=0,
        )
        assert "# Uber jobs" in out
        assert "https://jobs.uber.com/en/jobs/301041" in out
        assert "New York City, NY, United States" in out
        assert "13 by title" in out

    def test_secondary_locations_are_merged(self):
        job = dict(_JOB, secondaryLocations=[{"Name": "Toronto, ON, Canada"}])
        assert "Toronto, ON, Canada" in search._locations(job)

    def test_duplicate_locations_collapse(self):
        job = dict(_JOB, secondaryLocations=[{"Name": "New York City, NY, United States"}])
        assert search._locations(job).count("New York City") == 1

    def test_detail_renders_every_body_section(self):
        job = dict(
            _JOB,
            ExternalDescriptionStr="<p>Body</p>",
            ExternalResponsibilitiesStr="<p>Do things</p>",
            ExternalQualificationsStr="<p>Have skills</p>",
        )
        out = search._render_job(job, employer=KNOWN_EMPLOYERS["uber"])
        assert "## Description" in out
        assert "## Responsibilities" in out
        assert "## Qualifications" in out
        assert "**Source**: https://jobs.uber.com/en/jobs/301041" in out

    def test_markdown_metacharacters_escaped(self):
        out = search._render_results(
            [dict(_JOB, Title="Designer [L5] *Remote*")],
            employer=KNOWN_EMPLOYERS["uber"],
            title="",
            location="",
            total=1,
            title_filtered=0,
            location_filtered=0,
        )
        assert "\\[L5\\]" in out
