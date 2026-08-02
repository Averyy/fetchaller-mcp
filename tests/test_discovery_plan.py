"""The plan model, and the traps in replaying one.

Most of these are regression tests for failures that looked like something
else. A dropped body reads as a bad token; a percent-encoded marker reads as a
rejected credential; a duplicated header reads as a protocol error. Each cost
real time to diagnose once.
"""

import json
from urllib.parse import parse_qsl

from fetchaller.discovery import plan as plan_mod


class TestHeaderDelta:
    def test_transport_owned_headers_are_dropped(self):
        # Sending a captured copy duplicates them under HTTP/2, which is a
        # protocol error rather than a last-wins overwrite — and overriding the
        # UA would contradict wafer's fingerprint envelope.
        captured = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Encoding": "gzip",
            "Cookie": "a=b",
            "Host": "example.com",
            "Content-Length": "12",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-mode": "cors",
            ":authority": "example.com",
            "X-Fb-Lsd": "keepme",
        }
        assert plan_mod.header_delta(captured) == {"x-fb-lsd": "keepme"}

    def test_names_are_lowercased(self):
        assert plan_mod.header_delta({"X-Custom": "1"}) == {"x-custom": "1"}

    def test_empty_input(self):
        assert plan_mod.header_delta({}) == {}


class TestRepeatedParameters:
    def test_repeats_are_kept_as_a_list(self):
        # dict(parse_qsl(...)) keeps only the last value, and Amazon's search
        # route sends facets[] twelve times.
        pairs = [("facets[]", f"f{i}") for i in range(12)] + [("base_query", "engineer")]
        fields = plan_mod.pairs_to_fields(pairs)
        assert len(fields["facets[]"]) == 12
        assert fields["base_query"] == "engineer"

    def test_encoding_round_trips_every_repeat(self):
        fields = {"facets[]": [f"f{i}" for i in range(12)], "base_query": "engineer"}
        encoded = plan_mod.encode_fields(fields)
        assert encoded.count("facets%5B%5D=") == 12
        assert plan_mod.pairs_to_fields(parse_qsl(encoded)) == fields

    def test_single_values_stay_scalar(self):
        assert plan_mod.pairs_to_fields([("a", "1")]) == {"a": "1"}


class TestClassifyBody:
    def test_no_body(self):
        assert plan_mod.classify_body(None) == (None, {})
        assert plan_mod.classify_body("") == (None, {})

    def test_json_object_exposes_its_fields(self):
        kind, fields = plan_mod.classify_body('{"query":"engineer","page":2}', "application/json")
        assert kind == "json"
        assert fields == {"query": "engineer", "page": 2}

    def test_json_array_is_raw_because_it_is_positional(self):
        # Google addresses arguments by index, so dropping a slot shifts every
        # argument after it. Leaving the body whole is the right answer.
        kind, fields = plan_mod.classify_body('["a","b","c"]', "application/json")
        assert kind == "raw"
        assert fields == {}

    def test_form_body(self):
        kind, fields = plan_mod.classify_body(
            "f.req=%5B%5D&at=xyz", "application/x-www-form-urlencoded"
        )
        assert kind == "form"
        assert fields == {"f.req": "[]", "at": "xyz"}

    def test_form_is_sniffed_without_a_content_type(self):
        kind, fields = plan_mod.classify_body("a=1&b=2")
        assert kind == "form"
        assert fields == {"a": "1", "b": "2"}

    def test_json_is_sniffed_without_a_content_type(self):
        assert plan_mod.classify_body('{"a":1}')[0] == "json"

    def test_unrecognized_body_is_raw(self):
        assert plan_mod.classify_body("<xml/>") == ("raw", {})


