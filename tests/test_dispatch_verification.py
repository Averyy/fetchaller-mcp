"""Verify that CSS selectors and postprocessors are dispatched for the correct sites.

Each test passes site-specific HTML through clean_html() or html_to_markdown()
and asserts that the correct site-specific cleanup was applied. If _detect_site()
breaks for any site, these tests fail — even though isolated postprocessor tests
still pass.

This catches the class of bug where detection returns the wrong key but the
per-module unit tests (which don't go through detection) still pass.
"""


from fetchaller.content.html import _html_to_markdown_sync, clean_html

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap(body_content: str, head: str = "") -> str:
    """Wrap body content in a full HTML document."""
    return f"<html><head>{head}</head><body>{body_content}</body></html>"


# ---------------------------------------------------------------------------
# CSS selector dispatch — clean_html() must remove site-specific elements
# ---------------------------------------------------------------------------


class TestSelectorDispatch:
    """For each site: pass HTML with a known CSS-targeted element, assert it's removed."""

    def test_reddit_sidebar_removed(self):
        html = _wrap('<div class="side">sidebar junk</div><div class="content">real post</div>')
        soup, site = clean_html(html, is_reddit=True)
        assert site == "reddit"
        assert "sidebar junk" not in soup.get_text()
        assert "real post" in soup.get_text()

    def test_hackernews_yclinks_removed(self):
        html = _wrap(
            '<span class="yclinks">hn nav links</span><p>story content</p>',
        )
        soup, site = clean_html(html, url="https://news.ycombinator.com/")
        assert site == "hackernews"
        assert "hn nav links" not in soup.get_text()
        assert "story content" in soup.get_text()

    def test_github_appheader_removed(self):
        html = _wrap(
            '<div class="AppHeader">github header junk</div><div class="markdown-body">readme content</div>',
        )
        soup, site = clean_html(html, url="https://github.com/owner/repo")
        assert site == "github"
        assert "github header junk" not in soup.get_text()
        assert "readme content" in soup.get_text()

    def test_huggingface_mainheader_removed(self):
        html = _wrap(
            '<div data-target="MainHeader">hf header junk</div><div>model card content</div>',
        )
        soup, site = clean_html(html, url="https://huggingface.co/org/model")
        assert site == "huggingface"
        assert "hf header junk" not in soup.get_text()
        assert "model card content" in soup.get_text()

    def test_redflagdeals_navbar_removed(self):
        html = _wrap(
            '<div class="main_nav_bar">rfd nav bar</div><div class="post">deal content</div>',
        )
        soup, site = clean_html(html, url="https://forums.redflagdeals.com/deal-123/")
        assert site == "redflagdeals"
        assert "rfd nav bar" not in soup.get_text()
        assert "deal content" in soup.get_text()

    def test_stackoverflow_sidebar_removed(self):
        html = _wrap(
            '<div id="sidebar">so sidebar junk</div><div id="question">question content</div>',
        )
        soup, site = clean_html(html, url="https://stackoverflow.com/questions/1")
        assert site == "stackoverflow"
        assert "so sidebar junk" not in soup.get_text()
        assert "question content" in soup.get_text()

    def test_medium_signup_button_removed(self):
        html = _wrap(
            '<button data-testid="headerSignUpButton">Sign up</button><article>article content</article>',
        )
        soup, site = clean_html(html, url="https://medium.com/@user/article")
        assert site == "medium"
        assert soup.find(attrs={"data-testid": "headerSignUpButton"}) is None
        assert "article content" in soup.get_text()

    def test_wikipedia_editsection_removed(self):
        html = _wrap(
            '<span class="mw-editsection">[edit]</span><p>encyclopedia content</p>',
        )
        soup, site = clean_html(html, url="https://en.wikipedia.org/wiki/Python")
        assert site == "wikipedia"
        assert soup.find(class_="mw-editsection") is None
        assert "encyclopedia content" in soup.get_text()

    def test_forum_xenforo_pnav_removed(self):
        """Unknown domain with XenForo HTML markers → forum site key, .p-nav removed."""
        html = '<html id="XF"><head></head><body><div class="p-nav">forum nav</div><p>thread content</p></body></html>'
        soup, site = clean_html(html, url="https://unknown-forum.example.com/threads/1")
        assert site == "forum"
        assert "forum nav" not in soup.get_text()
        assert "thread content" in soup.get_text()

    def test_generic_does_not_remove_site_specific_elements(self):
        """On a generic page, site-specific selectors must NOT be applied."""
        html = _wrap(
            '<div id="sidebar">sidebar content</div>'
            '<div class="AppHeader">header content</div>'
            '<span class="yclinks">yclinks content</span>'
            '<p>main content</p>',
        )
        soup, site = clean_html(html, url="https://example.com/page")
        assert site is None
        text = soup.get_text()
        # Generic junk selectors may remove .sidebar (it's in generic list)
        # but AppHeader and yclinks are site-specific and should be preserved
        assert "header content" in text
        assert "yclinks content" in text
        assert "main content" in text


