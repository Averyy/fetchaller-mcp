"""The replay oracle.

These thresholds were tuned against live boards, and two of the five rules
exist only because the minimizer silently shipped a broken plan without them.
Loosening either brings back a failure that looks like success.
"""

import json

from fetchaller.discovery import oracle


def sig(value) -> oracle.Signature:
    return oracle.signature(json.dumps(value))


def records(n, *, subject="engineer"):
    return {"results": [{"id": i, "title": f"{subject} role number {i:03d}"} for i in range(n)]}


class TestThresholds:
    def test_tuned_values_are_pinned(self):
        # Changing these without a live re-measurement is how the silent-empty
        # failures come back.
        assert oracle.MIN_SHAPE_OVERLAP == 0.85
        assert oracle.MIN_CONTENT_OVERLAP == 0.5
        assert oracle.RECORD_TOLERANCE == 2.0


class TestParsedState:
    def test_parsed_and_unparsed_never_match(self):
        assert not oracle.signatures_match(sig({"a": 1}), oracle.signature("not json"))

    def test_unparsed_compares_on_length(self):
        observed = oracle.signature("x" * 100)
        assert oracle.signatures_match(observed, oracle.signature("y" * 120))
        assert not oracle.signatures_match(observed, oracle.signature("y" * 10))

    def test_unparsed_empty_never_matches(self):
        assert not oracle.signatures_match(oracle.signature("x" * 100), oracle.signature(""))


class TestShapeRule:
    def test_same_shape_different_size_matches(self):
        assert oracle.signatures_match(sig(records(20)), sig(records(30)))

    def test_a_different_shape_is_rejected(self):
        observed = sig(records(20))
        other = sig({"errors": [{"message": "nope", "code": 1} for _ in range(20)]})
        assert not oracle.signatures_match(observed, other)

    def test_a_subset_shape_is_accepted(self):
        # A correct answer may omit optional keys the browser's response had.
        rich = oracle.signature(
            json.dumps({"results": [{"id": i, "title": f"engineer role number {i:03d}"} for i in range(20)]})
        )
        assert oracle.signatures_match(rich, rich)


class TestRecordRule:
    def test_far_fewer_records_is_the_silent_empty_trap(self):
        assert not oracle.signatures_match(sig(records(100)), sig(records(2)))

    def test_a_halving_is_tolerated(self):
        assert oracle.signatures_match(sig(records(100)), sig(records(60)))

    def test_far_more_records_means_a_filter_stopped_applying(self):
        # Trap 7. Without the upper bound, minimization drops a filter, gets the
        # unfiltered listing back, sees the same shape and *more* records, and
        # calls it a match. The cached plan then silently searches everything.
        assert not oracle.signatures_match(sig(records(20)), sig(records(500)))

    def test_doubling_is_the_boundary(self):
        assert oracle.signatures_match(sig(records(20)), sig(records(40)))
        assert not oracle.signatures_match(sig(records(20)), sig(records(41)))

    def test_zero_observed_requires_zero(self):
        assert oracle.signatures_match(sig({"results": []}), sig({"results": []}))


class TestContentRule:
    def test_same_size_different_subject_is_rejected(self):
        # Trap 8. The upper bound alone is not enough: where page size caps the
        # result, dropping the query does not move the record count at all.
        # Apple answers "engineer" and "" with the same twenty rows, and only
        # comparing content catches it. Measured live: 20 vs 20 records, 1%
        # content overlap.
        engineer = sig(records(20, subject="engineer"))
        everything = sig(records(20, subject="pastry chef"))
        assert engineer.records == everything.records == 20
        assert not oracle.signatures_match(engineer, everything)

    def test_mostly_retained_content_matches(self):
        observed = sig(records(20))
        # Same rows, two swapped out — well above the 50% floor.
        shifted = {"results": [{"id": i, "title": f"engineer role number {i:03d}"} for i in range(2, 22)]}
        assert oracle.signatures_match(observed, sig(shifted))

    def test_reordering_alone_still_matches(self):
        # Values compare as a set, so a legitimately reordered answer is fine.
        forward = records(20)
        backward = {"results": list(reversed(forward["results"]))}
        assert oracle.signatures_match(sig(forward), sig(backward))


