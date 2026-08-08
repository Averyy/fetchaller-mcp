"""Integration tests for fetch_url() — the full pipeline.

These tests mock the HTTP layer (wafer.AsyncSession) and SSRF check, then
exercise fetch_url() end-to-end. They catch bugs where site detection, URL
transforms, feed autodiscovery, or content type dispatch interact incorrectly.

The critical class is TestForumThreadNotHijacked: it verifies that forum thread
URLs on known domains are NOT hijacked by Tier 2 feed autodiscovery.
"""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from fetchaller.security.ssrf import BLOCK_PRIVATE, HostVerdict

# ---------------------------------------------------------------------------
# Mock wafer session — returns pre-configured responses by URL
# ---------------------------------------------------------------------------


class MockResponse:
    """Mock wafer response object."""

    def __init__(self, content: bytes, content_type: str, status_code: int, url: str, headers: dict | None = None):
        self.content = content
        self.status_code = status_code
        self.url = url
        hdrs = headers or {}
        hdrs.setdefault("content-type", content_type)
        self.headers = hdrs
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


def _html_response(html: str, url: str, content_type: str = "text/html") -> MockResponse:
    """Build a MockResponse from an HTML string."""
    return MockResponse(
        content=html.encode(),
        content_type=content_type,
        status_code=200,
        url=url,
    )


def _feed_response(xml: str, url: str) -> MockResponse:
    """Build a MockResponse from XML feed string."""
    return MockResponse(
        content=xml.encode(),
        content_type="application/rss+xml",
        status_code=200,
        url=url,
    )


