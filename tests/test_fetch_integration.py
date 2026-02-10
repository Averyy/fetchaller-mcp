"""Integration tests for fetch_url() — the full pipeline.

These tests mock the HTTP layer (ContentFetcher) and SSRF check, then exercise
fetch_url() end-to-end. They catch bugs where site detection, URL transforms,
feed autodiscovery, or content type dispatch interact incorrectly.

The critical class is TestForumThreadNotHijacked: it verifies that forum thread
URLs on known domains are NOT hijacked by Tier 2 feed autodiscovery.
"""

from unittest.mock import AsyncMock, patch

from fetchaller.content.fetcher import FetchResult

# ---------------------------------------------------------------------------
# MockFetcher — returns pre-configured FetchResult objects, tracks calls
# ---------------------------------------------------------------------------


class MockFetcher:
    """Drop-in replacement for ContentFetcher that returns canned responses."""

    def __init__(self, responses: dict[str, FetchResult] | None = None, default: FetchResult | None = None):
        """
        Args:
            responses: URL → FetchResult mapping for specific URLs
            default: FetchResult to return for URLs not in responses
        """
        self.responses = responses or {}
        self.default = default or FetchResult(
            content=b"<html><body><p>default</p></body></html>",
            content_type="text/html",
            status_code=200,
            final_url="https://example.com",
            headers={},
        )
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs) -> FetchResult:
        self.calls.append(url)
        return self.responses.get(url, self.default)

    async def close(self):
        pass


def _html_result(html: str, url: str, content_type: str = "text/html") -> FetchResult:
    """Build a FetchResult from an HTML string."""
    return FetchResult(
        content=html.encode(),
        content_type=content_type,
        status_code=200,
        final_url=url,
        headers={},
    )


def _feed_result(xml: str, url: str) -> FetchResult:
    """Build a FetchResult from XML feed string."""
    return FetchResult(
        content=xml.encode(),
        content_type="application/rss+xml",
        status_code=200,
        final_url=url,
        headers={},
    )


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
# Patch helper — is_private_host must be async and return False
# ---------------------------------------------------------------------------

_PATCH_SSRF = patch("fetchaller.tools.fetch.is_private_host", new_callable=AsyncMock, return_value=False)


