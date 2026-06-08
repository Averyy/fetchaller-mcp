"""URL detection, Apollo helpers, and rendering unit tests for wellfound.com."""

from fetchaller.wellfound import api, render
from fetchaller.wellfound.page import _search_title

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_job(self):
        u = "https://wellfound.com/jobs/4265927-software-engineer"
        assert api.is_wellfound(u)
        assert api.is_wellfound_job(u)
        assert not api.is_wellfound_search(u)
        assert api.extract_job_id(u) == "4265927"

    def test_jobs_feed_is_search_not_job(self):
        u = "https://wellfound.com/jobs"
        assert api.is_wellfound_search(u)
        assert not api.is_wellfound_job(u)
        assert api.extract_job_id(u) is None

    def test_role_and_location_search(self):
        for u in [
            "https://wellfound.com/role/r/software-engineer",
            "https://wellfound.com/role/l/product-manager/new-york",
            "https://wellfound.com/location/austin",
        ]:
            assert api.is_wellfound_search(u)

    def test_company(self):
        u = "https://wellfound.com/company/fayco"
        assert api.is_wellfound_company(u)
        assert not api.is_wellfound_search(u)

    def test_rejects_non_wellfound(self):
        for u in ["https://angel.co/jobs", "https://example.com/company/x"]:
            assert not api.is_wellfound(u)


# ---------------------------------------------------------------------------
# Apollo cache helpers
# ---------------------------------------------------------------------------

_CACHE = {
    "JobListing:1": {"__typename": "JobListing", "id": "1", "slug": "swe", "title": "SWE",
                     "startup": {"__ref": "Startup:9"}},
    "Startup:9": {"__typename": "Startup", "id": "9", "name": "Acme",
                  "conn({\"first\":2})": {"edges": [{"node": {"__ref": "JobListing:1"}}]}},
}


class TestApolloHelpers:
    def test_deref_single(self):
        assert api.deref(_CACHE, {"__ref": "Startup:9"})["name"] == "Acme"

    def test_deref_list(self):
        out = api.deref(_CACHE, [{"__ref": "JobListing:1"}, {"__ref": "Startup:9"}])
        assert [o["__typename"] for o in out] == ["JobListing", "Startup"]

    def test_deref_passthrough(self):
        assert api.deref(_CACHE, {"x": 1}) == {"x": 1}

    def test_entities(self):
        assert len(api.entities(_CACHE, "JobListing")) == 1
        assert api.entities(_CACHE, "Nope") == []

    def test_connection_with_args(self):
        startup = _CACHE["Startup:9"]
        conn = api.connection(startup, "conn")
        assert "edges" in conn
        nodes = api.connection_nodes(_CACHE, startup, "conn")
        assert nodes[0]["title"] == "SWE"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    def test_money(self):
        assert render._money(4600000) == "$4.6M"
        assert render._money(1500000000) == "$1.5B"
        assert render._money(50000) == "$50K"
        assert render._money(0) == ""
        assert render._money(None) == ""

    def test_company_size(self):
        assert render._company_size("SIZE_51_200") == "51-200 employees"
        assert render._company_size("SIZE_10000_PLUS") == "10000+ employees"
        assert render._company_size(None) == ""

    def test_date(self):
        assert render._date(1661904000) == "Aug 31, 2022"
        assert render._date(None) == ""

    def test_clean_url(self):
        assert render._clean_url("twitter.com/https://x.com/acme") == "https://x.com/acme"
        assert render._clean_url("twitter.com/acme") == "https://twitter.com/acme"
        assert render._clean_url("https://x.com/a") == "https://x.com/a"
        assert render._clean_url("") == ""

    def test_salary_handles_decimal_strings(self):
        # JSON-LD QuantitativeValue bounds can be "120000.0" — int() would raise.
        base = {"currency": "USD",
                "value": {"minValue": "120000.0", "maxValue": "150000.0", "unitText": "YEAR"}}
        assert render._salary(base) == "USD 120,000–150,000/yr"

    def test_salary_only_max_and_garbage(self):
        assert render._salary({"currency": "USD", "value": {"maxValue": 90000}}) == "USD up to 90,000/yr"
        assert render._salary({"value": {"minValue": "negotiable"}}) == ""
        assert render._salary({"value": {"minValue": 0, "maxValue": 0}}) == ""


