"""Browse Reddit tool - browse subreddit listings."""

import asyncio
import re
from urllib.parse import urlencode

import wafer

from ..config import get_wafer_cache_dir
from ..content.reddit import format_reddit_post
from ..queue.reddit_queue import RedditRequestQueue

# Pre-compiled regex for subreddit name validation
_SUBREDDIT_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$")

# Shared session for Reddit requests (reuses TLS identity and cookies)
_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(max_rotations=0, cache_dir=get_wafer_cache_dir())
    return _session


async def close_session() -> None:
    global _session
    _session = None

_JSON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


async def fetch_reddit_json(
    url: str,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None = None,
    timeout: float = 10.0,
) -> dict:
    """
    Fetch Reddit JSON API with rate limiting.

    Args:
        url: Reddit API URL (must end in .json)
        session: wafer AsyncSession instance
        queue: Optional RedditRequestQueue for rate limiting
        timeout: Request timeout

    Returns:
        Dict with either 'data' or 'error'
    """

    async def _do_fetch() -> dict:
        try:
            resp = await session.get(url, headers=_JSON_HEADERS, timeout=timeout)

            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after", "60")
                if queue:
                    queue.set_backoff(429)
                return {"error": f"Rate limited. Reddit allows ~10 requests/min. Retry after {retry_after}s."}

            if resp.status_code == 403:
                if queue:
                    queue.set_backoff(403)
                return {"error": "Access forbidden (403). Reddit may be blocking requests."}

            if resp.status_code >= 400:
                return {"error": f"HTTP {resp.status_code}"}

            data = resp.json()
            return {"data": data}

        except wafer.WaferTimeout:
            return {"error": f"Request timed out ({timeout}s limit)"}
        except Exception as e:
            return {"error": f"Fetch failed: {e}"}

    # Use queue if available. Pass the caller's timeout as the queue-wait bound so
    # a long rate-limit backoff can't hang the request past its own timeout.
    if queue:
        return await queue.enqueue(_do_fetch, _queue_timeout=timeout)
    return await _do_fetch()


async def browse_reddit(
    subreddit: str,
    sort: str = "hot",
    time: str = "day",
    limit: int = 10,
    after: str | None = None,
    timeout: int = 10,
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
    params = {"limit": str(limit)}
    if sort == "top":
        params["t"] = time
    if after:
        params["after"] = after

    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?{urlencode(params)}"

    # Get queue if not provided
    owns_queue = queue is None
    if owns_queue:
        queue = RedditRequestQueue()

    session = await _get_session()
    try:
        result = await fetch_reddit_json(url, session, queue, float(timeout))

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

        return {"content": "\n".join(lines)}
    finally:
        if owns_queue:
            await queue.stop()
