"""Unit tests for the shared job title/location matching helpers.

These encode the behaviour every board client depends on: boards rank rather
than filter, so a caller's title and location are re-applied here, and the
board's own spelling of a place is never the caller's spelling.
"""

from fetchaller import jobfilter


class TestTokens:
    def test_drops_stopwords_and_punctuation(self):
        assert jobfilter.tokens("Director of Product Design") == [
            "director",
            "product",
            "design",
        ]

    def test_keeps_technical_symbols(self):
        assert jobfilter.tokens("C++ / C# Engineer") == ["c++", "c#", "engineer"]

    def test_empty(self):
        assert jobfilter.tokens("") == []
        assert jobfilter.tokens(None) == []


class TestTitleMatches:
    def test_exact_words_in_longer_title(self):
        wanted = jobfilter.tokens("software engineer")
        assert jobfilter.title_matches("Senior Software Engineer, Infra", wanted)

    def test_requires_every_token(self):
        wanted = jobfilter.tokens("product designer")
        # The role Microsoft's board actually returned for this query.
        assert not jobfilter.title_matches("Cloud Solution Architect - Factory", wanted)

    def test_prefix_match_recovers_word_forms(self):
        wanted = jobfilter.tokens("product designer")
        # "designer" must still match a title spelled "Design".
        assert jobfilter.title_matches("Director of Product Design", wanted)

    def test_short_token_must_match_exactly(self):
        wanted = jobfilter.tokens("ux designer")
        # "ux" is below the prefix threshold, so it cannot match "UXO".
        assert jobfilter.title_matches("Senior UX Designer", wanted)
        assert not jobfilter.title_matches("Senior Product Design Manager", wanted)

    def test_empty_wanted_matches_everything(self):
        assert jobfilter.title_matches("Anything At All", [])

    def test_empty_title_never_matches(self):
        assert not jobfilter.title_matches("", jobfilter.tokens("engineer"))


class TestLocationMatches:
    def test_city_inside_board_specific_formats(self):
        wanted = jobfilter.tokens("Toronto")
        for value in ("Canada, Toronto", "Canada - Toronto", "Toronto, Ontario, CAN"):
            assert jobfilter.location_matches(value, wanted), value

    def test_country_matches_composite_values(self):
        wanted = jobfilter.tokens("Canada")
        assert jobfilter.location_matches("Canada Ontario Remote", wanted)
        assert not jobfilter.location_matches("Vancouver, WA", wanted)


class TestFilterByTitle:
    def test_reports_drop_count(self):
        items = [{"t": "Product Designer"}, {"t": "Software Engineer"}]
        kept, dropped = jobfilter.filter_by_title(items, lambda i: i["t"], "product designer")
        assert [i["t"] for i in kept] == ["Product Designer"]
        assert dropped == 1

    def test_no_title_keeps_everything(self):
        items = [{"t": "A"}, {"t": "B"}]
        kept, dropped = jobfilter.filter_by_title(items, lambda i: i["t"], "")
        assert len(kept) == 2
        assert dropped == 0


class TestBroadenedQuery:
    def test_stems_agentive_suffix(self):
        # The case that matters: Microsoft's board returns 6 hits for
        # "product designer" and 864 for "product design".
        assert jobfilter.broadened_query("product designer") == "product design"

    def test_stems_plural_then_suffix(self):
        assert jobfilter.broadened_query("designers") == "design"

    def test_returns_none_when_nothing_changes(self):
        assert jobfilter.broadened_query("product design") is None
        assert jobfilter.broadened_query("") is None

    def test_never_stems_below_minimum_length(self):
        # "ios" must survive intact rather than becoming "io".
        assert jobfilter.broadened_query("ios") is None


class TestCountryAlpha3:
    def test_bare_name(self):
        assert jobfilter.country_alpha3("Canada") == "CAN"

    def test_trailing_country_in_longer_string(self):
        assert jobfilter.country_alpha3("Toronto, Ontario, Canada") == "CAN"

    def test_existing_code_passes_through(self):
        assert jobfilter.country_alpha3("CAN") == "CAN"

    def test_unknown_returns_empty(self):
        assert jobfilter.country_alpha3("Narnia") == ""
        assert jobfilter.country_alpha3("") == ""


class TestStripCountryTokens:
    def test_removes_country_word_once_filtered_server_side(self):
        wanted = jobfilter.tokens("Vancouver, British Columbia, Canada")
        assert jobfilter.strip_country_tokens(wanted, "CAN") == [
            "vancouver",
            "british",
            "columbia",
        ]

    def test_removes_alpha3_form_too(self):
        assert jobfilter.strip_country_tokens(["toronto", "can"], "CAN") == ["toronto"]

    def test_country_only_location_becomes_empty(self):
        assert jobfilter.strip_country_tokens(jobfilter.tokens("Canada"), "CAN") == []

    def test_no_country_leaves_tokens_alone(self):
        assert jobfilter.strip_country_tokens(["toronto"], "") == ["toronto"]