class TestSearchTitle:
    def test_titles(self):
        assert _search_title("https://wellfound.com/role/r/software-engineer") == "Software Engineer jobs (remote)"
        assert _search_title("https://wellfound.com/role/l/product-manager/new-york") == \
            "Product Manager jobs in New York"
        assert _search_title("https://wellfound.com/location/austin") == "Jobs in Austin"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderJob:
    def test_job_posting(self):
        job = {
            "@type": "JobPosting", "title": "Software Engineer", "employmentType": "FULL_TIME",
            "datePosted": "2026-05-27T09:43:14Z",
            "hiringOrganization": {"name": "Acme", "sameAs": "https://acme.com"},
            "baseSalary": {"currency": "USD", "value": {"minValue": 185000, "maxValue": 250000, "unitText": "YEAR"}},
            "jobLocationType": "TELECOMMUTE", "applicantLocationRequirements": {"name": "United States"},
            "description": "<p>Build <strong>things</strong>.</p>", "industry": "Software",
        }
        out = render.render_job(job, "https://wellfound.com/jobs/1-swe")
        assert out.startswith("# Software Engineer")
        assert "**Acme**" in out
        assert "Full-Time" in out
        assert "Remote (United States)" in out
        assert "USD 185,000–250,000/yr" in out
        assert "Posted 2026-05-27" in out
        assert "Build **things**." in out


class TestJobPostingExtraction:
    def test_jsonld_present(self):
        html = '<html><head><script type="application/ld+json">' \
               '{"@type":"JobPosting","title":"X"}</script></head></html>'
        assert api.jsonld_jobposting(html)["title"] == "X"

    def test_soft_404_detected(self):
        # wellfound serves HTTP 200 with this title for a bare /jobs/{id} (no slug).
        html = "<html><head><title>Page not found - 404 | Wellfound</title></head><body></body></html>"
        assert api.jsonld_jobposting(html) is None
        assert api.is_not_found_page(html) is True

    def test_real_page_not_flagged_not_found(self):
        html = "<html><head><title>Software Engineer at Acme | Wellfound</title></head></html>"
        assert api.is_not_found_page(html) is False

    def test_title_starting_with_404_not_flagged(self):
        # A real job whose title starts with/contains "404" or "found" must pass.
        for title in ("404 Response Engineer at Acme | Wellfound",
                      "Lost & Found Operations Lead at Acme | Wellfound"):
            html = f"<html><head><title>{title}</title></head></html>"
            assert api.is_not_found_page(html) is False, title


_SEARCH_CACHE = {
    "JobListingSearchResult:1": {"__typename": "JobListingSearchResult", "id": "1", "slug": "swe",
                                 "title": "Senior SWE", "compensation": "$150k", "locationNames": ["NYC"],
                                 "remote": False, "jobType": "full-time"},
    "StartupResult:9": {"__typename": "StartupResult", "id": "9", "name": "Acme", "slug": "acme",
                        "highConcept": "We build X", "companySize": "SIZE_51_200",
                        "badges": [{"__ref": "Badge:AH"}],
                        "highlightedJobListings": [{"__ref": "JobListingSearchResult:1"}]},
    "Badge:AH": {"__typename": "Badge", "label": "Actively Hiring"},
}

