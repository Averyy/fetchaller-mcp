"""Shared JSON extraction via string-aware brace counting.

Used by site-specific modules that need to extract JSON objects from
embedded JavaScript in SSR HTML pages (Alibaba, AliExpress).
"""

import json


def extract_json_object(html: str, start: int, max_scan: int = 5_000_000) -> dict | None:
    """Extract a JSON object from HTML using string-aware brace counting.

    Args:
        html: The HTML string containing embedded JSON.
        start: Index of the opening '{' character.
        max_scan: Maximum characters to scan from start.

    Returns:
        Parsed dict, or None if no valid JSON found.
    """
    depth = 0
    in_string = False
    escape_next = False
    end = min(start + max_scan, len(html))
    for i in range(start, end):
        ch = html[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
