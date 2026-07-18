"""Tests for Ashby script-tag embed detection on company career pages.

Companies that embed the Ashby job board via
``<script src="https://jobs.ashbyhq.com/{org}/embed">`` (e.g.
skywatch.com/careers/) need the org slug extracted so we can hit the
canonical board API.
"""

from unittest.mock import AsyncMock, patch

from fetchaller.content import ashby


def test_basic_embed_script():
    html = '<script src="https://jobs.ashbyhq.com/skywatch/embed" data-rocket-defer defer></script>'
    assert ashby.extract_ashby_embed_slug_from_html(html) == "skywatch"


def test_embed_script_with_query():
    html = '<script src="https://jobs.ashbyhq.com/example-co/embed?v=2"></script>'
    assert ashby.extract_ashby_embed_slug_from_html(html) == "example-co"


def test_no_embed_script():
    assert ashby.extract_ashby_embed_slug_from_html("<div>nothing</div>") is None


def test_ignores_other_ashby_subpaths():
    # The /api or /embed paths without an org slug should not match.
    html = '<script src="https://jobs.ashbyhq.com/api/some.js"></script>'
    assert ashby.extract_ashby_embed_slug_from_html(html) is None


async def test_resolve_embed_refuses_internal_chunk_url():
    """SSRF: the careers-chunk <script src> is attacker-controlled page content;
    an internal chunk host must be refused before it is fetched."""
    page_html = (
        '<html><script src="http://169.254.169.254/x/_next/static/chunks/'
        'pages/careers-a.js"></script></html>'
    )
    calls: list[str] = []

    class _Resp:
        def __init__(self, u):
            self.text = page_html
            self.url = u

    class _Session:
        async def get(self, u, **kwargs):
            calls.append(u)
            return _Resp(u)

    jid = "12345678-1234-1234-1234-123456789abc"
    with patch("fetchaller.content.ashby.is_private_host", new_callable=AsyncMock, return_value=True):
        result = await ashby.resolve_ashby_embed_url(
            f"https://attacker.example/careers?ashby_jid={jid}", _Session()
        )
    assert result is None
    # The internal chunk URL was never fetched (only the embed page was).
    assert not any("169.254.169.254" in c for c in calls)
