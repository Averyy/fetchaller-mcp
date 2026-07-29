"""Unit tests for Cornerstone OnDemand (CSOD) content module."""

from unittest.mock import AsyncMock, patch

from fetchaller.content.cornerstone import (
    extract_cornerstone_board_params,
    extract_cornerstone_params,
    extract_csod_context,
    fetch_cornerstone_board,
    is_cornerstone_board_url,
    is_cornerstone_url,
    render_cornerstone_board,
    render_cornerstone_job,
)
from fetchaller.security.ssrf import HostVerdict


class TestUrlDetection:
    def test_posting_url(self):
        url = "https://seequent.csod.com/ux/ats/careersite/1/home/requisition/4265?c=seequent&source=LinkedIn"
        assert is_cornerstone_url(url)
        assert extract_cornerstone_params(url) == ("seequent", "1", "4265")

    def test_board_url(self):
        url = "https://seequent.csod.com/ux/ats/careersite/1/home?c=seequent"
        assert is_cornerstone_board_url(url)
        assert not is_cornerstone_url(url)
        assert extract_cornerstone_board_params(url) == ("seequent", "1")

    def test_tenants_with_hyphens(self):
        url = "https://my-corp.csod.com/ux/ats/careersite/42/home/requisition/99"
        assert is_cornerstone_url(url)
        assert extract_cornerstone_params(url) == ("my-corp", "42", "99")

    def test_other_host_rejected(self):
        assert not is_cornerstone_url(
            "https://example.com/ux/ats/careersite/1/home/requisition/4265"
        )
        assert not is_cornerstone_board_url("https://example.com/ux/ats/careersite/1/home")

    def test_admin_path_rejected(self):
        # Only the public career-site path
        assert not is_cornerstone_url(
            "https://seequent.csod.com/services/x/job-requisition/v2/requisitions/4265"
        )


class TestExtractCsodContext:
    def test_parses_inline_context(self):
        html = (
            "<html><body><script>csod.context={"
            '"corp":"acme","cultureID":1,"cultureName":"en-US","token":"abc",'
            '"endpoints":{"cloud":"https://us.api.csod.com/","api":"/"}'
            "};</script></body></html>"
        )
        ctx = extract_csod_context(html)
        assert ctx is not None
        assert ctx["corp"] == "acme"
        assert ctx["cultureID"] == 1
        assert ctx["token"] == "abc"
        assert ctx["endpoints"]["cloud"] == "https://us.api.csod.com/"

    def test_missing_context_returns_none(self):
        assert extract_csod_context("<html><body></body></html>") is None

    def test_malformed_context_returns_none(self):
        assert extract_csod_context("<script>csod.context={broken};</script>") is None


class TestRenderCornerstoneJob:
    def _payload(self) -> dict:
        return {
            "jobDetails": {
                "displayTitle": "Senior Product Designer",
                "externalDescription": "<p>Reporting to the <strong>Manager</strong>.</p>",
                "ref": "req4265",
                "requisitionStatusId": 2,  # skip
                "hiringManagerId": 75079,   # skip
                "allowApply": True,
                "primaryLocation": {
                    "title": "Suite 810, 207 Queens Quay West, Toronto",
                    "city": "Toronto",
                    "country": "CA",
                },
                "defaultCultureId": 2,
                "availableCultures": [1, 2],
                "additionalLocations": [
                    {"city": "Calgary", "country": "CA"},
                ],
                "openDate": "2026-04-17T19:11:01",
                "companyApplyUrl": "https://seequent.csod.com/ux/ats/careersite/1/home/requisition/4265?c=seequent",
                "positionOUId": 855,  # skip
            },
            "context": {"corp": "seequent"},
            "requisitionId": "4265",
        }

    def test_renders_header_metadata_and_description(self):
        md = render_cornerstone_job(self._payload(), source_url="https://example.com/job")
        assert md.startswith("# Senior Product Designer @ seequent\n")
        assert "- **corp**: seequent" in md
        assert "- **requisitionId**: 4265" in md
        assert "- **ref**: req4265" in md
        assert "- **allowApply**: true" in md
        assert "- **availableCultures**: 1; 2" in md
        assert "## externalDescription" in md
        assert "Reporting to the **Manager**." in md
        assert "- **primaryLocation**: Suite 810, 207 Queens Quay West, Toronto · Toronto · CA" in md
        assert "- **additionalLocations**: Calgary · CA" in md
        assert md.rstrip().endswith("**sourceUrl**: https://example.com/job")

    def test_skips_internal_handles(self):
        md = render_cornerstone_job(self._payload())
        assert "hiringManagerId" not in md
        assert "requisitionStatusId" not in md
        assert "positionOUId" not in md

    def test_header_without_corp(self):
        payload = self._payload()
        payload["context"] = {}
        md = render_cornerstone_job(payload)
        assert md.startswith("# Senior Product Designer\n")

    def test_fallback_header(self):
        md = render_cornerstone_job({"jobDetails": {}, "context": {}})
        assert md.startswith("# Job Posting\n")


