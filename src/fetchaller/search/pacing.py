"""Shared parsing for provider-request cooldowns."""

import math
import time
from email.utils import parsedate_to_datetime


def retry_after_seconds(value: str | None) -> float:
    """Read Retry-After without accepting non-finite or malformed durations."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0.0