class TestSubstitution:
    def test_replaces_markers_in_strings(self):
        assert plan_mod.substitute("token={{mint:LSD}}", {"LSD": "abc"}) == "token=abc"

    def test_recurses_through_containers(self):
        value = {"h": ["{{mint:A}}", {"n": "{{mint:B}}"}]}
        assert plan_mod.substitute(value, {"A": "1", "B": "2"}) == {"h": ["1", {"n": "2"}]}

    def test_unknown_markers_are_left_in_place(self):
        assert plan_mod.substitute("{{mint:GONE}}", {}) == "{{mint:GONE}}"

    def test_non_strings_pass_through(self):
        assert plan_mod.substitute({"page": 2, "ok": True}, {}) == {"page": 2, "ok": True}


class TestUnresolvedMarkers:
    def test_finds_markers_anywhere(self):
        assert plan_mod.unresolved_markers({"a": ["{{mint:X}}"], "b": "{{mint:Y}}"}) == {"X", "Y"}

    def test_clean_values_report_nothing(self):
        assert plan_mod.unresolved_markers({"a": "plain", "b": 2}) == set()


class TestResolvePlan:
    def test_percent_encoded_markers_are_substituted(self):
        # Trap 2. A marker inside a form body is stored as
        # %7B%7Bmint%3AX%7D%7D. Substituting into the serialized string finds
        # nothing, and the marker itself goes out as the token.
        body = plan_mod.encode_fields({"lsd": "{{mint:LSD}}", "av": "0"})
        assert "%7B%7Bmint%3ALSD%7D%7D" in body
        plan = plan_mod.RequestPlan(method="POST", url="https://x/graphql", body=body, body_kind="form")
        _url, headers, send, pending = plan_mod.resolve_plan(plan, {"LSD": "REAL"})
        assert pending == set()
        assert "lsd=REAL" in send["body"]
        assert "mint" not in send["body"]
        assert headers["content-type"] == "application/x-www-form-urlencoded"

    def test_unresolved_markers_are_detected_before_encoding(self):
        # Trap 2, second bite: the check scanned the already-encoded body and
        # cheerfully reported everything resolved.
        body = plan_mod.encode_fields({"lsd": "{{mint:LSD}}"})
        plan = plan_mod.RequestPlan(method="POST", url="https://x/g", body=body, body_kind="form")
        _url, _headers, _send, pending = plan_mod.resolve_plan(plan, {})
        assert pending == {"LSD"}

    def test_json_bodies_do_not_carry_a_captured_content_type(self):
        # Trap 5. wafer sets the JSON content-type itself; sending the captured
        # one alongside duplicates the header, and HTTP/2 rejects duplicates.
        plan = plan_mod.RequestPlan(
            method="POST",
            url="https://x/api",
            headers={"content-type": "application/json", "x-keep": "1"},
            body='{"query":"engineer"}',
            body_kind="json",
        )
        _url, headers, send, _pending = plan_mod.resolve_plan(plan, {})
        assert "content-type" not in headers
        assert headers["x-keep"] == "1"
        assert send["json"] == {"query": "engineer"}

    def test_form_bodies_are_encoded_here_not_by_wafer(self):
        # Trap 5. form= flattens repeats, so the body is encoded by hand and
        # sent as body=.
        body = plan_mod.encode_fields({"facets[]": ["a", "b", "c"]})
        plan = plan_mod.RequestPlan(method="POST", url="https://x/s", body=body, body_kind="form")
        _url, _headers, send, _pending = plan_mod.resolve_plan(plan, {})
        assert "json" not in send
        assert send["body"].count("facets%5B%5D=") == 3

    def test_query_repeats_survive_resolution(self):
        # Trap 4, on the URL side.
        url = "https://x/s?" + plan_mod.encode_fields({"facets[]": ["a", "b"], "q": "engineer"})
        plan = plan_mod.RequestPlan(method="GET", url=url)
        out, _headers, _send, _pending = plan_mod.resolve_plan(plan, {})
        assert out.count("facets%5B%5D=") == 2

    def test_markers_in_query_values_resolve(self):
        url = "https://x/s?" + plan_mod.encode_fields({"t": "{{mint:T}}"})
        plan = plan_mod.RequestPlan(method="GET", url=url)
        out, _headers, _send, pending = plan_mod.resolve_plan(plan, {"T": "abc"})
        assert pending == set()
        assert out.endswith("t=abc")

    def test_raw_bodies_are_sent_whole(self):
        plan = plan_mod.RequestPlan(method="POST", url="https://x/r", body='["a",null,2]', body_kind="raw")
        _url, _headers, send, _pending = plan_mod.resolve_plan(plan, {})
        assert send["body"] == '["a",null,2]'


