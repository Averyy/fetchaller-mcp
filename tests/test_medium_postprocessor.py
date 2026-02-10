"""Tests for Medium markdown post-processing — outcome-level tests."""

import pytest

from fetchaller.content.html import html_to_markdown


class TestMediumArticlePage:
    """A Medium article page should come out clean."""

    async def test_header_chrome_stripped(self):
        """Sign up/in buttons, Write, Search, Sitemap, Open in app are stripped."""
        html = """<body>
        <a href="/sitemap/sitemap.xml">Sitemap</a>
        <a href="https://play.google.com/store/apps/details?id=com.medium.reader">Open in app</a>
        <button data-testid="headerSignUpButton">Sign up</button>
        <button data-testid="headerSignInButton">Sign in</button>
        <a data-testid="headerWriteButton" href="/m/signin?operation=register&redirect=https%3A%2F%2Fmedium.com%2Fnew-story&source=---top_nav">Write</a>
        <input data-testid="headerSearchInput"/>
        <a href="/m/signin?operation=login&redirect=foo&source=login---top_nav">Sign in</a>
        <p>The actual article content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://medium.com/@user/article-slug")
        assert "Sitemap" not in md
        assert "Open in app" not in md
        assert "Sign up" not in md
        assert "Sign in" not in md
        assert "Write" not in md
        assert "actual article content" in md

    async def test_article_actions_stripped(self):
        """Clap separator, Listen, Share, and member-only label are stripped."""
        html = """<body>
        <p>Member-only story</p>
        <h1>My Article Title</h1>
        <p>--</p>
        <p>1</p>
        <p>Listen</p>
        <p>Share</p>
        <p>The real article text about Python programming.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://medium.com/@user/article-slug")
        assert "Member-only" not in md
        assert "Listen" not in md
        assert "Share" not in md
        assert "My Article Title" in md
        assert "Python programming" in md

    async def test_image_fullsize_prompt_stripped(self):
        """'Press enter or click to view image in full size' is stripped."""
        html = """<body>
        <p>Press enter or click to view image in full size</p>
        <img src="https://miro.medium.com/v2/resize:fit:1358/img.png" alt="Chart"/>
        <p>Analysis of the data shows interesting trends.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://medium.com/@user/article-slug")
        assert "Press enter" not in md
        assert "view image in full size" not in md
        assert "Analysis of the data" in md

    async def test_source_tracking_params_stripped(self):
        """?source=... tracking parameters are removed from URLs."""
        html = """<body>
        <a href="/tag/python?source=post_page-----abc123-------">Python</a>
        <a href="https://medium.com/pub?source=topic_portal---nav---python---">Publication</a>
        <p>Real content here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://medium.com/tag/python")
        assert "source=" not in md
        assert "Python" in md
        assert "Real content" in md


class TestMediumPostArticleBlocks:
    """Post-article blocks should be stripped."""

    async def test_published_in_block_stripped(self):
        """'Published in Publication' block after article is stripped."""
        html = """<body>
        <h1>Article Title</h1>
        <p>Article body content.</p>
        <h2><a href="https://python.plainenglish.io/?source=post_page">Published in Python in Plain English</a></h2>
        <p><a href="/followers?source=post_page">92K followers</a></p>
        <p>New Python content every day. Follow to join our 3.5M+ monthly readers.</p>
        <p>Follow</p>
        <h2><a href="https://medium.com/@author?source=post_page">Written by Author</a></h2>
        <p><a href="/followers?source=post_page">158 followers</a></p>
        <p>Biologist | Data Scientist</p>
        <h2>Responses (2)</h2>
        <p>See all responses</p>
        <a href="https://help.medium.com/hc/en-us?source=post_page">Help</a>
        <a href="https://status.medium.com/?source=post_page">Status</a>
        <a href="https://medium.com/about?source=post_page">About</a>
        <a href="mailto:pressinquiries@medium.com">Press</a>
        <a href="https://policy.medium.com/medium-terms-of-service?source=post_page">Terms</a>
        <a href="https://speechify.com/medium?source=post_page">Text to speech</a>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://medium.com/@user/article-slug")
        # Stripped
        assert "Published in" not in md
        assert "Written by" not in md
        assert "92K followers" not in md
        assert "158 followers" not in md
        assert "Responses (2)" not in md
        assert "See all responses" not in md
        assert "[Help]" not in md
        assert "[Status]" not in md
        assert "[Terms]" not in md
        assert "Text to speech" not in md
        # Preserved
        assert "Article Title" in md
        assert "Article body content" in md

    async def test_follow_publication_stripped(self):
        """'Follow publication' and standalone 'Follow' are stripped."""
        html = """<body>
        <p>Follow publication</p>
        <h1>Title</h1>
        <p>Content here.</p>
        <p>Follow</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://medium.com/@user/article-slug")
        assert "Follow publication" not in md
        assert "Follow" not in md
        assert "Title" in md
        assert "Content here" in md


class TestMediumTagPage:
    """Tag/listing pages should preserve article titles and metadata."""

    async def test_article_titles_preserved(self):
        """Article title headings in list views are not stripped."""
        html = """<body>
        <h2>Python</h2>
        <p>Topic · 4.5M followers</p>
        <a href="/@user/great-article-slug"><h2>A Great Article About Python</h2>
        <h3>Subtitle of the article</h3></a>
        <p>2d ago</p>
        <a href="/@author/another-article-slug"><h2>Another Good Post</h2></a>
        <p>3d ago</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://medium.com/tag/python")
        assert "Great Article About Python" in md
        assert "Another Good Post" in md
        assert "Python" in md


class TestMediumPublicationDomains:
    """Custom Medium publication domains should be detected."""

    async def test_plainenglish_detected(self):
        html = '<body><button data-testid="headerSignUpButton">Sign up</button><p>Content</p></body>'
        md, _ = await html_to_markdown(html, url="https://python.plainenglish.io/article")
        assert "Sign up" not in md
        assert "Content" in md

    async def test_levelup_detected(self):
        html = '<body><button data-testid="headerSignUpButton">Sign up</button><p>Content</p></body>'
        md, _ = await html_to_markdown(html, url="https://levelup.gitconnected.com/article")
        assert "Sign up" not in md

    async def test_uxdesign_detected(self):
        html = '<body><button data-testid="headerSignUpButton">Sign up</button><p>Content</p></body>'
        md, _ = await html_to_markdown(html, url="https://uxdesign.cc/article")
        assert "Sign up" not in md

    async def test_subdomain_detected(self):
        html = '<body><button data-testid="headerSignUpButton">Sign up</button><p>Content</p></body>'
        md, _ = await html_to_markdown(html, url="https://user.medium.com/article")
        assert "Sign up" not in md


class TestMediumIsolation:
    """Medium post-processing must not affect other sites."""

    async def test_non_medium_url_unaffected(self):
        html = """<body>
        <p>Sign up</p><p>Follow</p><p>Share</p><p>Listen</p>
        <p>Member-only story</p><p>Real content</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://example.com/page")
        assert "Sign up" in md
        assert "Follow" in md
        assert "Share" in md
        assert "Listen" in md
        assert "Real content" in md

    async def test_no_url_unaffected(self):
        html = "<body><p>Sign up</p><p>Follow</p></body>"
        md, _ = await html_to_markdown(html)
        assert "Sign up" in md
        assert "Follow" in md

    async def test_tds_not_matched(self):
        """Towards Data Science (no longer on Medium) should not be matched."""
        html = "<body><p>Sign up</p><p>Content</p></body>"
        md, _ = await html_to_markdown(html, url="https://towardsdatascience.com/article")
        assert "Sign up" in md
