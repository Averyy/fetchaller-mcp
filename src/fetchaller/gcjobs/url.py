"""URL recognition for GC Jobs, so ``fetch`` can route a board URL here."""

from __future__ import annotations

import urllib.parse

HOST = "emploisfp-psjobs.cfp-psc.gc.ca"
SEARCH_PATH = "/psrs-srfp/applicant/page2440"
POSTING_PATH = "/psrs-srfp/applicant/page1800"


def _parts(url: str):
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    if host != HOST and host != f"www.{HOST}":
        return None
    # The app appends ``;jsessionid=…`` to its own paths; that is session
    # scratch, not identity.
    path = parsed.path.split(";", 1)[0].rstrip("/")
    return path, urllib.parse.parse_qs(parsed.query)


def extract_gcjobs_poster(url: str) -> str:
    """The numeric poster id of a posting URL, or ``""``."""
    parts = _parts(url)
    if parts is None or parts[0] != POSTING_PATH:
        return ""
    values = parts[1].get("poster") or []
    value = (values[0] if values else "").strip()
    return value if value.isdigit() else ""


def is_gcjobs_search_url(url: str) -> bool:
    parts = _parts(url)
    return parts is not None and parts[0] == SEARCH_PATH


def gcjobs_search_criteria(url: str) -> dict[str, str]:
    """The criteria a search URL carries that this client can honour.

    Only ``title`` is lifted. The board's own location input is a coded
    ``addedLocation=W232`` and a caller pasting a URL is far more likely to
    hold the plain page than a filtered one.
    """
    parts = _parts(url)
    if parts is None:
        return {}
    titles = parts[1].get("title") or []
    return {"title": " ".join((titles[0] if titles else "").split())}
