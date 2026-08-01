"""jobs.apple.com URL detection, hydration parsing, and rendering.

Apple ships no usable JSON API, so the client reads the SSR hydration blob.
That blob is a JS string literal handed to ``JSON.parse``, meaning it is
escaped twice — the parsing tests below pin that down, including the case
where an escaped quote appears before the real terminator.
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
