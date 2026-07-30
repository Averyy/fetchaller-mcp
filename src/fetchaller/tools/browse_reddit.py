"""Browse Reddit tool - browse subreddit listings."""

import asyncio
import re
import time as monotonic_time
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

import wafer

from ..config import get_wafer_cache_dir
from ..content.reddit import format_reddit_post
from ..queue.reddit_queue import RedditRequestQueue, parse_retry_after
from ..ratelimit import reddit_limiter

# Pre-compiled regex for subreddit name validation
_SUBREDDIT_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_]{0,20}\Z")
_PAGINATION_CURSOR_PATTERN = re.compile(r"t[1-6]_[A-Za-z0-9]{2,16}\Z")

# A 403 that wafer identified as Reddit's anonymous-session gate clears as soon
# as wafer re-runs its verification -- measured at 1.9s from a cold cookie cache
# -- so the pause only has to outlast that, not the configured block backoff.
_REDDIT_SESSION_GATE_BACKOFF = 5.0
# An opaque 403 wafer could not identify keeps the conservative delay: with no
# evidence it is transient, backing off hard is the safe assumption.
_OPAQUE_403_BACKOFF = 300.0

# Shared session for Reddit requests (reuses TLS identity and cookies)
_session: wafer.AsyncSession | None = None
_session_solver: object | None = None
_session_lock = asyncio.Lock()
_session_audit: dict[str, int] | None = None
_REDDIT_ANONYMOUS_COOKIE_NAMES = frozenset(
    {"csv", "edgebucket", "loid", "token_v2"}
)


def _instrument_reddit_session(session: wafer.AsyncSession) -> None:
    """Record non-secret cache hydration and inline verification counts."""

    global _session_audit
    # NOTE: reads two wafer private attributes. CLAUDE.md assigns cookie
    # management to wafer, and the public `cookie_scope_summary()` is the right
    # long-term home for this -- but it reflects cookies observed so far, and
    # this runs at construction time, before any request. Switching to it made
    # the audit report hydrated_anonymous=0/count=0 on a warm cache, i.e. a
    # silently broken release metric. Kept deliberately until wafer exposes
    # cache-hydration state; both reads are getattr-guarded and value-free.
    hydrated_identities: set[tuple[str, str]] = set()
    scopes = getattr(session, "_cookie_scopes", {})
    hydrated_identities.update(
        (identity[0], identity[1])
        for identity in scopes
        if (
            isinstance(identity, tuple)
            and len(identity) == 3
            and isinstance(identity[0], str)
            and isinstance(identity[1], str)
            and (
                identity[1] == "reddit.com"
                or identity[1].endswith(".reddit.com")
            )
            and identity[0] in _REDDIT_ANONYMOUS_COOKIE_NAMES
        )
    )
    jar = getattr(getattr(session, "_client", None), "cookie_jar", None)
    get_all = getattr(jar, "get_all", None)
    if callable(get_all):
        try:
            for cookie in get_all():
                name = getattr(cookie, "name", None)
                domain = str(getattr(cookie, "domain", "")).lstrip(".").lower()
                if (
                    isinstance(name, str)
                    and name in _REDDIT_ANONYMOUS_COOKIE_NAMES
                    and (
                        domain == "reddit.com"
                        or domain.endswith(".reddit.com")
                    )
                ):
                    hydrated_identities.add((name, domain))
        except Exception:
            # Telemetry fails closed below; cookie values are never inspected.
            pass
    hydrated_names = {identity[0] for identity in hydrated_identities}
    _session_audit = {
        "hydrated_cookie_count": len(hydrated_names),
        "hydrated_anonymous": int(
            "loid" in hydrated_names
            and bool({"csv", "token_v2"}.intersection(hydrated_names))
        ),
        "bootstrap_instrumented": 0,
        "bootstrap_network_attempts": 0,
    }

    original = getattr(session, "_reddit_bootstrap_on_client", None)
    if not callable(original):
        return
    _session_audit["bootstrap_instrumented"] = 1

    async def audited_bootstrap(*args, **kwargs):
        assert _session_audit is not None
        _session_audit["bootstrap_network_attempts"] += 1
        return await original(*args, **kwargs)

    session._reddit_bootstrap_on_client = audited_bootstrap


