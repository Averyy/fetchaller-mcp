"""Tests for GitHub URL transformation and tree extraction."""

import json

from fetchaller.content.github import (
    extract_github_file_listing,
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
