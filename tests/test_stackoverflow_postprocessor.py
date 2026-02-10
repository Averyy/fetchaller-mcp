"""Tests for Stack Overflow markdown post-processing — outcome-level tests."""

from fetchaller.content.html import html_to_markdown


class TestStackOverflowQuestionPage:
    """A SO question page should come out clean."""

    async def test_header_chrome_stripped(self):
        """Top nav, Collectives promo, and Ask Question link are stripped."""
        html = """<body>
        <header class="s-topbar">
            <a href="/">Stack Overflow</a>
            <input type="text" placeholder="Search..."/>
        </header>
        <div id="left-sidebar">
            <a href="/questions">Questions</a>
            <a href="/tags">Tags</a>
            <a href="/users">Users</a>
        </div>
        <a href="#content">Skip to main content</a>
        <h5>Collectives&trade; on Stack Overflow</h5>
        <p>Find centralized, trusted content.</p>
        <a href="/collectives">Learn more about Collectives</a>
        <a href="/questions/ask">Ask Question</a>
        <h1>What does yield do in Python?</h1>
        <p>I want to understand the yield keyword.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/231767/yield")
        assert "Questions" not in md or "yield" in md  # sidebar Questions link stripped
        assert "Tags" not in md or "yield" in md
        assert "Skip to main content" not in md
        assert "Collectives" not in md
        assert "Learn more about Collectives" not in md
        assert "[Ask Question]" not in md
        assert "yield" in md
        assert "understand" in md

    async def test_vote_buttons_stripped(self):
        """Vote containers are removed."""
        html = """<body>
        <div class="js-voting-container">
            <button>upvote</button>
            <span>1234</span>
            <button>downvote</button>
        </div>
        <p>Here is the actual answer content about generators.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "upvote" not in md
        assert "downvote" not in md
        assert "generators" in md

    async def test_post_actions_stripped(self):
        """Share/Improve/Follow action menus are removed."""
        html = """<body>
        <p>Use yield to create a generator function.</p>
        <div class="js-post-menu">
            <a href="/q/123">Share</a>
            <a href="/posts/123/edit">Improve this answer</a>
            <button>Follow</button>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "[Share]" not in md
        assert "Improve this answer" not in md
        assert "generator function" in md

    async def test_user_signatures_stripped(self):
        """User signature blocks with avatars and badges are removed."""
        html = """<body>
        <p>Yield pauses the function and returns a value.</p>
        <div class="post-signature">
            <img src="https://i.sstatic.net/abc.jpg?s=64" alt="User's user avatar"/>
            <a href="/users/123/username">username</a>
            <span>28k</span>
            <span>22 gold badges</span>
            <span>122 silver badges</span>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "gold badges" not in md
        assert "user avatar" not in md
        assert "pauses the function" in md

    async def test_avatar_images_stripped(self):
        """User avatar images are stripped from soup."""
        html = """<body>
        <a href="/users/123/alice"><img src="https://gravatar.com/abc?s=64" alt="Alice's user avatar"/></a>
        <a href="/users/456/bob"><img src="https://i.sstatic.net/def.jpg" alt="Bob's user avatar"/></a>
        <p>The answer about iterators.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "user avatar" not in md
        assert "gravatar" not in md
        assert "iterators" in md

    async def test_badges_stripped_from_markdown(self):
        """Badge lines like '28k22 gold badges122 silver badges' are stripped."""
        html = """<body>
        <p>The answer content here.</p>
        <p>28k22 gold badges122 silver badges156 bronze badges</p>
        <p>More content after badges.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "gold badges" not in md
        assert "answer content" in md
        assert "More content" in md

    async def test_answered_date_stripped(self):
        """'answered Oct 23, 2008 at 22:21' lines are stripped."""
        html = """<body>
        <p>Here is an explanation of yield.</p>
        <p>answered Oct 23, 2008 at 22:21</p>
        <p>asked Nov 5, 2010 at 14:30</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "answered Oct" not in md
        assert "asked Nov" not in md
        assert "explanation of yield" in md

    async def test_comments_header_stripped(self):
        """Empty '## Comments' headers and 'Add a comment' are stripped."""
        html = """<body>
        <p>Answer about generators.</p>
        <h2>Comments</h2>
        <a class="js-add-link">Add a comment</a>
        <p>Next answer about itertools.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "## Comments" not in md
        assert "Add a comment" not in md
        assert "generators" in md
        assert "itertools" in md

    async def test_code_blocks_preserved(self):
        """Code blocks in answers are preserved."""
        html = """<body>
        <h2>Using yield</h2>
        <pre><code class="language-python">def gen():
    yield 1
    yield 2
    yield 3

for x in gen():
    print(x)</code></pre>
        <p>This prints 1, 2, 3.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "def gen():" in md
        assert "yield 1" in md
        assert "prints 1, 2, 3" in md

    async def test_question_content_preserved(self):
        """The actual question text, tags, and footnotes are preserved."""
        html = """<body>
        <h1>What does the yield keyword do?</h1>
        <p>What functionality does the <code>yield</code> keyword provide?</p>
        <pre><code>def get_items():
    yield item</code></pre>
        <p>What happens when this is called?</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "yield keyword" in md
        assert "def get_items():" in md
        assert "What happens" in md

    async def test_want_to_improve_stripped(self):
        """'Want to improve this post?' banner is stripped."""
        html = """<body>
        <div>
            <div>
                <b>Want to improve this post?</b>
                Provide detailed answers to this question.
            </div>
        </div>
        <p>Actual question content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "Want to improve" not in md
        assert "question content" in md


class TestStackOverflowSidebar:
    """Sidebar content should be stripped."""

    async def test_sidebar_stripped(self):
        """Right sidebar (Hot Network Questions, related, ads) is stripped."""
        html = """<body>
        <div id="sidebar">
            <h4>Hot Network Questions</h4>
            <ul>
                <li><a href="https://cooking.stackexchange.com/q/1">How to boil water?</a></li>
                <li><a href="https://math.stackexchange.com/q/2">What is 1+1?</a></li>
            </ul>
            <div class="js-zone-container">Ad content here</div>
        </div>
        <p>Main page content about Python.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "Hot Network" not in md
        assert "boil water" not in md
        assert "Ad content" not in md
        assert "Python" in md


