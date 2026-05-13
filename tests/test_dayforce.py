"""Unit tests for Dayforce content module."""

import json

from fetchaller.content.dayforce import (
    extract_dayforce_board_params,
    extract_dayforce_params,
    extract_next_data,
    is_dayforce_board_url,
    is_dayforce_url,
    render_dayforce_board,
    render_dayforce_job,
)


class TestUrlDetection:
    def test_posting_url(self):
        url = "https://jobs.dayforcehcm.com/en-US/tml/ATXCANDIDATEPORTAL/jobs/1023?source=Linkedin"
        assert is_dayforce_url(url)
        assert extract_dayforce_params(url) == ("en-US", "tml", "ATXCANDIDATEPORTAL", "1023")

    def test_posting_fr_locale(self):
        url = "https://jobs.dayforcehcm.com/fr-CA/acme/BOARD/jobs/42"
        assert is_dayforce_url(url)

    def test_board_url(self):
        url = "https://jobs.dayforcehcm.com/en-US/tml/ATXCANDIDATEPORTAL"
        assert is_dayforce_board_url(url)
        assert not is_dayforce_url(url)
        assert extract_dayforce_board_params(url) == ("en-US", "tml", "ATXCANDIDATEPORTAL")

    def test_other_host_rejected(self):
        assert not is_dayforce_url("https://example.com/en-US/tml/BOARD/jobs/1")
        assert not is_dayforce_board_url("https://example.com/en-US/tml/BOARD")

    def test_apply_subpath_rejected(self):
        # We only handle the canonical jobs/{id} path
        assert not is_dayforce_url(
            "https://jobs.dayforcehcm.com/en-US/tml/BOARD/jobs/1/apply"
        )


