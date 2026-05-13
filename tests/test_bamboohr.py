"""URL detection + rendering unit tests for the BambooHR module."""

from fetchaller.content import bamboohr

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestUrlDetection:
    def test_board(self):
        for u in [
            "https://avidbots.bamboohr.com/careers",
            "https://avidbots.bamboohr.com/careers/",
        ]:
            assert bamboohr.is_bamboohr_board_url(u)
            assert bamboohr.extract_bamboohr_board_params(u) == "avidbots"
            assert not bamboohr.is_bamboohr_url(u)

    def test_posting(self):
        for u in [
            "https://avidbots.bamboohr.com/careers/304",
            "https://avidbots.bamboohr.com/careers/304/",
            "https://avidbots.bamboohr.com/careers/304/whatever",
        ]:
            assert bamboohr.is_bamboohr_url(u)
            assert bamboohr.extract_bamboohr_params(u) == ("avidbots", "304")

    def test_subdomain_with_hyphens(self):
        u = "https://my-org.bamboohr.com/careers/12"
        assert bamboohr.is_bamboohr_url(u)
        assert bamboohr.extract_bamboohr_params(u) == ("my-org", "12")

    def test_rejects_non_bamboohr(self):
        for u in [
            "https://avidbots.com/careers/304",
            "https://bamboohr.com/careers/304",
            "https://avidbots.bamboohr.com/jobs/embed2.php",
            "https://avidbots.bamboohr.com/careers/foo",  # non-numeric id
        ]:
            assert not bamboohr.is_bamboohr_url(u)
            assert not bamboohr.is_bamboohr_board_url(u)


# ---------------------------------------------------------------------------
# Embed widget detection
# ---------------------------------------------------------------------------

class TestEmbedDetection:
    def test_basic_embed(self):
        html = '<div id="BambooHR" data-domain="avidbots.bamboohr.com" data-version="1.0.0"></div>'
        assert bamboohr.extract_bamboohr_embed_tenant(html) == "avidbots"

    def test_embed_with_extra_attrs(self):
        html = (
            '<div data-version="1.0.0" id="BambooHR" data-domain="contoso.bamboohr.com" '
            'data-departmentId="">stuff</div>'
        )
        assert bamboohr.extract_bamboohr_embed_tenant(html) == "contoso"

    def test_no_embed(self):
        assert bamboohr.extract_bamboohr_embed_tenant("<div>nothing</div>") is None
        assert bamboohr.extract_bamboohr_embed_tenant("") is None

    def test_wrong_domain(self):
        # id="BambooHR" but data-domain is something else — strict regex demands the bamboohr.com host.
        html = '<div id="BambooHR" data-domain="example.com"></div>'
        assert bamboohr.extract_bamboohr_embed_tenant(html) is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRender:
    def test_render_posting(self):
        payload = {
            "meta": {},
            "result": {
                "jobOpening": {
                    "jobOpeningShareUrl": "https://avidbots.bamboohr.com/careers/304",
                    "jobOpeningName": "Technical Support Specialist",
                    "departmentLabel": "Technical Support",
                    "employmentStatusLabel": "Full-Time",
                    "location": {"city": "Cali", "state": "Colombia"},
                    "description": "<p>Hello <strong>world</strong></p>",
                    "additionalInformation": "<p>Extra notes.</p>",
                },
            },
        }
        out = bamboohr.render_bamboohr_job(payload, source_url="https://avidbots.bamboohr.com/careers/304")
        assert out.startswith("# Technical Support Specialist\n")
        assert "- **departmentLabel**: Technical Support" in out
        assert "- **location**: Cali, Colombia" in out
        assert "## description" in out
        assert "Hello **world**" in out
        assert "## additionalInformation" in out
        assert "Extra notes." in out
        assert "**sourceUrl**: https://avidbots.bamboohr.com/careers/304" in out

    def test_render_board_groups_by_department(self):
        payload = {
            "meta": {"totalCount": 3},
            "result": [
                {
                    "id": "1",
                    "jobOpeningName": "Software Engineer",
                    "departmentLabel": "Engineering",
                    "employmentStatusLabel": "Full-Time",
                    "atsLocation": {"city": "Remote", "state": "Ontario", "country": "Canada"},
                },
                {
                    "id": "2",
                    "jobOpeningName": "Recruiter",
                    "departmentLabel": "People",
                    "employmentStatusLabel": "Full-Time",
                    "atsLocation": {"city": "Kitchener", "state": "Ontario"},
                },
                {
                    "id": "3",
                    "jobOpeningName": "QA",
                    "departmentLabel": "Engineering",
                    "employmentStatusLabel": "Full-Time",
                    "atsLocation": {},
                    "location": {"city": "Toronto"},
                },
            ],
        }
        out = bamboohr.render_bamboohr_board(
            payload, "avidbots", source_url="https://avidbots.com/company/careers/"
        )
        assert out.startswith("# avidbots — Job Board (3 open positions)\n")
        # Both engineering jobs under one heading
        assert "## Engineering" in out
        assert "## People" in out
        # Posting URLs
        assert "https://avidbots.bamboohr.com/careers/1" in out
        assert "https://avidbots.bamboohr.com/careers/3" in out
        # Falls back to location when atsLocation is empty
        assert "Toronto" in out
