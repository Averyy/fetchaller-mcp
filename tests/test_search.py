"""Tests for web search module (Google SSR + DuckDuckGo)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.fetchaller.search import (
    _MAX_RESULTS_OUTPUT_CHARS,
    _cache,
    _dedup_and_merge,
    _dedup_key,
    _format_output,
    _format_results,
    search,
)
from src.fetchaller.search.ddg import extract_results as ddg_extract
from src.fetchaller.search.ddg import search_ddg as ddg_search
from src.fetchaller.search.google import (
    extract_results as google_extract,
)
from src.fetchaller.search.google import (
    is_captcha,
)
from src.fetchaller.search.google import search_google as google_search
from src.fetchaller.search.models import SearchResult

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Google extraction
# ---------------------------------------------------------------------------


class TestGoogleExtraction:
    """Test Google SSR result extraction."""

    def test_normal_results_from_fixture(self):
        """Real Google SSR fixture produces expected results with clean titles and URLs."""
        html = (FIXTURES / "google_ssr.html").read_text()
        results = google_extract(html)
        assert len(results) >= 8
        # First result should be Real Python
        assert "realpython.com" in results[0].url
        assert "asyncio" in results[0].title.lower() or "async" in results[0].title.lower()
        # Titles should NOT contain breadcrumb URLs (e.g., "realpython.com ›")
        for r in results:
            assert "›" not in r.title, f"Breadcrumb leaked into title: {r.title}"

    def test_captcha_detection_sorry_url(self):
        """CAPTCHA detected via sorry.google.com in response URL."""
        response = MagicMock()
        response.url = "https://sorry.google.com/sorry/index?continue=..."
        response.text = "Our systems have detected unusual traffic"
        response.status_code = 200
        assert is_captcha(response) is True

    def test_captcha_detection_sorry_path(self):
        """CAPTCHA detected via /sorry path."""
        response = MagicMock()
        response.url = "https://www.google.com/sorry/index?continue=..."
        response.text = "some page"
        response.status_code = 200
        assert is_captcha(response) is True

    def test_captcha_detection_unusual_traffic_text(self):
        """CAPTCHA detected via 'unusual traffic' in response body."""
        response = MagicMock()
        response.url = "https://www.google.com/search?q=test"
        response.text = "We detected Unusual Traffic from your network"
        response.status_code = 200
        assert is_captcha(response) is True

    def test_captcha_detection_429_status(self):
        """CAPTCHA detected via 429 status code."""
        response = MagicMock()
        response.url = "https://www.google.com/search?q=test"
        response.text = ""
        response.status_code = 429
        assert is_captcha(response) is True

    def test_captcha_check_handles_relative_response_url(self):
        """A malformed/relative response URL has no hostname and must not crash."""
        response = MagicMock()
        response.url = "/search?q=test"
        response.text = ""
        response.status_code = 200
        assert is_captcha(response) is False

    def test_normal_response_not_captcha(self):
        """Normal 200 response with results is NOT detected as CAPTCHA."""
        response = MagicMock()
        response.url = "https://www.google.com/search?q=test"
        response.text = "<html>normal results page</html>"
        response.status_code = 200
        assert is_captcha(response) is False

    def test_google_internal_url_filtering(self):
        """Google internal URLs (maps, accounts, support) are filtered out."""
        html = """
        <html><body>
        <a href="/url?q=https://maps.google.com/maps%3Fq%3Dtest&sa=U">Maps</a>
        <a href="/url?q=https://accounts.google.com/signin&sa=U">Sign in</a>
        <a href="/url?q=https://support.google.com/help&sa=U">Help</a>
        <a href="/url?q=https://www.google.com/preferences&sa=U">Settings</a>
        <a href="/url?q=https://example.com/real-result&sa=U">
            <div><h3><div>Real Result</div></h3></div>
            <div><div>A real snippet here for the result</div></div>
        </a>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) == 1
        assert results[0].url == "https://example.com/real-result"
        assert results[0].title == "Real Result"

    def test_cite_removal_from_title(self):
        """<cite> elements inside links are stripped from title text."""
        html = """
        <html><body>
        <a href="/url?q=https://example.com/page&sa=U">
            <h3>Page Title</h3><cite>example.com › page</cite>
            <div><div>This is the snippet text for the search result page</div></div>
        </a>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) == 1
        assert results[0].title == "Page Title"
        assert "example.com" not in results[0].title

    def test_breadcrumb_div_removal_from_title(self):
        """Breadcrumb divs with › are stripped from title text (Google SSR uses divs not cite)."""
        html = """
        <html><body>
        <a href="/url?q=https://example.com/page&sa=U">
            <div>
                <div><h3><div>Page Title</div></h3></div>
                <div><div>example.com › page</div></div>
            </div>
            <div><div><div>This is the snippet text describing the search result</div></div></div>
        </a>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) == 1
        assert results[0].title == "Page Title"

    def test_anchor_fragment_dedup(self):
        """URLs with #fragment are deduped against the base URL."""
        html = """
        <html><body>
        <a href="/url?q=https://example.com/page&sa=U">
            <h3>Page Title</h3>
            <div><div>Main result snippet text here for the actual result</div></div>
        </a>
        <a href="/url?q=https://example.com/page%23section-1&sa=U">
            <span>Section 1</span>
        </a>
        <a href="/url?q=https://example.com/page%23section-2&sa=U">
            <span>Section 2</span>
        </a>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) == 1
        assert results[0].url == "https://example.com/page"

    def test_carousel_items_filtered(self):
        """Google news carousel items (pcitem class) are skipped."""
        html = """
        <html><body>
        <a href="/url?q=https://example.com/real&sa=U">
            <h3>Real Result</h3>
            <div><div>Real snippet about the actual search result here</div></div>
        </a>
        <div class="pcitem">
            <a href="/url?q=https://news.example.com/carousel&sa=U">
                <span>Carousel News Item</span>
            </a>
        </div>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) == 1
        assert results[0].title == "Real Result"
        assert all("carousel" not in r.url for r in results)

    def test_non_http_urls_filtered(self):
        """Non-HTTP URLs (fragments, javascript:) are filtered out."""
        html = """
        <html><body>
        <a href="/url?q=%23&sa=U"><span>Fragment</span></a>
        <a href="/url?q=javascript:void(0)&sa=U"><span>JS</span></a>
        <a href="/url?q=https://example.com/real&sa=U">
            <h3>Real Result</h3>
            <div><div>A real search result snippet that is long enough</div></div>
        </a>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) == 1
        assert results[0].url == "https://example.com/real"

    def test_bare_domain_div_stripped_from_title(self):
        """Secondary results with bare domain divs (no ›) get domain stripped."""
        html = """
        <html><body>
        <a href="/url?q=https://justpy.io/&sa=U">
            <span><div class="UFvD1"><span>JustPy</span></div></span>
            <span><div class="BamJPe">justpy.io</div></span>
        </a>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) == 1
        assert results[0].title == "JustPy"
        assert "justpy.io" not in results[0].title

    def test_featured_snippet_text_cleaned(self):
        """'About Featured Snippets' UI text is stripped from snippet."""
        html = """
        <html><body>
        <a href="/url?q=https://example.com/page&sa=U">
            <h3>Example Page</h3>
        </a>
        <div>
            <div>The actual snippet content here with details.  About Featured Snippets</div>
        </div>
        </body></html>
        """
        results = google_extract(html)
        assert len(results) >= 1
        for r in results:
            assert "About Featured Snippets" not in r.snippet

    def test_empty_html_returns_empty(self):
        """Empty or minimal HTML returns no results."""
        results = google_extract("<html><body></body></html>")
        assert results == []


