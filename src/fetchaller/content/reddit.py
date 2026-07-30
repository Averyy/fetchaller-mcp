"""Reddit URL routing and compact anonymous-JSON renderers.

Normal Reddit URLs are canonicalized to ``www.reddit.com`` and, when a public
JSON mapping exists, fetched as anonymous JSON and rendered into selected-field
Markdown. Explicit ``.json`` URLs remain raw JSON; ``raw=True`` is handled by
the normal fetch pipeline and returns New Reddit HTML.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from math import isfinite
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

# Keep both New Reddit chrome and legacy selectors here. The legacy selectors
# make explicitly fetched snapshots readable, but no normal fetch path routes to
# Old Reddit.
SELECTORS_LIST = [
    "reddit-header-large",
    "shreddit-header",
    "reddit-sidebar-nav",
    "shreddit-async-loader",
    "shreddit-experience-tree",
    "faceplate-tracker",
    "[slot='left-sidebar']",
    "[slot='right-sidebar']",
    "[data-testid='left-sidebar']",
    "[data-testid='right-sidebar']",
    ".side",
    ".footer-parent",
    ".listing-chooser",
    ".searchpane",
    ".infobar",
    ".premium-banner-outer",
    ".morelink",
    ".titlebox",
    ".login-form-side",
    ".promotedlink",
    ".organic-listing",
    ".score.dislikes",
    ".score.likes",
    ".arrow",
    ".rank",
    "span.error",
    ".clearleft",
    ".comment .flat-list",
    ".commentarea > .menuarea",
    ".panestack-title",
    ".comment .expand",
    ".thumbnail",
    "img[src*='pixel.png']",
    ".bottommenu",
    ".tabmenu",
    ".dropdown-title.lightdrop",
    ".dropdown.lightdrop",
    ".drop-choices.lightdrop",
]

_SUBREDDIT_RE = re.compile(
    r"(?=.{1,100}\Z)[A-Za-z0-9_]{1,21}(?:[+-][A-Za-z0-9_]{1,21})*\Z"
)
_USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_THING_ID_RE = re.compile(r"[A-Za-z0-9]{2,16}\Z")
_CURSOR_RE = re.compile(r"t[1-6]_[A-Za-z0-9]{2,16}\Z")
_LIVE_CURSOR_RE = re.compile(
    r"LiveUpdate_[A-Fa-f0-9-]{16,64}\Z",
)
_WIKI_CURSOR_RE = re.compile(
    r"WikiRevision_[A-Fa-f0-9-]{16,64}\Z",
)
_REVISION_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_UUID_RE = re.compile(
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}\Z"
)
_PERMALINK_SLUG_RE = re.compile(r"(/comments/[a-z0-9]+)/[^/]+/?$", re.IGNORECASE)
_LISTING_SORTS = frozenset(
    {"best", "hot", "new", "top", "rising", "controversial", "randomrising"}
)
_PROFILE_SORTS = frozenset({"hot", "new", "top", "controversial"})
_COMMENT_SORTS = frozenset(
    {"confidence", "top", "new", "controversial", "old", "qa", "random", "live"}
)
_SEARCH_SORTS = frozenset({"relevance", "hot", "top", "new", "comments"})
_TIME_FILTERS = frozenset({"hour", "day", "week", "month", "year", "all"})
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_GEOGRAPHIC_RE = re.compile(r"(?:GLOBAL|[A-Z]{2}(?:_[A-Z]{2})?)\Z")
_OLD_REDDIT_URL_RE = re.compile(
    # Do not rewrite a hostname-shaped string inside another URL's path/query,
    # a custom scheme, or a lookalike hostname.  Real authored links appear at
    # a text/Markdown boundary and may be absolute, protocol-relative, or bare.
    r"(?<![A-Za-z0-9./?=&%:+@\\-])"
    r"(?:(?:https?:)?//)?old\.reddit\.com"
    r"(?![A-Za-z0-9.%+-])",
    re.IGNORECASE,
)
_CANONICAL_REDDIT_HOSTS = frozenset(
    {"reddit.com", "www.reddit.com", "old.reddit.com"}
)

# This is the production inventory.  The checked-in live-parity corpus imports
# it, so adding a route without an evidence target cannot silently leave a gap.
REDDIT_ROUTE_KINDS = frozenset(
    {
        "collection", "comment_listing", "domain_listing", "duplicates",
        "explicit_json", "html_fallback", "listing", "live", "live_about",
        "live_contributors", "live_update", "moderators", "morechildren",
        "multi_about", "multi_profile", "related", "rules", "search",
        "subreddit_about", "subreddit_directory", "thread", "trophies",
        "user_about", "user_directory", "user_listing", "user_profile", "wiki",
        "wiki_diff", "wiki_discussions", "wiki_pages", "wiki_revisions",
    }
)


@dataclass(frozen=True)
class RedditTransformResult:
    """Strict Reddit recognition plus canonical New Reddit URL."""

    url: str
    is_reddit: bool


@dataclass(frozen=True)
class RedditRoute:
    """A normal Reddit URL and the anonymous JSON request(s) that represent it."""

    canonical_url: str
    kind: str
    requests: tuple[str, ...] = ()
    subreddit: str | None = None
    username: str | None = None
    selected_comment_id: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in REDDIT_ROUTE_KINDS:
            raise ValueError(f"unknown Reddit route kind: {self.kind}")

    @property
    def is_explicit_json(self) -> bool:
        return self.kind == "explicit_json"

    @property
    def is_mapped(self) -> bool:
        return bool(self.requests)


def is_reddit_host(hostname: str) -> bool:
    """Return True only for reddit.com itself or a real subdomain."""

    host = (hostname or "").rstrip(".").lower()
    return host == "reddit.com" or host.endswith(".reddit.com")


def transform_reddit_url(url: str) -> RedditTransformResult:
    """Recognize Reddit strictly and canonicalize it to New Reddit.

    Old, bare, and ``www`` content hosts become ``https://www.reddit.com``.
    Other Reddit subdomains keep their own host and representation; the old
    implementation did not rewrite hosts such as ``oauth.reddit.com`` or
    ``mod.reddit.com``, so treating their paths as ``www`` content routes would
    be a migration regression.
    """

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # Accessing .port validates malformed ports before we accept the URL.
        _ = parsed.port
    except (TypeError, ValueError):
        return RedditTransformResult(url=url, is_reddit=False)

    if not is_reddit_host(hostname):
        return RedditTransformResult(url=url, is_reddit=False)

    if hostname.rstrip(".").lower() not in _CANONICAL_REDDIT_HOSTS:
        return RedditTransformResult(url=url, is_reddit=True)

    path = parsed.path or "/"
    canonical_netloc = "www.reddit.com"
    if parsed.port not in (None, 443):
        canonical_netloc += f":{parsed.port}"
    canonical = urlunparse(
        (
            "https",
            canonical_netloc,
            path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    return RedditTransformResult(url=canonical, is_reddit=True)


def _bounded_limit(max_tokens: int) -> int:
    """Bound Reddit transfer size before the final rendered character budget."""

    return max(5, min(500, max_tokens // 100))


def _user_directory_limit(query: dict[str, list[str]]) -> int:
    """Keep each directory page small enough to hydrate every user exactly."""

    requested = _first_query(query, "limit")
    if requested and requested.isdigit() and int(requested) > 0:
        return min(int(requested), 1)
    return 1


_MAX_THREAD_DEPTH = 10


def _bounded_depth(max_tokens: int) -> int:
    if max_tokens < 1000:
        return 1
    if max_tokens < 2500:
        return 2
    if max_tokens < 5000:
        return 4
    if max_tokens < 10000:
        return 6
    if max_tokens < 20000:
        return 8
    return _MAX_THREAD_DEPTH


def _first_query(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _boolean_query(
    query: dict[str, list[str]],
    name: str,
) -> str | None:
    value = (_first_query(query, name) or "").casefold()
    if value in {"1", "true"}:
        return "true"
    if value in {"0", "false"}:
        return "false"
    return None


def _thread_params(
    query: dict[str, list[str]],
    limit: int,
    depth: int,
) -> dict[str, str]:
    """Preserve caller-selected thread scope without allowing budget expansion."""

    requested_limit = _first_query(query, "limit")
    effective_limit = (
        min(limit, int(requested_limit))
        if requested_limit and requested_limit.isdigit() and int(requested_limit) > 0
        else limit
    )
    requested_depth = _first_query(query, "depth")
    effective_depth = (
        min(depth, int(requested_depth))
        if requested_depth
        and requested_depth.isdigit()
        and int(requested_depth) >= 0
        else depth
    )
    requested_sort = _first_query(query, "sort")
    params = {
        "sort": (
            requested_sort
            if requested_sort in _COMMENT_SORTS
            else "confidence"
        ),
        "limit": str(effective_limit),
        "depth": str(effective_depth),
    }
    sr_detail = _boolean_query(query, "sr_detail")
    if sr_detail is not None:
        params["sr_detail"] = sr_detail
    # These documented controls can reduce or reshape the public comment
    # representation. Preserve only their bounded, typed forms.
    for name in ("showedits", "showmedia", "showmore", "showtitle", "threaded"):
        value = _boolean_query(query, name)
        if value is not None:
            params[name] = value
    truncate_at = _first_query(query, "truncate")
    if truncate_at and truncate_at.isdigit():
        params["truncate"] = str(min(50, int(truncate_at)))
    return params


def _cursor(value: str | None) -> str | None:
    return value if value and _CURSOR_RE.fullmatch(value) else None


def _live_cursor(value: str | None) -> str | None:
    return value if value and _LIVE_CURSOR_RE.fullmatch(value) else None


def _listing_params(query: dict[str, list[str]], limit: int, *, top: bool = False) -> dict[str, str]:
    # Keep the output-budget-derived bound, while honoring a URL that asks for
    # a smaller public listing just as the Old Reddit page did.
    requested_limit = _first_query(query, "limit")
    effective_limit = limit
    if requested_limit and requested_limit.isdigit() and int(requested_limit) > 0:
        effective_limit = min(limit, int(requested_limit))
    params = {"limit": str(effective_limit), "raw_json": "1"}
    after = _cursor(_first_query(query, "after"))
    before = _cursor(_first_query(query, "before"))
    if after:
        params["after"] = after
    elif before:
        params["before"] = before
    count = _first_query(query, "count")
    if count and count.isdigit():
        params["count"] = str(min(int(count), 1000))
    if _first_query(query, "show") == "all":
        params["show"] = "all"
    sr_detail = _boolean_query(query, "sr_detail")
    if sr_detail is not None:
        params["sr_detail"] = sr_detail
    geographic = _first_query(query, "g")
    if geographic and _GEOGRAPHIC_RE.fullmatch(geographic):
        params["g"] = geographic
    if top:
        time_filter = _first_query(query, "t")
        if time_filter in _TIME_FILTERS:
            params["t"] = time_filter
    return params


def _randomrising_params(query: dict[str, list[str]]) -> dict[str, str]:
    """Fetch the full rising pool so its shuffled order can paginate safely."""

    params = _listing_params(query, 100)
    params["limit"] = "100"
    for name in ("after", "before", "count"):
        params.pop(name, None)
    return params


def _duplicates_params(
    query: dict[str, list[str]],
    limit: int,
) -> dict[str, str]:
    params = _listing_params(query, limit)
    duplicate_sort = _first_query(query, "sort")
    if duplicate_sort in {"num_comments", "new"}:
        params["sort"] = duplicate_sort
    crossposts_only = _boolean_query(query, "crossposts_only")
    if crossposts_only is not None:
        params["crossposts_only"] = crossposts_only
    subreddit = _first_query(query, "sr")
    if subreddit and _safe_part(subreddit, _SUBREDDIT_RE):
        params["sr"] = subreddit
    return params


def _live_listing_params(query: dict[str, list[str]], limit: int) -> dict[str, str]:
    requested_limit = _first_query(query, "limit")
    effective_limit = limit
    if requested_limit and requested_limit.isdigit() and int(requested_limit) > 0:
        effective_limit = min(limit, int(requested_limit))
    params = {"limit": str(effective_limit), "raw_json": "1"}
    after = _live_cursor(_first_query(query, "after"))
    before = _live_cursor(_first_query(query, "before"))
    if after:
        params["after"] = after
    elif before:
        params["before"] = before
    count = _first_query(query, "count")
    if count and count.isdigit():
        params["count"] = str(min(int(count), 1000))
    if _first_query(query, "show") == "all":
        params["show"] = "all"
    return params


def _wiki_listing_params(query: dict[str, list[str]], limit: int) -> dict[str, str]:
    requested_limit = _first_query(query, "limit")
    effective_limit = limit
    if requested_limit and requested_limit.isdigit() and int(requested_limit) > 0:
        effective_limit = min(limit, int(requested_limit))
    params = {"limit": str(effective_limit), "raw_json": "1"}
    after = _first_query(query, "after")
    before = _first_query(query, "before")
    if after and _WIKI_CURSOR_RE.fullmatch(after):
        params["after"] = after
    elif before and _WIKI_CURSOR_RE.fullmatch(before):
        params["before"] = before
    count = _first_query(query, "count")
    if count and count.isdigit():
        params["count"] = str(min(int(count), 1000))
    if _first_query(query, "show") == "all":
        params["show"] = "all"
    return params


def _activity_params(query: dict[str, list[str]], limit: int) -> dict[str, str]:
    params = _listing_params(query, limit)
    params.pop("show", None)
    activity_sort = _first_query(query, "sort")
    if activity_sort in _PROFILE_SORTS:
        params["sort"] = activity_sort
    time_filter = _first_query(query, "t")
    if time_filter in _TIME_FILTERS:
        params["t"] = time_filter
    activity_type = _first_query(query, "type")
    if activity_type in {"links", "comments"}:
        params["type"] = activity_type
    context = _first_query(query, "context")
    if context and context.isdigit() and int(context) >= 2:
        params["context"] = str(min(10, int(context)))
    if _first_query(query, "show") == "given":
        params["show"] = "given"
    return params


def _search_params(
    query: dict[str, list[str]],
    limit: int,
    *,
    restrict_sr_by_default: bool = False,
) -> dict[str, str]:
    params = _listing_params(query, limit)
    search_query = _first_query(query, "q")
    if search_query is not None and len(search_query) <= 512:
        params["q"] = search_query
    search_sort = _first_query(query, "sort")
    if search_sort in _SEARCH_SORTS:
        params["sort"] = search_sort
    time_filter = _first_query(query, "t")
    if time_filter in _TIME_FILTERS:
        params["t"] = time_filter
    search_type = _first_query(query, "type")
    if search_type:
        accepted_types = [
            value
            for value in search_type.split(",")
            if value in {"link", "sr", "user"}
        ]
        if accepted_types:
            params["type"] = ",".join(accepted_types)
    category = _first_query(query, "category")
    if category is not None and len(category) <= 5:
        params["category"] = category
    include_facets = _boolean_query(query, "include_facets")
    if include_facets is not None:
        params["include_facets"] = include_facets
    restrict_sr = _boolean_query(query, "restrict_sr")
    if restrict_sr is not None:
        params["restrict_sr"] = restrict_sr
    elif restrict_sr_by_default:
        params["restrict_sr"] = "true"
    return params


def _directory_search_params(
    query: dict[str, list[str]],
    limit: int,
) -> dict[str, str]:
    params = _listing_params(query, limit)
    search_query = _first_query(query, "q")
    if search_query is not None and len(search_query) <= 512:
        params["q"] = search_query
    search_query_id = _first_query(query, "search_query_id")
    if search_query_id and _UUID_RE.fullmatch(search_query_id):
        params["search_query_id"] = search_query_id
    directory_sort = _first_query(query, "sort")
    if directory_sort in {"relevance", "activity"}:
        params["sort"] = directory_sort
    for name in ("show_users", "typeahead_active"):
        value = _boolean_query(query, name)
        if value is not None:
            params[name] = value
    return params


def _json_url(path: str, params: dict[str, str] | None = None) -> str:
    url = f"https://www.reddit.com{path}"
    complete = dict(params or {})
    complete["raw_json"] = "1"
    return f"{url}?{urlencode(complete)}"


def _safe_part(value: str, pattern: re.Pattern[str]) -> str | None:
    return value if pattern.fullmatch(value) else None


def _safe_wiki_parts(parts: list[str]) -> bool:
    """Validate decoded public wiki page components before fixed-origin quoting."""

    return bool(parts) and sum(len(part) for part in parts) <= 1024 and all(
        part not in {"", ".", ".."} and len(part) <= 256
        for part in parts
    )


def route_reddit_url(url: str, max_tokens: int = 25000) -> RedditRoute | None:
    """Map a normal public Reddit URL to bounded anonymous JSON requests.

    Unrecognized Reddit paths return an ``html_fallback`` route so callers can
    deliberately fall through to compact New Reddit HTML extraction. Explicit
    ``.json`` paths are marked separately and must not be compact-rendered.
    """

    transformed = transform_reddit_url(url)
    if not transformed.is_reddit:
        return None

    parsed = urlparse(transformed.url)
    if (
        (parsed.hostname or "").rstrip(".").lower() != "www.reddit.com"
        or parsed.port not in (None, 443)
        or parsed.params
    ):
        return RedditRoute(transformed.url, "html_fallback")
    path = parsed.path or "/"
    if path.rstrip("/").endswith(".json"):
        return RedditRoute(transformed.url, "explicit_json")

    raw_parts = [part for part in path.split("/") if part]
    parts = [unquote(part) for part in raw_parts]
    query = parse_qs(parsed.query, keep_blank_values=True)
    limit = _bounded_limit(max_tokens)
    depth = _bounded_depth(max_tokens)

    # Global public comment/gilded feeds must be recognized before the generic
    # /comments/{post_id} permalink rule ("gilded" is otherwise a valid-looking
    # base36 thing id).
    lowered_parts = [part.lower() for part in parts]
    if lowered_parts in (["comments"], ["gilded"], ["comments", "gilded"]):
        endpoint_parts = (
            ["r", "all", "comments"]
            if lowered_parts == ["comments"]
            else parts
        )
        params = _listing_params(query, limit)
        return RedditRoute(
            transformed.url,
            "comment_listing",
            (
                _json_url(
                    f"/{'/'.join(endpoint_parts)}.json",
                    params,
                ),
            ),
            label=" ".join(lowered_parts),
        )
    if (
        len(lowered_parts) == 4
        and lowered_parts[0] == "r"
        and lowered_parts[2:] == ["comments", "gilded"]
        and _safe_part(parts[1], _SUBREDDIT_RE)
    ):
        return RedditRoute(
            transformed.url,
            "comment_listing",
            (
                _json_url(
                    f"/r/{quote(parts[1])}/comments/gilded.json",
                    _listing_params(query, limit),
                ),
            ),
            subreddit=parts[1],
            label="comments gilded",
        )

    # Thread and comment permalink routes.
    thread_offset = None
    subreddit = None
    if len(parts) >= 4 and parts[0].lower() == "r" and parts[2].lower() == "comments":
        subreddit = _safe_part(parts[1], _SUBREDDIT_RE)
        thread_offset = 3
    elif len(parts) >= 2 and parts[0].lower() == "comments":
        thread_offset = 1
    if thread_offset is not None:
        max_parts = thread_offset + 3
        if len(parts) > max_parts:
            return RedditRoute(transformed.url, "html_fallback")
        post_id = _safe_part(parts[thread_offset], _THING_ID_RE)
        if post_id and (subreddit is not None or parts[0].lower() == "comments"):
            comment_id = None
            # Reddit permalinks include a slug before the selected comment ID.
            if len(parts) > thread_offset + 2:
                comment_id = _safe_part(parts[thread_offset + 2], _THING_ID_RE)
            if comment_id is None:
                comment_id = _safe_part(
                    _first_query(query, "comment") or "",
                    _THING_ID_RE,
                )
            params = _thread_params(query, limit, depth)
            if comment_id:
                context = _first_query(query, "context")
                params["comment"] = comment_id
                params["context"] = (
                    str(min(8, int(context)))
                    if context and context.isdigit()
                    else "3"
                )
            endpoint = (
                f"/r/{quote(subreddit)}/comments/{quote(post_id)}.json"
                if subreddit
                else f"/comments/{quote(post_id)}.json"
            )
            return RedditRoute(
                transformed.url,
                "thread",
                (_json_url(endpoint, params),),
                subreddit=subreddit,
                selected_comment_id=comment_id,
            )

    # Subreddit surfaces.
    if len(parts) >= 2 and parts[0].lower() == "r":
        subreddit = _safe_part(parts[1], _SUBREDDIT_RE)
        if subreddit:
            base = f"/r/{quote(subreddit)}"
            tail = [part.lower() for part in parts[2:]]
            if (
                len(tail) in {2, 3}
                and tail[0] == "duplicates"
                and (post_id := _safe_part(parts[3], _THING_ID_RE))
            ):
                return RedditRoute(
                    transformed.url,
                    "duplicates",
                    (
                        _json_url(
                            f"/duplicates/{quote(post_id)}.json",
                            _duplicates_params(query, limit),
                        ),
                    ),
                    subreddit=subreddit,
                )
            if (
                len(tail) in {2, 3}
                and tail[0] == "related"
                and (post_id := _safe_part(parts[3], _THING_ID_RE))
            ):
                return RedditRoute(
                    transformed.url,
                    "related",
                    (
                        _json_url(
                            "/api/info.json",
                            {"id": f"t3_{post_id}", "limit": "1"},
                        ),
                    ),
                    subreddit=subreddit,
                    label="related posts",
                )
            if not tail or (len(tail) == 1 and tail[0] in _LISTING_SORTS):
                sort = tail[0] if tail else "hot"
                params = (
                    _randomrising_params(query)
                    if sort == "randomrising"
                    else _listing_params(
                        query,
                        limit,
                        top=sort in {"top", "controversial"},
                    )
                )
                endpoint_sort = "rising" if sort == "randomrising" else sort
                return RedditRoute(
                    transformed.url,
                    "listing",
                    (_json_url(f"{base}/{endpoint_sort}.json", params),),
                    subreddit=subreddit,
                    label=sort,
                )
            if tail == ["comments"]:
                params = _listing_params(query, limit)
                return RedditRoute(
                    transformed.url,
                    "comment_listing",
                    (_json_url(f"{base}/comments.json", params),),
                    subreddit=subreddit,
                    label="comments",
                )
            if tail == ["gilded"] or tail == ["comments", "gilded"]:
                params = _listing_params(query, limit)
                return RedditRoute(
                    transformed.url,
                    "comment_listing",
                    (
                        _json_url(
                            f"{base}/{'/'.join(parts[2:])}.json",
                            params,
                        ),
                    ),
                    subreddit=subreddit,
                    label="comments gilded" if len(tail) == 2 else "gilded",
                )
            if tail == ["search"]:
                params = _search_params(
                    query,
                    limit,
                    restrict_sr_by_default=True,
                )
                search_query = _first_query(query, "q")
                if search_query is not None and len(search_query) > 512:
                    return RedditRoute(transformed.url, "html_fallback")
                return RedditRoute(
                    transformed.url,
                    "search",
                    (_json_url(f"{base}/search.json", params),),
                    subreddit=subreddit,
                    label=search_query or "",
                )
            if tail == ["about"]:
                return RedditRoute(
                    transformed.url,
                    "subreddit_about",
                    (_json_url(f"{base}/about.json"),),
                    subreddit=subreddit,
                )
            if tail == ["about", "rules"]:
                return RedditRoute(
                    transformed.url,
                    "rules",
                    (_json_url(f"{base}/about/rules.json"),),
                    subreddit=subreddit,
                )
            if tail == ["about", "moderators"]:
                return RedditRoute(
                    transformed.url,
                    "moderators",
                    (
                        _json_url(
                            f"{base}/about/moderators.json",
                            {"limit": "500"},
                        ),
                    ),
                    subreddit=subreddit,
                )
            if tail == ["about", "sidebar"]:
                return RedditRoute(
                    transformed.url,
                    "subreddit_about",
                    (_json_url(f"{base}/about.json"),),
                    subreddit=subreddit,
                    label="sidebar",
                )
            if tail == ["about", "sticky"]:
                number = _first_query(query, "num")
                params = _thread_params(query, limit, depth)
                params["num"] = number if number in {"1", "2"} else "1"
                return RedditRoute(
                    transformed.url,
                    "thread",
                    (_json_url(f"{base}/about/sticky.json", params),),
                    subreddit=subreddit,
                )
            if tail and tail[0] == "wiki":
                wiki_action = tail[1] if len(tail) > 1 else None
                if wiki_action == "pages" and len(tail) == 2:
                    return RedditRoute(
                        transformed.url,
                        "wiki_pages",
                        (
                            f"https://www.reddit.com{base}/wiki/pages/",
                        ),
                        subreddit=subreddit,
                    )
                if wiki_action == "revisions":
                    wiki_parts = parts[4:]
                    if not wiki_parts or _safe_wiki_parts(wiki_parts):
                        suffix = (
                            "/" + "/".join(quote(part, safe="._-") for part in wiki_parts)
                            if wiki_parts
                            else ""
                        )
                        params = _wiki_listing_params(query, limit)
                        return RedditRoute(
                            transformed.url,
                            "wiki_revisions",
                            (_json_url(f"{base}/wiki/revisions{suffix}.json", params),),
                            subreddit=subreddit,
                            label="/".join(wiki_parts) if wiki_parts else "all pages",
                        )
                if wiki_action == "discussions":
                    wiki_parts = parts[4:]
                    if _safe_wiki_parts(wiki_parts):
                        params = _listing_params(query, limit)
                        return RedditRoute(
                            transformed.url,
                            "wiki_discussions",
                            (
                                _json_url(
                                    f"{base}/wiki/discussions/"
                                    f"{'/'.join(quote(part, safe='._-') for part in wiki_parts)}.json",
                                    params,
                                ),
                            ),
                            subreddit=subreddit,
                            label="/".join(wiki_parts),
                        )
                if wiki_action in {"pages", "revisions", "discussions"}:
                    return RedditRoute(transformed.url, "html_fallback")
                wiki_parts = parts[3:] or ["index"]
                if _safe_wiki_parts(wiki_parts):
                    wiki_path = "/".join(quote(part, safe="._-") for part in wiki_parts)
                    params: dict[str, str] = {}
                    for name in ("v", "v2"):
                        revision = _first_query(query, name)
                        if revision and _REVISION_RE.fullmatch(revision):
                            params[name] = revision
                    if "v" in params and "v2" in params:
                        revision_requests = [
                            _json_url(
                                f"{base}/wiki/{wiki_path}.json",
                                {"v": params["v"]},
                            )
                        ]
                        if params["v2"] != params["v"]:
                            revision_requests.append(
                                _json_url(
                                    f"{base}/wiki/{wiki_path}.json",
                                    {"v": params["v2"]},
                                )
                            )
                        return RedditRoute(
                            transformed.url,
                            "wiki_diff",
                            tuple(revision_requests),
                            subreddit=subreddit,
                            label="/".join(wiki_parts),
                        )
                    return RedditRoute(
                        transformed.url,
                        "wiki",
                        (_json_url(f"{base}/wiki/{wiki_path}.json", params),),
                        subreddit=subreddit,
                        label="/".join(wiki_parts),
                    )

    # User profile and activity.
    if len(parts) >= 2 and parts[0].lower() in {"user", "u"}:
        username = _safe_part(parts[1], _USERNAME_RE)
        if username:
            base = f"/user/{quote(username)}"
            activity = parts[2].lower() if len(parts) == 3 else None
            if len(parts) == 2:
                params = _activity_params(query, limit)
                return RedditRoute(
                    transformed.url,
                    "user_profile",
                    (
                        _json_url(f"{base}/about.json"),
                        _json_url(f"{base}/overview.json", params),
                        _json_url(f"{base}/trophies.json"),
                        _json_url(f"/api/multi/user/{quote(username)}.json"),
                        _json_url(f"{base}/moderated_subreddits.json"),
                    ),
                    username=username,
                )
            if activity == "about":
                return RedditRoute(
                    transformed.url,
                    "user_about",
                    (_json_url(f"{base}/about.json"),),
                    username=username,
                )
            if activity in {
                "overview",
                "submitted",
                "comments",
                "gilded",
                "upvoted",
                "downvoted",
            }:
                params = _activity_params(query, limit)
                if activity == "gilded" and _first_query(query, "show") == "given":
                    params["show"] = "given"
                return RedditRoute(
                    transformed.url,
                    "user_listing",
                    (_json_url(f"{base}/{activity}.json", params),),
                    username=username,
                    label=(
                        "gilded given"
                        if activity == "gilded"
                        and params.get("show") == "given"
                        else activity
                    ),
                )
            if activity == "trophies":
                return RedditRoute(
                    transformed.url,
                    "trophies",
                    (_json_url(f"{base}/trophies.json"),),
                    username=username,
                )

            # Public multireddits are readable anonymously.
            if (
                len(parts) in {4, 5, 6}
                and parts[2].lower() == "m"
                and (multi_name := _safe_part(parts[3], _USERNAME_RE))
            ):
                multi_base = f"{base}/m/{quote(multi_name)}"
                metadata_url = _json_url(
                    f"/api/multi/user/{quote(username)}/m/{quote(multi_name)}.json"
                )
                surface = [part.lower() for part in parts[4:]]
                if surface == ["about"]:
                    return RedditRoute(
                        transformed.url,
                        "multi_about",
                        (metadata_url,),
                        username=username,
                        label=multi_name,
                    )
                if not surface or (len(surface) == 1 and surface[0] in _LISTING_SORTS):
                    sort = surface[0] if surface else "hot"
                    params = (
                        _randomrising_params(query)
                        if sort == "randomrising"
                        else _listing_params(
                            query,
                            limit,
                            top=sort in {"top", "controversial"},
                        )
                    )
                    endpoint_sort = (
                        "rising" if sort == "randomrising" else sort
                    )
                    return RedditRoute(
                        transformed.url,
                        "multi_profile",
                        (
                            metadata_url,
                            _json_url(
                                f"{multi_base}/{endpoint_sort}.json",
                                params,
                            ),
                        ),
                        username=username,
                        label=f"u/{username}/m/{multi_name} · {sort}",
                    )
                if surface in (["comments"], ["gilded"], ["comments", "gilded"]):
                    params = _listing_params(query, limit)
                    return RedditRoute(
                        transformed.url,
                        "multi_profile",
                        (
                            metadata_url,
                            _json_url(
                                f"{multi_base}/{'/'.join(surface)}.json",
                                params,
                            ),
                        ),
                        username=username,
                        label=f"u/{username}/m/{multi_name} · {' '.join(surface)}",
                    )
                if surface == ["search"]:
                    params = _search_params(
                        query,
                        limit,
                        restrict_sr_by_default=True,
                    )
                    search_query = _first_query(query, "q")
                    if search_query is not None and len(search_query) > 512:
                        return RedditRoute(transformed.url, "html_fallback")
                    return RedditRoute(
                        transformed.url,
                        "multi_profile",
                        (
                            metadata_url,
                            _json_url(f"{multi_base}/search.json", params),
                        ),
                        username=username,
                        label=f"u/{username}/m/{multi_name} · search",
                    )

    # Old Reddit's interactive "load more comments" action used this anonymous
    # GET endpoint. Keep it mapped and readable so bounded comment output never
    # turns omitted public replies into a dead end.
    if lowered_parts == ["api", "morechildren"]:
        link_id = _first_query(query, "link_id")
        raw_children = _first_query(query, "children")
        child_ids = [
            value
            for value in (raw_children or "").split(",")
            if value
        ]
        if (
            link_id
            and re.fullmatch(r"t3_[A-Za-z0-9]{2,16}", link_id)
            and 1 <= len(child_ids) <= 100
            and all(_THING_ID_RE.fullmatch(value) for value in child_ids)
        ):
            sort = _first_query(query, "sort")
            requested_depth = _first_query(query, "depth")
            params = {
                "link_id": link_id,
                "children": ",".join(child_ids),
                "sort": sort if sort in _COMMENT_SORTS else "confidence",
                "depth": str(min(int(requested_depth), depth, 10))
                if requested_depth and requested_depth.isdigit()
                else "0",
                "api_type": "json",
            }
            more_id = _first_query(query, "id")
            if more_id and _THING_ID_RE.fullmatch(more_id):
                params["id"] = more_id
            limit_children = _boolean_query(query, "limit_children")
            params["limit_children"] = limit_children or "true"
            return RedditRoute(
                transformed.url,
                "morechildren",
                (_json_url("/api/morechildren.json", params),),
                label=f"{len(child_ids)} comments",
            )

    # Global search and front-page listings.
    if parts == ["search"]:
        params = _search_params(query, limit)
        search_query = _first_query(query, "q")
        if search_query is not None and len(search_query) > 512:
            return RedditRoute(transformed.url, "html_fallback")
        return RedditRoute(
            transformed.url,
            "search",
            (_json_url("/search.json", params),),
            label=search_query or "",
        )
    if not parts or (len(parts) == 1 and parts[0].lower() in _LISTING_SORTS):
        sort = parts[0].lower() if parts else "best"
        params = (
            _randomrising_params(query)
            if sort == "randomrising"
            else _listing_params(
                query,
                limit,
                top=sort in {"top", "controversial"},
            )
        )
        endpoint = "/r/all/rising.json" if sort == "randomrising" else f"/{sort}.json"
        return RedditRoute(
            transformed.url,
            "listing",
            (_json_url(endpoint, params),),
            label=sort,
        )

    # Public subreddit directory surfaces.
    if lowered_parts in (["r"], ["reddits"], ["subreddits"]):
        parts = ["subreddits", "popular"]
        lowered_parts = parts
    elif (
        len(lowered_parts) == 2
        and lowered_parts[0] == "reddits"
        and lowered_parts[1]
        in {
            "search",
            "new",
            "popular",
            "banned",
            "employee",
            "gold",
            "default",
            "quarantine",
            "featured",
        }
    ):
        parts = ["subreddits", parts[1]]
        lowered_parts = [part.lower() for part in parts]
    if len(parts) == 2 and parts[0].lower() == "subreddits":
        directory = parts[1].lower()
        if directory in {
            "popular",
            "new",
            "default",
            "gold",
            "banned",
            "employee",
            "quarantine",
            "featured",
        }:
            params = _listing_params(query, limit)
            return RedditRoute(
                transformed.url,
                "subreddit_directory",
                (_json_url(f"/subreddits/{directory}.json", params),),
                label=directory,
            )
        if directory == "search":
            params = _directory_search_params(query, limit)
            search_query = _first_query(query, "q")
            if search_query is not None and len(search_query) > 512:
                return RedditRoute(transformed.url, "html_fallback")
            return RedditRoute(
                transformed.url,
                "subreddit_directory",
                (_json_url("/subreddits/search.json", params),),
                label=f'subreddit search · "{search_query or ""}"',
            )

    # Public user directories exposed by Old Reddit.
    if lowered_parts == ["users", "search"]:
        params = _directory_search_params(query, _user_directory_limit(query))
        search_query = _first_query(query, "q")
        if search_query is not None and len(search_query) > 512:
            return RedditRoute(transformed.url, "html_fallback")
        return RedditRoute(
            transformed.url,
            "user_directory",
            (_json_url("/users/search.json", params),),
            label=f'user search · "{search_query or ""}"',
        )
    if lowered_parts in (["users"], ["users", "new"], ["users", "popular"]):
        directory = lowered_parts[1] if len(lowered_parts) == 2 else "popular"
        return RedditRoute(
            transformed.url,
            "user_directory",
            (
                _json_url(
                    f"/users/{directory}.json",
                    _listing_params(query, _user_directory_limit(query)),
                ),
            ),
            label=directory,
        )

    # Other public post/listing surfaces exposed by Old Reddit.
    if len(parts) == 2 and parts[0].lower() in {"gallery", "comments"}:
        post_id = _safe_part(parts[1], _THING_ID_RE)
        if post_id:
            return RedditRoute(
                transformed.url,
                "thread",
                (
                    _json_url(
                        f"/comments/{quote(post_id)}.json",
                        _thread_params(query, limit, depth),
                    ),
                ),
            )
    if len(parts) in {2, 3} and parts[0].lower() == "duplicates":
        post_id = _safe_part(parts[1], _THING_ID_RE)
        if post_id:
            return RedditRoute(
                transformed.url,
                "duplicates",
                (
                    _json_url(
                        f"/duplicates/{quote(post_id)}.json",
                        _duplicates_params(query, limit),
                    ),
                ),
            )
    if len(parts) in {2, 3} and parts[0].lower() == "related":
        post_id = _safe_part(parts[1], _THING_ID_RE)
        if post_id:
            return RedditRoute(
                transformed.url,
                "related",
                (
                    _json_url(
                        "/api/info.json",
                        {"id": f"t3_{post_id}", "limit": "1"},
                    ),
                ),
                label="related posts",
            )
    if len(parts) == 2 and parts[0].lower() == "by_id":
        requested_fullnames = [
            value for value in re.split(r"[\s,]+", parts[1]) if value
        ]
        if 1 <= len(requested_fullnames) <= 100 and all(
            re.fullmatch(r"t[13]_[A-Za-z0-9]{2,16}", value)
            for value in requested_fullnames
        ):
            fullnames = ",".join(requested_fullnames)
            # New Reddit's /by_id endpoint no longer returns t1 comments.
            # Its public /api/info endpoint accepts the same fullname list and
            # preserves both comment-only and mixed t1/t3 lookups.
            request = (
                _json_url("/api/info.json", {"id": fullnames})
                if any(value.startswith("t1_") for value in requested_fullnames)
                else _json_url(f"/by_id/{quote(fullnames, safe=',_')}.json")
            )
            return RedditRoute(
                transformed.url,
                "listing",
                (request,),
                label="items by ID",
            )
    if (
        len(parts) == 4
        and parts[0].lower() == "live"
        and parts[2].lower() == "updates"
    ):
        thread_id = _safe_part(parts[1], _THING_ID_RE)
        update_id = _safe_part(parts[3], _REVISION_RE)
        if thread_id and update_id:
            return RedditRoute(
                transformed.url,
                "live_update",
                (
                    _json_url(
                        f"/live/{quote(thread_id)}/updates/{quote(update_id)}.json"
                    ),
                ),
            )
    if len(parts) == 3 and parts[0].lower() == "live":
        thread_id = _safe_part(parts[1], _THING_ID_RE)
        surface = parts[2].lower()
        if thread_id and surface == "about":
            return RedditRoute(
                transformed.url,
                "live_about",
                (_json_url(f"/live/{quote(thread_id)}/about.json"),),
            )
        if thread_id and surface == "discussions":
            return RedditRoute(
                transformed.url,
                "listing",
                (
                    _json_url(
                        f"/live/{quote(thread_id)}/discussions.json",
                        _listing_params(query, limit),
                    ),
                ),
                label="live discussions",
            )
        if thread_id and surface == "contributors":
            return RedditRoute(
                transformed.url,
                "live_contributors",
                (
                    _json_url(
                        f"/live/{quote(thread_id)}/contributors.json"
                    ),
                ),
            )
    if len(parts) == 2 and parts[0].lower() == "live":
        thread_id = _safe_part(parts[1], _THING_ID_RE)
        if thread_id:
            return RedditRoute(
                transformed.url,
                "live",
                (
                    _json_url(f"/live/{quote(thread_id)}/about.json"),
                    _json_url(
                        f"/live/{quote(thread_id)}.json",
                        _live_listing_params(query, limit),
                    ),
                ),
            )
    if (
        len(parts) == 4
        and parts[0].lower() == "r"
        and parts[2].lower() == "collection"
    ):
        collection_id = _safe_part(parts[3], _UUID_RE)
        if collection_id:
            return RedditRoute(
                transformed.url,
                "collection",
                (
                    _json_url(
                        "/api/v1/collections/collection",
                        {
                            "collection_id": collection_id,
                            "include_links": "true",
                        },
                    ),
                ),
                subreddit=_safe_part(parts[1], _SUBREDDIT_RE),
            )

    # Public domain listings were readable through Old Reddit's generic HTML
    # path. Map them explicitly so New Reddit's application shell cannot erase
    # every post/link from the cleaned fallback output.
    if len(parts) in {2, 3} and parts[0].lower() == "domain":
        domain = _safe_part(parts[1], _DOMAIN_RE)
        sort = parts[2].lower() if len(parts) == 3 else "hot"
        if domain and sort in _LISTING_SORTS:
            params = (
                _randomrising_params(query)
                if sort == "randomrising"
                else _listing_params(
                    query,
                    limit,
                    top=sort in {"top", "controversial"},
                )
            )
            endpoint_sort = "rising" if sort == "randomrising" else sort
            return RedditRoute(
                transformed.url,
                "domain_listing",
                (
                    _json_url(
                        f"/domain/{quote(domain)}/{endpoint_sort}.json",
                        params,
                    ),
                ),
                label=f"domain {domain} · {sort}",
            )

    return RedditRoute(transformed.url, "html_fallback")


def format_relative_time(utc_seconds: float) -> str:
    """Format a Unix timestamp as compact relative time."""

    diff = max(0.0, time.time() - utc_seconds)
    if diff < 60:
        return "now"
    if diff < 3600:
        return f"{int(diff / 60)}m"
    if diff < 86400:
        return f"{int(diff / 3600)}h"
    if diff < 2592000:
        return f"{int(diff / 86400)}d"
    if diff < 31536000:
        return f"{int(diff / 2592000)}mo"
    return f"{int(diff / 31536000)}y"


def _absolute_reddit_url(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("//"):
        value = f"https:{value}"
    if value.startswith("/"):
        return f"https://www.reddit.com{value}"
    transformed = transform_reddit_url(value)
    return transformed.url if transformed.is_reddit else value


def _canonicalize_embedded_reddit_links(text: str) -> str:
    """Keep authored Reddit links usable after Old Reddit is retired."""

    return _OLD_REDDIT_URL_RE.sub("https://www.reddit.com", text)


def canonicalize_reddit_links(text: str) -> str:
    """Public fallback postprocessor for authored legacy Reddit links."""

    return _canonicalize_embedded_reddit_links(text)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _flag(data: dict, field: str) -> bool:
    """True only for a real JSON ``true``.

    These flags become factual labels ("NSFW", "Locked", "Archived") or hide a
    valid score, and nothing between Reddit's JSON and the renderer constrains
    their type -- Reddit has already been observed sending ``false`` where a
    mapping was declared. A plain truthiness test turns any truthy non-boolean,
    the string ``"false"`` included, into a confident false statement about
    someone's post.

    ``edited`` is deliberately not routed through here: Reddit sends either
    ``false`` or an edit timestamp, so its truthiness is the intended meaning.
    """

    return data.get(field) is True


def _status_labels(data: dict) -> list[str]:
    labels = []
    if _flag(data, "over_18"):
        labels.append("NSFW")
    if _flag(data, "spoiler"):
        labels.append("Spoiler")
    if _flag(data, "locked"):
        labels.append("Locked")
    if _flag(data, "archived"):
        labels.append("Archived")
    if data.get("edited"):
        labels.append("Edited")
    if _flag(data, "stickied"):
        labels.append("Stickied")
    if _flag(data, "pinned"):
        labels.append("Pinned")
    if data.get("distinguished") == "moderator":
        labels.append("Moderator")
    elif data.get("distinguished") == "admin":
        labels.append("Admin")
    if _flag(data, "is_original_content"):
        labels.append("OC")
    if _flag(data, "contest_mode"):
        labels.append("Contest mode")
    if data.get("discussion_type") == "CHAT":
        labels.append("Live chat")
    removed_by = data.get("removed_by_category")
    if removed_by == "deleted" or (
        data.get("author") == "[deleted]" and data.get("selftext") == "[deleted]"
    ):
        labels.append("Deleted")
    elif removed_by:
        labels.append("Removed")
    return labels


def _event_summary(data: dict) -> str:
    details = ["Live event" if _flag(data, "event_is_live") else "Event"]
    start = data.get("event_start")
    end = data.get("event_end")
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        details.append(f"starts {_created(start)}")
    if isinstance(end, (int, float)) and not isinstance(end, bool):
        details.append(f"ends {_created(end)}")
    return " · ".join(details) if len(details) > 1 else ""


def _score_summary(data: dict, *, compact: bool = False) -> str:
    """Render Reddit's public (fuzzed) score and optional upvote ratio."""

    if _flag(data, "score_hidden") or _flag(data, "hide_score"):
        return "score hidden"
    raw_score = data.get("score")
    if not isinstance(raw_score, (int, float)):
        score_text = "score hidden"
    elif compact:
        score_text = f"score {int(raw_score):,}"
    else:
        score_text = f"{int(raw_score):,} score"
    ratio = data.get("upvote_ratio")
    if isinstance(ratio, (int, float)) and 0 <= ratio <= 1:
        score_text += f" · {ratio:.0%} upvoted"
    return score_text


