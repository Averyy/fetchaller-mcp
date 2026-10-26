"""Search provider parsing, pagination, fallback, provenance and cancellation."""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.fetchaller.search as search_mod
from src.fetchaller.ratelimit import DomainRateLimiter
from src.fetchaller.search import _MAX_RESULTS_OUTPUT_CHARS, _cache, _dedup_key, _format_results, google, search
from src.fetchaller.search import brave as brave_mod
from src.fetchaller.search import ddg as ddg_mod
from src.fetchaller.search.brave import extract_results as brave_extract
from src.fetchaller.search.brave import fetch_brave
from src.fetchaller.search.ddg import MAX_DDG_PAGE
from src.fetchaller.search.ddg import extract_results as ddg_extract
from src.fetchaller.search.ddg import search_ddg as ddg_search

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def provider_pacing_state(monkeypatch):
    # Unit tests use captured responses without wall-clock waits. Dedicated
    # timing tests below restore the real interval on a controlled clock.
    for module, name in ((brave_mod, "brave_search_limiter"), (ddg_mod, "ddg_search_limiter")):
        monkeypatch.setattr(module, name, DomainRateLimiter(0.0, jitter=(0.0, 0.0)))
        monkeypatch.setattr(module, "_request_lock", asyncio.Lock())
        monkeypatch.setattr(module, "_backoff_until", 0.0)


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


class TestDDGPaging:
    """DDG has no page parameter: page N is the previous page's "Next" form, POSTed."""

    @pytest.fixture(autouse=True)
    def clear_pages(self):
        import src.fetchaller.search.ddg as ddg_mod

        ddg_mod._pages.clear()
        yield
        ddg_mod._pages.clear()

    @staticmethod
    def _page(urls, next_s=None, prev_s=None):
        rows = "".join(
            f'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg={u}">T {u}</a>'
            f'<a class="result__snippet">snip</a></div>'
            for u in urls
        )
        form = ""
        if prev_s is not None:
            form += (
                '<form action="/html/" method="post">'
                '<input type="submit" value="Previous">'
                '<input type="hidden" name="q" value="q">'
                f'<input type="hidden" name="s" value="{prev_s}">'
                '<input type="hidden" name="dc" value="1">'
                '<input type="hidden" name="vqd" value="4-token">'
                "</form>"
            )
        if next_s is not None:
            form += (
                '<form action="/html/" method="post">'
                '<input type="submit" value="Next">'
                '<input type="hidden" name="q" value="q">'
                f'<input type="hidden" name="s" value="{next_s}">'
                '<input type="hidden" name="dc" value="11">'
                '<input type="hidden" name="vqd" value="4-token">'
                "</form>"
            )
        return f"<html><body>{rows}{form}</body></html>"

    @staticmethod
    def _resp(html, status=200):
        resp = MagicMock()
        resp.text = html
        resp.status_code = status
        resp.challenge_type = None
        return resp

    def _session(self, *pages):
        """GET answers page 1; each POST answers the next scripted page."""
        session = MagicMock()
        session.get = AsyncMock(return_value=self._resp(pages[0]))
        session.post = AsyncMock(side_effect=[self._resp(p) for p in pages[1:]])
        return session

    async def test_page_two_posts_the_next_form(self):
        session = self._session(
            self._page(["https://a.example/1", "https://a.example/2"], next_s=2),
            self._page(["https://a.example/3"], next_s=3),
        )
        results, error = await ddg_search(session, "q", 2)
        assert error is None
        assert [r.url for r in results] == ["https://a.example/3"]
        session.get.assert_awaited_once()
        form = session.post.await_args.kwargs["form"]
        assert (form["s"], form["dc"], form["vqd"], form["kp"]) == ("2", "11", "4-token", "-2")

    async def test_page_drops_urls_given_on_earlier_pages(self):
        """DDG re-ranks between requests, so page 2 repeats page 1 URLs."""
        session = self._session(
            self._page(["https://a.example/1", "https://a.example/2"], next_s=2),
            self._page(["https://a.example/2/", "https://a.example/3"], next_s=3),
        )
        results, error = await ddg_search(session, "q", 2)
        assert error is None
        assert [r.url for r in results] == ["https://a.example/3"]

    async def test_page_three_after_page_two_costs_one_post(self):
        session = self._session(
            self._page(["https://a.example/1"], next_s=1),
            self._page(["https://a.example/2"], next_s=2),
            self._page(["https://a.example/3"], next_s=3),
        )
        await ddg_search(session, "q", 2)
        results, error = await ddg_search(session, "q", 3)
        assert error is None
        assert [r.url for r in results] == ["https://a.example/3"]
        assert session.get.await_count == 1
        assert session.post.await_count == 2

    async def test_cold_page_three_walks_page_two(self):
        session = self._session(
            self._page(["https://a.example/1"], next_s=1),
            self._page(["https://a.example/2"], next_s=2),
            self._page(["https://a.example/3"], next_s=3),
        )
        results, error = await ddg_search(session, "q", 3)
        assert error is None
        assert [r.url for r in results] == ["https://a.example/3"]
        assert session.post.await_count == 2

    async def test_previous_form_is_never_followed(self):
        """From page 2 DDG lists "Previous" (s=0) before "Next"; page 3 is Next's offset."""
        session = self._session(
            self._page(["https://a.example/1"], next_s=10),
            self._page(["https://a.example/2"], next_s=25, prev_s=0),
            self._page(["https://a.example/3"], next_s=40, prev_s=10),
        )
        results, error = await ddg_search(session, "q", 3)
        assert error is None
        assert [r.url for r in results] == ["https://a.example/3"]
        offsets = [call.kwargs["form"]["s"] for call in session.post.await_args_list]
        assert offsets == ["10", "25"]

    async def test_last_page_with_only_previous_has_no_next(self):
        """A last page offers only "Previous"; that must not read as a way forward."""
        session = self._session(
            self._page(["https://a.example/1"], next_s=10),
            self._page(["https://a.example/2"], next_s=None, prev_s=0),
        )
        results, error = await ddg_search(session, "q", 3)
        assert (results, error) == ([], None)
        assert session.post.await_count == 1

    async def test_every_request_in_a_walk_is_paced(self):
        session = self._session(
            self._page(["https://a.example/1"], next_s=1),
            self._page(["https://a.example/2"], next_s=2),
            self._page(["https://a.example/3"], next_s=3),
        )
        calls = []

        async def pace():
            calls.append(1)

        await ddg_search(session, "q", 3, pace=pace)
        assert len(calls) == 3

    async def test_page_past_the_last_is_empty_not_an_error(self):
        session = self._session(self._page(["https://a.example/1"], next_s=None))
        results, error = await ddg_search(session, "q", 2)
        assert (results, error) == ([], None)
        session.post.assert_not_awaited()

    async def test_throttled_page_is_an_error(self):
        session = self._session(
            self._page(["https://a.example/1"], next_s=1),
            "<html><body>DuckDuckGo homepage</body></html>",
        )
        session.post = AsyncMock(return_value=self._resp("<html></html>", status=202))
        results, error = await ddg_search(session, "q", 2)
        assert results == []
        assert "HTTP 202" in error

    async def test_page_past_the_cap_makes_no_request(self):
        session = self._session(self._page(["https://a.example/1"], next_s=1))
        results, error = await ddg_search(session, "q", MAX_DDG_PAGE + 1)
        assert results == []
        assert error
        session.get.assert_not_awaited()