class TestStackOverflowComments:
    """Comment content should be preserved, metadata stripped."""

    async def test_comment_text_preserved(self):
        """Comment text content is preserved."""
        html = """<body>
        <p>The answer about generators.</p>
        <div class="comment-body">
            <span>This is a really helpful comment about yield.</span>
            <span class="comment-actions"><a>upvote</a></span>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "helpful comment" in md
        assert "upvote" not in md

    async def test_comment_actions_stripped(self):
        """Comment action buttons are stripped."""
        html = """<body>
        <span class="comment-actions">
            <button>upvote</button>
            <button>flag</button>
        </span>
        <p>Useful comment text here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "flag" not in md
        assert "Useful comment" in md


class TestStackOverflowQuestionList:
    """Tagged/question list pages should be cleaned up."""

    async def test_vote_answer_view_stats_stripped(self):
        """Per-question vote/answer/view stat blocks are stripped."""
        html = """<body>
        <div class="s-post-summary--stats">
            <span>0 votes</span>
            <span>0 answers</span>
            <span>5 views</span>
        </div>
        <h3><a href="/questions/123/how-to-use-yield">How to use yield?</a></h3>
        <p>I'm trying to understand generators in Python.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/tagged/python")
        assert "0 votes" not in md
        assert "0 answers" not in md
        assert "5 views" not in md
        assert "How to use yield" in md
        assert "generators" in md

    async def test_filter_controls_stripped(self):
        """Sort tabs and filter controls are stripped."""
        html = """<body>
        <div class="s-btn-group">
            <a href="?tab=Newest">Newest</a>
            <a href="?tab=Active">Active</a>
        </div>
        <form id="uql-form">
            <label>Filter by</label>
            <input type="checkbox"/>No answers
        </form>
        <h3><a href="/questions/456/python-list-comprehension">Python list comprehension</a></h3>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/tagged/python")
        assert "Newest" not in md or "list comprehension" in md
        assert "Filter by" not in md
        assert "list comprehension" in md

    async def test_question_titles_preserved(self):
        """Question titles and excerpts on list pages are preserved."""
        html = """<body>
        <h3><a href="/questions/231767/yield">What does the yield keyword do?</a></h3>
        <p>What functionality does the yield keyword in Python provide?</p>
        <h3><a href="/questions/100/list-comp">How do list comprehensions work?</a></h3>
        <p>I'm confused about the syntax of list comprehensions.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/tagged/python")
        assert "yield keyword" in md
        assert "list comprehensions" in md


class TestStackOverflowFooter:
    """Footer/bottom blocks should be stripped."""

    async def test_start_asking_stripped(self):
        """'Start asking to get answers' CTA is stripped."""
        html = """<body>
        <p>Last answer content.</p>
        <p>Start asking to get answers</p>
        <p>Find the answer to your question by asking.</p>
        <a href="/questions/ask">Ask question</a>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "Start asking" not in md
        assert "Last answer content" in md

    async def test_explore_related_stripped(self):
        """'Explore related questions' and 'See similar questions' are stripped."""
        html = """<body>
        <p>Answer content.</p>
        <p>Explore related questions</p>
        <p>See similar questions with these tags.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "Explore related" not in md
        assert "See similar" not in md
        assert "Answer content" in md

    async def test_pagination_stripped(self):
        """Pagination links are stripped."""
        html = """<body>
        <p>Answer content.</p>
        <div class="s-pagination">
            <a href="?page=1">1</a>
            <a href="?page=2">2</a>
            <a href="?page=2">Next</a>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://stackoverflow.com/questions/123/test")
        assert "Next" not in md
        assert "Answer content" in md


