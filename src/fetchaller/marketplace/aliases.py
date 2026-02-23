"""Cross-platform filter aliases for marketplace search.

Maps human-readable filter values to platform-specific API values.
When a platform's value is ``None``, the filter is not applied on that
platform (falls back to keyword filtering).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Sort aliases
# ---------------------------------------------------------------------------

SORT_MAP: dict[str, dict[str, str | None]] = {
    "date": {
        "craigslist": "date",
        "kijiji": "dateDesc",
        "facebook": "CREATION_TIME_DESCEND",
    },
    "price_asc": {
        "craigslist": "priceasc",
        "kijiji": "priceAsc",
        "facebook": "PRICE_ASCEND",
    },
    "price_desc": {
        "craigslist": "pricedsc",
        "kijiji": "priceDesc",
        "facebook": "PRICE_DESCEND",
    },
    "relevance": {
        "craigslist": "relevance",
        "kijiji": None,  # default (omit param)
        "facebook": "BEST_MATCH",
    },
}

# ---------------------------------------------------------------------------
# Category aliases
# ---------------------------------------------------------------------------

CATEGORY_MAP: dict[str, dict[str, str | None]] = {
    "all": {
        "craigslist": "sss",
        "kijiji": "c10",
        "facebook": None,
    },
    "cars": {
        "craigslist": "cta",
        "kijiji": "c174",
        "facebook": "vehicles",
    },
    "electronics": {
        "craigslist": "ela",
        "kijiji": "c12",
        "facebook": "electronics",
    },
    "furniture": {
        "craigslist": "fua",
        "kijiji": "c235",
        "facebook": None,
    },
    "clothing": {
        "craigslist": "cla",
        "kijiji": None,
        "facebook": "apparel",
    },
    "tools": {
        "craigslist": "tla",
        "kijiji": "c110",
        "facebook": None,
    },
    "free": {
        "craigslist": "zip",
        "kijiji": "c17220001",
        "facebook": None,
    },
    "bikes": {
        "craigslist": "bia",
        "kijiji": None,
        "facebook": None,
    },
    "phones": {
        "craigslist": "moa",
        "kijiji": None,
        "facebook": None,
    },
    "motorcycles": {
        "craigslist": "mca",
        "kijiji": "c30",
        "facebook": "vehicles",
    },
    "boats": {
        "craigslist": "boo",
        "kijiji": "c29",
        "facebook": None,
    },
    "rvs": {
        "craigslist": "rva",
        "kijiji": "c172",
        "facebook": None,
    },
    "auto_parts": {
        "craigslist": "pta",
        "kijiji": "c31",
        "facebook": None,
    },
    "sporting": {
        "craigslist": "sga",
        "kijiji": None,
        "facebook": None,
    },
    "toys": {
        "craigslist": "taa",
        "kijiji": None,
        "facebook": None,
    },
    "baby": {
        "craigslist": "baa",
        "kijiji": None,
        "facebook": None,
    },
}

# ---------------------------------------------------------------------------
# Condition aliases
# ---------------------------------------------------------------------------

CONDITION_MAP: dict[str, dict[str, str | None]] = {
    "new": {
        "craigslist": "10",
        "kijiji": "new",
        "facebook": "new",
    },
    "like_new": {
        "craigslist": "20",
        "kijiji": "used___like_new",
        "facebook": "used_like_new",
    },
    "good": {
        "craigslist": "40",
        "kijiji": "used___good",
        "facebook": "used_good",
    },
    "fair": {
        "craigslist": "50",
        "kijiji": "used___fair",
        "facebook": "used_fair",
    },
}


def resolve_alias(
    value: str | None,
    alias_map: dict[str, dict[str, str | None]],
    platform: str,
) -> str | None:
    """Look up a platform-specific value from an alias map.

    Returns None if the alias is not in the map or the platform has no
    mapping for it.
    """
    if value is None:
        return None
    entry = alias_map.get(value)
    if entry is None:
        return None
    return entry.get(platform)
