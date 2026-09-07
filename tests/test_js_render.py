"""Tests for client-side-rendering detection.

fetchaller does not execute JavaScript. A client-rendered page therefore comes
back as a shell that extracts "successfully", and the caller records "this site
has no content" instead of "this needs a browser". The detector cannot fix that
— it exists so the answer is not mistaken for a complete one.

The false-positive tests matter as much as the positive ones: a warning that
fires on ordinary pages is one callers learn to ignore.
"""

import pytest

from fetchaller.content.js_render import (
    SHELL_TEXT_CHARS,
    collect_markers,
    describe,
    needs_dom_scan,
)

FRAMER_SHELL = """
<html><head><title>Greypoint Industries</title></head>
<body>
  <script src="https://framerusercontent.com/sites/abc/script.js"></script>
  <div id="main"></div>
</body></html>
"""

MOUNT_POINT_SHELL = """
<html><head><title>App</title></head>
<body><div id="root"></div><script src="/static/bundle.js"></script></body></html>
"""

LOADING_REGION = """
<html><head><title>Careers</title></head>
<body>
  <script src="/_next/static/chunks/main.js"></script>
  <h1>Join our team</h1>
  <p>%s</p>
  <h2>Open Roles</h2>
  <div class="loaderContainer"><svg class="fa-spinner"></svg></div>
</body></html>
""" % ("We are a team of engineers and operators. " * 12)

# The shape that made a naive class-name check unusable: utility-class CSS puts
# "spinner" in the class list of ordinary links and buttons that have their own
# text and are not placeholders at all.
UTILITY_CLASS_PAGE = """
<html><head><title>Real page</title></head>
<body>
  <script src="/_next/static/chunks/main.js"></script>
  <a class="group/trigger relative spinner-none" href="/x">Products</a>
  <button class="loading-none">Sign up</button>
  <p>%s</p>
</body></html>
""" % ("Real prose that a reader actually wants. " * 40)


class TestMarkerCollection:
    def test_framer_is_detected(self):
        assert "framer" in collect_markers(FRAMER_SHELL)

    def test_empty_mount_point_is_detected(self):
        assert "mount-point" in collect_markers(MOUNT_POINT_SHELL)

    def test_a_filled_mount_point_is_not_a_marker(self):
        html = '<html><body><div id="root"><h1>Server rendered</h1></div></body></html>'
        assert "mount-point" not in collect_markers(html)

    def test_empty_loading_container_is_detected(self):
        assert "loading-placeholder" in collect_markers(LOADING_REGION)

    def test_interactive_elements_with_text_are_not_placeholders(self):
        assert "loading-placeholder" not in collect_markers(UTILITY_CLASS_PAGE)

    def test_hidden_placeholders_are_ignored(self):
        html = '<html><body><div class="spinner" style="display:none"></div></body></html>'
        assert "loading-placeholder" not in collect_markers(html)

    def test_aria_hidden_placeholders_are_ignored(self):
        html = '<html><body><div class="spinner" aria-hidden="true"></div></body></html>'
        assert "loading-placeholder" not in collect_markers(html)

    def test_a_placeholder_with_its_own_text_is_ignored(self):
        html = '<html><body><div class="loading">Loading is disabled</div></body></html>'
        assert "loading-placeholder" not in collect_markers(html)

    def test_plain_page_has_no_markers(self):
        assert collect_markers("<html><body><h1>Hi</h1><p>Words.</p></body></html>") == ()

    def test_unparseable_input_still_yields_text_markers(self):
        assert "framer" in collect_markers("framerusercontent.com <<<>>")