class MockWaferSession:
    """Drop-in mock for wafer.AsyncSession that returns canned responses."""

    def __init__(self, responses: dict[str, MockResponse] | None = None, default: MockResponse | None = None):
        self.responses = responses or {}
        self.default = default or _html_response(
            "<html><body><p>default</p></body></html>",
            "https://example.com",
        )
        self.calls: list[str] = []
        self.request_headers: list[dict[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def add_cookie(self, raw_set_cookie: str, url: str) -> None:
        pass

    async def get(self, url: str, **kwargs) -> MockResponse:
        self.calls.append(url)
        self.request_headers.append(dict(kwargs.get("headers") or {}))
        return self.responses.get(url, self.default)


class PassthroughRedditQueue:
    """Exercise the injected server-queue path without real test delays."""

    def __init__(self):
        self.enqueued = 0
        self.backoffs: list[tuple[int, float | None]] = []

    async def enqueue(self, callback, *_args, **_kwargs):
        self.enqueued += 1
        return await callback(*_args)

    def set_backoff(self, status_code: int, retry_after: float | None = None) -> None:
        self.backoffs.append((status_code, retry_after))


def _patch_wafer(mock_session):
    """Create a patch for wafer.AsyncSession that returns the given mock session."""
    return patch("fetchaller.tools.fetch.wafer.AsyncSession", return_value=mock_session)


# Minimal RSS feed for testing
_SAMPLE_FEED_XML = """<?xml version="1.0"?>
<rss version="2.0">
<channel>
  <title>Test Forum</title>
  <item>
    <title>Thread 1</title>
    <link>https://forum.example.com/threads/1</link>
  </item>
</channel>
</rss>"""


# XenForo thread HTML with <link rel="alternate"> (the trap)
def _xenforo_thread_html(feed_href: str = "/forums/general.1/index.rss") -> str:
    return f"""<html id="XF">
<head>
  <title>My Thread Title</title>
  <link rel="alternate" type="application/rss+xml" href="{feed_href}">
</head>
<body>
  <div class="p-body">
    <article class="message">
      <div class="message-body">This is the thread content.</div>
    </article>
  </div>
</body>
</html>"""


def _vbulletin_thread_html(feed_href: str = "/forums/external.php?type=RSS2") -> str:
    return f"""<html>
<head>
  <title>vB Thread</title>
  <meta name="generator" content="vBulletin 4.2.5">
  <link rel="alternate" type="application/rss+xml" href="{feed_href}">
</head>
<body>
  <div id="posts">
    <div class="postcontainer">Thread body content here.</div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Patch helper — check_host must be async and return a HostVerdict.
# blocked=False with no IPs = public host, no pin. fetch_url() uses this for both
# the early guard and the connection pin, so patching it covers the whole SSRF path.
# ---------------------------------------------------------------------------


def _verdict_from(fn):
    """Adapt a ``(is_private, ips)``-style mock into a ``check_host`` verdict."""

    async def _inner(host):
        blocked, ips = await fn(host)
        return HostVerdict(host, blocked, list(ips), BLOCK_PRIVATE if blocked else None)

    return _inner


_PATCH_SSRF = patch(
    "fetchaller.tools.fetch.check_host",
    new_callable=AsyncMock,
    return_value=HostVerdict("public.example", False, []),
)


# ---------------------------------------------------------------------------
# Forum thread NOT hijacked by feed autodiscovery
# ---------------------------------------------------------------------------


class TestEmptyExtractionIsNotSuccess:
    """An HTML page that extracts to nothing must be reported, not returned as "".

    A JavaScript-gated interstitial (vwvortex serves one on its forum index)
    survives the fetch as real HTML and then postprocesses down to an empty
    string. Returning that as a successful empty document left the caller unable
    to tell "this page has no content" from "we were served a shell", and it
    cached the empty result for the next caller too.
    """

    @_PATCH_SSRF
    async def test_javascript_interstitial_reports_instead_of_empty_success(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.vwvortex.com/forums/"
        # No title and no prose: exactly what survives postprocessing when the
        # response body is only a bootstrap script.
        interstitial = (
            "<html><head></head>"
            "<body><script>window.location='/x'</script></body></html>"
        )

        session = MockWaferSession(default=_html_response(interstitial, url))
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert "content" not in result
        assert "No readable content could be extracted" in result["error"]
        assert url in result["error"]

    @_PATCH_SSRF
    async def test_empty_extraction_is_not_cached(self, _mock_ssrf):
        from fetchaller.cache.response_cache import ResponseCache
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.vwvortex.com/forums/"
        interstitial = "<html><body><script>var a=1;</script></body></html>"
        cache = ResponseCache(default_ttl=60, max_entries=10, max_entry_size=10_000)

        session = MockWaferSession(default=_html_response(interstitial, url))
        with _patch_wafer(session):
            result = await fetch_url(url, cache=cache)

        assert "error" in result
        assert all(cache.get(key) is None for key in (url, f"{url}|raw"))

    @_PATCH_SSRF
    async def test_real_content_still_succeeds(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://example.com/"
        page = "<html><body><h1>Example Domain</h1><p>Real text.</p></body></html>"

        session = MockWaferSession(default=_html_response(page, url))
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert "error" not in result
        assert "Example Domain" in result["content"]


class TestForumThreadNotHijacked:
    """Critical: thread URLs on known domains must NOT be hijacked by Tier 2 feed autodiscovery.

    The bug: Tier 2 found <link rel="alternate"> in thread HTML and fetched the feed,
    replacing the thread content with a forum listing. These tests ensure threads
    are passed through to the HTML→markdown pipeline.
    """

    @_PATCH_SSRF
    async def test_xenforo_thread_not_hijacked(self, _mock_ssrf):
        """XenForo thread (/threads/...) on known domain with <link rel="alternate"> → HTML, not feed."""
        from fetchaller.tools.fetch import fetch_url

        thread_url = "https://www.vwvortex.com/threads/my-thread.12345/"
        thread_html = _xenforo_thread_html()

        session = MockWaferSession(default=_html_response(thread_html, thread_url))
        with _patch_wafer(session):
            result = await fetch_url(thread_url)

        assert result.get("content_type") == "markdown"
        assert "thread content" in result["content"].lower()
        # Only one fetch call — no feed autodiscovery
        assert len(session.calls) == 1

    @_PATCH_SSRF
    async def test_vbulletin_thread_not_hijacked(self, _mock_ssrf):
        """vBulletin thread (showthread.php) on known domain with <link rel="alternate"> → HTML, not feed."""
        from fetchaller.tools.fetch import fetch_url

        thread_url = "https://u11.bimmerpost.com/forums/showthread.php?t=12345"
        thread_html = _vbulletin_thread_html()

        session = MockWaferSession(default=_html_response(thread_html, thread_url))
        with _patch_wafer(session):
            result = await fetch_url(thread_url)

        assert result.get("content_type") == "markdown"
        assert "thread body content" in result["content"].lower()
        assert len(session.calls) == 1

    @_PATCH_SSRF
    async def test_phpbb_rfd_thread_not_hijacked(self, _mock_ssrf):
        """phpBB thread on RFD (not matching listing pattern) with feed link → HTML, not feed."""
        from fetchaller.tools.fetch import fetch_url

        thread_url = "https://forums.redflagdeals.com/some-deal-thread-2612345/"
        thread_html = """<html>
<head>
  <title>RFD Deal</title>
  <link rel="alternate" type="application/rss+xml" href="/feed/topic/2612345">
</head>
<body id="phpbb">
  <div class="post">RFD deal thread content here.</div>
</body>
</html>"""

        session = MockWaferSession(default=_html_response(thread_html, thread_url))
        with _patch_wafer(session):
            result = await fetch_url(thread_url)

        assert result.get("content_type") == "markdown"
        # Should have thread content, not feed listing
        assert "rfd deal thread content" in result["content"].lower()
        assert len(session.calls) == 1

    @_PATCH_SSRF
    async def test_unknown_domain_xenforo_thread_not_hijacked(self, _mock_ssrf):
        """Unknown XenForo domain: thread with <link rel="alternate"> → HTML, not feed.

        is_thread_url() detects /threads/ in the URL path and skips autodiscovery
        even when the domain isn't in _KNOWN_DOMAINS.
        """
        from fetchaller.tools.fetch import fetch_url

        thread_url = "https://unknown-forum.example.com/threads/my-thread.999/"
        feed_url = "https://unknown-forum.example.com/forums/general.1/index.rss"
        thread_html = _xenforo_thread_html(feed_href=feed_url)

        session = MockWaferSession(
            responses={
                thread_url: _html_response(thread_html, thread_url),
                feed_url: _feed_response(_SAMPLE_FEED_XML, feed_url),
            },
        )
        with _patch_wafer(session):
            result = await fetch_url(thread_url)

        assert "thread content" in result["content"].lower()
        assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# Forum listing → feed discovery
# ---------------------------------------------------------------------------


class TestForumListingFeedDiscovery:
    """Forum listings on known domains should transform to feed URLs (Tier 1)."""

    @_PATCH_SSRF
    async def test_known_xenforo_listing_to_feed(self, _mock_ssrf):
        """Known XenForo listing → Tier 1 transforms URL to .rss, returns parsed feed."""
        from fetchaller.tools.fetch import fetch_url

        listing_url = "https://www.vwvortex.com/forums/general.1/"
        feed_url = "https://www.vwvortex.com/forums/general.1/index.rss"

        session = MockWaferSession(
            responses={feed_url: _feed_response(_SAMPLE_FEED_XML, feed_url)},
        )
        with _patch_wafer(session):
            result = await fetch_url(listing_url)

        assert result.get("content_type") == "markdown"
        assert "Thread 1" in result["content"]
        # Should fetch the feed URL, not the listing URL
        assert session.calls == [feed_url]

    @_PATCH_SSRF
    async def test_known_vbulletin_listing_to_feed(self, _mock_ssrf):
        """Known vBulletin listing → Tier 1 transforms to external.php RSS feed."""
        from fetchaller.tools.fetch import fetch_url

        listing_url = "https://u11.bimmerpost.com/forums/forumdisplay.php?f=944"

        feed_xml = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>U11 Forum</title>
<item><title>New BMW X1</title><link>https://u11.bimmerpost.com/forums/showthread.php?t=1</link></item>
</channel></rss>"""

        # The transform produces this feed URL
        expected_feed = "https://u11.bimmerpost.com/forums/external.php?type=RSS2&forumids=944"
        session = MockWaferSession(
            responses={expected_feed: _feed_response(feed_xml, expected_feed)},
        )
        with _patch_wafer(session):
            result = await fetch_url(listing_url)

        assert result.get("content_type") == "markdown"
        assert "New BMW X1" in result["content"]
        assert session.calls == [expected_feed]

    @_PATCH_SSRF
    async def test_unknown_forum_with_autodiscoverable_feed(self, _mock_ssrf):
        """Unknown XenForo forum listing with <link rel="alternate"> → autodiscovery (Tier 2)."""
        from fetchaller.tools.fetch import fetch_url

        listing_url = "https://new-forum.example.com/forums/general.1/"
        feed_url = "https://new-forum.example.com/forums/general.1/index.rss"

        listing_html = f"""<html id="XF">
<head>
  <link rel="alternate" type="application/rss+xml" href="{feed_url}">
</head>
<body><div class="p-body">Forum listing content</div></body>
</html>"""

        session = MockWaferSession(
            responses={
                listing_url: _html_response(listing_html, listing_url),
                feed_url: _feed_response(_SAMPLE_FEED_XML, feed_url),
            },
        )
        with _patch_wafer(session):
            result = await fetch_url(listing_url)

        assert result.get("content_type") == "markdown"
        assert "Thread 1" in result["content"]
        # Two fetches: original page + autodiscovered feed
        assert len(session.calls) == 2

    @_PATCH_SSRF
    async def test_unknown_forum_no_feed_falls_through(self, _mock_ssrf):
        """Unknown forum with no <link rel="alternate"> → falls through to HTML markdown."""
        from fetchaller.tools.fetch import fetch_url

        url = "https://new-forum.example.com/forums/general/"
        html = """<html id="XF">
<head><title>Forum Page</title></head>
<body><div class="p-body"><p>Forum listing content here</p></div></body>
</html>"""

        session = MockWaferSession(default=_html_response(html, url))
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert result.get("content_type") == "markdown"
        assert "forum listing content" in result["content"].lower()
        assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# URL transforms
# ---------------------------------------------------------------------------


class TestRedditUrlTransform:
    """Normal Reddit URLs should use compact New Reddit JSON."""

    @_PATCH_SSRF
    async def test_www_routes_to_json_without_old_reddit(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/homelab/"
        canonical_fetch_url = (
            "https://www.reddit.com/r/homelab/hot.json?"
            "limit=250&raw_json=1"
        )
        transport_url = canonical_fetch_url.replace(
            "www.reddit.com",
            "api.reddit.com",
        )
        payload = {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "abc123",
                            "title": "Homelab post",
                            "subreddit": "homelab",
                            "author": "reader",
                            "score": 12,
                            "num_comments": 3,
                            "created_utc": 1,
                            "permalink": "/r/homelab/comments/abc123/title/",
                            "is_self": True,
                        },
                    }
                ],
            },
        }
        session = MockWaferSession(
            responses={
                transport_url: MockResponse(
                    json.dumps(payload).encode(),
                    "application/json",
                    200,
                    transport_url,
                )
            },
        )
        with patch("fetchaller.tools.reddit_fetch._get_session", AsyncMock(return_value=session)):
            result = await fetch_url(url, reddit_queue=PassthroughRedditQueue())

        assert result.get("content_type") == "markdown"
        assert "Homelab post" in result["content"]
        assert "https://www.reddit.com/r/homelab/comments/abc123/" in result["content"]
        assert "old.reddit.com" not in result["content"]
        assert session.calls == [transport_url]

    @_PATCH_SSRF
    async def test_old_input_is_canonicalized_without_requesting_old(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://old.reddit.com/r/homelab/"
        canonical_fetch_url = (
            "https://www.reddit.com/r/homelab/hot.json?"
            "limit=250&raw_json=1"
        )
        transport_url = canonical_fetch_url.replace(
            "www.reddit.com",
            "api.reddit.com",
        )
        payload = {"kind": "Listing", "data": {"children": []}}
        session = MockWaferSession(
            responses={
                transport_url: MockResponse(
                    json.dumps(payload).encode(),
                    "application/json",
                    200,
                    transport_url,
                )
            }
        )
        with patch("fetchaller.tools.reddit_fetch._get_session", AsyncMock(return_value=session)):
            result = await fetch_url(url, reddit_queue=PassthroughRedditQueue())

        assert result.get("content_type") == "markdown"
        assert session.calls == [transport_url]
        assert all("old.reddit.com" not in call for call in session.calls)

    @_PATCH_SSRF
    async def test_raw_old_input_fetches_canonical_new_reddit_html(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://old.reddit.com/r/homelab/"
        canonical = "https://www.reddit.com/r/homelab/"
        session = MockWaferSession(default=_html_response("<html><body>New Reddit</body></html>", canonical))
        queue = PassthroughRedditQueue()
        with _patch_wafer(session):
            result = await fetch_url(url, raw=True, reddit_queue=queue)

        assert result["content_type"] == "html"
        assert "New Reddit" in result["content"]
        assert session.calls == [canonical]
        assert queue.enqueued == 1

    @_PATCH_SSRF
    async def test_special_reddit_subdomains_keep_their_exact_representation(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        urls = [
            "https://oauth.reddit.com/api/v1/me",
            "https://mod.reddit.com/mail/all",
            "https://chat.reddit.com/",
            "https://api.reddit.com/r/Python",
        ]
        responses = {
            url: MockResponse(
                json.dumps({"source": url}).encode(),
                "application/json",
                200,
                url,
            )
            for url in urls
        }
        session = MockWaferSession(responses=responses)
        queue = PassthroughRedditQueue()

        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            results = [await fetch_url(url, reddit_queue=queue) for url in urls]

        assert session.calls == urls
        assert queue.enqueued == len(urls)
        assert [result["content_type"] for result in results] == ["json"] * len(urls)
        for url, result in zip(urls, results, strict=True):
            assert json.loads(result["content"]) == {"source": url}

    @_PATCH_SSRF
    async def test_old_html_fallback_note_stays_inside_output_budget(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        source = "https://old.reddit.com/settings/privacy/"
        canonical = "https://www.reddit.com/settings/privacy/"
        html = "<html><body>" + "<p>Privacy details remain visible.</p>" * 50 + "</body></html>"
        session = MockWaferSession(
            default=_html_response(html, canonical),
        )
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(source, max_tokens=30)

        assert result["content"].startswith(f"[Fetched via: {canonical}]")
        assert len(result["content"]) <= 30 * 4
        assert session.calls == [canonical]

    async def test_cached_old_html_fallback_note_stays_inside_output_budget(self):
        from types import SimpleNamespace

        from fetchaller.tools.fetch import fetch_url

        source = "https://old.reddit.com/settings/privacy/"
        canonical = "https://www.reddit.com/settings/privacy/"

        class CacheHit:
            def get(self, _key):
                return SimpleNamespace(
                    content="Cached privacy details. " * 100,
                    content_type="markdown",
                )

        result = await fetch_url(
            source,
            max_tokens=30,
            cache=CacheHit(),
        )

        assert result["cached"] is True
        assert result["content"].startswith(f"[Fetched via: {canonical}]")
        assert len(result["content"]) <= 30 * 4

    @_PATCH_SSRF
    async def test_explicit_json_remains_raw_json(self, _mock_ssrf):
        from fetchaller.tools.fetch import (
            _truncate_json_isolated,
            fetch_url,
        )

        url = "https://www.reddit.com/r/homelab/hot.json?limit=1"
        session = MockWaferSession(default=MockResponse(b'{"kind":"Listing"}', "application/json", 200, url))
        queue = PassthroughRedditQueue()
        with (
            patch(
                "fetchaller.tools.browse_reddit._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.search_reddit._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.fetch._truncate_json_isolated",
                wraps=_truncate_json_isolated,
            ) as bounded_json,
        ):
            result = await fetch_url(url, reddit_queue=queue)

        assert result["content_type"] == "json"
        assert result["content"] == '{"kind":"Listing"}'
        assert session.calls == [url]
        assert queue.enqueued == 1
        assert bounded_json.await_count == 1

    @_PATCH_SSRF
    @pytest.mark.parametrize("raw", [False, True])
    async def test_explicit_reddit_json_rejects_redirect_to_profile_html(
        self,
        _mock_ssrf,
        raw,
    ):
        from fetchaller.tools.fetch import fetch_url

        source = "https://www.reddit.com/user/spez/gilded.json?limit=25"
        profile = "https://www.reddit.com/user/spez/"
        session = MockWaferSession(
            responses={
                source: MockResponse(
                    b"",
                    "text/html",
                    302,
                    source,
                    headers={"location": profile},
                ),
            },
        )
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(
                source,
                raw=raw,
                reddit_queue=PassthroughRedditQueue(),
            )

        assert result == {
            "error": (
                "Reddit explicit JSON redirected to a different or non-JSON route."
            )
        }
        assert session.calls == [source]

    @_PATCH_SSRF
    async def test_explicit_reddit_json_rejects_html_without_redirect(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        source = "https://www.reddit.com/r/Python/hot.json?limit=1"
        session = MockWaferSession(
            default=_html_response("<html>soft JSON failure</html>", source)
        )
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(
                source,
                reddit_queue=PassthroughRedditQueue(),
            )

        assert result == {
            "error": (
                "Reddit explicit JSON returned a non-JSON representation."
            )
        }

    @_PATCH_SSRF
    async def test_explicit_reddit_json_rejects_different_json_route_redirect(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        source = "https://www.reddit.com/user/spez/gilded.json?limit=25"
        substitute = "https://www.reddit.com/r/Python/hot.json?limit=25"
        session = MockWaferSession(
            responses={
                source: MockResponse(
                    b"",
                    "application/json",
                    302,
                    source,
                    headers={"location": substitute},
                ),
            },
        )
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(
                source,
                reddit_queue=PassthroughRedditQueue(),
            )

        assert result == {
            "error": (
                "Reddit explicit JSON redirected to a different or non-JSON route."
            )
        }
        assert session.calls == [source]

    @_PATCH_SSRF
    async def test_explicit_reddit_json_invalidates_stale_markdown_cache(
        self,
        _mock_ssrf,
    ):
        from types import SimpleNamespace

        from fetchaller.tools.fetch import fetch_url

        source = "https://www.reddit.com/r/Python/hot.json?limit=1"
        session = MockWaferSession(
            default=MockResponse(
                b'{"kind":"Listing","data":{"children":[]}}',
                "application/json",
                200,
                source,
            )
        )

        class StaleCache:
            invalidated = False

            def get(self, _key):
                return SimpleNamespace(
                    content="# Wrong profile Markdown",
                    content_type="markdown",
                )

            def invalidate(self, _key):
                self.invalidated = True

            def set(self, *_args, **_kwargs):
                pass

        cache = StaleCache()
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(
                source,
                cache=cache,
                reddit_queue=PassthroughRedditQueue(),
            )

        assert cache.invalidated is True
        assert result["content_type"] == "json"
        assert json.loads(result["content"])["kind"] == "Listing"
        assert session.calls == [source]

    def test_flat_json_prefix_is_linear_and_parseable(self):
        from fetchaller.tools.fetch import truncate_json

        payload = json.dumps(list(range(20_000)), separators=(",", ":"))
        budget = int(len(payload) * 0.9)
        started = time.monotonic()
        bounded = truncate_json(
            payload,
            max_tokens=budget,
            chars_per_token=1,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert bounded is not None
        decoded = json.loads(bounded)
        assert decoded[:-1] == list(range(len(decoded) - 1))

        # The marker has to say how much of the array is missing: a valid,
        # plausible-looking prefix is otherwise indistinguishable from the whole
        # thing. "" is the path of the root container itself.
        marker = decoded[-1]["_fetchaller_truncated"]
        assert marker["path"] == ""
        assert marker["total"] == 20_000
        assert marker["included"] == len(decoded) - 1
        assert marker["included"] < marker["total"]
        assert marker["bytes_total"] == len(payload)

    @_PATCH_SSRF
    async def test_explicit_json_over_budget_is_parseable_uncached(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/homelab/hot.json?limit=1"
        payload = {
            "nested": {"items": [{"title": "x" * 200}, {"title": "y" * 200}]},
            "after": "t3_abcdef",
        }
        session = MockWaferSession(default=MockResponse(json.dumps(payload).encode(), "application/json", 200, url))
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(url, max_tokens=80, reddit_queue=PassthroughRedditQueue())

        assert result["content_type"] == "json"
        assert len(result["content"]) <= 80 * 4
        decoded = json.loads(result["content"])
        assert decoded["nested"]["items"][0]["title"] == "x" * 200
        assert len(decoded["nested"]["items"]) == 1

        marker = decoded["_fetchaller_truncated"]
        assert marker["included"] < marker["total"]
        assert marker["bytes_total"] == len(json.dumps(payload))

    async def test_explicit_json_over_budget_is_parseable_from_cache(self):
        from types import SimpleNamespace

        from fetchaller.tools.fetch import fetch_url

        source = "https://old.reddit.com/r/homelab/hot.json?limit=1"
        payload = {
            "nested": {"items": [{"title": "x" * 200}, {"title": "y" * 200}]},
            "after": "t3_abcdef",
        }

        class CacheHit:
            def get(self, _key):
                return SimpleNamespace(
                    content=json.dumps(payload, indent=2),
                    content_type="application/json",
                )

            def invalidate(self, _key):
                raise AssertionError("valid JSON cache entry must be served")

        result = await fetch_url(source, max_tokens=80, cache=CacheHit())

        assert result["cached"] is True
        assert result["content_type"] == "application/json"
        assert len(result["content"]) <= 80 * 4
        decoded = json.loads(result["content"])
        assert decoded["nested"]["items"][0]["title"] == "x" * 200
        assert len(decoded["nested"]["items"]) == 1

        marker = decoded["_fetchaller_truncated"]
        assert marker["included"] < marker["total"]

    @_PATCH_SSRF
    async def test_valid_json_too_small_for_marker_returns_budget_error(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/homelab/hot.json?limit=1"
        session = MockWaferSession(
            default=MockResponse(
                json.dumps({"title": "x" * 200}).encode(),
                "application/json",
                200,
                url,
            )
        )
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(
                url,
                max_tokens=5,
                reddit_queue=PassthroughRedditQueue(),
            )

        assert result == {
            "error": ("JSON exceeds maxTokens; increase maxTokens to return a useful whole-value prefix.")
        }

    async def test_cached_valid_json_budget_error_does_not_invalidate_or_refetch(
        self,
    ):
        from types import SimpleNamespace

        from fetchaller.tools.fetch import fetch_url

        source = "https://old.reddit.com/r/homelab/hot.json?limit=1"

        class CacheHit:
            invalidated = False

            def get(self, _key):
                return SimpleNamespace(
                    content=json.dumps({"title": "x" * 200}),
                    content_type="application/json",
                )

            def invalidate(self, _key):
                self.invalidated = True

        cache = CacheHit()
        result = await fetch_url(source, max_tokens=5, cache=cache)

        assert result["error"].startswith("JSON exceeds maxTokens")
        assert result["cached"] is True
        assert cache.invalidated is False

    @_PATCH_SSRF
    async def test_json_marker_collision_is_budget_error_uncached(self, _mock_ssrf):
        from fetchaller.tools.fetch import _JSON_BUDGET_ERROR, fetch_url

        url = "https://www.reddit.com/r/homelab/hot.json?limit=1"
        payload = {
            "_fetchaller_truncated": False,
            "payload": "x" * 1000,
        }
        session = MockWaferSession(
            default=MockResponse(
                json.dumps(payload).encode(),
                "application/json",
                200,
                url,
            )
        )
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(
                url,
                max_tokens=20,
                reddit_queue=PassthroughRedditQueue(),
            )

        assert result == {"error": _JSON_BUDGET_ERROR}
        assert session.calls == [url]

    async def test_cached_json_marker_collision_is_not_invalidated_or_refetched(self):
        from types import SimpleNamespace

        from fetchaller.tools.fetch import _JSON_BUDGET_ERROR, fetch_url

        source = "https://old.reddit.com/r/homelab/hot.json?limit=1"

        class CacheHit:
            invalidated = False

            def get(self, _key):
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "_fetchaller_truncated": False,
                            "payload": "x" * 1000,
                        }
                    ),
                    content_type="application/json",
                )

            def invalidate(self, _key):
                self.invalidated = True

        cache = CacheHit()
        result = await fetch_url(source, max_tokens=20, cache=cache)

        assert result["error"] == _JSON_BUDGET_ERROR
        assert result["cached"] is True
        assert result["url"] == "https://www.reddit.com/r/homelab/hot.json?limit=1"
        assert cache.invalidated is False

    @pytest.mark.parametrize("first_request", ["search", "browse", "explicit_json"])
    @_PATCH_SSRF
    async def test_reddit_shared_session_never_carries_a_prior_callers_referer(
        self,
        _mock_ssrf,
        first_request,
    ):
        """A durable identity may share cookies/TLS, never a caller's URL."""

        from fetchaller.tools.browse_reddit import browse_reddit
        from fetchaller.tools.fetch import fetch_url
        from fetchaller.tools.search_reddit import search_reddit

        secret = "caller-a-private-query"
        raw_url = "https://www.reddit.com/r/python/about/"
        explicit_url = f"https://www.reddit.com/r/python/hot.json?query={secret}"
        search_url = (
            f"https://www.reddit.com/r/python/search.json?q={secret}&sort=new&t=all&limit=1&raw_json=1&restrict_sr=1"
        )
        browse_url = "https://www.reddit.com/r/python/new.json?limit=1&raw_json=1"
        search_transport_url = search_url.replace(
            "www.reddit.com",
            "api.reddit.com",
        )
        browse_transport_url = browse_url.replace(
            "www.reddit.com",
            "api.reddit.com",
        )
        listing = json.dumps({"_reddit_content_state": "no results"}).encode()
        session = MockWaferSession(
            responses={
                explicit_url: MockResponse(
                    json.dumps({"items": []}).encode(),
                    "application/json",
                    200,
                    explicit_url,
                ),
                search_transport_url: MockResponse(
                    listing,
                    "application/json",
                    200,
                    search_transport_url,
                ),
                browse_transport_url: MockResponse(
                    listing,
                    "application/json",
                    200,
                    browse_transport_url,
                ),
                raw_url: _html_response("<html><body>safe raw response</body></html>", raw_url),
            }
        )
        queue = PassthroughRedditQueue()
        with (
            patch(
                "fetchaller.tools.browse_reddit._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.search_reddit._get_session",
                AsyncMock(return_value=session),
            ),
        ):
            if first_request == "search":
                first = await search_reddit(
                    secret,
                    subreddit="python",
                    sort="new",
                    limit=1,
                    queue=queue,
                )
            elif first_request == "browse":
                first = await browse_reddit("python", sort="new", limit=1, queue=queue)
            else:
                first = await fetch_url(explicit_url, reddit_queue=queue)
            second = await fetch_url(raw_url, raw=True, reddit_queue=queue)

        assert "error" not in first
        assert "error" not in second
        assert secret not in second["content"]
        assert len(session.request_headers) == 2
        for headers in session.request_headers:
            assert headers["Referer"] == "https://www.reddit.com/"
            assert secret not in json.dumps(headers)

    @_PATCH_SSRF
    async def test_explicit_json_429_applies_dynamic_backoff_to_shared_queue(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/homelab/hot.json?limit=1"
        session = MockWaferSession(
            default=MockResponse(
                b'{"message":"Too Many Requests"}',
                "application/json",
                429,
                url,
                headers={"retry-after": "91"},
            )
        )
        queue = PassthroughRedditQueue()
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(url, reddit_queue=queue)

        assert result["error"] == "Rate limited (HTTP 429). Retry after 91 seconds."
        assert queue.enqueued == 1
        assert queue.backoffs == [(429, 91.0)]

    @_PATCH_SSRF
    async def test_explicit_json_unstructured_403_applies_retry_after(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/homelab/hot.json?limit=1"
        session = MockWaferSession(
            default=MockResponse(
                b"{}",
                "application/json",
                403,
                url,
                headers={"retry-after": "37"},
            )
        )
        queue = PassthroughRedditQueue()
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(url, reddit_queue=queue)

        assert result["error"] == "HTTP 403"
        assert queue.enqueued == 1
        assert queue.backoffs == [(403, 37.0)]

    @_PATCH_SSRF
    async def test_explicit_json_content_state_403_does_not_poison_queue(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/private/about.json"
        session = MockWaferSession(
            default=MockResponse(
                b'{"reason":"private"}',
                "application/json",
                403,
                url,
                headers={"retry-after": "37"},
            )
        )
        queue = PassthroughRedditQueue()
        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(url, reddit_queue=queue)

        assert result["error"] == "HTTP 403"
        assert queue.enqueued == 1
        assert queue.backoffs == []

    async def test_short_link_redirect_is_reclassified_to_compact_json(self):
        from fetchaller.tools.fetch import fetch_url

        short = "https://redd.it/abc123"
        permalink = "https://www.reddit.com/r/Python/comments/abc123/title/"
        json_url = "https://www.reddit.com/r/Python/comments/abc123.json?sort=confidence&limit=250&depth=10&raw_json=1"
        json_transport_url = json_url.replace(
            "www.reddit.com",
            "api.reddit.com",
        )
        redirect = MockResponse(
            b"",
            "text/html",
            302,
            short,
            headers={"location": permalink},
        )
        redirect_session = MockWaferSession(responses={short: redirect})
        payload = [
            {
                "data": {
                    "children": [
                        {
                            "kind": "t3",
                            "data": {
                                "id": "abc123",
                                "title": "Redirected Reddit thread",
                                "subreddit": "Python",
                                "author": "alice",
                                "score": 1,
                                "num_comments": 0,
                                "created_utc": 1,
                                "permalink": "/r/Python/comments/abc123/title/",
                                "is_self": True,
                                "selftext": "Full body",
                            },
                        }
                    ]
                }
            },
            {"data": {"children": []}},
        ]
        reddit_session = MockWaferSession(
            responses={
                json_transport_url: MockResponse(
                    json.dumps(payload).encode(),
                    "application/json",
                    200,
                    json_transport_url,
                )
            }
        )

        async def _rac(_host):
            return False, ["203.0.113.10"]

        with (
            patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)),
            _patch_wafer(redirect_session),
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=reddit_session),
            ),
        ):
            result = await fetch_url(short, reddit_queue=PassthroughRedditQueue())

        assert result["content_type"] == "markdown"
        assert "Redirected Reddit thread" in result["content"]
        assert "Full body" in result["content"]
        assert redirect_session.calls == [short]
        assert reddit_session.calls == [json_transport_url]

    async def test_redirect_leaving_reddit_uses_generic_html_cleanup(self):
        from fetchaller.tools.fetch import fetch_url

        start = "https://www.reddit.com/settings/privacy/"
        external = "https://docs.example.com/privacy"
        session = MockWaferSession(
            responses={
                start: MockResponse(
                    b"",
                    "text/html",
                    302,
                    start,
                    headers={"location": external},
                ),
                external: _html_response(
                    "<html><body>External documentation</body></html>",
                    external,
                ),
            }
        )
        markdown = AsyncMock(return_value=("GENERIC OUTPUT", {}))

        async def _rac(_host):
            return False, ["203.0.113.11"]

        with (
            patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)),
            patch(
                "fetchaller.tools.browse_reddit._get_session",
                AsyncMock(return_value=session),
            ),
            _patch_wafer(session),
            patch("fetchaller.tools.fetch.html_to_markdown", markdown),
        ):
            result = await fetch_url(start)

        assert result["content"].endswith("GENERIC OUTPUT")
        assert markdown.await_args.kwargs["is_reddit"] is False
        assert session.calls == [start, external]


class TestJsonTruncationIsSelfDescribing:
    """A truncated listing must not read as a complete short one.

    The encoder emits valid JSON on purpose, which is exactly what makes the
    loss invisible: a board cut to 7 of 747 jobs parses cleanly and looks like a
    company with 7 openings. The marker is the only thing that distinguishes
    them, so it carries counts rather than a bare flag.
    """

    @staticmethod
    def _marker(text, max_tokens, chars_per_token=4):
        from fetchaller.tools.fetch import truncate_json

        bounded = truncate_json(text, max_tokens=max_tokens, chars_per_token=chars_per_token)
        assert bounded is not None
        assert len(bounded) <= max_tokens * chars_per_token
        decoded = json.loads(bounded)  # never emit unparseable JSON
        # A dict carries the marker as a member; an array as a trailing element.
        holder = decoded if isinstance(decoded, dict) else decoded[-1]
        return decoded, holder["_fetchaller_truncated"]

    def test_marker_counts_the_records_that_were_dropped(self):
        payload = json.dumps(
            {"jobs": [{"id": i, "title": f"Job {i}", "location": "SF"} for i in range(747)]}
        )
        decoded, marker = self._marker(payload, max_tokens=2000)

        assert marker["path"] == "jobs"
        assert marker["total"] == 747
        assert marker["included"] == len(decoded["jobs"])
        assert marker["included"] < 747
        assert marker["bytes_total"] == len(payload)

    def test_marker_blames_the_array_not_the_half_written_record(self):
        """The innermost cut container is the wrong one to report.

        Stopping mid-record leaves a partly-serialized job object as the
        innermost truncation. Reporting that ("1 of 4 fields") hides the only
        number that matters — that the array lost hundreds of records.
        """
        payload = json.dumps({"jobs": [{"a": 1, "b": 2, "c": 3, "d": 4} for _ in range(500)]})
        _decoded, marker = self._marker(payload, max_tokens=200)

        assert marker["path"] == "jobs"
        assert marker["total"] == 500

    def test_marker_stays_truthy_for_callers_that_only_check_presence(self):
        payload = json.dumps({"jobs": [{"id": i} for i in range(400)]})
        _decoded, marker = self._marker(payload, max_tokens=100)

        assert marker  # `if data.get("_fetchaller_truncated"):` still works

    def test_top_level_array_reports_the_root_container(self):
        payload = json.dumps([{"id": i, "name": f"item {i}"} for i in range(500)])
        decoded, marker = self._marker(payload, max_tokens=200)

        assert marker["path"] == ""  # the document root itself
        assert marker["total"] == 500
        assert marker["included"] == len(decoded) - 1  # the marker is the last element

    def test_nested_container_path_locates_the_cut(self):
        payload = json.dumps({"data": {"results": [{"x": i} for i in range(400)]}, "meta": {"p": 1}})
        _decoded, marker = self._marker(payload, max_tokens=200)

        assert marker["path"] == "data.results"
        assert marker["total"] == 400

    def test_awkward_keys_are_quoted_so_the_path_stays_unambiguous(self):
        """A key named "data.results" must not render like data -> results."""
        payload = json.dumps({"data.results": [{"x": i} for i in range(400)]})
        _decoded, marker = self._marker(payload, max_tokens=200)

        assert marker["path"] == '["data.results"]'
        assert marker["total"] == 400

    def test_complete_json_is_never_marked(self):
        from fetchaller.tools.fetch import truncate_json

        payload = json.dumps({"jobs": [{"id": 1}, {"id": 2}]})
        bounded = truncate_json(payload, max_tokens=25_000)

        assert bounded == payload
        assert "_fetchaller_truncated" not in json.loads(bounded)

    def test_source_owning_the_reserved_key_is_never_overwritten(self):
        from fetchaller.tools.fetch import _JsonBudgetExceededError, truncate_json

        payload = json.dumps({"_fetchaller_truncated": "mine", "jobs": [{"id": i} for i in range(400)]})
        with pytest.raises(_JsonBudgetExceededError):
            truncate_json(payload, max_tokens=100)


class TestURLValidation:
    async def test_malformed_ports_and_embedded_credentials_return_clean_errors(self):
        from fetchaller.tools.fetch import fetch_url

        for url in (
            "https://example.com:99999/",
            "https://example.com:not-a-port/",
            "https://user:secret@example.com/",
        ):
            result = await fetch_url(url)
            assert "error" in result
            assert "unexpected" not in result["error"].lower()


# ---------------------------------------------------------------------------
# Content type dispatch
# ---------------------------------------------------------------------------


class TestContentTypeDispatch:
    """fetch_url() must return the correct content_type for each MIME type."""

    @_PATCH_SSRF
    async def test_json(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(
            default=MockResponse(
                content=b'{"key": "value"}',
                content_type="application/json",
                status_code=200,
                url="https://api.example.com/data",
            )
        )
        with _patch_wafer(session):
            result = await fetch_url("https://api.example.com/data")
        assert result["content_type"] == "json"
        assert '"key"' in result["content"]

    @_PATCH_SSRF
    async def test_plain_text(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(
            default=MockResponse(
                content=b"Hello world",
                content_type="text/plain",
                status_code=200,
                url="https://example.com/file.txt",
            )
        )
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/file.txt")
        assert result["content_type"] == "text"
        assert "Hello world" in result["content"]

    @_PATCH_SSRF
    async def test_malformed_dayforce_preflight_preserves_visible_html(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        url = "https://example.com/careers"
        malformed = {
            "runtimeConfig": {"BASE_URL": "https://jobs.dayforcehcm.com"},
            "query": {
                "clientNamespace": 42,
                "careerSiteXRefCode": "BOARD",
            },
        }
        html = (
            "<html><body><h1>Visible careers</h1>"
            "<p>Real page content</p>"
            '<script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(malformed)}</script></body></html>"
        )
        session = MockWaferSession(default=_html_response(html, url))
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert "error" not in result
        assert "Visible careers" in result["content"]
        assert "Real page content" in result["content"]

    @_PATCH_SSRF
    async def test_rss_xml(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(default=_feed_response(_SAMPLE_FEED_XML, "https://example.com/feed.rss"))
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/feed.rss")
        assert result["content_type"] == "markdown"
        assert "Thread 1" in result["content"]

    @_PATCH_SSRF
    async def test_reddit_rss_markdown_canonicalizes_authored_old_links(
        self,
        _mock_ssrf,
    ):
        from fetchaller.tools.fetch import fetch_url

        input_url = "https://old.reddit.com/r/Python/.rss"
        canonical_url = "https://www.reddit.com/r/Python/.rss"
        feed_xml = """<?xml version="1.0"?>
<rss version="2.0">
<channel>
  <title>r/Python</title>
  <item>
    <title>Legacy discussion link</title>
    <link>https://old.reddit.com/r/Python/comments/abc123/</link>
  </item>
</channel>
</rss>"""
        session = MockWaferSession(default=_feed_response(feed_xml, canonical_url))

        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_url(
                input_url,
                reddit_queue=PassthroughRedditQueue(),
            )

        assert result["content_type"] == "markdown"
        assert "https://www.reddit.com/r/Python/comments/abc123/" in result["content"]
        assert "old.reddit.com" not in result["content"]
        assert session.calls == [canonical_url]

    @_PATCH_SSRF
    async def test_html(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(
            default=_html_response(
                "<html><body><p>Hello</p></body></html>",
                "https://example.com/page",
            )
        )
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/page")
        assert result["content_type"] == "markdown"
        assert "Hello" in result["content"]

    @_PATCH_SSRF
    async def test_oversized_html_returns_safe_processing_error(self, _mock_ssrf):
        from fetchaller.content.html import _MAX_HTML_INPUT_CHARS
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(
            default=_html_response(
                "x" * (_MAX_HTML_INPUT_CHARS + 1),
                "https://example.com/oversized",
            )
        )
        with _patch_wafer(session):
            result = await fetch_url(
                "https://example.com/oversized",
                timeout=10,
            )
        assert result["error"].startswith("HTML input is too large")
        assert "content" not in result

    @_PATCH_SSRF
    async def test_fetch_timeout_reaps_html_worker_before_next_fetch(self, _mock_ssrf):
        import multiprocessing

        from fetchaller.tools.fetch import fetch_url

        slow_html = "<html><body>" + ("<p>content that requires conversion</p>" * 100_000) + "</body></html>"
        session = MockWaferSession(
            default=_html_response(
                slow_html,
                "https://example.com/slow",
            )
        )
        with _patch_wafer(session):
            timed_out = await fetch_url(
                "https://example.com/slow",
                timeout=0.02,
            )

        assert timed_out["error"].startswith("Request timed out after 0.02s")
        assert not any(child.name == "fetchaller-html-parser" for child in multiprocessing.active_children())

        session.default = _html_response(
            "<html><body><p>after fetch timeout</p></body></html>",
            "https://example.com/after-timeout",
        )
        with _patch_wafer(session):
            subsequent = await fetch_url(
                "https://example.com/after-timeout",
                timeout=10,
            )
        assert subsequent["content_type"] == "markdown"
        assert subsequent["content"] == "after fetch timeout"

    @_PATCH_SSRF
    async def test_svg_returned_as_raw_xml(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
        session = MockWaferSession(
            default=MockResponse(
                content=svg,
                content_type="image/svg+xml",
                status_code=200,
                url="https://example.com/logo.svg",
            )
        )
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/logo.svg")
        assert result["content_type"] == "svg"
        assert "<svg" in result["content"]
        assert "<rect" in result["content"]

    @_PATCH_SSRF
    async def test_png_returns_metadata_summary(self, _mock_ssrf):
        import struct

        from fetchaller.tools.fetch import fetch_url

        # Minimal PNG: 8-byte signature + IHDR chunk declaring 42x17
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 42, 17)
            + b"\x08\x06\x00\x00\x00"
            + b"\x00" * 4
        )
        session = MockWaferSession(
            default=MockResponse(
                content=png,
                content_type="image/png",
                status_code=200,
                url="https://example.com/pic.png",
                headers={
                    "content-type": "image/png",
                    "content-length": str(len(png)),
                    "last-modified": "Wed, 14 Aug 2024 19:19:46 GMT",
                    "etag": '"abc123"',
                },
            )
        )
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/pic.png")
        assert result["content_type"] == "text"
        content = result["content"]
        assert "[Image: image/png]" in content
        assert "Filename: pic.png" in content
        assert "Dimensions: 42x17" in content
        assert "Modified: Wed, 14 Aug 2024" in content
        assert "abc123" in content


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """fetch_url() must return error dicts for invalid/failed requests."""

    async def test_invalid_protocol(self):
        from fetchaller.tools.fetch import fetch_url

        result = await fetch_url("ftp://example.com/file")
        assert "error" in result
        assert "Invalid protocol" in result["error"]

    @_PATCH_SSRF
    async def test_ssrf_blocked(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        # Simulate SSRF detection
        _mock_ssrf.return_value = HostVerdict("internal.corp", True, [], BLOCK_PRIVATE)
        result = await fetch_url("https://internal.corp/secret")
        assert "error" in result
        assert "private" in result["error"].lower()
        # Reset for other tests
        _mock_ssrf.return_value = HostVerdict("public.example", False, [])

    @_PATCH_SSRF
    async def test_timeout(self, _mock_ssrf):
        import wafer

        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession()
        session.get = AsyncMock(side_effect=wafer.WaferTimeout("https://example.com/slow", 10.0))
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/slow")
        assert "error" in result
        assert "timed out" in result["error"].lower()

    @_PATCH_SSRF
    async def test_connection_error(self, _mock_ssrf):
        import wafer

        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession()
        session.get = AsyncMock(side_effect=wafer.ConnectionFailed("https://example.com/down", reason="ECONNREFUSED"))
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/down")
        assert "error" in result
        assert "econnrefused" in result["error"].lower()

    @_PATCH_SSRF
    async def test_http_429(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(
            default=MockResponse(
                content=b"rate limited",
                content_type="text/plain",
                status_code=429,
                url="https://example.com/api",
            )
        )
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/api")
        assert "error" in result
        assert "429" in result["error"]

    @_PATCH_SSRF
    async def test_http_404(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(
            default=MockResponse(
                content=b"not found",
                content_type="text/html",
                status_code=404,
                url="https://example.com/missing",
            )
        )
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/missing")
        assert "error" in result
        assert "404" in result["error"]

    @_PATCH_SSRF
    async def test_challenge_detected(self, _mock_ssrf):
        import wafer

        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession()
        session.get = AsyncMock(side_effect=wafer.ChallengeDetected("cloudflare", "https://protected.example.com", 403))
        with _patch_wafer(session):
            result = await fetch_url("https://protected.example.com")
        assert "error" in result
        assert "cloudflare" in result["error"].lower()

    @_PATCH_SSRF
    async def test_rate_limited_exception(self, _mock_ssrf):
        import wafer

        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession()
        session.get = AsyncMock(side_effect=wafer.RateLimited("https://api.example.com/limited", retry_after=60.0))
        with _patch_wafer(session):
            result = await fetch_url("https://api.example.com/limited")
        assert "error" in result
        assert "429" in result["error"] or "rate limited" in result["error"].lower()


# ---------------------------------------------------------------------------
# SSRF DNS pinning + per-hop redirect validation
# ---------------------------------------------------------------------------


class TestSSRFPinning:
    """The fetch host is pinned to its pre-validated IPs, and every redirect hop
    is validated before we connect to it (closing the DNS-rebinding window)."""

    async def test_fetch_pins_validated_ips(self):
        """The session is built with resolve={host: ips} and follow_redirects=False."""
        from fetchaller.tools.fetch import fetch_url

        captured: dict = {}
        session = MockWaferSession(default=_html_response("<html><body>ok</body></html>", "https://pin.example/"))

        def _factory(*args, **kwargs):
            captured.update(kwargs)
            return session

        async def _rac(_host):
            return (False, ["203.0.113.7"])

        with (
            patch("fetchaller.tools.fetch.wafer.AsyncSession", side_effect=_factory),
            patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)),
        ):
            result = await fetch_url("https://pin.example/page")

        assert "content" in result
        assert captured.get("resolve") == {"pin.example": ["203.0.113.7"]}
        assert captured.get("follow_redirects") is False

    async def test_redirect_to_private_host_blocked(self):
        """A redirect to an internal host is refused before we connect to it."""
        from fetchaller.tools.fetch import fetch_url

        async def _rac(host):
            # public start host is fine; the redirect target is internal
            return (True, []) if host == "internal.corp" else (False, [])

        start = "https://public.example/start"
        redirect = MockResponse(
            content=b"",
            content_type="text/html",
            status_code=302,
            url=start,
            headers={"location": "https://internal.corp/secret"},
        )
        session = MockWaferSession(responses={start: redirect})
        with patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)), _patch_wafer(session):
            result = await fetch_url(start)

        assert "error" in result
        assert "private" in result["error"].lower()
        # We refused before ever connecting to the internal host.
        assert session.calls == [start]

    async def test_same_host_redirect_followed(self):
        """A same-host redirect is followed (validated) and returns final content."""
        from fetchaller.tools.fetch import fetch_url

        async def _rac(_host):
            return (False, ["198.51.100.9"])

        start = "https://blog.example/a"
        final = "https://blog.example/b"
        redirect = MockResponse(
            content=b"",
            content_type="text/html",
            status_code=301,
            url=start,
            headers={"location": "/b"},
        )
        final_resp = _html_response("<html><body>arrived</body></html>", final)
        session = MockWaferSession(responses={start: redirect, final: final_resp})
        with patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)), _patch_wafer(session):
            result = await fetch_url(start)

        assert "arrived" in result["content"].lower()
        assert session.calls == [start, final]

    async def test_final_host_must_be_validated(self):
        """Defense in depth: a 200 whose final URL host we never validated is
        rejected (guards against a browser/native passthrough surfacing an
        unvetted host without going through the per-hop validation)."""
        from fetchaller.tools.fetch import fetch_url

        # Requested a public host, but the response claims to come from loopback.
        sneaky = MockResponse(
            content=b"<html>internal secret</html>",
            content_type="text/html",
            status_code=200,
            url="http://127.0.0.1/private",
        )
        session = MockWaferSession(responses={"https://public.example/": sneaky})

        async def _rac(_host):
            return (False, [])

        with patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)), _patch_wafer(session):
            result = await fetch_url("https://public.example/")

        assert "error" in result
        assert "unvalidated host" in result["error"].lower()

    async def test_final_host_empty_is_rejected(self):
        """Fail closed: a response with no determinable final host is rejected,
        not accepted (the check must not skip when the host is empty)."""
        from fetchaller.tools.fetch import fetch_url

        nohost = MockResponse(content=b"secret", content_type="text/html", status_code=200, url="")
        session = MockWaferSession(responses={"https://public.example/": nohost})

        async def _rac(_host):
            return (False, [])

        with patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)), _patch_wafer(session):
            result = await fetch_url("https://public.example/")

        assert "error" in result
        assert "unvalidated host" in result["error"].lower()

    async def test_trailing_dot_host_not_false_rejected(self):
        """Host canonicalization matches wafer: a trailing-dot request host is
        validated as example.com, so wafer's canonicalized example.com response
        is accepted, not falsely rejected."""
        from fetchaller.tools.fetch import fetch_url

        # Request Example.COM. — wafer canonicalizes the dispatched URL, so the
        # response comes back with url https://example.com/ .
        resp = MockResponse(
            content=b"<html>ok</html>", content_type="text/html", status_code=200, url="https://example.com/"
        )
        session = MockWaferSession(default=resp)

        async def _rac(_host):
            return (False, [])

        with patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)), _patch_wafer(session):
            result = await fetch_url("https://Example.COM./")

        assert "content" in result  # not falsely rejected
        assert "ok" in result["content"].lower()

    async def test_autodiscovered_feed_to_internal_host_refused(self):
        """SSRF: a forum page whose autodiscovered feed <link> points to an
        internal host must NOT be fetched — the feed host (from page content) is
        validated fail-closed before we connect, like the main fetch."""
        from fetchaller.tools.fetch import fetch_url

        listing_url = "https://new-forum.example.com/forums/general.1/"
        feed_url = "http://169.254.169.254/latest/meta-data/"  # cloud metadata
        listing_html = f"""<html id="XF">
<head><link rel="alternate" type="application/rss+xml" href="{feed_url}"></head>
<body><div class="p-body">Forum listing content</div></body>
</html>"""

        async def _rac(host):
            # public listing host is fine; the feed's internal host is blocked
            return (True, []) if host == "169.254.169.254" else (False, [])

        session = MockWaferSession(
            responses={listing_url: _html_response(listing_html, listing_url)},
        )
        with patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)), _patch_wafer(session):
            result = await fetch_url(listing_url)

        # Returned the page as markdown, never fetched the internal "feed".
        assert result.get("content_type") == "markdown"
        assert "forum listing content" in result["content"].lower()
        assert feed_url not in session.calls
        assert session.calls == [listing_url]


