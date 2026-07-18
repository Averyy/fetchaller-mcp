"""Integration tests for fetch_url() — the full pipeline.

These tests mock the HTTP layer (wafer.AsyncSession) and SSRF check, then
exercise fetch_url() end-to-end. They catch bugs where site detection, URL
transforms, feed autodiscovery, or content type dispatch interact incorrectly.

The critical class is TestForumThreadNotHijacked: it verifies that forum thread
URLs on known domains are NOT hijacked by Tier 2 feed autodiscovery.
"""

from unittest.mock import AsyncMock, patch

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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def add_cookie(self, raw_set_cookie: str, url: str) -> None:
        pass

    async def get(self, url: str, **kwargs) -> MockResponse:
        self.calls.append(url)
        return self.responses.get(url, self.default)


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
# Patch helper — resolve_and_check must be async and return (is_private, ips).
# (False, []) = public host, no pin. fetch_url() uses this for both the early
# guard and the connection pin, so patching it covers the whole SSRF path.
# ---------------------------------------------------------------------------

_PATCH_SSRF = patch(
    "fetchaller.tools.fetch.resolve_and_check",
    new_callable=AsyncMock,
    return_value=(False, []),
)


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
    """Reddit URLs should be transformed to old.reddit.com."""

    @_PATCH_SSRF
    async def test_www_to_old(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://www.reddit.com/r/homelab/"
        expected_fetch_url = "https://old.reddit.com/r/homelab/"

        session = MockWaferSession(
            responses={expected_fetch_url: _html_response(
                "<html><body><div class='content'><p>Reddit post</p></div></body></html>",
                expected_fetch_url,
            )},
        )
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert result.get("content_type") == "markdown"
        assert "[Fetched via:" in result["content"]
        assert session.calls == [expected_fetch_url]

    @_PATCH_SSRF
    async def test_old_not_double_transformed(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        url = "https://old.reddit.com/r/homelab/"
        session = MockWaferSession(default=_html_response(
            "<html><body><p>content</p></body></html>", url,
        ))
        with _patch_wafer(session):
            result = await fetch_url(url)

        assert result.get("content_type") == "markdown"
        # The fetch URL should be old.reddit.com, not old.old.reddit.com
        assert session.calls[0].startswith("https://old.reddit.com/")


# ---------------------------------------------------------------------------
# Content type dispatch
# ---------------------------------------------------------------------------


class TestContentTypeDispatch:
    """fetch_url() must return the correct content_type for each MIME type."""

    @_PATCH_SSRF
    async def test_json(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(default=MockResponse(
            content=b'{"key": "value"}',
            content_type="application/json",
            status_code=200,
            url="https://api.example.com/data",
        ))
        with _patch_wafer(session):
            result = await fetch_url("https://api.example.com/data")
        assert result["content_type"] == "json"
        assert '"key"' in result["content"]

    @_PATCH_SSRF
    async def test_plain_text(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(default=MockResponse(
            content=b"Hello world",
            content_type="text/plain",
            status_code=200,
            url="https://example.com/file.txt",
        ))
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/file.txt")
        assert result["content_type"] == "text"
        assert "Hello world" in result["content"]

    @_PATCH_SSRF
    async def test_rss_xml(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(default=_feed_response(_SAMPLE_FEED_XML, "https://example.com/feed.rss"))
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/feed.rss")
        assert result["content_type"] == "markdown"
        assert "Thread 1" in result["content"]

    @_PATCH_SSRF
    async def test_html(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(default=_html_response(
            "<html><body><p>Hello</p></body></html>",
            "https://example.com/page",
        ))
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/page")
        assert result["content_type"] == "markdown"
        assert "Hello" in result["content"]

    @_PATCH_SSRF
    async def test_svg_returned_as_raw_xml(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
        session = MockWaferSession(default=MockResponse(
            content=svg,
            content_type="image/svg+xml",
            status_code=200,
            url="https://example.com/logo.svg",
        ))
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
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 42, 17) + b"\x08\x06\x00\x00\x00" + b"\x00" * 4
        session = MockWaferSession(default=MockResponse(
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
        ))
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

        _mock_ssrf.return_value = (True, [])  # Simulate SSRF detection
        result = await fetch_url("https://internal.corp/secret")
        assert "error" in result
        assert "private" in result["error"].lower()
        _mock_ssrf.return_value = (False, [])  # Reset for other tests

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

        session = MockWaferSession(default=MockResponse(
            content=b"rate limited",
            content_type="text/plain",
            status_code=429,
            url="https://example.com/api",
        ))
        with _patch_wafer(session):
            result = await fetch_url("https://example.com/api")
        assert "error" in result
        assert "429" in result["error"]

    @_PATCH_SSRF
    async def test_http_404(self, _mock_ssrf):
        from fetchaller.tools.fetch import fetch_url

        session = MockWaferSession(default=MockResponse(
            content=b"not found",
            content_type="text/html",
            status_code=404,
            url="https://example.com/missing",
        ))
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
        session = MockWaferSession(
            default=_html_response("<html><body>ok</body></html>", "https://pin.example/")
        )

        def _factory(*args, **kwargs):
            captured.update(kwargs)
            return session

        async def _rac(_host):
            return (False, ["203.0.113.7"])

        with patch("fetchaller.tools.fetch.wafer.AsyncSession", side_effect=_factory), \
             patch("fetchaller.tools.fetch.resolve_and_check", side_effect=_rac):
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
        with patch("fetchaller.tools.fetch.resolve_and_check", side_effect=_rac), \
             _patch_wafer(session):
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
        with patch("fetchaller.tools.fetch.resolve_and_check", side_effect=_rac), \
             _patch_wafer(session):
            result = await fetch_url(start)

        assert "arrived" in result["content"].lower()
        assert session.calls == [start, final]
