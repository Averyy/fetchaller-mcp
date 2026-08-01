"""metacareers.com URL detection, SSR extraction, and search-input shaping.

Two behaviours are pinned here because getting them wrong fails silently
rather than loudly: the office name must be the *display* name (Meta answers
an unrecognised office with the unfiltered board, not an error), and the
persisted-query ids must stay discoverable rather than hardcoded.
"""

from fetchaller.meta_careers import api, url

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_job(self):
        u = "https://www.metacareers.com/jobs/733224206480023/"
        assert url.is_meta_job_url(u)
        assert url.extract_meta_job_id(u) == "733224206480023"

    def test_job_without_trailing_slash(self):
        assert url.extract_meta_job_id("https://www.metacareers.com/jobs/733224206480023") == (
            "733224206480023"
        )

    def test_apex_host(self):
        assert url.is_meta_job_url("https://metacareers.com/jobs/733224206480023/")

    def test_index(self):
        assert url.is_meta_jobs_index_url("https://www.metacareers.com/jobs")
        assert url.is_meta_jobs_index_url("https://www.metacareers.com/jobsearch/")
        assert not url.is_meta_job_url("https://www.metacareers.com/jobs")

    def test_other_host_rejected(self):
        assert not url.is_meta_job_url("https://www.facebook.com/jobs/123456789")


# ---------------------------------------------------------------------------
# Search input
# ---------------------------------------------------------------------------


class TestBuildSearchInput:
    def test_carries_every_key_the_board_sends(self):
        payload = api.build_search_input(query="engineer", offices=["Vancouver, Canada"])
        assert payload["q"] == "engineer"
        assert payload["offices"] == ["Vancouver, Canada"]
        # Meta's client always sends the full key set; omitting keys changes
        # the persisted query's shape.
        for key in (
            "divisions",
            "roles",
            "leadership_levels",
            "saved_jobs",
            "saved_searches",
            "sub_teams",
            "teams",
            "is_leadership",
            "is_remote_only",
            "sort_by_new",
            "results_per_page",
        ):
            assert key in payload

    def test_defaults_are_empty_not_missing(self):
        payload = api.build_search_input()
        assert payload["q"] == ""
        assert payload["offices"] == []
        assert payload["is_remote_only"] is False


# ---------------------------------------------------------------------------
# Embedded JSON extraction
# ---------------------------------------------------------------------------


class TestExtractJsonObject:
    def test_reads_one_balanced_object(self):
        text = 'prefix {"a": {"b": 1}, "c": 2} suffix'
        assert api._extract_json_object(text, text.index("{")) == {"a": {"b": 1}, "c": 2}

    def test_braces_inside_strings_do_not_confuse_it(self):
        text = '{"title": "Manager {special}", "id": 1}'
        assert api._extract_json_object(text, 0) == {"title": "Manager {special}", "id": 1}

    def test_escaped_quote_inside_string(self):
        text = '{"title": "The \\"Best\\" Job"}'
        assert api._extract_json_object(text, 0) == {"title": 'The "Best" Job'}

    def test_unbalanced_returns_none(self):
        assert api._extract_json_object('{"a": 1', 0) is None


class TestDocIdDiscovery:
    def test_pattern_matches_metas_module_shape(self):
        import re

        bundle = (
            '__d("CareersJobSearchResultsV2DataQuery_candidate_portalRelayOperation",[],'
            '(function(t,n,r,o,a,i){a.exports="27129360303422352"}),null);'
        )
        pattern = re.compile(
            api._DOC_ID_TEMPLATE.format(operation=re.escape(api.SEARCH_OPERATION)),
            re.DOTALL,
        )
        match = pattern.search(bundle)
        assert match and match.group(1) == "27129360303422352"

    def test_known_ids_exist_as_a_starting_guess(self):
        # These are a starting point only — a miss triggers rediscovery — but
        # every operation the client uses must have one.
        for operation in (api.SEARCH_OPERATION, api.FILTERS_OPERATION, api.LOCATIONS_OPERATION):
            assert api._KNOWN_DOC_IDS[operation].isdigit()


class TestJobPostingJsonLd:
    """Meta publishes schema.org JobPosting on posting pages for search engines.

    That surface is far less build-coupled than the internal
    xcp_requisition_job_description blob, so it is preferred for the standard
    fields; the internal object still supplies teams and compensation.
    """

    def test_extracts_the_jobposting_object(self):
        html = (
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"JobPosting","title":"Network Engineer"}'
            "</script>"
        )
        assert api._parse_job_posting_ld(html)["title"] == "Network Engineer"

    def test_ignores_other_ld_types(self):
        html = (
            '<script type="application/ld+json">{"@type":"Organization","name":"Meta"}</script>'
            '<script type="application/ld+json">{"@type":"JobPosting","title":"Real"}</script>'
        )
        assert api._parse_job_posting_ld(html)["title"] == "Real"

    def test_handles_an_ld_array(self):
        html = (
            '<script type="application/ld+json">'
            '[{"@type":"Organization"},{"@type":"JobPosting","title":"In Array"}]'
            "</script>"
        )
        assert api._parse_job_posting_ld(html)["title"] == "In Array"

    def test_malformed_ld_is_skipped_not_fatal(self):
        html = (
            '<script type="application/ld+json">{not json}</script>'
            '<script type="application/ld+json">{"@type":"JobPosting","title":"Good"}</script>'
        )
        assert api._parse_job_posting_ld(html)["title"] == "Good"

    def test_absent_returns_none(self):
        assert api._parse_job_posting_ld("<html><body>nothing</body></html>") is None


class TestLocationsFromLd:
    def test_builds_readable_locations(self):
        posting = {
            "jobLocation": [
                {
                    "address": {
                        "addressLocality": "Vancouver",
                        "addressRegion": "BC",
                        "addressCountry": "Canada",
                    }
                }
            ]
        }
        assert api._locations_from_ld(posting) == ["Vancouver, BC, Canada"]

    def test_single_object_not_a_list(self):
        posting = {"jobLocation": {"address": {"addressLocality": "Menlo Park"}}}
        assert api._locations_from_ld(posting) == ["Menlo Park"]

    def test_deduplicates(self):
        entry = {"address": {"addressLocality": "Menlo Park"}}
        assert api._locations_from_ld({"jobLocation": [entry, entry]}) == ["Menlo Park"]

    def test_missing_returns_empty(self):
        assert api._locations_from_ld({}) == []
