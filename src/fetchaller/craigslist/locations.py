"""Craigslist location resolution.

Maps city name strings to Craigslist hostnames (e.g. ``"toronto"`` →
``"toronto.craigslist.org"``).  Covers ~50 Canadian cities and ~100 US
cities, with common aliases and fuzzy matching.

Function: ``resolve_hostname(city_str) -> str | None``
"""

from __future__ import annotations

import re
from difflib import get_close_matches

# ---------------------------------------------------------------------------
# Canada: city/alias → CL subdomain
# Sourced from geo.craigslist.org/iso/ca
# ---------------------------------------------------------------------------

_CANADA: dict[str, str] = {
    # Alberta
    "CALGARY": "calgary",
    "EDMONTON": "edmonton",
    "FORT MCMURRAY": "ftmcmurray",
    "LETHBRIDGE": "lethbridge",
    "MEDICINE HAT": "hat",
    "RED DEER": "reddeer",
    "GRANDE PRAIRIE": "grandeprairie",
    # British Columbia
    "VANCOUVER": "vancouver",
    "VICTORIA": "victoria",
    "KELOWNA": "kelowna",
    "KAMLOOPS": "kamloops",
    "NANAIMO": "nanaimo",
    "PRINCE GEORGE": "princegeorge",
    "CRANBROOK": "cranbrook",
    "COMOX": "comoxvalley",
    "COURTENAY": "comoxvalley",
    "CARIBOO": "cariboo",
    "SUNSHINE COAST": "sunshine",
    "WHISTLER": "whistler",
    "ABBOTSFORD": "abbotsford",
    "CHILLIWACK": "abbotsford",
    # Manitoba
    "WINNIPEG": "winnipeg",
    "BRANDON": "brandon",
    # New Brunswick
    "FREDERICTON": "newbrunswick",
    "MONCTON": "moncton",
    "SAINT JOHN": "saintjohn",
    # Newfoundland
    "ST JOHNS": "newfoundland",
    "ST JOHNS NL": "newfoundland",
    "CORNER BROOK": "newfoundland",
    # Nova Scotia
    "HALIFAX": "halifax",
    "CAPE BRETON": "capebreton",
    # Ontario
    "TORONTO": "toronto",
    "OTTAWA": "ottawa",
    "HAMILTON": "hamilton",
    "KITCHENER": "kitchener",
    "WATERLOO": "kitchener",
    "CAMBRIDGE": "kitchener",
    "LONDON": "londonon",
    "WINDSOR": "windsor",
    "BARRIE": "barrie",
    "KINGSTON": "kingston",
    "SUDBURY": "sudbury",
    "THUNDER BAY": "thunderbay",
    "BELLEVILLE": "belleville",
    "BRANTFORD": "brantford",
    "CHATHAM": "chatham",
    "CHATHAM KENT": "chatham",
    "CORNWALL": "cornwall",
    "GUELPH": "guelph",
    "NIAGARA": "hamilton",
    "NIAGARA FALLS": "hamilton",
    "NIAGARA ON THE LAKE": "hamilton",
    "ST CATHARINES": "hamilton",
    "WELLAND": "hamilton",
    "BURLINGTON": "hamilton",
    "OSHAWA": "toronto",
    "MARKHAM": "toronto",
    "MISSISSAUGA": "toronto",
    "BRAMPTON": "toronto",
    "PETERBOROUGH": "peterborough",
    "SARNIA": "sarnia",
    "SAULT STE MARIE": "soo",
    "NORTH BAY": "northbay",
    "OWEN SOUND": "owensound",
    # PEI
    "CHARLOTTETOWN": "pei",
    # Quebec
    "MONTREAL": "montreal",
    "QUEBEC CITY": "quebec",
    "SHERBROOKE": "sherbrooke",
    "TROIS RIVIERES": "troisrivieres",
    "SAGUENAY": "saguenay",
    "GATINEAU": "ottawa",
    # Saskatchewan
    "SASKATOON": "saskatoon",
    "REGINA": "regina",
    "PRINCE ALBERT": "princealbert",
    "SWIFT CURRENT": "swiftcurrent",
    # Territories
    "WHITEHORSE": "whitehorse",
    "YELLOWKNIFE": "yellowknife",
    "IQALUIT": "territories",
}