class TestExtractNextData:
    def test_parses_embedded_blob(self):
        html = (
            '<html><body><div></div>'
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"jobData":{"jobTitle":"x"}}}}'
            "</script></body></html>"
        )
        d = extract_next_data(html)
        assert d["props"]["pageProps"]["jobData"]["jobTitle"] == "x"

    def test_missing_blob_returns_none(self):
        assert extract_next_data("<html><body>no data</body></html>") is None

    def test_malformed_json_returns_none(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{not json}</script>'
        assert extract_next_data(html) is None


class TestRenderDayforceJob:
    def _payload(self) -> dict:
        return {
            "jobData": {
                "jobPostingId": 1023,
                "jobReqId": 255,
                "jobTitle": "Lead Product Designer",
                "postingStartTimestampUTC": "2026-04-15T04:00:00+00:00",
                "isEvergreen": False,
                "createdTimestampUTC": "2026-04-15T14:26:26",
                "jobPostingContent": {
                    "jobDescriptionHeader": "<p>Header <strong>line</strong></p>",
                    "jobDescription": "<p>Body paragraph</p>",
                    "jobDescriptionFooter": "<p>Footer text</p>",
                },
                "postingLocations": [
                    {
                        "formattedAddress": "725 Baransway Drive, London, Ontario, Canada",
                        "cityName": "London",
                        "stateCode": "ON",
                        "isoCountryCode": "CA",
                    }
                ],
                "jobPostingAttributes": [
                    {"name": "PayType", "value": "Salary", "type": "string"}
                ],
                "postingStatus": 1,            # skip
                "jobApplicationTemplateId": 23,  # skip
            },
            "siteInfo": {
                "clientNamespace": "tml",
                "jobBoardCode": "atxcandidateportal",
                "cultureCode": "en-US",
            },
        }

    def test_renders_title_metadata_and_description(self):
        md = render_dayforce_job(self._payload(), source_url="https://example.com/job")
        assert md.startswith("# Lead Product Designer\n")
        assert "- **clientNamespace**: tml" in md
        assert "- **jobBoardCode**: atxcandidateportal" in md
        assert "- **jobPostingId**: 1023" in md
        assert "- **jobReqId**: 255" in md
        assert "## postingLocations" in md
        assert "725 Baransway Drive, London, Ontario, Canada" in md
        assert "## jobPostingAttributes" in md
        assert "- **PayType**: Salary" in md
        assert "## jobDescriptionHeader" in md
        assert "Header **line**" in md
        assert "## jobDescription" in md
        assert "Body paragraph" in md
        assert "## jobDescriptionFooter" in md
        assert "Footer text" in md
        assert md.rstrip().endswith("**sourceUrl**: https://example.com/job")

    def test_skips_internal_handles(self):
        md = render_dayforce_job(self._payload())
        # Skip-listed fields should not appear
        assert "postingStatus" not in md
        assert "jobApplicationTemplateId" not in md
        assert "postingThemeOverrideBoardCode" not in md

    def test_handles_missing_site_info(self):
        payload = self._payload()
        payload["siteInfo"] = None
        md = render_dayforce_job(payload)
        # No crash, no namespace/jobBoardCode lines
        assert "clientNamespace" not in md
        # Still renders title + description
        assert md.startswith("# Lead Product Designer\n")
        assert "Body paragraph" in md

    def test_missing_job_data_fallback_header(self):
        md = render_dayforce_job({"jobData": {}, "siteInfo": {}})
        assert md.startswith("# Job Posting\n")


class TestRenderDayforceBoard:
    def _payload(self) -> dict:
        return {
            "siteInfo": {
                "clientNamespace": "tml",
                "jobBoardCode": "atxcandidateportal",
                "cultureCode": "en-US",
            },
            "jobPostings": [
                {
                    "jobPostingId": 1117,
                    "jobTitle": "Director, Software Engineering",
                    "postingLocations": [
                        {"formattedAddress": "Remote", "cityName": "London"}
                    ],
                    "jobDescription": "<p>Lead the engineering squads.</p>",
                },
                {
                    "jobPostingId": 1023,
                    "jobTitle": "Lead Product Designer",
                    "postingLocations": [{"cityName": "Ottawa", "stateCode": "ON"}],
                },
            ],
            "totalCount": 2,
        }

    def test_header_and_metadata(self):
        md = render_dayforce_board(self._payload(), source_url="https://x")
        assert md.startswith("# atxcandidateportal — Job Board (2 open positions)\n")
        assert "- **clientNamespace**: tml" in md
        assert "- **jobBoardCode**: atxcandidateportal" in md
        assert "- **cultureCode**: en-US" in md
        assert "- **boardUrl**: https://x" in md

    def test_posting_lines_with_links(self):
        md = render_dayforce_board(self._payload())
        assert "- **Director, Software Engineering** — Remote" in md
        assert (
            "https://jobs.dayforcehcm.com/en-US/tml/ATXCANDIDATEPORTAL/jobs/1117"
            in md
        )
        # Second posting uses cityName fallback
        assert "- **Lead Product Designer** — Ottawa" in md
        # Description snippet for first job
        assert "Lead the engineering squads." in md

    def test_empty_board(self):
        md = render_dayforce_board({"siteInfo": {}, "jobPostings": [], "totalCount": 0})
        assert "Job Board (0 open positions)" in md


class TestRoundTripFromNextData:
    """Quick end-to-end on the JSON shape we extract from real pages."""

    def test_render_uses_extracted_data(self):
        next_data = {
            "props": {
                "pageProps": {
                    "jobData": {"jobTitle": "Engineer", "jobPostingContent": {}},
                    "dehydratedState": {
                        "queries": [
                            {
                                "queryKey": ["site-info", {}],
                                "state": {
                                    "data": {
                                        "clientNamespace": "tml",
                                        "jobBoardCode": "board",
                                        "cultureCode": "en-US",
                                    }
                                },
                            }
                        ]
                    },
                }
            }
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(next_data)
            + "</script>"
        )
        nd = extract_next_data(html)
        assert nd is not None
        # Mimic fetch_dayforce_job's bundling
        from fetchaller.content.dayforce import (
            _job_data_from_next_data,
            _site_info_from_next_data,
        )
        payload = {
            "jobData": _job_data_from_next_data(nd),
            "siteInfo": _site_info_from_next_data(nd),
        }
        md = render_dayforce_job(payload)
        assert "# Engineer" in md
        assert "- **clientNamespace**: tml" in md
