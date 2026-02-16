"""Tests for _detect_site() — the single dispatch point for all site-specific behavior.

If _detect_site() returns the wrong key (or None) for a URL, the entire
selector + postprocessor pipeline silently falls back to generic cleanup.
These tests ensure every site is correctly detected by URL and by HTML fallback.
"""

from bs4 import BeautifulSoup

from fetchaller.content.html import _detect_site

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _soup(html: str) -> BeautifulSoup:
    """Parse minimal HTML into soup."""
    return BeautifulSoup(html, "lxml")


def _empty_soup() -> BeautifulSoup:
    return _soup("<html><body></body></html>")


# ---------------------------------------------------------------------------
# URL-based detection
# ---------------------------------------------------------------------------


class TestDetectSiteByUrl:
    """Every site must be detected by its URL alone (no soup needed)."""

    def test_amazon_ca(self):
        assert _detect_site("https://www.amazon.ca/dp/B0D1XD1ZV3", False) == "amazon"

    def test_amazon_com(self):
        assert _detect_site("https://www.amazon.com/dp/B0D1XD1ZV3", False) == "amazon"

    def test_amazon_co_uk(self):
        assert _detect_site("https://www.amazon.co.uk/dp/B0D1XD1ZV3", False) == "amazon"

    def test_amazon_de(self):
        assert _detect_site("https://www.amazon.de/dp/B0D1XD1ZV3", False) == "amazon"

    def test_amazon_gp_product(self):
        assert _detect_site("https://www.amazon.ca/gp/product/B08KTM4SNY", False) == "amazon"

    def test_hackernews(self):
        assert _detect_site("https://news.ycombinator.com/", False) == "hackernews"

    def test_hackernews_item(self):
        assert _detect_site("https://news.ycombinator.com/item?id=123", False) == "hackernews"

    def test_github(self):
        assert _detect_site("https://github.com/owner/repo", False) == "github"

    def test_github_www(self):
        assert _detect_site("https://www.github.com/owner/repo", False) == "github"

    def test_github_issues(self):
        assert _detect_site("https://github.com/owner/repo/issues/1", False) == "github"

    def test_huggingface(self):
        assert _detect_site("https://huggingface.co/org/model", False) == "huggingface"

    def test_huggingface_www(self):
        assert _detect_site("https://www.huggingface.co/org/model", False) == "huggingface"

    def test_redflagdeals(self):
        assert _detect_site("https://forums.redflagdeals.com/hot-deals-f9/", False) == "redflagdeals"

    def test_redflagdeals_thread(self):
        assert _detect_site("https://forums.redflagdeals.com/some-deal-12345/", False) == "redflagdeals"

    def test_stackoverflow(self):
        assert _detect_site("https://stackoverflow.com/questions/1", False) == "stackoverflow"

    def test_superuser(self):
        assert _detect_site("https://superuser.com/questions/1", False) == "stackoverflow"

    def test_askubuntu(self):
        assert _detect_site("https://askubuntu.com/questions/1", False) == "stackoverflow"

    def test_serverfault(self):
        assert _detect_site("https://serverfault.com/questions/1", False) == "stackoverflow"

    def test_mathoverflow(self):
        assert _detect_site("https://mathoverflow.net/questions/1", False) == "stackoverflow"

    def test_stackexchange_subdomain(self):
        assert _detect_site("https://gaming.stackexchange.com/questions/1", False) == "stackoverflow"

    def test_soylent_ca(self):
        assert _detect_site("https://www.soylent.ca/products/soylent-drink", False) == "soylent"

    def test_soylent_com(self):
        assert _detect_site("https://www.soylent.com/products/soylent-drink", False) == "soylent"

    def test_soylent_ca_no_www(self):
        assert _detect_site("https://soylent.ca/collections/all", False) == "soylent"

    def test_soylent_com_no_www(self):
        assert _detect_site("https://soylent.com/collections/all", False) == "soylent"

    def test_medium(self):
        assert _detect_site("https://medium.com/@user/article-slug", False) == "medium"

    def test_medium_subdomain(self):
        assert _detect_site("https://engineering.medium.com/article", False) == "medium"

    def test_wikipedia(self):
        assert _detect_site("https://en.wikipedia.org/wiki/Python", False) == "wikipedia"

    def test_wikipedia_other_lang(self):
        assert _detect_site("https://fr.wikipedia.org/wiki/Article", False) == "wikipedia"

    def test_generic_returns_none(self):
        assert _detect_site("https://example.com/page", False) is None

    def test_none_url_returns_none(self):
        assert _detect_site(None, False) is None