# ---------------------------------------------------------------------------
# DDG extraction
# ---------------------------------------------------------------------------


class TestDDGExtraction:
    """Test DuckDuckGo HTML extraction."""

    def test_normal_results_from_fixture(self):
        """Real DDG fixture produces expected results."""
        html = (FIXTURES / "ddg_html.html").read_text()
        results = ddg_extract(html)
        assert len(results) == 10
        # Should have Real Python as first result
        assert "realpython.com" in results[0].url
        # All results should have titles, URLs, and snippets
        for r in results:
            assert r.title
            assert r.url.startswith("http")
            assert r.snippet

    def test_inline_html_extraction(self):
        """DDG extraction works with minimal inline HTML."""
        html = """
        <html><body>
        <div class="result">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc">
                Example Page Title
            </a>
            <a class="result__snippet">This is a snippet about the page.</a>
        </div>
        <div class="result">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.com%2F&rut=def">
                Other Page
            </a>
            <a class="result__snippet">Another snippet here.</a>
        </div>
        </body></html>
        """
        results = ddg_extract(html)
        assert len(results) == 2
        assert results[0].title == "Example Page Title"
        assert results[0].url == "https://example.com/page"
        assert results[0].snippet == "This is a snippet about the page."
        assert results[1].url == "https://other.com/"

    def test_empty_html_returns_empty(self):
        """Empty HTML returns no results."""
        results = ddg_extract("<html><body></body></html>")
        assert results == []

    def test_adversarial_result_count_and_fields_are_bounded(self):
        huge = "x" * 20_000
        html = (
            "<html><body>"
            + "".join(
                (
                    '<div class="result">'
                    f'<a class="result__a" href="https://example.com/{i}">'
                    f"{huge}</a>"
                    f'<div class="result__snippet">{huge}</div>'
                    "</div>"
                )
                for i in range(2_000)
            )
            + "</body></html>"
        )
        results = ddg_extract(html)
        assert len(results) == 20
        assert all(len(result.title) <= 500 for result in results)
        assert all(len(result.snippet) <= 1_000 for result in results)