class TestBrave:
    """Brave is one GET per page: `offset` is the zero-based page index."""

    @pytest.fixture(autouse=True)
    def clear_state(self):
        _cache.clear()
        yield
        _cache.clear()

    @staticmethod
    def _page(rows, banner=False, extra=""):
        snippets = "".join(
            f'<div class="snippet svelte-x" data-type="web" data-pos="{i}">'
            f'<a href="{url}" class="l1"><span>site</span><div class="title">{title}</div></a>'
            f'<div class="content"><div class="generic-snippet">{desc}</div></div></div>'
            for i, (url, title, desc) in enumerate(rows)
        )
        banner_html = '<div id="bad-results-info-banner">Not many great matches</div>' if banner else ""
        return f"<html><head><title>q - Brave Search</title></head><body>{banner_html}{extra}{snippets}</body></html>"

    @staticmethod
    def _session(html, status=200):
        resp = MagicMock()
        resp.text = html
        resp.status_code = status
        resp.challenge_type = None
        session = MagicMock()
        session.get = AsyncMock(return_value=resp)
        return session

    def test_extracts_only_organic_web_snippets(self):
        cluster = '<div class="snippet" data-type="cluster"><a href="https://news.example/x"><div class="title">News</div></a></div>'
        html = self._page([("https://a.example/1", "First", "About first")], extra=cluster)
        results = brave_extract(html)
        assert [(r.title, r.url, r.snippet) for r in results] == [("First", "https://a.example/1", "About first")]

    def test_skips_a_snippet_without_a_title_or_an_http_link(self):
        html = self._page([("https://a.example/1", "", "no title"), ("/relative", "Rel", "no http")])
        assert brave_extract(html) == []

    async def test_page_one_sends_no_offset(self):
        session = self._session(self._page([("https://a.example/1", "First", "d")]))
        results, error = await fetch_brave(session, "q", 1)
        assert error is None and len(results) == 1
        assert "offset" not in session.get.await_args.kwargs["params"]

    async def test_page_n_sends_the_zero_based_offset(self):
        session = self._session(self._page([("https://a.example/1", "First", "d")]))
        await fetch_brave(session, "q", 3)
        assert session.get.await_args.kwargs["params"]["offset"] == "2"

    async def test_the_pace_runs_before_the_request(self):
        session = self._session(self._page([("https://a.example/1", "First", "d")]))
        order = []

        async def pace():
            order.append("pace")

        async def get(*_a, **_k):
            order.append("get")
            return session.get.return_value

        session.get = get
        await fetch_brave(session, "q", 1, pace=pace)
        assert order == ["pace", "get"]

    async def test_banner_with_no_results_is_empty_not_an_error(self):
        session = self._session(self._page([], banner=True))
        assert await fetch_brave(session, "q", 40) == ([], None)

    async def test_unrecognized_200_is_an_error(self):
        session = self._session("<html><body>something else</body></html>")
        results, error = await fetch_brave(session, "q", 1)
        assert results == [] and "Unexpected Brave response shape" in error

    async def test_non_200_is_an_error(self):
        results, error = await fetch_brave(self._session("", status=429), "q", 1)
        assert results == [] and "HTTP 429" in error and "backoff" in error

    async def test_request_exception_is_an_error(self):
        session = MagicMock()
        session.get = AsyncMock(side_effect=RuntimeError("dns failure"))
        results, error = await fetch_brave(session, "q", 1)
        assert results == [] and "RuntimeError" in error and "dns failure" in error


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


