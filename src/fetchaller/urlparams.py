"""Query-string helpers shared by site packages."""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


def with_param(url: str, name: str, value: int | str | None) -> str:
    """``url`` with one query parameter set (or removed when ``value`` is None).

    Paging links are built from the caller's own URL rather than reassembled,
    so the slug, refinements and every parameter this package does not read
    survive untouched — only the one being changed moves.
    """
    parsed = urlparse(url)
    pairs = [
        (k, v)
        for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
        if k.lower() != name.lower()
    ]
    flat = [(k, v) for k, values in pairs for v in values]
    if value is not None:
        flat.append((name, str(value)))
    return urlunparse(parsed._replace(query=urlencode(flat)))
