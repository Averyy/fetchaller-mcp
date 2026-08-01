"""Google careers: batchexecute wire format, URL detection, rendering.

Google's careers data comes from an internal BOQ RPC whose request and
response are both positional and doubly encoded. Nothing has field names, so
the slot indices are pinned here — a silent off-by-one would surface as
missing data rather than an error.
"""

import json

from fetchaller.google_jobs import api, search, url

# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestFReq:
    def test_envelope_shape(self):
        raw = api.build_f_req("r06xKb", [["engineer"]])
        parsed = json.loads(raw)
        assert parsed[0][0][0] == "r06xKb"
        assert parsed[0][0][3] == "generic"

    def test_arguments_are_json_encoded_inside_the_envelope(self):
        # The args are a JSON *string* nested in the outer JSON, not an object.
        parsed = json.loads(api.build_f_req("r06xKb", [["engineer"]]))
        assert isinstance(parsed[0][0][1], str)
        assert json.loads(parsed[0][0][1]) == [["engineer"]]


class TestDecodeResponse:
    def _wrap(self, payload) -> str:
        envelope = [["wrb.fr", "r06xKb", json.dumps(payload), None, None, None, "generic"]]
        return ")]}'\n\n" + json.dumps(envelope)

    def test_strips_xssi_guard_and_double_encoding(self):
        assert api.decode_response(self._wrap([["a"], None, 7, 20])) == [["a"], None, 7, 20]

    def test_null_payload_returns_none(self):
        envelope = [["wrb.fr", "r06xKb", None, None, None, None, "generic"]]
        assert api.decode_response(")]}'\n\n" + json.dumps(envelope)) is None

    def test_garbage_returns_none(self):
        assert api.decode_response("not a response") is None
        assert api.decode_response("") is None


class TestBuildSearchArgs:
    def test_wraps_slots_in_one_more_array(self):
        # The RPC rejects a bare slot list: it expects [[slots]].
        args = api.build_search_args(query="engineer")
        assert isinstance(args, list) and len(args) == 1
        assert isinstance(args[0], list)

    def test_query_locale_and_page_positions(self):
        slots = api.build_search_args(query="engineer", page=3)[0]
        assert slots[api._ARG_QUERY] == "engineer"
        assert slots[api._ARG_LOCALE] == "en-US"
        assert slots[api._ARG_PAGE] == 3

    def test_page_is_one_based(self):
        assert api.build_search_args(page=0)[0][api._ARG_PAGE] == 1

    def test_locations_are_arrays_of_arrays(self):
        # Comma-joining locations makes Google treat them as one fuzzy string.
        slots = api.build_search_args(locations=["Canada", "United States"])[0]
        assert slots[api._ARG_LOCATIONS] == [["Canada"], ["United States"]]

    def test_empty_query_is_null_not_empty_string(self):
        assert api.build_search_args()[0][api._ARG_QUERY] is None

    def test_optional_filters_stay_null_when_unset(self):
        slots = api.build_search_args(query="x")[0]
        assert slots[api._ARG_REMOTE] is None
        assert slots[api._ARG_SORT] is None
        assert slots[api._ARG_EMPLOYMENT] is None

    def test_sort_and_remote_codes(self):
        slots = api.build_search_args(query="x", sort="date", remote_only=True)[0]
        assert slots[api._ARG_SORT] == api.SORT_CODES["date"]
        assert slots[api._ARG_REMOTE] == 1

    def test_employment_codes_are_numeric(self):
        slots = api.build_search_args(query="x", employment_types=["full_time", "intern"])[0]
        assert slots[api._ARG_EMPLOYMENT] == [1, 2]

    def test_unknown_enum_values_are_dropped_not_passed_through(self):
        slots = api.build_search_args(query="x", employment_types=["nonsense"])[0]
        assert slots[api._ARG_EMPLOYMENT] is None


# ---------------------------------------------------------------------------
# Job record accessors
# ---------------------------------------------------------------------------

