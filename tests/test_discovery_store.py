"""Caching a discovered plan.

Discovery costs a browser launch; replay costs one request. The cache is what
makes that difference usable, and the decay check is what stops a rotted plan
from being mistaken for an empty board.
"""

from fetchaller.discovery import store
from fetchaller.discovery.plan import MintStep, RequestPlan


def make_plan(**kwargs) -> RequestPlan:
    defaults = {
        "method": "POST",
        "url": "https://www.metacareers.com/graphql",
        "body_kind": "form",
        "body": "av=0&doc_id=27129360303422352",
        "verified": True,
        "record_count": 588,
    }
    return RequestPlan(**{**defaults, **kwargs})


class TestPaths:
    def test_key_is_slugified_and_hashed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        path = store.plan_path("meta:jobs search")
        assert path.parent == tmp_path
        assert path.name.startswith("meta-jobs-search.")
        assert path.suffix == ".json"

    def test_keys_that_slugify_alike_do_not_collide(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        assert store.plan_path("a b") != store.plan_path("a/b")

    def test_long_keys_are_truncated_but_stay_distinct(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        first = store.plan_path("x" * 200 + "1")
        second = store.plan_path("x" * 200 + "2")
        assert first != second
        assert len(first.name) < 120


class TestRoundTrip:
    def test_save_then_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        plan = make_plan(
            mint=(MintStep(name="LSD", method="GET", url="https://x/", source="regex", selector="t=(.{8,40})"),)
        )
        assert store.save_plan("meta", plan)
        loaded = store.load_plan("meta")
        assert loaded.to_json() == plan.to_json()
        assert loaded.mint[0].name == "LSD"
        assert loaded.record_count == 588

    def test_missing_key_is_none_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        assert store.load_plan("never-saved") is None

    def test_corrupt_plan_is_discarded_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        path = store.plan_path("broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert store.load_plan("broken") is None

    def test_forget(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan())
        assert store.forget_plan("meta")
        assert store.load_plan("meta") is None

    def test_no_temporary_files_are_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan())
        assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


class TestDecay:
    def test_a_collapsed_record_count_reads_as_decay(self):
        # Measured on Meta: the healthy plan returns 588 records; the same
        # request with doc_id incremented by one returns HTTP 200 and 1 record.
        plan = make_plan(record_count=588)
        assert store.looks_decayed(plan, 1)

    def test_a_normal_fluctuation_does_not(self):
        plan = make_plan(record_count=588)
        assert not store.looks_decayed(plan, 500)
        assert not store.looks_decayed(plan, 600)

    def test_the_floor_is_half(self):
        assert store.DECAY_FLOOR == 0.5
        plan = make_plan(record_count=100)
        assert not store.looks_decayed(plan, 50)
        assert store.looks_decayed(plan, 49)

    def test_a_plan_with_no_recorded_count_never_reads_as_decayed(self):
        assert not store.looks_decayed(make_plan(record_count=0), 0)


class TestResolve:
    async def test_a_verified_cached_plan_is_returned_without_discovery(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan())

        async def explode(*_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("discovery should not have run")

        monkeypatch.setattr("fetchaller.discovery.pipeline.discover", explode)
        plan = await store.resolve_plan_for("meta", "https://www.metacareers.com/jobs")
        assert plan.record_count == 588

    async def test_an_unverified_cached_plan_is_not_trusted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan(verified=False))
        calls: list = []

        async def fake(url, **kwargs):
            calls.append(url)

            class Result:
                plan = None
                reason = "nope"

            return Result()

        monkeypatch.setattr("fetchaller.discovery.pipeline.discover", fake)
        assert await store.resolve_plan_for("meta", "https://x/") is None
        assert calls == ["https://x/"]

    async def test_a_freshly_discovered_plan_is_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        discovered = make_plan(record_count=42)

        async def fake(_url, **_kwargs):
            class Result:
                plan = discovered
                reason = ""

            return Result()

        monkeypatch.setattr("fetchaller.discovery.pipeline.discover", fake)
        assert (await store.resolve_plan_for("meta", "https://x/")).record_count == 42
        assert store.load_plan("meta").record_count == 42


class TestReplaySelfHeal:
    class FakeResponse:
        def __init__(self, text):
            self.text = text

    def _payload(self, n):
        import json as _json

        return _json.dumps({"results": [{"id": i, "title": f"engineer role {i:03d}"} for i in range(n)]})

    async def test_a_healthy_replay_is_returned_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan(record_count=588))

        async def fake_execute(_s, _p, **_k):
            return self.FakeResponse(self._payload(588))

        monkeypatch.setattr("fetchaller.discovery.plan.execute", fake_execute)
        out = await store.replay(object(), "meta", "https://x/")
        assert out.ok and out.records == 588 and not out.rediscovered

    async def test_a_rotted_plan_triggers_one_rediscovery(self, tmp_path, monkeypatch):
        # Measured on Meta: a rotated doc_id answers HTTP 200 with 1 record
        # instead of 588. It does not error, so only the recorded count catches it.
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan(record_count=588))
        payloads = [self._payload(1), self._payload(588)]

        async def fake_execute(_s, _p, **_k):
            return self.FakeResponse(payloads.pop(0))

        async def fake_discover(_url, **_kwargs):
            class Result:
                plan = make_plan(record_count=588)
                reason = ""

            return Result()

        monkeypatch.setattr("fetchaller.discovery.plan.execute", fake_execute)
        monkeypatch.setattr("fetchaller.discovery.pipeline.discover", fake_discover)
        out = await store.replay(object(), "meta", "https://x/")
        assert out.rediscovered
        assert out.records == 588

    async def test_a_genuinely_empty_board_is_reported_not_faked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan(record_count=588))

        async def fake_execute(_s, _p, **_k):
            return self.FakeResponse(self._payload(0))

        async def fake_discover(_url, **_kwargs):
            class Result:
                plan = None
                reason = "nothing observed"

            return Result()

        monkeypatch.setattr("fetchaller.discovery.plan.execute", fake_execute)
        monkeypatch.setattr("fetchaller.discovery.pipeline.discover", fake_discover)
        out = await store.replay(object(), "meta", "https://x/")
        assert not out.rediscovered
        assert "may really be empty" in out.reason
        # Known-stale must never read as success: a caller would otherwise
        # render "no jobs found" from a request that is simply broken.
        assert out.decayed and not out.ok
        assert out.response is not None  # still available for inspection

    async def test_self_heal_can_be_declined(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan(record_count=588))

        async def fake_execute(_s, _p, **_k):
            return self.FakeResponse(self._payload(1))

        monkeypatch.setattr("fetchaller.discovery.plan.execute", fake_execute)
        out = await store.replay(object(), "meta", "https://x/", self_heal=False)
        assert not out.rediscovered
        assert "against a recorded 588" in out.reason
        assert out.decayed and not out.ok

    async def test_a_failed_heal_is_not_reported_as_success(self, tmp_path, monkeypatch):
        # The fresh plan verified during discovery, but that was a different
        # request moments earlier. Without re-checking here, a heal that did
        # not actually work reports ok=True with one record.
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan(record_count=588))

        async def fake_execute(_s, _p, **_k):
            return self.FakeResponse(self._payload(1))

        async def fake_discover(_url, **_kwargs):
            class Result:
                plan = make_plan(record_count=588)
                reason = ""

            return Result()

        monkeypatch.setattr("fetchaller.discovery.plan.execute", fake_execute)
        monkeypatch.setattr("fetchaller.discovery.pipeline.discover", fake_discover)
        out = await store.replay(object(), "meta", "https://x/")
        assert out.rediscovered
        assert out.decayed and not out.ok
        # A single-entry list is not a record set (two entries sharing
        # structure is the floor), so Meta's "200 OK with 1 row" measures 0.
        assert "still returned 0 records against a recorded 588" in out.reason

    async def test_the_original_failure_is_preserved_when_healing_finds_nothing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(store, "_root", lambda: tmp_path)
        store.save_plan("meta", make_plan(record_count=588))

        async def boom(_s, _p, **_k):
            raise RuntimeError("connection reset")

        async def fake_discover(_url, **_kwargs):
            class Result:
                plan = None
                reason = "nothing observed"

            return Result()

        monkeypatch.setattr("fetchaller.discovery.plan.execute", boom)
        monkeypatch.setattr("fetchaller.discovery.pipeline.discover", fake_discover)
        out = await store.replay(object(), "meta", "https://x/")
        assert not out.ok
        assert "RuntimeError" in out.reason
