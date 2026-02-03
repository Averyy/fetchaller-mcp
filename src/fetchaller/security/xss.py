"""XSS prevention utilities."""

import html
import re


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
    # Remove control characters (0x00-0x1f and 0x7f)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", str(value))
    return cleaned[:max_length]