# ---------------------------------------------------------------------------
# wafer 0.3.2 cleanups: charset via resp.text, total-budget timeout, size cap
# ---------------------------------------------------------------------------


class TestWaferCleanups:
    def test_fetchresult_text_decodes_charset(self):
        """FetchResult.text uses wafer's charset-aware decode (header → meta → utf-8)."""
        from fetchaller.tools.fetch import FetchResult

        sjis = FetchResult(
            content="日本語".encode("shift_jis"),
            content_type="text/html; charset=shift_jis",
            status_code=200,
            final_url="https://x/",
            headers={"content-type": "text/html; charset=shift_jis"},
        )
        assert sjis.text == "日本語"
        # utf-8 fallback when no charset declared
        plain = FetchResult(
            content=b"hello", content_type="text/plain", status_code=200, final_url="https://x/", headers={}
        )
        assert plain.text == "hello"

    def test_fetchresult_text_uses_content_type_field_not_headers(self):
        """content_type is the source of truth even with empty / mismatched-case headers."""
        from fetchaller.tools.fetch import FetchResult

        # No headers at all — must still honor the declared charset.
        r = FetchResult(
            content="日本語".encode("shift_jis"),
            content_type="text/html; charset=shift_jis",
            status_code=200,
            final_url="https://x/",
            headers={},
        )
        assert r.text == "日本語"
        # A title-cased header with a WRONG charset must not win over content_type.
        r2 = FetchResult(
            content="café".encode("latin-1"),
            content_type="text/html; charset=latin-1",
            status_code=200,
            final_url="https://x/",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        assert r2.text == "café"

    async def test_timeout_is_strict_end_to_end_budget(self):
        """The caller's timeout is both the wafer total and outer call budget."""
        from datetime import timedelta

        from fetchaller.tools.fetch import (
            MAX_RESPONSE_SIZE,
            fetch_url,
        )

        captured: dict = {}
        session = MockWaferSession(default=_html_response("<html>ok</html>", "https://t.example/"))

        def _factory(*args, **kwargs):
            captured.update(kwargs)
            return session

        async def _rac(_host):
            return (False, ["1.2.3.4"])

        with (
            patch("fetchaller.tools.fetch.wafer.AsyncSession", side_effect=_factory),
            patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)),
        ):
            await fetch_url("https://t.example/", timeout=10)

        assert captured["attempt_timeout"] == timedelta(seconds=10)  # per-attempt cap = caller's timeout
        assert captured["timeout"] == timedelta(seconds=10)
        assert captured["max_response_size"] == MAX_RESPONSE_SIZE

    async def test_response_too_large_returns_error(self):
        import wafer

        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession()
        session.get = AsyncMock(side_effect=wafer.ResponseTooLarge("https://big.example/", 999, 100))
        with (
            _patch_wafer(session),
            patch(
                "fetchaller.tools.fetch.check_host",
                new_callable=AsyncMock,
                return_value=HostVerdict("big.example", False, []),
            ),
        ):
            result = await fetch_url("https://big.example/")
        assert "error" in result
        assert "too large" in result["error"].lower()