# ---------------------------------------------------------------------------
# US: city/alias → CL subdomain
# Top ~100 metro areas
# ---------------------------------------------------------------------------

_US: dict[str, str] = {
    # Northeast
    "NEW YORK": "newyork",
    "NEW YORK CITY": "newyork",
    "NYC": "newyork",
    "MANHATTAN": "newyork",
    "BROOKLYN": "newyork",
    "QUEENS": "newyork",
    "BRONX": "newyork",
    "LONG ISLAND": "longisland",
    "JERSEY CITY": "jerseyshore",
    "NEWARK": "newjersey",
    "PHILADELPHIA": "philadelphia",
    "PHILLY": "philadelphia",
    "BOSTON": "boston",
    "PITTSBURGH": "pittsburgh",
    "HARTFORD": "hartford",
    "PROVIDENCE": "providence",
    "BUFFALO": "buffalo",
    "ROCHESTER NY": "rochester",
    "SYRACUSE": "syracuse",
    "ALBANY": "albany",
    "NEW HAVEN": "newhaven",
    # Southeast
    "MIAMI": "miami",
    "FORT LAUDERDALE": "fortlauderdale",
    "WEST PALM BEACH": "palmbeach",
    "ORLANDO": "orlando",
    "TAMPA": "tampa",
    "JACKSONVILLE": "jacksonville",
    "ATLANTA": "atlanta",
    "CHARLOTTE": "charlotte",
    "RALEIGH": "raleigh",
    "DURHAM": "raleigh",
    "RICHMOND": "richmond",
    "VIRGINIA BEACH": "norfolk",
    "NORFOLK": "norfolk",
    "NASHVILLE": "nashville",
    "MEMPHIS": "memphis",
    "LOUISVILLE": "louisville",
    "BIRMINGHAM": "bham",
    "NEW ORLEANS": "neworleans",
    "CHARLESTON SC": "charleston",
    "COLUMBIA SC": "columbia",
    "SAVANNAH": "savannah",
    "KNOXVILLE": "knoxville",
    "GREENVILLE SC": "greenville",
    # Midwest
    "CHICAGO": "chicago",
    "DETROIT": "detroit",
    "MINNEAPOLIS": "minneapolis",
    "ST PAUL": "minneapolis",
    "MILWAUKEE": "milwaukee",
    "COLUMBUS": "columbus",
    "INDIANAPOLIS": "indianapolis",
    "INDY": "indianapolis",
    "CINCINNATI": "cincinnati",
    "CLEVELAND": "cleveland",
    "KANSAS CITY": "kansascity",
    "ST LOUIS": "stlouis",
    "DES MOINES": "desmoines",
    "OMAHA": "omaha",
    "MADISON": "madison",
    "GRAND RAPIDS": "grandrapids",
    "ANN ARBOR": "annarbor",
    "DAYTON": "dayton",
    "AKRON": "akroncanton",
    # Southwest
    "HOUSTON": "houston",
    "DALLAS": "dallas",
    "FORT WORTH": "dallas",
    "SAN ANTONIO": "sanantonio",
    "AUSTIN": "austin",
    "PHOENIX": "phoenix",
    "SCOTTSDALE": "phoenix",
    "MESA": "phoenix",
    "TUCSON": "tucson",
    "ALBUQUERQUE": "albuquerque",
    "EL PASO": "elpaso",
    "LAS VEGAS": "lasvegas",
    "VEGAS": "lasvegas",
    "OKLAHOMA CITY": "oklahomacity",
    "TULSA": "tulsa",
    # West
    "LOS ANGELES": "losangeles",
    "LA": "losangeles",
    "SAN FRANCISCO": "sfbay",
    "SF": "sfbay",
    "SAN JOSE": "sfbay",
    "OAKLAND": "sfbay",
    "BAY AREA": "sfbay",
    "SAN DIEGO": "sandiego",
    "SEATTLE": "seattle",
    "TACOMA": "seattle",
    "PORTLAND": "portland",
    "PORTLAND OR": "portland",
    "SACRAMENTO": "sacramento",
    "DENVER": "denver",
    "BOULDER": "boulder",
    "COLORADO SPRINGS": "cosprings",
    "SALT LAKE CITY": "saltlakecity",
    "SLC": "saltlakecity",
    "BOISE": "boise",
    "HONOLULU": "honolulu",
    "HAWAII": "honolulu",
    "ANCHORAGE": "anchorage",
    "FRESNO": "fresno",
    "BAKERSFIELD": "bakersfield",
    "RIVERSIDE": "inlandempire",
    "INLAND EMPIRE": "inlandempire",
    "ORANGE COUNTY": "orangecounty",
    "VENTURA": "ventura",
    "SANTA BARBARA": "santabarbara",
    "RENO": "reno",
    "SPOKANE": "spokane",
    "EUGENE": "eugene",
    "SALEM OR": "salem",
    "STOCKTON": "stockton",
    "MODESTO": "modesto",
    "SANTA CRUZ": "santacruz",
}

