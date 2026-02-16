"""Tests for GitHub URL transformation, tree extraction, and issue extraction."""

import json

from fetchaller.content.github import (
    extract_github_file_listing,
    extract_github_issue,
    transform_github_url,
)


def _make_tree_page(items, readme_html=None, variant="react-app"):
    """Build minimal HTML with embedded JSON matching GitHub's structure."""
    tree = {"items": items, "totalCount": len(items)}
    if readme_html:
        tree["readme"] = {"displayName": "README.md", "richText": readme_html}

    if variant == "react-app":
        payload = json.dumps({"payload": {"tree": tree}})
        return f'<html><script data-target="react-app.embeddedData" type="application/json">{payload}</script></html>'
    else:
        data = {"props": {"initialPayload": {"tree": tree}}, "_pad": "x" * 200}
        payload = json.dumps(data)
        return f'<html><script data-target="react-partial.embeddedData" type="application/json">{payload}</script></html>'


class TestTransformGithubUrl:
    def test_blob_to_raw(self):
        result = transform_github_url("https://github.com/owner/repo/blob/main/src/file.py")
        assert result.url == "https://raw.githubusercontent.com/owner/repo/main/src/file.py"
        assert result.is_github and result.is_blob

    def test_non_blob_urls_pass_through(self):
        """Tree, issues, profiles, and non-GitHub URLs are not transformed."""
        for url in [
            "https://github.com/owner/repo/tree/main/src",
            "https://github.com/owner/repo/issues/123",
            "https://github.com/torvalds",
            "https://example.com/owner/repo/blob/main/file.py",
        ]:
            result = transform_github_url(url)
            assert result.url == url
            assert result.is_blob is False


class TestExtractGithubFileListing:
    def test_tree_with_dirs_and_files(self):
        """Dirs come first with / suffix, then files. Heading uses URL path."""
        items = [
            {"name": "b_file.py", "contentType": "file"},
            {"name": "a_dir", "contentType": "directory"},
            {"name": "README.md", "contentType": "file"},
        ]
        html = _make_tree_page(items)
        result = extract_github_file_listing(html, "https://github.com/owner/repo/tree/main/src")
        assert "# /owner/repo/tree/main/src" in result
        assert "  a_dir/" in result
        assert "  b_file.py" in result
        # Dirs before files
        assert result.index("a_dir/") < result.index("b_file.py")

    def test_tree_with_readme(self):
        items = [{"name": "README.md", "contentType": "file"}]
        html = _make_tree_page(items, readme_html="<p>Hello world</p>")
        result = extract_github_file_listing(html, "https://github.com/owner/repo/tree/main/sub")
        assert "---" in result
        assert "Hello world" in result

    def test_react_partial_variant(self):
        """Repo root pages use react-partial.embeddedData."""
        items = [{"name": "src", "contentType": "directory"}]
        html = _make_tree_page(items, variant="react-partial")
        result = extract_github_file_listing(html, "https://github.com/owner/repo")
        assert result is not None and "src/" in result

    def test_missing_or_invalid_data_returns_none(self):
        assert extract_github_file_listing("<html><body>nothing</body></html>", "https://github.com/x") is None
        assert extract_github_file_listing(_make_tree_page([]), "https://github.com/x") is None
        assert extract_github_file_listing('<html><script data-target="react-app.embeddedData">bad json</script></html>', "https://github.com/x") is None

    def test_readme_camo_badges_stripped(self):
        badge_html = '<p><a href="https://example.com"><img src="https://camo.githubusercontent.com/abc/badge" alt="badge"/></a></p><p>Real content</p>'
        html = _make_tree_page([{"name": "README.md", "contentType": "file"}], readme_html=badge_html)
        result = extract_github_file_listing(html, "https://github.com/owner/repo/tree/main/")
        assert "camo.githubusercontent.com" not in result
        assert "Real content" in result


# ---------------------------------------------------------------------------
# Issue / PR extraction from embedded JSON
# ---------------------------------------------------------------------------


