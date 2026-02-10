"""Tests for GitHub markdown post-processing — outcome-level tests."""


from fetchaller.content.html import html_to_markdown


class TestGitHubIssuePage:
    """A GitHub issue page should come out clean."""

    async def test_issue_list_cleanup(self):
        """Issue list metadata (repo line, author links, labels) is cleaned up."""
        html = """<body>
        <p>Issue list header</p>
        <p>#35729\xa0In facebook/react;</p>
        <p>\xb7\xa0<a href="/issues?q=author%3Auser1">user1</a> opened on Feb 9, 2026</p>
        <a href="/repo/issues?q=label%3A%22bug%22">bug</a>
        <a href="/repo/issues?q=label%3A%22urgent%22">urgent</a>
        <p>Real issue content here</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/facebook/react/issues")
        # Cruft stripped
        assert "In facebook/react" not in md
        assert "author%3A" not in md
        assert "label%3A" not in md
        # Useful content preserved
        assert "user1 opened on Feb 9, 2026" in md
        assert "bug" in md
        assert "urgent" in md
        assert "Real issue content" in md

    async def test_single_issue_cleanup(self):
        """Single issue page: empty metadata stripped, title anchor removed."""
        html = """<body>
        <p>Header</p>
        <p><a href="#top">Fix the bug</a>#123</p>
        <h3>Assignees</h3><p>No one assigned</p>
        <h3>Projects</h3><p>No projects</p>
        <h3>Milestone</h3><p>No milestone</p>
        <p>The actual bug description goes here.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/owner/repo/issues/123")
        assert "(#top)" not in md
        assert "No one assigned" not in md
        assert "No projects" not in md
        assert "No milestone" not in md
        assert "bug description" in md

    async def test_inline_labels_comma_separated(self):
        """Adjacent label links on one line become comma-separated text."""
        html = """<body><p>
        <a href="/r/issues?q=label%3A%22bug%22">bug</a><a href="/r/issues?q=label%3A%22urgent%22">urgent</a><a href="/r/issues?q=label%3A%22confirmed%22">confirmed</a>
        </p></body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/owner/repo/issues")
        assert "bug, urgent, confirmed" in md

    async def test_issue_list_nav_stripped(self):
        """[Labels](...)[Milestones](...) nav links at top are stripped."""
        html = """<body>
        <a href="/facebook/react/labels">Labels</a><a href="/facebook/react/milestones">Milestones</a>
        <h3><a href="/facebook/react/issues/123">Bug title</a></h3>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/facebook/react/issues")
        assert "[Labels]" not in md
        assert "[Milestones]" not in md
        assert "Bug title" in md


class TestGitHubRepoPage:
    """A GitHub repo page should preserve useful info and strip chrome."""

    async def test_repo_page_cleanup(self):
        """Stars/forks preserved, nav links and badges stripped."""
        html = """<body>
        <a href="#start-of-content">Skip to content</a>
        <p>You must be signed in to change notification settings</p>
        <a href="/repo/stargazers">106k\nstars</a>
        <a href="/repo/forks">5.9k\nforks</a>
        <a href="/repo/branches">Branches</a>
        <a href="/repo/tags">Tags</a>
        <a href="/repo/activity">Activity</a>
        <p>Header</p>
        <p>Go to file</p>
        <a href="https://example.com"><img src="https://camo.githubusercontent.com/abc/badge.svg" alt="badge"/></a>
        <img src="https://camo.githubusercontent.com/def/other.svg" alt="other"/>
        <p>A fast modern JavaScript runtime.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/owner/repo")
        # Preserved
        assert "106k stars" in md
        assert "5.9k forks" in md
        assert "fast modern JavaScript" in md
        # Stripped
        assert "Skip to content" not in md
        assert "notification settings" not in md
        assert "[Branches]" not in md
        assert "Go to file" not in md
        assert "camo.githubusercontent.com" not in md

    async def test_org_page_commit_activity_stripped(self):
        html = "<body><p>Header</p><p>org/repo\u2019s past year of commit activity</p><p>Content</p></body>"
        md, _ = await html_to_markdown(html, url="https://github.com/org")
        assert "past year of commit activity" not in md
        assert "Content" in md

    async def test_org_page_sponsor_link_stripped(self):
        """[Sponsor](/sponsors) nav link is stripped from org pages."""
        html = """<body>
        <p>Develop. Preview. Ship.</p>
        <ul><li><a href="/sponsors">Sponsor</a></li>
        <li>25.9k followers</li></ul>
        <p>Real content</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/vercel")
        assert "[Sponsor]" not in md
        assert "followers" in md
        assert "Real content" in md


