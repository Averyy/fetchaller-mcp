"""ca.indeed.com client: parsing, page identity, and boundary validation."""

from __future__ import annotations

import json
import time

import pytest

from fetchaller import server
from fetchaller.indeed import api, render, search


class TestUnrecognisedPage:
    """A 200 that is not a search page must not read as 'no jobs matched'."""

    @pytest.mark.parametrize(
        "body",
        [
            "<html><body>Sign in to continue</body></html>",
            "<html><body><div>totally different markup</div></body></html>",
            "",
        ],
    )
    def test_a_page_without_the_container_raises(self, body):
        # Login shells, soft 404s and markup changes all arrive as HTTP 200 and
        # all used to parse to jobs=[]/total=0 — a confident wrong answer about
        # a search that never ran.
        with pytest.raises(api.IndeedUnrecognisedPageError):
            api.parse_search(body)

    def test_a_genuinely_empty_result_set_is_not_an_error(self):
        # An empty search still HAS the container; only its absence is fatal.
        jobs, total = api.parse_search("<html>mosaic-provider-jobcards</html>")
        assert jobs == [] and total == 0

    def test_a_positive_total_with_a_malformed_container_is_not_empty(self):
        body = (
            '<html>mosaic-provider-jobcards "totalJobCount":12 '
            'window.mosaic.providerData["mosaic-provider-jobcards"] = {broken</html>'
        )
        with pytest.raises(api.IndeedUnrecognisedPageError):
            api.parse_search(body)


class TestAdvisoryLocationProbe:
    @pytest.mark.asyncio
    async def test_probe_failure_keeps_the_successful_primary_results(self, monkeypatch):
        async def fake_session(*args, **kwargs):
            return object()

        async def fake_search_all(**kwargs):
            return [
                {
                    "job_key": "abc123",
                    "title": "Nurse",
                    "employer": "Hospital",
                    "location": "Toronto, ON",
                    "url": "https://ca.indeed.com/viewjob?jk=abc123",
                }
            ], 10, False

        async def blocked_probe(**kwargs):
            raise api.IndeedBlockedError("comparison request blocked")

        monkeypatch.setattr(api, "_get_session", fake_session)
        monkeypatch.setattr(api, "search_all", fake_search_all)
        monkeypatch.setattr(api, "search_page", blocked_probe)

        result = await search.search_indeed(
            title="nurse",
            location="Toronto",
            strict_title=False,
        )

        assert "error" not in result
        assert "Nurse" in result["content"]
        assert "could not be independently verified" in result["content"]


class TestSalaryBand:
    def test_missing_unit_does_not_double_space(self):
        out = render._band(
            {"salary_min": 19, "salary_max": 23, "salary_unit": None, "currency": "CAD"}
        )
        assert out == "$19 - $23 CAD"
        assert "  " not in out

    def test_no_salary_is_empty(self):
        assert render._band({}) == ""

    def test_negative_missing_endpoint_is_not_rendered_as_pay(self):
        assert api._salary(
            {"extractedSalary": {"min": 35, "max": -1, "type": "hourly"}}
        ) == "$35 hourly"

    def test_malformed_numeric_endpoint_falls_back_to_the_snippet(self):
        assert api._salary(
            {
                "extractedSalary": {"min": "35", "max": float("nan")},
                "salarySnippet": {"text": "$35 hourly"},
            }
        ) == "$35 hourly"


class TestBooleanBoundary:
    """Schema-declared booleans are validated by TYPE, not truthiness."""

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, [], None])
    def test_non_booleans_are_rejected(self, value):
        # "false" is Python-truthy: before this, writing it turned the filter ON.
        error = server._validate_tool_arguments(
            "search_indeed", {"title": "x", "strict_title": value}
        )
        assert error and "must be a boolean" in error

    @pytest.mark.parametrize("value", [True, False])
    def test_real_booleans_pass(self, value):
        assert (
            server._validate_tool_arguments(
                "search_indeed", {"title": "x", "strict_title": value}
            )
            is None
        )

    def test_every_schema_boolean_is_covered(self):
        # The set is derived from the schemas; if a tool adds a boolean and this
        # drifts, the guarantee silently stops applying to it.
        assert {"strict_title", "strict_location", "raw", "remote_only"} <= server._BOOLEAN_ARGS


