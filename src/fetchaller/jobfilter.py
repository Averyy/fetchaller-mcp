"""Shared matching helpers for job boards.

Every board in this repo ranks rather than filters: a ``query`` reorders the
list but adjacent roles still come back, so a "product designer" search returns
engineering reqs and a location search returns the whole country. Callers mean
a title and a location as constraints, so these helpers re-apply them against
the board's own values.

Board vocabularies differ in punctuation and word order for the same place
("Canada, Toronto", "Canada - Toronto", "Canada Ontario Remote"), which is why
matching is token containment rather than string equality.

**Cross-client invariant:** a location the caller asked for always constrains
the result. When a board cannot resolve it — Meta answers an unknown office
with the *unfiltered* board, Workday may have no matching facet, Apple no
matching location code — the filter is applied here instead, so the caller gets
zero results and an explanation rather than a page of postings from elsewhere
under a heading naming the place they asked for.
"""

from __future__ import annotations

import re

# Words that carry no signal in a title or place name and would over-constrain.
_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "in", "of", "or", "the", "to", "with", "at", "on"}
)
_TOKEN_RE = re.compile(r"[a-z0-9+#]+")
# Below this length a prefix match is noise ("ai" would match "aid", "air").
_MIN_PREFIX = 4


def tokens(text: str) -> list[str]:
    """Lowercase word tokens, stopwords removed."""
    return [t for t in _TOKEN_RE.findall((text or "").casefold()) if t not in _STOPWORDS]


def _token_hit(wanted: str, have: list[str]) -> bool:
    for candidate in have:
        if wanted == candidate:
            return True
        shorter, longer = sorted((wanted, candidate), key=len)
        if len(shorter) >= _MIN_PREFIX and longer.startswith(shorter):
            return True
    return False


def title_matches(name: str, wanted: list[str]) -> bool:
    """True when every wanted token appears in the posting's own title.

    Tokens match on a shared prefix so "designer" matches "Design" and
    "engineering" matches "Engineer" — the same word in a different form is
    still the same role — but unrelated words are not.
    """
    if not wanted:
        return True
    have = tokens(name)
    if not have:
        return False
    return all(_token_hit(token, have) for token in wanted)


def location_matches(value: str, wanted: list[str]) -> bool:
    """True when every wanted token appears in a board's location value.

    Exact-ish by design: "Toronto" matches "Canada - Toronto" and "Canada,
    Toronto", and "Canada" matches both of those plus "Canada Remote".
    """
    if not wanted:
        return True
    have = tokens(value)
    if not have:
        return False
    return all(_token_hit(token, have) for token in wanted)


def filter_by_title(items, name_of, title: str):
    """Split ``items`` into (kept, dropped_count) by title match."""
    wanted = tokens(title)
    if not wanted:
        return list(items), 0
    kept = [item for item in items if title_matches(name_of(item) or "", wanted)]
    return kept, len(list(items)) - len(kept)


def counts_line(
    shown: int,
    *,
    dropped_by_title: int = 0,
    dropped_by_location: int = 0,
    board_total: int = 0,
    board_label: str = "The board",
    board_scope: str = "",
) -> list[str]:
    """The result-count preamble, as markdown lines.

    Two different numbers get reported and they count different things. The
    drop counts describe the postings this process actually examined, so
    ``shown + dropped`` always reconciles. ``board_total`` is the board's own
    figure for a query it *ranked* rather than filtered, and is a larger pool
    than the one examined whenever a page limit applied.

    Concatenating the two reads as arithmetic and produces nonsense — Microsoft
    reported "0 jobs shown of 33 matching; 34 dropped by the title filter",
    where 33 was the board's count for Canada and 34 was the number of postings
    fetched (the broadened-query retry adds postings past the first count). So
    the board's figure gets its own sentence, explicitly labelled as the
    board's and as loose.
    """
    plural = "" if shown == 1 else "s"
    dropped: list[tuple[int, str]] = []
    if dropped_by_title:
        dropped.append((dropped_by_title, "title"))
    if dropped_by_location:
        dropped.append((dropped_by_location, "location"))

    head = f"_{shown} job{plural} shown"
    if dropped:
        head += "; dropped " + " and ".join(f"{n} by {field}" for n, field in dropped)
    lines = [head + "_"]

    # Compared against `shown`, not against everything examined: a broadened
    # retry can fetch more postings than the board's own count for the original
    # query, and suppressing the board's figure whenever that happens would
    # drop it in precisely the case a caller most wants it — a search the board
    # says has results and the filter emptied.
    if board_total > shown:
        where = f" {board_scope}" if board_scope else ""
        if dropped:
            fields = " and ".join(field for _, field in dropped)
            lines.append("")
            lines.append(
                f"_{board_label} reported {board_total} loose "
                f"match{'' if board_total == 1 else 'es'}{where} for this query; the "
                f"count above is after re-checking each posting's own {fields}._"
            )
        else:
            lines.append("")
            lines.append(
                f"_{board_label} has {board_total}{where} for this query; "
                "raise `limit` to see more._"
            )
    return lines