def _award_summary(data: dict) -> str:
    count = data.get("total_awards_received")
    if not isinstance(count, (int, float)) or count <= 0:
        gildings = data.get("gildings") or {}
        gilding_count = (
            sum(
                int(value)
                for value in gildings.values()
                if isinstance(value, (int, float)) and value > 0
            )
            if isinstance(gildings, dict)
            else 0
        )
        count = gilding_count or data.get("gilded")
    if not isinstance(count, (int, float)) or count <= 0:
        return ""
    names = [
        str(award.get("name"))
        for award in (data.get("all_awardings") or [])
        if isinstance(award, dict) and award.get("name")
    ][:3]
    if not names and isinstance(data.get("gildings"), dict):
        legacy_names = {
            "gid_1": "Silver",
            "gid_2": "Gold",
            "gid_3": "Platinum",
        }
        names = [
            legacy_names.get(
                str(name),
                str(name).removeprefix("gid_").replace("_", " ").title(),
            )
            for name, value in data["gildings"].items()
            if isinstance(value, (int, float)) and value > 0
        ][:3]
    return f"{int(count):,} awards" + (f" ({', '.join(names)})" if names else "")


def _archived_gilding_evidence(data: dict) -> str:
    if data.get("_fetchaller_reddit_archived_gilded") is not True:
        return ""
    count = data.get("_fetchaller_reddit_archived_gilding_count")
    if (
        isinstance(count, bool)
        or not isinstance(count, (int, float))
        or count <= 0
    ):
        return "Gilded in the exact archived Reddit snapshot"
    noun = "gilding" if int(count) == 1 else "gildings"
    return (
        f"{int(count):,} {noun} in the exact archived Reddit snapshot"
    )


