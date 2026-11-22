"""Search result data model."""

from dataclasses import dataclass


@dataclass
class SearchResult:
    """A single search result."""

    title: str
    url: str
    snippet: str
    source: str = ""
    # How the engine displays the destination when ``url`` is the engine's own
    # redirect rather than the destination (Brave's cloaked links). Display
    # text, never a URL.
    display: str = ""