_JOBS_FEED_CACHE = {
    "JobListing:1": {"__typename": "JobListing", "id": "1", "slug": "swe", "title": "SWE",
                     "compensation": "$120k", "locationNames": ["Remote"], "remote": True,
                     "acceptedRemoteLocationNames": ["US"], "startup": {"__ref": "Startup:9"}},
    "Startup:9": {"__typename": "Startup", "id": "9", "name": "Acme", "slug": "acme"},
}


class TestRenderSearch:
    def test_company_grouped(self):
        out = render.render_search(_SEARCH_CACHE, "https://wellfound.com/role/r/swe", "SWE jobs (remote)")
        assert "SWE jobs (remote)" in out
        assert "1 roles across 1 startups" in out
        assert "## Acme" in out
        assert "We build X" in out
        assert "51-200 employees" in out
        assert "Actively Hiring" in out
        assert "Senior SWE" in out
        assert "https://wellfound.com/jobs/1-swe" in out
        assert "https://wellfound.com/company/acme" in out

    def test_flat_job_list(self):
        out = render.render_search(_JOBS_FEED_CACHE, "https://wellfound.com/jobs", "Jobs")
        assert "1 roles (first page)" in out
        assert "SWE" in out
        assert "Acme" in out
        assert "Remote (US)" in out
        assert "https://wellfound.com/jobs/1-swe" in out


class TestRenderCompany:
    def test_company(self):
        cache = {
            "Startup:9": {
                "__typename": "Startup", "id": "9", "name": "Acme", "slug": "acme",
                "highConcept": "We build X", "companySize": "SIZE_51_200",
                "totalRaisedAmount": 4600000, "companyUrl": "https://acme.com",
                "twitterUrl": "twitter.com/https://x.com/acme",
                "productDescription": "Long about text.",
                "marketTaggings": [{"__ref": "NewTag:1"}],
                "culturePerks": [{"__ref": "CulturePerk:1"}],
                "badges": [{"__ref": "Badge:AH"}],
                "startupRounds": {"edges": [{"node": {"__ref": "StartupRound:1"}}]},
                "currentTeamMemberRoles({\"first\":3})": {"edges": [{"node": {"__ref": "StartupRole:1"}}]},
                # totalCount (12) intentionally exceeds the rendered edges (1): the
                # connection is stored under an arg-qualified key, so reading the bare
                # key would underreport as 1. Verifies the resolved-connection fix.
                "jobListingsConnection({\"first\":10})": {"totalCount": 12,
                                                          "edges": [{"node": {"__ref": "JobListing:1"}}]},
            },
            "NewTag:1": {"__typename": "NewTag", "displayName": "Healthcare"},
            "CulturePerk:1": {"__typename": "CulturePerk", "title": "Equity", "description": "You get equity"},
            "Badge:AH": {"__typename": "Badge", "label": "Actively Hiring"},
            "StartupRound:1": {"__typename": "StartupRound", "roundType": "Seed", "valuation": "0",
                               "closedAt": 1661904000},
            "StartupRole:1": {"__typename": "StartupRole", "user": {"__ref": "User:1"},
                              "roleDisplayName": "Employee"},
            "User:1": {"__typename": "User", "name": "Bob"},
            "JobListing:1": {"__typename": "JobListing", "id": "1", "slug": "swe", "title": "SWE",
                             "compensation": "$120k", "locationNames": ["NYC"]},
        }
        out = render.render_company(cache, cache["Startup:9"], "https://wellfound.com/company/acme")
        assert out.startswith("# Acme")
        assert "We build X" in out
        assert "51-200 employees" in out
        assert "$4.6M raised" in out
        assert "Actively Hiring" in out
        assert "(https://x.com/acme)" in out  # cleaned twitter url
        assert "## About" in out
        assert "**Markets:** Healthcare" in out
        assert "## Funding" in out
        assert "**Seed**" in out
        assert "## Perks" in out
        assert "**Equity:** You get equity" in out
        assert "Bob (Employee)" in out
        assert "## Open Jobs (12)" in out  # from totalCount, not the single rendered edge
        assert "SWE" in out