class TestHostileInput:
    """Parsers run over remote bodies up to 16MB, synchronously."""

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("ld+json openers", '<script type="application/ld+json">' * 20000),
            ("ld+json pairs", '<script type="application/ld+json">x</script>' * 20000),
        ],
    )
    def test_unterminated_openers_do_not_stall(self, label, body):
        # Measured before the fix: 20,000 unterminated openers cost 35s of
        # synchronous CPU, blocking the event loop for every concurrent MCP
        # request. A lazy `(.*?)</script>` rescanned the body per candidate.
        import time

        start = time.monotonic()
        api.parse_job(body)
        assert time.monotonic() - start < 2.0, label

    def test_unterminated_mosaic_assignments_do_not_stall(self):
        import time

        opener = 'window.mosaic.providerData["mosaic-provider-jobcards"] = {'
        body = opener * 2_000
        started = time.monotonic()
        jobs, total = api.parse_search(body)
        assert jobs == [] and total == 0
        assert time.monotonic() - started < 2.0

    def test_unterminated_tags_do_not_stall(self):
        import time

        body = "<" * 20_000
        started = time.monotonic()
        assert api._plain(body) == body
        assert time.monotonic() - started < 2.0

    @pytest.mark.parametrize(
        "key",
        ["x) [click](javascript:alert(1))", "a" * 500, "../../etc/passwd", "a b", ""],
    )
    def test_a_hostile_jobkey_never_becomes_a_url(self, key):
        # jobkey is remote JSON and is interpolated into a markdown link.
        card = json.dumps({"jobkey": key, "title": "T", "company": "C"})
        body = (
            "mosaic-provider-jobcards\n"
            'window.mosaic.providerData["mosaic-provider-jobcards"] = '
            '{"metaData":{"mosaicProviderJobCardsModel":{"results":['
            + card
            + "]}}};\n"
        )
        jobs, _ = api.parse_search(body)
        assert jobs == [], f"hostile key accepted: {key!r}"

    def test_plain_description_cannot_create_an_active_markdown_link(self):
        out = render.render_job(
            {
                "title": "Example",
                "description": "Apply [here](javascript:alert(1))",
            }
        )

        assert "[here](javascript:" not in out
        assert r"\[here\]\(javascript:alert\(1\)\)" in out


def _viewjob_page(preloaded: dict, *, ld: dict | None = None) -> str:
    """A /viewjob page as Indeed shipped it on 2026-11-02: no JSON-LD, the posting in _rootProps."""
    parts = ["<html><head><title>Posting - Indeed.com</title></head><body>"]
    if ld is not None:
        parts.append(f'<script type="application/ld+json">{json.dumps(ld)}</script>')
    parts.append(
        "<script>window._rootProps = "
        + json.dumps({"url": "https://ca.indeed.com/viewjob?jk=d1fedee2cc5aaf65", "locale": "en", "preloadedVJData": preloaded})
        + ";</script>"
        "<script>window._initialData = {ssr: true, viewJobClientSideModel: window._rootProps.preloadedVJData};</script>"
        "</body></html>"
    )
    return "".join(parts)


# Trimmed from the live page for jk=d1fedee2cc5aaf65 (2026-11-02).
_LIVE_PRELOADED = {
    "jobInfoWrapperModel": {
        "jobInfoModel": {
            "jobInfoHeaderModel": {
                "jobTitle": "Software Engineer I (entry level)",
                "companyName": "Identity Digital",
                "formattedLocation": "Toronto, ON M2N 6L4",
                "salaryMin": None,
                "salaryMax": None,
                "salaryCurrency": None,
                "salaryType": None,
            },
            "jobMetadataHeaderModel": {"jobType": "Full-time"},
            "jobDescriptionSectionModel": {"jobDetailsSection": {"isJobExpired": False}},
            "expiredJobMetadataModel": None,
            "showExpiredHeader": False,
            "sanitizedJobDescription": "<div><h2><b>About</b></h2><p>Build <i>registry</i> systems.</p></div>",
        }
    },
    "salaryInfoModel": {
        "salaryCurrency": "CAD",
        "salaryMax": 95000,
        "salaryMin": 74000,
        "salaryText": "$74,000–$95,000 a year",
        "salaryType": "YEARLY",
    },
    "hiringInsightsModel": {"age": "19 days ago"},
}