def _flair(data: dict, prefix: str) -> tuple[str, list[str]]:
    text = str(data.get(f"{prefix}_flair_text") or "").strip()
    urls: list[str] = []
    rich = data.get(f"{prefix}_flair_richtext") or []
    rich_text: list[str] = []
    for item in rich:
        if not isinstance(item, dict):
            continue
        if item.get("e") == "text" and item.get("t"):
            rich_text.append(str(item["t"]))
        if item.get("e") == "emoji" and item.get("u"):
            urls.append(str(item["u"]))
    if not text and rich_text:
        text = "".join(rich_text)
    return text, _unique_urls(urls)


def format_reddit_post(
    post_data: dict,
    index: int,
    include_subreddit: bool = False,
    preview_length: int = 160,
) -> str:
    """Format a compact post-listing entry with canonical New Reddit links."""

    title = post_data.get("title") or "Untitled"
    num_comments = post_data.get("num_comments") or 0
    author = post_data.get("author") or "[deleted]"
    created_utc = post_data.get("created_utc") or 0
    permalink = post_data.get("permalink") or ""
    selftext = post_data.get("selftext") or ""
    subreddit = post_data.get("subreddit") or ""
    # Strict, but keep the absent-means-self default: a truthy non-boolean
    # otherwise counted as a self post and suppressed the outbound URL.
    is_self = post_data.get("is_self", True) is True
    external_url = unescape(
        post_data.get("url_overridden_by_dest") or post_data.get("url") or ""
    )

    short_permalink = _PERMALINK_SLUG_RE.sub(r"\1/", permalink)
    discussion_url = _absolute_reddit_url(short_permalink)
    outbound_urls: list[str] = []
    if not is_self and external_url and external_url != discussion_url:
        outbound_urls.append(external_url)
    outbound_urls.extend(_gallery_urls(post_data))
    video = _reddit_video(post_data)
    if video:
        outbound_urls.extend(video)

    urls = [f"   {media_url}" for media_url in _unique_urls(outbound_urls)]
    if discussion_url:
        urls.append(f"   {discussion_url}")

    preview = ""
    if selftext:
        clean = _collapse_whitespace(selftext)
        preview_text = clean[:preview_length] + ("..." if len(clean) > preview_length else "")
        preview = f"\n   > {preview_text}"

    sub_line = f"r/{subreddit} · " if include_subreddit and subreddit else ""
    time_str = format_relative_time(float(created_utc)) if created_utc else "?"
    labels = _status_labels(post_data)
    status = f" · [{', '.join(labels)}]" if labels else ""
    flair, flair_urls = _flair(post_data, "link")
    author_flair, author_flair_urls = _flair(post_data, "author")
    visible_flair = f" · post flair: {flair}" if flair else ""
    visible_author_flair = f" · author flair: {author_flair}" if author_flair else ""
    award_text = _award_summary(post_data)
    visible_awards = f" · {award_text}" if award_text else ""
    archived_gilding = _archived_gilding_evidence(post_data)
    if archived_gilding:
        urls.append(f"   Archived gilding evidence: {archived_gilding}")
    urls.extend(
        f"   Flair emoji: {flair_url}"
        for flair_url in _unique_urls([*flair_urls, *author_flair_urls])
    )
    event = _event_summary(post_data)
    if event:
        urls.append(f"   Event: {event}")
    url_block = "\n".join(urls)
    vote_text = _score_summary(post_data, compact=True)
    rendered = (
        f"{index}. {title}\n"
        f"   {sub_line}{vote_text} · {num_comments:,} comments · u/{author}"
        f"{visible_author_flair} · {time_str}{visible_flair}{visible_awards}{status}\n"
        f"{url_block}{preview}"
    ).rstrip()
    return _canonicalize_embedded_reddit_links(rendered)


def _unix_seconds(value: object) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(timestamp):
        return None
    # Reddit's classic created_utc fields are seconds, while newer web-model
    # fields such as poll and post-event timestamps are Unix milliseconds.
    if abs(timestamp) >= 100_000_000_000:
        timestamp /= 1000
    return timestamp