class TestInterceptorCacheNamespace:
    """The structured-intercept cache must not read back a generic-HTML fallback
    entry (they share normalize_url but the interceptor namespaces its key), and
    a failed structured fetch must retry rather than serve the stale fallback.
    """

    async def test_generic_fallback_not_served_as_structured(self):
        from fetchaller.cache.response_cache import ResponseCache
        from fetchaller.config import load_config
        from fetchaller.content.url import normalize_url
        from fetchaller.tools.fetch import fetch_url

        cfg = load_config()
        cache = ResponseCache.from_config(cfg)
        url = "https://www.aliexpress.com/item/1005009999999999.html"

        # A prior generic-HTML fallthrough cached under the PLAIN key.
        cache.set(normalize_url(url), "GENERIC-HTML-FALLBACK", "markdown")

        with (
            _PATCH_SSRF,
            patch(
                "fetchaller.aliexpress.product.get_product",
                new=AsyncMock(return_value={"content": "STRUCTURED-API-RESULT"}),
            ) as gp,
        ):
            r1 = await fetch_url(url, max_tokens=25000, cache=cache, config=cfg)
            # Interceptor must NOT serve the generic fallback; it calls the API.
            assert r1["content"].startswith("STRUCTURED")
            assert gp.await_count == 1

            # Second call is served from the interceptor's OWN namespaced entry.
            r2 = await fetch_url(url, max_tokens=25000, cache=cache, config=cfg)
            assert r2["content"].startswith("STRUCTURED")
            assert r2.get("cached") is True
            assert gp.await_count == 1  # not re-fetched

        # The plain generic key is left untouched.
        assert cache.get(normalize_url(url)).content == "GENERIC-HTML-FALLBACK"

    async def test_alibaba_search_intercept_preserves_caller_timeout(self):
        from fetchaller.tools.fetch import fetch_url

        url = (
            "https://www.alibaba.com/trade/search?"
            "SearchText=timeout-propagation-unique"
            "&page=3&sortType=PRICE_DESC&minPrice=2.5&maxPrice=9"
        )
        with (
            _PATCH_SSRF,
            patch(
                "fetchaller.alibaba.search.search_alibaba",
                new=AsyncMock(return_value={"content": "STRUCTURED-SEARCH-RESULT"}),
            ) as search,
        ):
            result = await fetch_url(url, timeout=37)

        assert result["content"] == "STRUCTURED-SEARCH-RESULT"
        assert search.await_args.kwargs["timeout"] == 37
        assert search.await_args.kwargs["page"] == 3
        assert search.await_args.kwargs["sort"] == "price_desc"
        assert search.await_args.kwargs["min_price"] == 2.5
        assert search.await_args.kwargs["max_price"] == 9.0

    async def test_other_commerce_intercepts_preserve_timeout_and_filters(self):
        from fetchaller.tools.fetch import fetch_url

        aliexpress_product_url = "https://www.aliexpress.com/item/1005009999999911.html"
        alibaba_product_url = "https://www.alibaba.com/product-detail/Timeout-Widget_1600999999911.html"
        aliexpress_search_url = (
            "https://www.aliexpress.com/w/wholesale-timeout-widget.html?"
            "page=4&sortType=total_tranpro_desc&minPrice=3.5&maxPrice=12"
        )

        with (
            _PATCH_SSRF,
            patch(
                "fetchaller.aliexpress.product.get_product",
                new=AsyncMock(return_value={"content": "AE-PRODUCT"}),
            ) as ae_product,
            patch(
                "fetchaller.alibaba.product.get_product",
                new=AsyncMock(return_value={"content": "ALIBABA-PRODUCT"}),
            ) as alibaba_product,
            patch(
                "fetchaller.aliexpress.search.search_aliexpress",
                new=AsyncMock(return_value={"content": "AE-SEARCH"}),
            ) as ae_search,
        ):
            assert (await fetch_url(aliexpress_product_url, timeout=37))["content"] == "AE-PRODUCT"
            assert (await fetch_url(alibaba_product_url, timeout=38))["content"] == "ALIBABA-PRODUCT"
            assert (await fetch_url(aliexpress_search_url, timeout=39))["content"] == "AE-SEARCH"

        assert ae_product.await_args.kwargs["timeout"] == 37
        assert alibaba_product.await_args.kwargs["timeout"] == 38
        assert ae_search.await_args.kwargs["timeout"] == 39
        assert ae_search.await_args.kwargs["page"] == 4
        assert ae_search.await_args.kwargs["sort"] == "orders"
        assert ae_search.await_args.kwargs["min_price"] == 3.5
        assert ae_search.await_args.kwargs["max_price"] == 12.0

    async def test_search_intercepts_decode_encoded_scientific_filters(self):
        from fetchaller.tools.fetch import fetch_url

        alibaba_url = (
            "https://www.alibaba.com/trade/search?SearchText=large-filter-unique&minPrice=1e%2B20&maxPrice=2e%2B20"
        )
        aliexpress_url = (
            "https://www.aliexpress.com/w/wholesale-large-filter-unique.html?minPrice=1e%2B20&maxPrice=2e%2B20"
        )
        with (
            _PATCH_SSRF,
            patch(
                "fetchaller.alibaba.search.search_alibaba",
                new=AsyncMock(return_value={"content": "ALIBABA"}),
            ) as alibaba_search,
            patch(
                "fetchaller.aliexpress.search.search_aliexpress",
                new=AsyncMock(return_value={"content": "ALIEXPRESS"}),
            ) as aliexpress_search,
        ):
            assert (await fetch_url(alibaba_url))["content"] == "ALIBABA"
            assert (await fetch_url(aliexpress_url))["content"] == "ALIEXPRESS"

        assert alibaba_search.await_args.kwargs["min_price"] == 1e20
        assert alibaba_search.await_args.kwargs["max_price"] == 2e20
        assert aliexpress_search.await_args.kwargs["min_price"] == 1e20
        assert aliexpress_search.await_args.kwargs["max_price"] == 2e20


