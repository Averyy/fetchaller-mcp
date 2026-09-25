"""Teamtailor career-site client and markdown renderer.

A Teamtailor career site lives at ``{slug}.teamtailor.com`` (or a regional
``{slug}.{region}.teamtailor.com``) or, just as often, on the company's own
domain (``careers.oatly.com``). The HTML job list shows 20 postings and pulls
the rest in through a Turbo Stream, carries no salary, and is mostly cookie
banner and filter facets, so a generic read of it looks complete and is not.

**Board** (``/jobs``, optionally locale-prefixed) is read from two public
feeds joined on the posting UUID (JSON ``id`` == RSS ``guid``), because
neither carries every field:

- ``/jobs.json`` (JSON Feed 1.1): numeric id, title, url, ``date_published``,
  ``validThrough`` and ``baseSalary`` — the employer's band, absent when they
  do not publish one.
- ``/jobs.rss``: department, role, location names and ``remoteStatus``.

Both carry every description, which is what makes them large.

Paging differs by mode, and that is the trap (measured 2026-09-25):

- **Unfiltered**, JSON pages at 100 (``per_page`` is capped there) and emits
  ``next_url``. RSS returns the first 100, ignores ``page`` entirely, and
  honours ``per_page`` above 100 — so it is asked for as many as JSON found.
- **With any board filter** (``query``, ``department``, ``location``,
  ``country``, ``remote_status_id``, ``employment_type``, ``language``), both
  feeds page at 20, ignore ``per_page``, emit **no** ``next_url``, and honour
  ``page=N``. A reader that trusts ``next_url`` stops at 20 without a sign.

The JSON-LD ``jobLocation`` address is the address on file for the location
record, usually an office — a fully remote "Canada - REMOTE" posting carries a
Calgary street address — so it is never rendered as where the job is. Location
is the location *name*, from RSS or the page.

``remoteStatus`` ``none`` is Teamtailor's default. The board's filter calls it
"No Remote Work", but the job page shows no remote status for it (while it does
show "Onsite" for ``onsite``), and employers leave it on postings whose
location reads "United States - Remote". The render keeps Teamtailor's enum
and glosses only that value, as ``none (not set)``, so it never reads as
on-site.

**Job page** (``/jobs/{id}-{slug}``) is rendered from the page itself — the
JSON-LD JobPosting, the header's remote-status badge and the page's labelled
facts — with no second request.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify

from ._isolated import IsolatedProcessingError, run_isolated

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)(?:\.[a-z]{2})?\.teamtailor\.com$")
# Teamtailor's own properties, which are not career sites. career.teamtailor.com
# is Teamtailor's own board and IS one, so it is deliberately absent.
_NON_TENANT_SLUGS = frozenset({
    "www", "app", "api", "support", "integrations", "status", "partner",
    "partners", "developer", "developers", "docs", "help", "cdn", "images",
    "assets",
})
_LOCALE = r"[A-Za-z]{2}(?:-[A-Za-z0-9]{2,4})?"
_BOARD_PATH_RE = re.compile(rf"^(/{_LOCALE})?/jobs/?$")
_JOB_PATH_RE = re.compile(rf"^(?:/{_LOCALE})?/jobs/(\d+)(?:-[^/]*)?/?$")


def _host_and_path(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        return (parsed.hostname or "").lower(), parsed.path or "/"
    except Exception:
        return None


def is_teamtailor_host(host: str) -> bool:
    m = _HOST_RE.match((host or "").lower())
    return bool(m) and m.group(1) not in _NON_TENANT_SLUGS


def is_teamtailor_board_path(url: str) -> bool:
    """``/jobs`` or ``/{locale}/jobs`` on any host (the page decides whether it
    is Teamtailor — see :func:`is_teamtailor_html`)."""
    parts = _host_and_path(url)
    return bool(parts) and bool(_BOARD_PATH_RE.match(parts[1]))


def is_teamtailor_board_url(url: str) -> bool:
    """A board on a Teamtailor host, recognisable before anything is fetched.

    Custom domains cannot be recognised by URL; they are caught after the HTML
    fetch by :func:`is_teamtailor_html`.
    """
    parts = _host_and_path(url)
    return bool(parts) and is_teamtailor_host(parts[0]) and bool(_BOARD_PATH_RE.match(parts[1]))


def teamtailor_job_id(url: str) -> str | None:
    """Numeric posting id for a ``/jobs/{id}[-slug]`` path on any host."""
    parts = _host_and_path(url)
    if not parts:
        return None
    m = _JOB_PATH_RE.match(parts[1])
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# HTML detection (custom domains)
# ---------------------------------------------------------------------------


def is_teamtailor_html(html: str) -> bool:
    """Every career-site page loads Teamtailor's CDN bundle and carries the
    employee-login link to ``app.teamtailor.com/companies/{id}`` and the
    "Applicant tracking system by Teamtailor" footer. A CDN image alone is not
    enough — a company blog can embed one — so one of the other two must be
    present as well."""
    if "teamtailor-cdn.com" not in html:
        return False
    return "app.teamtailor.com/companies/" in html or "Applicant tracking system by Teamtailor" in html


# ---------------------------------------------------------------------------
# Board: request shape
# ---------------------------------------------------------------------------

# Parameters the board acts on (verified against the HTML list, 2026-09-25).
FILTER_PARAMS = (
    "query", "department", "location", "country",
    "remote_status_id", "employment_type", "language",
)
# The list form submits split_view with every search; it only toggles the map.
_DISPLAY_PARAMS = frozenset({"split_view"})
_PAGING_PARAMS = frozenset({"page", "per_page"})
# Page size of both feeds whenever a filter is present.
_FILTERED_PAGE_SIZE = 20
# Page size of unfiltered JSON, and RSS's default length.
_UNFILTERED_PAGE_SIZE = 100
# Per feed. 3,000 postings unfiltered, 600 filtered; past it the render says so.
MAX_FEED_PAGES = 30
# A single feed page carries every description on it (Uniflex: 254 postings,
# 1 MB of RSS), so the ceiling is set per response, not per board.
BOARD_MAX_RESPONSE_BYTES = 25 * 1024 * 1024


def _classify_params(url: str) -> dict[str, list[tuple[str, str]]]:
    """Split the caller's query string by what the board does with each part.

    An empty value is how the list form submits "All", and the board treats it
    as absent; an empty map-bounds parameter even makes the feeds answer 422.
    So empties are dropped without comment, as is the display toggle.
    """
    try:
        pairs = parse_qsl(urlparse(url).query, keep_blank_values=True)
    except Exception:
        pairs = []
    out: dict[str, list[tuple[str, str]]] = {"forward": [], "applied": [], "unknown": [], "paging": []}
    for key, value in pairs:
        if not value.strip() or key in _DISPLAY_PARAMS:
            continue
        if key in _PAGING_PARAMS:
            out["paging"].append((key, value))
            continue
        out["forward"].append((key, value))
        out["applied" if key in FILTER_PARAMS else "unknown"].append((key, value))
    return out


def _feed_url(base: str, name: str, params: list[tuple[str, str]]) -> str:
    return f"{base}/{name}" + (f"?{urlencode(params)}" if params else "")


def _is_same_feed(candidate: str, host: str, path: str) -> bool:
    """``next_url`` is followed only back into the feed it came from."""
    try:
        parsed = urlparse(candidate)
    except Exception:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == host and parsed.path == path


async def _get(session, url: str):
    try:
        return await session.get(url), None
    except Exception as exc:
        return None, type(exc).__name__


def _json_feed(resp) -> dict | None:
    if resp is None or resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    if not str(data.get("version") or "").startswith("https://jsonfeed.org/version/"):
        return None
    return data


def _slim_json_item(item: dict) -> dict:
    """Keep what the list renders; the description is most of each item."""
    posting = item.get("_jobposting") if isinstance(item.get("_jobposting"), dict) else {}
    identifier = posting.get("identifier")
    numeric_id = identifier.get("value") if isinstance(identifier, dict) else None
    return {
        "id": item.get("id"),
        "title": item.get("title") or posting.get("title"),
        "url": item.get("url"),
        "date_published": item.get("date_published") or posting.get("datePosted"),
        "numeric_id": numeric_id,
        "baseSalary": posting.get("baseSalary"),
        "validThrough": posting.get("validThrough"),
    }


def _failure(resp, err: str | None) -> str:
    if err:
        return err
    if resp is None:
        return "no response"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}"
    return "not a JSON Feed"


# ---------------------------------------------------------------------------
# Board: RSS parsing (runs in the isolated worker)
# ---------------------------------------------------------------------------

_TT_NS = {"tt": "https://teamtailor.com/locations"}


def _text(el: ET.Element | None, path: str) -> str:
    # Collapses the U+202F / U+00A0 spaces employers paste into names, which
    # otherwise make a role look different from the title it repeats.
    if el is None:
        return ""
    return " ".join((el.findtext(path, namespaces=_TT_NS) or "").split())


def parse_teamtailor_rss(xml_bytes: bytes) -> list[dict] | None:
    """Per-posting fields the JSON Feed lacks. ``None`` if it is not the feed."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    if root.tag != "rss":
        return None
    out: list[dict] = []
    for item in root.iter("item"):
        guid = _text(item, "guid")
        if not guid:
            continue
        locations = []
        for loc in item.findall("tt:locations/tt:location", _TT_NS):
            name = _text(loc, "tt:name")
            if name:
                locations.append(name)
        out.append({
            "guid": guid,
            "title": _text(item, "title"),
            "link": _text(item, "link"),
            "pubDate": _text(item, "pubDate"),
            "remoteStatus": _text(item, "remoteStatus"),
            "department": _text(item, "tt:department"),
            "role": _text(item, "tt:role"),
            "locations": locations,
        })
    return out