# ---------------------------------------------------------------------------
# Dedup and merge
# ---------------------------------------------------------------------------


class TestDedupAndMerge:
    """Test result dedup and merge logic."""

    def test_same_url_keeps_google_version(self):
        """When both engines return the same URL, Google version is kept."""
        google = [SearchResult("Google Title", "https://example.com/page", "Google snippet")]
        ddg = [SearchResult("DDG Title", "https://example.com/page", "DDG snippet")]
        merged, ddg_new = _dedup_and_merge(google, ddg)
        assert len(merged) == 1
        assert merged[0].title == "Google Title"
        assert ddg_new == 0

    def test_fragment_dedup(self):
        """url#section is deduped against url (base URL wins)."""
        google = [SearchResult("Page", "https://example.com/page", "snippet")]
        ddg = [SearchResult("Section", "https://example.com/page#section", "snippet")]
        merged, ddg_new = _dedup_and_merge(google, ddg)
        assert len(merged) == 1
        assert merged[0].title == "Page"
        assert ddg_new == 0

    def test_google_first_ddg_supplements(self):
        """Google results come first, DDG unique results appended after."""
        google = [
            SearchResult("G1", "https://google-only.com/a", "gs1"),
            SearchResult("G2", "https://shared.com/page", "gs2"),
        ]
        ddg = [
            SearchResult("D1", "https://shared.com/page", "ds1"),  # dup
            SearchResult("D2", "https://ddg-only.com/b", "ds2"),
        ]
        merged, ddg_new = _dedup_and_merge(google, ddg)
        assert len(merged) == 3
        assert merged[0].title == "G1"
        assert merged[1].title == "G2"  # Google version of shared URL
        assert merged[2].title == "D2"  # DDG supplement
        assert ddg_new == 1

    def test_www_normalization_dedup(self):
        """www.example.com and example.com are deduped."""
        google = [SearchResult("With WWW", "https://www.example.com/page", "s")]
        ddg = [SearchResult("Without WWW", "https://example.com/page", "s")]
        merged, ddg_new = _dedup_and_merge(google, ddg)
        assert len(merged) == 1
        assert ddg_new == 0


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------


class TestOutputFormat:
    """Test search output formatting."""

    def test_normal_output(self):
        """Normal search output has summary line and numbered results."""
        results = [
            SearchResult("Title One", "https://example.com/one", "Snippet one"),
            SearchResult("Title Two", "https://example.com/two", "Snippet two"),
        ]
        output = _format_output("test query", results, 2, 0, False, 1)
        assert output.startswith('Search: "test query" | google: 2 | ddg: 0 new | 2 total')
        assert "1. Title One" in output
        assert "   https://example.com/one" in output
        assert "   Snippet one" in output
        assert "2. Title Two" in output

    def test_adversarial_output_ends_at_complete_result_boundary(self):
        results = [
            SearchResult(
                "t" * 10_000,
                f"https://example.com/{index}",
                "s" * 100_000,
            )
            for index in range(1_000)
        ]
        output = _format_results(results)
        assert len(output) <= _MAX_RESULTS_OUTPUT_CHARS
        assert "trailing search results" in output
        assert output.count("https://example.com/") > 0

    def test_captcha_output(self):
        """CAPTCHA summary shows 'captcha' for google count."""
        results = [SearchResult("DDG Result", "https://ddg.com/r", "snippet")]
        output = _format_output("test", results, 0, 1, True, 1)
        assert "google: captcha" in output
        assert "ddg: 1 new" in output

    def test_page2_output(self):
        """Page 2+ shows 'n/a' for DDG."""
        results = [SearchResult("G", "https://g.com", "s")]
        output = _format_output("test", results, 1, 0, False, 2)
        assert "(page 2)" in output
        assert "ddg: n/a" in output

    def test_empty_results_output(self):
        """Empty results show 'No results found.' message."""
        output = _format_output("xyzzy nonsense", [], 0, 0, False, 1)
        assert "google: 0" in output
        assert "ddg: 0 new" in output
        assert "0 total" in output
        assert "No results found." in output


