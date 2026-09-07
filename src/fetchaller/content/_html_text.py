"""Linear removal of HTML tags and comments for small site-specific parsers."""

from __future__ import annotations


def strip_markup(value: str) -> str:
    """Return text outside tags/comments without regex backtracking.

    This intentionally mirrors the lightweight tag stripping used by the job
    board parsers; it is not a DOM implementation. Each input character is
    visited at most a constant number of times, including malformed input with
    thousands of unterminated ``<`` or ``<!--`` openers.
    """

    if not value:
        return ""
    pieces: list[str] = []
    cursor = 0
    length = len(value)
    while cursor < length:
        opener = value.find("<", cursor)
        if opener < 0:
            pieces.append(value[cursor:])
            break
        pieces.append(value[cursor:opener])
        if value.startswith("<!--", opener):
            end = value.find("-->", opener + 4)
            pieces.append(" ")
            if end < 0:
                break
            cursor = end + 3
            continue
        end = value.find(">", opener + 1)
        if end < 0:
            # Match the old tag regex: an unterminated ordinary tag is text.
            pieces.append(value[opener:])
            break
        pieces.append(" ")
        cursor = end + 1
    return "".join(pieces).replace("-->", " ")