# ---------------------------------------------------------------------------
# Board: fetch
# ---------------------------------------------------------------------------


async def fetch_teamtailor_board(board_url: str, session, *, timeout: float = 20.0) -> dict | None:
    """Read a whole board from its two feeds.

    Returns ``None`` when the first ``jobs.json`` page is not a JSON Feed (not a
    Teamtailor board, or the feed is down) so the caller can fall back. Any
    later failure is recorded on the result and rendered, never swallowed.
    """
    try:
        parsed = urlparse(board_url)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    m = _BOARD_PATH_RE.match(parsed.path or "")
    if not host or not m:
        return None
    prefix = m.group(1) or ""
    base = f"https://{host}{prefix}"
    params = _classify_params(board_url)
    forward = params["forward"]

    # --- jobs.json: follow next_url when given, else walk page=N while pages
    # come back full at the filtered size.
    items: list[dict] = []
    seen: set[str] = set()
    title = ""
    json_pages = 0
    json_stop: str | None = None
    page = 1
    url = _feed_url(base, "jobs.json", forward)
    while True:
        if json_pages >= MAX_FEED_PAGES:
            json_stop = f"stopped after {json_pages} pages of jobs.json; the board lists more"
            break
        resp, err = await _get(session, url)
        json_pages += 1
        data = _json_feed(resp)
        if data is None:
            if json_pages == 1:
                return None
            json_stop = f"jobs.json page {json_pages} failed ({_failure(resp, err)}); postings after it are missing"
            break
        title = title or str(data.get("title") or "").strip()
        page_items = [i for i in data["items"] if isinstance(i, dict)]
        fresh = 0
        for item in page_items:
            iid = item.get("id")
            if not isinstance(iid, str) or not iid or iid in seen:
                continue
            seen.add(iid)
            items.append(_slim_json_item(item))
            fresh += 1
        next_url = data.get("next_url")
        if isinstance(next_url, str) and _is_same_feed(next_url, host, f"{prefix}/jobs.json"):
            if not fresh:
                break
            url = next_url
            continue
        # Filtered mode: no next_url, 20 a page, page=N honoured. Unfiltered
        # never pages this way, so an unfiltered board of exactly 20 costs no
        # extra request.
        if forward and len(page_items) == _FILTERED_PAGE_SIZE and fresh:
            page += 1
            url = _feed_url(base, "jobs.json", forward + [("page", str(page))])
            continue
        break

    # --- jobs.rss: one request unfiltered (sized to what JSON found), or
    # page=N at 20 when filtered. Stop as soon as every JSON posting is covered.
    rss: dict[str, dict] = {}
    rss_order: list[str] = []
    rss_pages = 0
    rss_stop: str | None = None
    rss_base = list(forward)
    if len(items) > _UNFILTERED_PAGE_SIZE:
        rss_base.append(("per_page", str(len(items))))
    page = 1
    while True:
        if rss_pages >= MAX_FEED_PAGES:
            rss_stop = f"stopped after {rss_pages} pages of jobs.rss"
            break
        rss_params = rss_base + ([("page", str(page))] if page > 1 else [])
        resp, err = await _get(session, _feed_url(base, "jobs.rss", rss_params))
        rss_pages += 1
        if resp is None or resp.status_code != 200:
            rss_stop = f"jobs.rss page {rss_pages} failed ({_failure(resp, err)})"
            break
        try:
            parsed_items = await run_isolated(parse_teamtailor_rss, resp.content, timeout=timeout)
        except IsolatedProcessingError:
            parsed_items = None
        if parsed_items is None:
            rss_stop = f"jobs.rss page {rss_pages} was not a readable RSS feed"
            break
        fresh = 0
        for entry in parsed_items:
            guid = entry["guid"]
            if guid in rss:
                continue
            rss[guid] = entry
            rss_order.append(guid)
            fresh += 1
        if seen and seen <= rss.keys():
            break
        if forward and len(parsed_items) == _FILTERED_PAGE_SIZE and fresh:
            page += 1
            continue
        break

    return {
        "title": title,
        "board_url": board_url,
        "feed_base": base,
        "locale": prefix.lstrip("/"),
        "params": params,
        "items": items,
        "json_pages": json_pages,
        "json_stop": json_stop,
        "rss": rss,
        "rss_order": rss_order,
        "rss_pages": rss_pages,
        "rss_stop": rss_stop,
    }


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_UNITS = {"YEAR": "year", "MONTH": "month", "WEEK": "week", "DAY": "day", "HOUR": "hour"}


