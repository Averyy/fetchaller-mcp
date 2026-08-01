"""jobs.apple.com URL detection, API contract, hydration parsing, rendering.

The JSON API is the primary surface and the SSR hydration blob is the
fallback, so both are covered. The hydration blob is a JS string literal
handed to ``JSON.parse``, meaning it is escaped twice — the parsing tests pin
that down, including an escaped quote appearing before the real terminator.
"""

import json

from fetchaller.apple_jobs import api, render, url

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_details(self):
        u = "https://jobs.apple.com/en-ca/details/200674861/staff-ml-engineer"
        assert url.is_apple_job_url(u)
        assert url.extract_apple_job(u) == ("200674861", "staff-ml-engineer")
        assert url.extract_locale(u) == "en-ca"

    def test_details_without_slug(self):
        assert url.extract_apple_job("https://jobs.apple.com/en-ca/details/200674861") == (
            "200674861",
            "",
        )

    def test_search(self):
        u = "https://jobs.apple.com/en-ca/search?search=engineer&location=toronto-TOR"
        assert url.is_apple_search_url(u)
        assert url.extract_apple_search(u) == {
            "search": "engineer",
            "location": "toronto-TOR",
        }

    def test_search_without_parameters(self):
        assert url.extract_apple_search("https://jobs.apple.com/en-ca/search") == {
            "search": "",
            "location": "",
        }

    def test_incomplete_details_rejected(self):
        assert not url.is_apple_job_url("https://jobs.apple.com/en-ca/details/")

    def test_other_host_rejected(self):
        assert not url.is_apple_job_url("https://apple.com/en-ca/details/200674861/x")


class TestLocationSlug:
    def test_builds_url_form(self):
        assert api.location_slug("Toronto", "postLocation-TOR") == "toronto-TOR"

    def test_multiword_name(self):
        assert api.location_slug("Vancouver Metro Area", "postLocation-VANC") == (
            "vancouver-metro-area-VANC"
        )

    def test_missing_code_yields_nothing(self):
        assert api.location_slug("Toronto", "") == ""


class TestLocationFilterId:
    def test_url_form_to_api_form(self):
        assert api.location_filter_id("toronto-TOR") == "postLocation-TOR"

    def test_country_code(self):
        assert api.location_filter_id("canada-CANC") == "postLocation-CANC"

    def test_multiword_slug_keeps_only_the_code(self):
        assert api.location_filter_id("vancouver-metro-area-VANC") == "postLocation-VANC"

    def test_empty(self):
        assert api.location_filter_id("") == ""


class TestNormalizeLocale:
    def test_valid(self):
        assert api.normalize_locale("en-us") == "en-us"

    def test_falls_back_on_garbage(self):
        assert api.normalize_locale("nonsense") == api.DEFAULT_LOCALE
        assert api.normalize_locale("") == api.DEFAULT_LOCALE


# ---------------------------------------------------------------------------
# Hydration parsing
# ---------------------------------------------------------------------------


def _page(payload: dict) -> str:
    """Wrap a payload the way Apple's SSR does: JSON inside a JS string literal."""
    inner = json.dumps(payload)
    literal = json.dumps(inner)  # adds the outer quotes and escapes
    return (
        "<html><body><script>window.__staticRouterHydrationData = "
        f"JSON.parse({literal});</script></body></html>"
    )