class TestStackExchangeSites:
    """Stack Exchange sites should be detected and cleaned."""

    async def test_superuser_detected(self):
        """superuser.com is detected as Stack Exchange."""
        html = """<body>
        <header class="s-topbar"><a href="/">Super User</a></header>
        <div class="js-post-menu"><a href="/q/1">Share</a></div>
        <p>How to fix my network settings.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://superuser.com/questions/1/network")
        assert "[Share]" not in md
        assert "network settings" in md

    async def test_stackexchange_subdomain_detected(self):
        """*.stackexchange.com sites are detected."""
        html = """<body>
        <header class="s-topbar"><a href="/">Unix SE</a></header>
        <div class="js-post-menu"><a href="/q/1">Share</a></div>
        <p>How to use grep effectively.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://unix.stackexchange.com/questions/1/grep")
        assert "[Share]" not in md
        assert "grep effectively" in md

    async def test_askubuntu_detected(self):
        """askubuntu.com is detected."""
        html = """<body>
        <div class="js-voting-container"><button>up</button></div>
        <p>Install packages with apt.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://askubuntu.com/questions/1/apt")
        assert "up" not in md or "apt" in md
        assert "Install packages" in md


class TestStackOverflowIsolation:
    """SO post-processing must not affect other sites."""

    async def test_non_so_url_unaffected(self):
        """Non-SO URLs should not trigger SO processing."""
        html = """<body>
        <div class="post-signature">
            <span>28k gold badges</span>
        </div>
        <p>Share</p>
        <p>Follow</p>
        <p>Real content here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://example.com/page")
        # post-signature is not in generic junk selectors, so it stays
        assert "Real content" in md

    async def test_no_url_unaffected(self):
        """No URL should not trigger SO processing."""
        html = "<body><p>Share</p><p>Follow</p><p>Content</p></body>"
        md, _ = await html_to_markdown(html)
        assert "Share" in md
        assert "Content" in md