@pytest.fixture
def google_state(monkeypatch):
    google._document_unavailable.clear()
    google._cursors.clear()
    monkeypatch.setattr(google, "_last_request", 0)
    monkeypatch.setattr(google, "_backoff_until", 0)
    monkeypatch.setattr(google, "_throttle_count", 0)
    monkeypatch.setattr(google, "_last_document", None)
    monkeypatch.setattr(google, "_MIN_INTERVAL", 0)
    monkeypatch.delenv("FETCHALLER_GOOGLE_DOCUMENT_IDS", raising=False)
    yield
    google._document_unavailable.clear()
    google._cursors.clear()


def captured_response(name="page1", status=200, headers=None):
    return SimpleNamespace(
        status_code=status, headers=headers or {}, text=(FIXTURES / "google_docs" / f"{name}.json").read_text()
    )


class TestGoogleDocs:
    def test_real_pages_and_empty_response(self):
        first, cursor = google.parse_response(captured_response().text)
        second, next_cursor = google.parse_response(captured_response("page2").text)
        assert len(first) == len(second) == 10
        assert first[0].url == "https://docs.python.org/3/library/asyncio.html"
        assert "Asynchronous I/O" in first[0].title
        assert not {r.url for r in first} & {r.url for r in second}
        assert (cursor, next_cursor) == ("CAo=", "CBQ=")
        assert google.parse_response(captured_response("empty").text) == ([], None)
        assert google.parse_response(captured_response().text.split("\n", 1)[1])[0] == first

    @pytest.mark.parametrize(
        "body",
        [
            "<html>Sign in</html>",
            "{}",
            "[]",
            "[[]]",
            '[["wrong"]]',
            '[["documentsuggest.search.search_response",{}]]',
            '[["documentsuggest.search.search_response",null,42]]',
        ],
    )
    def test_protocol_errors_do_not_become_empty_results(self, body):
        with pytest.raises(ValueError):
            google.parse_response(body)

    @pytest.mark.parametrize(("page", "offset"), [(1, 0), (2, 10), (14, 130), (100, 990)])
    def test_cursor_varint_roundtrip(self, page, offset):
        data = base64.b64decode(google.page_cursor(page))
        assert data[0] == 8
        decoded = sum((byte & 127) << (7 * index) for index, byte in enumerate(data[1:]))
        assert decoded == offset
        assert all(byte & 128 for byte in data[1:-1])
        assert data[-1] < 128

    async def test_request_form_and_cached_server_cursor(self, google_state):
        session = SimpleNamespace(post=AsyncMock(return_value=captured_response()))
        query = 'Louvre musée & "art"'
        first = await google.search_google(session, query)
        assert len(first[0]) == 10 and first[1:] == (False, None)
        url = session.post.call_args.args[0]
        assert url.startswith("https://docs.google.com/document/d/") and url.endswith("/explore/search")
        form = session.post.call_args.kwargs["form"]
        assert json.loads(form["request"]) == [
            "documentsuggest.search.search_request",
            query,
            [10, "CAA="],
            ["en", "en"],
            1,
        ]
        # Server-provided cursors take precedence over the synthesized offset.
        google._cursors[(query, "en", 10, 2)] = ("server-cursor", google.time.monotonic())
        await google.search_google(session, query, page=2)
        assert json.loads(session.post.call_args.kwargs["form"]["request"])[2] == [10, "server-cursor"]

    async def test_random_selection_avoids_consecutive_reuse(self, google_state):
        session = SimpleNamespace(post=AsyncMock(return_value=captured_response()))
        with patch.object(google.random, "shuffle", wraps=google.random.shuffle) as shuffle:
            for _ in range(5):
                await google.search_google(session, "Python asyncio documentation")
        urls = [call.args[0] for call in session.post.call_args_list]
        assert all(a != b for a, b in zip(urls, urls[1:]))
        assert shuffle.call_count == 5

    @pytest.mark.parametrize("status", [401, 403, 404, 302])
    async def test_failed_document_is_skipped_and_warned(self, google_state, capsys, status):
        session = SimpleNamespace(
            post=AsyncMock(
                side_effect=[
                    captured_response(
                        status=status, headers={"location": "https://accounts.google.com/signin?secret=ignored"}
                    ),
                    captured_response(),
                ]
            )
        )
        result = await google.search_google(session, "Python asyncio documentation", page=2)
        assert len(result[0]) == 10 and result[2] is None
        calls = session.post.call_args_list
        assert len(calls) == 2 and calls[0].args[0] != calls[1].args[0]
        assert calls[0].kwargs["form"] == calls[1].kwargs["form"]
        failed = calls[0].args[0].split("/d/")[1].split("/")[0]
        await google.search_google(session, "another query")
        assert failed not in session.post.call_args.args[0]
        log = capsys.readouterr().err
        assert "WARNING" in log and "cooling down and skipping" in log
        assert "secret=ignored" not in log

    async def test_all_documents_unavailable_is_bounded(self, google_state):
        session = SimpleNamespace(post=AsyncMock(return_value=captured_response(status=403)))
        result = await google.search_google(session, "query")
        assert result[2] == "Google document unavailable (HTTP 403)"
        assert session.post.await_count == 3
        result = await google.search_google(session, "query")
        assert "cooldown" in result[2] and session.post.await_count == 3

    async def test_429_honors_global_backoff_without_rotating(self, google_state):
        session = SimpleNamespace(
            post=AsyncMock(return_value=captured_response(status=429, headers={"retry-after": "600"}))
        )
        first = await google.search_google(session, "query")
        second = await google.search_google(session, "different query")
        assert first[1] and "600s" in first[2]
        assert second[1] and "backoff" in second[2]
        assert session.post.await_count == 1

    @pytest.mark.parametrize("failure", [captured_response(status=503), TimeoutError("deadline")])
    async def test_transient_failure_retries_once_same_document(self, google_state, failure):
        session = SimpleNamespace(post=AsyncMock(side_effect=[failure, captured_response()]))
        result = await google.search_google(session, "query")
        assert len(result[0]) == 10 and result[2] is None
        assert session.post.call_args_list[0] == session.post.call_args_list[1]

    async def test_protocol_failure_does_not_rotate(self, google_state):
        response = captured_response()
        response.text = "<html>Sign in</html>"
        session = SimpleNamespace(post=AsyncMock(return_value=response))
        result = await google.search_google(session, "query")
        assert "Unexpected Google response" in result[2]
        assert session.post.await_count == 1

    async def test_valid_empty_stops_and_ends_pagination(self, google_state):
        session = SimpleNamespace(post=AsyncMock(return_value=captured_response("empty")))
        assert await google.search_google(session, "site:example.invalid wafer-search") == ([], False, None)
        assert await google.search_google(session, "site:example.invalid wafer-search", 2) == ([], False, None)
        assert session.post.await_count == 1

    async def test_configurable_pool_and_invalid_configuration(self, google_state, monkeypatch):
        ids = google.document_ids()
        monkeypatch.setenv("FETCHALLER_GOOGLE_DOCUMENT_IDS", json.dumps([ids[0], ids[0]]))
        assert google.document_ids() == (ids[0],)
        session = SimpleNamespace(post=AsyncMock(return_value=captured_response()))
        for value in ("[]", '["../../sorry"]', "{}", "not-json"):
            monkeypatch.setenv("FETCHALLER_GOOGLE_DOCUMENT_IDS", value)
            result = await google.search_google(session, "query")
            assert "configuration" in result[2]
        session.post.assert_not_called()


