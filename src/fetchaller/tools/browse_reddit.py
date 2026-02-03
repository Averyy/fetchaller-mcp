"""Browse Reddit tool - browse subreddit listings."""

import re

from ..content.fetcher import ContentFetcher, RetryConfig
from ..content.reddit import format_reddit_post
from ..queue.reddit_queue import RedditRequestQueue, get_reddit_queue

# Pre-compiled regex for subreddit name validation
_SUBREDDIT_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$")


async def fetch_reddit_json(
    url: str,
    fetcher: ContentFetcher,
    queue: RedditRequestQueue | None = None,
    timeout: float = 10.0,
) -> dict:
    """
    Fetch Reddit JSON API with rate limiting.

    Args:
        url: Reddit API URL (must end in .json)
        fetcher: ContentFetcher instance
        queue: Optional RedditRequestQueue for rate limiting
        timeout: Request timeout

    Returns:
        Dict with either 'data' or 'error'
    """

    async def _do_fetch() -> dict:
        try:
            result = await fetcher.fetch(url, timeout=timeout, use_json_headers=True)

            if result.status_code == 429:
                retry_after = result.headers.get("retry-after", "60")
                if queue:
                    queue.set_backoff(429)
                return {"error": f"Rate limited. Reddit allows ~10 requests/min. Retry after {retry_after}s."}

            if result.status_code == 403:
                if queue:
                    queue.set_backoff(403)
                return {"error": "Access forbidden (403). Reddit may be blocking requests."}

            if result.status_code >= 400:
                return {"error": f"HTTP {result.status_code}"}

            import json

            data = json.loads(result.content.decode("utf-8"))
            return {"data": data}

        except TimeoutError:
            return {"error": f"Request timed out ({timeout}s limit)"}
        except Exception as e:
            return {"error": f"Fetch failed: {e}"}

    # Use queue if available
    if queue:
        return await queue.enqueue(_do_fetch)
    return await _do_fetch()


async def browse_reddit(
    subreddit: str,
    sort: str = "hot",
    time: str = "day",
    limit: int = 10,
    after: str | None = None,
    timeout: int = 10,
    fetcher: ContentFetcher | None = None,
    queue: RedditRequestQueue | None = None,
) -> dict:
    """
    Browse a subreddit's posts.

    Args:
        subreddit: Subreddit name without r/ prefix
        sort: Sort order - hot, new, top, rising
        time: Time filter for 'top' - hour, day, week, month, year, all
        limit: Number of posts (1-25)
        after: Pagination cursor from previous response
        timeout: Request timeout in seconds
        fetcher: Optional ContentFetcher instance
        queue: Optional RedditRequestQueue for rate limiting

    Returns:
        Dict with content or error
    """
    # Validate subreddit name
    if not _SUBREDDIT_PATTERN.match(subreddit):
        return {"error": "Invalid subreddit name"}

    # Validate sort
    if sort not in ("hot", "new", "top", "rising"):
        return {"error": "Invalid sort. Must be: hot, new, top, rising"}

    # Validate time filter
    if time not in ("hour", "day", "week", "month", "year", "all"):
        return {"error": "Invalid time. Must be: hour, day, week, month, year, all"}

    # Clamp limit
    limit = max(1, min(25, limit))

    # Build URL
    from urllib.parse import urlencode

    params = {"limit": str(limit)}
    if sort == "top":
        params["t"] = time
    if after:
        params["after"] = after

    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?{urlencode(params)}"

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
        return {"content": f"r/{subreddit} · {sort} · No posts found"}

    # Format output
    lines = [f"r/{subreddit} · {sort} · {len(posts)} posts\n"]

    for i, post in enumerate(posts, 1):
        lines.append(format_reddit_post(post.get("data", {}), i, include_subreddit=False))

    if after_cursor:
        lines.append(f"\n[Next page: after={after_cursor}]")

    lines.append(f'\n---\nTo read full post: mcp__fetchaller__fetch({{ url: "https://old.reddit.com/r/{subreddit}/comments/..." }})')

    return {"content": "\n".join(lines)}