def reddit_session_audit() -> dict[str, int] | None:
    """Return a value-free snapshot for the server shutdown evidence log."""

    return dict(_session_audit) if _session_audit is not None else None


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    """Return the durable anonymous Reddit session.

    Browse/search may create it before the server's browser solver is
    available. The first later call that supplies a solver rebuilds the wafer
    session once so challenge escalation is not silently disabled; wafer's
    cache directory preserves Reddit cookies across that rebuild.
    """

    global _session, _session_solver
    needs_solver_upgrade = (
        _session is not None
        and browser_solver is not None
        and _session_solver is None
    )
    if _session is None or needs_solver_upgrade:
        async with _session_lock:
            needs_solver_upgrade = (
                _session is not None
                and browser_solver is not None
                and _session_solver is None
            )
            if _session is None or needs_solver_upgrade:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    # A cold anonymous bootstrap can establish cookies but
                    # still leave the transport identity that received the
                    # gate unusable. wafer's Reddit contract preserves one
                    # bounded transport rotation for exactly that recovery;
                    # disabling rotations here prevented it from ever running.
                    max_rotations=1,
                    cache_dir=get_wafer_cache_dir(),
                    follow_redirects=False,
                    max_response_size=50 * 1024 * 1024,
                )
                _session_solver = browser_solver
                _instrument_reddit_session(_session)
                _session.add_cookie(
                    "over18=1; Domain=.reddit.com; Path=/; Secure; SameSite=Lax",
                    "https://www.reddit.com/",
                )
    return _session


async def close_session() -> None:
    global _session, _session_solver, _session_audit
    _session = None
    _session_solver = None
    _session_audit = None

_JSON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    # The durable anonymous wafer session intentionally shares its browser
    # identity and cookies.  Never let its automatic Referer state carry one
    # caller's path/query into another caller's request.
    "Referer": "https://www.reddit.com/",
}
_JSON_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_JSON_REDIRECTS = 5


def _find_reddit_reason(value: object) -> str | None:
    """Find Reddit's structured access reason without depending on one shape."""

    if isinstance(value, dict):
        reason = value.get("reason")
        if isinstance(reason, str) and reason:
            return reason.lower()
        for child in value.values():
            found = _find_reddit_reason(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_reddit_reason(child)
            if found:
                return found
    return None


def format_reddit_http_error(status_code: int, payload: object | None) -> tuple[str, bool]:
    """Map Reddit's public access states to concise messages.

    Returns ``(message, should_backoff)``. Private/quarantined/banned results
    are content states, not transport blocks, so they must not poison the
    shared request queue with a five-minute 403 backoff.
    """

    reason = _find_reddit_reason(payload)
    if reason == "quarantined":
        return (
            "This community is quarantined and requires an email-verified "
            "Reddit account to opt in; anonymous access is unavailable.",
            False,
        )
    if reason == "private":
        return ("This is a private Reddit community; anonymous access is unavailable.", False)
    if reason == "gated":
        return ("This Reddit content requires account access; anonymous access is unavailable.", False)
    if reason == "banned":
        return ("This Reddit community has been banned.", False)
    if status_code == 404:
        return ("Reddit content not found.", False)
    if status_code == 403:
        return ("Access forbidden by Reddit (HTTP 403).", True)
    return (f"Reddit returned HTTP {status_code}.", False)


def _is_reddit_content_state(status_code: int, payload: object | None) -> bool:
    """Return whether an HTTP response represents Reddit content, not transport failure."""

    reason = _find_reddit_reason(payload)
    return status_code == 404 or reason in {
        "banned",
        "gated",
        "private",
        "quarantined",
    }


def _is_unstructured_error(payload: object | None) -> bool:
    """Return whether Reddit supplied no machine-readable error details."""
    return payload is None or payload == {} or payload == []


def _validated_reddit_json_url(url: str):
    """Accept only fixed-origin Reddit read endpoints used by this module."""
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or not url.isascii()
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or character == "\\"
            for character in url
        )
    ):
        return None
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    collection_query = parse_qsl(parsed.query, keep_blank_values=True)
    is_collection_read = (
        parsed.path == "/api/v1/collections/collection"
        and len(collection_query) == 3
        and dict(collection_query).get("include_links") == "true"
        and dict(collection_query).get("raw_json") == "1"
        and re.fullmatch(
            r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
            r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}",
            dict(collection_query).get("collection_id", ""),
        )
        is not None
    )
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").rstrip(".").lower() != "www.reddit.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.fragment
        or (not parsed.path.endswith(".json") and not is_collection_read)
    ):
        return None
    return parsed


