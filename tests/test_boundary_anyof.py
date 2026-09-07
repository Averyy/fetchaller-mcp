"""The anyOf contract: presence is required, non-blankness is not."""

from fetchaller.server import _validate_tool_arguments as validate


class TestPartialFilters:
    """A client that sends "" for unfilled fields must still be able to search.

    Regression: making every anyOf member non-blank rejected
    `{title: "nurse", location: ""}` — a call the published schema accepts,
    since anyOf is presence-based with no minLength. Three tools were affected;
    search_gojobs worst, with five fields in its any-set.
    """

    def test_one_real_value_and_one_blank_is_accepted(self):
        assert validate("search_jobbank", {"title": "nurse", "location": ""}) is None
        assert validate("search_indeed", {"title": "", "location": "Toronto"}) is None
        assert validate("search_gojobs", {"title": "analyst", "min_salary": ""}) is None

    def test_all_blank_is_still_the_board_dump_it_was_meant_to_stop(self):
        for args in ({"title": "", "location": ""}, {}):
            error = validate("search_jobbank", args)
            assert error and "at least one of" in error

    def test_a_flat_required_field_may_still_not_be_blank(self):
        assert validate("get_gojobs_job", {"job_id": ""}) is not None
        assert validate("search_workday_jobs", {"employer": ""}) is not None

    def test_an_optional_blank_is_untouched(self):
        # The original Workday fix: title="" means "no filter".
        assert validate("search_workday_jobs", {"employer": "acme", "title": ""}) is None