# ---------------------------------------------------------------------------
# Postprocessor dispatch — html_to_markdown() must apply site-specific regex
# ---------------------------------------------------------------------------


class TestPostprocessorDispatch:
    """For each site with a postprocessor: pass HTML that produces a pattern
    only the correct postprocessor removes."""

    def test_github_issue_in_repo_removed(self):
        """GitHub postprocessor removes '#123 In org/repo;' patterns."""
        # This pattern appears in GitHub issue list pages after markdownify
        html = _wrap(
            '<p>Some issue title</p>'
            '<p>#123\xa0In facebook/react;</p>'
            '<p>Another issue</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://github.com/facebook/react/issues")
        assert "In facebook/react" not in md
        assert "Some issue title" in md

    def test_stackoverflow_answered_date_removed(self):
        """SO postprocessor removes 'answered Oct 23, 2008 at 22:21' lines."""
        html = _wrap(
            '<div id="answers">'
            '<p>The answer is 42.</p>'
            '<p>answered Oct 23, 2008 at 22:21</p>'
            '</div>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://stackoverflow.com/questions/1")
        assert "answered Oct" not in md
        assert "The answer is 42" in md

    def test_redflagdeals_back_to_menu_removed(self):
        """RFD postprocessor removes 'Back to Menu' lines."""
        html = _wrap(
            '<p>Back to Menu</p>'
            '<div class="post"><p>Great deal on monitors!</p></div>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://forums.redflagdeals.com/deal-123/")
        assert "Back to Menu" not in md
        assert "Great deal on monitors" in md

    def test_forum_xenforo_login_menu_removed(self):
        """Forum postprocessor removes XenForo 'Menu / Log in / Sign up' blocks."""
        html = """<html id="XF"><head></head><body>
<p>Menu</p>
<a href="/login/">Log in</a>
<hr>
<a href="/register/">Sign up</a>
<hr>
<p>Actual thread content here.</p>
</body></html>"""
        md, _ = _html_to_markdown_sync(html, url="https://unknown-forum.example.com/threads/1")
        assert "Log in" not in md
        assert "Sign up" not in md
        assert "Actual thread content" in md

    def test_generic_does_not_apply_stackoverflow_postprocessor(self):
        """On a generic page, SO-specific patterns must NOT be removed."""
        html = _wrap(
            '<p>The answer is 42.</p>'
            '<p>answered Oct 23, 2008 at 22:21</p>',
        )
        md, _ = _html_to_markdown_sync(html, url="https://example.com/page")
        # On a generic page, the SO postprocessor should NOT fire
        assert "answered Oct" in md

    def test_generic_does_not_apply_rfd_postprocessor(self):
        """On a generic page, RFD-specific 'Back to Menu' must NOT be removed."""
        html = _wrap('<p>Back to Menu</p><p>Other content</p>')
        md, _ = _html_to_markdown_sync(html, url="https://example.com/page")
        assert "Back to Menu" in md

    def test_generic_does_not_apply_github_postprocessor(self):
        """On a generic page, GitHub-specific '#123 In org/repo;' must NOT be removed."""
        html = _wrap('<p>#123\xa0In facebook/react;</p><p>Content</p>')
        md, _ = _html_to_markdown_sync(html, url="https://example.com/page")
        assert "In facebook/react" in md