# ---------------------------------------------------------------------------
# Build flat lookup + fuzzy match index
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """Normalize a location name for matching."""
    name = name.upper().strip()
    name = name.replace(".", "").replace("-", " ").replace("'", "")
    name = re.sub(r"\s+", " ", name)
    return name


# Merge Canada + US into one dict
_ALL_LOCATIONS: dict[str, str] = {}
for _src in (_CANADA, _US):
    for _city, _subdomain in _src.items():
        _ALL_LOCATIONS[_normalize(_city)] = _subdomain

_ALL_NAMES: list[str] = list(_ALL_LOCATIONS.keys())

# Track which cities are Canadian for Kijiji-skip logic
_CANADIAN_CITIES: frozenset[str] = frozenset(_normalize(c) for c in _CANADA)


def resolve_hostname(location_str: str) -> str | None:
    """Resolve a location string to a Craigslist hostname.

    Supports:
    - Exact city names: "toronto", "St. Catharines"
    - City + province/state: "vancouver, BC", "austin, TX"
    - Common aliases: "SF" → "sfbay", "niagara" → "hamilton"
    - Fuzzy matching: "st cathrines" → "hamilton"

    Returns:
        Full hostname (e.g. "toronto.craigslist.org") or None if no match.
    """
    key = _resolve_key(location_str)
    if key is None:
        return None
    return f"{_ALL_LOCATIONS[key]}.craigslist.org"


def _resolve_key(location_str: str) -> str | None:
    """Resolve a location string to its normalized key in _ALL_LOCATIONS.

    Shared logic for both resolve_hostname and is_canadian_location to ensure
    they always resolve to the same city.
    """
    norm = _normalize(location_str)

    if "," in norm:
        city_part = norm.split(",", 1)[0].strip()
        if norm in _ALL_LOCATIONS:
            return norm
        if city_part in _ALL_LOCATIONS:
            return city_part
        norm = city_part

    if norm in _ALL_LOCATIONS:
        return norm

    matches = get_close_matches(norm, _ALL_NAMES, n=1, cutoff=0.7)
    if matches:
        return matches[0]

    return None


def is_canadian_location(location_str: str) -> bool:
    """Check if a location string resolves to a Canadian city.

    Used to determine whether to include Kijiji in platform search
    (Kijiji is Canada-only). Uses the same resolution logic as
    resolve_hostname to ensure consistent results.
    """
    key = _resolve_key(location_str)
    return key is not None and key in _CANADIAN_CITIES
