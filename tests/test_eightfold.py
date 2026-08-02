"""URL detection, generation normalisation, and rendering for Eightfold boards.

Live tenant calls are exercised manually; this covers the parsing and shaping
that has to stay stable — in particular that the two Eightfold generations
(PCS-X and classic) come out of the client with one field vocabulary.
"""

from fetchaller.eightfold import api, render, url

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_eightfold_ai_board(self):
        u = "https://paypal.eightfold.ai/careers"
        assert url.is_eightfold_board_url(u)
        assert not url.is_eightfold_job_url(u)

    def test_vanity_host_board(self):
        u = "https://apply.careers.microsoft.com/careers"
        assert url.is_eightfold_board_url(u)
        assert url.board_root(u) == "https://apply.careers.microsoft.com"

    def test_job_path(self):
        u = "https://apply.careers.microsoft.com/careers/job/1970393556750546"
        assert url.is_eightfold_job_url(u)
        assert url.extract_position_id(u) == "1970393556750546"
        assert not url.is_eightfold_board_url(u)

    def test_job_as_board_query_parameter(self):
        # The board links a selected posting as ?pid=, which is still a posting.
        u = "https://apply.careers.microsoft.com/careers?pid=1970393556750546&query=x"
        assert url.extract_position_id(u) == "1970393556750546"
        assert not url.is_eightfold_board_url(u)

    def test_board_with_query_but_no_pid(self):
        u = "https://apply.careers.microsoft.com/careers?query=engineer"
        assert url.is_eightfold_board_url(u)

    def test_unrelated_host_rejected(self):
        assert not url.is_eightfold_board_url("https://example.com/careers")
        assert not url.is_eightfold_job_url("https://example.com/careers/job/123")

    def test_lookalike_host_rejected(self):
        # Must not match a host that merely ends with the brand name.
        assert not url.is_eightfold_host("noteightfold.ai.evil.com")

    def test_non_http_scheme_rejected(self):
        assert not url.is_eightfold_board_url("ftp://paypal.eightfold.ai/careers")


class TestResolveEmployer:
    def test_alias(self):
        assert url.resolve_employer("microsoft") == url.KNOWN_EMPLOYERS["microsoft"]

    def test_alias_is_case_insensitive(self):
        assert url.resolve_employer("Microsoft") == url.KNOWN_EMPLOYERS["microsoft"]

    def test_bare_hostname_gets_careers_path(self):
        assert url.resolve_employer("acme.eightfold.ai") == "https://acme.eightfold.ai/careers"

    def test_full_board_url_preserved(self):
        u = "https://acme.eightfold.ai/careers"
        assert url.resolve_employer(u) == u

    def test_unknown_returns_none(self):
        assert url.resolve_employer("not-a-board") is None
        assert url.resolve_employer("") is None


# ---------------------------------------------------------------------------
# Classic -> PCS-X normalisation
# ---------------------------------------------------------------------------


class TestNormalizeClassic:
    def test_renames_fields_to_pcsx_names(self):
        classic = {
            "id": 790316470001,
            "name": "Post Engineer",
            "display_job_id": "JR41213",
            "ats_job_id": "JR41213",
            "t_create": 1781654400,
            "t_update": 1781654400,
            "work_location_option": "onsite",
            "job_description": "<p>Body</p>",
            "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/1",
        }
        out = api._normalize_classic(classic)
        assert out["displayJobId"] == "JR41213"
        assert out["atsJobId"] == "JR41213"
        assert out["creationTs"] == 1781654400
        assert out["postedTs"] == 1781654400
        assert out["workLocationOption"] == "onsite"
        assert out["jobDescription"] == "<p>Body</p>"
        assert out["publicUrl"].endswith("/careers/job/1")
        assert out["name"] == "Post Engineer"

    def test_repairs_missing_space_after_comma(self):
        # Classic writes "Vancouver,Canada"; PCS-X writes "Vancouver, BC, CA".
        out = api._normalize_classic({"locations": ["Vancouver,Canada"]})
        assert out["locations"] == ["Vancouver, Canada"]

    def test_unknown_fields_survive(self):
        out = api._normalize_classic({"business_unit": "Animation", "custom": 1})
        assert out["businessUnit"] == "Animation"
        assert out["custom"] == 1


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_POSITION = {
    "id": 1970393556750546,
    "name": "Software Engineer II",
    "displayJobId": "200024676",
    "standardizedLocations": ["Vancouver, BC, CA"],
    "department": "Software Engineering",
    "workLocationOption": "onsite",
    "postedTs": 1771542086,
    "positionUrl": "/careers/job/1970393556750546",
}


