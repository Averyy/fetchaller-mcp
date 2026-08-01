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
