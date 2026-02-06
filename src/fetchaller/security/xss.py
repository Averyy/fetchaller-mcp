"""XSS prevention utilities."""

import html
import re

# Pre-compiled regex for control character removal (hot path)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def escape_html(value: str | None) -> str:
    """
    HTML entity encoding to prevent XSS.

    Escapes: & < > " '
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def sanitize_for_log(value: str | None, max_length: int = 100) -> str:
    """
    Sanitize values for logging.

    Removes control characters and newlines, truncates to max_length.
    """
    if value is None:
        return ""
    cleaned = _CONTROL_CHARS.sub("", str(value))
    return cleaned[:max_length]
