"""Search Reddit tool - search posts across Reddit."""

from ..content.reddit import format_reddit_post
from ..content.reddit_graphql import (
    search_posts_listing,
    search_posts_request,
    valid_search_cursor,
)
from ..queue.reddit_queue import RedditRequestQueue
from .browse_reddit import (
    _SUBREDDIT_PATTERN,
    _get_session,
    _validated_listing_data,
    fetch_reddit_graphql,
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
    # Search pages are cursors of Reddit's GraphQL search (opaque base64), not
    # the t3_ fullnames REST listings page by: REST /search.json is refused
    # to logged-out clients, so post search is the app's SearchPosts read.
    if after is not None and not valid_search_cursor(after):
        return {"error": "Invalid Reddit search pagination cursor"}

    request = search_posts_request(
        query,
        sort=sort,
        time=time,
        subreddits=(subreddit,) if subreddit else (),
        limit=limit,
        after=after,
    )
    session = await _get_session(browser_solver)
    result = await fetch_reddit_graphql(
        request,
        session,
        queue,
        float(timeout),
    )

    if "error" in result:
        return result

    converted = search_posts_listing(
        result["data"], subreddits=(subreddit,) if subreddit else ()
    )
    if "error" in converted:
        return {"error": converted["error"]}
    listing_data = _validated_listing_data(converted["listing"])
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

    if valid_search_cursor(after_cursor):
        lines.append(f"\n[Next page: after={after_cursor}]")

    return {"content": "\n".join(lines)}