# ---------------------------------------------------------------------------
# HTML-based fallback detection
# ---------------------------------------------------------------------------


class TestDetectSiteByHtml:
    """Sites detected via HTML markers when URL doesn't match."""

    def test_medium_custom_domain(self):
        html = '<html><body><button data-testid="headerSignUpButton">Sign up</button></body></html>'
        assert _detect_site("https://blog.example.com/post", False, _soup(html)) == "medium"

    def test_discourse_meta_generator(self):
        html = '<html><head><meta name="generator" content="Discourse 3.2.0"></head><body></body></html>'
        assert _detect_site("https://forum.example.com/", False, _soup(html)) == "discourse"

    def test_xenforo_html_id(self):
        html = '<html id="XF"><body></body></html>'
        assert _detect_site("https://forum.example.com/", False, _soup(html)) == "forum"

    def test_xenforo_1x_html_id(self):
        html = '<html id="XenForo"><body></body></html>'
        assert _detect_site("https://forum.example.com/", False, _soup(html)) == "forum"

    def test_vbulletin_meta_generator(self):
        html = '<html><head><meta name="generator" content="vBulletin 4.2.5"></head><body></body></html>'
        assert _detect_site("https://forum.example.com/", False, _soup(html)) == "forum"

    def test_phpbb_body_id(self):
        html = '<html><body id="phpbb"><div>content</div></body></html>'
        assert _detect_site("https://forum.example.com/", False, _soup(html)) == "forum"

    def test_phpbb_powered_by(self):
        html = '<html><body><div>Powered by phpBB</div></body></html>'
        assert _detect_site("https://forum.example.com/", False, _soup(html)) == "forum"

    def test_generic_html_returns_none(self):
        html = '<html><body><div>Just a page</div></body></html>'
        assert _detect_site("https://example.com/", False, _soup(html)) is None

    def test_no_soup_returns_none(self):
        """Without soup, HTML-based fallbacks can't fire."""
        assert _detect_site("https://example.com/", False, None) is None


# ---------------------------------------------------------------------------
# Priority / override rules
# ---------------------------------------------------------------------------


class TestDetectSitePriority:
    """Verify detection priority: is_reddit > URL match > HTML fallback."""

    def test_is_reddit_overrides_url(self):
        """is_reddit=True must return 'reddit' even if URL matches another site."""
        # This shouldn't happen in practice, but the flag must win
        assert _detect_site("https://github.com/owner/repo", True) == "reddit"

    def test_url_match_overrides_html_fallback(self):
        """URL-based match takes precedence over HTML markers."""
        # GitHub URL + XenForo HTML markers → github wins
        html = '<html id="XF"><body></body></html>'
        assert _detect_site("https://github.com/owner/repo", False, _soup(html)) == "github"

    def test_discourse_detected_before_generic_forum(self):
        """Discourse gets its own key, not 'forum'."""
        html = '<html><head><meta name="generator" content="Discourse 3.2.0"></head><body></body></html>'
        assert _detect_site("https://community.example.com/", False, _soup(html)) == "discourse"

    def test_medium_html_before_forum_html(self):
        """Medium HTML detection runs before forum HTML detection."""
        # Page with both Medium and XenForo markers — Medium should win
        html = '<html id="XF"><body><button data-testid="headerSignUpButton">Sign up</button></body></html>'
        assert _detect_site("https://blog.example.com/", False, _soup(html)) == "medium"

    def test_known_site_url_skips_html_detection(self):
        """A known URL match means soup is never consulted for fallback."""
        # SO URL + Discourse HTML — SO wins
        html = '<html><head><meta name="generator" content="Discourse 3.2.0"></head><body></body></html>'
        assert _detect_site("https://stackoverflow.com/q/1", False, _soup(html)) == "stackoverflow"
