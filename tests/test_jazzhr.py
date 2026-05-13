"""URL detection + rendering unit tests for the JazzHR module."""

from fetchaller.content import jazzhr

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestUrlDetection:
    def test_board(self):
        for u in [
            "https://earthdaily.applytojob.com/apply",
            "https://earthdaily.applytojob.com/apply/",
        ]:
            assert jazzhr.is_jazzhr_board_url(u)
            assert jazzhr.extract_jazzhr_tenant(u) == "earthdaily"
            assert not jazzhr.is_jazzhr_url(u)

    def test_posting_with_slug(self):
        u = "https://earthdaily.applytojob.com/apply/AcXUJLBsep/Sr-Software-Engineer"
        assert jazzhr.is_jazzhr_url(u)
        assert jazzhr.extract_jazzhr_params(u) == ("earthdaily", "AcXUJLBsep")

    def test_posting_without_slug(self):
        u = "https://earthdaily.applytojob.com/apply/AcXUJLBsep"
        assert jazzhr.is_jazzhr_url(u)

    def test_tenant_hyphens(self):
        u = "https://cb-insights.applytojob.com/apply"
        assert jazzhr.extract_jazzhr_tenant(u) == "cb-insights"

    def test_rejects_non_jazzhr(self):
        for u in [
            "https://earthdaily.com/apply",
            "https://example.applytojob.com/foo",  # wrong path prefix
            "https://example.applytojob.com/apply/short",  # id too short
        ]:
            assert not jazzhr.is_jazzhr_url(u)
            assert not jazzhr.is_jazzhr_board_url(u)


# ---------------------------------------------------------------------------
# Embed-page tenant extraction
# ---------------------------------------------------------------------------

class TestEmbedDetection:
    def test_extracts_multiple_tenants(self):
        html = (
            '<a href="https://earthdaily.applytojob.com/apply/foo">Jobs</a> '
            '<a href="https://earthdailyagro.applytojob.com/apply/">Agro</a> '
            '<a href="https://earthdaily.applytojob.com/apply/">dup</a>'
        )
        tenants = jazzhr.extract_jazzhr_embed_tenants(html)
        assert tenants == ["earthdaily", "earthdailyagro"]

    def test_empty(self):
        assert jazzhr.extract_jazzhr_embed_tenants("<div>nothing</div>") == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRender:
    def test_render_posting(self):
        payload = {
            "jobPosting": {
                "@context": "http://schema.org/",
                "@type": "JobPosting",
                "url": "https://earthdaily.applytojob.com/apply/abc/foo",
                "title": "Senior Engineer",
                "datePosted": "2026-02-21",
                "employmentType": "FULL_TIME",
                "description": "<p>About the role.</p>",
                "hiringOrganization": {"@type": "Organization", "name": "EarthDaily"},
            },
            "pageTitle": "Senior Engineer - EarthDaily Career Page",
            "tenant": "earthdaily",
            "postingId": "abc",
        }
        out = jazzhr.render_jazzhr_job(
            payload, source_url="https://earthdaily.applytojob.com/apply/abc"
        )
        assert out.startswith("# Senior Engineer\n")
        assert "- **datePosted**: 2026-02-21" in out
        assert "- **employmentType**: FULL_TIME" in out
        assert "## description" in out
        assert "About the role." in out
        # Top-level @context / @type keys should be skipped (not rendered as their own bullets).
        assert "- **@context**" not in out
        assert "- **@type**" not in out
        assert "**sourceUrl**: https://earthdaily.applytojob.com/apply/abc" in out

    def test_render_board_groups_by_department(self):
        jobs = [
            {
                "title": "Director, Product Management",
                "posting_id": "ABC",
                "slug": "Director-PM",
                "department": "Agriculture",
                "location": "Minneapolis, MN",
                "url": "https://earthdaily.applytojob.com/apply/ABC/Director-PM",
            },
            {
                "title": "Sr. Software Engineer",
                "posting_id": "DEF",
                "slug": "Sr-SE",
                "department": "Engineering",
                "location": "Vancouver, BC",
                "url": "https://earthdaily.applytojob.com/apply/DEF/Sr-SE",
            },
        ]
        out = jazzhr.render_jazzhr_board(
            jobs, "earthdaily", source_url="https://earthdaily.applytojob.com/apply"
        )
        assert "# earthdaily — Job Board (2 open positions)" in out
        assert "## Agriculture" in out
        assert "## Engineering" in out
        assert "Director, Product Management" in out
        assert "Minneapolis, MN" in out
        assert "https://earthdaily.applytojob.com/apply/ABC/Director-PM" in out

    def test_render_multi_board(self):
        boards = [
            ("a", [{"title": "J1", "department": "Eng", "location": "X",
                    "url": "https://a.applytojob.com/apply/1/j1"}]),
            ("b", []),
        ]
        out = jazzhr.render_jazzhr_boards(boards, source_url="https://co.example/jobs")
        assert "# Job Board (1 open positions across 2 JazzHR sites)" in out
        assert "## a (1 positions)" in out
        assert "## b (0 positions)" in out
        assert "J1" in out