# ---------------------------------------------------------------------------
# Forum thread NOT hijacked by feed autodiscovery
# ---------------------------------------------------------------------------


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

        fetcher = MockFetcher(default=_html_result(thread_html, thread_url))
        result = await fetch_url(thread_url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        assert "thread content" in result["content"].lower()
        # Only one fetch call — no feed autodiscovery
        assert len(fetcher.calls) == 1

    @_PATCH_SSRF
    async def test_vbulletin_thread_not_hijacked(self, _mock_ssrf):
        """vBulletin thread (showthread.php) on known domain with <link rel="alternate"> → HTML, not feed."""
        from fetchaller.tools.fetch import fetch_url

        thread_url = "https://u11.bimmerpost.com/forums/showthread.php?t=12345"
        thread_html = _vbulletin_thread_html()

        fetcher = MockFetcher(default=_html_result(thread_html, thread_url))
        result = await fetch_url(thread_url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        assert "thread body content" in result["content"].lower()
        assert len(fetcher.calls) == 1

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

        fetcher = MockFetcher(default=_html_result(thread_html, thread_url))
        result = await fetch_url(thread_url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        # Should have thread content, not feed listing
        assert "rfd deal thread content" in result["content"].lower()
        assert len(fetcher.calls) == 1

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

        fetcher = MockFetcher(
            responses={
                thread_url: _html_result(thread_html, thread_url),
                feed_url: _feed_result(_SAMPLE_FEED_XML, feed_url),
            },
        )
        result = await fetch_url(thread_url, fetcher=fetcher)

        assert "thread content" in result["content"].lower()
        assert len(fetcher.calls) == 1


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

        fetcher = MockFetcher(
            responses={feed_url: _feed_result(_SAMPLE_FEED_XML, feed_url)},
        )
        result = await fetch_url(listing_url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        assert "Thread 1" in result["content"]
        # Should fetch the feed URL, not the listing URL
        assert fetcher.calls == [feed_url]

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
        fetcher = MockFetcher(
            responses={expected_feed: _feed_result(feed_xml, expected_feed)},
        )
        result = await fetch_url(listing_url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        assert "New BMW X1" in result["content"]
        assert fetcher.calls == [expected_feed]

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

        fetcher = MockFetcher(
            responses={
                listing_url: _html_result(listing_html, listing_url),
                feed_url: _feed_result(_SAMPLE_FEED_XML, feed_url),
            },
        )
        result = await fetch_url(listing_url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        assert "Thread 1" in result["content"]
        # Two fetches: original page + autodiscovered feed
        assert len(fetcher.calls) == 2

    @_PATCH_SSRF
    async def test_unknown_forum_no_feed_falls_through(self, _mock_ssrf):
        """Unknown forum with no <link rel="alternate"> → falls through to HTML markdown."""
        from fetchaller.tools.fetch import fetch_url

        url = "https://new-forum.example.com/forums/general/"
        html = """<html id="XF">
<head><title>Forum Page</title></head>
<body><div class="p-body"><p>Forum listing content here</p></div></body>
</html>"""

        fetcher = MockFetcher(default=_html_result(html, url))
        result = await fetch_url(url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        assert "forum listing content" in result["content"].lower()
        assert len(fetcher.calls) == 1


# ---------------------------------------------------------------------------
# URL transforms
# ---------------------------------------------------------------------------


class TestRedditUrlTransform:
    """Reddit URLs should be transformed to old.reddit.com."""

    @_PATCH_SSRF
    async def test_www_to_old(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/homelab/"
        expected_fetch_url = "https://old.reddit.com/r/homelab/"

        fetcher = MockFetcher(
            responses={expected_fetch_url: _html_result(
                "<html><body><div class='content'><p>Reddit post</p></div></body></html>",
                expected_fetch_url,
            )},
        )
        result = await fetch_url(url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        assert "[Fetched via:" in result["content"]
        assert fetcher.calls == [expected_fetch_url]

    @_PATCH_SSRF
    async def test_old_not_double_transformed(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://old.reddit.com/r/homelab/"
        fetcher = MockFetcher(default=_html_result(
            "<html><body><p>content</p></body></html>", url,
        ))
        result = await fetch_url(url, fetcher=fetcher)

        assert result.get("content_type") == "markdown"
        # The fetch URL should be old.reddit.com, not old.old.reddit.com
        assert fetcher.calls[0].startswith("https://old.reddit.com/")


# ---------------------------------------------------------------------------
# Content type dispatch
# ---------------------------------------------------------------------------


class TestContentTypeDispatch:
    """fetch_url() must return the correct content_type for each MIME type."""

    @_PATCH_SSRF
    async def test_json(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher(default=FetchResult(
            content=b'{"key": "value"}',
            content_type="application/json",
            status_code=200,
            final_url="https://api.example.com/data",
            headers={},
        ))
        result = await fetch_url("https://api.example.com/data", fetcher=fetcher)
        assert result["content_type"] == "json"
        assert '"key"' in result["content"]

    @_PATCH_SSRF
    async def test_plain_text(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher(default=FetchResult(
            content=b"Hello world",
            content_type="text/plain",
            status_code=200,
            final_url="https://example.com/file.txt",
            headers={},
        ))
        result = await fetch_url("https://example.com/file.txt", fetcher=fetcher)
        assert result["content_type"] == "text"
        assert "Hello world" in result["content"]

    @_PATCH_SSRF
    async def test_rss_xml(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher(default=_feed_result(_SAMPLE_FEED_XML, "https://example.com/feed.rss"))
        result = await fetch_url("https://example.com/feed.rss", fetcher=fetcher)
        assert result["content_type"] == "markdown"
        assert "Thread 1" in result["content"]

    @_PATCH_SSRF
    async def test_html(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher(default=_html_result(
            "<html><body><p>Hello</p></body></html>",
            "https://example.com/page",
        ))
        result = await fetch_url("https://example.com/page", fetcher=fetcher)
        assert result["content_type"] == "markdown"
        assert "Hello" in result["content"]


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

        _mock_ssrf.return_value = True  # Simulate SSRF detection
        result = await fetch_url("https://internal.corp/secret")
        assert "error" in result
        assert "private" in result["error"].lower()
        _mock_ssrf.return_value = False  # Reset for other tests

    @_PATCH_SSRF
    async def test_timeout(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher()
        fetcher.fetch = AsyncMock(side_effect=TimeoutError("timed out"))
        result = await fetch_url("https://example.com/slow", fetcher=fetcher)
        assert "error" in result
        assert "timed out" in result["error"].lower()

    @_PATCH_SSRF
    async def test_connection_error(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher()
        fetcher.fetch = AsyncMock(side_effect=ConnectionError("ECONNREFUSED"))
        result = await fetch_url("https://example.com/down", fetcher=fetcher)
        assert "error" in result
        assert "refused" in result["error"].lower()

    @_PATCH_SSRF
    async def test_http_429(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher(default=FetchResult(
            content=b"rate limited",
            content_type="text/plain",
            status_code=429,
            final_url="https://example.com/api",
            headers={},
        ))
        result = await fetch_url("https://example.com/api", fetcher=fetcher)
        assert "error" in result
        assert "429" in result["error"]

    @_PATCH_SSRF
    async def test_http_404(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        fetcher = MockFetcher(default=FetchResult(
            content=b"not found",
            content_type="text/html",
            status_code=404,
            final_url="https://example.com/missing",
            headers={},
        ))
        result = await fetch_url("https://example.com/missing", fetcher=fetcher)
        assert "error" in result
        assert "404" in result["error"]