def _is_missing_subreddit_redirect(source_url: str, target_url: str) -> bool:
    """Recognize Reddit's exact nonexistent-community redirect contract."""
    source = _validated_reddit_json_url(source_url)
    target = _validated_reddit_json_url(target_url)
    if source is None or target is None:
        return False
    source_match = re.match(
        r"/r/(?P<subreddit>[a-zA-Z0-9][a-zA-Z0-9_]{0,20})/",
        source.path,
    )
    if source_match is None or target.path != "/subreddits/search.json":
        return False
    query = parse_qs(target.query, keep_blank_values=True)
    return (
        set(query) == {"q"}
        and len(query["q"]) == 1
        and query["q"][0].casefold()
        == source_match.group("subreddit").casefold()
    )


def _is_same_moderator_route(source_url: str, current_url: str) -> bool:
    """Keep the account boundary pinned to the original moderator route."""
    source = _validated_reddit_json_url(source_url)
    current = _validated_reddit_json_url(current_url)
    if source is None or current is None:
        return False
    pattern = re.compile(
        r"/r/(?P<subreddit>[a-zA-Z0-9][a-zA-Z0-9_]{0,20})/"
        r"about/moderators\.json\Z"
    )
    source_match = pattern.fullmatch(source.path)
    current_match = pattern.fullmatch(current.path)
    return (
        source_match is not None
        and current_match is not None
        and source_match.group("subreddit").casefold()
        == current_match.group("subreddit").casefold()
    )


def _is_route_preserving_json_redirect(source_url: str, target_url: str) -> bool:
    """Allow only an equivalent JSON route, never an endpoint substitution."""

    source = _validated_reddit_json_url(source_url)
    target = _validated_reddit_json_url(target_url)
    if source is None or target is None or source.path != target.path:
        return False
    return sorted(parse_qsl(source.query, keep_blank_values=True)) == sorted(
        parse_qsl(target.query, keep_blank_values=True)
    )


def _sticky_json_redirect_target(
    source_url: str,
    target_url: str,
) -> str | None:
    """Validate Reddit's sticky-to-thread redirect and retain bounded scope."""

    source = _validated_reddit_json_url(source_url)
    target = _validated_reddit_json_url(target_url)
    if source is None or target is None or target.query:
        return None
    source_match = re.fullmatch(
        r"/r/(?P<subreddit>[a-zA-Z0-9][a-zA-Z0-9_]{0,20})/"
        r"about/sticky\.json",
        source.path,
    )
    target_match = re.fullmatch(
        r"/r/(?P<subreddit>[a-zA-Z0-9][a-zA-Z0-9_]{0,20})/"
        r"comments/[A-Za-z0-9]{2,16}/[^/]{1,512}/\.json",
        target.path,
    )
    if (
        source_match is None
        or target_match is None
        or source_match.group("subreddit").casefold()
        != target_match.group("subreddit").casefold()
    ):
        return None
    source_query = parse_qs(source.query, keep_blank_values=True)
    allowed = {"raw_json", "sort", "limit", "depth"}
    retained = {
        name: values[0]
        for name, values in source_query.items()
        if name in allowed and len(values) == 1
    }
    return target._replace(query=urlencode(retained), fragment="").geturl()


