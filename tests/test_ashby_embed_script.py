"""Tests for Ashby script-tag embed detection on company career pages.

Companies that embed the Ashby job board via
``<script src="https://jobs.ashbyhq.com/{org}/embed">`` (e.g.
skywatch.com/careers/) need the org slug extracted so we can hit the
canonical board API.
"""

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
