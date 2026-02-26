"""Kijiji location resolution.

Maps city name strings to Kijiji location codes (e.g. ``l80016`` for
St. Catharines).  Location data sourced from kijiji-scraper's ``locations.ts``.

The Kijiji GraphQL API's ``placeSuggestions`` query requires authentication,
so we use a static map with fuzzy matching instead.

URL format: ``/b-{category}/{slug}/{query}/k0{cat_code}l{location_id}``
The slug is cosmetic — only the ``l{id}`` code matters for filtering.
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Flat location map: normalized name → (location_id, display_name)
#
# Province-level entries use the province name as key.
# City-level entries use "city, province" as key (and also just "city"
# when the city name is unambiguous across provinces).
# ---------------------------------------------------------------------------

# Raw tree from kijiji-scraper/lib/locations.ts
_LOCATION_TREE: dict = {
    "ALBERTA": {
        "id": 9003,
        "cities": {
            "BANFF": 1700234, "CANMORE": 1700234,
            "CALGARY": 1700199,
            "EDMONTON": 1700203, "ST ALBERT": 1700205,
            "STRATHCONA COUNTY": 1700204,
            "FORT MCMURRAY": 1700232,
            "GRANDE PRAIRIE": 1700233,
            "LETHBRIDGE": 1700230,
            "LLOYDMINSTER": 1700095,
            "MEDICINE HAT": 1700231,
            "RED DEER": 1700136,
        },
    },
    "BRITISH COLUMBIA": {
        "id": 9007,
        "cities": {
            "HUNDRED MILE HOUSE": 1700307, "QUESNEL": 1700306,
            "WILLIAMS LAKE": 1700305,
            "CAMPBELL RIVER": 1700316,
            "COMOX": 1700315, "COURTENAY": 1700315, "CUMBERLAND": 1700315,
            "COWICHAN VALLEY": 1700300, "DUNCAN": 1700300,
            "CRANBROOK": 1700224,
            "ABBOTSFORD": 1700140, "CHILLIWACK": 1700141,
            "HOPE": 1700320, "MISSION": 1700319,
            "BURNABY": 1700286, "NEW WESTMINSTER": 1700286,
            "SURREY": 1700285, "LANGLEY": 1700285, "DELTA": 1700285,
            "NORTH VANCOUVER": 1700289, "WEST VANCOUVER": 1700289,
            "RICHMOND": 1700288,
            "COQUITLAM": 1700290, "PORT COQUITLAM": 1700290,
            "PORT MOODY": 1700290, "PITT MEADOWS": 1700290,
            "MAPLE RIDGE": 1700290,
            "VANCOUVER": 80003,  # Greater Vancouver Area (metro)
            "KAMLOOPS": 1700227,
            "KELOWNA": 1700228, "PENTICTON": 1700246,
            "NANAIMO": 1700263,
            "NELSON": 1700226,
            "DAWSON CREEK": 1700304, "FORT ST JOHN": 1700303,
            "PARKSVILLE": 1700317, "QUALICUM BEACH": 1700317,
            "PORT ALBERNI": 1700318,
            "PORT HARDY": 1700301, "PORT MCNEILL": 1700301,
            "POWELL RIVER": 1700294,
            "PRINCE GEORGE": 1700143,
            "REVELSTOKE": 1700302,
            "BURNS LAKE": 1700314, "HOUSTON": 1700313,
            "KITIMAT": 1700310, "PRINCE RUPERT": 1700308,
            "SMITHERS": 1700311, "TERRACE": 1700309,
            "VANDERHOOF": 1700312,
            "SUNSHINE COAST": 1700293,
            "VERNON": 1700229,
            "VICTORIA": 1700173,
            "WHISTLER": 1700100,
        },
    },
    "MANITOBA": {
        "id": 9006,
        "cities": {
            "BRANDON": 1700086,
            "PORTAGE LA PRAIRIE": 1700087,
            "FLIN FLON": 1700236,
            "THOMPSON": 1700235,
            "WINNIPEG": 1700192,
        },
    },
    "NEW BRUNSWICK": {
        "id": 9005,
        "cities": {
            "BATHURST": 1700260,
            "EDMUNDSTON": 1700261,
            "FREDERICTON": 1700018,
            "MIRAMICHI": 1700262,
            "MONCTON": 1700001,
            "SAINT JOHN": 80017,
        },
    },
    "NEWFOUNDLAND": {
        "id": 9008,
        "cities": {
            "CORNER BROOK": 1700254,
            "GANDER": 1700255,
            "GOOSE BAY": 1700045,
            "LABRADOR CITY": 1700046,
            "ST JOHNS": 1700113,
        },
    },
    "NOVA SCOTIA": {
        "id": 9002,
        "cities": {
            "ANNAPOLIS VALLEY": 1700256,
            "BRIDGEWATER": 1700257,
            "CAPE BRETON": 1700011,
            "HALIFAX": 80010, "BEDFORD": 1700107,
            "COLE HARBOUR": 1700108, "DARTMOUTH": 1700109,
            "NEW GLASGOW": 1700258,
            "TRURO": 1700047,
            "YARMOUTH": 1700259,
        },
    },
    "ONTARIO": {
        "id": 9004,
        "cities": {
            "BARRIE": 1700006,
            "BELLEVILLE": 1700130, "TRENTON": 1700132,
            "BRANTFORD": 1700206,
            "BROCKVILLE": 1700247,
            "CHATHAM": 1700239, "CHATHAM KENT": 1700239,
            "CORNWALL": 1700133,
            "GUELPH": 1700242,
            "HAMILTON": 80014,
            "KAPUSKASING": 1700237,
            "KENORA": 1700249,
            "KINGSTON": 1700183, "NAPANEE": 1700182,
            "CAMBRIDGE": 1700210,
            "KITCHENER": 1700212, "WATERLOO": 1700212,
            "STRATFORD": 1700213,
            "LEAMINGTON": 1700240,
            "LONDON": 1700214,
            "MUSKOKA": 1700078,
            "NORFOLK COUNTY": 1700248,
            "NORTH BAY": 1700243,
            "GATINEAU": 1700186, "OTTAWA": 1700185,
            "OWEN SOUND": 1700187,
            "KAWARTHA LAKES": 1700219,
            "PETERBOROUGH": 1700218,
            "PEMBROKE": 1700075, "PETAWAWA": 1700076,
            "RENFREW": 1700077,
            "GRAND BEND": 1700190, "SARNIA": 1700191,
            "SAULT STE MARIE": 1700244,
            "ST CATHARINES": 80016, "NIAGARA": 80016,
            "NIAGARA FALLS": 80016, "NIAGARA ON THE LAKE": 80016,
            "WELLAND": 80016,
            "SUDBURY": 1700245,
            "THUNDER BAY": 1700126,
            "TIMMINS": 1700238,
            "TORONTO": 1700272,  # Greater Toronto Area (metro)
            "MARKHAM": 1700274, "YORK REGION": 1700274,
            "MISSISSAUGA": 1700276, "BRAMPTON": 1700276,
            "PEEL REGION": 1700276,
            "OAKVILLE": 1700277, "BURLINGTON": 1700277,
            "HALTON": 1700277,
            "OSHAWA": 1700275, "DURHAM REGION": 1700275,
            "AJAX": 1700275, "WHITBY": 1700275, "PICKERING": 1700275,
            "WINDSOR": 1700220,
            "WOODSTOCK": 1700241,
        },
    },
    "PRINCE EDWARD ISLAND": {
        "id": 9011,
        "cities": {
            "CHARLOTTETOWN": 1700119,
            "SUMMERSIDE": 1700120,
        },
    },
    "QUEBEC": {
        "id": 9001,
        "cities": {
            "ROUYN NORANDA": 1700060, "VAL DOR": 1700061,
            "BAIE COMEAU": 1700251,
            "DRUMMONDVILLE": 1700122, "VICTORIAVILLE": 1700123,
            "LEVIS": 1700063,
            "ST GEORGES DE BEAUCE": 1700065,
            "THETFORD MINES": 1700064,
            "GASPE": 1700066,
            "GRANBY": 1700253,
            "MONTREAL": 80002,  # Greater Montreal Area (metro)
            "LAVAL": 1700278,
            "LONGUEUIL": 1700279,
            "WEST ISLAND": 1700280,
            "TROIS RIVIERES": 1700150, "SHAWINIGAN": 1700148,
            "QUEBEC CITY": 1700124,
            "RIMOUSKI": 1700250,
            "SAGUENAY": 1700179, "LAC SAINT JEAN": 1700180,
            "SAINT HYACINTHE": 1700151,
            "SAINT JEAN SUR RICHELIEU": 1700252,
            "SEPT ILES": 1700071,
            "SHERBROOKE": 1700156,
        },
    },
    "SASKATCHEWAN": {
        "id": 9009,
        "cities": {
            "LA RONGE": 1700265,
            "MEADOW LAKE": 1700264,
            "NIPAWIN": 1700266,
            "PRINCE ALBERT": 1700088,
            "MOOSE JAW": 1700195,
            "REGINA": 1700196,
            "SASKATOON": 1700197,
            "SWIFT CURRENT": 1700093,
        },
    },
    "TERRITORIES": {
        "id": 9010,
        "cities": {
            "YELLOWKNIFE": 1700104,
            "IQALUIT": 1700106,
            "WHITEHORSE": 1700102,
        },
    },
}

# Province abbreviations → full name
_PROVINCE_ABBREV: dict[str, str] = {
    "AB": "ALBERTA", "BC": "BRITISH COLUMBIA", "MB": "MANITOBA",
    "NB": "NEW BRUNSWICK", "NL": "NEWFOUNDLAND", "NS": "NOVA SCOTIA",
    "NT": "TERRITORIES", "NU": "TERRITORIES", "YT": "TERRITORIES",
    "ON": "ONTARIO", "PE": "PRINCE EDWARD ISLAND", "PEI": "PRINCE EDWARD ISLAND",
    "QC": "QUEBEC", "SK": "SASKATCHEWAN",
}

# Build flat lookup: normalized_name → (location_id, display_name)
_LOCATIONS: dict[str, tuple[int, str]] = {}
# Also track city→province for disambiguation
_CITY_PROVINCES: dict[str, list[str]] = {}


def _normalize(name: str) -> str:
    """Normalize a location name for matching."""
    name = name.upper().strip()
    # Normalize punctuation: "ST." → "ST", hyphens → spaces
    name = name.replace(".", "").replace("-", " ").replace("'", "")
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    return name


def _build_lookup() -> None:
    """Build the flat lookup dict from the tree (called once at import)."""
    for prov_name, prov_data in _LOCATION_TREE.items():
        prov_id = prov_data["id"]
        norm_prov = _normalize(prov_name)

        # Province-level entry
        _LOCATIONS[norm_prov] = (prov_id, prov_name.title())

        # Province abbreviation entries
        for abbrev, full in _PROVINCE_ABBREV.items():
            if _normalize(full) == norm_prov:
                _LOCATIONS[abbrev] = (prov_id, prov_name.title())

        # City-level entries
        for city_name, city_id in prov_data.get("cities", {}).items():
            norm_city = _normalize(city_name)
            display = city_name.title()

            # "city, province" is always unambiguous
            _LOCATIONS[f"{norm_city}, {norm_prov}"] = (city_id, display)

            # Track which provinces have this city name
            _CITY_PROVINCES.setdefault(norm_city, []).append(norm_prov)

            # Bare city name — add if unambiguous or first occurrence
            if norm_city not in _LOCATIONS:
                _LOCATIONS[norm_city] = (city_id, display)


_build_lookup()

# All normalized names for fuzzy matching
_ALL_NAMES: list[str] = list(_LOCATIONS.keys())


def resolve_location(location_str: str) -> tuple[int, str] | None:
    """Resolve a location string to a Kijiji (location_id, display_name).

    Supports:
    - Exact city names: "toronto", "St. Catharines"
    - City + province: "vancouver, BC", "ottawa, ontario"
    - Province names: "Ontario", "BC"
    - Fuzzy matching: "st cathrines" → "St Catharines"

    Returns:
        (location_id, display_name) tuple, or None if no match found.
    """
    norm = _normalize(location_str)

    # Try exact match first
    if norm in _LOCATIONS:
        return _LOCATIONS[norm]

    # Try "city, province" by splitting on comma
    if "," in norm:
        parts = [p.strip() for p in norm.split(",", 1)]
        city_part, prov_part = parts[0], parts[1]

        # Resolve province abbreviation
        resolved_prov = _PROVINCE_ABBREV.get(prov_part, prov_part)

        key = f"{city_part}, {resolved_prov}"
        if key in _LOCATIONS:
            return _LOCATIONS[key]

        # Try just the city part
        if city_part in _LOCATIONS:
            return _LOCATIONS[city_part]

    # Fuzzy match — find closest name
    matches = get_close_matches(norm, _ALL_NAMES, n=1, cutoff=0.7)
    if matches:
        return _LOCATIONS[matches[0]]

    return None


def build_search_url(
    query: str,
    location_id: int,
    category_code: str = "c10",
    sort: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    condition: str | None = None,
) -> str:
    """Build a Kijiji search URL from parameters.

    Args:
        query: Search keywords.
        location_id: Kijiji location ID (from resolve_location).
        category_code: Category code (e.g. "c174" for cars, "c10" for all).
        sort: Sort param value (e.g. "dateDesc", "priceAsc").
        min_price: Minimum price in dollars.
        max_price: Maximum price in dollars.
        condition: Condition filter (e.g. "new__used___like_new").

    Returns:
        Full Kijiji search URL.
    """
    # Slugify the query for the URL path
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "search"

    path = f"/b-buy-sell/k/{slug}/k0{category_code}l{location_id}"

    params: dict[str, str | int] = {}
    if sort:
        params["sortByName"] = sort
    if min_price is not None:
        params["minPrice"] = min_price
    if max_price is not None:
        params["maxPrice"] = max_price
    if condition:
        params["condition"] = condition

    url = f"https://www.kijiji.ca{path}"
    if params:
        url += "?" + urlencode(params)

    return url
