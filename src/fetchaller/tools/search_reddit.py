"""Search Reddit tool - search posts across Reddit."""

from ..content.fetcher import ContentFetcher, RetryConfig
from ..content.reddit import format_reddit_post
from ..queue.reddit_queue import RedditRequestQueue, get_reddit_queue
from .browse_reddit import fetch_reddit_json


async def search_reddit(
    query: str,
    subreddit: str | None = None,
    sort: str = "relevance",
    time: str = "all",
    limit: int = 10,
    after: str | None = None,
    timeout: int = 10,
    fetcher: ContentFetcher | None = None,
    queue: RedditRequestQueue | None = None,
) -> dict:
    """
    Search Reddit posts.

    Args:
        query: Search query
        subreddit: Optional subreddit to limit search to (without r/)
        sort: Sort order - relevance, hot, top, new, comments
        time: Time filter - hour, day, week, month, year, all
        limit: Number of results (1-25)
        after: Pagination cursor from previous response
        timeout: Request timeout in seconds
        fetcher: Optional ContentFetcher instance
        queue: Optional RedditRequestQueue for rate limiting

    Returns:
        Dict with content or error
    """
    # Validate query
    if not query or not query.strip():
        return {"error": "Query is required"}

    # Validate sort
    if sort not in ("relevance", "hot", "top", "new", "comments"):
        return {"error": "Invalid sort. Must be: relevance, hot, top, new, comments"}

    # Validate time filter
    if time not in ("hour", "day", "week", "month", "year", "all"):
        return {"error": "Invalid time. Must be: hour, day, week, month, year, all"}

    # Validate subreddit if provided
    import re

    if subreddit and not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$", subreddit):
        return {"error": "Invalid subreddit name"}

    # Clamp limit
    limit = max(1, min(25, limit))

    # Build URL
    from urllib.parse import urlencode

    params = {
        "q": query,
        "sort": sort,
        "t": time,
        "limit": str(limit),
    }
    if after:
        params["after"] = after

    if subreddit:
        params["restrict_sr"] = "1"
        url = f"https://www.reddit.com/r/{subreddit}/search.json?{urlencode(params)}"
    else:
        url = f"https://www.reddit.com/search.json?{urlencode(params)}"

    # Create fetcher if not provided
    if fetcher is None:
        fetcher = ContentFetcher(retry_config=RetryConfig())

    # Get queue if not provided
    if queue is None:
        queue = get_reddit_queue()

    result = await fetch_reddit_json(url, fetcher, queue, float(timeout))

    if "error" in result:
        return result

    data = result["data"]
    posts = data.get("data", {}).get("children", [])
    after_cursor = data.get("data", {}).get("after")

    if not posts:
        return {"content": f'Search: "{query}" · {sort} · {time} · No results found'}

    # Format output
    sub_note = f" in r/{subreddit}" if subreddit else ""
    lines = [f'Search: "{query}"{sub_note} · {sort} · {time} · {len(posts)} results\n']

    for i, post in enumerate(posts, 1):
        # Include subreddit in search results (unless limited to one)
        lines.append(format_reddit_post(post.get("data", {}), i, include_subreddit=not subreddit))

    if after_cursor:
        lines.append(f"\n[Next page: after={after_cursor}]")

    lines.append('\n---\nTo read full post: mcp__fetchaller__fetch({ url: "https://old.reddit.com/r/.../comments/..." })')

    return {"content": "\n".join(lines)}