class TestVerdict:
    def test_shell_is_reported(self):
        note = describe(("framer",), 2905, "# Greypoint Industries")
        assert note is not None
        assert "possibly_js_rendered" in note
        assert "un-hydrated shell" in note

    def test_large_document_that_renders_to_almost_nothing_is_a_shell(self):
        """Framer ships 140KB of markup that renders to a few hundred characters:
        too much text for the absolute threshold, still not a real page."""
        note = describe(("framer",), 147_359, "x" * 548)
        assert note is not None
        assert "un-hydrated shell" in note

    def test_next_js_alone_never_triggers_the_ratio_rule(self):
        """Next.js is the common case for server-rendered pages too, so on its
        own it proves nothing — vercel.com would otherwise be flagged."""
        assert describe(("next.js",), 584_677, "x" * 4046) is None

    def test_loading_region_on_a_real_page_is_reported_as_partial(self):
        note = describe(("next.js", "loading-placeholder"), 23_377, "x" * 604)
        assert note is not None
        assert "loading placeholder" in note
        assert "un-hydrated shell" not in note

    def test_no_markers_leave_even_an_empty_large_document_unknown(self):
        # Ratio alone cannot distinguish a shell from a complete page dominated
        # by inline data. Known client-rendered platforms carry strong markers.
        assert describe((), 100_000, "") is None

    def test_no_markers_and_readable_text_is_still_no_verdict(self):
        # The guard that keeps the above from becoming a false-positive engine.
        assert describe((), 100_000, "x" * 5_000) is None

    def test_a_tiny_document_is_not_judged(self):
        """Below the floor, emptiness means nothing."""
        assert describe(("mount-point",), 120, "") is None

    def test_a_short_but_real_page_is_not_flagged(self):
        text = "A stub page with one real paragraph of prose on it, and nothing more."
        assert len(text) < SHELL_TEXT_CHARS
        assert describe((), 4000, text) is None

    def test_content_heavy_page_with_a_framework_is_not_flagged(self):
        assert describe(("next.js",), 67_846, "x" * 4885) is None


class TestScanGate:
    """The DOM scan costs a second full parse, so it must not run on every page."""

    def test_thin_extraction_is_scanned(self):
        assert needs_dom_scan("x" * 100_000, "tiny") is True

    def test_mention_of_a_loader_is_scanned(self):
        html = '<div class="loaderContainer"></div>' + "x" * 100_000
        assert needs_dom_scan(html, "y" * 50_000) is True

    def test_ordinary_content_heavy_page_is_skipped(self):
        assert needs_dom_scan("<p>hello</p>" * 5000, "y" * 50_000) is False

    def test_tiny_document_is_skipped(self):
        assert needs_dom_scan("<p>hi</p>", "") is False

    @pytest.mark.parametrize("hint", ["spinner", "loader", "loading", "skeleton"])
    def test_every_hint_word_opens_the_gate(self, hint):
        html = f'<div class="{hint}"></div>' + "x" * 100_000
        assert needs_dom_scan(html, "y" * 50_000) is True


