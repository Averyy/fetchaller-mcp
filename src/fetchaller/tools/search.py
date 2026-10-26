"""Unified web search tool."""

from ..search import search


async def search_web(query: str, provider: str, page: int = 1, *, browser_solver=None) -> dict:
    """Search via the selected provider, with Google failure fallback and source labels."""
    return await search(query=query, page=page, provider=provider, browser_solver=browser_solver)
