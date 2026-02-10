"""Reddit-specific HTML cleanup, URL transforms, and post formatting.

Exports the standard site interface (SELECTORS_LIST) plus Reddit-specific
helpers (transform_reddit_url, format_reddit_post, format_relative_time).
"""

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

# Strip slug from Reddit permalink: /r/sub/comments/id/slug/ → /r/sub/comments/id/
_PERMALINK_SLUG_RE = re.compile(r"(/comments/[a-z0-9]+)/[^/]+/?$")

# Media domains and extensions (used to filter link posts vs media posts)
_MEDIA_DOMAINS = ("i.redd.it", "v.redd.it", "preview.redd.it", "i.imgur.com")
_MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".webp", ".mp4", ".webm")

# ---------------------------------------------------------------------------
# CSS selectors for elements to remove before markdown conversion (old.reddit.com)
# ---------------------------------------------------------------------------

SELECTORS_LIST = [
    # Sidebar / structural
    ".side",
    ".footer-parent",
    ".listing-chooser",
    ".search-page",
    ".searchpane",
    ".infobar",
    ".premium-banner-outer",
    ".morelink",
    ".titlebox",
    ".login-form-side",
    ".promotedlink",
    ".organic-listing",
    # Vote UI — keep .score.unvoted (real score), strip the other two variants
    ".score.dislikes",
    ".score.likes",
    ".arrow",
    # Post rank numbers (1, 2, 3...)
    ".rank",
    # "loading..." placeholder text
    "span.error",
    # Empty clearfix divs
    ".clearleft",
    # Per-comment action buttons (permalink/embed/save/report/reply/parent)
    ".comment .flat-list",
    # Per-post action buttons on listings — DON'T remove .link .flat-list
    # because it contains the useful "[N comments]" link; _strip_junk_links
    # already handles the share/save/hide/report items within it.
    # Sort menu in thread header
    ".commentarea > .menuarea",
    # "all N comments" header in threads
    ".panestack-title",
    # Collapse/expand toggles
    ".comment .expand",
    # Thumbnail images (tiny previews, no LLM value)
    ".thumbnail",
    # Tracking pixel
    "img[src*='pixel.png']",
    # Server-rendered footer ("π Rendered by PID...")
    ".bottommenu",
    # Tab nav (hot/new/rising/top) and time period dropdown
    ".tabmenu",
    ".dropdown-title.lightdrop",
    ".dropdown.lightdrop",
    ".drop-choices.lightdrop",
]


@dataclass
class RedditTransformResult:
    """Result of Reddit URL transformation."""

    url: str
    is_reddit: bool


def transform_reddit_url(url: str) -> RedditTransformResult:
    """
    Transform Reddit URLs for optimal fetching.

    Transforms:
    - www.reddit.com -> old.reddit.com (65-70% smaller markdown)
    - reddit.com -> old.reddit.com
    - Adds trailing slash to avoid 301 redirects

    JSON URLs (ending in .json) are left unchanged.

    Args:
        url: URL to potentially transform

    Returns:
        RedditTransformResult with transformed URL and is_reddit flag
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return RedditTransformResult(url=url, is_reddit=False)

    hostname = parsed.hostname or ""
    if "reddit.com" not in hostname.lower():
        return RedditTransformResult(url=url, is_reddit=False)

    # Leave JSON URLs alone (user explicitly requested JSON)
    if parsed.path.endswith(".json"):
        return RedditTransformResult(url=url, is_reddit=True)

    # Transform www.reddit.com or reddit.com -> old.reddit.com
    if hostname in ("www.reddit.com", "reddit.com"):
        # Build new URL with old.reddit.com
        new_netloc = "old.reddit.com"
        if parsed.port:
            new_netloc += f":{parsed.port}"
    else:
        new_netloc = parsed.netloc

    # Add trailing slash if needed (avoids 301 redirect, saves ~50-100ms)
    path = parsed.path
    if path and not path.endswith("/") and "." not in path.split("/")[-1]:
        path += "/"

    new_url = urlunparse(
        (
            parsed.scheme,
            new_netloc,
            path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )

    return RedditTransformResult(url=new_url, is_reddit=True)


def format_relative_time(utc_seconds: float) -> str:
    """
    Format a Unix timestamp as compact relative time.

    Args:
        utc_seconds: Unix timestamp

    Returns:
        Compact relative time string (e.g., "3h", "2d", "5mo")
    """
    import time

    now = time.time()
    diff = now - utc_seconds

    if diff < 60:
        return "now"
    if diff < 3600:
        return f"{int(diff / 60)}m"
    if diff < 86400:
        return f"{int(diff / 3600)}h"
    if diff < 2592000:  # 30 days
        return f"{int(diff / 86400)}d"
    if diff < 31536000:  # 365 days
        return f"{int(diff / 2592000)}mo"
    return f"{int(diff / 31536000)}y"


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace (spaces, newlines, tabs) into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def format_reddit_post(
    post_data: dict,
    index: int,
    include_subreddit: bool = False,
    preview_length: int = 160,
) -> str:
    """
    Format a Reddit post for display.

    Args:
        post_data: Post data from Reddit API (the 'data' field)
        index: Post index (1-based)
        include_subreddit: Whether to include subreddit name
        preview_length: Max chars for selftext preview

    Returns:
        Formatted post string
    """
    title = post_data.get("title", "Untitled")
    score = post_data.get("score", 0)
    num_comments = post_data.get("num_comments", 0)
    author = post_data.get("author", "[deleted]")
    created_utc = post_data.get("created_utc", 0)
    permalink = post_data.get("permalink", "")
    selftext = post_data.get("selftext", "")
    subreddit = post_data.get("subreddit", "")
    is_self = post_data.get("is_self", True)
    external_url = post_data.get("url", "")

    # Strip slug from permalink (it just repeats the title)
    short_permalink = _PERMALINK_SLUG_RE.sub(r"\1/", permalink)
    discussion_url = f"https://old.reddit.com{short_permalink}"

    # For link posts, show external article URL + discussion URL
    # For self-posts / images / videos, just show discussion URL
    is_media = any(d in external_url for d in _MEDIA_DOMAINS) or any(external_url.lower().endswith(e) for e in _MEDIA_EXTENSIONS)
    if is_self or not external_url or "reddit.com" in external_url or is_media:
        urls = f"   {discussion_url}"
    else:
        urls = f"   {external_url}\n   {discussion_url}"

    # Preview of selftext with collapsed whitespace
    preview = ""
    if selftext:
        clean = _collapse_whitespace(selftext)
        preview_text = clean[:preview_length]
        if len(clean) > preview_length:
            preview_text += "..."
        preview = f"\n   > {preview_text}"

    sub_line = f"r/{subreddit} · " if include_subreddit else ""
    time_str = format_relative_time(created_utc) if created_utc else "?"

    return f"""{index}. {title}
   {sub_line}▲{score:,} 💬{num_comments} u/{author} {time_str}
{urls}{preview}"""
