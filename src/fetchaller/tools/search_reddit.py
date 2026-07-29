"""Search Reddit tool - search posts across Reddit."""

from urllib.parse import urlencode

from ..content.reddit import format_reddit_post
from ..queue.reddit_queue import RedditRequestQueue
from .browse_reddit import (
    _PAGINATION_CURSOR_PATTERN,
    _SUBREDDIT_PATTERN,
    _get_session,
    _validated_listing_data,
    fetch_reddit_json,
)


async def search_reddit(
    query: str,
    subreddit: str | None = None,
    sort: str = "relevance",
    time: str = "all",
    limit: int = 10,
    after: str | None = None,
    timeout: int = 10,
    queue: RedditRequestQueue | None = None,
    browser_solver=None,
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
        queue: Optional RedditRequestQueue for rate limiting

    Returns:
        Dict with content or error
    """
    # Validate query
    if not query or not query.strip():
        return {"error": "Query is required"}
    if len(query) > 512:
        return {"error": "Query must be 512 characters or fewer"}

    # Validate sort
    if sort not in ("relevance", "hot", "top", "new", "comments"):
        return {"error": "Invalid sort. Must be: relevance, hot, top, new, comments"}

    # Validate time filter
    if time not in ("hour", "day", "week", "month", "year", "all"):
        return {"error": "Invalid time. Must be: hour, day, week, month, year, all"}

    # Validate subreddit if provided
    if subreddit and not _SUBREDDIT_PATTERN.fullmatch(subreddit):
        return {"error": "Invalid subreddit name"}

    # Clamp limit
    limit = max(1, min(25, limit))
    if after is not None and not _PAGINATION_CURSOR_PATTERN.fullmatch(after):
        return {"error": "Invalid Reddit pagination cursor"}

    # Build URL
    params = {
        "q": query,
        "sort": sort,
        "t": time,
        "limit": str(limit),
        "raw_json": "1",
    }
    if after:
        params["after"] = after

    if subreddit:
        params["restrict_sr"] = "1"
        url = f"https://www.reddit.com/r/{subreddit}/search.json?{urlencode(params)}"
    else:
        url = f"https://www.reddit.com/search.json?{urlencode(params)}"

    session = await _get_session(browser_solver)
    result = await fetch_reddit_json(url, session, queue, float(timeout))

    if "error" in result:
        return result

    payload = result["data"]
    if (
        isinstance(payload, dict)
        and (content_state := payload.get("_reddit_content_state"))
    ):
        return {"content": f'Search: "{query}" · {sort} · {time}\n\n{content_state}'}
    listing_data = _validated_listing_data(payload)
    if listing_data is None:
        return {"error": "Reddit returned an invalid search response"}
    posts = listing_data["children"]
    after_cursor = listing_data.get("after")

    if not posts:
        return {"content": f'Search: "{query}" · {sort} · {time} · No results found'}

    # Format output
    sub_note = f" in r/{subreddit}" if subreddit else ""
    lines = [f'Search: "{query}"{sub_note} · {sort} · {time} · {len(posts)} results\n']

    for i, post in enumerate(posts, 1):
        # Include subreddit in search results (unless limited to one)
        lines.append(format_reddit_post(post.get("data", {}), i, include_subreddit=not subreddit))

    if isinstance(after_cursor, str) and _PAGINATION_CURSOR_PATTERN.fullmatch(
        after_cursor
    ):
        lines.append(f"\n[Next page: after={after_cursor}]")
    elif after_cursor is not None:
        lines.append(
            "\n[Next page unavailable: Reddit returned an invalid pagination "
            "cursor]"
        )

    return {"content": "\n".join(lines)}
