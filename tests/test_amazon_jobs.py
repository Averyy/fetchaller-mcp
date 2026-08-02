"""amazon.jobs URL detection, pay-band extraction, and rendering.

The pay-band tests matter most: Amazon publishes bands only where local law
requires it, buried at the tail of ``preferred_qualifications``, and lifting
them into a field is the main reason this client exists.
"""

from fetchaller.amazon_jobs import api, render, url

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_posting(self):
        u = "https://www.amazon.jobs/en/jobs/10471950/art-director-amazon-ads"
        assert url.is_amazon_job_url(u)
        assert url.extract_amazon_job_path(u) == "/en/jobs/10471950/art-director-amazon-ads"

    def test_posting_without_slug(self):
        assert url.is_amazon_job_url("https://www.amazon.jobs/en/jobs/10471950")

    def test_posting_without_locale(self):
        assert url.is_amazon_job_url("https://amazon.jobs/jobs/10471950/slug")

    def test_search(self):
        assert url.is_amazon_jobs_search_url("https://www.amazon.jobs/en/search")
        assert url.is_amazon_jobs_search_url("https://www.amazon.jobs/en/search.json?q=x")

    def test_retail_storefront_is_not_a_job_board(self):
        # Must never be confused with the amazon.com/.ca shopping post-processor.
        assert not url.is_amazon_job_url("https://www.amazon.ca/dp/B01234")
        assert not url.is_amazon_jobs_host("www.amazon.ca")
        assert not url.is_amazon_jobs_host("amazon.com")

    def test_incomplete_path_rejected(self):
        assert not url.is_amazon_job_url("https://www.amazon.jobs/en/jobs/")


class TestCategorySlug:
    def test_simple(self):
        assert api.category_slug("Design") == "design"

    def test_multiword(self):
        assert api.category_slug("Software Development") == "software-development"

    def test_punctuation_collapsed(self):
        assert api.category_slug("Sales, Advertising, & Account Management") == (
            "sales-advertising-account-management"
        )

    def test_empty(self):
        assert api.category_slug("") == ""


# ---------------------------------------------------------------------------
# Pay bands
# ---------------------------------------------------------------------------

_PAY_TAIL = (
    "<p>...only those interviewed will be advised as to hiring status.</p>"
    "<br/><br/>CAN, ON, Toronto - 185,400.00 - 309,600.00 CAD annually"
)


class TestPayBands:
    def test_extracts_canadian_band(self):
        job = {"preferred_qualifications": _PAY_TAIL}
        assert render.extract_pay_bands(job) == ["Toronto, ON: 185,400-309,600 CAD annually"]

    def test_drops_trailing_cents(self):
        job = {"preferred_qualifications": "CAN, BC, Vancouver - 114,800.00 - 191,800.00 CAD annually"}
        assert render.extract_pay_bands(job) == ["Vancouver, BC: 114,800-191,800 CAD annually"]

    def test_multiple_locations_deduplicated(self):
        job = {
            "preferred_qualifications": (
                "CAN, ON, Toronto - 100,000.00 - 200,000.00 CAD annually "
                "US, WA, Seattle - 151,300.00 - 261,500.00 USD annually "
                "CAN, ON, Toronto - 100,000.00 - 200,000.00 CAD annually"
            )
        }
        assert render.extract_pay_bands(job) == [
            "Toronto, ON: 100,000-200,000 CAD annually",
            "Seattle, WA: 151,300-261,500 USD annually",
        ]

    def test_hourly_period_preserved(self):
        job = {"preferred_qualifications": "US, CA, Fresno - 18.50 - 24.00 USD hourly"}
        assert render.extract_pay_bands(job) == ["Fresno, CA: 18.50-24.00 USD hourly"]

    def test_no_band_is_empty_not_an_error(self):
        assert render.extract_pay_bands({"preferred_qualifications": "No pay here."}) == []
        assert render.extract_pay_bands({}) == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_JOB = {
    "id_icims": "10471950",
    "title": "Art Director, Amazon Ads Brand Innovation Lab",
    "normalized_location": "Toronto, Ontario, CAN",
    "job_category": "Design",
    "company_name": "Amazon Advertising Canada Inc.",
    "posted_date": "July 13, 2026",
    "job_path": "/en/jobs/10471950/art-director",
    "preferred_qualifications": _PAY_TAIL,
    "description_short": "A versatile creative.",
}


class TestRender:
    def test_search_results(self):
        out = render.render_search_results([_JOB], title="", location="Toronto", hits=1)
        assert "# Amazon jobs" in out
        assert "https://www.amazon.jobs/en/jobs/10471950/art-director" in out
        assert "**Category**: Design" in out
        assert "Toronto, ON: 185,400-309,600 CAD annually" in out

    def test_both_drop_counts_reported(self):
        out = render.render_search_results(
            [], title="product designer", location="Toronto", hits=22,
            title_filtered=8, location_filtered=15,
        )
        assert "dropped 8 by title and 15 by location" in out

    def test_job_detail_includes_qualifications(self):
        job = dict(_JOB, description="<p>Body</p>", basic_qualifications="<p>Basic</p>")
        out = render.render_job(job)
        assert "## Description" in out
        assert "## Basic qualifications" in out
        assert "**Pay**:" in out

    def test_markdown_metacharacters_escaped(self):
        out = render.render_search_results([dict(_JOB, title="Designer [L5] *Remote*")], hits=1)
        assert "\\[L5\\]" in out