class TestPostingPageData:
    """/viewjob without JSON-LD: the posting comes from window._rootProps.preloadedVJData."""

    def test_live_shape_parses(self):
        job = api.parse_job(_viewjob_page(_LIVE_PRELOADED))

        assert job["title"] == "Software Engineer I (entry level)"
        assert job["employer"] == "Identity Digital"
        assert job["location"] == "Toronto, ON M2N 6L4"
        assert job["employment_type"] == "Full-time"
        assert job["posted"] == "19 days ago"
        assert (job["salary_min"], job["salary_max"], job["salary_unit"], job["currency"]) == (74000, 95000, "YEAR", "CAD")
        assert job["description"] == "About Build registry systems."
        assert job["valid_through"] == ""
        assert job["expired"] is False

    def test_rendered_without_inventing_an_expiry(self):
        job = api.parse_job(_viewjob_page(_LIVE_PRELOADED))
        job["url"] = "https://ca.indeed.com/viewjob?jk=d1fedee2cc5aaf65"

        rendered = render.render_job(job)

        assert "- **Salary:** $74,000 - $95,000 annually CAD" in rendered
        assert "- **Posted:** 19 days ago" in rendered
        assert "valid through" not in rendered.lower()
        assert "expired" not in rendered.lower()

    def test_expired_posting_says_so(self):
        preloaded = json.loads(json.dumps(_LIVE_PRELOADED))
        preloaded["jobInfoWrapperModel"]["jobInfoModel"]["jobDescriptionSectionModel"]["jobDetailsSection"]["isJobExpired"] = True
        job = api.parse_job(_viewjob_page(preloaded))
        job["url"] = "https://ca.indeed.com/viewjob?jk=x"

        assert "- **Status:** Indeed marks this posting expired" in render.render_job(job)

    def test_header_salary_is_the_fallback(self):
        preloaded = json.loads(json.dumps(_LIVE_PRELOADED))
        preloaded["salaryInfoModel"] = None
        header = preloaded["jobInfoWrapperModel"]["jobInfoModel"]["jobInfoHeaderModel"]
        header.update(salaryMin=21, salaryMax=24, salaryCurrency="CAD", salaryType="HOURLY")

        job = api.parse_job(_viewjob_page(preloaded))

        assert (job["salary_min"], job["salary_max"], job["salary_unit"]) == (21, 24, "HOUR")

    @pytest.mark.parametrize("value", [-1, 0, True, "74000", None])
    def test_missing_or_malformed_pay_is_not_pay(self, value):
        preloaded = json.loads(json.dumps(_LIVE_PRELOADED))
        preloaded["salaryInfoModel"]["salaryMin"] = value
        preloaded["salaryInfoModel"]["salaryMax"] = value

        job = api.parse_job(_viewjob_page(preloaded))

        assert job["salary_min"] is None and job["salary_max"] is None

    def test_json_ld_is_still_preferred(self):
        ld = {
            "@type": "JobPosting",
            "title": "From JSON-LD",
            "hiringOrganization": {"name": "Acme"},
            "datePosted": "2026-10-01T00:00:00Z",
            "validThrough": "2026-12-01T00:00:00Z",
        }

        job = api.parse_job(_viewjob_page(_LIVE_PRELOADED, ld=ld))

        assert job["title"] == "From JSON-LD"
        assert job["valid_through"] == "2026-12-01"

    def test_page_without_a_posting_is_none(self):
        assert api.parse_job(_viewjob_page({"jobInfoWrapperModel": {"jobInfoModel": {}}})) is None
        assert api.parse_job("<html><body>Sign in</body></html>") is None

    def test_unterminated_root_props_does_not_stall(self):
        body = "<script>window._rootProps = {" + '"a":"' * 200_000

        started = time.monotonic()
        assert api.parse_job(body) is None
        assert time.monotonic() - started < 2
