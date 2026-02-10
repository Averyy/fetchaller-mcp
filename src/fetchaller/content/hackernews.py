"""Hacker News-specific HTML cleanup and story reformatter.

Exports the standard site interface (SELECTORS_LIST, is_hackernews,
strip_hn_junk, postprocess_hn).
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


def is_hackernews(url: str) -> bool:
    """Check if URL is a Hacker News page."""
    hostname = urlparse(url).hostname or ""
    return hostname == "news.ycombinator.com"


# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Vote arrows
    ".votelinks",
    # Comment navigation (next/prev/parent/root/context + collapse toggle)
    ".navs",
    # Reply links
    ".reply",
    # Header navigation (new | past | comments | ask | show | jobs | submit)
    ".pagetop",
    # Footer links (Guidelines | FAQ | Lists | API | Security | Legal | Apply to YC)
    ".yclinks",
    # Spacer rows between stories
    "tr.spacer",
    # Spacer GIF images used for indentation
    "img[src$='s.gif']",
    # "on:" story reference in single-comment view
    ".onstory",
    # Empty unvoted spans
    "[id^='unv_']",
    # "More" pagination row
    "tr.morespace",
    # HN logo image
    "img[src*='y18']",
    # Search form in footer
    "form",
]


# ---------------------------------------------------------------------------
# Soup-level cleanup (runs before markdownify)
# ---------------------------------------------------------------------------


def strip_hn_junk(soup: BeautifulSoup) -> None:
    """Remove HN-specific action links and unwrap layout tables."""
    # Remove action links: hide, favorite, vote, login, reply, past-search
    for tag in list(soup.find_all("a", href=True)):
        href = tag["href"].strip()
        if any(href.startswith(p) for p in ("hide?", "fave?", "vote?", "login?", "reply?")):
            tag.decompose()
        elif "hn.algolia.com" in href:
            # "past" link that searches Algolia for duplicate stories
            tag.decompose()
        elif href.startswith("from?"):
            # Domain link like (from?site=github.com) — keep text, remove link
            tag.unwrap()

    # Clean up stray pipe separators left after link removal in sublines
    for subline in soup.find_all("span", class_="subline"):
        for text_node in list(subline.find_all(string=True)):
            if text_node.strip() == "|":
                text_node.extract()

    # Also clean up stray pipes in comhead (comment headers)
    for comhead in soup.find_all("span", class_="comhead"):
        for text_node in list(comhead.find_all(string=True)):
            if text_node.strip() == "|":
                text_node.extract()

    # Convert layout tables to simple block/inline elements.
    # HN uses tables purely for layout — this prevents ugly markdown table syntax.

    # Unwrap cell elements (contents become inline within rows)
    for tag_name in ("td", "th"):
        for tag in soup.find_all(tag_name):
            tag.unwrap()

    # Convert rows to divs (preserves line separation between stories/comments)
    for tag in soup.find_all("tr"):
        tag.name = "div"
        tag.attrs = {}

    # Unwrap remaining table containers
    for tag_name in ("table", "tbody", "thead"):
        for tag in soup.find_all(tag_name):
            tag.unwrap()


# ---------------------------------------------------------------------------
# Markdown-level post-processing (runs after markdownify)
# ---------------------------------------------------------------------------

# Matches: N.[Title](url) (domain)  OR  N.[Title](item?id=ID)
# Then: N points by [user](user?id=...) [time](item?id=ID) [comments](item?id=ID)
_STORY_RE = re.compile(
    r"(\d+)\.\[(.+?)\]\((.+?)\)"       # rank.[title](url)
    r"(?: \(.+?\))?"                     # optional (domain) — dropped
    r"\n\n"                              # blank line
    r"(\d+) points by "                  # points
    r"\[(.+?)\]\(user\?id=.+?\) "       # [user](user?id=...)
    r"\[(.+?)\]\(item\?id=(\d+)\) "     # [time](item?id=ID)
    r"\[(.+?)\]\(item\?id=\d+\)"        # [comments/discuss](item?id=ID)
)
_TIME_RE = re.compile(r"(\d+) (minute|hour|day|month|year)s? ago")
_COMMENTS_RE = re.compile(r"(\d+)[\s\xa0]comments?")


def _compact_time(time_str: str) -> str:
    """Convert HN time like '3 hours ago' to compact '3h'."""
    m = _TIME_RE.match(time_str)
    if m:
        n, unit = m.group(1), m.group(2)
        abbrev = {"minute": "m", "hour": "h", "day": "d", "month": "mo", "year": "y"}
        return f"{n}{abbrev.get(unit, unit[0])}"
    return time_str


def _format_story(match: re.Match) -> str:
    """Reformat a single HN story block to compact form."""
    rank = match.group(1)
    title = match.group(2)
    url = match.group(3)
    points = match.group(4)
    username = match.group(5)
    time_str = match.group(6)
    item_id = match.group(7)
    comments_text = match.group(8)

    compact_time = _compact_time(time_str)

    cm = _COMMENTS_RE.match(comments_text)
    comment_count = cm.group(1) if cm else "0"

    discussion_url = f"https://news.ycombinator.com/item?id={item_id}"

    meta = f"▲{points} 💬{comment_count} {username} {compact_time}"

    # Self-posts (Ask HN, etc.) link to item?id= — only show discussion URL
    if url.startswith("item?id="):
        return (
            f"{rank}. {title}\n"
            f"   {meta}\n"
            f"   {discussion_url}"
        )

    return (
        f"{rank}. {title}\n"
        f"   {meta}\n"
        f"   {url}\n"
        f"   {discussion_url}"
    )


def postprocess_hn(markdown: str) -> str:
    """Reformat HN story listings for token efficiency."""
    result = _STORY_RE.sub(_format_story, markdown)
    # Clean up [More] pagination link
    result = result.replace("[More](?p=", "[More](https://news.ycombinator.com/?p=")
    return result
