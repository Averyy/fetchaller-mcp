"""Tracing a volatile value back to whatever minted it.

The dangerous failure here is a pattern with too little context: it degenerates
into "any run of token characters" and re-mints the first random string in the
document, which then fails at the origin as an ordinary-looking rejection.
"""

from fetchaller.discovery.observe import Exchange
from fetchaller.discovery.provenance import (
    MIN_PREFIX_CONTEXT,
    MIN_VOLATILE_LEN,
    anchored_pattern,
    build_mint_steps,
    marker,
    trace,
)

TOKEN = "AVqR9Ov4XyzAbC123"


def exchange(url, body, *, order, headers=None, method="GET", status=200):
    return Exchange(
        order=order,
        phase="load",
        method=method,
        url=url,
        resource_type="fetch",
        status=status,
        request_headers={},
        request_body=None,
        response_headers=headers or {},
        body=body,
    )


class TestAnchoredPattern:
    def test_recovers_the_value(self):
        text = f'window.config = {{"csrf_token":"{TOKEN}"}};'
        pattern = anchored_pattern(text, TOKEN)
        import re

        assert re.search(pattern, text).group(1) == TOKEN

    def test_refuses_without_enough_context(self):
        # An unanchored pattern re-mints the first random string it sees.
        assert anchored_pattern("ab" + TOKEN, TOKEN) is None

    def test_context_floor_is_pinned(self):
        assert MIN_PREFIX_CONTEXT == 8

    def test_missing_value_yields_nothing(self):
        assert anchored_pattern("nothing here at all", TOKEN) is None

    def test_non_token_values_use_the_looser_character_class(self):
        # A JWT-ish value: punctuation, but no whitespace.
        value = "eyJhbGci.eyJzdWIiOiIx.SflKxwRJSM"
        text = f'some leading context here "{value}"'
        pattern = anchored_pattern(text, value)
        import re

        assert re.search(pattern, text).group(1) == value

    def test_a_pattern_that_cannot_recover_its_own_value_is_refused(self):
        # Neither character class can represent whitespace, so a spaced value
        # would produce a pattern that captures something else. A mint step
        # whose regex captures the wrong thing fails at the origin as an
        # ordinary-looking rejection.
        value = "a value with spaces in it"
        assert anchored_pattern("some leading context here >" + value + "<", value) is None


class TestTrace:
    def test_prefers_an_exact_header_match(self):
        exchanges = [
            exchange("https://x/csrf", "", order=0, headers={"x-apple-csrf-token": TOKEN}),
            exchange("https://x/page", f'token = "{TOKEN}"', order=1),
        ]
        step = trace(TOKEN, exchanges=exchanges, page_url="https://x/", page_html="", before_order=5)
        assert step.source == "header"
        assert step.selector == "x-apple-csrf-token"

    def test_falls_back_to_an_earlier_body(self):
        exchanges = [exchange("https://x/boot", f'lsd token is "{TOKEN}" ok', order=0)]
        step = trace(TOKEN, exchanges=exchanges, page_url="https://x/", page_html="", before_order=3)
        assert step.source == "regex"
        assert step.url == "https://x/boot"

    def test_falls_back_to_the_page_html(self):
        html = f'["LSD",[],{{"token":"{TOKEN}"}}]'
        step = trace(TOKEN, exchanges=[], page_url="https://x/p", page_html=html, before_order=1)
        assert step.source == "regex"
        assert step.url == "https://x/p"

    def test_later_exchanges_are_never_a_source(self):
        # A value cannot have been minted by a request that had not happened.
        exchanges = [exchange("https://x/late", f'token "{TOKEN}"', order=9)]
        assert trace(TOKEN, exchanges=exchanges, page_url="https://x/", page_html="", before_order=2) is None

    def test_post_exchanges_are_not_used_as_sources(self):
        # A POST mint step would need its own body reproduced to be replayable,
        # and an unreplayable mint step is worse than a literal.
        exchanges = [exchange("https://x/p", f'token "{TOKEN}"', order=0, method="POST")]
        assert trace(TOKEN, exchanges=exchanges, page_url="https://x/", page_html="", before_order=5) is None

    def test_untraceable_values_return_none(self):
        assert trace(TOKEN, exchanges=[], page_url="https://x/", page_html="", before_order=1) is None


class TestBuildMintSteps:
    def _html(self):
        return f'["LSD",[],{{"token":"{TOKEN}"}}]'

    def test_one_step_is_shared_by_every_use_of_the_value(self):
        # A CSRF token often feeds both a header and the body. Two steps would
        # refetch the page twice and the copies could disagree.
        fields = {"field:lsd": TOKEN, "header:x-fb-lsd": TOKEN}
        out, steps = build_mint_steps(
            fields, exchanges=[], page_url="https://x/p", page_html=self._html(), before_order=1
        )
        assert len(steps) == 1
        assert out["field:lsd"] == out["header:x-fb-lsd"] == marker(steps[0].name)

    def test_short_values_are_left_alone(self):
        fields = {"query:base_query": "engineer"}
        out, steps = build_mint_steps(
            fields, exchanges=[], page_url="https://x/p", page_html="engineer", before_order=1
        )
        assert steps == ()
        assert out == fields

    def test_volatile_floor_is_pinned(self):
        assert MIN_VOLATILE_LEN == 12

    def test_untraceable_values_stay_literal(self):
        fields = {"field:doc_id": "27129360303422352"}
        out, steps = build_mint_steps(
            fields, exchanges=[], page_url="https://x/p", page_html="", before_order=1
        )
        # Meta's doc_id lives only in a JS bundle, which capture does not
        # observe, so it stays a literal and rotation is what triggers rediscovery.
        assert steps == ()
        assert out["field:doc_id"] == "27129360303422352"

    def test_list_values_are_handled_elementwise(self):
        fields = {"query:facets[]": ["normalized_country_code", "short"]}
        out, _steps = build_mint_steps(
            fields,
            exchanges=[],
            page_url="https://x/p",
            page_html="prefix context here normalized_country_code",
            before_order=1,
        )
        assert isinstance(out["query:facets[]"], list)
        assert len(out["query:facets[]"]) == 2

    def test_step_names_are_derived_from_the_field(self):
        fields = {"header:x-fb-lsd": TOKEN}
        _out, steps = build_mint_steps(
            fields, exchanges=[], page_url="https://x/p", page_html=self._html(), before_order=1
        )
        assert steps[0].name == "X_FB_LSD"

    def test_duplicate_fetches_are_deduplicated(self):
        second = "ZZqR9Ov4XyzAbC999"
        html = f'["LSD",[],{{"token":"{TOKEN}"}}]["OTHER",[],{{"token":"{second}"}}]'
        fields = {"field:a": TOKEN, "field:b": second}
        _out, steps = build_mint_steps(
            fields, exchanges=[], page_url="https://x/p", page_html=html, before_order=1
        )
        # Different values, different anchors: two steps, same URL.
        assert len({s.url for s in steps}) == 1
        assert len(steps) == 2