def _created(value: object) -> str:
    timestamp = _unix_seconds(value)
    if timestamp is None:
        return "unknown time"
    try:
        return datetime.fromtimestamp(timestamp, UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (OverflowError, OSError, ValueError):
        return "unknown time"


def _unique_urls(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        url = unescape(value or "").strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _media_metadata_urls(media: dict) -> list[str]:
    """Return original image/animation/video URLs from Reddit media metadata."""

    source = media.get("s") or {}
    return _unique_urls(
        [
            *(source.get(key) or "" for key in ("u", "gif", "mp4")),
            *(media.get(key) or "" for key in ("hlsUrl", "dashUrl")),
            *(media.get(key) or "" for key in ("hls_url", "dash_url")),
        ]
    )


def _gallery_items(data: dict) -> list[dict[str, object]]:
    metadata = data.get("media_metadata") or {}
    result: list[dict[str, object]] = []
    for item in (data.get("gallery_data") or {}).get("items") or []:
        media = metadata.get(item.get("media_id")) or {}
        urls = _media_metadata_urls(media)
        status = str(media.get("status") or "").casefold()
        result.append(
            {
                "urls": urls,
                "caption": str(item.get("caption") or "").strip(),
                "outbound_url": unescape(item.get("outbound_url") or "").strip(),
                "unavailable": item.get("is_deleted") is True
                or status in {"failed", "invalid"},
            }
        )
    return result


def _gallery_urls(data: dict) -> list[str]:
    result: list[str] = []
    for item in _gallery_items(data):
        result.extend(item["urls"])
        outbound_url = item["outbound_url"]
        if isinstance(outbound_url, str):
            result.append(outbound_url)
    return _unique_urls(result)


def _reddit_video(data: dict) -> tuple[str, str, str] | None:
    for container in (data.get("secure_media"), data.get("media")):
        video = (container or {}).get("reddit_video") or {}
        if video:
            fallback = unescape(video.get("fallback_url") or "")
            hls = unescape(video.get("hls_url") or "")
            dash = unescape(video.get("dash_url") or "")
            return fallback, hls, dash
    return None


def _render_post(data: dict, *, _crosspost_depth: int = 0) -> str:
    title = data.get("title") or "Untitled"
    subreddit = data.get("subreddit_name_prefixed") or (
        f"r/{data.get('subreddit')}" if data.get("subreddit") else "Reddit"
    )
    author = data.get("author") or "[deleted]"
    comments = data.get("num_comments") or 0
    metadata = (
        f"{subreddit} · u/{author} · {_score_summary(data)} · "
        f"{comments:,} comments · {_created(data.get('created_utc'))}"
    )
    labels = _status_labels(data)
    lines = [f"# {title}", "", metadata]
    if labels:
        lines.extend(["", f"**{' · '.join(labels)}**"])
    visible_flair, flair_urls = _flair(data, "link")
    author_flair, author_flair_urls = _flair(data, "author")
    if visible_flair:
        lines.extend(["", f"**Post flair:** {visible_flair}"])
    if author_flair:
        lines.extend(["", f"**Author flair:** {author_flair}"])
    for flair_url in _unique_urls([*flair_urls, *author_flair_urls]):
        lines.extend(["", f"**Flair emoji:** {flair_url}"])
    if data.get("edited"):
        lines.extend(["", f"**Edited:** {_created(data.get('edited'))}"])
    awards = _award_summary(data)
    if awards:
        lines.extend(["", f"**Awards:** {awards}"])
    archived_gilding = _archived_gilding_evidence(data)
    if archived_gilding:
        lines.extend(
            ["", f"**Archived gilding evidence:** {archived_gilding}"]
        )
    event = _event_summary(data)
    if event:
        lines.extend(["", f"**Event:** {event}"])

    selftext = data.get("selftext")
    if selftext:
        lines.extend(["", str(selftext)])

    urls: list[str] = []
    external_url = unescape(data.get("url_overridden_by_dest") or data.get("url") or "")
    discussion = _absolute_reddit_url(data.get("permalink"))
    # ``is not True`` keeps absent-means-link-post while rejecting "false".
    if data.get("is_self") is not True and external_url and external_url != discussion:
        urls.append(f"**Link:** {external_url}")

    gallery = _gallery_items(data)
    if gallery:
        lines.extend(["", f"**Gallery ({len(gallery)} items):**"])
        for index, item in enumerate(gallery, 1):
            caption = item["caption"]
            unavailable = item["unavailable"]
            lines.append(
                f"- **Item {index}**"
                + (f" — {caption}" if isinstance(caption, str) and caption else "")
                + (" — unavailable" if unavailable else "")
            )
            for url in item["urls"]:
                lines.append(f"  - {url}")
            outbound_url = item["outbound_url"]
            if isinstance(outbound_url, str) and outbound_url:
                lines.append(f"  - Outbound: {outbound_url}")

    video = _reddit_video(data)
    if video:
        fallback, hls, dash = video
        lines.extend(["", "**Video:**"])
        if fallback:
            lines.append(f"- MP4: {fallback}")
        if hls:
            lines.append(f"- HLS: {hls}")
        if dash:
            lines.append(f"- DASH: {dash}")

    oembed = ((data.get("secure_media") or {}).get("oembed") or {})
    if oembed:
        provider = oembed.get("provider_name") or "external media"
        media_title = oembed.get("title")
        lines.extend(["", f"**Media:** {provider}" + (f" — {media_title}" if media_title else "")])

    crossposts = data.get("crosspost_parent_list") or []
    if crossposts and _crosspost_depth < 1:
        parent = crossposts[0]
        parent_url = _absolute_reddit_url(parent.get("permalink"))
        source = parent.get("subreddit_name_prefixed") or parent.get("subreddit") or "Reddit"
        lines.extend(
            [
                "",
                f"**Crosspost from {source}:** {parent.get('title') or 'Untitled'}",
                *( [parent_url] if parent_url else [] ),
            ]
        )
        # The wrapper's parent is often the substantive post. Preserve its
        # body/media/poll rather than reducing it to a title and link.
        parent_rendered = _render_post(parent, _crosspost_depth=_crosspost_depth + 1)
        if parent_rendered:
            parent_rendered = parent_rendered.replace("# ", "### ", 1)
            lines.extend(["", "## Crosspost content", "", parent_rendered])

    poll = data.get("poll_data") or {}
    options = poll.get("options") or []
    if options:
        total_votes = poll.get("total_vote_count")
        heading = "**Poll"
        if total_votes is not None:
            heading += f" · {int(total_votes):,} votes"
        heading += ":**"
        lines.extend(["", heading])
        for option in options:
            votes = option.get("vote_count")
            suffix = f" — {int(votes):,}" if votes is not None else ""
            lines.append(f"- {option.get('text') or '(untitled option)'}{suffix}")
        voting_end = poll.get("voting_end_timestamp")
        if voting_end:
            voting_end_seconds = _unix_seconds(voting_end)
            state = (
                "closed"
                if voting_end_seconds is not None
                and voting_end_seconds <= time.time()
                else "voting ends"
            )
            lines.append(f"- {state.title()}: {_created(voting_end)}")

    if urls:
        lines.extend(["", *urls])
    if discussion:
        lines.extend(["", f"**Discussion:** {discussion}"])
    return "\n".join(lines).strip()


def _rich_comment_urls(data: dict, body: str) -> list[str]:
    urls: list[str] = []
    body_html = data.get("body_html") or ""
    if body_html:
        soup = BeautifulSoup(unescape(body_html), "lxml")
        for tag in soup.find_all(["a", "img", "video", "source"]):
            urls.extend([tag.get("href") or "", tag.get("src") or ""])
    for media in (data.get("media_metadata") or {}).values():
        urls.extend(_media_metadata_urls(media or {}))
    return [url for url in _unique_urls(urls) if url not in body]


def _comment_navigation(
    data: dict,
    *,
    post_id: str | None = None,
) -> tuple[str, str]:
    """Return a canonical comment permalink and its bounded parent context."""

    comment_id = str(data.get("id") or "")
    link_id = str(data.get("link_id") or "")
    resolved_post_id = (
        link_id.removeprefix("t3_")
        if re.fullmatch(r"t3_[A-Za-z0-9]{2,16}", link_id)
        else str(post_id or "")
    )
    permalink = _absolute_reddit_url(data.get("permalink"))
    if (
        not permalink
        and _THING_ID_RE.fullmatch(resolved_post_id)
        and _THING_ID_RE.fullmatch(comment_id)
    ):
        permalink = (
            "https://www.reddit.com/comments/"
            f"{quote(resolved_post_id)}/_/{quote(comment_id)}/"
        )

    parent_id = str(data.get("parent_id") or "")
    parent_context = ""
    if parent_id.startswith("t1_") and _THING_ID_RE.fullmatch(parent_id[3:]):
        if permalink:
            parsed = urlparse(permalink)
            query = parse_qs(parsed.query, keep_blank_values=True)
            query["context"] = ["3"]
            parent_context = urlunparse(
                parsed._replace(query=urlencode(query, doseq=True))
            )
        elif _THING_ID_RE.fullmatch(resolved_post_id):
            parent_context = (
                "https://www.reddit.com/comments/"
                f"{quote(resolved_post_id)}/_/{quote(parent_id[3:])}/?context=3"
            )
    elif parent_id.startswith("t3_") and _THING_ID_RE.fullmatch(parent_id[3:]):
        parent_context = (
            f"https://www.reddit.com/comments/{quote(parent_id[3:])}/"
        )
    elif _THING_ID_RE.fullmatch(resolved_post_id):
        parent_context = (
            f"https://www.reddit.com/comments/{quote(resolved_post_id)}/"
        )
    return permalink, parent_context


def _comment_body_text(value: object) -> str:
    """Return a comment body without leading or trailing blank lines.

    Reddit bodies sometimes open with whitespace-only lines, and rendering them
    verbatim puts blank lines between a comment's heading and its first real
    content -- the card then reads as an empty comment even though the body is
    there. Only fully blank lines are dropped, so a body that genuinely starts
    with an indented code block keeps its indentation. A body that is blank all
    the way through is returned unchanged rather than replaced with a sentinel
    that would claim more than Reddit said.
    """

    text = str(value or "[deleted]")
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) if lines else text


def _render_comment(
    data: dict,
    depth: int,
    selected_comment_id: str | None = None,
    *,
    post_id: str | None = None,
) -> str:
    author = data.get("author") or "[deleted]"
    score_text = _score_summary(data)
    selected = " · **selected comment**" if data.get("id") == selected_comment_id else ""
    # Markdown stops at six heading levels, so the heading alone cannot express
    # nesting past depth 3. The branch marker has no such limit, and capping it
    # too made a depth-9 reply indistinguishable from a depth-3 one -- the reply
    # structure of any deep thread was silently flattened. Bound it only by the
    # deepest level a thread is ever fetched at.
    heading_level = min(6, 3 + depth)
    branch = "↳ " * min(depth, _MAX_THREAD_DEPTH)
    distinctions = []
    if _flag(data, "is_submitter"):
        distinctions.append("OP")
    if data.get("distinguished") == "moderator":
        distinctions.append("mod")
    elif data.get("distinguished") == "admin":
        distinctions.append("admin")
    if data.get("controversiality"):
        distinctions.append("controversial")
    if _flag(data, "stickied"):
        distinctions.append("stickied")
    if _flag(data, "locked"):
        distinctions.append("locked")
    if _flag(data, "archived"):
        distinctions.append("archived")
    if _flag(data, "collapsed"):
        collapsed_reason = str(
            data.get("collapsed_reason")
            or data.get("collapsed_reason_code")
            or ""
        ).strip()
        if _flag(data, "collapsed_because_crowd_control") and not collapsed_reason:
            collapsed_reason = "crowd control"
        distinctions.append(
            f"collapsed: {collapsed_reason}" if collapsed_reason else "collapsed"
        )
    author_flair, author_flair_urls = _flair(data, "author")
    if author_flair:
        distinctions.append(f"flair: {author_flair}")
    distinction = f" · {', '.join(distinctions)}" if distinctions else ""
    body = _comment_body_text(data.get("body"))
    lines = [
        f"{'#' * heading_level} {branch}u/{author} · {score_text} · {_created(data.get('created_utc'))}{distinction}{selected}",
        "",
        body,
    ]
    if data.get("edited"):
        lines.extend(["", f"Edited: {_created(data.get('edited'))}"])
    awards = _award_summary(data)
    if awards:
        lines.extend(["", f"Awards: {awards}"])
    archived_gilding = _archived_gilding_evidence(data)
    if archived_gilding:
        lines.extend(["", f"Archived gilding evidence: {archived_gilding}"])
    rich_urls = _rich_comment_urls(data, body)
    rich_urls.extend(author_flair_urls)
    if rich_urls:
        lines.extend(["", *(f"Media: {url}" for url in _unique_urls(rich_urls))])
    permalink, parent_context = _comment_navigation(
        data,
        post_id=post_id,
    )
    if permalink:
        lines.extend(["", f"Permalink: {permalink}"])
    if parent_context:
        lines.append(f"Parent context: {parent_context}")
    return "\n".join(lines)


def _more_comment_sections(
    data: dict,
    *,
    post_id: str,
    sort: str,
    depth: int,
) -> list[tuple[str, int, bool]]:
    count = data.get("count")
    child_ids = data.get("children") or []
    count = int(count) if isinstance(count, (int, float)) else len(child_ids)
    valid_ids = [
        value
        for value in child_ids
        if isinstance(value, str) and _THING_ID_RE.fullmatch(value)
    ]
    link_id = data.get("link_id")
    if not (
        isinstance(link_id, str)
        and re.fullmatch(r"t3_[A-Za-z0-9]{2,16}", link_id)
    ):
        link_id = f"t3_{post_id}"
    sections: list[tuple[str, int, bool]] = []
    if valid_ids and _THING_ID_RE.fullmatch(post_id):
        for batch_start in range(0, len(valid_ids), 20):
            batch = valid_ids[batch_start : batch_start + 20]
            continuation = (
                "https://www.reddit.com/api/morechildren?"
                + urlencode(
                    {
                        "link_id": link_id,
                        "children": ",".join(batch),
                        "sort": sort,
                        "depth": str(min(max(depth, 0), 10)),
                    }
                )
            )
            sections.append(
                (
                    f"[Load {len(batch):,} more "
                    f"{'reply' if len(batch) == 1 else 'replies'}: "
                    f"{continuation}]",
                    0,
                    True,
                )
            )
        return sections

    parent_id = data.get("parent_id")
    if (
        isinstance(parent_id, str)
        and parent_id.startswith("t1_")
        and _THING_ID_RE.fullmatch(parent_id[3:])
        and _THING_ID_RE.fullmatch(post_id)
    ):
        continuation = (
            f"https://www.reddit.com/comments/{quote(post_id)}/_/"
            f"{quote(parent_id[3:])}/?context=3"
        )
        return [(f"[Continue this thread: {continuation}]", 0, True)]
    return [
        (
            f"[{count:,} more "
            f"{'reply' if count == 1 else 'replies'} omitted; "
            "Reddit returned no continuation IDs]",
            max(count, 0),
            False,
        )
    ]


def _walk_comments(
    children: list[dict],
    selected_comment_id: str | None,
    *,
    post_id: str,
    sort: str,
    depth: int = 0,
):
    for child in children:
        kind = child.get("kind")
        data = child.get("data") or {}
        if kind == "more":
            yield from _more_comment_sections(
                data,
                post_id=post_id,
                sort=sort,
                depth=depth,
            )
            continue
        if kind != "t1":
            continue
        yield (
            _render_comment(
                data,
                depth,
                selected_comment_id,
                post_id=post_id,
            ),
            1,
            False,
        )
        replies = data.get("replies")
        if isinstance(replies, dict):
            nested = (replies.get("data") or {}).get("children") or []
            yield from _walk_comments(
                nested,
                selected_comment_id,
                post_id=post_id,
                sort=sort,
                depth=depth + 1,
            )


def _listing_children(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    if not isinstance(children, list):
        return []
    return [child for child in children if isinstance(child, dict)]


def parse_reddit_wiki_pages_html(
    html: str,
    subreddit: str,
) -> list[str] | None:
    """Return the canonical New Reddit SSR wiki tree, or reject the document.

    Reddit no longer serves ``/wiki/pages.json`` to anonymous callers.
    New Reddit still server-renders a complete page tree for communities that
    expose one, inside the wiki right rail.  Parsing only that named tree avoids
    confusing authored page links, community bookmarks, or navigation chrome
    with the authoritative page index.
    """

    if (
        not isinstance(html, str)
        or not html
        or _SUBREDDIT_RE.fullmatch(subreddit) is None
    ):
        return None
    soup = BeautifulSoup(html, "lxml")
    app = soup.select_one(
        'shreddit-app[pagetype="community_wiki"][routename="subreddit_wiki"]'
    )
    if app is None:
        return None
    canonical = app.select_one("#canonical-url-updater[value]")
    tree = app.select_one("#wikis-right-rail-container .page-tree")
    if canonical is None or tree is None:
        return None

    try:
        canonical_url = urlparse(
            unescape(str(canonical.get("value") or "")).strip()
        )
        canonical_port = canonical_url.port
    except (TypeError, ValueError):
        return None
    expected_path = f"/r/{subreddit}/wiki/pages/"
    if (
        canonical_url.scheme != "https"
        or (canonical_url.hostname or "").rstrip(".").casefold()
        != "www.reddit.com"
        or canonical_port not in (None, 443)
        or canonical_url.username is not None
        or canonical_url.password is not None
        or canonical_url.params
        or canonical_url.query
        or canonical_url.fragment
        or canonical_url.path.casefold() != expected_path.casefold()
    ):
        return None

    anchors = tree.find_all("a", href=True)
    if not anchors or len(anchors) > 10_000:
        return None
    prefix_parts = ("r", subreddit, "wiki")
    pages: list[str] = []
    seen: set[str] = set()
    for anchor in anchors:
        raw_href = unescape(str(anchor.get("href") or "")).strip()
        if (
            not raw_href
            or len(raw_href) > 4096
            or any(
                ord(character) <= 0x20
                or ord(character) == 0x7F
                or character == "\\"
                for character in raw_href
            )
        ):
            return None
        try:
            href = urlparse(raw_href)
            port = href.port
        except (TypeError, ValueError):
            return None
        if href.scheme or href.netloc:
            if (
                href.scheme != "https"
                or (href.hostname or "").rstrip(".").casefold()
                != "www.reddit.com"
                or port not in (None, 443)
                or href.username is not None
                or href.password is not None
            ):
                return None
        if href.params or href.query or href.fragment:
            return None
        if not href.path.startswith("/"):
            return None
        path_body = href.path[1:]
        if path_body.endswith("/"):
            path_body = path_body[:-1]
        if not path_body:
            return None
        raw_parts = path_body.split("/")
        if any(not part for part in raw_parts):
            return None
        if len(raw_parts) < 4:
            return None
        decoded_parts = [unquote(part) for part in raw_parts]
        if (
            tuple(part.casefold() for part in decoded_parts[:3])
            != tuple(part.casefold() for part in prefix_parts)
        ):
            return None
        wiki_parts = decoded_parts[3:]
        if not _safe_wiki_parts(wiki_parts) or any(
            "/" in part
            or "\\" in part
            or any(
                ord(character) <= 0x20 or ord(character) == 0x7F
                for character in part
            )
            for part in wiki_parts
        ):
            return None
        page = "/".join(wiki_parts)
        if len(page) > 1024:
            return None
        page_key = page.casefold()
        if page_key in seen:
            continue
        seen.add(page_key)
        pages.append(page)
    return pages or None


def parse_reddit_wiki_page_tree(
    payload: object,
    subreddit: str,
) -> list[str] | None:
    """Return the authoritative anonymous wiki page index, or reject the payload.

    New Reddit's ``WikiPageRevisionsV2`` operation answers logged-out callers
    with the same ``pageTree`` the wiki UI renders, so no OAuth scope and no
    browser are required.  Every structural claim is checked here: a tree whose
    subreddit identity, path/parent/depth agreement, or uniqueness does not hold
    is rejected outright rather than rendered as a short page list.

    Nodes with ``isPagePresent`` false are namespace parents (``config`` above
    ``config/sidebar``), not pages, so they are excluded exactly as Reddit's own
    page index excludes them.  A valid tree with no present pages returns an
    empty list; only malformed payloads return ``None``.
    """

    if not isinstance(payload, dict) or _SUBREDDIT_RE.fullmatch(subreddit) is None:
        return None
    if payload.get("errors"):
        return None
    data = payload.get("data")
    community = data.get("subreddit") if isinstance(data, dict) else None
    if not isinstance(community, dict):
        return None
    if community.get("__typename") != "Subreddit":
        return None
    name = community.get("name")
    if not isinstance(name, str) or name.casefold() != subreddit.casefold():
        return None
    prefixed = community.get("prefixedName")
    if prefixed is not None and (
        not isinstance(prefixed, str)
        or prefixed.casefold() != f"r/{name}".casefold()
    ):
        return None
    wiki = community.get("wiki")
    index = wiki.get("index") if isinstance(wiki, dict) else None
    tree = index.get("pageTree") if isinstance(index, dict) else None
    if not isinstance(tree, list) or len(tree) > 10_000:
        return None

    pages: list[str] = []
    seen: set[str] = set()
    for node in tree:
        if not isinstance(node, dict):
            return None
        path = node.get("path")
        node_name = node.get("name")
        parent = node.get("parent")
        depth = node.get("depth")
        present = node.get("isPagePresent")
        if (
            not isinstance(path, str)
            or not isinstance(node_name, str)
            or not isinstance(present, bool)
            or not isinstance(depth, int)
            or isinstance(depth, bool)
            or (parent is not None and not isinstance(parent, str))
        ):
            return None
        if len(path) > 1024 or "\\" in path or any(
            ord(character) <= 0x20 or ord(character) == 0x7F
            for character in path
        ):
            return None
        parts = path.split("/")
        if not _safe_wiki_parts(parts):
            return None
        if parts[-1] != node_name:
            return None
        expected_parent = "/".join(parts[:-1]) or None
        if expected_parent != parent or depth != len(parts) - 1:
            return None
        page_key = path.casefold()
        if page_key in seen:
            return None
        seen.add(page_key)
        if present:
            pages.append(path)
    return pages


def parse_reddit_related_html(html: str, limit: int) -> dict:
    """Convert New Reddit's related-post partial into a normal bounded Listing."""

    soup = BeautifulSoup(html, "lxml")
    related_cards = soup.select("reddit-pdp-right-rail-post[event-data]")
    source_valid = bool(related_cards) or soup.select_one(
        'aside[aria-label="Related Posts Section"], '
        "shreddit-post-overflow-menu + aside, "
        "#pdp-right-rail"
    ) is not None
    children: list[dict] = []
    seen: set[str] = set()
    invalid_count = 0
    for element in related_cards:
        try:
            event = json.loads(unquote(str(element.get("event-data") or "")))
        except (TypeError, ValueError):
            invalid_count += 1
            continue
        post = event.get("post") if isinstance(event, dict) else None
        if not isinstance(post, dict):
            invalid_count += 1
            continue
        # Strict: a truthy non-boolean would silently DROP a real post
        # from the listing, which is data loss, not a mislabel.
        if post.get("promoted") is True:
            continue
        fullname = str(post.get("id") or "")
        if not re.fullmatch(r"t3_[A-Za-z0-9]{2,16}", fullname):
            invalid_count += 1
            continue
        post_id = fullname[3:]
        if post_id in seen:
            continue
        title = str(post.get("title") or "").strip()
        subreddit = str(post.get("subreddit_name") or "").strip()
        permalink_value = str(post.get("url") or "").strip()
        permalink_parsed = urlparse(permalink_value)
        if (
            not title
            or _SUBREDDIT_RE.fullmatch(subreddit) is None
            or not is_reddit_host(permalink_parsed.hostname or "")
            or "/comments/" not in permalink_parsed.path
        ):
            invalid_count += 1
            continue

        external_url = ""
        for link in element.find_all("a", href=True):
            candidate = unescape(str(link.get("href") or "")).strip()
            parsed_candidate = urlparse(candidate)
            if (
                parsed_candidate.scheme in {"http", "https"}
                and parsed_candidate.hostname
                and not is_reddit_host(parsed_candidate.hostname)
            ):
                external_url = candidate
                break
        if not external_url and str(post.get("type") or "") in {
            "image",
            "video",
        }:
            image = element.find("img", src=True)
            if image is not None:
                candidate = unescape(str(image.get("src") or "")).strip()
                if candidate.startswith(("http://", "https://")):
                    external_url = candidate

        created = post.get("created_timestamp")
        if isinstance(created, (int, float)) and created > 10_000_000_000:
            created = created / 1000
        data = {
            "id": post_id,
            "name": fullname,
            "title": title,
            "subreddit": subreddit,
            "subreddit_name_prefixed": f"r/{subreddit}",
            "author": "[unknown]",
            "score": post.get("score"),
            "num_comments": post.get("number_comments"),
            "created_utc": created,
            "permalink": permalink_parsed.path,
            "url": external_url or _absolute_reddit_url(permalink_parsed.path),
            "url_overridden_by_dest": external_url or None,
            "is_self": str(post.get("type") or "") == "self",
            # ``bool()`` on an arbitrary embedded value makes the string
            # "false" mean True, which then renders as an NSFW/Archived label.
            "over_18": post.get("nsfw") is True,
            "archived": post.get("archived") is True,
        }
        children.append({"kind": "t3", "data": data})
        seen.add(post_id)
        if len(children) >= max(1, limit):
            break
    return {
        "kind": "Listing",
        "_related_partial_valid": source_valid,
        "_related_partial_invalid_count": invalid_count,
        "data": {
            "children": children,
            "after": None,
            "before": None,
            "dist": len(children),
        },
    }


def normalize_moderator_roster_children(
    payload: object,
    *,
    allow_wrapped: bool = True,
) -> list[dict[str, object]] | None:
    """Return one flat moderator schema, or reject an ambiguous roster.

    Reddit's public listing shape wraps each user in ``{"kind": "t2",
    "data": ...}``, while the OAuth UserList response supplies flat children.
    Accept either complete representation, but never merge a child that mixes
    fields from both layers: choosing one side would make an untrusted response
    silently change a moderator's identity or permissions.
    """

    children = _listing_children(payload)
    if not isinstance(payload, dict) or not isinstance(
        (payload.get("data") or {}).get("children"), list
    ):
        return None

    normalized: list[dict[str, object]] = []
    moderator_fields = frozenset({"name", "mod_permissions", "date"})
    for child in children:
        if not isinstance(child, dict):
            return None
        if "data" in child:
            if (
                not allow_wrapped
                or (
                    child.get("kind") != "t2"
                    or moderator_fields.intersection(child)
                    or not isinstance(child["data"], dict)
                )
            ):
                return None
            record = child["data"]
        else:
            record = child

        name = record.get("name")
        permissions = record.get("mod_permissions")
        added_at = record.get("date")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,20}", name) is None
            or not isinstance(permissions, list)
            or any(
                not isinstance(permission, str)
                or not permission
                or len(permission) > 64
                or not permission.isascii()
                or any(
                    not 0x21 <= ord(character) <= 0x7E
                    for character in permission
                )
                for permission in permissions
            )
            or (
                added_at is not None
                and (
                    isinstance(added_at, bool)
                    or not isinstance(added_at, (int, float))
                    or not isfinite(float(added_at))
                )
            )
        ):
            return None
        normalized.append(
            {
                "name": name,
                "mod_permissions": permissions,
                "date": added_at,
            }
        )
    return normalized


