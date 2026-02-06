"""Reddit URL transformation and formatting."""

from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse


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
    Format a Unix timestamp as relative time.

    Args:
        utc_seconds: Unix timestamp

    Returns:
        Human-readable relative time string
    """
    import time

    now = time.time()
    diff = now - utc_seconds

    if diff < 60:
        return "just now"
    if diff < 3600:
        n = int(diff / 60)
        return f"{n} {'minute' if n == 1 else 'minutes'} ago"
    if diff < 86400:
        n = int(diff / 3600)
        return f"{n} {'hour' if n == 1 else 'hours'} ago"
    if diff < 2592000:  # 30 days
        n = int(diff / 86400)
        return f"{n} {'day' if n == 1 else 'days'} ago"
    if diff < 31536000:  # 365 days
        n = int(diff / 2592000)
        return f"{n} {'month' if n == 1 else 'months'} ago"
    n = int(diff / 31536000)
    return f"{n} {'year' if n == 1 else 'years'} ago"


def format_reddit_post(
    post_data: dict,
    index: int,
    include_subreddit: bool = False,
) -> str:
    """
    Format a Reddit post for display.

    Args:
        post_data: Post data from Reddit API (the 'data' field)
        index: Post index (1-based)
        include_subreddit: Whether to include subreddit name

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

    url = f"https://old.reddit.com{permalink}"

    # Preview of selftext (first 200 chars)
    preview = ""
    if selftext:
        preview_text = selftext[:200].replace("\n", " ").strip()
        if len(selftext) > 200:
            preview_text += "..."
        preview = f'\n   > "{preview_text}"'

    sub_line = f"r/{subreddit} · " if include_subreddit else ""
    time_str = format_relative_time(created_utc) if created_utc else "unknown"

    return f"""{index}. {title}
   {sub_line}▲ {score:,} · 💬 {num_comments} · u/{author} · {time_str}
   {url}{preview}"""