class TestGitHubReleasePage:
    """Release pages should show just version, date, author, and changelog."""

    async def test_release_page_cleanup(self):
        """Release chrome (compare dropdown, assets, reactions) is stripped."""
        html = """<body>
        <p>Header</p>
        <h2>v15.5.12</h2>
        <select-panel>
            <p>Compare</p>
            <h1>Choose a tag to compare</h1>
            <p>Filter</p>
            <a href="/owner/repo/tags">View all tags</a>
        </select-panel>
        <a href="/owner/repo/releases/tag/v15.5.12">v15.5.12</a>
        <p>Pre-release</p><p>Pre-release</p>
        <h3>Core Changes</h3>
        <p>Fixed a critical bug</p>
        <details-toggle>
            <p>Assets</p><p>2</p><p>Loading</p>
        </details-toggle>
        <div class="comment-reactions">
            <p>🎉 1 user reacted with hooray emoji</p>
        </div>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/owner/repo/releases/tag/v15.5.12")
        # Preserved
        assert "v15.5.12" in md
        assert "Core Changes" in md
        assert "critical bug" in md
        # Stripped
        assert "Compare" not in md
        assert "Choose a tag" not in md
        assert "Assets" not in md
        assert "Loading" not in md
        assert "reacted" not in md


class TestGitHubPRPage:
    """PR pages should show title, status, conversation — no duplicate headers."""

    async def test_pr_sticky_header_stripped(self):
        """The sticky duplicate header is removed, keeping just the main one."""
        html = """<body>
        <p>Header</p>
        <h1>Fix the bug #123</h1>
        <p>Merged</p>
        <p>author wants to merge 3 commits into main</p>
        <div class="StickyPullRequestHeader-module__prHeader__abc123">
            <h2>Fix the bug #123</h2>
            <p>Merged</p>
            <p>author wants to merge 3 commits into main</p>
        </div>
        <h2>Conversation</h2>
        <p>Great work on this PR!</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/owner/repo/pull/123")
        assert "Fix the bug" in md
        assert "Conversation" in md
        assert "Great work" in md
        # Title should appear only once
        assert md.count("Fix the bug") == 1

    async def test_pr_empty_sidebar_stripped(self):
        """Empty sidebar metadata (Reviewers: No reviews, etc.) is stripped."""
        html = """<body>
        <p>Header</p>
        <h3>Reviewers</h3><p>No reviews</p>
        <h3>Labels</h3><p>None yet</p>
        <h3>Development</h3><p>Successfully merging this pull request may close these issues.</p>
        <h3>4 participants</h3>
        <p>The actual PR content.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/owner/repo/pull/456")
        assert "No reviews" not in md
        assert "None yet" not in md
        assert "Successfully merging" not in md
        assert "participants" not in md
        assert "PR content" in md


class TestGitHubDiscussionsPage:
    """Discussions list should show titles/authors, not filter UI."""

    async def test_discussions_filter_stripped(self):
        html = """<body>
        <p>Header</p>
        <query-builder>
            <p>Search all discussions</p>
            <input placeholder="Filter"/>
        </query-builder>
        <p>Filter: Open</p>
        <ul>
            <li><a href="/repo/discussions?discussions_q=is%3Aclosed">Closed</a></li>
            <li><a href="/repo/discussions?discussions_q=is%3Aopen+is%3Alocked">Locked</a></li>
        </ul>
        <p>You must be logged in to vote</p>
        <h3><a href="/repo/discussions/100">How do I configure X?</a></h3>
        <p>user1 started Feb 9, 2026</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/vercel/next.js/discussions")
        # Stripped
        assert "Search all discussions" not in md
        assert "Filter: Open" not in md
        assert "logged in to vote" not in md
        # Preserved
        assert "How do I configure X?" in md
        assert "user1" in md

    async def test_discussions_category_links_cleaned(self):
        """Category filter URLs become plain text, reply counts stripped."""
        html = """<body>
        <h3><a href="/repo/discussions/100">How do I configure X?</a></h3>
        <p>user1 started Feb 9, 2026 in
        <a href="/repo/discussions/categories/ideas?discussions_q=is%3Aopen+category%3AIdeas">Ideas</a></p>
        <a href="/repo/discussions/100">5</a>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/vercel/next.js/discussions")
        # Category as plain text, not link
        assert "Ideas" in md
        assert "category%3A" not in md
        assert "discussions_q=" not in md
        # Reply count link stripped
        assert "[5]" not in md


class TestGitHubSearchResults:
    """Search results should show repos/results, not the filter sidebar."""

    async def test_search_sidebar_stripped(self):
        html = """<body>
        <p>Header</p>
        <ul>
            <li><ul>
                <li><a href="https://github.com/search?q=foo&type=code">Code (123)results</a></li>
                <li><a href="https://github.com/search?q=foo&type=repositories">Repositoriesresults</a></li>
            </ul></li>
            <li><h3>Languages</h3>
                <ul>
                    <li><a href="https://github.com/search?q=foo+language%3APython&type=repositories">Python</a></li>
                </ul>
            </li>
            <li><a href="/search/advanced">Advanced search</a></li>
        </ul>
        <h3><a href="/owner/repo">owner/repo</a></h3>
        <p>A great library for doing things.</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/search?q=foo&type=repositories")
        # Stripped
        assert "language%3A" not in md
        assert "Advanced search" not in md
        # Preserved
        assert "owner/repo" in md
        assert "great library" in md

    async def test_search_count_header_stripped(self):
        """Search count header with non-breaking spaces is stripped."""
        html = """<body>
        <h1>repositories Search Results</h1>
        <p>Repositories\xa0(250)</p>
        <p>250 results</p>
        <p>\xa0(218 ms)</p>
        <h2>250 results</h2>
        <p>Sort by: Best match</p>
        <h3><a href="/owner/repo">owner/repo</a></h3>
        <p>A great library</p>
        </body>"""
        md, _ = await html_to_markdown(html, url="https://github.com/search?q=foo&type=repositories")
        assert "250 results" not in md
        assert "(218 ms)" not in md
        assert "Sort by" not in md
        assert "owner/repo" in md
        assert "great library" in md


class TestGitHubIsolation:
    """GitHub post-processing must not affect other sites."""

    async def test_non_github_url_unaffected(self):
        html = "<body><p>Go to file</p><p>Skip to content</p><p>Search Issues</p></body>"
        md, _ = await html_to_markdown(html, url="https://example.com/page")
        assert "Go to file" in md
        assert "Search Issues" in md

    async def test_no_url_unaffected(self):
        html = "<body><p>Skip to content</p></body>"
        md, _ = await html_to_markdown(html)
        assert "Skip to content" in md