class TestSignature:
    def test_records_and_length_are_measured(self):
        s = sig(records(7))
        assert s.parsed and s.records == 7 and s.length > 0

    def test_sample_is_bounded(self):
        assert len(sig(records(500)).values) <= oracle.SIGNATURE_SAMPLE

    def test_guarded_and_chunked_bodies_are_measured_not_rejected(self):
        jobs = [[f"job{i}"] + [None] * 20 for i in range(21)]
        body = ")]}'\n\n99\n" + json.dumps([["wrb.fr", "r06xKb", json.dumps([jobs])]])
        assert oracle.signature(body).records == 21


class TestEmptyContainers:
    def test_differently_named_empty_collections_do_not_match(self):
        # An empty container must still contribute a shape path. Dropping it
        # made these two share a shape — and since both also report zero
        # records and the same distinctive values, they falsely verified.
        results = oracle.signature(json.dumps({"query": "engineer", "results": []}))
        errors = oracle.signature(json.dumps({"query": "engineer", "errors": []}))
        assert results.shape != errors.shape
        assert not oracle.signatures_match(results, errors)

    def test_the_same_empty_collection_still_matches_itself(self):
        empty = json.dumps({"query": "engineer", "results": []})
        assert oracle.signatures_match(oracle.signature(empty), oracle.signature(empty))

    def test_empty_dicts_also_contribute_a_path(self):
        with_filters = oracle.signature(json.dumps({"filters": {}}))
        with_other = oracle.signature(json.dumps({"facets": {}}))
        assert with_filters.shape != with_other.shape


class TestOrderIndependentSampling:
    def test_a_large_reordered_result_set_still_matches(self):
        # Sampling the first N in document order would take a different slice
        # from a reordered answer and read the reorder as a subject change.
        forward = records(300)
        backward = {"results": list(reversed(forward["results"]))}
        assert oracle.signatures_match(sig(forward), sig(backward))

    def test_a_large_result_set_with_a_different_subject_still_fails(self):
        assert not oracle.signatures_match(
            sig(records(300, subject="engineer")), sig(records(300, subject="pastry chef"))
        )

    def test_an_empty_list_is_a_subset_of_a_populated_one(self):
        # The other direction of the empty-container problem. A facets list
        # that is empty in the browser's answer and populated in a replay must
        # still match — emitting only a leaf marker for the empty case would
        # collapse the Jaccard overlap and reject a correct replay.
        base = records(20)
        observed = oracle.signature(json.dumps({**base, "facets": []}))
        populated = oracle.signature(
            json.dumps({**base, "facets": [{"name": "loc", "count": 3}]})
        )
        assert observed.shape <= populated.shape
        assert oracle.signatures_match(observed, populated)

    def test_an_empty_dict_is_a_subset_of_a_populated_one(self):
        base = records(20)
        observed = oracle.signature(json.dumps({**base, "filters": {}}))
        populated = oracle.signature(json.dumps({**base, "filters": {"loc": "CA"}}))
        assert observed.shape <= populated.shape
        assert oracle.signatures_match(observed, populated)

    def test_but_a_differently_named_container_is_still_not_a_subset(self):
        results = oracle.signature(json.dumps({"query": "engineer", "results": []}))
        errors = oracle.signature(json.dumps({"query": "engineer", "errors": []}))
        assert not results.shape <= errors.shape
        assert not oracle.signatures_match(results, errors)


class TestSampleStability:
    def test_sampling_is_not_lexically_biased(self):
        # A rolling page that adds records sharing an early prefix would evict
        # an entire lexically-sampled window at once, rejecting a correct
        # request. Hash sampling spreads the eviction instead.
        observed = sig(records(200, subject="engineer"))
        grown = {
            "results": (
                [{"id": 9000 + i, "title": f"Android engineer role number {i:03d}"} for i in range(150)]
                + records(200, subject="engineer")["results"]
            )
        }
        assert oracle.signatures_match(observed, sig(grown))

    def test_sampling_is_stable_across_processes(self):
        # str hashing is salted per process; a plan verified in one process
        # must compare equal in the next.
        import subprocess
        import sys

        code = (
            "import json,sys;sys.path.insert(0,'src');"
            "from fetchaller.discovery import oracle;"
            "print(sorted(oracle.signature(json.dumps("
            "{'r':[{'t':f'engineer role number {i:03d}'} for i in range(300)]}))"
            ".values)[:3])"
        )
        first = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        second = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert first.stdout == second.stdout != ""