class TestHangIsBoundedByTheCallerTimeout:
    """A hang must degrade to a failed fetch, never to a stuck process.

    Reported against wellfound.com: an instant 403 bot challenge after which the
    solver hung and the configured timeout was ignored, freezing two long-running
    crawler processes for ~2 hours each. The interceptor path is the hazard —
    site handlers run their own sessions with their own budgets, and wellfound's
    is 90s regardless of what the caller asked for.
    """

    async def test_a_never_returning_interceptor_still_honours_the_timeout(self):
        import asyncio
        import time as _time

        from fetchaller.tools.fetch import fetch_url

        async def _never_returns(*args, **kwargs):
            await asyncio.sleep(3600)

        async def _rac(_host):
            return (False, ["1.2.3.4"])

        with (
            patch("fetchaller.wellfound.page.get_wellfound", side_effect=_never_returns),
            patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)),
        ):
            started = _time.monotonic()
            result = await fetch_url("https://wellfound.com/jobs", timeout=2)
            elapsed = _time.monotonic() - started

        assert "error" in result
        assert "timed out" in result["error"]
        assert elapsed < 5, f"outer deadline did not fire: {elapsed:.1f}s"

    async def test_a_hanging_transport_still_honours_the_timeout(self):
        import asyncio
        import time as _time

        from fetchaller.tools.fetch import fetch_url

        class _HangingSession:
            def __init__(self, *args, **kwargs):
                pass

            async def get(self, *args, **kwargs):
                await asyncio.sleep(3600)

            async def request(self, *args, **kwargs):
                await asyncio.sleep(3600)

        async def _rac(_host):
            return (False, ["1.2.3.4"])

        with (
            patch("fetchaller.tools.fetch.wafer.AsyncSession", _HangingSession),
            patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)),
        ):
            started = _time.monotonic()
            result = await fetch_url("https://slow.example/", timeout=2)
            elapsed = _time.monotonic() - started

        assert "error" in result
        assert "timed out" in result["error"]
        assert elapsed < 5, f"outer deadline did not fire: {elapsed:.1f}s"

    async def test_wellfound_receives_the_callers_budget(self):
        """Without this the request runs on a 90s session budget that a caller
        asking for 10s never agreed to."""
        from fetchaller.tools.fetch import fetch_url

        captured: dict = {}

        async def _capture(url, browser_solver=None, timeout=None):
            captured["timeout"] = timeout
            return {"content": "ok"}

        async def _rac(_host):
            return (False, ["1.2.3.4"])

        with (
            patch("fetchaller.wellfound.page.get_wellfound", side_effect=_capture),
            patch("fetchaller.tools.fetch.check_host", side_effect=_verdict_from(_rac)),
        ):
            await fetch_url("https://wellfound.com/jobs", timeout=12)

        assert captured["timeout"] == 12.0