def _fit_sections(
    prefix: str,
    sections: list[tuple[str, int]],
    max_chars: int,
    *,
    omission_label: str,
    required_sections: list[tuple[str, int]] | None = None,
    introduction_label: str = "Page introduction",
) -> str:
    """Fit whole sections, reserving navigation and an exact omission marker."""

    max_chars = max(1, max_chars)
    # Canonicalization can expand protocol-relative authored links. Do it
    # before measuring so the final canonicalization pass cannot invalidate a
    # whole-card/comment/fence boundary chosen here.
    prefix = _canonicalize_embedded_reddit_links(prefix)
    sections = [
        (_canonicalize_embedded_reddit_links(section), count)
        for section, count in sections
    ]
    required_sections = [
        (_canonicalize_embedded_reddit_links(section), count)
        for section, count in (required_sections or [])
    ]
    required = [section for section, _ in (required_sections or []) if section]

    def _marker(detailed: str, budget: int = max_chars) -> str:
        for candidate in (detailed, "[truncated]", "…"):
            if len(candidate) <= budget:
                return candidate
        return ""

    def _omission_marker(count: int, omitted_any: bool = True) -> str:
        if not omitted_any:
            return ""
        if count:
            return (
                f"[{count:,} {omission_label} omitted due to output limit]"
            )
        return f"[Additional {omission_label} omitted due to output limit]"

    def _fit_prefix(value: str, available: int) -> tuple[str, bool]:
        if len(value) <= available:
            return value, False
        if available <= 0:
            return "", True
        marker = _marker(
            f"[{introduction_label} truncated at output limit]",
            available,
        )
        if not marker:
            return "", True
        if len(marker) >= available:
            return marker, True
        paragraph_budget = available - len(marker) - 2
        kept: list[str] = []
        for paragraph in value.split("\n\n"):
            candidate = "\n\n".join([*kept, paragraph]) if kept else paragraph
            if len(candidate) > paragraph_budget:
                break
            kept.append(paragraph)
        if not kept and paragraph_budget >= 4:
            first_line = value.splitlines()[0].strip()
            heading = "# " if first_line.startswith("# ") else ""
            text = first_line[2:] if heading else first_line
            shortened = (
                heading
                + text[: max(1, paragraph_budget - len(heading) - 1)]
                + "…"
            )
            kept.append(shortened[:paragraph_budget])
        body = "\n\n".join(kept).rstrip()
        return (f"{body}\n\n{marker}" if body else marker), True

    complete = "\n\n".join(
        part
        for part in [prefix, *(section for section, _ in sections), *required]
        if part
    )
    if len(complete) <= max_chars:
        return complete

    join_required = "\n\n".join(required)
    required_reserve = len(join_required) + (2 if join_required else 0)
    total_represented = sum(max(0, count) for _, count in sections)
    initial_omission = _omission_marker(total_represented)
    worst_marker_reserve = len(initial_omission) + (2 if sections else 0)
    prefix_budget = max_chars - required_reserve
    if sections:
        prefix_budget -= worst_marker_reserve
    fitted_prefix, prefix_truncated = _fit_prefix(prefix, max(0, prefix_budget))
    parts = [fitted_prefix] if fitted_prefix else []
    omitted = total_represented
    omitted_any = bool(sections)
    included_any = False
    for index, (section, represented_count) in enumerate(sections):
        remaining_sections = sections[index + 1 :]
        prospective_omitted = sum(
            max(0, remaining_count)
            for _, remaining_count in remaining_sections
        )
        candidate_parts = [*parts, section, *required]
        if remaining_sections:
            candidate_parts.append(_omission_marker(prospective_omitted))
        candidate = "\n\n".join(part for part in candidate_parts if part)
        if len(candidate) > max_chars:
            break
        parts.append(section)
        included_any = True
        omitted = prospective_omitted
        omitted_any = bool(remaining_sections)

    output_parts = [*parts, *required]
    if omitted_any:
        output_parts.append(_omission_marker(omitted))
    elif sections and not included_any:
        output_parts.append(
            f"[Additional {omission_label} omitted due to output limit]"
        )
    output = "\n\n".join(part for part in output_parts if part)
    if len(output) <= max_chars:
        return output

    # A very small caller budget may not fit even the heading, required
    # navigation and compact marker. Prefer reachable navigation, then a clear
    # marker, and never slice a rendered card or Markdown fence.
    compact_marker = _marker(
        _omission_marker(omitted, omitted_any)
        or "[Page truncated at output limit]"
    )
    compact_parts = [*required, compact_marker]
    compact = "\n\n".join(compact_parts)
    if len(compact) <= max_chars:
        return compact
    if prefix_truncated:
        return compact_marker
    return _marker("[Page truncated at output limit]")


def _render_thread(payload: object, route: RedditRoute, max_chars: int) -> str:
    if not isinstance(payload, list) or not payload:
        return "# Reddit thread\n\nNo post data returned."
    posts = _listing_children(payload[0])
    if not posts:
        return "# Reddit thread\n\nPost not found."
    post_data = posts[0].get("data") or {}
    post = _render_post(post_data)
    comments = _listing_children(payload[1]) if len(payload) > 1 else []
    query = parse_qs(urlparse(route.canonical_url).query, keep_blank_values=True)
    requested_sort = _first_query(query, "sort")
    sort = requested_sort if requested_sort in _COMMENT_SORTS else "confidence"
    walked = list(
        _walk_comments(
            comments,
            route.selected_comment_id,
            post_id=str(post_data.get("id") or ""),
            sort=sort,
        )
    )
    # Preserve tree order. Continuation links are useful sections, but they
    # must never consume the required-section reserve and erase the post that
    # gives them meaning under a tight output budget.
    sections = [(text, count) for text, count, _required in walked]
    if not sections:
        return _fit_sections(
            f"{post}\n\n## Comments\n\nNo comments returned.",
            [],
            max_chars,
            omission_label="comments",
            introduction_label="Post content",
        )
    return _fit_sections(
        f"{post}\n\n## Comments",
        sections,
        max_chars,
        omission_label="comments/replies",
        introduction_label="Post content",
    )


def _render_listing(payload: object, route: RedditRoute, max_chars: int) -> str:
    children = _listing_children(payload)
    listing_data = payload.get("data") if isinstance(payload, dict) else {}
    listing_data = listing_data or {}
    place = (
        f"r/{route.subreddit}"
        if route.subreddit
        else f"u/{route.username}"
        if route.username
        else "Reddit"
    )
    label = route.label or "listing"
    query = parse_qs(
        urlparse(route.canonical_url).query,
        keep_blank_values=True,
    )
    if route.kind == "search":
        requested_sort = _first_query(query, "sort")
        search_sort = (
            requested_sort
            if requested_sort in _SEARCH_SORTS
            else "relevance"
        )
        requested_time = _first_query(query, "t")
        time_filter = (
            requested_time
            if requested_time in _TIME_FILTERS
            else "all"
        )
        requested_types = (_first_query(query, "type") or "link").split(",")
        search_types = [
            value for value in requested_types if value in {"link", "sr", "user"}
        ] or ["link"]
        label = (
            f"{label} · sort {search_sort} · time {time_filter} · "
            f"type {','.join(search_types)}"
        )
    else:
        requested_sort = _first_query(query, "sort")
        if route.username and requested_sort in _PROFILE_SORTS:
            label = f"{label} · sort {requested_sort}"
        requested_time = _first_query(query, "t")
        if requested_time in _TIME_FILTERS:
            label = f"{label} · time {requested_time}"
    prefix = f"# {place} · {label}\n\n{len(children)} items returned"
    if (
        isinstance(payload, dict)
        and payload.get("_fetchaller_reddit_provenance")
        == "wayback_gold_directory"
    ):
        timestamp = str(
            payload.get("_fetchaller_reddit_archive_timestamp") or ""
        )
        archived_date = (
            f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
            if re.fullmatch(r"\d{14}", timestamp)
            else "unknown date"
        )
        prefix += (
            "\n\nDirectory state source: exact archived Reddit snapshot "
            f"(Wayback, {archived_date}). No gold-only communities were "
            "listed."
        )
    if (
        isinstance(payload, dict)
        and payload.get("_fetchaller_reddit_provenance") == "wayback"
    ):
        timestamp = str(
            payload.get("_fetchaller_reddit_archive_timestamp") or ""
        )
        archived_date = (
            f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
            if re.fullmatch(r"\d{14}", timestamp)
            else "unknown date"
        )
        prefix += (
            "\n\nGilded ordering source: archived Reddit snapshot "
            f"(Wayback, {archived_date}). Item details: current Reddit API."
        )
    sections = []
    include_subreddit = (
        route.subreddit is None
        or "+" in route.subreddit
        or "-" in route.subreddit
        or route.subreddit.lower() in {"all", "popular"}
    )
    for index, child in enumerate(children, 1):
        data = child.get("data") or {}
        if child.get("kind") == "t3":
            sections.append((format_reddit_post(data, index, include_subreddit=include_subreddit), 1))
        elif child.get("kind") == "t1":
            sections.append((_render_activity_comment(data, index), 1))
        elif child.get("kind") == "t5":
            sections.append(
                (_render_subreddit_directory_item(data, index, route.label), 1)
            )
        elif child.get("kind") == "t2":
            sections.append((_render_user_directory_item(data, index), 1))
        else:
            sections.append(
                (
                    f"{index}. [Unsupported Reddit item kind: "
                    f"{child.get('kind') or 'unknown'}]",
                    1,
                )
            )
    pagination = _pagination_sections(payload, route.canonical_url)
    return _fit_sections(
        prefix,
        sections,
        max_chars,
        omission_label="items",
        required_sections=pagination,
    )