def _remote_label(value: str) -> str:
    # Teamtailor's own enum (fully, hybrid, onsite, temporary, none), kept
    # as-is like every board renderer here. Only `none` is glossed, because
    # read bare it says "not remote" and it does not mean that.
    value = (value or "").strip()
    if not value:
        return "remote: (blank)"
    if value == "none":
        return "remote: none (not set)"
    return f"remote: {value}"


def _amount(value) -> str:
    """Thousands separators on a number; anything else verbatim."""
    raw = str(value).strip()
    try:
        number = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return raw
    if not number.is_finite():
        return raw
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return f"{number.normalize():,}"


def _present(value) -> bool:
    return value is not None and str(value).strip() != ""


def format_salary(base) -> str | None:
    """``CAD 125,000–145,000 / year``; ``None`` when no band is published.

    Teamtailor sends the band as strings, sometimes a single ``value`` instead
    of a range, and sometimes with an empty currency — which is said, not
    guessed.
    """
    if not isinstance(base, dict):
        return None
    currency = str(base.get("currency") or "").strip()
    value = base.get("value")
    unit = ""
    low = high = single = None
    if isinstance(value, dict):
        unit = str(value.get("unitText") or "").strip()
        low, high, single = value.get("minValue"), value.get("maxValue"), value.get("value")
    elif _present(value) and not isinstance(value, (list, bool)):
        single = value
    if _present(low) and _present(high):
        amount = _amount(low) if _amount(low) == _amount(high) else f"{_amount(low)}–{_amount(high)}"
    elif _present(low):
        amount = f"from {_amount(low)}"
    elif _present(high):
        amount = f"up to {_amount(high)}"
    elif _present(single):
        amount = _amount(single)
    else:
        return None
    text = f"{currency} {amount}" if currency else amount
    if unit:
        text += f" / {_UNITS.get(unit.upper(), unit.lower())}"
    if not currency:
        text += " (currency not stated)"
    return text