# ---------------------------------------------------------------------------
# Content-Type sniffing — the header is not the last word on the media type
# ---------------------------------------------------------------------------


def _make_test_pdf(text: str = "Hello from a mislabelled PDF") -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    writer = pymupdf.TextWriter(page.rect)
    writer.append((72, 72), text)
    writer.write_text(page)
    content = doc.tobytes()
    doc.close()
    return content


_PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestContentTypeSniffing:
    """S3/CDN-hosted files carry the uploader's Content-Type, not the file's.

    reolink.us serves its spec-sheet PDFs as ``multipart/form-data;`` — the
    dispatch used to reject them as an unsupported type even though the body
    starts with ``%PDF-``.
    """

    def test_file_signature_overrides_the_declared_type(self):
        from fetchaller.tools.fetch import sniff_content_type

        assert sniff_content_type(b"%PDF-1.5\n%\xb5\xed\xae\xfb", "multipart/form-data") == "application/pdf"
        assert sniff_content_type(_PNG_1PX, "application/octet-stream") == "image/png"
        # Even a type the dispatch understands loses to the bytes.
        assert sniff_content_type(b"%PDF-1.4\n", "text/html") == "application/pdf"

    def test_an_accurate_header_is_left_alone(self):
        from fetchaller.tools.fetch import sniff_content_type

        assert sniff_content_type(b"%PDF-1.5\n", "application/pdf") is None
        assert sniff_content_type(_PNG_1PX, "image/png") is None
        assert sniff_content_type(b"<html><body>hi</body></html>", "text/html") is None
        # Markup shape is weak evidence: a dispatchable header still wins.
        assert sniff_content_type(b"<html>not really</html>", "text/plain") is None
        assert sniff_content_type(b'{"a": 1}', "application/json") is None

    def test_markup_fills_in_for_an_undispatchable_type(self):
        from fetchaller.tools.fetch import sniff_content_type

        assert sniff_content_type(b"\xef\xbb\xbf<!DOCTYPE html>\n<html>", "application/octet-stream") == "text/html"
        assert sniff_content_type(b'  <?xml version="1.0"?><rss>', "binary/octet-stream") == "application/xml"
        assert sniff_content_type(b"<rss version='2.0'>", "") == "application/rss+xml"

    def test_unidentifiable_bytes_keep_the_declared_type(self):
        from fetchaller.tools.fetch import sniff_content_type

        assert sniff_content_type(b"\x00\x01\x02\x03binary junk", "application/octet-stream") is None
        assert sniff_content_type(b"", "multipart/form-data") is None
        # "BM" alone is printable ASCII — a bad size field is not a bitmap.
        assert sniff_content_type(b"BM garbage that is not a bitmap", "application/octet-stream") is None

    @_PATCH_SSRF
    async def test_pdf_served_as_multipart_form_data_is_extracted(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://home-cdn.example.com/specs-pdf/doorbell.pdf"
        session = MockWaferSession(
            {
                url: MockResponse(
                    content=_make_test_pdf(),
                    content_type="multipart/form-data;",
                    status_code=200,
                    url=url,
                )
            }
        )
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert "error" not in result
        assert result["content_type"] == "pdf"
        assert "Hello from a mislabelled PDF" in result["content"]

    @_PATCH_SSRF
    async def test_image_served_as_octet_stream_is_summarized(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://cdn.example.com/uploads/logo.png"
        session = MockWaferSession(
            {
                url: MockResponse(
                    content=_PNG_1PX,
                    content_type="application/octet-stream",
                    status_code=200,
                    url=url,
                )
            }
        )
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert "error" not in result
        assert "[Image: image/png]" in result["content"]

    @_PATCH_SSRF
    async def test_html_served_as_octet_stream_becomes_markdown(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://cdn.example.com/page"
        session = MockWaferSession(
            {
                url: MockResponse(
                    content=b"<html><head><title>Mislabelled</title></head><body><p>Real content here.</p></body></html>",
                    content_type="application/octet-stream",
                    status_code=200,
                    url=url,
                )
            }
        )
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert "error" not in result
        assert result["content_type"] == "markdown"
        assert "Real content here." in result["content"]

    @_PATCH_SSRF
    async def test_unidentifiable_binary_still_reports_unsupported(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://cdn.example.com/firmware.bin"
        session = MockWaferSession(
            {
                url: MockResponse(
                    content=b"\x00\x01\x02\x03\x04not a known format",
                    content_type="application/octet-stream",
                    status_code=200,
                    url=url,
                )
            }
        )
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert "Unsupported content type" in result["error"]


class TestRenderFallbackOnChallenge:
    """A WAF challenge must try wafer's render path before giving up.

    `get()` and `render()` do not have the same reach on interstitials.
    Measured on support.lutron.com (Imperva): `get()` raises ChallengeDetected
    even with a real system-Chrome solver attached, while `render()` returns
    200 with 110,305 bytes of the article. Returning the error without trying
    render meant fetchaller reported "could not be bypassed" for a page wafer
    could already fetch.
    """

    def _session(self, *, content=b"<html><body>Real page</body></html>", raises=None):
        class FakeResponse:
            status_code = 200
            url = "https://support.example.com/article"
            headers = {"content-type": "text/html"}

            def __init__(self, body):
                self.content = body

        class FakeSession:
            async def render(self, url, timeout=None):
                if raises is not None:
                    raise raises
                return FakeResponse(content)

        return FakeSession()

    async def _call(self, session, *, method="GET", solver=object(), deadline_in=120.0):
        from fetchaller.tools.fetch import _render_after_challenge

        return await _render_after_challenge(
            session,
            "https://support.example.com/article",
            method=method,
            browser_solver=solver,
            deadline=time.monotonic() + deadline_in,
        )

    async def test_a_challenged_get_is_retried_through_render(self):
        out = await self._call(self._session())
        assert out is not None
        assert out.status_code == 200
        assert b"Real page" in out.content

    async def test_post_is_never_rendered(self):
        # A render is a navigation: it cannot carry the caller's method, body
        # or headers, so replaying a POST would send something never asked for.
        assert await self._call(self._session(), method="POST") is None

    async def test_no_solver_means_no_fallback(self):
        assert await self._call(self._session(), solver=None) is None

    async def test_an_exhausted_deadline_is_respected(self):
        assert await self._call(self._session(), deadline_in=1.0) is None

    async def test_a_failing_render_falls_back_to_the_original_error(self):
        assert await self._call(self._session(raises=RuntimeError("boom"))) is None

    async def test_an_empty_render_is_not_treated_as_success(self):
        assert await self._call(self._session(content=b"")) is None