def _pagination_url(
    canonical_url: str,
    name: str,
    cursor: str,
    *,
    count: int | None = None,
) -> str:
    parsed = urlparse(canonical_url)
    allowed_query = {
        "q",
        "sort",
        "t",
        "type",
        "context",
        "show",
        "restrict_sr",
        "num",
        "limit",
        "category",
        "include_facets",
        "g",
        "crossposts_only",
        "sr",
        "show_users",
        "typeahead_active",
        "search_query_id",
        "sr_detail",
    }
    query = {
        key: [value[:512] for value in values[:1]]
        for key, values in parse_qs(
            parsed.query,
            keep_blank_values=True,
        ).items()
        if key in allowed_query
    }
    for boolean_name in ("sr_detail", "show_users", "typeahead_active"):
        values = query.get(boolean_name)
        normalized = (
            values[0].casefold()
            if values and isinstance(values[0], str)
            else ""
        )
        if normalized in {"1", "true"}:
            query[boolean_name] = ["true"]
        elif normalized in {"0", "false"}:
            query[boolean_name] = ["false"]
        else:
            query.pop(boolean_name, None)
    search_query_id = query.get("search_query_id")
    if not (
        search_query_id
        and _UUID_RE.fullmatch(search_query_id[0])
    ):
        query.pop("search_query_id", None)
    query.pop("before", None)
    query.pop("after", None)
    query[name] = [cursor]
    if count is not None:
        query["count"] = [str(max(0, min(count, 1000)))]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def _pagination_sections(
    payload: object,
    canonical_url: str,
    *,
    cursor_pattern: re.Pattern[str] = _CURSOR_RE,
) -> list[tuple[str, int]]:
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data or {}
    sections: list[tuple[str, int]] = []
    parsed_query = parse_qs(urlparse(canonical_url).query, keep_blank_values=True)
    raw_count = _first_query(parsed_query, "count")
    current_count = min(int(raw_count), 1000) if raw_count and raw_count.isdigit() else 0
    dist_value = data.get("dist")
    dist = (
        int(dist_value)
        if isinstance(dist_value, (int, float)) and dist_value > 0
        else len(_listing_children(payload))
    )
    before = data.get("before")
    after = data.get("after")
    if isinstance(before, str) and cursor_pattern.fullmatch(before):
        previous_count = (
            max(0, current_count - dist) if current_count or dist else None
        )
        sections.append(
            (
                f"[Previous page: "
                f"{_pagination_url(canonical_url, 'before', before, count=previous_count)}]",
                0,
            )
        )
    if isinstance(after, str) and cursor_pattern.fullmatch(after):
        next_count = current_count + dist if current_count or dist else None
        sections.append(
            (
                f"[Next page: "
                f"{_pagination_url(canonical_url, 'after', after, count=next_count)}]",
                0,
            )
        )
    return sections


def _render_activity_comment(data: dict, index: int) -> str:
    subreddit = data.get("subreddit_name_prefixed") or (
        f"r/{data.get('subreddit')}" if data.get("subreddit") else "Reddit"
    )
    author = data.get("author") or "[deleted]"
    body = _comment_body_text(data.get("body"))
    permalink = _absolute_reddit_url(data.get("permalink"))
    title = data.get("link_title") or "Comment"
    author_flair, author_flair_urls = _flair(data, "author")
    states = []
    if _flag(data, "stickied"):
        states.append("stickied")
    if _flag(data, "locked"):
        states.append("locked")
    if _flag(data, "archived"):
        states.append("archived")
    # ``controversiality`` is an int (0/1), not a boolean -- truthiness is right.
    if data.get("controversiality"):
        states.append("controversial")
    if _flag(data, "collapsed"):
        states.append("collapsed")
    lines = [
        f"{index}. **{title}**",
        (
            f"   {subreddit} · u/{author}"
            + (
                f" · author flair: {author_flair}"
                if author_flair
                else ""
            )
            + f" · {_score_summary(data)} · {_created(data.get('created_utc'))}"
            + (f" · {', '.join(states)}" if states else "")
        ),
        "",
        body,
    ]
    if data.get("edited"):
        lines.extend(["", f"Edited: {_created(data.get('edited'))}"])
    rich_urls = _rich_comment_urls(data, body)
    rich_urls.extend(author_flair_urls)
    if rich_urls:
        lines.extend(["", *(f"Media: {url}" for url in _unique_urls(rich_urls))])
    awards = _award_summary(data)
    if awards:
        lines.extend(["", f"Awards: {awards}"])
    archived_gilding = _archived_gilding_evidence(data)
    if archived_gilding:
        lines.extend(["", f"Archived gilding evidence: {archived_gilding}"])
    comment_permalink, parent_context = _comment_navigation(data)
    if comment_permalink or permalink:
        lines.extend(["", f"Permalink: {comment_permalink or permalink}"])
    if parent_context:
        lines.append(f"Parent context: {parent_context}")
    return "\n".join(lines)


def _render_user_about(payload: object) -> str:
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data or {}
    name = data.get("name") or "[unknown]"
    lines = [f"# u/{name}"]
    # Strictly ``is True``: labelling a live account "suspended" off a truthy
    # non-boolean (Reddit's own string "false" included) is a factual error.
    if data.get("is_suspended") is True:
        lines.extend(["", "**Suspended account**"])
    # ``or {}`` only rescues falsy values: a truthy non-mapping (a string or a
    # list) reached ``.get`` and crashed the whole render with AttributeError.
    profile_subreddit = data.get("subreddit")
    if not isinstance(profile_subreddit, dict):
        profile_subreddit = {}
    if profile_subreddit.get("title"):
        lines.extend(["", str(profile_subreddit["title"])])
    if profile_subreddit.get("public_description"):
        lines.extend(["", str(profile_subreddit["public_description"])])
    fields = [
        ("Post karma", data.get("link_karma")),
        ("Comment karma", data.get("comment_karma")),
        ("Total karma", data.get("total_karma")),
        ("Created", _created(data.get("created_utc")) if data.get("created_utc") else None),
        ("Verified", "yes" if _flag(data, "verified") else None),
        ("Reddit employee", "yes" if _flag(data, "is_employee") else None),
        ("Reddit Premium", "yes" if _flag(data, "is_gold") else None),
    ]
    lines.extend(f"- **{label}:** {value}" for label, value in fields if value is not None)
    icon = unescape(profile_subreddit.get("icon_img") or data.get("icon_img") or "")
    if icon:
        lines.append(f"- **Avatar:** {icon}")
    lines.append(f"- **Profile:** https://www.reddit.com/user/{quote(str(name), safe='')}/")
    return "\n".join(lines)