class TestRenderCornerstoneBoard:
    def _payload(self) -> dict:
        return {
            "requisitions": [
                {
                    "requisitionId": 4282,
                    "displayJobTitle": "Salesforce Administrator",
                    "locations": [{"city": "Toronto", "country": "CA"}],
                    "postingEffectiveDate": "12/05/2026",
                    "externalDescription": "Role: keep Salesforce humming.",
                },
                {
                    "requisitionId": 4281,
                    "displayJobTitle": "Sales Operations Specialist",
                    "locations": [{"city": "Belo Horizonte", "country": "BR"}],
                    "postingEffectiveDate": "-",
                },
            ],
            "totalCount": 2,
            "context": {"corp": "seequent", "cultureName": "en-GB"},
        }

    def test_header_and_metadata(self):
        md = render_cornerstone_board(
            self._payload(),
            source_url="https://seequent.csod.com/ux/ats/careersite/1/home",
        )
        assert md.startswith("# seequent — Job Board (2 open positions)\n")
        assert "- **corp**: seequent" in md
        assert "- **cultureName**: en-GB" in md
        assert "- **boardUrl**: https://seequent.csod.com/ux/ats/careersite/1/home" in md

    def test_posting_lines_with_links(self):
        md = render_cornerstone_board(
            self._payload(),
            source_url="https://seequent.csod.com/ux/ats/careersite/1/home?c=seequent",
        )
        assert "- **Salesforce Administrator** — Toronto, CA · posted 12/05/2026" in md
        assert (
            "https://seequent.csod.com/ux/ats/careersite/1/home/requisition/4282"
            in md
        )
        # Hyphen posting date is suppressed
        assert "- **Sales Operations Specialist** — Belo Horizonte, BR" in md
        assert "posted -" not in md
        assert "Role: keep Salesforce humming." in md

    def test_empty_board(self):
        md = render_cornerstone_board(
            {"requisitions": [], "totalCount": 0, "context": {"corp": "acme"}}
        )
        assert "Job Board (0 open positions)" in md


class TestBoardSsrf:
    async def test_board_refuses_internal_cloud(self):
        """SSRF: `cloud` is a base URL from the page's csod.context JS blob with
        no domain anchor; an internal cloud host must be refused before the POST."""
        page_html = (
            '<html><body><script>csod.context={"token":"t","cultureID":1,'
            '"cultureName":"en-US",'
            '"endpoints":{"cloud":"http://169.254.169.254","api":"/"}};'
            "</script></body></html>"
        )
        posts: list[str] = []

        class _Resp:
            status_code = 200
            text = page_html
            url = "https://acme.csod.com/ux/ats/careersite/5/home"

        class _Session:
            async def get(self, u, **kwargs):
                return _Resp()

            async def post(self, u, **kwargs):
                posts.append(u)
                return _Resp()

        result = await fetch_cornerstone_board(
            "https://acme.csod.com/ux/ats/careersite/5/home",
            _Session(),
        )
        assert result is None
        assert posts == []  # never POSTed to the internal cloud host

    async def test_board_pins_official_cloud_and_never_reuses_page_session_for_jwt(self):
        page_html = (
            '<script>csod.context={"token":"secret-jwt","cultureID":1,'
            '"cultureName":"en-US","corp":"acme",'
            '"endpoints":{"cloud":"https://us.api.csod.com/"}};</script>'
        )

        class _PageResponse:
            status_code = 200
            text = page_html

        class _PageSession:
            posts: list[str] = []

            async def get(self, _url, **_kwargs):
                return _PageResponse()

            async def post(self, url, **_kwargs):
                self.posts.append(url)
                raise AssertionError("JWT must not use the unpinned page session")

        posted: list[tuple[str, dict]] = []

        class _ApiResponse:
            status_code = 200

            def json(self):
                return {"data": {"requisitions": [], "totalCount": 0}}

        class _PinnedSession:
            async def post(self, url, **kwargs):
                posted.append((url, kwargs))
                return _ApiResponse()

        page_session = _PageSession()
        with (
            patch(
                "fetchaller.content.cornerstone.check_host",
                new_callable=AsyncMock,
                return_value=HostVerdict(
                    "us.api.csod.com",
                    False,
                    ["203.0.113.20"],
                ),
            ),
            patch(
                "fetchaller.content.cornerstone.wafer.AsyncSession",
                return_value=_PinnedSession(),
            ) as session_type,
        ):
            result = await fetch_cornerstone_board(
                "https://acme.csod.com/ux/ats/careersite/5/home",
                page_session,
            )

        assert result == {
            "requisitions": [],
            "totalCount": 0,
            "context": {
                "token": "secret-jwt",
                "cultureID": 1,
                "cultureName": "en-US",
                "corp": "acme",
                "endpoints": {"cloud": "https://us.api.csod.com/"},
            },
        }
        assert page_session.posts == []
        session_type.assert_called_once_with(
            resolve={"us.api.csod.com": ["203.0.113.20"]},
            follow_redirects=False,
            max_rotations=0,
            max_response_size=10 * 1024 * 1024,
        )
        assert posted[0][0] == "https://us.api.csod.com/rec-job-search/external/jobs"
        assert posted[0][1]["headers"]["authorization"] == "Bearer secret-jwt"