class TestTransportErrorsAreVisible:
    """A network failure must never render as a bare '0'.

    The regression: both engines threw, both returned empty, and the output said
    'google: 0 | ddg: 0 new | 0 total / No results found.' — indistinguishable
    from a query that genuinely has no hits. The caller concluded search was
    useless and worked around it.
    """

    def test_both_engines_failed_says_so(self):
        output = _format_output(
            "reddit api rate limits",
            [],
            0,
            0,
            False,
            1,
            google_error="ConnectTimeout: timed out",
            ddg_error="ConnectTimeout: timed out",
        )
        assert "google: ERROR" in output
        assert "ddg: ERROR" in output
        assert "Search FAILED" in output
        assert "ConnectTimeout" in output
        # The critical distinction the old output failed to make.
        assert "not an empty result set" in output
        assert "No results found." not in output

    def test_partial_failure_keeps_results_and_flags_engine(self):
        results = [SearchResult("DDG hit", "https://ddg.com/r", "snippet")]
        output = _format_output(
            "query",
            results,
            0,
            1,
            False,
            1,
            google_error="HTTP 503",
        )
        assert "google: ERROR" in output
        assert "ddg: 1 new" in output
        assert "Partial results" in output
        assert "HTTP 503" in output
        assert "1. DDG hit" in output

    def test_clean_empty_result_is_unchanged(self):
        """A real zero-hit query must still read as a normal empty result."""
        output = _format_output("xyzzy nonsense", [], 0, 0, False, 1)
        assert "No results found." in output
        assert "Search FAILED" not in output
        assert "ERROR" not in output

    def test_captcha_is_not_reported_as_an_error(self):
        results = [SearchResult("DDG hit", "https://ddg.com/r", "s")]
        output = _format_output("q", results, 0, 1, True, 1)
        assert "google: captcha" in output
        assert "ERROR" not in output

    def test_page2_ddg_error_suppressed(self):
        """DDG is not queried on page 2+, so it must not be blamed there."""
        output = _format_output("q", [], 0, 0, False, 2, ddg_error="stale")
        assert "ddg: n/a" in output
        assert "stale" not in output


class TestEngineErrorReturns:
    """The engine functions must report transport failures to the aggregator."""

    async def test_google_request_exception_returns_error(self):
        session = MagicMock()
        session.get = AsyncMock(side_effect=RuntimeError("connection reset"))
        results, captcha, error = await google_search(session, "q", 1)
        assert results == []
        assert captcha is False
        assert "RuntimeError" in error and "connection reset" in error

    async def test_google_non_200_returns_error(self):
        resp = MagicMock()
        resp.text = ""
        resp.url = "https://www.google.com/search?q=q"
        resp.status_code = 503
        session = MagicMock()
        session.get = AsyncMock(return_value=resp)
        results, captcha, error = await google_search(session, "q", 1)
        assert results == []
        assert error == "HTTP 503"

    async def test_ddg_request_exception_returns_error(self):
        session = MagicMock()
        session.get = AsyncMock(side_effect=RuntimeError("dns failure"))
        results, error = await ddg_search(session, "q")
        assert results == []
        assert "RuntimeError" in error and "dns failure" in error

    async def test_ddg_success_returns_no_error(self):
        resp = MagicMock()
        resp.text = '<html><body><div class="no-results">No results found for q</div></body></html>'
        resp.url = "https://html.duckduckgo.com/html/"
        resp.status_code = 200
        session = MagicMock()
        session.get = AsyncMock(return_value=resp)
        results, error = await ddg_search(session, "q")
        assert error is None

    @pytest.mark.parametrize(
        ("engine", "expected"),
        [
            ("google", "Unexpected Google response shape"),
            ("ddg", "Unexpected DuckDuckGo response shape"),
        ],
    )
    async def test_unknown_200_shape_is_an_error(self, engine, expected):
        """Markup breakage cannot masquerade as a legitimate zero-result query."""
        resp = MagicMock()
        resp.text = "<html><body>unknown provider shell</body></html>"
        resp.url = "https://www.google.com/search?q=q" if engine == "google" else "https://html.duckduckgo.com/html/"
        resp.status_code = 200
        session = MagicMock()
        session.get = AsyncMock(return_value=resp)

        if engine == "google":
            results, captcha, error = await google_search(session, "q", 1)
            assert captcha is False
        else:
            results, error = await ddg_search(session, "q")

        assert results == []
        assert expected in error