def _counter_text(value: object) -> str | None:
    """Format a Reddit count, or ``None`` when it is not a usable number.

    Booleans are excluded because ``bool`` is an ``int`` subclass and would
    otherwise render as "1 subscribers".
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return f"{int(value):,}"
    except (ValueError, OverflowError):
        return None


def _render_subreddit_about(payload: object) -> str:
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data or {}
    name = data.get("display_name_prefixed") or f"r/{data.get('display_name') or '?'}"
    lines = [f"# {name}", "", str(data.get("title") or "")]
    labels = []
    if _flag(data, "over18"):
        labels.append("NSFW")
    if data.get("subreddit_type") and data.get("subreddit_type") != "public":
        labels.append(str(data["subreddit_type"]).title())
    if labels:
        lines.extend(["", f"**{' · '.join(labels)}**"])
    fields = [
        (
            "Status",
            str(data.get("subreddit_type") or "public")
            .replace("_", " ")
            .title(),
        ),
        # ``int()`` on a non-numeric value raised ValueError out of the whole
        # render; an unusable count is simply omitted, like any absent field.
        ("Subscribers", _counter_text(data.get("subscribers"))),
        ("Active now", _counter_text(data.get("accounts_active"))),
        ("Created", _created(data.get("created_utc")) if data.get("created_utc") else None),
    ]
    lines.extend(["", *(f"- **{label}:** {value}" for label, value in fields if value is not None)])
    public_description = str(data.get("public_description") or "").strip()
    full_description = str(data.get("description") or "").strip()
    if public_description:
        lines.extend(["", public_description])
    if full_description and full_description != public_description:
        lines.extend(["", "## About", "", full_description])
    return "\n".join(lines).strip()


def _render_rules(payload: object, route: RedditRoute) -> str:
    data = payload if isinstance(payload, dict) else {}
    rules = data.get("rules") or []
    lines = [f"# Rules for r/{route.subreddit or '?'}"]
    for index, rule in enumerate(rules, 1):
        title = rule.get("short_name") or f"Rule {index}"
        lines.extend(["", f"## {index}. {title}"])
        description = rule.get("description")
        if not description and rule.get("description_html"):
            description = BeautifulSoup(unescape(rule["description_html"]), "lxml").get_text("\n", strip=True)
        if description:
            lines.extend(["", str(description)])
    site_rules = data.get("site_rules") or []
    if site_rules:
        lines.extend(["", "## Reddit-wide rules", "", *(f"- {rule}" for rule in site_rules)])
    if not rules and not site_rules:
        lines.extend(["", "No public rules returned."])
    return "\n".join(lines)


def _render_wiki(payload: object, route: RedditRoute) -> str:
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data or {}
    title = route.label or "wiki"
    lines = [f"# r/{route.subreddit or '?'} wiki · {title}"]
    revision_wrapper = data.get("revision_by") or {}
    revision_by = revision_wrapper.get("data") or revision_wrapper
    if revision_by.get("name") or data.get("revision_date"):
        lines.extend(
            [
                "",
                " · ".join(
                    value
                    for value in (
                        f"revised by u/{revision_by.get('name')}" if revision_by.get("name") else "",
                        _created(data.get("revision_date")) if data.get("revision_date") else "",
                    )
                    if value
                ),
            ]
        )
    if data.get("revision_id"):
        lines.extend(["", f"**Revision:** {data['revision_id']}"])
    if data.get("reason"):
        lines.extend(["", f"**Edit reason:** {data['reason']}"])
    content = data.get("content_md")
    lines.extend(["", str(content or "No wiki content returned.")])
    return "\n".join(lines)


def _wiki_revision_metadata(payload: object, fallback_id: str) -> str:
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data or {}
    revision_wrapper = data.get("revision_by") or {}
    revision_by = revision_wrapper.get("data") or revision_wrapper
    details = [f"revision `{data.get('revision_id') or fallback_id}`"]
    if isinstance(revision_by, dict) and revision_by.get("name"):
        details.append(f"u/{revision_by['name']}")
    if data.get("revision_date"):
        details.append(_created(data["revision_date"]))
    if data.get("reason"):
        details.append(f"reason: {data['reason']}")
    return " · ".join(details)


def _diff_fence(text: str) -> str:
    """Return a Markdown fence longer than any authored backtick run."""

    longest = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)),
        default=0,
    )
    return "`" * max(3, longest + 1)


def _render_wiki_diff(
    payloads: list[object],
    route: RedditRoute,
    max_chars: int,
) -> str:
    query = parse_qs(urlparse(route.canonical_url).query, keep_blank_values=True)
    left_id = _first_query(query, "v") or "first"
    right_id = _first_query(query, "v2") or "second"
    left = payloads[0] if payloads else {}
    right = payloads[1] if len(payloads) > 1 else left
    left_data = left.get("data") if isinstance(left, dict) else {}
    right_data = right.get("data") if isinstance(right, dict) else {}
    left_data = left_data or {}
    right_data = right_data or {}
    left_content = str(left_data.get("content_md") or "")
    right_content = str(right_data.get("content_md") or "")
    prefix = (
        f"# r/{route.subreddit or '?'} wiki diff · {route.label or 'wiki'}\n\n"
        f"**From:** {_wiki_revision_metadata(left, left_id)}\n\n"
        f"**To:** {_wiki_revision_metadata(right, right_id)}"
    )
    unavailable = []
    for side, revision in (("From", left), ("To", right)):
        if isinstance(revision, dict) and revision.get("_fetch_error"):
            unavailable.append(
                (
                    f"[{side} revision unavailable: {revision['_fetch_error']}]",
                    0,
                )
            )
    if unavailable:
        return _fit_sections(
            prefix,
            [],
            max_chars,
            omission_label="revision availability notices",
            required_sections=unavailable,
        )

    diff_lines = list(
        difflib.unified_diff(
            left_content.splitlines(),
            right_content.splitlines(),
            fromfile=f"revision {left_id}",
            tofile=f"revision {right_id}",
            lineterm="",
        )
    )
    if not diff_lines:
        return _fit_sections(
            prefix,
            [("No content differences returned.", 1)],
            max_chars,
            omission_label="diff hunks",
        )

    file_headers = diff_lines[:2]
    hunks: list[list[str]] = []
    current: list[str] = []
    for line in diff_lines[2:]:
        if line.startswith("@@") and current:
            hunks.append(current)
            current = []
        current.append(line)
    if current:
        hunks.append(current)
    sections: list[tuple[str, int]] = []
    for index, hunk in enumerate(hunks):
        body = "\n".join([*(file_headers if index == 0 else []), *hunk])
        fence = _diff_fence(body)
        sections.append((f"{fence}diff\n{body}\n{fence}", 1))
    return _fit_sections(
        prefix,
        sections,
        max_chars,
        omission_label="diff hunks",
    )


def _render_subreddit_directory_item(
    data: dict,
    index: int,
    directory_label: str | None = None,
) -> str:
    name = data.get("display_name_prefixed") or (
        f"r/{data.get('display_name')}" if data.get("display_name") else "r/?"
    )
    title = data.get("title") or ""
    subscribers = data.get("subscribers")
    lines = [f"{index}. **{name}**" + (f" — {title}" if title else "")]
    if isinstance(subscribers, (int, float)):
        lines.append(f"   {int(subscribers):,} subscribers")
    status = []
    if directory_label == "banned" or _flag(data, "is_banned"):
        status.append("Banned")
    subreddit_type = str(data.get("subreddit_type") or "").strip().lower()
    if subreddit_type:
        status.append(subreddit_type.replace("_", " ").title())
    if _flag(data, "quarantine"):
        status.append("Quarantined")
    if _flag(data, "over18"):
        status.append("NSFW")
    if status:
        lines.append(f"   {' · '.join(dict.fromkeys(status))}")
    if data.get("created_utc") or data.get("created"):
        lines.append(
            f"   Created: {_created(data.get('created_utc') or data.get('created'))}"
        )
    description = str(data.get("public_description") or "").strip()
    if description:
        lines.append(f"   {description}")
    path = data.get("url") or f"/r/{data.get('display_name') or ''}/"
    lines.append(f"   {_absolute_reddit_url(path)}")
    return "\n".join(lines)


def _render_subreddit_directory(payload: object, route: RedditRoute, max_chars: int) -> str:
    children = _listing_children(payload)
    sections = []
    for index, child in enumerate(children, 1):
        data = child.get("data") or {}
        if child.get("kind") == "t2":
            sections.append((_render_user_directory_item(data, index), 1))
        else:
            sections.append(
                (
                    _render_subreddit_directory_item(
                        data,
                        index,
                        route.label,
                    ),
                    1,
                )
            )
    pagination = _pagination_sections(payload, route.canonical_url)
    prefix = f"# Reddit communities · {route.label or 'directory'}\n\n{len(children)} items returned"
    if (
        isinstance(payload, dict)
        and payload.get("_fetchaller_reddit_provenance")
        == "wayback_gold_directory"
    ):
        timestamp = str(
            payload.get("_fetchaller_reddit_archive_timestamp") or ""
        )
        archived_date = (
            f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
            if re.fullmatch(r"\d{14}", timestamp)
            else "unknown date"
        )
        prefix += (
            "\n\nDirectory state source: exact archived Reddit snapshot "
            f"(Wayback, {archived_date}). No gold-only communities were "
            "listed."
        )
    return _fit_sections(
        prefix,
        sections,
        max_chars,
        omission_label="communities",
        required_sections=pagination,
    )


def _render_user_directory_item(data: dict, index: int) -> str:
    prefixed = str(data.get("display_name_prefixed") or "")
    if prefixed.lower().startswith("u/"):
        name = prefixed[2:]
    else:
        profile_path = str(data.get("url") or "")
        match = re.fullmatch(r"/user/([^/]+)/?", profile_path, re.I)
        name = unquote(match.group(1)) if match else data.get("name") or "[unknown]"
    karma = []
    if isinstance(data.get("link_karma"), (int, float)):
        karma.append(f"{int(data['link_karma']):,} post karma")
    if isinstance(data.get("comment_karma"), (int, float)):
        karma.append(f"{int(data['comment_karma']):,} comment karma")
    lines = [
        f"{index}. **u/{name}**"
        + (f" · {' · '.join(karma)}" if karma else ""),
        f"   https://www.reddit.com/user/{quote(str(name), safe='')}/",
        (
            "   Public activity: "
            f"https://www.reddit.com/user/{quote(str(name), safe='')}/overview/"
        ),
    ]
    if data.get("created_utc") or data.get("created"):
        lines.append(
            f"   Created: "
            f"{_created(data.get('created_utc') or data.get('created'))}"
        )
    title = str(data.get("title") or "").strip()
    description = str(data.get("public_description") or "").strip()
    icon = unescape(data.get("icon_img") or "")
    if title:
        lines.append(f"   {title}")
    if description and description != title:
        lines.append(f"   {description}")
    if icon:
        lines.append(f"   {icon}")
    return "\n".join(lines)


def _render_user_directory(payload: object, route: RedditRoute, max_chars: int) -> str:
    children = _listing_children(payload)
    sections = [
        (_render_user_directory_item(child.get("data") or {}, index), 1)
        for index, child in enumerate(children, 1)
    ]
    pagination = _pagination_sections(payload, route.canonical_url)
    return _fit_sections(
        f"# Reddit users · {route.label or 'popular'}\n\n{len(children)} users returned",
        sections,
        max_chars,
        omission_label="users",
        required_sections=pagination,
    )


def _render_trophies(payload: object, route: RedditRoute, max_chars: int) -> str:
    data = payload.get("data") if isinstance(payload, dict) else {}
    trophies = (data or {}).get("trophies") or []
    sections: list[tuple[str, int]] = []
    for index, child in enumerate(trophies, 1):
        trophy = child.get("data") if isinstance(child, dict) else {}
        trophy = trophy or {}
        lines = [f"{index}. **{trophy.get('name') or 'Unnamed trophy'}**"]
        description = str(trophy.get("description") or "").strip()
        if description:
            lines.append(f"   {description}")
        if trophy.get("granted_at"):
            lines.append(f"   Granted: {_created(trophy['granted_at'])}")
        icon = unescape(trophy.get("icon_70") or trophy.get("icon_40") or "")
        if icon:
            lines.append(f"   {icon}")
        target = trophy.get("url")
        if isinstance(target, str) and target:
            lines.append(f"   {_absolute_reddit_url(target)}")
        sections.append(("\n".join(lines), 1))
    return _fit_sections(
        f"# Trophy case for u/{route.username or '?'}\n\n{len(sections)} trophies returned",
        sections,
        max_chars,
        omission_label="trophies",
    )


def _multi_data(payload: object) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else {}
    return data if isinstance(data, dict) else {}


def _multi_header(payload: object, route: RedditRoute) -> str:
    data = _multi_data(payload)
    title = data.get("display_name") or data.get("name") or route.label or "Multireddit"
    owner = data.get("owner") or route.username
    prefix = f"# {title}"
    details = []
    if owner:
        details.append(f"owner u/{owner}")
    if data.get("visibility"):
        details.append(str(data["visibility"]))
    if isinstance(data.get("num_subscribers"), (int, float)):
        details.append(f"{int(data['num_subscribers']):,} subscribers")
    if data.get("created_utc") or data.get("created"):
        details.append(f"created {_created(data.get('created_utc') or data.get('created'))}")
    if details:
        prefix += f"\n\n{' · '.join(details)}"
    description = str(data.get("description_md") or "").strip()
    if description:
        prefix += f"\n\n{description}"
    if route.kind == "multi_profile":
        query = parse_qs(
            urlparse(route.canonical_url).query,
            keep_blank_values=True,
        )
        scope = route.label or "multireddit feed"
        is_search = scope.endswith("search")
        requested_time = _first_query(query, "t")
        if requested_time in _TIME_FILTERS:
            scope += f" · time {requested_time}"
        if is_search:
            search_query = _first_query(query, "q") or ""
            search_sort = _first_query(query, "sort")
            scope += f' · query "{search_query}"'
            if search_sort in _SEARCH_SORTS:
                scope += f" · sort {search_sort}"
        prefix += f"\n\n**Feed scope:** {scope}"
    return prefix


def _multi_community_sections(payload: object) -> list[tuple[str, int]]:
    data = _multi_data(payload)
    # Filter before enumerating: a skipped entry otherwise burns its index
    # and the visible list jumps (1, 3, 4...) while the count overstates it.
    named_subreddits = [
        subreddit
        for subreddit in (data.get("subreddits") or [])
        if isinstance(subreddit, dict)
        and (subreddit.get("name") or subreddit.get("display_name"))
    ]
    sections: list[tuple[str, int]] = []
    for index, subreddit in enumerate(named_subreddits, 1):
        name = subreddit.get("name") or subreddit.get("display_name")
        sections.append(
            (
                f"{index}. **r/{name}**\n"
                f"   https://www.reddit.com/r/{quote(str(name), safe='')}/",
                1,
            )
        )
    return sections


def _render_multi_about(
    payload: object,
    route: RedditRoute,
    max_chars: int,
) -> str:
    prefix = _multi_header(payload, route)
    sections = _multi_community_sections(payload)
    if sections:
        prefix += f"\n\n## Communities\n\n{len(sections)} communities returned"
    return _fit_sections(prefix, sections, max_chars, omission_label="communities")


def _render_multi_profile(
    payloads: list[object],
    route: RedditRoute,
    max_chars: int,
) -> str:
    metadata = payloads[0] if payloads else {}
    prefix = _multi_header(metadata, route)
    if isinstance(metadata, dict) and metadata.get("_fetch_error"):
        prefix += f"\n\n[Multireddit details unavailable: {metadata['_fetch_error']}]"
    listing = payloads[1] if len(payloads) > 1 else {}
    if (
        isinstance(listing, dict)
        and listing.get("_fetchaller_reddit_provenance") == "wayback"
    ):
        timestamp = str(
            listing.get("_fetchaller_reddit_archive_timestamp") or ""
        )
        archived_date = (
            f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
            if re.fullmatch(r"\d{14}", timestamp)
            else "unknown date"
        )
        prefix += (
            "\n\nGilded ordering source: archived Reddit snapshot "
            f"(Wayback, {archived_date}). Item details: current Reddit API."
        )
    # Only ``t3``/``t1`` children are rendered below, so restrict the list
    # before counting and numbering: any other kind was silently dropped from
    # the output while still inflating the count.
    feed_children = [
        child
        for child in _listing_children(listing)
        if child.get("kind") in {"t3", "t1"}
    ]
    sections: list[tuple[str, int]] = [
        (f"## Feed\n\n{len(feed_children)} items returned", 0)
    ]
    pagination: list[tuple[str, int]] = []
    if isinstance(listing, dict) and listing.get("_fetch_error"):
        sections.append((f"[Unavailable: {listing['_fetch_error']}]", 0))
    else:
        for index, child in enumerate(feed_children, 1):
            data = child.get("data") or {}
            if child.get("kind") == "t3":
                sections.append(
                    (format_reddit_post(data, index, include_subreddit=True), 1)
                )
            elif child.get("kind") == "t1":
                sections.append((_render_activity_comment(data, index), 1))
        pagination = _pagination_sections(listing, route.canonical_url)
    communities = _multi_community_sections(metadata)
    if communities:
        sections.append(("## Communities", 0))
        sections.extend(communities)
    return _fit_sections(
        prefix,
        sections,
        max_chars,
        omission_label="multireddit items",
        required_sections=pagination,
    )


def _render_moderators(payload: object, route: RedditRoute, max_chars: int) -> str:
    children = normalize_moderator_roster_children(payload)
    if children is None:
        return (
            f"# Moderators of r/{route.subreddit or '?'}\n\n"
            "[Moderator roster returned an invalid response.]"
        )
    sections: list[tuple[str, int]] = []
    for child in children:
        name = child.get("name")
        permissions = child.get("mod_permissions")
        assert isinstance(name, str)
        assert isinstance(permissions, list)
        permission_text = ", ".join(permissions) or "no listed permissions"
        date_text = (
            f" · added {_created(child['date'])}"
            if child.get("date")
            else ""
        )
        index = len(sections) + 1
        sections.append(
            (
                f"{index}. **u/{name}** · {permission_text}{date_text}\n"
                f"   https://www.reddit.com/user/{quote(name, safe='')}/",
                1,
            )
        )
    prefix = (
        f"# Moderators of r/{route.subreddit or '?'}\n\n"
        f"{len(sections)} moderators returned"
    )
    return _fit_sections(prefix, sections, max_chars, omission_label="moderators")


def _render_duplicates(payload: object, route: RedditRoute, max_chars: int) -> str:
    if not isinstance(payload, list) or not payload:
        return "# Reddit duplicates\n\nNo duplicate data returned."
    posts = _listing_children(payload[0])
    post = _render_post(posts[0].get("data") or {}) if posts else "# Reddit post\n\nPost not found."
    duplicates = _listing_children(payload[1]) if len(payload) > 1 else []
    # "Other discussions" is a set of distinct posts, but Reddit's duplicates
    # listing repeats one occasionally -- observed across separate runs as the
    # same crosspost arriving twice with different cached upvote ratios. Render
    # each post once, and count what was rendered: the count previously came
    # from the unfiltered children, so a repeated or non-``t3`` entry inflated
    # it past the cards actually shown.
    seen_identities: set[str] = set()
    unique_duplicates = []
    for child in duplicates:
        if child.get("kind") != "t3":
            continue
        identity = str((child.get("data") or {}).get("name") or "")
        if identity:
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
        unique_duplicates.append(child)
    sections = [
        (
            format_reddit_post(child.get("data") or {}, index, include_subreddit=True),
            1,
        )
        for index, child in enumerate(unique_duplicates, 1)
    ]
    pagination = (
        _pagination_sections(payload[1], route.canonical_url)
        if len(payload) > 1
        else []
    )
    return _fit_sections(
        f"{post}\n\n## Other discussions\n\n{len(unique_duplicates)} items returned",
        sections,
        max_chars,
        omission_label="duplicate discussions",
        required_sections=pagination,
    )


def _render_related(payload: object, route: RedditRoute, max_chars: int) -> str:
    if not isinstance(payload, list) or not payload:
        return "# Related Reddit posts\n\nNo related data returned."
    source_children = _listing_children(payload[0])
    source = (
        _render_post(source_children[0].get("data") or {})
        if source_children
        else "# Reddit post\n\nPost not found."
    )
    related_payload = payload[1] if len(payload) > 1 else {}
    unavailable: list[tuple[str, int]] = []
    if isinstance(related_payload, dict) and related_payload.get("_fetch_error"):
        unavailable.append(
            (
                f"[Related posts unavailable: "
                f"{related_payload['_fetch_error']}]",
                0,
            )
        )
        related_payload = {}
    # Count and number the posts actually rendered. Enumerating the unfiltered
    # children counted entries no reader ever sees and left gaps in the
    # numbering whenever a non-``t3`` child appeared.
    related = [
        child
        for child in _listing_children(related_payload)
        if child.get("kind") == "t3"
    ]
    sections = [
        (
            format_reddit_post(child.get("data") or {}, index, include_subreddit=True),
            1,
        )
        for index, child in enumerate(related, 1)
    ]
    pagination = _pagination_sections(related_payload, route.canonical_url)
    enrichment_notices: list[tuple[str, int]] = []
    notices = (
        related_payload.get("_enrichment_notices")
        if isinstance(related_payload, dict)
        else None
    )
    if isinstance(notices, list):
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            count = notice.get("count")
            detail = notice.get("detail")
            if (
                isinstance(count, int)
                and count > 0
                and isinstance(detail, str)
                and detail
            ):
                enrichment_notices.append(
                    (
                        f"[{count:,} related post "
                        f"{'detail' if count == 1 else 'details'} "
                        f"unavailable: {detail}]",
                        0,
                    )
                )
    return _fit_sections(
        f"{source}\n\n## Related posts\n\n{len(related)} items returned",
        sections,
        max_chars,
        omission_label="related posts",
        required_sections=[
            *pagination,
            *unavailable,
            *enrichment_notices,
        ],
    )


def _live_root_url(route: RedditRoute) -> str:
    """Return the canonical root URL for any validated live-thread route."""

    parsed = urlparse(route.canonical_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) >= 2
        and parts[0].casefold() == "live"
        and _THING_ID_RE.fullmatch(parts[1])
    ):
        return f"https://www.reddit.com/live/{quote(parts[1])}/"
    return route.canonical_url


def _live_about_lines(payload: object, route: RedditRoute) -> list[str]:
    live_root = _live_root_url(route)
    if isinstance(payload, dict) and payload.get("_fetch_error"):
        return [
            "# Reddit live thread",
            "",
            f"[Live thread details unavailable: {payload['_fetch_error']}]",
            "",
            f"**Live thread:** {live_root}",
            f"**Requested route:** {route.canonical_url}",
        ]
    data = payload.get("data") if isinstance(payload, dict) else {}
    data = data or {}
    title = data.get("title") or "Reddit live thread"
    lines = [f"# {title}"]
    if data.get("state"):
        lines.extend(["", f"**State:** {data['state']}"])
    if isinstance(data.get("viewer_count"), (int, float)):
        lines.append(f"**Viewers:** {int(data['viewer_count']):,}")
    description = str(data.get("description") or "").strip()
    if description:
        lines.extend(["", description])
    resources = str(data.get("resources") or "").strip()
    if resources:
        lines.extend(["", "## Resources", "", resources])
    lines.extend(["", f"**Live thread:** {live_root}"])
    if route.canonical_url != live_root:
        lines.append(f"**Requested route:** {route.canonical_url}")
    return lines


def _render_live_about(payload: object, route: RedditRoute) -> str:
    return "\n".join(_live_about_lines(payload, route))


def _render_live(payloads: list[object], route: RedditRoute, max_chars: int) -> str:
    payload = payloads[0] if payloads else {}
    lines = _live_about_lines(payload, route)
    lines.extend(["", "## Updates"])
    updates_payload = payloads[1] if len(payloads) > 1 else {}
    if isinstance(updates_payload, dict) and updates_payload.get("_fetch_error"):
        lines.extend(["", f"[Unavailable: {updates_payload['_fetch_error']}]"])
        return "\n".join(lines)
    # Filter before enumerating so the visible numbering stays contiguous; a
    # skipped non-``LiveUpdate`` child otherwise burns its index and the list
    # jumps from 1 to 3.
    children = [
        child
        for child in _listing_children(updates_payload)
        if child.get("kind") == "LiveUpdate"
    ]
    sections: list[tuple[str, int]] = [
        (_render_live_update(child.get("data") or {}, index), 1)
        for index, child in enumerate(children, 1)
    ]
    pagination = _pagination_sections(
        updates_payload,
        route.canonical_url,
        cursor_pattern=_LIVE_CURSOR_RE,
    )
    return _fit_sections(
        "\n".join(lines),
        sections,
        max_chars,
        omission_label="live updates",
        required_sections=pagination,
    )


def _render_live_contributors(
    payload: object,
    route: RedditRoute,
    max_chars: int,
) -> str:
    data = payload.get("data") if isinstance(payload, dict) else {}
    children = (data or {}).get("children") or []
    sections = []
    for index, child in enumerate(children, 1):
        nested = child.get("data") if isinstance(child, dict) else None
        contributor = nested if isinstance(nested, dict) else child
        contributor = contributor if isinstance(contributor, dict) else {}
        name = contributor.get("name") or "[unknown]"
        sections.append(
            (
                f"{index}. **u/{name}**\n"
                f"   https://www.reddit.com/user/{quote(str(name), safe='')}/",
                1,
            )
        )
    return _fit_sections(
        (
            "# Reddit live contributors"
            f"\n\n**Live thread:** {_live_root_url(route)}"
            f"\n**Requested route:** {route.canonical_url}"
            f"\n\n{len(sections)} contributors returned"
        ),
        sections,
        max_chars,
        omission_label="contributors",
    )


def _render_live_update(update: dict, index: int) -> str:
    author = update.get("author") or "[deleted]"
    heading = f"{index}. **u/{author} · {_created(update.get('created_utc'))}**"
    if _flag(update, "stricken"):
        heading += " · [stricken]"
    body = str(update.get("body") or "").strip()
    lines = [heading]
    if body:
        lines.extend(["", body])

    # Live updates can carry link/media embeds outside the Markdown body. Keep
    # their public URLs without dumping opaque embed metadata.
    urls: list[str] = []
    for embed in [*(update.get("embeds") or []), *(update.get("mobile_embeds") or [])]:
        if not isinstance(embed, dict):
            continue
        for key in ("url", "href", "source_url", "thumbnail_url"):
            value = embed.get(key)
            if isinstance(value, str):
                urls.append(value)
    unique_urls = [url for url in _unique_urls(urls) if url not in body]
    if unique_urls:
        lines.extend(["", *(f"Media: {url}" for url in unique_urls)])
    update_id = update.get("id")
    if update_id:
        lines.extend(["", f"Update ID: {update_id}"])
    return "\n".join(lines)


def _render_live_update_listing(
    payload: object,
    route: RedditRoute,
    max_chars: int,
) -> str:
    # Filter before enumerating: counting the unfiltered children overstated
    # the updates shown and left gaps in their numbering.
    children = [
        child
        for child in _listing_children(payload)
        if child.get("kind") == "LiveUpdate"
    ]
    sections = [
        (_render_live_update(child.get("data") or {}, index), 1)
        for index, child in enumerate(children, 1)
    ]
    pagination = _pagination_sections(
        payload,
        route.canonical_url,
        cursor_pattern=_LIVE_CURSOR_RE,
    )
    return _fit_sections(
        (
            "# Reddit live update"
            f"\n\n**Live thread:** {_live_root_url(route)}"
            f"\n**Requested route:** {route.canonical_url}"
            f"\n\n{len(children)} updates returned"
        ),
        sections,
        max_chars,
        omission_label="live updates",
        required_sections=pagination,
    )


def _render_morechildren(
    payload: object,
    route: RedditRoute,
    max_chars: int,
) -> str:
    """Render flattened Things from Reddit's jQuery-command envelope."""

    things: list[dict] = []
    seen: set[str] = set()

    def _visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("kind") in {"t1", "more"} and isinstance(
                value.get("data"),
                dict,
            ):
                data = value["data"]
                thing_id = str(data.get("id") or "")
                key = f"{value.get('kind')}:{thing_id or id(value)}"
                if key not in seen:
                    seen.add(key)
                    things.append(value)
                return
            for child in value.values():
                _visit(child)
        elif isinstance(value, list):
            for child in value:
                _visit(child)

    _visit(payload)
    comments = [
        thing["data"]
        for thing in things
        if thing.get("kind") == "t1"
    ]
    by_id = {
        str(comment.get("id")): comment
        for comment in comments
        if comment.get("id")
    }
    depth_cache: dict[str, int] = {}

    def _comment_depth(comment: dict, visiting: set[str] | None = None) -> int:
        comment_id = str(comment.get("id") or "")
        if comment_id in depth_cache:
            return depth_cache[comment_id]
        visiting = set(visiting or ())
        if not comment_id or comment_id in visiting:
            return 0
        visiting.add(comment_id)
        parent_id = str(comment.get("parent_id") or "")
        parent = by_id.get(parent_id.removeprefix("t1_"))
        depth = (
            min(10, _comment_depth(parent, visiting) + 1)
            if parent is not None
            else 0
        )
        depth_cache[comment_id] = depth
        return depth

    query = parse_qs(
        urlparse(route.canonical_url).query,
        keep_blank_values=True,
    )
    link_id = _first_query(query, "link_id") or ""
    post_id = link_id.removeprefix("t3_")

    indexed_comments = list(enumerate(comments))
    indexed_comments.sort(
        key=lambda item: (_comment_depth(item[1]), item[0])
    )
    sections = [
        (
            _render_comment(
                comment,
                _comment_depth(comment),
                post_id=post_id,
            ),
            1,
        )
        for _, comment in indexed_comments
    ]
    requested_sort = _first_query(query, "sort")
    sort = requested_sort if requested_sort in _COMMENT_SORTS else "confidence"
    requested_depth = _first_query(query, "depth")
    base_depth = (
        min(int(requested_depth), 10)
        if requested_depth and requested_depth.isdigit()
        else 0
    )
    continuations: list[tuple[str, int]] = []
    for thing in things:
        if thing.get("kind") != "more":
            continue
        for text, count, required in _more_comment_sections(
            thing["data"],
            post_id=post_id,
            sort=sort,
            depth=base_depth,
        ):
            if required:
                continuations.append((text, count))
            else:
                sections.append((text, count))
    prefix = f"# More Reddit comments\n\n{len(comments)} comments returned"
    return _fit_sections(
        prefix,
        sections,
        max_chars,
        omission_label="comments",
        required_sections=continuations,
    )


