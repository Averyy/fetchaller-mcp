"""URL normalization for consistent caching."""

from urllib.parse import unquote_plus, urlparse, urlunparse

from ..config import TRACKING_PARAMS


def normalize_url(url: str) -> str:
    """
    Canonicalize URL for consistent caching.

    Transforms:
    - https://EXAMPLE.COM/Page?utm_source=x&b=2&a=1#section
    To:
    - https://example.com/Page?a=1&b=2

    Benefits:
    - Same page with different tracking params -> single cache entry
    - Query order stays intact because some origins assign it meaning
    - Reduces redundant fetches
    """
    parsed = urlparse(url)
    query_was_present = "?" in url.split("#", 1)[0]

    # Lowercase only the scheme and hostname. Userinfo is case-sensitive and
    # must never collide merely because it appeared in the same netloc.
    scheme = parsed.scheme.lower()
    userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    hostname = (parsed.hostname or "").lower()
    host_display = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    host = f"{userinfo}{host_display}" + (f":{port}" if port is not None else "")

    # Parse and filter query params
    # Remove tracking components without parsing/re-encoding anything retained.
    # Raw spelling is part of the cache identity: origins/signature validators
    # may distinguish ``?flag`` from ``?flag=``, ``%20`` from ``+``, escape
    # case, and arbitrary component order.
    retained_segments = []
    for segment in parsed.query.split("&"):
        raw_key = segment.partition("=")[0]
        decoded_key = unquote_plus(raw_key)
        if decoded_key.lower() not in TRACKING_PARAMS:
            retained_segments.append(segment)
    normalized_query = "&".join(retained_segments)

    # Reconstruct without fragment
    normalized = urlunparse(
        (
            scheme,
            host,
            parsed.path,  # Keep path case (some servers are case-sensitive)
            parsed.params,  # Path parameters are part of the resource identity
            normalized_query,
            "",  # Remove fragment
        )
    )
    if query_was_present and not normalized_query:
        normalized += "?"
    return normalized