class TestParseHydration:
    def test_round_trip(self):
        payload = {"loaderData": {"search": {"totalRecords": 3, "searchResults": []}}}
        assert api.parse_hydration(_page(payload)) == payload

    def test_survives_escaped_quotes_in_content(self):
        # A posting title containing a quote produces an escaped `\"` in the
        # literal, which must not be mistaken for the closing delimiter.
        payload = {"loaderData": {"search": {"searchResults": [{"postingTitle": 'The "Best" Job'}]}}}
        parsed = api.parse_hydration(_page(payload))
        assert parsed["loaderData"]["search"]["searchResults"][0]["postingTitle"] == (
            'The "Best" Job'
        )

    def test_missing_marker_returns_none(self):
        assert api.parse_hydration("<html><body>nothing here</body></html>") is None

    def test_malformed_payload_returns_none(self):
        broken = '<script>window.__staticRouterHydrationData = JSON.parse("{not json}");</script>'
        assert api.parse_hydration(broken) is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_JOB = {
    "id": "200674861-3350",
    "positionId": "200674861",
    "postingTitle": "Staff Machine Learning Engineer",
    "transformedPostingTitle": "staff-machine-learning-engineer",
    "locations": [{"name": "Toronto", "countryName": "Canada"}],
    "team": {"teamName": "Software and Services"},
    "postingDate": "Jul 29, 2026",
    "reqId": "200674861-3965",
    "jobSummary": "Work on Apple News.",
}


class TestRender:
    def test_search_results(self):
        out = render.render_search_results([_JOB], locale="en-ca", title="engineer", total=3)
        assert "# Apple jobs" in out
        assert (
            "https://jobs.apple.com/en-ca/details/200674861/staff-machine-learning-engineer" in out
        )
        assert "**Location**: Toronto" in out
        assert "**Team**: Software and Services" in out

    def test_unmatched_location_is_flagged(self):
        out = render.render_search_results(
            [], locale="en-ca", location="Narnia", location_applied=False
        )
        assert "no location matching" in out

    def test_job_detail_sections(self):
        job = dict(_JOB, jobDescription="<p>Body</p>", minimumQualifications="<p>Min</p>")
        out = render.render_job(job, locale="en-ca")
        assert "## Description" in out
        assert "## Minimum qualifications" in out
        assert "**Source**:" in out


class TestApiRequestContract:
    """Apple's JSON API returns 200 with totalRecords: 0 when `format` is
    missing, which reads as "no jobs" rather than "bad request". These pin the
    body shape so nobody removes the field while tidying up."""

    def _body(self, monkeypatch, **kwargs):
        captured = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"res": {"searchResults": [], "totalRecords": 0}}

        class _Session:
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["body"] = json
                captured["headers"] = headers
                return _Resp()

        import asyncio

        asyncio.run(api.search_api_page(session=_Session(), **kwargs))
        return captured

    def test_format_is_always_sent(self, monkeypatch):
        captured = self._body(monkeypatch, search="engineer")
        assert "format" in captured["body"], "omitting `format` silently returns zero results"
        assert captured["body"]["format"]

    def test_posts_to_the_search_endpoint(self, monkeypatch):
        captured = self._body(monkeypatch, search="engineer")
        assert captured["url"].endswith("/api/v1/search")
        assert captured["headers"]["content-type"] == "application/json"

    def test_location_is_sent_in_api_form(self, monkeypatch):
        captured = self._body(monkeypatch, search="engineer", location="toronto-TOR")
        assert captured["body"]["filters"] == {"locations": ["postLocation-TOR"]}

    def test_no_location_means_no_filter(self, monkeypatch):
        captured = self._body(monkeypatch, search="engineer")
        assert captured["body"]["filters"] == {}

    def test_page_is_one_based(self, monkeypatch):
        captured = self._body(monkeypatch, search="engineer", page=0)
        assert captured["body"]["page"] == 1


class TestJobLocations:
    """Used to enforce a location the board itself could not resolve."""

    def test_flattens_every_location_field(self):
        from fetchaller.apple_jobs.search import _job_locations

        job = {"locations": [{"name": "Toronto", "countryName": "Canada", "city": ""}]}
        flat = _job_locations(job)
        assert "Toronto" in flat and "Canada" in flat

    def test_multiple_locations(self):
        from fetchaller.apple_jobs.search import _job_locations

        job = {"locations": [{"name": "Toronto"}, {"name": "Vancouver"}]}
        flat = _job_locations(job)
        assert "Toronto" in flat and "Vancouver" in flat

    def test_missing_locations(self):
        from fetchaller.apple_jobs.search import _job_locations

        assert _job_locations({}) == ""
