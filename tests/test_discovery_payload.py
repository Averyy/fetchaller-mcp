"""Payload decoding and structure metrics.

Every shape here was measured on a live board, and each one reads as *zero
records* if it is mishandled — which is the failure this package exists to
prevent, because zero records is indistinguishable from an empty board.
"""

import json

from fetchaller.discovery import payload as p


class TestGuards:
    def test_strips_googles_guard(self):
        assert p.strip_json_guard(")]}'\n[1,2]") == "[1,2]"

    def test_strips_the_other_known_guards(self):
        for guard in (")]}", "while(1);", "for (;;);", "for(;;);"):
            assert p.strip_json_guard(guard + '\n{"a":1}') == '{"a":1}'

    def test_leaves_unguarded_text_alone(self):
        assert p.strip_json_guard('{"a":1}') == '{"a":1}'


class TestDecode:
    def test_plain_json(self):
        assert p.decode_payload('{"a":1}') == {"a": 1}

    def test_ndjson_takes_the_first_line(self):
        # Meta's GraphQL responses are newline-delimited.
        assert p.decode_payload('{"data":1}\n{"more":2}') == {"data": 1}

    def test_chunk_stream_skips_the_bare_length(self):
        # batchexecute interleaves a bare number with each array. Decoding the
        # length instead of the array reports a scalar and therefore no records.
        assert p.decode_payload("1234\n[[1,2,3],[4,5,6]]") == [[1, 2, 3], [4, 5, 6]]

    def test_garbage_is_none_not_an_exception(self):
        assert p.decode_payload("not json at all") is None
        assert p.decode_payload("") is None


class TestNested:
    def test_follows_json_inside_a_string(self):
        assert p.nested('[1, 2, 3, 4]') == [1, 2, 3, 4]

    def test_ignores_plain_strings(self):
        assert p.nested("Product Designer") is None

    def test_ignores_scalars_and_short_strings(self):
        assert p.nested(7) is None
        assert p.nested("[1]") is None  # under the length floor


class TestIsRecordList:
    def test_amazon_facet_dicts_are_not_records(self):
        # One key each, and a *different* key each. Counting these as a record
        # set makes the facet array outrank the postings.
        facets = [
            {"job_function_corporate_80rdb4": 6286},
            {"city_toronto_x": 12},
            {"region_on_y": 3},
        ]
        assert not p.is_record_list(facets)

    def test_dicts_sharing_two_keys_are_records(self):
        assert p.is_record_list([{"id": 1, "t": "a"}, {"id": 2, "t": "b"}])

    def test_positional_arrays_of_equal_width_are_records(self):
        # Google's job records are 21-slot arrays with no field names.
        assert p.is_record_list([[1, 2, 3], [4, 5, 6]])

    def test_ragged_positional_arrays_are_not(self):
        assert not p.is_record_list([[1, 2, 3], [4, 5]])

    def test_narrow_positional_arrays_are_not(self):
        assert not p.is_record_list([[1, 2], [3, 4]])

    def test_single_entry_is_never_a_record_list(self):
        assert not p.is_record_list([{"id": 1, "t": "a"}])


class TestCollectionSize:
    def test_finds_the_largest_homogeneous_list(self):
        value = {"small": [{"a": 1, "b": 2}] * 3, "big": [{"a": 1, "b": 2}] * 70}
        assert p.collection_size(value) == 70

    def test_follows_json_nested_in_a_string(self):
        # Google hides 21 job records inside a JSON string. Without following
        # it the payload measures as 3 records instead.
        jobs = [[str(i)] + [None] * 20 for i in range(21)]
        assert p.collection_size([["wrb.fr", "r06xKb", json.dumps([jobs])]]) == 21

    def test_workday_video_labels_really_are_records(self):
        # 334 homogeneous entries. Nothing structural excludes them — only the
        # query hint separates them from the 70 real postings.
        labels = [{"key": f"WDRES.{i}", "label": "Close"} for i in range(334)]
        assert p.collection_size({"body": labels}) == 334

    def test_amazon_facets_measure_zero(self):
        facets = [{f"facet_{i}": i} for i in range(50)]
        assert p.collection_size({"facets": facets}) == 0


