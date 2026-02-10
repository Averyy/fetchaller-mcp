"""GitHub-specific HTML cleanup, URL transforms, and file tree extraction.

Exports the standard site interface (SELECTORS_LIST, is_github, strip_github_junk,
postprocess_github) plus GitHub-specific helpers (transform_github_url,
extract_github_file_listing).
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

# ---------------------------------------------------------------------------
# URL detection & transformation
# ---------------------------------------------------------------------------

# Match /owner/repo/blob/ref/path — captures owner, repo, ref, path
_BLOB_RE = re.compile(r"^/([^/]+)/([^/]+)/blob/(.+?)/(.+)")


def is_github(url: str) -> bool:
    """Check if URL is a GitHub page."""
    hostname = urlparse(url).hostname or ""
    return hostname in ("github.com", "www.github.com")


@dataclass
class GitHubTransformResult:
    """Result of GitHub URL transformation."""

    url: str
    is_github: bool
    is_blob: bool  # True if this was a blob URL transformed to raw


def transform_github_url(url: str) -> GitHubTransformResult:
    """
    Transform GitHub blob URLs to raw.githubusercontent.com for direct file access.

    Transforms:
    - github.com/{owner}/{repo}/blob/{ref}/{path}
      -> raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}

    Other GitHub URLs are left unchanged.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return GitHubTransformResult(url=url, is_github=False, is_blob=False)

    hostname = (parsed.hostname or "").lower()
    if hostname not in ("github.com", "www.github.com"):
        return GitHubTransformResult(url=url, is_github=False, is_blob=False)

    # Transform blob URLs to raw
    match = _BLOB_RE.match(parsed.path)
    if match:
        owner, repo, ref, path = match.groups()
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        return GitHubTransformResult(url=raw_url, is_github=True, is_blob=True)

    return GitHubTransformResult(url=url, is_github=True, is_blob=False)


# ---------------------------------------------------------------------------
# File tree extraction from embedded JSON
# ---------------------------------------------------------------------------

# Camo badge images in README (shield.io badges proxied through GitHub)
_CAMO_LINKED_RE = re.compile(
    r"\[!\[[^\]]*\]\(https?://camo\.githubusercontent\.com/[^\)]+\)\]\([^\)]+\)"
)
_CAMO_STANDALONE_RE = re.compile(
    r"!\[[^\]]*\]\(https?://camo\.githubusercontent\.com/[^\)]+\)"
)


def _find_github_tree(soup: BeautifulSoup) -> dict | None:
    """
    Find the tree data from GitHub's embedded JSON.

    GitHub uses two different data structures:
    - Tree/blob subpages: react-app.embeddedData -> payload.tree
    - Repo root pages: react-partial.embeddedData -> props.initialPayload.tree
    """
    # Try react-app first (tree subpages)
    el = soup.select_one('[data-target="react-app.embeddedData"]')
    if el and el.string:
        try:
            data = json.loads(el.string)
            tree = data.get("payload", {}).get("tree")
            if tree and tree.get("items"):
                return tree
        except (json.JSONDecodeError, TypeError):
            pass

    # Try react-partial (repo root pages)
    for el in soup.select('[data-target="react-partial.embeddedData"]'):
        text = el.string or ""
        if len(text) < 200:
            continue
        try:
            data = json.loads(text)
            tree = data.get("props", {}).get("initialPayload", {}).get("tree")
            if tree and tree.get("items"):
                return tree
        except (json.JSONDecodeError, TypeError):
            continue

    return None