async def fetch_reddit_json(
    url: str,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None = None,
    timeout: float = 10.0,
    *,
    auth_required_on_403: bool = False,
    account_private_on_403: bool = False,
) -> dict:
    """
    Fetch Reddit JSON API with rate limiting.

    Args:
        url: Reddit API URL (must end in .json)
        session: wafer AsyncSession instance
        queue: Optional RedditRequestQueue for rate limiting
        timeout: Request timeout
        auth_required_on_403: Return a non-backoff marker for the one route
            whose anonymous 403 is a known user-context OAuth boundary.
        account_private_on_403: Treat an exact unstructured 403 from a known
            private account-activity route as a scoped access state rather
            than a transport-wide block.

    Returns:
        Dict with either 'data' or 'error'
    """

    deadline = monotonic_time.monotonic() + timeout

    if _validated_reddit_json_url(url) is None:
        return {"error": "Invalid Reddit JSON URL."}

    async def _do_get(request_url: str) -> dict:
        try:
            remaining = deadline - monotonic_time.monotonic()
            if remaining <= 0:
                return {"error": f"Request timed out ({timeout:g}s limit)"}
            response = await session.get(
                request_url,
                headers=_JSON_HEADERS,
                timeout=remaining,
            )
            return {"response": response}
        except wafer.WaferTimeout:
            return {"error": f"Request timed out ({timeout}s limit)"}
        except wafer.ResponseTooLarge:
            return {"error": "Reddit response too large (exceeds 50MB limit)."}
        except Exception as e:
            return {"error": f"Fetch failed: {e}"}

    async def _budgeted_get(request_url: str) -> dict:
        remaining = deadline - monotonic_time.monotonic()
        if remaining <= 0:
            return {"error": f"Request timed out ({timeout:g}s limit)"}
        if queue:
            async def queued_get():
                return await _do_get(request_url)

            try:
                return await queue.enqueue(
                    queued_get,
                    _queue_timeout=remaining,
                )
            except TimeoutError:
                return {"error": f"Request timed out ({timeout:g}s limit)"}
        try:
            await asyncio.wait_for(reddit_limiter.wait(), timeout=remaining)
        except TimeoutError:
            return {"error": f"Request timed out ({timeout:g}s limit)"}
        return await _do_get(request_url)

    current_url = url
    seen_urls: set[str] = set()
    redirect_count = 0
    gate_retry_attempted = False
    while redirect_count <= _MAX_JSON_REDIRECTS:
        if current_url in seen_urls:
            return {"error": "Reddit JSON redirect loop detected."}
        seen_urls.add(current_url)

        fetched = await _budgeted_get(current_url)
        if "error" in fetched:
            return fetched
        resp = fetched["response"]

        if resp.status_code in _JSON_REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return {
                    "error": (
                        "Reddit returned a redirect without a Location header."
                    )
                }
            target_url = urljoin(current_url, location)
            if _is_missing_subreddit_redirect(current_url, target_url):
                return {
                    "data": {
                        "_reddit_content_state": "Reddit content not found."
                    }
                }
            sticky_target = _sticky_json_redirect_target(
                current_url,
                target_url,
            )
            if sticky_target is not None:
                target_url = sticky_target
            elif not _is_route_preserving_json_redirect(current_url, target_url):
                return {"error": "Reddit returned an unsafe JSON redirect."}
            if redirect_count >= _MAX_JSON_REDIRECTS:
                return {"error": "Too many Reddit JSON redirects."}
            redirect_count += 1
            current_url = target_url
            continue

        payload = None
        try:
            payload = resp.json()
        except Exception:
            pass

        if resp.status_code == 429:
            retry_header = resp.headers.get("retry-after")
            retry_after = parse_retry_after(retry_header)
            applied_delay = 60.0 if retry_after is None else retry_after
            if queue:
                queue.set_backoff(429, retry_after=retry_after)
            else:
                reddit_limiter.defer(applied_delay)
            return {
                "error": (
                    "Rate limited. Reddit allows ~10 requests/min. "
                    f"Retry after {applied_delay:g}s."
                )
            }

        if resp.status_code >= 400:
            message, should_backoff = format_reddit_http_error(
                resp.status_code,
                payload,
            )
            retry_after = parse_retry_after(resp.headers.get("retry-after"))
            if _is_reddit_content_state(resp.status_code, payload):
                return {"data": {"_reddit_content_state": message}}
            if (
                resp.status_code == 403
                and auth_required_on_403
                and current_url == url
                and _is_same_moderator_route(url, current_url)
                and _is_unstructured_error(payload)
            ):
                return {"auth_required": True}
            if (
                resp.status_code == 403
                and account_private_on_403
                and current_url == url
                and (
                    _is_unstructured_error(payload)
                    or payload == {
                        "message": "Forbidden",
                        "error": 403,
                    }
                )
            ):
                return {
                    "error": (
                        "Reddit account-private activity is not publicly "
                        "readable."
                    )
                }
            if should_backoff:
                # Reddit's anonymous-session gate ("You've been blocked by
                # network security") is transient: wafer re-runs its
                # verification and re-establishes cookies in about two seconds,
                # which is why a manual retry succeeds immediately. Parking
                # every Reddit request behind a five-minute wall for it turned
                # a two-second self-healing blip into a five-minute outage.
                # An opaque 403 wafer did NOT recognise still gets the
                # conservative delay, because there we have no evidence it is
                # short-lived.
                gate = getattr(resp, "challenge_type", None) == "reddit"
                default_delay = (
                    _REDDIT_SESSION_GATE_BACKOFF if gate else _OPAQUE_403_BACKOFF
                )
                applied_delay = (
                    default_delay if retry_after is None else retry_after
                )
                if queue:
                    queue.set_backoff(
                        403,
                        retry_after=retry_after,
                        default_delay=default_delay,
                    )
                else:
                    reddit_limiter.defer(applied_delay)
                if gate:
                    message = (
                        "Reddit's anonymous session gate blocked this request. "
                        "The session is being re-established -- retry in a few "
                        "seconds."
                    )
                    # wafer has already re-established the anonymous cookies by
                    # the time it returns this tagged response. Keep the retry
                    # inside the caller's original deadline so a cold request
                    # succeeds without asking the MCP client to coordinate a
                    # second call. One retry is enough; a repeated gate remains
                    # an honest error and cannot loop.
                    remaining = deadline - monotonic_time.monotonic()
                    if (
                        not gate_retry_attempted
                        and applied_delay < remaining
                    ):
                        gate_retry_attempted = True
                        seen_urls.discard(current_url)
                        continue
            return {"error": message}

        if payload is None:
            return {"error": "Reddit returned a non-JSON response."}
        return {"data": payload}

    return {"error": "Too many Reddit JSON redirects."}