class TestShellMetadataSalvage:
    """A page whose body never rendered still ships its metadata.

    Returning only "# Greypoint Industries" discards a description of what the
    company does, its canonical domain, and its company profiles — on a page
    where that is the entire available answer.
    """

    @staticmethod
    def _soup(html):
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "lxml")

    def test_og_description_is_preferred(self):
        from fetchaller.content.js_render import extract_shell_metadata

        html = """
        <html><head>
          <meta name="description" content="Generic boilerplate.">
          <meta property="og:description" content="See the invisible battlefield.">
        </head><body></body></html>
        """
        assert extract_shell_metadata(self._soup(html))["description"] == "See the invisible battlefield."

    def test_falls_back_through_the_chain(self):
        from fetchaller.content.js_render import extract_shell_metadata

        html = '<html><head><meta name="twitter:description" content="Last resort."></head></html>'
        assert extract_shell_metadata(self._soup(html))["description"] == "Last resort."

    def test_canonical_is_captured(self):
        from fetchaller.content.js_render import extract_shell_metadata

        html = '<html><head><link rel="canonical" href="https://example.ca/"></head></html>'
        assert extract_shell_metadata(self._soup(html))["canonical"] == "https://example.ca/"

    def test_organization_profiles_are_captured(self):
        from fetchaller.content.js_render import extract_shell_metadata

        html = """
        <html><head><script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Organization","name":"X",
         "sameAs":["https://www.linkedin.com/company/x/","https://x.com/x_"]}
        </script></head></html>
        """
        assert extract_shell_metadata(self._soup(html))["profiles"] == (
            "https://www.linkedin.com/company/x/",
            "https://x.com/x_",
        )

    def test_non_organization_jsonld_is_ignored(self):
        """A Product's sameAs is not the site owner's profile."""
        from fetchaller.content.js_render import extract_shell_metadata

        html = """
        <html><head><script type="application/ld+json">
        {"@type":"Product","sameAs":["https://example.com/not-a-profile"]}
        </script></head></html>
        """
        assert "profiles" not in extract_shell_metadata(self._soup(html))

    def test_non_http_sameas_is_rejected(self):
        from fetchaller.content.js_render import extract_shell_metadata

        html = """
        <html><head><script type="application/ld+json">
        {"@type":"Organization","sameAs":["javascript:alert(1)","mailto:a@b.c"]}
        </script></head></html>
        """
        assert "profiles" not in extract_shell_metadata(self._soup(html))

    @pytest.mark.parametrize(
        "body",
        ["not json", "{", '{"@type":"Organization","sameAs":"notalist"}', "[]", "null"],
    )
    def test_malformed_jsonld_is_survivable(self, body):
        from fetchaller.content.js_render import extract_shell_metadata

        html = f'<html><head><script type="application/ld+json">{body}</script></head></html>'
        assert "profiles" not in extract_shell_metadata(self._soup(html))

    def test_description_is_bounded(self):
        from fetchaller.content.js_render import _MAX_METADATA_CHARS, extract_shell_metadata

        html = f'<html><head><meta name="description" content="{"x" * 5000}"></head></html>'
        assert len(extract_shell_metadata(self._soup(html))["description"]) <= _MAX_METADATA_CHARS

    def test_profile_count_is_bounded(self):
        from fetchaller.content.js_render import _MAX_PROFILE_LINKS, extract_shell_metadata

        links = ",".join(f'"https://example.com/{n}"' for n in range(50))
        html = (
            '<html><head><script type="application/ld+json">'
            f'{{"@type":"Organization","sameAs":[{links}]}}'
            "</script></head></html>"
        )
        profiles = extract_shell_metadata(self._soup(html))["profiles"]
        assert len(profiles) <= _MAX_PROFILE_LINKS

    def test_whitespace_and_control_characters_are_cleaned(self):
        from fetchaller.content.js_render import extract_shell_metadata

        html = '<html><head><meta name="description" content="a\r\n  b\tc"></head></html>'
        assert extract_shell_metadata(self._soup(html))["description"] == "a b c"

    def test_page_without_metadata_yields_nothing(self):
        from fetchaller.content.js_render import extract_shell_metadata

        assert extract_shell_metadata(self._soup("<html><body><p>x</p></body></html>")) == {}

    def test_evidence_returns_markers_and_metadata_from_one_parse(self):
        from fetchaller.content.js_render import collect_shell_evidence

        html = """
        <html><head><meta property="og:description" content="What we do."></head>
        <body><div id="root"></div></body></html>
        """
        markers, metadata = collect_shell_evidence(html)

        assert "mount-point" in markers
        assert metadata["description"] == "What we do."


class TestSalvageAppearsInTheVerdict:
    def test_shell_verdict_carries_the_recovered_metadata(self):
        note = describe(
            ("framer",),
            2905,
            "# Greypoint Industries",
            {
                "description": "See the invisible battlefield.",
                "canonical": "https://greypointindustries.ca/",
                "profiles": ("https://www.linkedin.com/company/greypoint-industries/",),
            },
        )

        assert "Recovered from page metadata" in note
        assert "See the invisible battlefield." in note
        assert "https://greypointindustries.ca/" in note
        assert "linkedin.com/company/greypoint-industries" in note

    def test_partial_verdict_does_not_repeat_the_description(self):
        """The page's own text is already there; the meta description would
        just restate it."""
        note = describe(
            ("next.js", "loading-placeholder"),
            23_377,
            "x" * 604,
            {"description": "CSMC is an infrastructure company."},
        )

        assert "Recovered from page metadata" not in note

    def test_shell_without_metadata_is_unchanged(self):
        note = describe(("framer",), 2905, "# Title", {})

        assert "possibly_js_rendered" in note
        assert "Recovered from page metadata" not in note

    def test_metadata_is_optional(self):
        assert "possibly_js_rendered" in describe(("framer",), 2905, "# Title")