def _render_wiki_revisions(payload: object, route: RedditRoute, max_chars: int) -> str:
    children = _listing_children(payload)
    sections: list[tuple[str, int]] = []
    for index, child in enumerate(children, 1):
        revision = child.get("data") or child
        author_wrapper = revision.get("author") or {}
        author = (
            (author_wrapper.get("data") or author_wrapper).get("name")
            if isinstance(author_wrapper, dict)
            else None
        )
        page = revision.get("page") or route.label or "wiki"
        revision_id = revision.get("id") or revision.get("revision_id")
        line = (
            f"{index}. **{page}** · "
            f"{_created(revision.get('timestamp') or revision.get('created_utc'))}"
        )
        if author:
            line += f" · u/{author}"
        if revision_id:
            line += f"\n   revision: {revision_id}"
        reason = revision.get("reason")
        if reason:
            line += f"\n   {reason}"
        sections.append((line, 1))
    pagination = _pagination_sections(
        payload,
        route.canonical_url,
        cursor_pattern=_WIKI_CURSOR_RE,
    )
    return _fit_sections(
        f"# Wiki revisions for r/{route.subreddit or '?'} · {route.label or 'all pages'}"
        f"\n\n{len(children)} revisions returned",
        sections,
        max_chars,
        omission_label="wiki revisions",
        required_sections=pagination,
    )


def _escape_markdown_link_label(value: str) -> str:
    """Keep an untrusted Reddit name inside one Markdown link label."""

    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _render_wiki_pages(payload: object, route: RedditRoute, max_chars: int) -> str:
    data = payload.get("data") if isinstance(payload, dict) else payload
    # Only str pages render, so filter before enumerating/counting.
    pages = [page for page in (data if isinstance(data, list) else []) if isinstance(page, str)]
    sections = [
        (
            f"{index}. [{_escape_markdown_link_label(page)}]"
            f"(https://www.reddit.com/r/{quote(route.subreddit or '')}/wiki/"
            f"{quote(str(page), safe='._-/')}/)",
            1,
        )
        for index, page in enumerate(pages, 1)
    ]
    sources = {
        "graphql": "\n\nSource: anonymous Reddit wiki page tree",
    }
    provenance = (
        sources.get(str(payload.get("_fetchaller_reddit_provenance")), "")
        if isinstance(payload, dict)
        else ""
    )
    return _fit_sections(
        f"# Wiki pages for r/{route.subreddit or '?'}\n\n{len(sections)} pages returned{provenance}",
        sections,
        max_chars,
        omission_label="wiki pages",
    )


def _render_collection(payloads: list[object], route: RedditRoute, max_chars: int) -> str:
    collection = payloads[0] if payloads and isinstance(payloads[0], dict) else {}
    title = collection.get("title") or "Reddit collection"
    description = str(collection.get("description") or "").strip()
    prefix = f"# {title}"
    if route.subreddit:
        prefix += f"\n\nr/{route.subreddit}"
    if collection.get("_fetchaller_reddit_provenance") == "wayback":
        timestamp = str(
            collection.get("_fetchaller_reddit_archive_timestamp") or ""
        )
        archived_date = (
            f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
            if re.fullmatch(r"\d{14}", timestamp)
            else "unknown date"
        )
        prefix += (
            "\n\nMetadata source: archived New Reddit snapshot "
            f"(Wayback, {archived_date}). "
            "Post details: current Reddit API."
        )
    if description:
        prefix += f"\n\n{description}"
    requested_ids = collection.get("link_ids") or []
    requested_ids = requested_ids if isinstance(requested_ids, list) else []
    valid_ids = [
        value
        for value in requested_ids
        if isinstance(value, str) and re.fullmatch(r"t3_[A-Za-z0-9]+", value)
    ]
    children_by_fullname: dict[str, dict] = {}
    unavailable: list[tuple[str, int]] = []
    for listing in payloads[1:]:
        if isinstance(listing, dict) and listing.get("_fetch_error"):
            count = listing.get("_omitted_count")
            count = int(count) if isinstance(count, (int, float)) and count > 0 else 0
            unavailable.append(
                (
                    f"[{count:,} collection posts unavailable: "
                    f"{listing['_fetch_error']}]"
                    if count
                    else f"[Collection posts unavailable: {listing['_fetch_error']}]",
                    0,
                )
            )
            continue
        for child in _listing_children(listing):
            data = child.get("data") or {}
            post_id = data.get("id")
            if child.get("kind") == "t3" and isinstance(post_id, str):
                children_by_fullname[f"t3_{post_id}"] = child
    children = [
        children_by_fullname[fullname]
        for fullname in valid_ids
        if fullname in children_by_fullname
    ]
    unexplained_missing = len(valid_ids) - len(children)
    declared_unavailable = sum(
        int(payload.get("_omitted_count") or 0)
        for payload in payloads[1:]
        if isinstance(payload, dict) and payload.get("_fetch_error")
    )
    if unexplained_missing > declared_unavailable:
        unavailable.append(
            (
                f"[{unexplained_missing - declared_unavailable:,} collection posts "
                "were not returned by Reddit.]",
                0,
            )
        )
    sections = [
        (
            format_reddit_post(
                child.get("data") or {},
                index,
                include_subreddit=route.subreddit is None,
            ),
            1,
        )
        for index, child in enumerate(children, 1)
        if child.get("kind") == "t3"
    ]
    return _fit_sections(
        f"{prefix}\n\n## Posts\n\n{len(sections)} items returned",
        sections,
        max_chars,
        omission_label="collection posts",
        required_sections=unavailable,
    )


def _fit_static_markdown(rendered: str, max_chars: int, label: str) -> str:
    """Keep Markdown paragraph boundaries and make any truncation explicit."""

    paragraphs = rendered.split("\n\n")
    prefix = paragraphs[0] if paragraphs else ""
    sections = [(paragraph, 1) for paragraph in paragraphs[1:] if paragraph]
    return _fit_sections(prefix, sections, max_chars, omission_label=label)


def render_reddit_route(
    route: RedditRoute,
    payloads: list[object],
    max_tokens: int,
    chars_per_token: int = 4,
) -> str:
    """Render mapped Reddit JSON without exposing raw API metadata."""

    max_chars = max(1, max_tokens * chars_per_token)
    payload = payloads[0] if payloads else {}
    if route.kind == "thread":
        rendered = _render_thread(payload, route, max_chars)
    elif route.kind in {
        "listing",
        "domain_listing",
        "comment_listing",
        "search",
        "user_listing",
    }:
        rendered = _render_listing(payload, route, max_chars)
    elif route.kind == "user_profile":
        if isinstance(payload, dict) and payload.get("_fetch_error"):
            about = (
                f"# u/{route.username or '[unknown]'}\n\n"
                f"[Profile details unavailable: {payload['_fetch_error']}]"
            )
        else:
            about = _render_user_about(payload)
        overview_payload = payloads[1] if len(payloads) > 1 else {}
        profile_sections: list[tuple[str, int]] = []
        if isinstance(overview_payload, dict) and overview_payload.get("_fetch_error"):
            profile_sections.append(
                (
                    f"## Recent activity\n\n"
                    f"[Unavailable: {overview_payload['_fetch_error']}]",
                    0,
                )
            )
            pagination = []
        else:
            children = [
                child
                for child in _listing_children(overview_payload)
                if child.get("kind") in {"t3", "t1"}
            ]
            profile_sections.append(("## Recent activity", 0))
            for index, child in enumerate(children, 1):
                data = child.get("data") or {}
                if child.get("kind") == "t3":
                    profile_sections.append(
                        (format_reddit_post(data, index, include_subreddit=True), 1)
                    )
                elif child.get("kind") == "t1":
                    profile_sections.append((_render_activity_comment(data, index), 1))
            pagination = _pagination_sections(
                overview_payload,
                route.canonical_url,
            )

        extra_specs = (
            ("Trophies", 2),
            ("Public multireddits", 3),
            ("Moderated communities", 4),
        )
        for title, payload_index in extra_specs:
            extra = payloads[payload_index] if len(payloads) > payload_index else {}
            profile_sections.append((f"## {title}", 0))
            if isinstance(extra, dict) and extra.get("_fetch_error"):
                profile_sections.append((f"[Unavailable: {extra['_fetch_error']}]", 0))
                continue
            if title == "Trophies":
                trophy_data = extra.get("data") if isinstance(extra, dict) else {}
                trophies = (trophy_data or {}).get("trophies") or []
                for index, child in enumerate(trophies, 1):
                    trophy = child.get("data") if isinstance(child, dict) else {}
                    trophy = trophy or {}
                    profile_sections.append(
                        (
                            f"{index}. **{trophy.get('name') or 'Unnamed trophy'}**"
                            + (
                                f"\n   {trophy['description']}"
                                if trophy.get("description")
                                else ""
                            ),
                            1,
                        )
                    )
            elif title == "Public multireddits":
                multis = extra if isinstance(extra, list) else []
                for index, wrapper in enumerate(multis, 1):
                    multi = (
                        wrapper.get("data")
                        if isinstance(wrapper, dict)
                        else {}
                    ) or {}
                    path = str(multi.get("path") or "")
                    name = multi.get("display_name") or multi.get("name") or path or "Multireddit"
                    profile_sections.append(
                        (
                            f"{index}. **{name}**"
                            + (
                                f"\n   {_absolute_reddit_url(path)}"
                                if path
                                else ""
                            ),
                            1,
                        )
                    )
            else:
                moderated_data = (
                    extra.get("data")
                    if isinstance(extra, dict)
                    and extra.get("kind") == "ModeratedList"
                    and isinstance(extra.get("data"), list)
                    else [
                        child.get("data") or {}
                        for child in _listing_children(extra)
                    ]
                )
                # Filter before numbering: a skipped entry burned its index
                # and the visible list jumped (1, 3, ...).
                moderated_data = [
                    community
                    for community in moderated_data
                    if isinstance(community, dict)
                ]
                for index, community in enumerate(moderated_data, 1):
                    name = str(community.get("display_name") or "")
                    if not name:
                        prefixed = str(
                            community.get("display_name_prefixed") or ""
                        )
                        name = (
                            prefixed[2:]
                            if prefixed.casefold().startswith("r/")
                            else prefixed
                        )
                    name = name or "?"
                    profile_sections.append(
                        (
                            f"{index}. **r/{name}**\n"
                            f"   https://www.reddit.com/r/{quote(str(name), safe='')}/",
                            1,
                        )
                    )
        rendered = _fit_sections(
            about,
            profile_sections,
            max_chars,
            omission_label="profile items",
            required_sections=pagination,
        )
    elif route.kind == "user_about":
        rendered = _render_user_about(payload)
    elif route.kind == "subreddit_about":
        rendered = _render_subreddit_about(payload)
    elif route.kind == "rules":
        rendered = _render_rules(payload, route)
    elif route.kind == "wiki":
        rendered = _render_wiki(payload, route)
    elif route.kind == "wiki_diff":
        rendered = _render_wiki_diff(payloads, route, max_chars)
    elif route.kind == "subreddit_directory":
        rendered = _render_subreddit_directory(payload, route, max_chars)
    elif route.kind == "user_directory":
        rendered = _render_user_directory(payload, route, max_chars)
    elif route.kind == "trophies":
        rendered = _render_trophies(payload, route, max_chars)
    elif route.kind == "multi_about":
        rendered = _render_multi_about(payload, route, max_chars)
    elif route.kind == "multi_profile":
        rendered = _render_multi_profile(payloads, route, max_chars)
    elif route.kind == "moderators":
        rendered = _render_moderators(payload, route, max_chars)
    elif route.kind == "duplicates":
        rendered = _render_duplicates(payload, route, max_chars)
    elif route.kind == "related":
        rendered = _render_related(payloads, route, max_chars)
    elif route.kind == "wiki_revisions":
        rendered = _render_wiki_revisions(payload, route, max_chars)
    elif route.kind == "wiki_discussions":
        listing_route = RedditRoute(
            route.canonical_url,
            "listing",
            subreddit=route.subreddit,
            label=route.kind.replace("_", " "),
        )
        rendered = _render_listing(payload, listing_route, max_chars)
    elif route.kind == "wiki_pages":
        rendered = _render_wiki_pages(payload, route, max_chars)
    elif route.kind == "live":
        rendered = _render_live(payloads, route, max_chars)
    elif route.kind == "live_about":
        rendered = _render_live_about(payload, route)
    elif route.kind == "live_contributors":
        rendered = _render_live_contributors(payload, route, max_chars)
    elif route.kind == "live_update":
        rendered = _render_live_update_listing(payload, route, max_chars)
    elif route.kind == "morechildren":
        rendered = _render_morechildren(payload, route, max_chars)
    elif route.kind == "collection":
        rendered = _render_collection(payloads, route, max_chars)
    else:
        rendered = "# Reddit\n\nNo compact renderer is available for this URL."
    if route.kind in {
        "user_about",
        "subreddit_about",
        "rules",
        "wiki",
        "live_about",
    }:
        rendered = _fit_static_markdown(rendered, max_chars, "sections")
    canonical = _canonicalize_embedded_reddit_links(rendered)
    if len(canonical) <= max_chars:
        return canonical
    # Canonicalizing authored bare/protocol-relative old.reddit.com links can
    # expand them after the route-specific formatter has already budgeted the
    # document. Re-fit at paragraph boundaries so the final output never cuts a
    # Markdown link or its explicit omission marker.
    return _fit_static_markdown(canonical, max_chars, "sections")