class TestRoundTrip:
    def test_json_round_trip_is_exact(self):
        plan = plan_mod.RequestPlan(
            method="POST",
            url="https://x/graphql?a=1",
            headers={"x-fb-lsd": "{{mint:LSD}}"},
            body=plan_mod.encode_fields({"lsd": "{{mint:LSD}}", "doc_id": "27129360303422352"}),
            body_kind="form",
            mint=(plan_mod.MintStep(name="LSD", method="GET", url="https://x/", source="regex", selector="t=(.{8,40})"),),
            verified=True,
            required_fields=("field:lsd", "field:doc_id"),
            dropped_fields=("field:__csr",),
            record_count=588,
            notes=("nudged",),
        )
        again = plan_mod.RequestPlan.from_json(plan.to_json())
        assert again.to_json() == plan.to_json()
        assert again.mint[0].selector == plan.mint[0].selector
        assert again.record_count == 588

    def test_a_bare_plan_round_trips(self):
        plan = plan_mod.RequestPlan(method="GET", url="https://x/y")
        assert plan_mod.RequestPlan.from_json(plan.to_json()).to_dict() == plan.to_dict()

    def test_record_count_is_what_makes_decay_detectable(self):
        # Measured on Meta: the healthy plan returns 588 records; incrementing
        # doc_id by one returns HTTP 200 with 1. Trivially distinguishable.
        healthy = plan_mod.RequestPlan(method="POST", url="https://x/g", record_count=588)
        assert healthy.record_count > 0
        assert json.loads(healthy.to_json())["record_count"] == 588


class TestThrottle:
    async def test_execute_calls_the_throttle_before_requesting(self):
        # A discovery pass is a burst of ~50 replays at one host. Unlimited, it
        # draws a 429 that then makes every capture read as "no API here".
        order: list[str] = []

        class FakeSession:
            async def request(self, *_a, **_k):
                order.append("request")

                class R:
                    status_code = 200
                    text = "{}"

                return R()

        async def throttle():
            order.append("throttle")

        plan = plan_mod.RequestPlan(method="GET", url="https://x/y")
        await plan_mod.execute(FakeSession(), plan, throttle=throttle)
        assert order == ["throttle", "request"]

    async def test_no_throttle_by_default(self):
        # A cached replay is one request per user action, like every other
        # client, and must not pay the discovery spacing.
        calls: list[str] = []

        class FakeSession:
            async def request(self, *_a, **_k):
                calls.append("request")

                class R:
                    status_code = 200
                    text = "{}"

                return R()

        await plan_mod.execute(FakeSession(), plan_mod.RequestPlan(method="GET", url="https://x/y"))
        assert calls == ["request"]

    async def test_mint_fetches_are_throttled_too(self):
        hits: list[str] = []

        class FakeSession:
            async def request(self, _m, url, **_k):
                hits.append(url)

                class R:
                    status_code = 200
                    text = 'prefix context here "AVqR9Ov4XyzAbC123"'
                    headers = {}

                return R()

        async def throttle():
            hits.append("throttle")

        step = plan_mod.MintStep(
            name="T", method="GET", url="https://x/mint", source="regex", selector=r'here "([A-Za-z0-9_-]{8,40})"'
        )
        out = await plan_mod.mint_values(FakeSession(), (step,), throttle=throttle)
        assert out == {"T": "AVqR9Ov4XyzAbC123"}
        assert hits[0] == "throttle"


class TestRateLimiterWiring:
    def test_discovery_has_its_own_limiter(self):
        from fetchaller.ratelimit import discovery_limiter

        # Slower than the per-board clients on purpose: they make one request
        # per user action, discovery makes ~50 per invocation.
        assert discovery_limiter._min_interval >= 1.5