class TestScriptDominatedShellNeedsStructuralEvidence:
    """A low text ratio is actionable only with a strong platform marker."""

    def _describe(self, html_bytes, text_len, markers=()):
        return describe(markers, html_bytes, "x" * text_len, None)

    def test_marker_free_data_heavy_short_article_is_not_flagged(self):
        assert self._describe(200_000, 1_000) is None

    def test_message_is_the_hard_shell_warning(self):
        assert "un-hydrated shell" in self._describe(85_416, 257, ("phenom",))

    def test_a_readable_page_is_not_flagged(self):
        # Real pages measured the same way: SAP's SuccessFactors board 2.06%,
        # GitHub 3.00%, Job Bank 5.83%, Wikipedia 9.10%, a gojobs posting
        # 19.07%. All must stay silent on the ratio.
        for html_bytes, ratio in ((83_745, 0.0206), (375_382, 0.0300),
                                  (286_413, 0.0583), (1_389_197, 0.0910)):
            assert self._describe(html_bytes, int(html_bytes * ratio)) is None

    def test_small_documents_are_left_alone(self):
        # Below the 20KB floor the emptiness means nothing on its own.
        assert self._describe(5_000, 10) is None

    def test_phenom_and_ukg_are_named_when_recognised(self):
        assert "(phenom)" in describe(("phenom",), 85_416, "x" * 257, None)
        assert "(ukg)" in describe(("ukg",), 286_553, "x" * 1_086, None)

    def test_markers_are_detected_from_markup(self):
        assert "phenom" in collect_markers(
            '<html><body><script>var phApp = phApp || {"x":1}</script></body></html>'
        )
        assert "ukg" in collect_markers(
            "<html><body><script>Recruiting.TenantFeatureToggle.setFeatureToggles([])"
            "</script></body></html>"
        )


class TestConnectionFailureMessages:
    """A transport failure must say which kind it was, without a debug dump."""

    def _d(self, raw):
        from fetchaller.content.connection_errors import describe_connection_failure

        return describe_connection_failure(raw)

    def test_certificate_chain_failure_is_named(self):
        raw = (
            'is_connect error: wreq::Error { kind: Request, source: Some(Error { code: SSL (1), '
            'cause: Some(Ssl(ErrorStack([Error { code: 268435581, library: "SSL routines", '
            'reason: "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate", '
            'file: "/Users/runner/work/wreq-python/..." '
        )
        out = self._d(raw)
        assert "certificate chain could not be verified" in out
        assert "intermediate" in out

    def test_generic_verification_failure_does_not_invent_a_chain_cause(self):
        out = self._d("CERTIFICATE_VERIFY_FAILED")
        assert "does not distinguish" in out
        assert "serving an incomplete chain" not in out

    def test_specific_certificate_reason_wins_over_generic_verification(self):
        assert "expired" in self._d(
            "CERTIFICATE_VERIFY_FAILED: certificate has expired"
        )
        assert "does not cover this hostname" in self._d(
            "CERTIFICATE_VERIFY_FAILED: HostnameMismatch"
        )

    def test_no_build_path_or_struct_dump_leaks(self):
        raw = 'Error { code: 1, file: "/Users/runner/work/wreq-python/target/x", cause: Some(y) }'
        out = self._d(raw)
        assert "/Users/runner" not in out
        assert len(out) < 300

    def test_unknown_failure_redacts_a_tokenized_url(self):
        out = self._d(
            "request failed for url "
            "https://example.com/private/export?api_key=TOPSECRET: peer disconnected"
        )
        assert "TOPSECRET" not in out
        assert "/private/export" not in out
        assert "https://example.com/…" in out

    def test_unknown_failure_redacts_a_build_path(self):
        out = self._d(
            "transport worker crashed at /Users/avery/build/wreq/target/release/client"
        )
        assert "/Users/avery" not in out
        assert "[redacted-path]" in out

    def test_each_cause_is_distinguished(self):
        assert "does not cover this hostname" in self._d("HostnameMismatch")
        assert "expired" in self._d("certificate has expired")
        assert "DNS lookup failed" in self._d("dns error: failed to lookup address")
        assert "refused" in self._d("Connection refused (os error 61)")

    def test_empty_reason_still_says_something(self):
        assert self._d("") .startswith("Connection error:")