class TestRender:
    def test_search_results_include_link_and_metadata(self):
        out = render.render_search_results(
            [_POSITION],
            employer="Microsoft",
            board_root="https://apply.careers.microsoft.com",
            query="software engineer",
            location="Vancouver, Canada",
            total=13,
        )
        assert "# Microsoft jobs" in out
        assert "_1 job shown_" in out
        assert "This board has 13 in Vancouver, Canada" in out
        assert "https://apply.careers.microsoft.com/careers/job/1970393556750546" in out
        assert "Vancouver, BC, CA" in out
        assert "200024676" in out

    def test_title_filter_count_is_reported_not_hidden(self):
        out = render.render_search_results(
            [],
            employer="Microsoft",
            board_root="https://apply.careers.microsoft.com",
            query="product designer",
            total=9,
            title_filtered=9,
        )
        assert "dropped 9 by title" in out
        assert "No postings matched." in out

    def test_position_renders_custom_tenant_fields(self):
        position = dict(_POSITION)
        position["efcustomTextWorkSite"] = "3 days / week in-office"
        position["jobDescription"] = "<p>Hello</p>"
        out = render.render_position(
            position, employer="Microsoft", board_root="https://apply.careers.microsoft.com"
        )
        assert "**Work Site**: 3 days / week in-office" in out
        assert "## Description" in out
        assert "Hello" in out

    def test_public_url_wins_over_relative_path(self):
        position = dict(_POSITION, publicUrl="https://example.com/real")
        assert render.posting_url(position, "https://apply.careers.microsoft.com") == (
            "https://example.com/real"
        )

    def test_markdown_metacharacters_escaped(self):
        out = render.render_search_results(
            [dict(_POSITION, name="Engineer [Senior] *Remote*")],
            employer="Microsoft",
            board_root="https://apply.careers.microsoft.com",
        )
        assert "\\[Senior\\]" in out
        assert "\\*Remote\\*" in out


class TestWorkType:
    """`workLocationOption` is not real data on the classic generation.

    Measured across Netflix's board: every posting carries the constant
    "onsite", including reqs whose own location reads "Canada - Remote" and
    "USA - Remote". Work mode is a hard screen, so a field that contradicts
    the location is worse than an absent one.
    """

    def test_a_remote_location_overrides_a_contradicting_onsite(self):
        out = render._work_type({"workLocationOption": "onsite"}, "Canada - Remote")
        assert out.startswith("remote")
        assert "unreliable" in out

    def test_a_genuine_onsite_posting_is_untouched(self):
        assert render._work_type({"workLocationOption": "onsite"}, "Helsinki, Finland") == "onsite"

    def test_a_populated_remote_field_is_kept_as_the_board_wrote_it(self):
        # `_clean` escapes the underscore for markdown; the value is untouched.
        assert render._work_type({"workLocationOption": "remote_local"}, "USA - Remote") == (
            "remote\\_local"
        )

    def test_an_absent_field_with_a_remote_location_still_reports_remote(self):
        assert render._work_type({}, "USA - Remote").startswith("remote")

    def test_an_absent_field_with_no_signal_stays_absent(self):
        assert render._work_type({}, "Redmond, WA") == ""

    def test_the_word_must_stand_alone(self):
        # "Remotely" or a street called "Remote Park" should not trigger it.
        assert render._work_type({"workLocationOption": "onsite"}, "Remoteness Rd") == "onsite"