# Suffixes stripped to widen a board-side query, longest first so "designers"
# loses "s" then "er" rather than stopping at "designer".
_SUFFIXES = ("ers", "ing", "er", "s")
_MIN_STEM = 4


def _stem(token: str) -> str:
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
                token = token[: -len(suffix)]
                changed = True
                break
    return token


# ISO-3166 alpha-3 codes, which is what amazon.jobs and uber.com both want.
# Only the countries these boards actually hire in are listed; an unknown name
# resolves to "" and the caller falls back to text matching.
COUNTRY_ALPHA3 = {
    "canada": "CAN",
    "united states": "USA",
    "united states of america": "USA",
    "usa": "USA",
    "us": "USA",
    "united kingdom": "GBR",
    "uk": "GBR",
    "india": "IND",
    "germany": "DEU",
    "ireland": "IRL",
    "australia": "AUS",
    "japan": "JPN",
    "mexico": "MEX",
    "brazil": "BRA",
    "spain": "ESP",
    "france": "FRA",
    "italy": "ITA",
    "poland": "POL",
    "netherlands": "NLD",
    "singapore": "SGP",
    "china": "CHN",
    "israel": "ISR",
    "korea": "KOR",
    "south korea": "KOR",
    "taiwan": "TWN",
    "sweden": "SWE",
    "switzerland": "CHE",
    "denmark": "DNK",
    "romania": "ROU",
    "portugal": "PRT",
    "argentina": "ARG",
    "colombia": "COL",
    "chile": "CHL",
    "new zealand": "NZL",
    "greece": "GRC",
    "thailand": "THA",
    "netherland": "NLD",
}


def country_alpha3(text: str) -> str:
    """Resolve a country name or code from free text, else ``""``.

    Accepts a bare name ("Canada"), an alpha-3 code ("CAN"), or a trailing
    country in a longer string ("Toronto, Ontario, Canada").
    """
    value = (text or "").strip().casefold()
    if not value:
        return ""
    if len(value) == 3 and value.isalpha() and value.upper() in set(COUNTRY_ALPHA3.values()):
        return value.upper()
    if value in COUNTRY_ALPHA3:
        return COUNTRY_ALPHA3[value]
    tail = value.rsplit(",", 1)[-1].strip()
    return COUNTRY_ALPHA3.get(tail, "")


def strip_country_tokens(wanted: list[str], alpha3: str) -> list[str]:
    """Drop country words once a board has already filtered on the country.

    Boards write the country as a code ("Vancouver, British Columbia, CAN"),
    so the caller's country word would never match and would reject every row.
    """
    if not alpha3:
        return wanted
    country_words = {alpha3.casefold()}
    for name, code in COUNTRY_ALPHA3.items():
        if code == alpha3:
            country_words.update(tokens(name))
    return [t for t in wanted if t not in country_words]


def broadened_query(title: str) -> str | None:
    """A stemmed spelling of ``title``, or None if stemming changes nothing.

    Job boards match query tokens literally, so "product designer" and
    "product design" are different searches — on Microsoft's board the first
    returns 6 hits and the second 864, including the "Director of Product
    Design" the first misses entirely. Callers run this as a *second* query and
    merge, rather than replacing the caller's wording: the original phrasing
    ranks best when it does match, and the stem is only there to recover the
    postings a literal match drops.
    """
    original = tokens(title)
    if not original:
        return None
    stemmed = [_stem(t) for t in original]
    if stemmed == original:
        return None
    return " ".join(stemmed)