@pytest.fixture
def search_state():
    _cache.clear()
    search_mod._sessions.clear()
    search_mod._session_solvers.clear()
    yield
    _cache.clear()
    search_mod._sessions.clear()
    search_mod._session_solvers.clear()


class TestUnifiedSearch:
    async def test_google_is_default_and_source_is_explicit(self, search_state):
        rows, _ = google.parse_response(captured_response().text)
        with patch.object(search_mod, "_run_engine", AsyncMock(return_value=(rows, None))) as run:
            result = await search("Python asyncio documentation")
        run.assert_awaited_once_with("google", "Python asyncio documentation", 1)
        assert "requested: google" in result["content"] and "[google]" in result["content"]
        assert "[ddg]" not in result["content"]

    @pytest.mark.parametrize("provider", ["ddg", "brave"])
    async def test_explicit_other_provider_does_not_fall_back(self, search_state, provider):
        with patch.object(search_mod, "_run_engine", AsyncMock(return_value=([], "HTTP 503"))) as run:
            result = await search("query", provider=provider)
        run.assert_awaited_once_with(provider, "query", 1)
        assert "Search FAILED" in result["error"] and f"{provider}: HTTP 503" in result["error"]

    async def test_google_failure_uses_ddg_and_discloses_fallback(self, search_state):
        rows = ddg_extract((FIXTURES / "ddg_html.html").read_text())
        with patch.object(search_mod, "_run_engine", AsyncMock(side_effect=[([], "HTTP 403"), (rows, None)])) as run:
            result = await search("query")
        assert [c.args[0] for c in run.await_args_list] == ["google", "ddg"]
        assert "automatic fallback to ddg" in result["content"]
        assert "[ddg]" in result["content"] and "[google]" not in result["content"]
        assert "google: HTTP 403" in result["content"]
        assert not _cache  # Retrying can recover Google; degraded results are not cached.

    async def test_brave_fallback_when_google_and_ddg_fail(self, search_state):
        rows, _ = google.parse_response(captured_response().text)
        with patch.object(
            search_mod, "_run_engine", AsyncMock(side_effect=[([], "HTTP 429"), ([], "HTTP 503"), (rows, None)])
        ) as run:
            result = await search("query")
        assert [c.args[0] for c in run.await_args_list] == ["google", "ddg", "brave"]
        assert "automatic fallback to brave" in result["content"] and "[brave]" in result["content"]

    async def test_deep_page_fallback_skips_ddg(self, search_state):
        rows, _ = google.parse_response(captured_response("page2").text)
        with patch.object(search_mod, "_run_engine", AsyncMock(side_effect=[([], "unavailable"), (rows, None)])) as run:
            result = await search("query", page=11)
        assert [c.args[0] for c in run.await_args_list] == ["google", "brave"]
        assert "skipped page 11" in result["content"] and "[brave]" in result["content"]

    async def test_honest_google_zero_does_not_trigger_fallback(self, search_state):
        with patch.object(search_mod, "_run_engine", AsyncMock(return_value=([], None))) as run:
            result = await search("site:example.invalid wafer-search")
        assert run.await_count == 1
        assert "No results found." in result["content"] and "fallback" not in result["content"]

    async def test_all_merges_deduplicates_and_preserves_sources(self, search_state):
        first, _ = google.parse_response(captured_response().text)
        second, _ = google.parse_response(captured_response("page2").text)

        async def engine(name, query, page):
            return ({"google": first, "ddg": first + second[:1], "brave": second}[name], None)

        with patch.object(search_mod, "_run_engine", engine):
            result = await search("query", provider="all")
        text = result["content"]
        assert "20 total" in text
        assert text.count("[google]") == 10 and text.count("[ddg]") == 1 and text.count("[brave]") == 9

    async def test_all_failures_are_error_not_empty(self, search_state):
        with patch.object(search_mod, "_run_engine", AsyncMock(side_effect=RuntimeError("broken"))):
            result = await search("query", provider="all")
        assert "Search FAILED" in result["error"]
        assert "No results found." not in result["error"]
        assert all(f"{name}: RuntimeError: broken" in result["error"] for name in search_mod.PROVIDERS)

    async def test_cache_separates_providers_pages_and_retains_source(self, search_state):
        rows, _ = google.parse_response(captured_response().text)
        with patch.object(search_mod, "_run_engine", AsyncMock(return_value=(rows, None))) as run:
            first = await search("query")
            assert await search("query") == first
            await search("query", provider="brave")
            await search("query", page=2)
        assert run.await_count == 3
        assert "[google]" in first["content"]

    @pytest.mark.parametrize(
        ("query", "page", "provider"),
        [
            ("", 1, "google"),
            ("x" * 513, 1, "google"),
            ("x", True, "google"),
            ("x", 101, "google"),
            ("x", 1, []),
            ("x", 1, "unknown"),
            ("x", 11, "ddg"),
        ],
    )
    async def test_invalid_input_never_calls_providers(self, search_state, query, page, provider):
        with patch.object(search_mod, "_run_engine", AsyncMock()) as run:
            assert "error" in await search(query, page=page, provider=provider)
        run.assert_not_called()

    async def test_provider_timeout_does_not_wait_for_cancellation_resistant_task(self, search_state, monkeypatch):
        stopped = asyncio.Event()
        started = asyncio.Event()

        async def resistant(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await stopped.wait()
            return [], False, None

        monkeypatch.setattr(search_mod, "_GOOGLE_TIMEOUT", 0.01)
        with patch.object(search_mod, "search_google", resistant):
            try:
                result = await asyncio.wait_for(search_mod._run_engine("google", "query", 1), 1)
                assert "timed out" in result[1]
                assert started.is_set()
            finally:
                stopped.set()
                await asyncio.sleep(0)

    async def test_outer_cancellation_reaches_all_providers(self, search_state):
        started = set()
        cancelled = set()

        async def engine(name, *args):
            started.add(name)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(name)
                raise

        with patch.object(search_mod, "_run_engine", engine):
            task = asyncio.create_task(search("query", provider="all"))
            for _ in range(10):
                await asyncio.sleep(0)
                if len(started) == 3:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        assert cancelled == set(search_mod.PROVIDERS)

    async def test_google_session_uses_plain_transport_without_redirects(self, search_state):
        with patch("wafer.AsyncSession", return_value=object()) as factory:
            first = await search_mod._get_session("google")
            assert await search_mod._get_session("google") is first
        assert factory.call_args.kwargs["follow_redirects"] is False
        assert "profile" not in factory.call_args.kwargs
        assert "cache_dir" not in factory.call_args.kwargs

    def test_output_is_bounded_at_complete_result_boundary(self):
        rows, _ = google.parse_response(captured_response().text)
        output = _format_results(rows * 1000)
        assert len(output) <= _MAX_RESULTS_OUTPUT_CHARS
        assert "trailing search results" in output


async def test_google_document_retries_share_global_pacing(google_state, monkeypatch):
    clock = [1000.0]
    sent = []

    async def sleep(seconds):
        clock[0] += seconds

    async def post(*args, **kwargs):
        sent.append(clock[0])
        return captured_response(status=403) if len(sent) == 1 else captured_response()

    monkeypatch.setattr(google.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(google.asyncio, "sleep", sleep)
    monkeypatch.setattr(google, "_MIN_INTERVAL", 6.0)
    session = SimpleNamespace(post=post)
    assert (await google.search_google(session, "query"))[2] is None
    assert (await google.search_google(session, "different query"))[2] is None
    assert sent == [1000.0, 1006.0, 1012.0]


async def test_google_document_recovers_after_cooldown(google_state, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(google.time, "monotonic", lambda: clock[0])
    only = google.document_ids()[0]
    monkeypatch.setenv("FETCHALLER_GOOGLE_DOCUMENT_IDS", json.dumps([only]))
    session = SimpleNamespace(post=AsyncMock(side_effect=[captured_response(status=404), captured_response()]))
    assert "HTTP 404" in (await google.search_google(session, "query"))[2]
    assert "cooldown" in (await google.search_google(session, "query"))[2]
    clock[0] += 901
    results, throttled, error = await google.search_google(session, "query")
    assert len(results) == 10 and not throttled and error is None
    assert session.post.await_count == 2


async def test_google_global_backoff_honors_http_date(google_state, monkeypatch):
    from email.utils import formatdate

    monkeypatch.setattr(google.time, "time", lambda: 1_000_000.0)
    session = SimpleNamespace(
        post=AsyncMock(
            return_value=captured_response(status=429, headers={"retry-after": formatdate(1_000_600, usegmt=True)})
        )
    )
    assert "600s" in (await google.search_google(session, "query"))[2]
    for value in ("not-a-date", "NaN", "inf"):
        assert google._retry_after(value) == 0


async def test_google_cursor_expiry_uses_stateless_offset(google_state, monkeypatch):
    google._cursors[("query", "en", 10, 2)] = ("expired-server-cursor", google.time.monotonic() - 301)
    session = SimpleNamespace(post=AsyncMock(return_value=captured_response("page2")))
    await google.search_google(session, "query", 2)
    assert json.loads(session.post.call_args.kwargs["form"]["request"])[2] == [10, "CAo="]


async def test_all_keeps_successful_results_and_visible_provider_failure(search_state):
    rows, _ = google.parse_response(captured_response().text)

    async def engine(name, *args):
        return (rows, None) if name == "google" else ([], "HTTP 429")

    with patch.object(search_mod, "_run_engine", engine):
        result = await search("query", provider="all")
    assert "[google]" in result["content"] and "ddg: HTTP 429" in result["content"]
    assert "brave: HTTP 429" in result["content"] and not _cache


async def test_provider_child_cancellation_is_failure_not_outer_cancellation(search_state):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    with patch.object(search_mod, "fetch_brave", cancelled):
        result = await search("query", provider="brave")
    assert "Provider task was cancelled" in result["error"]


async def test_search_logs_redact_query_and_provider_exception(search_state, capsys):
    secret_query = "private query terms"
    rows, _ = google.parse_response(captured_response().text)
    with patch.object(search_mod, "_run_engine", AsyncMock(return_value=(rows, None))):
        await search(secret_query)
    assert secret_query not in capsys.readouterr().err
    diagnostic = search_mod._safe_error(RuntimeError("token=abc123\n" + "x" * 1000))
    assert "abc123" not in diagnostic and "\n" not in diagnostic and len(diagnostic) < 250


def test_google_general_web_and_unicode_captures():
    banff, _ = google.parse_response(captured_response("banff").text)
    french, _ = google.parse_response(captured_response("french").text)
    assert banff[0].url == "https://parks.canada.ca/pn-np/ab/banff"
    assert banff[0].title == "Banff National Park - Parks Canada"
    assert french[0].title == "Musée du Louvre | Ministère de la Culture"
    assert french[0].url.startswith("https://www.culture.gouv.fr/")
    assert all(not row.url.startswith("https://docs.google.com/document/") for row in banff + french)


def test_real_brave_pages_have_disjoint_organic_results():
    first = brave_extract((FIXTURES / "brave_page1.html").read_text())
    second = brave_extract((FIXTURES / "brave_page2.html").read_text())
    assert len(first) == len(second) == 20
    assert not {r.url for r in first} & {r.url for r in second}
    assert any("docs.python.org" in r.url for r in first)


@pytest.mark.parametrize("provider", ["brave", "ddg"])
async def test_paced_http_transport_does_not_retry_or_bunch_requests(provider, search_state, monkeypatch):
    """Replay a 503 then a real captured page through the actual wafer client."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    module = brave_mod if provider == "brave" else ddg_mod
    body = (FIXTURES / ("brave_page1.html" if provider == "brave" else "ddg_html.html")).read_bytes()
    arrivals = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            arrivals.append(time.monotonic())
            self.send_response(503 if len(arrivals) == 1 else 200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(module, "_ENDPOINT", f"http://127.0.0.1:{server.server_port}/search")
    monkeypatch.setattr(module, f"{provider}_search_limiter", DomainRateLimiter(6.0, jitter=(0.3, 0.3)))
    try:
        first = await search("Python asyncio documentation", provider=provider)
        assert "HTTP 503" in first["error"]
        assert len(arrivals) == 1  # A hidden retry would already have returned 200.
        second = await search("Python asyncio documentation", provider=provider)
        assert f"[{provider}]" in second["content"]
        assert len(arrivals) == 2 and arrivals[1] - arrivals[0] >= 6.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.fixture
def provider_clock(monkeypatch):
    import time

    clock = [1000.0]
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        clock[0] += seconds
        await real_sleep(0)

    async def parse(function, *args, **_kwargs):
        return function(*args)

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(brave_mod, "run_isolated", parse)
    monkeypatch.setattr(ddg_mod, "run_isolated", parse)
    return clock, real_sleep


async def test_brave_modes_share_one_serial_paced_gate(search_state, provider_clock, monkeypatch):
    clock, yield_once = provider_clock
    starts = []
    active = False
    response = SimpleNamespace(status_code=200, headers={}, text=(FIXTURES / "brave_page1.html").read_text())

    async def get(*_args, **_kwargs):
        nonlocal active
        assert not active
        active = True
        starts.append(clock[0])
        await yield_once(0)
        clock[0] += 2.0
        active = False
        return response

    session = SimpleNamespace(get=get)
    monkeypatch.setattr(brave_mod, "brave_search_limiter", DomainRateLimiter(6.0, jitter=(0.3, 0.3)))
    with (
        patch.object(search_mod, "_get_session", AsyncMock(return_value=session)),
        patch.object(search_mod, "search_google", AsyncMock(return_value=([], False, "unavailable"))),
        patch.object(search_mod, "search_ddg", AsyncMock(return_value=([], "unavailable"))),
    ):
        results = await asyncio.gather(
            *(search("Python asyncio documentation", provider=name) for name in ("brave", "google", "all"))
        )
    assert all("[brave]" in result["content"] for result in results)
    assert len(starts) == 3 and all(b - a >= 6.3 - 1e-8 for a, b in zip(starts, starts[1:]))


@pytest.mark.parametrize(
    ("provider", "status", "delay"),
    [("brave", 429, 60), ("brave", 403, 300), ("ddg", 429, 60), ("ddg", 403, 300), ("ddg", 202, 300)],
)
async def test_block_response_stops_queued_queries_and_recovers(provider, status, delay, provider_clock):
    clock, yield_once = provider_clock
    calls = []
    module = brave_mod if provider == "brave" else ddg_mod
    html = (FIXTURES / ("brave_page1.html" if provider == "brave" else "ddg_html.html")).read_text()

    async def get(*_args, **_kwargs):
        calls.append(clock[0])
        await yield_once(0)
        return SimpleNamespace(status_code=status if len(calls) == 1 else 200, headers={}, text=html)

    session = SimpleNamespace(get=get)
    function = fetch_brave if provider == "brave" else ddg_search
    results = await asyncio.gather(function(session, "first query"), function(session, "second query"))
    assert len(calls) == 1
    assert f"HTTP {status}" in results[0][1] and "backoff" in results[1][1]
    clock[0] += delay + 1
    rows, error = await function(session, "recovery query")
    assert error is None and len(rows) >= 10 and len(calls) == 2
    assert module._backoff_until < clock[0]


@pytest.mark.parametrize("provider", ["brave", "ddg"])
async def test_provider_retry_after_extends_cooldown(provider, provider_clock):
    clock, _ = provider_clock
    function = fetch_brave if provider == "brave" else ddg_search
    response = SimpleNamespace(
        status_code=429, headers={"retry-after": "600"}, text=(FIXTURES / "brave_captcha_429.html").read_text()
    )
    session = SimpleNamespace(get=AsyncMock(return_value=response))
    assert "600s" in (await function(session, "query"))[1]
    clock[0] += 301
    assert "backoff" in (await function(session, "another query"))[1]
    assert session.get.await_count == 1


async def test_brave_sessions_reuse_only_the_same_guarded_solver(search_state):
    from wafer.browser import BrowserSolver

    # Real wafer sessions: constructing solvers does not launch a browser.
    with BrowserSolver(solve_timeout=120) as first, BrowserSolver(solve_timeout=120) as second:
        plain = await search_mod._get_session("brave")
        guarded = await search_mod._get_session("brave", browser_solver=first)
        assert guarded is not plain
        assert guarded._browser_solver is first
        assert await search_mod._get_session("brave", browser_solver=first) is guarded
        other = await search_mod._get_session("brave", browser_solver=second)
        assert other is not guarded and other._browser_solver is second
        plain_again = await search_mod._get_session("brave")
        assert plain_again is not other and plain_again._browser_solver is None
        # Sharing the server solver must not attach it to Google's JSON API or DDG.
        for provider in ("google", "ddg"):
            session = await search_mod._get_session(provider, browser_solver=first)
            assert session._browser_solver is None
        await search_mod.close_session()
        assert not search_mod._sessions and not search_mod._session_solvers
        assert not first._executor_closed and not second._executor_closed


@pytest.mark.parametrize("provider", ["brave", "all", "google"])
async def test_guarded_solver_reaches_brave_in_direct_all_and_fallback(search_state, provider):
    from wafer.browser import BrowserSolver

    rows = brave_extract((FIXTURES / "brave_page1.html").read_text())
    with BrowserSolver(solve_timeout=120) as solver:
        with (
            patch.object(search_mod, "fetch_brave", AsyncMock(return_value=(rows, None))) as brave,
            patch.object(search_mod, "search_google", AsyncMock(return_value=([], False, "unavailable"))),
            patch.object(search_mod, "search_ddg", AsyncMock(return_value=([], "unavailable"))),
        ):
            result = await search("Python asyncio documentation", provider=provider, browser_solver=solver)
        assert "[brave]" in result["content"] and "brave: 20" in result["content"]
        assert brave.await_args.args[0]._browser_solver is solver
        assert _cache == {} or provider == "brave"
        assert all(
            session._browser_solver is None
            for name, session in search_mod._sessions.items()
            if name != "brave"
        )


async def test_brave_recovery_has_its_own_budget_and_bounded_body(search_state, monkeypatch):
    captured = SimpleNamespace(
        status_code=200, challenge_type=None, headers={},
        text=(FIXTURES / "brave_page1.html").read_text(),
    )

    async def get(*_args, **kwargs):
        # A solve must outlive the old engine deadline while retaining its
        # transport deadline and body cap, without an extra application retry.
        assert kwargs["timeout"] == 150
        assert kwargs["max_response_size"] == brave_mod._MAX_SEARCH_HTML_CHARS
        await asyncio.sleep(0.03)
        return captured

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    monkeypatch.setattr(search_mod, "_ENGINE_TIMEOUT", 0.001)
    monkeypatch.setattr(search_mod, "_BRAVE_TIMEOUT", 5)
    with patch.object(search_mod, "_get_session", AsyncMock(return_value=session)):
        result = await search("Python asyncio documentation", provider="brave")
    assert "brave: 20" in result["content"]
    assert session.get.await_count == 1


@pytest.mark.parametrize("status,delay", [(200, 300), (429, 60), (403, 300)])
async def test_identified_brave_gate_is_an_error_and_holds_queued_modes(search_state, status, delay):
    captured = SimpleNamespace(
        status_code=status, challenge_type="brave", headers={},
        text=(FIXTURES / "brave_captcha_429.html").read_text(),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=captured))
    with (
        patch.object(search_mod, "_get_session", AsyncMock(return_value=session)),
        patch.object(search_mod, "search_google", AsyncMock(return_value=([], False, "unavailable"))),
        patch.object(search_mod, "search_ddg", AsyncMock(return_value=([], "unavailable"))),
        patch.object(brave_mod, "run_isolated", AsyncMock()) as parse,
    ):
        results = await asyncio.gather(
            *(search("Python asyncio documentation", provider=name) for name in ("brave", "all", "google"))
        )
    assert all("error" in result for result in results)
    assert "unresolved Brave challenge" in results[0]["error"]
    assert f"({delay:.0f}s)" in results[0]["error"]
    assert "backoff" in results[1]["error"] and "backoff" in results[2]["error"]
    assert session.get.await_count == 1
    parse.assert_not_called()
    assert not _cache


@pytest.mark.parametrize("status", [200, 429, 403])
async def test_cancelled_browser_exchange_keeps_gate_until_completion(search_state, status):
    started = asyncio.Event()
    finished = asyncio.Event()
    response = SimpleNamespace(
        status_code=status, challenge_type="brave" if status != 200 else None, headers={},
        text=(FIXTURES / ("brave_page1.html" if status == 200 else "brave_captcha_429.html")).read_text(),
    )

    async def get(*_args, **_kwargs):
        started.set()
        await finished.wait()
        return response

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    with patch.object(search_mod, "_get_session", AsyncMock(return_value=session)):
        first = asyncio.create_task(search("first query", provider="brave"))
        try:
            await asyncio.wait_for(started.wait(), 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 0.2)
            second = asyncio.create_task(search("second query", provider="brave"))
            await asyncio.sleep(0.02)
            assert session.get.await_count == 1  # Cancelled browser work still holds the gate.
            finished.set()
            result = await asyncio.wait_for(second, 5)
            if status == 200:
                assert "brave: 20" in result["content"] and session.get.await_count == 2
            else:
                assert "backoff" in result["error"] and session.get.await_count == 1
        finally:
            finished.set()
            await asyncio.sleep(0)