def _validated_listing_data(payload: object) -> dict | None:
    """Return Listing.data only when every renderer-facing field is safe."""

    # Imported lazily because reddit_fetch owns the structured-route validator
    # and itself reuses this module's transport/session implementation.
    from .reddit_fetch import _is_post_data

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    children = data.get("children")
    if not isinstance(children, list) or any(
        not isinstance(child, dict)
        or child.get("kind") != "t3"
        or not isinstance(child.get("data"), dict)
        or not _is_post_data(child["data"])
        for child in children
    ):
        return None
    return data


async def browse_reddit(
    subreddit: str,
    sort: str = "hot",
    time: str = "day",
    limit: int = 10,
    after: str | None = None,
    timeout: int = 10,
    queue: RedditRequestQueue | None = None,
    browser_solver=None,
) -> dict:
    """
    Browse a subreddit's posts.

    Args:
        subreddit: Subreddit name without r/ prefix
        sort: Sort order - hot, new, top, rising
        time: Time filter for 'top' - hour, day, week, month, year, all
        limit: Number of posts (1-25)
        after: Pagination cursor from previous response
        timeout: Request timeout in seconds
        queue: Optional RedditRequestQueue for rate limiting

    Returns:
        Dict with content or error
    """
    # Validate subreddit name
    if not _SUBREDDIT_PATTERN.fullmatch(subreddit):
        return {"error": "Invalid subreddit name"}

    # Validate sort
    if sort not in ("hot", "new", "top", "rising"):
        return {"error": "Invalid sort. Must be: hot, new, top, rising"}

    # Validate time filter
    if time not in ("hour", "day", "week", "month", "year", "all"):
        return {"error": "Invalid time. Must be: hour, day, week, month, year, all"}

    # Clamp limit
    limit = max(1, min(25, limit))
    if after is not None and not _PAGINATION_CURSOR_PATTERN.fullmatch(after):
        return {"error": "Invalid Reddit pagination cursor"}

    # Build URL
    params = {"limit": str(limit), "raw_json": "1"}
    if sort == "top":
        params["t"] = time
    if after:
        params["after"] = after

    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?{urlencode(params)}"

    session = await _get_session(browser_solver)
    result = await fetch_reddit_json(url, session, queue, float(timeout))

    if "error" in result:
        return result

    payload = result["data"]
    if (
        isinstance(payload, dict)
        and (content_state := payload.get("_reddit_content_state"))
    ):
        return {"content": f"r/{subreddit} · {sort}\n\n{content_state}"}
    listing_data = _validated_listing_data(payload)
    if listing_data is None:
        return {"error": "Reddit returned an invalid listing response"}
    posts = listing_data["children"]
    after_cursor = listing_data.get("after")

    if not posts:
        return {"content": f"r/{subreddit} · {sort} · No posts found"}

    # Format output
    lines = [f"r/{subreddit} · {sort} · {len(posts)} posts\n"]

    for i, post in enumerate(posts, 1):
        lines.append(format_reddit_post(post.get("data", {}), i, include_subreddit=False))

    if isinstance(after_cursor, str) and _PAGINATION_CURSOR_PATTERN.fullmatch(
        after_cursor
    ):
        lines.append(f"\n[Next page: after={after_cursor}]")
    elif after_cursor is not None:
        lines.append(
            "\n[Next page unavailable: Reddit returned an invalid pagination "
            "cursor]"
        )

    return {"content": "\n".join(lines)}