def _make_issue_page(
    issue_data: dict,
    query_name: str = "IssueViewerViewQuery",
    repo_key: str = "issue",
) -> str:
    """Build minimal HTML with embedded issue JSON matching GitHub's structure."""
    payload = {
        "payload": {
            "preloadedQueries": [
                {
                    "queryName": query_name,
                    "result": {
                        "data": {
                            "repository": {
                                repo_key: issue_data,
                            }
                        }
                    },
                }
            ]
        }
    }
    encoded = json.dumps(payload)
    return (
        '<html><script data-target="react-app.embeddedData" '
        f'type="application/json">{encoded}</script></html>'
    )


def _make_comment(author: str, date: str, body: str, **kwargs) -> dict:
    """Build a minimal IssueComment node."""
    node = {
        "__typename": "IssueComment",
        "author": {"login": author},
        "createdAt": date,
        "body": body,
    }
    node.update(kwargs)
    return node


class TestIssueExtraction:
    def test_basic_issue_with_comments(self):
        """Extracts title, body, state, labels, author, and comments."""
        issue = {
            "title": "Fix the widget",
            "number": 42,
            "state": "OPEN",
            "stateReason": None,
            "author": {"login": "alice"},
            "createdAt": "2024-01-27T08:17:05.000Z",
            "body": "The widget is broken.\n\nSteps to reproduce:\n1. Click it",
            "labels": {
                "edges": [
                    {"node": {"name": "bug"}},
                    {"node": {"name": "urgent"}},
                ]
            },
            "frontTimelineItems": {
                "edges": [
                    {"node": _make_comment("bob", "2024-01-28T10:00:00Z", "I can confirm this.")},
                    {"node": {"__typename": "LabeledEvent", "actor": {"login": "alice"}}},
                    {"node": _make_comment("carol", "2024-01-29T12:00:00Z", "Fixed in #43.")},
                ]
            },
            "backTimelineItems": {"edges": []},
        }
        html = _make_issue_page(issue)
        result = extract_github_issue(html, "https://github.com/owner/repo/issues/42")

        assert result is not None
        assert "# Fix the widget #42" in result
        assert "**alice** opened on Jan 27, 2024" in result
        assert "Open" in result
        assert "Labels: bug, urgent" in result
        assert "The widget is broken." in result
        assert "Steps to reproduce:" in result
        # Comments
        assert "**bob** on Jan 28, 2024:" in result
        assert "I can confirm this." in result
        assert "**carol** on Jan 29, 2024:" in result
        assert "Fixed in #43." in result
        # Non-comment events excluded
        assert "LabeledEvent" not in result

    def test_issue_no_comments(self):
        """Issue with no comments still outputs title and body."""
        issue = {
            "title": "Feature request",
            "number": 100,
            "state": "OPEN",
            "author": {"login": "dev"},
            "createdAt": "2024-06-15T00:00:00Z",
            "body": "Please add dark mode.",
            "labels": {"edges": []},
            "frontTimelineItems": {"edges": []},
            "backTimelineItems": {"edges": []},
        }
        html = _make_issue_page(issue)
        result = extract_github_issue(html, "https://github.com/owner/repo/issues/100")

        assert "# Feature request #100" in result
        assert "Please add dark mode." in result
        assert "---" not in result  # No comment separators

    def test_closed_issue_state(self):
        """Closed issues show state correctly, including 'not planned'."""
        for state, reason, expected in [
            ("CLOSED", "COMPLETED", "Closed"),
            ("CLOSED", "NOT_PLANNED", "Closed as not planned"),
            ("OPEN", None, "Open"),
        ]:
            issue = {
                "title": "Test",
                "number": 1,
                "state": state,
                "stateReason": reason,
                "author": {"login": "u"},
                "createdAt": "2024-01-01T00:00:00Z",
                "body": "",
                "labels": {"edges": []},
                "frontTimelineItems": {"edges": []},
                "backTimelineItems": {"edges": []},
            }
            result = extract_github_issue(_make_issue_page(issue), "https://github.com/x")
            assert expected in result

    def test_pr_extraction(self):
        """PR pages use PullRequestViewerViewQuery and pullRequest key."""
        pr = {
            "title": "Add tests",
            "number": 55,
            "state": "MERGED",
            "author": {"login": "dev"},
            "createdAt": "2024-03-01T00:00:00Z",
            "body": "This PR adds unit tests.",
            "labels": {"edges": [{"node": {"name": "enhancement"}}]},
            "frontTimelineItems": {
                "edges": [
                    {"node": _make_comment("reviewer", "2024-03-02T00:00:00Z", "LGTM!")},
                ]
            },
            "backTimelineItems": {"edges": []},
        }
        html = _make_issue_page(pr, query_name="PullRequestViewerViewQuery", repo_key="pullRequest")
        result = extract_github_issue(html, "https://github.com/owner/repo/pull/55")

        assert "# Add tests #55" in result
        assert "Merged" in result
        assert "Labels: enhancement" in result
        assert "**reviewer** on Mar 2, 2024:" in result
        assert "LGTM!" in result

    def test_non_issue_page_returns_none(self):
        """Repo pages, org pages, search — no matching query → returns None."""
        # Tree page (has payload.tree, not preloadedQueries)
        tree_html = _make_tree_page([{"name": "src", "contentType": "directory"}])
        assert extract_github_issue(tree_html, "https://github.com/owner/repo") is None
        # Plain HTML
        assert extract_github_issue("<html><body>Hello</body></html>", "https://github.com/owner") is None
        # Empty preloadedQueries
        payload = json.dumps({"payload": {"preloadedQueries": []}})
        html = f'<html><script data-target="react-app.embeddedData" type="application/json">{payload}</script></html>'
        assert extract_github_issue(html, "https://github.com/x") is None

    def test_malformed_json_returns_none(self):
        """Bad JSON in the script tag returns None gracefully."""
        html = '<html><script data-target="react-app.embeddedData" type="application/json">{bad json</script></html>'
        assert extract_github_issue(html, "https://github.com/x") is None

    def test_comment_ordering_preserved(self):
        """Comments from front + back timeline are in order."""
        issue = {
            "title": "Test",
            "number": 1,
            "state": "OPEN",
            "author": {"login": "u"},
            "createdAt": "2024-01-01T00:00:00Z",
            "body": "Body",
            "labels": {"edges": []},
            "frontTimelineItems": {
                "edges": [
                    {"node": _make_comment("first", "2024-01-02T00:00:00Z", "Comment 1")},
                    {"node": _make_comment("second", "2024-01-03T00:00:00Z", "Comment 2")},
                ]
            },
            "backTimelineItems": {
                "edges": [
                    {"node": _make_comment("third", "2024-01-04T00:00:00Z", "Comment 3")},
                ]
            },
        }
        result = extract_github_issue(_make_issue_page(issue), "https://github.com/x")
        assert result.index("Comment 1") < result.index("Comment 2") < result.index("Comment 3")

    def test_crlf_normalization(self):
        """\\r\\n in body is normalized to \\n."""
        issue = {
            "title": "Test",
            "number": 1,
            "state": "OPEN",
            "author": {"login": "u"},
            "createdAt": "2024-01-01T00:00:00Z",
            "body": "Line 1\r\nLine 2\r\nLine 3",
            "labels": {"edges": []},
            "frontTimelineItems": {"edges": []},
            "backTimelineItems": {"edges": []},
        }
        result = extract_github_issue(_make_issue_page(issue), "https://github.com/x")
        assert "\r\n" not in result
        assert "Line 1\nLine 2\nLine 3" in result

    def test_hidden_comments_excluded(self):
        """Hidden/minimized comments are skipped."""
        issue = {
            "title": "Test",
            "number": 1,
            "state": "OPEN",
            "author": {"login": "u"},
            "createdAt": "2024-01-01T00:00:00Z",
            "body": "Body",
            "labels": {"edges": []},
            "frontTimelineItems": {
                "edges": [
                    {"node": _make_comment("visible", "2024-01-02T00:00:00Z", "Good comment")},
                    {"node": _make_comment("hidden", "2024-01-03T00:00:00Z", "Spam", isHidden=True)},
                ]
            },
            "backTimelineItems": {"edges": []},
        }
        result = extract_github_issue(_make_issue_page(issue), "https://github.com/x")
        assert "Good comment" in result
        assert "Spam" not in result