class TestShape:
    def test_collapses_array_indices(self):
        # Containers contribute their own path as well as their children's, so
        # an empty container's shape is a proper subset of a populated one's.
        assert p.json_shape({"d": {"r": [{"id": 1, "t": "x"}]}}) == frozenset(
            {"d", "d.r[]", "d.r[].id", "d.r[].t"}
        )

    def test_an_empty_container_still_has_a_path(self):
        assert p.json_shape({"results": []}) == frozenset({"results[]"})
        assert p.json_shape({"filters": {}}) == frozenset({"filters"})

    def test_an_empty_containers_shape_is_a_subset_of_a_populated_one(self):
        empty = p.json_shape({"results": []})
        full = p.json_shape({"results": [{"id": 1}]})
        assert empty < full

    def test_the_top_level_container_needs_no_marker_of_its_own(self):
        assert "" not in p.json_shape({"a": 1})

    def test_size_does_not_change_shape(self):
        one = p.json_shape({"r": [{"id": 1}]})
        many = p.json_shape({"r": [{"id": i} for i in range(200)]})
        assert one == many

    def test_bounded_recursion_terminates(self):
        deep = current = {}
        for _ in range(50):
            current["next"] = current = {}
        assert isinstance(p.json_shape(deep), frozenset)


class TestDistinctiveValues:
    def test_document_order_is_preserved(self):
        value = {"a": "first value here", "b": "second value here"}
        assert p.distinctive_values(value) == ["first value here", "second value here"]

    def test_urls_and_data_uris_are_excluded(self):
        # They appear in page and payload regardless of subject, so they
        # inflate coverage without evidencing anything.
        value = ["https://example.com/x", "/relative/path", "data:image/png;base64,AAAA"]
        assert p.distinctive_values(value) == []

    def test_short_and_very_long_strings_are_excluded(self):
        assert p.distinctive_values(["tiny", "x" * 200]) == []

    def test_deduplicates(self):
        assert p.distinctive_values(["repeated value", "repeated value"]) == ["repeated value"]

    def test_limit_truncates(self):
        values = [f"distinct value {i}" for i in range(100)]
        assert len(p.distinctive_values(values, limit=40)) == 40

    def test_head_window_constant_is_forty(self):
        # Coverage is measured over the first forty because a listing renders
        # the top of its list and virtualizes the rest.
        assert p.HEAD_WINDOW == 40


class TestFlightDecoding:
    """React Flight framing, implemented but not wired into decode_payload.

    Enabling it made discovery confidently wrong about Uber — the largest
    record set in its Flight stream is a 16-entry navigation menu, identical on
    the detail and list pages. See `decode_flight`'s docstring for the numbers.
    """

    FLIGHT = (
        '1:"$Sreact.fragment"\n'
        '2:I[54371,["/chunk.js"],"default"]\n'
        '3:{"jobs":[{"id":"300543","title":"Engineer"},{"id":"300886","title":"Analyst"}]}\n'
        # Ta, == hex 0xa == 10 characters. The prefix is hex, not decimal.
        '4:Ta,0123456789\n'
    )

    def test_recognized_by_shape(self):
        assert p.looks_like_flight(self.FLIGHT)

    def test_plain_json_is_not_flight(self):
        assert not p.looks_like_flight('{"a": [1, 2, 3]}')

    def test_decodes_every_row_not_just_the_first(self):
        # The parser is used with .match(text, pos); a `^` anchor silently
        # matched only row 1 and reported a 383 KB document as one row.
        rows = p.decode_flight(self.FLIGHT)
        assert set(rows) == {"1", "3", "4"}  # module row 2 is transport

    def test_length_prefixed_text_rows_are_read_by_length(self):
        assert p.decode_flight(self.FLIGHT)["4"] == "0123456789"

    def test_application_records_are_still_found(self):
        assert p.collection_size(p.decode_flight(self.FLIGHT)) == 2

    def test_react_element_trees_are_not_records(self):
        # ["$", type, key, props] is markup. Counting it made every Next.js
        # page look like it carried a record set.
        elements = [
            ["$", "title", "0", {"children": "Senior Developer"}],
            ["$", "div", "1", {"children": "x"}],
            ["$", "span", "2", {"children": "y"}],
        ]
        assert not p.is_record_list(elements)

    def test_genuine_positional_records_still_count(self):
        # Google's 21-slot job arrays must survive the React exclusion.
        assert p.is_record_list([["a", 1, 2], ["b", 3, 4]])

    def test_decode_payload_does_not_use_it(self):
        # Deliberate: wiring it in produced a false positive on the only board
        # available to test against.
        assert p.decode_payload(self.FLIGHT) is None