_JOB = [None] * 21
_JOB[api.JOB_ID] = "92025237427626694"
_JOB[api.JOB_TITLE] = "Product Design Developer, XR"
_JOB[api.JOB_COMPANY_NAME] = "Google"
_JOB[api.JOB_LOCATIONS] = [
    ["San Jose, CA, USA", ["San Jose, CA, USA"], "San Jose", None, "CA", "US"],
    ["Waterloo, ON, Canada", ["Waterloo, ON, Canada"], "Waterloo", None, "ON", "CA"],
]
_JOB[api.JOB_DESCRIPTION] = [None, "<p>Build things.</p>"]
_JOB[api.JOB_MIN_QUALIFICATIONS] = [None, "<ul><li>A degree</li></ul>"]
_JOB[api.JOB_CREATED_TS] = [1784615890, 79000000]
_JOB[api.JOB_UPDATED_TS] = [1784615890, 79000000]


class TestAccessors:
    def test_locations(self):
        assert api.locations(_JOB) == ["San Jose, CA, USA", "Waterloo, ON, Canada"]

    def test_locations_missing(self):
        assert api.locations([None] * 21) == []

    def test_html_field_reads_the_second_slot(self):
        assert api.html_field(_JOB, api.JOB_DESCRIPTION) == "<p>Build things.</p>"

    def test_html_field_missing(self):
        assert api.html_field([None] * 21, api.JOB_DESCRIPTION) == ""

    def test_timestamp_formats_seconds(self):
        assert api.timestamp(_JOB, api.JOB_CREATED_TS) == "2026-07-21"

    def test_timestamp_missing(self):
        assert api.timestamp([None] * 21, api.JOB_CREATED_TS) == ""

    def test_posting_url_needs_no_slug(self):
        assert api.posting_url("92025237427626694").endswith(
            "/jobs/results/92025237427626694"
        )


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_BASE = "https://www.google.com/about/careers/applications"


class TestUrlDetection:
    def test_posting_with_slug(self):
        assert url.extract_google_job_id(f"{_BASE}/jobs/results/92025237427626694-product-x") == (
            "92025237427626694"
        )

    def test_posting_bare_id(self):
        assert url.extract_google_job_id(f"{_BASE}/jobs/results/92025237427626694") == (
            "92025237427626694"
        )

    def test_search(self):
        assert url.extract_google_search(f"{_BASE}/jobs/results?q=engineer&location=Canada") == {
            "title": "engineer",
            "location": "Canada",
        }

    def test_search_without_parameters(self):
        assert url.extract_google_search(f"{_BASE}/jobs/results") == {
            "title": "",
            "location": "",
        }

    def test_google_is_mostly_not_a_job_board(self):
        # Nothing outside the careers prefix may ever route here.
        for other in (
            "https://www.google.com/",
            "https://www.google.com/search?q=jobs",
            "https://www.google.com/about/",
            "https://mail.google.com/jobs/results/123456",
        ):
            assert not url.is_google_job_url(other), other
            assert not url.is_google_search_url(other), other


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRender:
    def test_search_results(self):
        out = search._render_results(
            [_JOB], title="product designer", location="Canada",
            google_total=38, title_filtered=22, location_filtered=0,
        )
        assert "# Google jobs" in out
        assert "1 job shown; dropped 22 by title" in out
        assert "/jobs/results/92025237427626694" in out
        assert "Waterloo, ON, Canada" in out

    def test_googles_loose_count_is_labelled_not_presented_as_the_answer(self):
        out = search._render_results(
            [_JOB], title="product designer", location="Canada",
            google_total=38, title_filtered=22, location_filtered=0,
        )
        assert "38 loose matches" in out
        assert "after checking each posting" in out

    def test_no_loose_count_note_when_nothing_was_dropped(self):
        out = search._render_results(
            [_JOB], title="", location="", google_total=1,
            title_filtered=0, location_filtered=0,
        )
        assert "loose matches" not in out

    def test_detail_sections(self):
        out = search._render_job(_JOB)
        assert "## About the job" in out
        assert "## Minimum qualifications" in out
        assert "**Source**:" in out

    def test_markdown_metacharacters_escaped(self):
        job = list(_JOB)
        job[api.JOB_TITLE] = "Designer [L5] *Remote*"
        out = search._render_results(
            [job], title="", location="", google_total=1,
            title_filtered=0, location_filtered=0,
        )
        assert "\\[L5\\]" in out