def _date(value) -> str:
    return str(value or "").strip()[:10]


def _rss_date(value: str) -> str:
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return ""


def _days_apart(a: str, b: str) -> int:
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).days)
    except ValueError:
        return 0


def _numeric_id(item: dict | None, rss_entry: dict | None) -> str:
    if item and _present(item.get("numeric_id")):
        return str(item["numeric_id"]).strip()
    for url in ((item or {}).get("url"), (rss_entry or {}).get("link")):
        if isinstance(url, str):
            found = teamtailor_job_id(url)
            if found:
                return found
    return ""


# ---------------------------------------------------------------------------
# Board: render
# ---------------------------------------------------------------------------

_NO_DEPARTMENT = "No department"
_NOT_IN_RSS = "Department unknown (not in jobs.rss)"


def _row(item: dict | None, entry: dict | None) -> tuple[str, list[str]]:
    title = str((item or {}).get("title") or (entry or {}).get("title") or "").strip() or "(untitled)"
    numeric = _numeric_id(item, entry)
    head = f"- **{title}**" + (f" ({numeric})" if numeric else "")
    details: list[str] = []

    if entry is not None:
        details.append(" / ".join(entry["locations"]) if entry["locations"] else "no location listed")
        details.append(_remote_label(entry["remoteStatus"]))
    else:
        details.append("location and remote status unknown (not in jobs.rss)")

    if item is not None:
        details.append(format_salary(item.get("baseSalary")) or "salary not published")
    else:
        details.append("salary unknown (not in jobs.json)")

    posted = _date((item or {}).get("date_published"))
    rss_posted = _rss_date((entry or {}).get("pubDate", ""))
    if posted:
        if rss_posted and _days_apart(posted, rss_posted) > 1:
            details.append(f"posted {posted} (RSS: {rss_posted})")
        else:
            details.append(f"posted {posted}")
    elif rss_posted:
        details.append(f"posted {rss_posted}")

    closes = _date((item or {}).get("validThrough"))
    if closes:
        details.append(f"closes {closes}")
    role = (entry or {}).get("role") or ""
    if role and role.casefold() != " ".join(title.split()).casefold():
        details.append(f"role: {role}")

    lines = [head + " — " + " · ".join(details)]
    url = str((item or {}).get("url") or (entry or {}).get("link") or "").strip()
    if url:
        lines.append(f"  - {url}")
    return title, lines