def _format_readme_from_json(readme: dict) -> str | None:
    """Convert GitHub's embedded README richText HTML to clean markdown."""
    rich_text = readme.get("richText", "")
    if not rich_text:
        return None

    md = markdownify(
        rich_text,
        heading_style="ATX",
        bullets="-",
        escape_asterisks=False,
        escape_underscores=False,
    ).strip()

    # Strip camo badge images
    md = _CAMO_LINKED_RE.sub("", md)
    md = _CAMO_STANDALONE_RE.sub("", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    return md if md else None


def extract_github_file_listing(html: str, url: str) -> str | None:
    """
    Extract file listing from a GitHub page's embedded JSON.

    Returns ONLY the directory listing (no README). The caller is responsible
    for appending README content from whatever source is appropriate.

    Args:
        html: Raw HTML of the GitHub page
        url: Page URL (used for heading)

    Returns:
        Formatted file listing string, or None if no tree found
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        tree = _find_github_tree(soup)
        if not tree:
            return None

        items = tree.get("items", [])
        if not items:
            return None

        # Build path from URL
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")

        # Format directory listing
        dirs = []
        files = []
        for item in items:
            name = item.get("name", "")
            content_type = item.get("contentType", "file")
            if content_type == "directory":
                dirs.append(f"  {name}/")
            else:
                files.append(f"  {name}")

        lines = [f"# {path}\n"]
        # Dirs first, then files (standard convention)
        for d in sorted(dirs):
            lines.append(d)
        for f in sorted(files):
            lines.append(f)

        # Try to get README from the embedded JSON (tree subpages have it, repo roots don't)
        readme = tree.get("readme")
        if readme and isinstance(readme, dict):
            readme_md = _format_readme_from_json(readme)
            if readme_md:
                lines.append(f"\n---\n")
                lines.append(readme_md)

        return "\n".join(lines)

    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Flash/notice banners ({{ message }} placeholders)
    ".flash",
    ".flash-full",
    ".js-flash-container",
    # Skip to content link
    ".skip-to-content",
    # App header / site nav
    ".AppHeader",
    ".Header",
    # Repo tab navigation (Code/Issues/PRs/Actions/Security/Insights)
    ".js-repo-nav",
    ".UnderlineNav",
    # Fork/Star/Notification action buttons
    ".pagehead-actions",
    # File navigation bar (Go to file / Code dropdown)
    ".file-navigation",
    # Sidebar on repo pages (About, topics, license, stats — duplicates README info)
    ".Layout-sidebar",
    # File tree table (huge, filenames repeated 3x — uses CSS modules classes)
    "table[aria-labelledby]",
    ".js-details-container table",
    "react-directory-row",
    # Commit info bar above file tree
    ".react-code-view-header-element--wide",
    # Branch picker / tag picker
    "react-branch-picker",
    # "Go to file" / branch / tag bar
    ".react-code-size-details-banner",
    # "Latest commit" / "History" bar above file tree
    "[class*='LatestCommit']",
    # Details overlay popups (dropdowns)
    "details.details-overlay",
    # Block or report section on user profiles
    ".Popover",
    # Login prompts
    ".signup-prompt-bg",
    # Contribution graph on profiles
    ".js-yearly-contributions",
    ".js-calendar-graph",
    # Achievement badges on profiles
    ".border-top.color-border-muted.pt-3.mt-3.d-none.d-md-block",
    # Sponsor/Sponsoring avatar grids on profiles
    ".avatar-group-item",
    # GitHub footer
    ".footer",
    ".site-footer",
    # --- Releases ---
    # Tag comparison dropdown (Compare / Choose a tag / Filter / View all tags)
    "select-panel",
    # Asset download section (Assets / N / Loading)
    "details-toggle",
    # Emoji reactions on releases and comments
    ".comment-reactions",
    ".js-reactions-container",
    # --- PR pages ---
    # Sticky duplicate header (repeats title, status, merge info)
    "[class*='StickyPullRequestHeader']",
    # --- Discussions ---
    # Search / filter query builder
    "query-builder",
    ".discussions-query-builder",
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (runs before markdownify)
# ---------------------------------------------------------------------------


def strip_github_junk(soup: BeautifulSoup) -> None:
    """Remove GitHub-specific junk that CSS selectors can't easily catch."""
    # Remove login links (sign in to fork/star/watch)
    for tag in list(soup.find_all("a", href=True)):
        href = tag["href"].strip()
        if href.startswith("/login?return_to="):
            tag.decompose()

    # Remove "Block or report" sections (contains form text)
    for tag in list(soup.find_all(string=lambda t: t and "Block or report" in t)):
        # Walk up to find containing section
        parent = tag.parent
        for _ in range(10):
            if parent and parent.name in ("div", "details") and len(str(parent)) > 200:
                parent.decompose()
                break
            parent = parent.parent if parent else None

    # Remove avatar images (profile pics, sponsor grids)
    for img in list(soup.find_all("img")):
        classes = " ".join(img.get("class", []))
        src = img.get("src", "")
        if "avatar" in classes or "avatars.githubusercontent.com" in src:
            img.decompose()

    # Remove achievement badge images
    for img in list(soup.find_all("img")):
        src = img.get("src", "")
        alt = img.get("alt", "")
        if "achievement" in alt.lower() or "githubassets.com/assets/" in src:
            # Remove the parent link too if it's just a badge link
            parent = img.parent
            if parent and parent.name == "a":
                parent.decompose()
            else:
                img.decompose()

    # Remove camo.githubusercontent.com badge images (shields.io proxied through GitHub)
    for img in list(soup.find_all("img", src=True)):
        if "camo.githubusercontent.com" in img["src"]:
            parent = img.parent
            if parent and parent.name == "a":
                parent.decompose()
            else:
                img.decompose()


# ---------------------------------------------------------------------------
# Markdown-level post-processing (runs after markdownify)
# ---------------------------------------------------------------------------

# Pre-compiled regex for whitespace cleanup
_EXCESSIVE_NEWLINES = re.compile(r"\n{3,}")

# Cruft strings to strip (simple string replace)
_CRUFT = [
    "[Skip to content](#start-of-content)\n\n",
    "[Skip to content](#start-of-content)\n",
    "You must be signed in to change notification settings\n\n",
    "You must be signed in to change notification settings\n",
    "\nGo to file\n",
    "\nCode\n",
    "\n## Folders and files\n",
    "\n## Repository files navigation\n",
    "## Pinned Loading\n",
    "## Pinned Loading",
    "## Popular repositories Loading\n",
    "## Popular repositories Loading",
    "Status: Open.\n",
    "\n## Issue actions",
    "\n## Metadata\n\n## Metadata\n",
    "\n## Metadata\n",
    "\nOpen\n\nOpen\n",
    "\nOpenClosed\n",
    "\n## Search results\n",
    "\nSearch Issues\n",
    # Releases
    "\nCompare\n",
    "\nPre-release\n\nPre-release\n",
    "\nOpen more actions menu\n",
    # Discussions
    "\nSearch all discussions\n",
    "\n# Filter by label\n",
    "\nUse `alt` + `click/return` to exclude labels.\n",
    # Search results
    "\n## Filter by\n",
    # Org pages
    "- [Sponsor](/sponsors)\n",
]

# --- Issues ---
# Label filter links — match by URL containing label%3A. Works both inline
# (all labels on one line, e.g., vscode issues) and one-per-line (e.g., react issues).
_LABEL_LINK_RE = re.compile(
    r"\[([^\]]+)\]\((?:https?://github\.com)?/[^\)]+label%3A[^\)]*\)"
)
# Issue list: "#35729 In facebook/react;" (note: \xa0 = non-breaking space from GitHub HTML)
_ISSUE_IN_REPO_RE = re.compile(r"\n\s*#\d+[\s\xa0]+In [^;]+;\n")
# Issue list: "· [author](/...?q=...author%3A...) opened on" -> "author opened"
_ISSUE_AUTHOR_RE = re.compile(
    r"\n\s*·[\s\xa0]*\[([^\]]+)\]\(/[^\)]+author[^\)]*\)\s*opened on (.+)"
)
# Issue list: "[Labels](/...) [Milestones](/...)" nav links
_ISSUE_NAV_RE = re.compile(r"\[Labels\]\(/[^\)]+\)\[Milestones\]\(/[^\)]+\)\n*")

# --- Single issue/PR ---
# Empty metadata fields
_EMPTY_META_RE = re.compile(
    r"\n### (?:Assignees|Type|Projects|Milestone|Relationships|Development|Reviewers|Labels)\n+"
    r"(?:No (?:one assigned|type|projects|milestone|reviews)|None yet|No branches or pull requests)\n"
)
# PR: "Development: Successfully merging..." boilerplate
_PR_DEV_META_RE = re.compile(
    r"\n### Development\n+Successfully merging this pull request may close these issues\.\n"
)
# PR/issue: "### N participants" sidebar section (also handles end-of-string)
_PARTICIPANTS_RE = re.compile(r"\n### \d+ participants?\n?$|\n### \d+ participants?\n", re.MULTILINE)
# Single issue: duplicate title anchor "[Title](#top)#35729\n"
_ISSUE_TITLE_ANCHOR_RE = re.compile(r"\n\[.+?\]\(#top\)#\d+\n")

# --- Org pages ---
# "org/repo's past year of commit activity"
_COMMIT_ACTIVITY_RE = re.compile(r"\n\s*.+['\u2019]s past year of commit activity\n")

# --- Repo pages ---
# Breadcrumb: "[org](/org) \n/\n**[repo](/org/repo)**\nPublic"
_BREADCRUMB_RE = re.compile(
    r"\n\[[\w.-]+\]\(/[\w.-]+\)\s*\n?/\s*\n\*?\*?\[[\w.-]+\]\(/[\w.-]+/[\w.-]+\)\*?\*?\s*\n(?:Public|Private)\n"
)
# Repo nav: keep stars/forks but strip [Branches][Tags][Activity] links
_REPO_MISC_NAV_RE = re.compile(
    r"\s*\[Branches\]\(/[^\)]+\)\s*\[Tags\]\(/[^\)]+\)\s*(?:\[Activity\]\(/[^\)]+\))?"
)
# Repo nav: clean "[368\nstars](...)" -> "368 stars"
_STARS_RE = re.compile(r"\[(\d[\d,.kKmM]*)\nstars\]\(/[^\)]+\)")
_FORKS_RE = re.compile(r"\[(\d[\d,.kKmM]*)\nforks\]\(/[^\)]+\)")
# Duplicate repo heading: "# owner/repo" (exact "owner/repo" pattern, not issue titles)
_REPO_HEADING_RE = re.compile(r"\n# [a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\n")
# Branch name alone on a line followed by branch/tag nav
_BRANCH_NAV_RE = re.compile(
    r"\n[a-zA-Z0-9_.-]+\n+\[Branches\]\(/[^\)]+\)\[Tags\]\(/[^\)]+\)\n"
)

# --- Releases ---
# "[View all tags](/owner/repo/tags)" nav link
_VIEW_TAGS_RE = re.compile(r"\n\[View all tags\]\(/[^\)]+/tags\)\n")
# "Assets\n2\n\nLoading" download section
_ASSETS_RE = re.compile(r"\nAssets\n\d+\n+Loading\n")
# Emoji reaction lines: "🎉\n1\n user1[, user2...] reacted with hooray emoji"
_REACTIONS_RE = re.compile(r"\n.+ reacted with .+ emoji\n")
# "N person/people reacted" summary
_PEOPLE_REACTED_RE = re.compile(r"\n\d+ (?:person|people) reacted\n")
# Duplicate release tag link [vX.Y.Z](/owner/repo/releases/tag/vX.Y.Z)
_RELEASE_TAG_DUP_RE = re.compile(r"\n\[[^\]]+\]\(/[^\)]+/releases/tag/[^\)]+\)\n")

# --- Discussions ---
# Block of filter state links (all consecutive): "- [Open](...)\n- [Closed](...)\n..."
_DISC_FILTER_BLOCK_RE = re.compile(
    r"(?:\n- \[(?:Open|Closed|Locked|Unlocked|Answered|Unanswered|Verified|All)\]"
    r"\(/[^\)]+/discussions[^\)]*\))+"
)
# "Filter: Open" or similar header
_FILTER_HEADER_RE = re.compile(r"\nFilter: (?:Open|Closed|All)\n")
# "You must be logged in to vote" (may have leading whitespace)
_VOTE_AUTH_RE = re.compile(r"\n\s*You must be logged in to vote\n")
# Standalone vote counts in discussion list (number alone before a discussion heading)
_DISC_VOTE_COUNT_RE = re.compile(r"\n- \d+\n")
# Category filter URLs in discussions: [Category](/repo/discussions?discussions_q=category%3A...)
_DISC_CATEGORY_LINK_RE = re.compile(
    r"\[([^\]]+)\]\(/[^\)]+/discussions(?:/categories/[^\)]+|[?][^\)]*discussions_q=[^\)]*category%3A[^\)]*)\)"
)
# Discussion reply/vote count links: [N](/owner/repo/discussions/ID) where text is just a number
_DISC_REPLY_COUNT_RE = re.compile(r"\n\s*\[\d+\]\(/[^\)]+/discussions/\d+\)\n?$|\n\s*\[\d+\]\(/[^\)]+/discussions/\d+\)\n", re.MULTILINE)

# --- Search results ---
# Entire filter sidebar block: from "- - [Code" through "[Advanced search]"
_SEARCH_SIDEBAR_RE = re.compile(
    r"\n- - \[(?:Code|Repositories).+?\[Advanced search\]\(/search/advanced\)\n",
    re.DOTALL,
)
# Results count header: "Repositories\xa0(250)\n\n250 results\n\n\xa0(270 ms)"
# Note: GitHub uses \xa0 (non-breaking space) before parentheses here
_SEARCH_COUNT_RE = re.compile(
    r"\n(?:Repositories|Code|Issues|Pull requests|Discussions|Users)"
    r"[\s\xa0]\(\d[\d,]*\)\n+"
    r"\d[\d,]* results\n+"
    r"(?:[\s\xa0]?\(\d+ ms\)\n)?"
)
# Sort/filter controls: "Sort by: Best match", "## N results" dupe heading, stray "Filter"
_SORT_BY_RE = re.compile(r"\nSort by:[^\n]+\n")
_SEARCH_RESULTS_HEADING_RE = re.compile(r"\n## \d[\d,]* results\n")
# Topic info cards on search: image + description blocks
_TOPIC_CARD_RE = re.compile(
    r"!\[.+?\]\(https://github\.com/github/explore/blob/main/topics/[^\)]+\).+?"
    r"\[View topic\]\(/topics?/[^\)]+\)\n",
    re.DOTALL,
)


def postprocess_github(markdown: str) -> str:
    """Strip remaining GitHub UI text from markdown."""
    for pattern in _CRUFT:
        markdown = markdown.replace(pattern, "\n")

    # Replace label links with just the label name (strip long filter URLs).
    # Uses a sentinel (\x00) so adjacent inline labels get comma-separated.
    def _clean_label(m: re.Match) -> str:
        text = m.group(1)
        # GitHub concatenates label name + description without separator
        # e.g., "Status: UnconfirmedA potential issue that..." -> "Status: Unconfirmed"
        # Split at lowercase->uppercase boundary (where description starts)
        split = re.search(r"[a-z][A-Z]", text)
        if split and len(text) > 30:
            text = text[: split.start() + 1]
        return f"\x00{text}"
    markdown = _LABEL_LINK_RE.sub(_clean_label, markdown)
    # Rejoin sentinel-marked labels: adjacent ones become comma-separated
    if "\x00" in markdown:
        lines = markdown.split("\n")
        for i, line in enumerate(lines):
            if "\x00" in line:
                parts = [p.strip() for p in line.split("\x00") if p.strip()]
                prefix = line[: line.index("\x00")]
                lines[i] = prefix + ", ".join(parts)
        markdown = "\n".join(lines)

    # Strip "In org/repo;" per issue
    markdown = _ISSUE_IN_REPO_RE.sub("\n", markdown)
    # Simplify author lines: "· [user](filter-url) opened on Date" -> "user opened on Date"
    markdown = _ISSUE_AUTHOR_RE.sub(r"\n  \1 opened on \2", markdown)
    # Strip issue list nav links
    markdown = _ISSUE_NAV_RE.sub("", markdown)
    # Strip empty metadata sections on single issues/PRs
    markdown = _EMPTY_META_RE.sub("\n", markdown)
    markdown = _PR_DEV_META_RE.sub("\n", markdown)
    markdown = _PARTICIPANTS_RE.sub("\n", markdown)
    # Strip commit activity text on org pages
    markdown = _COMMIT_ACTIVITY_RE.sub("\n", markdown)
    # Strip duplicate title anchor on single issues
    markdown = _ISSUE_TITLE_ANCHOR_RE.sub("\n", markdown)
    # Strip camo badge images (linked first to avoid dangling ](url), then standalone)
    markdown = _CAMO_LINKED_RE.sub("", markdown)
    markdown = _CAMO_STANDALONE_RE.sub("", markdown)
    # Strip breadcrumb navigation
    markdown = _BREADCRUMB_RE.sub("\n", markdown)
    # Strip branch name + branch/tag nav (must run before misc nav strips [Branches][Tags])
    markdown = _BRANCH_NAV_RE.sub("\n", markdown)
    # Clean repo nav: "[368\nstars](/...)" -> "368 stars", strip Branches/Tags/Activity links
    markdown = _STARS_RE.sub(r"\1 stars", markdown)
    markdown = _FORKS_RE.sub(r"\1 forks", markdown)
    markdown = _REPO_MISC_NAV_RE.sub("", markdown)
    # Strip duplicate repo heading (e.g., "# owner/repo" that duplicates page title)
    markdown = _REPO_HEADING_RE.sub("\n", markdown)

    # --- Releases ---
    markdown = _VIEW_TAGS_RE.sub("\n", markdown)
    markdown = _ASSETS_RE.sub("\n", markdown)
    markdown = _REACTIONS_RE.sub("\n", markdown)
    markdown = _PEOPLE_REACTED_RE.sub("\n", markdown)
    markdown = _RELEASE_TAG_DUP_RE.sub("\n", markdown)

    # --- Discussions ---
    markdown = _DISC_FILTER_BLOCK_RE.sub("\n", markdown)
    markdown = _FILTER_HEADER_RE.sub("\n", markdown)
    markdown = _VOTE_AUTH_RE.sub("\n", markdown)
    markdown = _DISC_VOTE_COUNT_RE.sub("\n", markdown)
    # Clean discussion category links: [Category](long-filter-url) -> Category
    markdown = _DISC_CATEGORY_LINK_RE.sub(r"\1", markdown)
    # Strip bare reply/vote count links
    markdown = _DISC_REPLY_COUNT_RE.sub("\n", markdown)

    # --- Search results ---
    markdown = _SEARCH_SIDEBAR_RE.sub("\n", markdown)
    markdown = _SEARCH_COUNT_RE.sub("\n", markdown)
    markdown = _SEARCH_RESULTS_HEADING_RE.sub("\n", markdown)
    markdown = _SORT_BY_RE.sub("\n", markdown)
    markdown = _TOPIC_CARD_RE.sub("", markdown)
    # Stray "Filter" from search filter buttons (after other search cleanup)
    markdown = markdown.replace("\n\nFilter\n\n", "\n\n")

    # Collapse excessive newlines left behind
    markdown = _EXCESSIVE_NEWLINES.sub("\n\n", markdown).strip()
    return markdown
