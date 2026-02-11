"""Search tool — web search via Google + DuckDuckGo."""

from ..search import search


async def search_web(
    query: str,
    page: int = 1,
) -> dict:
    """
    Search the web and return results with titles, URLs, and snippets.

    Args:
        query: Search query
        page: Result page (1-indexed, default 1)

    Returns:
        Dict with "content" (formatted text) or "error" (error message).
    """
    return await search(query=query, page=page)