class TestCountsLine:
    """The summary line, which reports two numbers that count different pools.

    Live regression: Microsoft rendered "0 jobs shown of 33 matching; 34
    dropped by the title filter" — 33 was the board's count for Canada and 34
    was the number of postings fetched (a broadened-query retry pushes the
    fetched set past the board's count for the original query). Read as one
    clause it is arithmetic, and the arithmetic is nonsense.
    """

    def test_shown_and_dropped_always_reconcile(self):
        line = jobfilter.counts_line(6, dropped_by_title=34, board_total=72)[0]
        assert "6 jobs shown; dropped 34 by title" in line

    def test_the_boards_number_gets_its_own_sentence(self):
        lines = jobfilter.counts_line(
            0, dropped_by_title=34, board_total=33, board_label="This board"
        )
        assert lines[0] == "_0 jobs shown; dropped 34 by title_"
        assert "This board reported 33 loose matches" in lines[-1]
        # The two numbers must never end up in one subtractable clause.
        assert "of 33" not in "".join(lines)

    def test_a_board_total_inside_the_examined_pool_is_not_repeated(self):
        # 33 shown + 0 dropped already accounts for the board's 33.
        assert len(jobfilter.counts_line(33, board_total=33)) == 1

    def test_an_unfiltered_total_reads_as_pagination_not_as_matches(self):
        lines = jobfilter.counts_line(10, board_total=200, board_scope="in Canada")
        assert "_10 jobs shown_" == lines[0]
        assert "has 200 in Canada for this query" in lines[-1]
        assert "raise `limit`" in lines[-1]
        assert "matching" not in "".join(lines)

    def test_both_filters_are_named(self):
        lines = jobfilter.counts_line(
            2, dropped_by_title=39, dropped_by_location=3, board_total=100
        )
        assert "dropped 39 by title and 3 by location" in lines[0]
        assert "own title and location" in lines[-1]

    def test_singular_job_and_singular_match(self):
        assert jobfilter.counts_line(1)[0] == "_1 job shown_"
        assert "1 loose match " in jobfilter.counts_line(0, dropped_by_title=0, board_total=1)[-1] \
            or "has 1 for this query" in jobfilter.counts_line(0, board_total=1)[-1]

    def test_nothing_extra_when_there_is_nothing_to_add(self):
        assert jobfilter.counts_line(0) == ["_0 jobs shown_"]


class TestCountryAliases:
    """Boards disagree on how to spell a country; it is one constraint.

    Live regression: Google spells the US "New York, NY, USA", so
    location="United States" with strict matching dropped all 60 of its US
    postings while location="Canada" matched "Waterloo, ON, Canada" fine.
    The asymmetry was the tell.
    """

    def test_the_country_name_matches_the_code(self):
        assert jobfilter.location_matches("New York, NY, USA", jobfilter.tokens("United States"))
        assert jobfilter.location_matches("Sunnyvale, CA, USA", jobfilter.tokens("United States"))

    def test_the_code_matches_the_name(self):
        assert jobfilter.location_matches("London, United Kingdom", jobfilter.tokens("GBR"))

    def test_the_previously_working_direction_still_works(self):
        assert jobfilter.location_matches("Waterloo, ON, Canada", jobfilter.tokens("Canada"))

    def test_a_city_plus_a_respelled_country_still_needs_the_city(self):
        assert jobfilter.location_matches("New York, NY, USA", jobfilter.tokens("New York, United States"))
        assert not jobfilter.location_matches("Austin, TX, USA", jobfilter.tokens("New York, United States"))

    def test_a_different_country_does_not_match(self):
        assert not jobfilter.location_matches("Austin, TX, USA", jobfilter.tokens("Canada"))
        assert not jobfilter.location_matches("Toronto, ON, Canada", jobfilter.tokens("United States"))

    def test_a_short_code_does_not_match_a_longer_word(self):
        # "us" must not hit "Houston" or similar.
        assert not jobfilter.location_matches("Houston, Texas", jobfilter.tokens("US"))


class TestTruncationDisclosure:
    """`limit` sizes the output; it must never silently hide matches."""

    def test_matches_that_did_not_fit_are_named(self):
        line = jobfilter.counts_line(3, dropped_by_title=136, truncated_by_limit=4)[0]
        assert "3 jobs shown of 7 matched (raise `limit` for the rest)" in line
        assert "dropped 136 by title" in line

    def test_nothing_is_added_when_everything_fitted(self):
        assert jobfilter.counts_line(7, dropped_by_title=136)[0] == (
            "_7 jobs shown; dropped 136 by title_"
        )

    def test_an_unexamined_remainder_is_stated(self):
        # Apple: 1510 loose matches, a 100-posting window, "13 jobs shown".
        lines = jobfilter.counts_line(
            13, dropped_by_title=87, board_total=1510, examined=100,
            board_label="Apple's board",
        )
        body = "\n".join(lines)
        assert "the first 100 examined" in body
        assert "remaining 1410 were not examined" in body

    def test_a_fully_examined_board_says_nothing_about_a_window(self):
        lines = jobfilter.counts_line(
            21, dropped_by_title=8, board_total=28, examined=29, board_label="This board"
        )
        body = "\n".join(lines)
        assert "examined" not in body
        assert "each posting's own title" in body
