"""Delta debugging over the combined header/query/field mapping.

Every probe is a live request, so the budget and the memoization are not
optimizations — they bound how much traffic one discovery pass generates.
"""

import pytest

from fetchaller.discovery import minimize as mini


def counting_probe(required: set, *, log=None):
    """Passes only while every name in ``required`` is still present."""

    async def probe(candidate):
        if log is not None:
            log.append(frozenset(candidate))
        return required <= set(candidate)

    return probe


class TestPartition:
    def test_splits_evenly(self):
        assert mini.partition([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

    def test_handles_uneven_splits_without_losing_items(self):
        chunks = mini.partition(list(range(7)), 3)
        assert sum(len(c) for c in chunks) == 7
        assert sorted(x for c in chunks for x in c) == list(range(7))

    def test_never_emits_empty_chunks(self):
        assert all(chunks for chunks in mini.partition([1, 2], 5))

    def test_empty_input(self):
        assert mini.partition([], 3) == []


class TestDdmin:
    async def test_finds_the_single_required_field(self):
        fields = {f"query:p{i}": str(i) for i in range(8)}
        fields["query:base_query"] = "engineer"
        kept, dropped, _ = await mini.ddmin(fields, counting_probe({"query:base_query"}))
        assert set(kept) == {"query:base_query"}
        assert len(dropped) == 8

    async def test_finds_several_required_fields(self):
        fields = {f"field:f{i}": str(i) for i in range(12)}
        needed = {"field:f3", "field:f7"}
        kept, dropped, _ = await mini.ddmin(fields, counting_probe(needed))
        assert needed <= set(kept)
        assert set(dropped) & needed == set()

    async def test_everything_required_keeps_everything(self):
        fields = {f"field:f{i}": str(i) for i in range(6)}
        kept, dropped, _ = await mini.ddmin(fields, counting_probe(set(fields)))
        assert set(kept) == set(fields)
        assert dropped == ()

    async def test_nothing_required_drops_everything(self):
        # Workday minimizes to an empty body: the observed request is all
        # defaults.
        fields = {f"field:f{i}": str(i) for i in range(9)}
        kept, dropped, _ = await mini.ddmin(fields, counting_probe(set()))
        assert kept == {}
        assert len(dropped) == 9

    async def test_a_failing_full_set_is_returned_whole(self):
        # You cannot minimize what does not work, and shipping a shrunken
        # broken request is worse than reporting failure.
        fields = {"a": "1", "b": "2"}

        async def always_fails(_candidate):
            return False

        kept, dropped, probes = await mini.ddmin(fields, always_fails)
        assert kept == fields
        assert dropped == ()
        assert probes == 1

    async def test_preserves_declared_required_names(self):
        fields = {"query:a": "1", "query:b": "2", "query:c": "3"}
        kept, _, _ = await mini.ddmin(
            fields, counting_probe(set()), required=("query:b",)
        )
        assert set(kept) == {"query:b"}

    async def test_ordering_of_the_original_mapping_is_kept(self):
        fields = {"query:z": "1", "query:a": "2", "query:m": "3"}
        kept, _, _ = await mini.ddmin(fields, counting_probe(set(fields)))
        assert list(kept) == ["query:z", "query:a", "query:m"]


class TestBudget:
    async def test_probes_are_bounded(self):
        fields = {f"f{i}": str(i) for i in range(40)}
        # A probe that only passes on the full set forces maximal searching.
        _, _, probes = await mini.ddmin(
            fields, counting_probe(set(fields)), max_probes=5
        )
        assert probes <= 5

    async def test_exhaustion_returns_a_working_subset(self):
        fields = {f"f{i}": str(i) for i in range(30)}
        needed = {"f11"}
        kept, _, _ = await mini.ddmin(fields, counting_probe(needed), max_probes=4)
        assert needed <= set(kept)

    async def test_repeated_candidates_are_memoized_not_re_probed(self):
        log: list = []
        fields = {f"f{i}": str(i) for i in range(10)}
        _, _, probes = await mini.ddmin(fields, counting_probe({"f0"}, log=log))
        assert len(log) == probes  # every logged call spent budget
        assert len(set(log)) == len(log)  # and none was a repeat

    async def test_default_budget_is_pinned(self):
        assert mini.DEFAULT_MAX_PROBES == 48


class TestNamespaces:
    def test_prefixes_are_distinct(self):
        assert len({mini.HEADER, mini.QUERY, mini.FIELD}) == 3

    @pytest.mark.parametrize("prefix", [mini.HEADER, mini.QUERY, mini.FIELD])
    def test_prefixes_end_with_a_separator(self, prefix):
        assert prefix.endswith(":")
