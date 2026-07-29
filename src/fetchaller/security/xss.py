"""XSS prevention utilities."""

import html
import re
from urllib.parse import urlsplit

# Pre-compiled regex for control character removal (hot path)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_URL_IN_LOG = re.compile(r"https?://[^\s)\]}>'\"]+")
_SECRET_FIELD = re.compile(
    r"(?i)(api[_-]?key|token|secret|authorization|code)=([^&\s]+)"
)


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


def redact_secrets_for_log(value: object) -> str:
    """Remove URL credentials, paths, queries, and named secrets from logs."""

    text = _CONTROL_CHARS.sub("", str(value))

    def redact_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group(0))
            host = parsed.hostname or "[invalid-host]"
            if parsed.port:
                host = f"{host}:{parsed.port}"
            suffix = "/…" if parsed.path not in {"", "/"} else "/"
            return f"{parsed.scheme}://{host}{suffix}"
        except ValueError:
            return "[redacted-url]"

    text = _URL_IN_LOG.sub(redact_url, text)
    return _SECRET_FIELD.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )


def safe_log_text(value: object, max_length: int = 500) -> str:
    """Return a single-line, bounded diagnostic with URL secrets removed."""

    single_line = " ".join(str(value).splitlines())
    return sanitize_for_log(
        redact_secrets_for_log(single_line),
        max_length=max_length,
    )