# ---------------------------------------------------------------------------
# Integration: search() function with mocked HTTP
# ---------------------------------------------------------------------------


class TestSearchIntegration:
    """Integration tests for the search() pipeline with mocked HTTP."""

    @pytest.fixture(autouse=True)
    def clear_state(self):
        """Clear cache and reset module state before each test."""
        import src.fetchaller.search as search_mod
        from src.fetchaller.search.ddg import search_ddg
        from src.fetchaller.search.google import search_google

        _cache.clear()
        search_mod._captcha_backoff_until = 0.0
        search_mod._captcha_count = 0
        search_mod._google_last_request = 0.0
        search_mod._ddg_last_request = 0.0

        # Bypass rate limiter sleeps — these tests use mocked HTTP
        async def _fast_google(session, query, page):
            return await search_google(session, query, page)

        async def _fast_ddg(session, query):
            return await search_ddg(session, query)

        with (
            patch("src.fetchaller.search._rate_limited_google", _fast_google),
            patch("src.fetchaller.search._rate_limited_ddg", _fast_ddg),
        ):
            yield

        _cache.clear()

    def _mock_response(self, html: str, url: str = "https://www.google.com/search?q=test", status: int = 200):
        """Create a mock response object."""
        resp = MagicMock()
        resp.text = html
        resp.url = url
        resp.status_code = status
        return resp

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self):
        """Empty query returns error dict without hitting engines."""
        result = await search("", page=1)
        assert "error" in result
        assert result["error"] == "Search query cannot be empty."

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_error(self):
        """Whitespace-only query returns error."""
        result = await search("   ", page=1)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_overlong_query_is_rejected_without_hitting_engines(self):
        """Bound query memory, provider URLs, cache keys, output, and logs."""
        with patch("src.fetchaller.search._get_session") as get_session:
            result = await search("x" * 513, page=1)

        assert result == {"error": "Search query is too long (maximum 512 characters)."}
        get_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_schema_maximum_query_length_is_accepted(self):
        """The implementation accepts the MCP schema's exact maxLength."""

        async def google(*_args):
            return [
                SearchResult("Result", "https://example.com/", "snippet")
            ], False, None

        async def ddg(*_args):
            return [], None

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            result = await search("x" * 512, page=1)

        assert "content" in result
        assert "Result" in result["content"]

    @pytest.mark.parametrize("page", [0, -1, 101])
    @pytest.mark.asyncio
    async def test_page_out_of_range_is_rejected(self, page):
        """Provider pagination is bounded before sessions or cache access."""
        with patch("src.fetchaller.search._get_session") as get_session:
            result = await search("query", page=page)

        assert result == {"error": "Search page must be between 1 and 100."}
        get_session.assert_not_called()

    @pytest.mark.parametrize("page", [True, 1.5, "2"])
    @pytest.mark.asyncio
    async def test_noninteger_page_is_rejected(self, page):
        """Direct callers get the same strict integer contract as MCP schema."""
        result = await search("query", page=page)

        assert result == {"error": "Search page must be an integer."}

    @pytest.mark.asyncio
    async def test_query_control_characters_are_normalized(self):
        """Control characters cannot inject lines into summaries or log fields."""
        google_result = [SearchResult("Google", "https://google-result.example/", "snippet")]

        async def google(*_args):
            return google_result, False, None

        async def ddg(*_args):
            return [], None

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            result = await search("  alpha\r\nINJECT\tbeta  ", page=1)

        assert result["content"].startswith('Search: "alpha INJECT beta" | google: 1')
        assert "\r" not in result["content"]

    @pytest.mark.asyncio
    async def test_parallel_engine_error_does_not_abandon_sibling(self):
        """One engine error still allows the other engine to return results."""
        ddg_finished = asyncio.Event()

        async def google(*_args):
            raise RuntimeError("google failed")

        async def ddg(*_args):
            await asyncio.sleep(0)
            ddg_finished.set()
            return [SearchResult("DDG", "https://ddg-result.example/", "snippet")], None

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            result = await search("partial engine error", page=1)

        assert ddg_finished.is_set()
        assert "Partial results" in result["content"]
        assert "google: ERROR" in result["content"]
        assert "DDG" in result["content"]

    @pytest.mark.asyncio
    async def test_outer_cancellation_cancels_and_drains_both_engines(self):
        """Cancelling search cannot leave either provider task running."""
        google_started = asyncio.Event()
        ddg_started = asyncio.Event()
        google_finished = asyncio.Event()
        ddg_finished = asyncio.Event()
        never = asyncio.Event()

        async def google(*_args):
            google_started.set()
            try:
                await never.wait()
            finally:
                google_finished.set()

        async def ddg(*_args):
            ddg_started.set()
            try:
                await never.wait()
            finally:
                ddg_finished.set()

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            search_task = asyncio.create_task(search("cancel both engines", page=1))
            await asyncio.wait_for(google_started.wait(), timeout=1)
            await asyncio.wait_for(ddg_started.wait(), timeout=1)
            search_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await search_task

        await asyncio.wait_for(google_finished.wait(), timeout=1)
        await asyncio.wait_for(ddg_finished.wait(), timeout=1)
        assert not [
            task for task in asyncio.all_tasks() if task.get_name().startswith("fetchaller-search-") and not task.done()
        ]

    @pytest.mark.asyncio
    async def test_outer_cancellation_detaches_noncooperative_engines(self):
        """Provider cancellation suppression cannot hold an MCP caller open."""
        release = asyncio.Event()
        started = [asyncio.Event(), asyncio.Event()]
        finished = [asyncio.Event(), asyncio.Event()]

        async def noncooperative(index):
            started[index].set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished[index].set()

        async def google(*_args):
            await noncooperative(0)
            return [], False, None

        async def ddg(*_args):
            await noncooperative(1)
            return [], None

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            caller = asyncio.create_task(search("noncooperative cancellation"))
            await asyncio.gather(*(event.wait() for event in started))
            caller.cancel()
            done, _ = await asyncio.wait({caller}, timeout=0.2)
            try:
                assert caller in done
                with pytest.raises(asyncio.CancelledError):
                    caller.result()
            finally:
                release.set()
                await asyncio.gather(*(event.wait() for event in finished))

    def test_cancel_cleanup_retrieves_an_already_done_exception(self):
        """Cancellation races must not emit unhandled task exceptions."""
        import src.fetchaller.search as search_mod

        done = MagicMock()
        done.done.return_value = True

        search_mod._cancel_and_detach((done,))

        done.exception.assert_called_once_with()
        done.cancel.assert_not_called()
        done.add_done_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_child_self_cancellation_is_engine_failure_not_outer_cancel(
        self,
    ):
        """A provider cancelling itself cannot cancel the completed sibling."""

        async def google(*_args):
            raise asyncio.CancelledError

        async def ddg(*_args):
            return [SearchResult("DDG", "https://ddg-result.example/", "snippet")], None

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            result = await search("self cancelled provider", page=1)

        assert "content" in result
        assert "Partial results" in result["content"]
        assert "CancelledError" in result["content"]
        assert "DDG" in result["content"]

    @pytest.mark.asyncio
    async def test_page_two_provider_self_cancellation_is_search_failure(self):
        """A child self-cancel must not cancel its MCP request task."""

        async def google(*_args):
            raise asyncio.CancelledError

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
        ):
            result = await search("self cancelled provider page two", page=2)

        assert "error" in result
        assert "CancelledError" in result["error"]

    @pytest.mark.asyncio
    async def test_search_log_uses_length_not_query_text(self, capsys):
        """Search terms and embedded secrets are absent from stderr."""
        query = "private-token=supersecret"

        async def google(*_args):
            return [], False, None

        async def ddg(*_args):
            return [], None

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            await search(query, page=1)

        stderr = capsys.readouterr().err
        assert query not in stderr
        assert "supersecret" not in stderr
        assert f"query_len={len(query)}" in stderr

    @pytest.mark.asyncio
    async def test_engine_exception_diagnostic_is_bounded_and_redacted(
        self,
        capsys,
    ):
        """Transport exception URLs/secrets/control text cannot leak to logs."""
        secret = "never-log-this"
        error = RuntimeError(
            f"GET https://user:password@example.com/private/path?token={secret}\nINJECT" + ("x" * 1_000)
        )

        async def google(*_args):
            raise error

        async def ddg(*_args):
            return [], None

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=MagicMock(),
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=MagicMock(),
            ),
            patch("src.fetchaller.search._rate_limited_google", google),
            patch("src.fetchaller.search._rate_limited_ddg", ddg),
        ):
            result = await search("safe query", page=1)

        stderr = capsys.readouterr().err
        assert secret not in stderr
        assert "password" not in stderr
        assert "/private/path" not in stderr
        assert "\nINJECT" not in stderr
        assert len(stderr) < 1_000
        assert secret not in result["content"]

    @pytest.mark.asyncio
    async def test_full_pipeline_with_fixtures(self):
        """Full pipeline: mock Google + DDG returning fixture HTML, verify merged output."""
        google_html = (FIXTURES / "google_ssr.html").read_text()
        ddg_html = (FIXTURES / "ddg_html.html").read_text()

        google_resp = self._mock_response(google_html)
        ddg_resp = self._mock_response(ddg_html, url="https://html.duckduckgo.com/html/?q=test")

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "google.com" in url:
                return google_resp
            return ddg_resp

        mock_session = AsyncMock()
        mock_session.get = mock_get

        with (
            patch("src.fetchaller.search._get_session", return_value=mock_session),
            patch("src.fetchaller.search._get_ddg_session", return_value=mock_session),
        ):
            result = await search("python asyncio tutorial", page=1)

        assert "content" in result
        content = result["content"]
        assert content.startswith('Search: "python asyncio tutorial"')
        assert "google:" in content
        assert "ddg:" in content
        # Should have numbered results
        assert "1." in content
        assert "https://" in content

    @pytest.mark.asyncio
    async def test_cache_hit_skips_fetch(self):
        """Second identical query returns cached result without re-fetching."""
        google_html = (FIXTURES / "google_ssr.html").read_text()
        ddg_html = (FIXTURES / "ddg_html.html").read_text()

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "google.com" in url:
                return self._mock_response(google_html)
            return self._mock_response(ddg_html, url="https://html.duckduckgo.com/html/?q=test")

        mock_session = AsyncMock()
        mock_session.get = mock_get

        with (
            patch("src.fetchaller.search._get_session", return_value=mock_session),
            patch("src.fetchaller.search._get_ddg_session", return_value=mock_session),
        ):
            result1 = await search("cache test query", page=1)
            first_call_count = call_count
            result2 = await search("cache test query", page=1)

        assert result1 == result2
        assert call_count == first_call_count  # No additional fetches

    @pytest.mark.asyncio
    async def test_captcha_degraded_still_cached(self):
        """CAPTCHA-degraded results (DDG only) ARE cached to avoid re-hitting DDG during backoff."""
        ddg_html = (FIXTURES / "ddg_html.html").read_text()

        async def mock_get(url, **kwargs):
            if "google.com" in url:
                return self._mock_response(
                    "unusual traffic detected",
                    url="https://sorry.google.com/sorry/index",
                    status=200,
                )
            return self._mock_response(ddg_html, url="https://html.duckduckgo.com/html/?q=test")

        mock_session = AsyncMock()
        mock_session.get = mock_get

        with (
            patch("src.fetchaller.search._get_session", return_value=mock_session),
            patch("src.fetchaller.search._get_ddg_session", return_value=mock_session),
        ):
            result = await search("captcha test", page=1)

        assert "content" in result
        assert "google: captcha" in result["content"]
        # DDG-only results should be cached (captcha flag preserved in cache)
        assert ("captcha test", 1) in _cache
        cached = _cache[("captcha test", 1)]
        assert cached[3] is True  # captcha flag preserved

    @pytest.mark.asyncio
    async def test_explicit_empty_results_not_cached(self):
        """Honest zero results are returned normally but never cached."""

        async def mock_get(url, **kwargs):
            if "google.com" in url:
                return self._mock_response("<html><body>No results found for query</body></html>")
            return self._mock_response(
                '<html><body><div class="no-results">No results found for query</div></body></html>',
                url="https://html.duckduckgo.com/html/?q=test",
            )

        mock_session = AsyncMock()
        mock_session.get = mock_get

        with (
            patch("src.fetchaller.search._get_session", return_value=mock_session),
            patch("src.fetchaller.search._get_ddg_session", return_value=mock_session),
        ):
            result = await search("empty results test", page=1)

        assert "content" in result
        assert "No results found." in result["content"]
        assert ("empty results test", 1) not in _cache

    @pytest.mark.asyncio
    async def test_all_engine_shape_failures_return_error_not_content(self):
        """Total provider failure must set MCP isError via the error key."""

        async def mock_get(url, **kwargs):
            return self._mock_response(
                "<html><body>unrecognized changed markup</body></html>",
                url=url,
            )

        mock_session = AsyncMock()
        mock_session.get = mock_get

        with (
            patch(
                "src.fetchaller.search._get_session",
                return_value=mock_session,
            ),
            patch(
                "src.fetchaller.search._get_ddg_session",
                return_value=mock_session,
            ),
        ):
            result = await search("both providers broken", page=1)

        assert "content" not in result
        assert "Search FAILED" in result["error"]
        assert "Unexpected Google response shape" in result["error"]
        assert "Unexpected DuckDuckGo response shape" in result["error"]

    @pytest.mark.asyncio
    async def test_page2_skips_ddg(self):
        """Page 2+ only queries Google, not DDG."""
        google_html = (FIXTURES / "google_ssr.html").read_text()
        urls_called = []

        async def mock_get(url, **kwargs):
            urls_called.append(url)
            return self._mock_response(google_html)

        mock_session = AsyncMock()
        mock_session.get = mock_get

        with (
            patch("src.fetchaller.search._get_session", return_value=mock_session),
            patch("src.fetchaller.search._get_ddg_session", return_value=mock_session),
        ):
            result = await search("page 2 test", page=2)

        assert "content" in result
        assert "(page 2)" in result["content"]
        assert "ddg: n/a" in result["content"]
        # Only Google should have been called
        assert all("google.com" in url for url in urls_called)
        assert not any("duckduckgo" in url for url in urls_called)