def _pairs(pairs: list[tuple[str, str]]) -> str:
    return ", ".join(f"{k}={v}" for k, v in pairs)


def render_teamtailor_board(board: dict) -> str:
    items: list[dict] = board.get("items") or []
    rss: dict[str, dict] = board.get("rss") or {}
    json_ids = {i["id"] for i in items}
    rss_only = [g for g in board.get("rss_order") or [] if g not in json_ids]
    missing_rss = [i for i in items if i["id"] not in rss]
    total = len(items) + len(rss_only)

    groups: dict[str, list[list[str]]] = {}
    for item in items:
        entry = rss.get(item["id"])
        if entry is None:
            dept = _NOT_IN_RSS
        else:
            dept = entry["department"] or _NO_DEPARTMENT
        groups.setdefault(dept, []).append(_row(item, entry)[1])
    for guid in rss_only:
        entry = rss[guid]
        groups.setdefault(entry["department"] or _NO_DEPARTMENT, []).append(_row(None, entry)[1])

    def _order(name: str) -> tuple[int, str]:
        return ({_NO_DEPARTMENT: 1, _NOT_IN_RSS: 2}.get(name, 0), name.casefold())

    departments = sorted(groups, key=_order)
    company = str(board.get("title") or "").strip() or urlparse(board["board_url"]).hostname or "Teamtailor"
    noun = "open position" if total == 1 else "open positions"
    parts: list[str] = [f"# {company} — Teamtailor job board ({total} {noun})", ""]
    parts.append(f"**board**: {board['board_url']}")
    base = board["feed_base"]
    parts.append(
        f"**sources**: {base}/jobs.json ({board['json_pages']} page{'s' if board['json_pages'] != 1 else ''}: "
        f"id, date, salary) + {base}/jobs.rss ({board['rss_pages']} page{'s' if board['rss_pages'] != 1 else ''}: "
        "department, location, remote status), joined on the posting UUID"
    )
    params = board.get("params") or {}
    if params.get("applied"):
        parts.append(f"**filters applied by the board**: {_pairs(params['applied'])}")
    if params.get("unknown"):
        parts.append(f"**forwarded, not a known Teamtailor filter**: {_pairs(params['unknown'])}")
    if params.get("paging"):
        parts.append(f"**ignored**: {_pairs(params['paging'])} (every page is read)")
    if total:
        published = sum(1 for i in items if format_salary(i.get("baseSalary")))
        parts.append(f"**salary published**: {published} of {len(items)} postings in jobs.json")
        parts.append("**departments**: " + " · ".join(f"{d} ({len(groups[d])})" for d in departments))

    notes: list[str] = []
    if board.get("json_stop"):
        notes.append(f"**incomplete**: {board['json_stop']}.")
    if board.get("rss_stop"):
        notes.append(
            f"**jobs.rss**: {board['rss_stop']}; department, location and remote status are "
            f"unknown for the {len(missing_rss)} posting{'s' if len(missing_rss) != 1 else ''} it did not cover."
        )
    elif missing_rss:
        notes.append(
            f"**{len(missing_rss)} posting{'s are' if len(missing_rss) != 1 else ' is'} in jobs.json but not "
            "jobs.rss**: department, location and remote status unknown."
        )
    if rss_only:
        notes.append(
            f"**{len(rss_only)} posting{'s are' if len(rss_only) != 1 else ' is'} in jobs.rss but not "
            "jobs.json**: salary unknown, which is not the same as unpublished."
        )
    if any((rss.get(i["id"]) or {}).get("remoteStatus") in ("", "none") for i in items if i["id"] in rss) or any(
        rss[g]["remoteStatus"] in ("", "none") for g in rss_only
    ):
        notes.append(
            "`remote: none` is Teamtailor's default value, not a statement that the job is on-site (that is "
            '`onsite`). The board\'s filter labels it "No Remote Work", but its posting pages show no remote '
            "status for it and employers leave it on postings whose location reads Remote, so read the "
            "location name."
        )
    if not total:
        if params.get("applied") or params.get("unknown"):
            notes.append("No postings match these filters.")
        elif board.get("locale"):
            host = urlparse(board["board_url"]).hostname
            notes.append(
                f"No postings are listed in the `{board['locale']}` language. Teamtailor lists postings "
                f"per language; the default-language board is https://{host}/jobs."
            )
        else:
            notes.append("The board lists no open positions.")
    if notes:
        parts.append("")
        parts.extend(notes)
    parts.append("")

    for dept in departments:
        parts.append(f"## {dept} ({len(groups[dept])})")
        parts.append("")
        for lines in groups[dept]:
            parts.extend(lines)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Job page (runs in the isolated preflight worker)