class TestOrphanedMintSteps:
    def test_a_step_survives_when_its_marker_is_percent_encoded(self):
        # The marker lives in the body as %7B%7Bmint%3ALSD%7D%7D. A raw
        # substring check would see no match and prune a live step, so the
        # plan would then send the marker itself as the token.
        from fetchaller.discovery.pipeline import _used_steps

        step = plan_mod.MintStep(
            name="LSD", method="GET", url="https://x/", source="regex", selector="t=(.{8,40})"
        )
        plan = plan_mod.RequestPlan(
            method="POST",
            url="https://x/g",
            body=plan_mod.encode_fields({"lsd": "{{mint:LSD}}"}),
            body_kind="form",
            mint=(step,),
        )
        assert "%7B%7Bmint%3ALSD%7D%7D" in plan.body
        assert _used_steps(plan) == (step,)

    def test_a_step_nothing_references_is_pruned(self):
        from fetchaller.discovery.pipeline import _used_steps

        step = plan_mod.MintStep(
            name="GONE", method="GET", url="https://x/", source="regex", selector="t=(.{8,40})"
        )
        plan = plan_mod.RequestPlan(method="GET", url="https://x/y", mint=(step,))
        assert _used_steps(plan) == ()

    def test_a_step_referenced_from_a_header_survives(self):
        from fetchaller.discovery.pipeline import _used_steps

        step = plan_mod.MintStep(
            name="LSD", method="GET", url="https://x/", source="regex", selector="t=(.{8,40})"
        )
        plan = plan_mod.RequestPlan(
            method="GET", url="https://x/y", headers={"x-fb-lsd": "{{mint:LSD}}"}, mint=(step,)
        )
        assert _used_steps(plan) == (step,)


class TestCookieSeeding:
    def test_renders_a_browser_cookie_as_a_set_cookie_header(self):
        header = plan_mod.cookie_header(
            {
                "name": "datr",
                "value": "abc123",
                "domain": ".metacareers.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
            }
        )
        assert header.startswith("datr=abc123")
        assert "Domain=.metacareers.com" in header
        assert "Path=/" in header
        assert "Secure" in header and "HttpOnly" in header
        assert "SameSite=None" in header

    def test_path_defaults_when_absent(self):
        assert "Path=/" in plan_mod.cookie_header({"name": "a", "value": "b", "domain": "x.com"})

    def test_an_invalid_samesite_is_dropped(self):
        header = plan_mod.cookie_header(
            {"name": "a", "value": "b", "domain": "x.com", "sameSite": "Whatever"}
        )
        assert "SameSite" not in header

    def test_seeding_scopes_each_cookie_to_its_own_origin(self):
        # Without this every probe goes out cookieless, so an origin sees a
        # browser load a page and then dozens of anonymous requests hit the
        # endpoint it just called — which is what draws the throttling.
        added: list[tuple[str, str]] = []

        class FakeSession:
            def add_cookie(self, raw, url):
                added.append((raw, url))

        seeded = plan_mod.seed_cookies(
            FakeSession(),
            [
                {"name": "a", "value": "1", "domain": ".metacareers.com", "path": "/", "secure": True},
                {"name": "b", "value": "2", "domain": "jobs.apple.com", "path": "/x", "secure": False},
            ],
        )
        assert seeded == 2
        assert added[0][1] == "https://metacareers.com/"
        assert added[1][1] == "http://jobs.apple.com/x"

    def test_cookies_without_a_name_or_domain_are_skipped(self):
        class FakeSession:
            def add_cookie(self, raw, url):
                raise AssertionError("should not be called")

        assert plan_mod.seed_cookies(FakeSession(), [{"value": "1"}, {"name": "a"}]) == 0

    def test_a_session_that_cannot_take_cookies_does_not_fail_the_pass(self):
        class FakeSession:
            def add_cookie(self, raw, url):
                raise NotImplementedError("Opera Mini has no jar")

        assert plan_mod.seed_cookies(FakeSession(), [{"name": "a", "value": "1", "domain": "x.com"}]) == 0
