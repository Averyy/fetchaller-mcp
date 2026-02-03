"""URL normalization for consistent caching."""

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

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
    - ?b=2&a=1 and ?a=1&b=2 -> same cache key
    - Reduces redundant fetches
    """
    parsed = urlparse(url)

    # Lowercase scheme and host
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()

    # Remove default ports
    if host.endswith(":443") and scheme == "https":
        host = host[:-4]
    elif host.endswith(":80") and scheme == "http":
        host = host[:-3]

    # Parse and filter query params
    params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {k: v for k, v in params.items() if k.lower() not in TRACKING_PARAMS}

    # Sort params for consistency
    sorted_query = urlencode(sorted(filtered.items()), doseq=True)

    # Reconstruct without fragment
    return urlunparse(
        (
            scheme,
            host,
            parsed.path,  # Keep path case (some servers are case-sensitive)
            "",  # params (rarely used)
            sorted_query,
            "",  # Remove fragment
        )
    )