# ---------------------------------------------------------------------------

_BLANK_LINE_COLLAPSE_RE = re.compile(r"\n{3,}")
_EMPTY_P_RE = re.compile(r"<p[^>]*>\s*(?:&nbsp;|\xa0)?\s*</p>")


def _html_to_markdown(fragment: str) -> str:
    if not fragment:
        return ""
    md = markdownify(
        _EMPTY_P_RE.sub("", fragment), heading_style="ATX", bullets="-",
        escape_asterisks=False, escape_underscores=False,
    )
    return _BLANK_LINE_COLLAPSE_RE.sub("\n\n", md).strip()


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def _unescape(value) -> str:
    # The page's JSON-LD strings are HTML-escaped once more than the feed's
    # ("&amp;" in a title, "&lt;p&gt;" in the description).
    return html_lib.unescape(str(value or "")).strip()


def _job_posting(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else (data.get("@graph") or [data]) if isinstance(data, dict) else []
        for node in candidates:
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                return node
    return None


def _names(value) -> str:
    nodes = value if isinstance(value, list) else [value]
    names = []
    for node in nodes:
        if isinstance(node, dict):
            name = _squash(str(node.get("name") or ""))
        elif isinstance(node, str):
            name = _squash(node)
        else:
            name = ""
        if name:
            names.append(name)
    return ", ".join(names)


def render_teamtailor_job(html: str, page_url: str) -> str | None:
    """Render a posting from its own page, or ``None`` if it carries no
    JobPosting (not a posting page, or a layout this does not know)."""
    soup = BeautifulSoup(html, "lxml")
    posting = _job_posting(soup)
    if posting is None:
        return None
    title = _unescape(posting.get("title"))
    if not title:
        return None

    # Header: the h1 carrying the title sits in a wrapper with the
    # "department · location · remote badge" line and the pitch. The badge is
    # the element holding Teamtailor's wifi icon; its text is the site's own
    # wording ("Onsite", "Fully Remote", "Hybrid"), and it is absent for `none`.
    header = None
    for h1 in soup.find_all("h1"):
        if _squash(h1.get_text(" ")) == _squash(title):
            header = h1.parent
            break
    badge = pitch = ""
    if header is not None:
        icon = header.find("i", class_="fa-wifi")
        if icon is not None and icon.parent is not None:
            badge = _squash(icon.parent.get_text(" "))
        subtitle = header.find("h2")
        if subtitle is not None:
            pitch = _squash(subtitle.get_text(" "))

    facts: list[tuple[str, str]] = []
    for dl in soup.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            label, value = _squash(dt.get_text(" ")), _squash(dd.get_text()) if dd is not None else ""
            if label and value:
                facts.append((label, value))
        if facts:
            break
    if header is not None:
        if not badge:
            facts.append(("Remote status", "not set (Teamtailor's default; the posting shows none)"))
        elif badge not in {v for _, v in facts}:
            facts.append(("Remote status", badge))

    org = posting.get("hiringOrganization")
    company = _unescape(org.get("name")) if isinstance(org, dict) else ""
    identifier = posting.get("identifier")
    numeric = _squash(str(identifier.get("value") or "")) if isinstance(identifier, dict) else ""
    numeric = numeric or teamtailor_job_id(page_url) or ""

    parts: list[str] = [f"# {title}", ""]
    byline = " · ".join(b for b in (f"**{company}**" if company else "", f"Teamtailor posting {numeric}" if numeric else "") if b)
    if byline:
        parts.extend([byline, ""])
    if pitch:
        parts.extend([f"> {pitch}", ""])
    if facts:
        parts.extend(f"- **{label}**: {value}" for label, value in facts)
        parts.append("")

    structured: list[tuple[str, str]] = []
    if _date(posting.get("datePosted")):
        structured.append(("Posted", _date(posting.get("datePosted"))))
    if _date(posting.get("validThrough")):
        structured.append(("Closes", _date(posting.get("validThrough"))))
    structured.append(("Salary", format_salary(posting.get("baseSalary")) or "not published"))
    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(e) for e in employment if e)
    if employment:
        structured.append(("Employment type", _squash(str(employment))))
    applicants = _names(posting.get("applicantLocationRequirements"))
    if applicants:
        structured.append(("Applicants must be in", applicants))
    location_type = posting.get("jobLocationType")
    if location_type:
        structured.append(("Location type", _squash(str(location_type))))
    parts.append("From the posting's JobPosting data:")
    parts.append("")
    parts.extend(f"- **{label}**: {value}" for label, value in structured)
    parts.append("")

    description = _html_to_markdown(_unescape(posting.get("description")))
    if not description:
        prose = soup.find(class_="prose")
        description = _html_to_markdown(str(prose)) if prose is not None else ""
    if description:
        parts.extend(["## Description", "", description, ""])

    parts.append(f"**sourceUrl**: {page_url}")
    return "\n".join(parts).rstrip() + "\n"