# ---------------------------------------------------------------------------
# Dedup key
# ---------------------------------------------------------------------------


class TestDedupKey:
    """Test URL dedup key generation."""

    def test_strips_fragment(self):
        """Fragment is stripped before normalization."""
        assert _dedup_key("https://example.com/page#section") == _dedup_key("https://example.com/page")

    def test_strips_tracking_params(self):
        """Tracking params are stripped."""
        assert _dedup_key("https://example.com/page?utm_source=google") == _dedup_key("https://example.com/page")

    def test_normalizes_www(self):
        """www prefix is normalized."""
        key1 = _dedup_key("https://www.example.com/page")
        key2 = _dedup_key("https://example.com/page")
        assert key1 == key2


class TestEngineSessionsAreSeparate:
    """Google and DDG must not share a TLS identity.

    Regression: both engines used one Opera Mini session. That profile is
    required by Google (its SSR request declares client=ms-opera-mini-android),
    but DDG answers it with HTTP 202 and the generic homepage instead of
    results — so DDG returned nothing on every query, and the old
    "non-200 -> empty list" handling rendered that as a believable 'ddg: 0 new'.
    """

    def teardown_method(self):
        import src.fetchaller.search as search_mod

        search_mod._session = None
        search_mod._ddg_session = None

    async def test_ddg_session_is_not_the_google_session(self):
        import src.fetchaller.search as search_mod

        google_session = await search_mod._get_session()
        ddg_session = await search_mod._get_ddg_session()
        assert google_session is not ddg_session

    # Assert on the kwargs fetchaller passes, not on wafer's internals — the
    # profile choice is the part this codebase owns.
    async def test_google_session_keeps_opera_mini(self):
        from wafer import Profile

        import src.fetchaller.search as search_mod

        captured = {}

        def _factory(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("wafer.AsyncSession", side_effect=_factory):
            await search_mod._get_session()
        assert captured.get("profile") == Profile.OPERA_MINI

    async def test_ddg_session_does_not_use_opera_mini(self):
        import src.fetchaller.search as search_mod

        captured = {}

        def _factory(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("wafer.AsyncSession", side_effect=_factory):
            await search_mod._get_ddg_session()
        # Must not inherit Google's Opera Mini identity — that is what DDG 202s on.
        assert "profile" not in captured or captured["profile"] is None

    async def test_sessions_are_memoized(self):
        import src.fetchaller.search as search_mod

        assert await search_mod._get_ddg_session() is await search_mod._get_ddg_session()

    async def test_close_session_releases_both(self):
        import src.fetchaller.search as search_mod

        await search_mod._get_session()
        await search_mod._get_ddg_session()
        await search_mod.close_session()
        assert search_mod._session is None
        assert search_mod._ddg_session is None
