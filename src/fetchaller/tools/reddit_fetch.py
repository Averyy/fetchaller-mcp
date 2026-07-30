"""Structured ``fetch`` support for normal Reddit URLs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from math import isfinite
from urllib.parse import (
    parse_qs,
    quote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import wafer
from bs4 import BeautifulSoup

from ..config import Config
from ..content.reddit import (
    RedditRoute,
    normalize_moderator_roster_children,
    parse_reddit_related_html,
    parse_reddit_wiki_page_tree,
    parse_reddit_wiki_pages_html,
    render_reddit_route,
)
from ..queue.reddit_queue import RedditRequestQueue, parse_retry_after
from ..ratelimit import reddit_limiter
from .browse_reddit import _get_session, fetch_reddit_json

_MODERATOR_AUTH_REQUIRED = (
    "Reddit requires a logged-in account for exact moderator rosters, and "
    "fetchaller reads Reddit anonymously only. No moderator names were guessed "
    "or reconstructed."
)


def valid_moderator_roster(value: object) -> bool:
    """Require the UserList shape before handing it to the renderer."""
    if (
        not isinstance(value, dict)
        or value.get("error")
        or value.get("kind") != "UserList"
    ):
        return False
    return normalize_moderator_roster_children(value, allow_wrapped=False) is not None
_MODERATOR_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_MODERATOR_PAGES = 20
_COLLECTION_UUID = re.compile(
    r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
    r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}\Z"
)
_WAYBACK_TIMESTAMP = re.compile(r"\d{14}\Z")
_WAYBACK_ORIGIN = "https://web.archive.org"
_MAX_COLLECTION_ARCHIVE_CANDIDATES = 5
_MAX_COLLECTION_ARCHIVE_HTML = 8 * 1024 * 1024
_MAX_GILDED_ARCHIVE_HTML = 8 * 1024 * 1024
_MAX_GILDED_ARCHIVE_CANDIDATES = 5
_MAX_GILDED_ARCHIVE_PAGES = 10
_GOLD_DIRECTORY_ARCHIVE_TIMESTAMP = "20180823171238"
_GOLD_DIRECTORY_ARCHIVE_ORIGINAL = (
    "https://www.reddit.com/subreddits/gold/"
)
_PINNED_GILDED_ARCHIVE_CANDIDATES: dict[
    str,
    tuple[tuple[str, str], ...],
] = {
    "www.reddit.com/gilded/": (
        (
            "20170523232153",
            "https://www.reddit.com/gilded/",
        ),
    ),
    "www.reddit.com/comments/gilded": (
        (
            "20150509040515",
            "http://www.reddit.com:80/comments/gilded",
        ),
    ),
}
_REDUX_ASSIGNMENT = "window.___r = "
_SCRIPT_OPEN = re.compile(r"<script(?:\s[^<>]{0,4096})?>\Z", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script\s*>\Z", re.IGNORECASE)
_DIRECTORY_USERNAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_THING_FULLNAME = re.compile(r"t[13]_[A-Za-z0-9]{2,16}\Z")
_PAGINATION_FULLNAME = re.compile(r"t[1-6]_[A-Za-z0-9]{2,16}\Z")
_COMMENT_FULLNAME = re.compile(r"t1_[A-Za-z0-9]{2,16}\Z")
_GLOBAL_COMMENTS_MIN_TRANSFER = 25
_SHREDDIT_GRAPHQL_URL = "https://www.reddit.com/svc/shreddit/graphql"
_WIKI_PAGE_TREE_OPERATION = "WikiPageRevisionsV2"
_CSRF_TOKEN_RE = re.compile(r"[A-Fa-f0-9]{20,80}\Z")
_MAX_WIKI_PAGE_TREE_BYTES = 4 * 1024 * 1024


def _nonempty_string(value: object, *, maximum: int = 4096) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_thing_identity(data: dict, prefix: str) -> bool:
    thing_id = data.get("id")
    fullname = data.get("name")
    valid_id = (
        isinstance(thing_id, str)
        and re.fullmatch(r"[A-Za-z0-9]{2,16}", thing_id) is not None
    )
    valid_fullname = (
        isinstance(fullname, str)
        and re.fullmatch(rf"{prefix}_[A-Za-z0-9]{{2,16}}", fullname) is not None
    )
    if thing_id is not None and not valid_id:
        return False
    if fullname is not None and not valid_fullname:
        return False
    if valid_id and valid_fullname and fullname != f"{prefix}_{thing_id}":
        return False
    return valid_id or valid_fullname


def _valid_optional_fields(
    data: dict,
    *,
    strings: tuple[str, ...] = (),
    numbers: tuple[str, ...] = (),
    mappings: tuple[str, ...] = (),
    lists: tuple[str, ...] = (),
) -> bool:
    return (
        all(
            field not in data
            or data[field] is None
            or isinstance(data[field], str)
            for field in strings
        )
        and all(
            field not in data
            or data[field] is None
            or _finite_number(data[field])
            for field in numbers
        )
        # Reddit marks an absent structured field with ``false`` as readily as
        # with ``null`` -- ``poll_data`` on a moderator-removed post is the case
        # that bit us. Only ``false`` is tolerated; any other non-mapping value
        # is still malformed.
        and all(
            field not in data
            or data[field] is None
            # ``false`` means "absent" for every one of these, not just
            # poll_data (where it was first observed): the same Reddit
            # serializer produces gallery_data/gildings/media_embed. Narrowing
            # this to poll_data was tried and reverted -- rejecting the field
            # fails the WHOLE listing, so the user gets nothing instead of a
            # post without its gallery. Only ``False`` is tolerated; any other
            # non-mapping is still malformed.
            or data[field] is False
            or isinstance(data[field], dict)
            for field in mappings
        )
        and all(
            field not in data
            or data[field] is None
            or isinstance(data[field], list)
            for field in lists
        )
    )


def _valid_media_metadata(metadata: object) -> bool:
    if metadata is None:
        return True
    if not isinstance(metadata, dict):
        return False
    for media in metadata.values():
        if not isinstance(media, dict):
            return False
        source = media.get("s")
        if source is not None and not isinstance(source, dict):
            return False
        for container in (media, source or {}):
            if any(
                key in container
                and container[key] is not None
                and not isinstance(container[key], str)
                for key in (
                    "u",
                    "gif",
                    "mp4",
                    "hlsUrl",
                    "dashUrl",
                    "hls_url",
                    "dash_url",
                )
            ):
                return False
    return True


def _valid_media_container(container: object) -> bool:
    if container is None:
        return True
    if not isinstance(container, dict):
        return False
    for name in ("reddit_video", "oembed"):
        nested = container.get(name)
        if nested is not None and not isinstance(nested, dict):
            return False
    video = container.get("reddit_video") or {}
    oembed = container.get("oembed") or {}
    return all(
        field not in video
        or video[field] is None
        or isinstance(video[field], str)
        for field in ("fallback_url", "hls_url", "dash_url")
    ) and all(
        field not in oembed
        or oembed[field] is None
        or isinstance(oembed[field], str)
        for field in ("provider_name", "title")
    )


def _valid_poll_data(poll: object) -> bool:
    # Reddit sends ``"poll_data": false`` -- not null -- on moderator-removed
    # posts, the same false-means-absent idiom it uses for ``edited``. Treating
    # that as malformed failed the post, and because every child must validate,
    # two removed posts discarded a user's entire 100-item activity listing.
    if poll is None or poll is False:
        return True
    if not isinstance(poll, dict):
        return False
    options = poll.get("options")
    if options is not None and (
        not isinstance(options, list)
        or any(
            not isinstance(option, dict)
            or (
                option.get("text") is not None
                and not isinstance(option.get("text"), str)
            )
            or (
                option.get("vote_count") is not None
                and not _finite_number(option.get("vote_count"))
            )
            for option in options
        )
    ):
        return False
    return all(
        field not in poll
        or poll[field] is None
        or _finite_number(poll[field])
        for field in ("total_vote_count", "voting_end_timestamp")
    )


def _is_post_data(
    data: dict,
    *,
    require_identity: bool = True,
    validate_crossposts: bool = True,
) -> bool:
    if not (
        (not require_identity or _valid_thing_identity(data, "t3"))
        and (
            not require_identity
            or _nonempty_string(data.get("title"), maximum=1000)
        )
        and _valid_optional_fields(
            data,
            strings=(
                "title",
                "author",
                "permalink",
                "selftext",
                "subreddit",
                "subreddit_name_prefixed",
                "url",
                "url_overridden_by_dest",
                "link_flair_text",
                "author_flair_text",
            ),
            numbers=(
                "score",
                "upvote_ratio",
                "num_comments",
                "created_utc",
                "total_awards_received",
            ),
            mappings=(
                "gallery_data",
                "media_metadata",
                "secure_media",
                "media",
                "poll_data",
                "gildings",
            ),
            lists=(
                "crosspost_parent_list",
                "all_awardings",
                "link_flair_richtext",
                "author_flair_richtext",
            ),
        )
    ):
        return False
    edited = data.get("edited")
    if edited is not None and edited is not False and not _finite_number(edited):
        return False
    gallery = data.get("gallery_data")
    if isinstance(gallery, dict):
        items = gallery.get("items")
        if items is not None and (
            not isinstance(items, list)
            or any(
                not isinstance(item, dict)
                or (
                    item.get("media_id") is not None
                    and not isinstance(item.get("media_id"), str)
                )
                or (
                    item.get("caption") is not None
                    and not isinstance(item.get("caption"), str)
                )
                or (
                    item.get("outbound_url") is not None
                    and not isinstance(item.get("outbound_url"), str)
                )
                for item in items
            )
        ):
            return False
    metadata = data.get("media_metadata")
    if not _valid_media_metadata(metadata):
        return False
    for container_name in ("secure_media", "media"):
        if not _valid_media_container(data.get(container_name)):
            return False
    if not _valid_poll_data(data.get("poll_data")):
        return False
    crossposts = data.get("crosspost_parent_list")
    if not isinstance(crossposts, list):
        return True
    if not all(isinstance(parent, dict) for parent in crossposts):
        return False
    return not validate_crossposts or all(
        _is_post_data(
            parent,
            require_identity=False,
            validate_crossposts=False,
        )
        for parent in crossposts
    )


def _is_comment_data(data: dict) -> bool:
    return (
        _valid_thing_identity(data, "t1")
        and isinstance(data.get("body"), str)
        and _valid_optional_fields(
            data,
            strings=(
                "author",
                "body_html",
                "permalink",
                "subreddit",
                "subreddit_name_prefixed",
                "link_title",
                "author_flair_text",
            ),
            numbers=(
                "score",
                "created_utc",
                "total_awards_received",
            ),
            mappings=("media_metadata", "gildings"),
            lists=("all_awardings", "author_flair_richtext"),
        )
        and (
            data.get("edited") is None
            or data.get("edited") is False
            or _finite_number(data.get("edited"))
        )
        and _valid_media_metadata(data.get("media_metadata"))
    )


def _is_more_data(data: dict) -> bool:
    children = data.get("children")
    count = data.get("count")
    return (
        isinstance(children, list)
        and all(
            isinstance(child, str)
            and re.fullmatch(r"[A-Za-z0-9]{2,16}", child) is not None
            for child in children
        )
        and (count is None or (_finite_number(count) and float(count) >= 0))
    )


def _is_user_data(data: dict) -> bool:
    prefixed = data.get("display_name_prefixed")
    profile_path = data.get("url")
    return (
        _nonempty_string(data.get("name"), maximum=64)
        or (
            isinstance(prefixed, str)
            and re.fullmatch(r"u/[A-Za-z0-9_-]{1,64}", prefixed, re.IGNORECASE)
            is not None
        )
        or (
            isinstance(profile_path, str)
            and re.fullmatch(
                r"/(?:user|u)/[A-Za-z0-9_-]{1,64}/?",
                profile_path,
                re.IGNORECASE,
            )
            is not None
        )
    )


def _directory_username(data: dict, kind: object) -> str | None:
    """Extract a canonical public username without trusting the t5 fullname."""

    account_name = data.get("name")
    if (
        kind == "t2"
        and isinstance(account_name, str)
        and _DIRECTORY_USERNAME.fullmatch(account_name)
    ):
        return account_name
    prefixed = data.get("display_name_prefixed")
    if isinstance(prefixed, str) and prefixed.casefold().startswith("u/"):
        candidate = prefixed[2:]
        if _DIRECTORY_USERNAME.fullmatch(candidate):
            return candidate
    profile_path = data.get("url")
    if isinstance(profile_path, str):
        match = re.fullmatch(
            r"/(?:user|u)/([A-Za-z0-9_-]{1,64})/?",
            profile_path,
            re.IGNORECASE,
        )
        if match is not None:
            return match.group(1)
    return None


def _valid_directory_user_about(payload: object, username: str) -> bool:
    """Require the exact account fields absent from Reddit's t5 directory card."""

    if not (
        isinstance(payload, dict)
        and payload.get("kind") == "t2"
        and isinstance(payload.get("data"), dict)
    ):
        return False
    data = payload["data"]
    return (
        isinstance(data.get("name"), str)
        and data["name"].casefold() == username.casefold()
        and _finite_number(data.get("link_karma"))
        and _finite_number(data.get("comment_karma"))
        and _finite_number(data.get("created_utc") or data.get("created"))
        and float(data.get("created_utc") or data.get("created")) > 0
    )


async def _hydrate_user_directory(
    payload: object,
    *,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None,
    deadline: float,
) -> dict:
    """Hydrate every bounded directory card with its exact public account."""

    if not isinstance(payload, dict):
        return {"error": "Reddit user directory returned an invalid response."}
    listing_data = payload.get("data")
    children = (
        listing_data.get("children")
        if isinstance(listing_data, dict)
        else None
    )
    if not isinstance(children, list):
        return {"error": "Reddit user directory returned an invalid response."}

    enriched_children: list[dict] = []
    for child in children:
        card = child.get("data") if isinstance(child, dict) else None
        username = (
            _directory_username(card, child.get("kind"))
            if isinstance(card, dict)
            else None
        )
        if username is None:
            return {
                "error": (
                    "Reddit user directory profile hydration lacked an exact "
                    "username."
                )
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "error": "Reddit user directory profile hydration timed out."
            }
        about_url = (
            "https://www.reddit.com/user/"
            f"{quote(username, safe='')}/about.json?raw_json=1"
        )
        result = await fetch_reddit_json(
            about_url,
            session,
            queue,
            remaining,
        )
        if "error" in result:
            detail = _bounded_related_detail(result["error"])
            return {
                "error": (
                    "Reddit user directory profile hydration failed "
                    f"({detail})"
                )
            }
        about = result.get("data")
        if not _valid_directory_user_about(about, username):
            return {
                "error": (
                    "Reddit user directory profile hydration returned a "
                    "substituted or incomplete account."
                )
            }
        about_data = about["data"]
        enriched_card = dict(card)
        for field in ("link_karma", "comment_karma", "created_utc", "created"):
            if field in about_data:
                enriched_card[field] = about_data[field]
        enriched_child = dict(child)
        enriched_child["data"] = enriched_card
        enriched_children.append(enriched_child)

    enriched = dict(payload)
    enriched_data = dict(listing_data)
    enriched_data["children"] = enriched_children
    enriched["data"] = enriched_data
    return {"data": enriched}


def _is_community_data(data: dict) -> bool:
    prefixed = data.get("display_name_prefixed")
    return _nonempty_string(data.get("display_name"), maximum=100) or (
        isinstance(prefixed, str)
        and re.fullmatch(r"r/[A-Za-z0-9_+/-]{1,100}", prefixed, re.IGNORECASE)
        is not None
    )


def _is_wiki_revision_data(data: dict) -> bool:
    author = data.get("author")
    return (
        _nonempty_string(
            data.get("id") or data.get("revision_id"),
            maximum=128,
        )
        and _nonempty_string(data.get("page"), maximum=1024)
        and (
            data.get("timestamp") is None
            or _finite_number(data.get("timestamp"))
        )
        and (
            author is None
            or (
                isinstance(author, dict)
                and _is_user_data(
                    author["data"]
                    if isinstance(author.get("data"), dict)
                    else author
                )
            )
        )
    )


def _is_live_update_data(data: dict) -> bool:
    return (
        _nonempty_string(data.get("id"), maximum=128)
        and isinstance(data.get("body"), str)
    )


def _is_listing_child(child: object, allowed_kinds: frozenset[str]) -> bool:
    if not isinstance(child, dict) or not isinstance(child.get("data"), dict):
        return False
    kind = child.get("kind")
    if kind not in allowed_kinds:
        return False
    data = child["data"]
    validators = {
        "t1": _is_comment_data,
        "t2": _is_user_data,
        "t3": _is_post_data,
        "t5": lambda value: _is_community_data(value) or _is_user_data(value),
        "more": _is_more_data,
        "WikiRevision": _is_wiki_revision_data,
        "LiveUpdate": _is_live_update_data,
    }
    validator = validators.get(str(kind))
    return validator is not None and validator(data)


def _is_listing_payload(
    payload: object,
    *,
    allowed_kinds: frozenset[str],
    require_nonempty: bool = False,
) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    children = data.get("children")
    return (
        isinstance(children, list)
        and (not require_nonempty or bool(children))
        and all(
            _is_listing_child(child, allowed_kinds)
            for child in children
        )
    )


def _is_comment_listing_payload(payload: object) -> bool:
    """Validate nested t1/more trees iteratively to avoid recursion hazards."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return False
    children = payload["data"].get("children")
    if not isinstance(children, list):
        return False
    pending = list(children)
    visited = 0
    while pending:
        child = pending.pop()
        visited += 1
        if visited > 100_000 or not _is_listing_child(
            child,
            frozenset({"t1", "more"}),
        ):
            return False
        if child["kind"] != "t1":
            continue
        replies = child["data"].get("replies")
        if replies in (None, ""):
            continue
        if not isinstance(replies, dict) or not isinstance(
            replies.get("data"),
            dict,
        ):
            return False
        nested = replies["data"].get("children")
        if not isinstance(nested, list):
            return False
        pending.extend(nested)
    return True


def _is_wiki_revision_listing_payload(payload: object) -> bool:
    """Accept Reddit's real flat WikiRevision children and wrapped fixtures."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return False
    children = payload["data"].get("children")
    if not isinstance(children, list):
        return False
    return all(
        (
            _is_listing_child(child, frozenset({"WikiRevision"}))
            if isinstance(child, dict) and "kind" in child
            else isinstance(child, dict) and _is_wiki_revision_data(child)
        )
        for child in children
    )


def _is_data_object(payload: object) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("data"), dict)


def _has_data_field(payload: object, field: str, expected_type: type) -> bool:
    return _is_data_object(payload) and isinstance(
        payload["data"].get(field),
        expected_type,
    )


def _has_nonempty_data_string(
    payload: object,
    field: str,
    *,
    maximum: int = 4096,
) -> bool:
    return _is_data_object(payload) and _nonempty_string(
        payload["data"].get(field),
        maximum=maximum,
    )


def _is_trophy_payload(payload: object) -> bool:
    if not _is_data_object(payload):
        return False
    trophies = payload["data"].get("trophies")
    return isinstance(trophies, list) and all(
        isinstance(trophy, dict)
        and isinstance(trophy.get("data"), dict)
        and _nonempty_string(trophy["data"].get("name"), maximum=256)
        for trophy in trophies
    )


def _is_multi_payload(payload: object) -> bool:
    if not _is_data_object(payload):
        return False
    data = payload["data"]
    subreddits = data.get("subreddits")
    identity = data.get("display_name") or data.get("name") or data.get("path")
    return (
        _nonempty_string(identity, maximum=512)
        and isinstance(subreddits, list)
        and all(
            isinstance(subreddit, dict)
            and _nonempty_string(
                subreddit.get("name") or subreddit.get("display_name"),
                maximum=100,
            )
            for subreddit in subreddits
        )
    )


def _is_rules_payload(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("rules"), list)
        and all(
            isinstance(rule, dict)
            and _nonempty_string(rule.get("short_name"), maximum=256)
            and (
                rule.get("description") is None
                or isinstance(rule.get("description"), str)
            )
            and (
                rule.get("description_html") is None
                or isinstance(rule.get("description_html"), str)
            )
            for rule in payload["rules"]
        )
        and isinstance(payload.get("site_rules"), list)
        and all(
            _nonempty_string(rule, maximum=1024)
            for rule in payload["site_rules"]
        )
    )


def _is_wiki_pages_payload(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("kind") == "wikipagelisting"
        and isinstance(payload.get("data"), list)
        and all(
            _nonempty_string(page, maximum=1024)
            and all(part not in {"", ".", ".."} for part in page.split("/"))
            and "\\" not in page
            and all(
                ord(character) > 0x20 and ord(character) != 0x7F
                for character in page
            )
            for page in payload["data"]
        )
    )


def _is_live_contributors_payload(payload: object) -> bool:
    if not _is_data_object(payload):
        return False
    children = payload["data"].get("children")
    if not isinstance(children, list):
        return False
    return all(
        isinstance(child, dict)
        and _is_user_data(
            child["data"]
            if isinstance(child.get("data"), dict)
            else child
        )
        for child in children
    )


def _is_moderated_list_payload(payload: object) -> bool:
    """Validate Reddit's real ``ModeratedList`` profile subresource."""

    return (
        isinstance(payload, dict)
        and payload.get("kind") == "ModeratedList"
        and isinstance(payload.get("data"), list)
        and all(
            isinstance(community, dict)
            and _is_community_data(community)
            for community in payload["data"]
        )
    )


def _valid_morechildren_things(value: object) -> bool:
    """Validate every nested Thing while tolerating Reddit's command arrays."""

    pending = [value]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 100_000:
            return False
        if isinstance(current, list):
            pending.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        if "kind" in current and not _is_listing_child(
            current,
            frozenset({"t1", "more"}),
        ):
            return False
        pending.extend(current.values())
    return True


def _is_morechildren_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return False
    jquery = payload.get("jquery")
    if isinstance(jquery, list):
        return (
            payload.get("success") is True
            and all(isinstance(command, list) for command in jquery)
            and _valid_morechildren_things(jquery)
        )
    json_wrapper = payload.get("json")
    if isinstance(json_wrapper, dict):
        data = json_wrapper.get("data")
        things = data.get("things") if isinstance(data, dict) else None
        return isinstance(things, list) and all(
            _is_listing_child(thing, frozenset({"t1", "more"}))
            for thing in things
        )
    return False


def _is_collection_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    link_ids = payload.get("link_ids")
    title = payload.get("title")
    description = payload.get("description")
    return (
        isinstance(link_ids, list)
        and 1 <= len(link_ids) <= 1000
        and all(
            isinstance(link_id, str)
            and re.fullmatch(r"t3_[A-Za-z0-9]{2,16}", link_id) is not None
            for link_id in link_ids
        )
        and len(set(link_ids)) == len(link_ids)
        and isinstance(title, str)
        and bool(title.strip())
        and len(title) <= 1000
        and (
            description is None
            or (
                isinstance(description, str)
                and len(description) <= 100_000
            )
        )
    )


def _payload_schema_error(
    route: RedditRoute,
    index: int,
    payload: object,
) -> str | None:
    """Name malformed 2xx JSON instead of rendering it as an empty success."""

    kind = route.kind
    label = kind.replace("_", " ")
    valid = True
    listing_kinds = {
        "listing": frozenset({"t1", "t3"}),
        "domain_listing": frozenset({"t3"}),
        "comment_listing": frozenset({"t1", "t3"}),
        "search": frozenset({"t2", "t3", "t5"}),
        "user_listing": frozenset({"t1", "t3"}),
        "subreddit_directory": frozenset({"t5"}),
        "user_directory": frozenset({"t2", "t5"}),
        "wiki_discussions": frozenset({"t3"}),
        "live_update": frozenset({"LiveUpdate"}),
    }
    if kind in listing_kinds:
        valid = _is_listing_payload(
            payload,
            allowed_kinds=listing_kinds[kind],
            require_nonempty=kind == "live_update",
        )
    elif kind == "thread":
        valid = (
            isinstance(payload, list)
            and len(payload) >= 2
            and _is_listing_payload(
                payload[0],
                allowed_kinds=frozenset({"t3"}),
                require_nonempty=True,
            )
            and _is_comment_listing_payload(payload[1])
        )
    elif kind == "duplicates":
        valid = (
            isinstance(payload, list)
            and len(payload) >= 2
            and _is_listing_payload(
                payload[0],
                allowed_kinds=frozenset({"t3"}),
                require_nonempty=True,
            )
            and _is_listing_payload(
                payload[1],
                allowed_kinds=frozenset({"t3"}),
            )
        )
    elif kind == "wiki_revisions":
        valid = _is_wiki_revision_listing_payload(payload)
    elif kind == "user_profile":
        validators = (
            lambda value: _has_nonempty_data_string(
                value,
                "name",
                maximum=64,
            ),
            lambda value: _is_listing_payload(
                value,
                allowed_kinds=frozenset({"t1", "t3"}),
            ),
            _is_trophy_payload,
            lambda value: isinstance(value, list)
            and all(_is_multi_payload(item) for item in value),
            # Reddit answers `{}` for an account that moderates nothing --
            # confirmed on both bh-alienux and AutoModerator, while a real
            # moderator gets a ModeratedList. That is an empty result, not a
            # malformed one.
            lambda value: value == {}
            or _is_listing_payload(value, allowed_kinds=frozenset({"t5"}))
            or _is_moderated_list_payload(value),
        )
        valid = index < len(validators) and validators[index](payload)
        label = (
            "user profile details",
            "user profile activity",
            "user trophies",
            "public multireddits",
            "moderated communities",
        )[index]
    elif kind == "user_about":
        valid = _has_nonempty_data_string(payload, "name", maximum=64)
    elif kind == "subreddit_about":
        valid = _has_nonempty_data_string(
            payload,
            "display_name",
            maximum=100,
        )
    elif kind == "wiki":
        valid = _has_data_field(payload, "content_md", str)
    elif kind == "live_about":
        valid = _has_nonempty_data_string(payload, "title", maximum=1000)
    elif kind == "rules":
        valid = _is_rules_payload(payload)
    elif kind == "wiki_diff":
        valid = _has_data_field(payload, "content_md", str)
        label = f"wiki revision {index + 1}"
    elif kind == "wiki_pages":
        valid = _is_wiki_pages_payload(payload)
    elif kind == "trophies":
        valid = _is_trophy_payload(payload)
    elif kind == "multi_about":
        valid = _is_multi_payload(payload)
    elif kind == "multi_profile":
        valid = (
            _is_multi_payload(payload)
            if index == 0
            else _is_listing_payload(
                payload,
                allowed_kinds=frozenset({"t1", "t3"}),
            )
        )
        label = "multireddit details" if index == 0 else "multireddit feed"
    elif kind == "moderators":
        # Exact field validation and pagination are handled separately.
        return None
    elif kind == "related":
        valid = _is_listing_payload(
            payload,
            allowed_kinds=frozenset({"t3"}),
            require_nonempty=True,
        )
        label = "related-post source"
    elif kind == "live":
        valid = (
            _has_nonempty_data_string(payload, "title", maximum=1000)
            if index == 0
            else _is_listing_payload(
                payload,
                allowed_kinds=frozenset({"LiveUpdate"}),
            )
        )
        label = "live thread details" if index == 0 else "live updates"
    elif kind == "live_contributors":
        valid = _is_live_contributors_payload(payload)
    elif kind == "morechildren":
        valid = _is_morechildren_payload(payload)
    elif kind == "collection":
        valid = _is_collection_payload(payload)
    return None if valid else f"Reddit returned an invalid {label} response."


def _global_comments_transfer(
    route: RedditRoute,
    request_url: str,
) -> tuple[str, int] | None:
    """Overfetch the live global comment feed without changing page semantics."""

    if (
        route.kind != "comment_listing"
        or route.label != "comments"
        or urlparse(route.canonical_url).path.rstrip("/") != "/comments"
    ):
        return None
    parsed = urlparse(request_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    values = query.get("limit")
    if (
        not values
        or not values[0].isdigit()
        or not 1 <= int(values[0]) <= 500
    ):
        return None
    requested_limit = int(values[0])
    query["limit"] = [str(max(requested_limit, _GLOBAL_COMMENTS_MIN_TRANSFER))]
    # The current /r/all/comments JSON backend accepts the canonical t1
    # fullname in rendered Reddit URLs but only paginates when its API request
    # receives the bare base36 id. Keep the public cursor canonical and adapt
    # only the fixed-origin backend request.
    for cursor_name in ("after", "before"):
        cursor_values = query.get(cursor_name)
        if (
            cursor_values
            and _COMMENT_FULLNAME.fullmatch(cursor_values[0]) is not None
        ):
            query[cursor_name] = [cursor_values[0].removeprefix("t1_")]
    transfer_url = urlunparse(
        parsed._replace(query=urlencode(query, doseq=True)),
    )
    return transfer_url, requested_limit


def _slice_global_comments_page(
    payload: object,
    requested_limit: int,
) -> dict | None:
    """Retain the caller's page size and cursor to the last visible comment."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None
    children = payload["data"].get("children")
    if not isinstance(children, list):
        return None
    retained = children[:requested_limit]
    if retained:
        last = retained[-1]
        last_data = last.get("data") if isinstance(last, dict) else None
        fullname = last_data.get("name") if isinstance(last_data, dict) else None
        if (
            not isinstance(last, dict)
            or last.get("kind") != "t1"
            or not isinstance(fullname, str)
            or _COMMENT_FULLNAME.fullmatch(fullname) is None
        ):
            return None
    else:
        fullname = None
    sliced = dict(payload)
    sliced_data = dict(payload["data"])
    sliced_data["children"] = retained
    sliced_data["dist"] = len(retained)
    # Reddit's small-limit global comment feed can return an empty page. The
    # transfer therefore asks for at least 25, but pagination must start after
    # the last comment actually shown or the hidden comments would be skipped.
    sliced_data["after"] = fullname
    sliced["data"] = sliced_data
    return sliced


def _restore_reverse_listing_after(
    payload: object,
    request_url: str,
) -> object:
    """Restore forward navigation after Reddit serves a ``before`` page.

    Some anonymous listing endpoints return the correct earlier items and a
    correct ``before`` cursor, but omit ``data.after``.  A valid ``before``
    request proves that later content exists.  The last exact fullname on the
    returned page is therefore the canonical forward cursor back to it.
    Thread/duplicates JSON wraps the paginated listing in an array, so the
    final eligible listing in that exact response receives the same repair.
    """

    query = parse_qs(
        urlparse(request_url).query,
        keep_blank_values=True,
    )
    before_values = query.get("before") or []
    if (
        len(before_values) != 1
        or _PAGINATION_FULLNAME.fullmatch(before_values[0]) is None
        or query.get("after")
    ):
        return payload
    if isinstance(payload, list):
        for index in range(len(payload) - 1, -1, -1):
            candidate = payload[index]
            if not (
                isinstance(candidate, dict)
                and candidate.get("kind") == "Listing"
                and isinstance(candidate.get("data"), dict)
            ):
                continue
            restored_child = _restore_reverse_listing_after(
                candidate,
                request_url,
            )
            if restored_child is candidate:
                return payload
            restored_payload = list(payload)
            restored_payload[index] = restored_child
            return restored_payload
        return payload
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "Listing"
        or not isinstance(payload.get("data"), dict)
        or payload["data"].get("after") is not None
    ):
        return payload
    children = payload["data"].get("children")
    if not isinstance(children, list) or not children:
        return payload
    last = children[-1]
    last_data = last.get("data") if isinstance(last, dict) else None
    fullname = last_data.get("name") if isinstance(last_data, dict) else None
    if (
        not isinstance(last, dict)
        or re.fullmatch(r"t[1-6]", str(last.get("kind") or "")) is None
        or not isinstance(fullname, str)
        or _PAGINATION_FULLNAME.fullmatch(fullname) is None
        or not fullname.startswith(f"{last['kind']}_")
    ):
        return payload
    restored = dict(payload)
    restored_data = dict(payload["data"])
    restored_data["after"] = fullname
    restored["data"] = restored_data
    return restored


def _slice_randomrising_page(payload: object, route: RedditRoute) -> dict | None:
    """Apply a stable pseudo-random order to the exact current rising pool."""

    if not (
        isinstance(payload, dict)
        and payload.get("kind") == "Listing"
        and isinstance(payload.get("data"), dict)
        and isinstance(payload["data"].get("children"), list)
        and (route.label or "").endswith("randomrising")
    ):
        return None
    children = payload["data"]["children"]
    fullnames: list[str] = []
    for child in children:
        data = child.get("data") if isinstance(child, dict) else None
        fullname = data.get("name") if isinstance(data, dict) else None
        if (
            not isinstance(child, dict)
            or child.get("kind") != "t3"
            or not isinstance(fullname, str)
            or re.fullmatch(r"t3_[A-Za-z0-9]{2,16}", fullname) is None
            or fullname in fullnames
        ):
            return None
        fullnames.append(fullname)
    scope = urlparse(route.canonical_url).path.rstrip("/").casefold()
    ordered = sorted(
        zip(fullnames, children),
        key=lambda pair: hashlib.sha256(
            (
                "fetchaller-randomrising-v1\0"
                + scope
                + "\0"
                + pair[0]
            ).encode()
        ).digest(),
    )
    query = parse_qs(
        urlparse(route.canonical_url).query,
        keep_blank_values=True,
    )
    raw_limit = (query.get("limit") or [""])[0]
    requested_limit = (
        min(100, int(raw_limit))
        if raw_limit.isdigit() and int(raw_limit) > 0
        else 25
    )
    requested_after = (query.get("after") or [None])[0]
    requested_before = (query.get("before") or [None])[0]
    ordered_fullnames = [pair[0] for pair in ordered]
    start = 0
    end = len(ordered)
    if requested_after is not None:
        if requested_after not in ordered_fullnames:
            return None
        start = ordered_fullnames.index(requested_after) + 1
    elif requested_before is not None:
        if requested_before not in ordered_fullnames:
            return None
        end = ordered_fullnames.index(requested_before)
        start = max(0, end - requested_limit)
    bounded = ordered[start : min(end, start + requested_limit)]
    bounded_end = start + len(bounded)
    result = dict(payload)
    data = dict(payload["data"])
    data.update(
        {
            "children": [pair[1] for pair in bounded],
            "dist": len(bounded),
            "after": (
                bounded[-1][0]
                if bounded and bounded_end < len(ordered)
                else None
            ),
            "before": (
                bounded[0][0]
                if bounded and start > 0
                else None
            ),
        }
    )
    result["data"] = data
    return result


def _multi_feed_scope_error(metadata: object, listing: object) -> str | None:
    """Reject a multireddit feed that escapes its exact community membership."""

    if not isinstance(metadata, dict) or not isinstance(metadata.get("data"), dict):
        return "Reddit returned invalid multireddit details."
    members = {
        str(subreddit.get("name") or subreddit.get("display_name")).casefold()
        for subreddit in metadata["data"].get("subreddits") or []
        if isinstance(subreddit, dict)
        and _nonempty_string(
            subreddit.get("name") or subreddit.get("display_name"),
            maximum=100,
        )
    }
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), dict):
        return "Reddit returned an invalid multireddit feed."
    children = listing["data"].get("children")
    if not isinstance(children, list):
        return "Reddit returned an invalid multireddit feed."
    for child in children:
        data = child.get("data") if isinstance(child, dict) else None
        subreddit = data.get("subreddit") if isinstance(data, dict) else None
        if (
            not isinstance(subreddit, str)
            or not subreddit
            or subreddit.casefold() not in members
        ):
            return (
                "Reddit returned an item outside the requested "
                "multireddit communities."
            )
    return None


async def _paginate_anonymous_moderators(
    first_payload: dict,
    *,
    subreddit: str,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None,
    deadline: float,
) -> dict:
    """Merge an anonymously readable roster without silently losing pages."""

    first_data = first_payload.get("data")
    if not isinstance(first_data, dict):
        return {"error": "Reddit moderator roster returned an invalid response."}
    first_children = first_data.get("children")
    if not isinstance(first_children, list):
        return {"error": "Reddit moderator roster returned an invalid response."}

    children = list(first_children)
    after = first_data.get("after")
    seen: set[str] = set()
    for _page in range(1, _MAX_MODERATOR_PAGES):
        if after is None:
            merged = dict(first_payload)
            merged_data = dict(first_data)
            merged_data["children"] = children
            merged_data["after"] = None
            merged["data"] = merged_data
            return {"data": merged}
        if (
            not isinstance(after, str)
            or not _MODERATOR_CURSOR.fullmatch(after)
            or after in seen
        ):
            return {
                "error": (
                    "Reddit moderator roster returned an invalid pagination "
                    "cursor."
                )
            }
        seen.add(after)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"error": "Reddit moderator roster request timed out."}
        page_url = (
            f"https://www.reddit.com/r/{quote(subreddit)}/"
            "about/moderators.json?"
            + urlencode(
                {
                    "limit": "500",
                    "raw_json": "1",
                    "after": after,
                }
            )
        )
        result = await fetch_reddit_json(
            page_url,
            session,
            queue,
            remaining,
            auth_required_on_403=True,
        )
        if "data" not in result:
            return result
        page_payload = result["data"]
        if not valid_moderator_roster(page_payload):
            return {
                "error": "Reddit moderator roster returned an invalid response."
            }
        page_data = page_payload["data"]
        page_children = page_data["children"]
        children.extend(page_children)
        after = page_data.get("after")

    if after is not None:
        return {
            "error": (
                "Reddit moderator roster exceeded the bounded pagination limit."
            )
        }
    merged = dict(first_payload)
    merged_data = dict(first_data)
    merged_data["children"] = children
    merged_data["after"] = None
    merged["data"] = merged_data
    return {"data": merged}


def _is_fixed_new_reddit_html_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return not (
        len(value) > 8192
        or not value.isascii()
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or character == "\\"
            for character in value
        )
        or parsed.scheme != "https"
        or (parsed.hostname or "").rstrip(".").casefold()
        != "www.reddit.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.fragment
    )


def _is_exact_wiki_pages_html_route(source_url: str, response_url: str) -> bool:
    """Pin the wiki OAuth boundary to one unchanged anonymous HTML route."""

    if not (
        _is_fixed_new_reddit_html_url(source_url)
        and _is_fixed_new_reddit_html_url(response_url)
    ):
        return False
    source = urlparse(source_url)
    response = urlparse(response_url)
    pattern = re.compile(
        r"/r/(?P<subreddit>[a-zA-Z0-9][a-zA-Z0-9_]{0,20})/"
        r"wiki/pages/\Z"
    )
    source_match = pattern.fullmatch(source.path)
    response_match = pattern.fullmatch(response.path)
    return (
        source_match is not None
        and response_match is not None
        and not source.query
        and not response.query
        and source_match.group("subreddit").casefold()
        == response_match.group("subreddit").casefold()
    )


async def _fetch_reddit_html(
    url: str,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None,
    deadline: float,
    *,
    auth_required_on_403: bool = False,
) -> dict:
    """Fetch one fixed-origin New Reddit HTML document under the shared budget."""

    if not _is_fixed_new_reddit_html_url(url):
        return {"error": "Invalid New Reddit HTML URL."}

    async def _get():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise wafer.WaferTimeout(url, 0)
        return await session.get(
            url,
            headers={
                "Accept": "text/html, application/xhtml+xml",
                "Referer": "https://www.reddit.com/",
            },
            timeout=remaining,
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"error": "Request timed out."}
    try:
        if queue is not None:
            response = await queue.enqueue(_get, _queue_timeout=remaining)
        else:
            await asyncio.wait_for(reddit_limiter.wait(), timeout=remaining)
            response = await _get()
    except (TimeoutError, wafer.WaferTimeout):
        return {"error": "Request timed out."}
    except wafer.ResponseTooLarge:
        return {"error": "Reddit response too large (exceeds 50MB limit)."}
    except Exception:
        return {"error": "New Reddit HTML fetch failed."}

    response_url = str(getattr(response, "url", url))
    if not _is_fixed_new_reddit_html_url(response_url):
        return {"error": "New Reddit HTML fetch left the fixed Reddit origin."}
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    if response.status_code == 429:
        applied_delay = 60.0 if retry_after is None else retry_after
        if queue is not None:
            queue.set_backoff(429, retry_after=retry_after)
        else:
            reddit_limiter.defer(applied_delay)
        return {
            "error": f"Rate limited by Reddit. Retry after {applied_delay:g}s."
        }
    if response.status_code == 403:
        payload = None
        try:
            payload = response.json()
        except Exception:
            pass
        if (
            auth_required_on_403
            and _is_exact_wiki_pages_html_route(url, response_url)
            and (payload is None or payload == {} or payload == [])
        ):
            # This exact anonymous boundary is account-gated by Reddit.
            # It is not a transport block and must not poison the shared queue.
            return {"auth_required": True}
        applied_delay = 300.0 if retry_after is None else retry_after
        if queue is not None:
            queue.set_backoff(403, retry_after=retry_after)
        else:
            reddit_limiter.defer(applied_delay)
    if response.status_code >= 400:
        return {"error": f"Reddit returned HTTP {response.status_code}."}
    content_type = str(response.headers.get("content-type") or "").lower()
    mime = content_type.partition(";")[0].strip()
    if mime not in {"text/html", "application/xhtml+xml"} and not mime.endswith(
        "+html"
    ):
        return {"error": "Reddit returned a non-HTML response."}
    return {"html": response.text}


async def _fetch_reddit_wiki_page_tree(
    subreddit: str,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None,
    deadline: float,
) -> dict:
    """Read the wiki page index from New Reddit's own anonymous GraphQL route.

    This is the logged-out path the wiki UI itself uses, so it needs no OAuth
    scope and no browser.  It only works with the CSRF token Reddit hands the
    same session, so the caller must already have loaded a New Reddit wiki
    document; a missing or malformed token is reported instead of retried
    without one.
    """

    if re.fullmatch(r"[A-Za-z0-9_]{1,21}", subreddit) is None:
        return {"error": "Invalid subreddit for the Reddit wiki page index."}
    csrf_token = session.get_cookie("csrf_token", "https://www.reddit.com/")
    if not isinstance(csrf_token, str) or _CSRF_TOKEN_RE.fullmatch(csrf_token) is None:
        return {
            "error": (
                "Reddit did not issue the CSRF token its anonymous wiki page "
                "index requires."
            )
        }

    async def _post():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise wafer.WaferTimeout(_SHREDDIT_GRAPHQL_URL, 0)
        return await session.post(
            _SHREDDIT_GRAPHQL_URL,
            json={
                "operation": _WIKI_PAGE_TREE_OPERATION,
                "variables": {
                    "subredditName": subreddit,
                    "wikiPageName": "index",
                },
                "csrf_token": csrf_token,
            },
            headers={
                "Accept": "*/*",
                "Origin": "https://www.reddit.com",
                "Referer": f"https://www.reddit.com/r/{quote(subreddit, safe='')}/wiki/pages/",
            },
            timeout=remaining,
            max_response_size=_MAX_WIKI_PAGE_TREE_BYTES,
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"error": "Request timed out."}
    try:
        if queue is not None:
            response = await queue.enqueue(_post, _queue_timeout=remaining)
        else:
            await asyncio.wait_for(reddit_limiter.wait(), timeout=remaining)
            response = await _post()
    except (TimeoutError, wafer.WaferTimeout):
        return {"error": "Request timed out."}
    except wafer.ResponseTooLarge:
        return {"error": "Reddit wiki page index response too large."}
    except Exception:
        return {"error": "Reddit anonymous wiki page index fetch failed."}

    response_url = str(getattr(response, "url", _SHREDDIT_GRAPHQL_URL))
    if response_url.split("?", 1)[0].split("#", 1)[0] != _SHREDDIT_GRAPHQL_URL:
        return {
            "error": "Reddit wiki page index left its fixed anonymous route."
        }
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    if response.status_code in {403, 429}:
        applied_delay = (
            (60.0 if response.status_code == 429 else 300.0)
            if retry_after is None
            else retry_after
        )
        if queue is not None:
            queue.set_backoff(response.status_code, retry_after=retry_after)
        else:
            reddit_limiter.defer(applied_delay)
    if response.status_code != 200:
        return {
            "error": f"Reddit wiki page index returned HTTP {response.status_code}."
        }
    mime = str(response.headers.get("content-type") or "").partition(";")[0].strip()
    if mime.lower() != "application/json":
        return {"error": "Reddit wiki page index returned a non-JSON response."}
    try:
        payload = response.json()
    except Exception:
        return {"error": "Reddit wiki page index returned unparsable JSON."}
    pages = parse_reddit_wiki_page_tree(payload, subreddit)
    if pages is None:
        return {
            "error": (
                "Reddit returned an invalid anonymous wiki page tree for "
                f"r/{subreddit}."
            )
        }
    return {"pages": pages}


def _bounded_related_detail(detail: object) -> str:
    text = re.sub(r"\s+", " ", str(detail)).strip() or "unknown error"
    return text if len(text) <= 300 else text[:297].rstrip() + "..."


def _add_related_notice(listing: dict, count: int, detail: object) -> None:
    if count <= 0:
        return
    notices = listing.setdefault("_enrichment_notices", [])
    if isinstance(notices, list):
        notices.append(
            {
                "count": count,
                "detail": _bounded_related_detail(detail),
            }
        )


async def _enrich_related_listing(
    listing: dict,
    *,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None,
    deadline: float,
) -> dict:
    """Replace partial cards with authoritative post JSON without reordering."""

    data = listing.get("data")
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list) or not children:
        return listing

    enriched = dict(listing)
    enriched_data = dict(data)
    enriched_children = list(children)
    enriched_data["children"] = enriched_children
    enriched["data"] = enriched_data

    for batch_start in range(0, len(children), 100):
        batch = children[batch_start : batch_start + 100]
        fullnames = [
            str(child["data"].get("name") or f"t3_{child['data']['id']}")
            for child in batch
        ]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _add_related_notice(
                enriched,
                len(children) - batch_start,
                "the overall Reddit request timed out before enrichment",
            )
            break
        info_url = "https://www.reddit.com/api/info.json?" + urlencode(
            {
                "raw_json": "1",
                "limit": str(len(batch)),
                "id": ",".join(fullnames),
            }
        )
        result = await fetch_reddit_json(
            info_url,
            session,
            queue,
            remaining,
        )
        if "error" in result:
            _add_related_notice(enriched, len(batch), result["error"])
            continue
        authoritative_payload = result["data"]
        if not _is_listing_payload(
            authoritative_payload,
            allowed_kinds=frozenset({"t3"}),
        ):
            _add_related_notice(
                enriched,
                len(batch),
                "Reddit returned an invalid related-post detail response",
            )
            continue
        authoritative: dict[str, dict] = {}
        for child in authoritative_payload["data"]["children"]:
            child_data = child["data"]
            fullname = str(
                child_data.get("name") or f"t3_{child_data['id']}"
            )
            if fullname in fullnames and fullname not in authoritative:
                authoritative[fullname] = child

        missing = 0
        for offset, fullname in enumerate(fullnames):
            replacement = authoritative.get(fullname)
            if replacement is None:
                missing += 1
                continue
            enriched_children[batch_start + offset] = replacement
        _add_related_notice(
            enriched,
            missing,
            "Reddit omitted the requested post details",
        )
    return enriched


def _collection_identity(route: RedditRoute) -> tuple[str, str] | None:
    """Return the fixed subreddit/UUID identity for an archived collection."""

    try:
        parsed = urlparse(route.canonical_url)
        port = parsed.port
    except ValueError:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if (
        route.kind != "collection"
        or route.subreddit is None
        or parsed.scheme != "https"
        or (parsed.hostname or "").rstrip(".").lower() != "www.reddit.com"
        or port not in (None, 443)
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or len(parts) != 4
        or parts[0].casefold() != "r"
        or parts[1].casefold() != route.subreddit.casefold()
        or parts[2].casefold() != "collection"
        or _COLLECTION_UUID.fullmatch(parts[3]) is None
    ):
        return None
    return route.subreddit, parts[3].lower()


def _valid_archived_collection_permalink(
    value: object,
    subreddit: str,
    collection_id: str,
) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").rstrip(".").lower()
        in {"www.reddit.com", "new.reddit.com"}
        and port in (None, 443)
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
        and len(parts) == 4
        and parts[0].casefold() == "r"
        and parts[1].casefold() == subreddit.casefold()
        and parts[2].casefold() == "collection"
        and parts[3].casefold() == collection_id
    )


def _parse_archived_collection(
    html: str,
    *,
    subreddit: str,
    collection_id: str,
    timestamp: str,
) -> dict | None:
    """Extract one exact collection model from archived New Reddit Redux."""

    if (
        not isinstance(html, str)
        or len(html) > _MAX_COLLECTION_ARCHIVE_HTML
        or _COLLECTION_UUID.fullmatch(collection_id) is None
        or _WAYBACK_TIMESTAMP.fullmatch(timestamp) is None
    ):
        return None
    if html.count(_REDUX_ASSIGNMENT) != 1:
        return None
    assignment_start = html.find(_REDUX_ASSIGNMENT)
    opening_start = html.rfind("<script", 0, assignment_start)
    if opening_start < 0:
        return None
    opening_end = html.find(">", opening_start, assignment_start + 1)
    if (
        opening_end + 1 != assignment_start
        or _SCRIPT_OPEN.fullmatch(html[opening_start : opening_end + 1])
        is None
    ):
        return None
    closing_start = html.find("</script", assignment_start)
    if closing_start < 0:
        return None
    closing_end = html.find(">", closing_start, closing_start + 32)
    if (
        closing_end < 0
        or _SCRIPT_CLOSE.fullmatch(html[closing_start : closing_end + 1])
        is None
    ):
        return None
    serialized = html[
        assignment_start + len(_REDUX_ASSIGNMENT) : closing_start
    ].strip()
    if serialized.endswith(";"):
        serialized = serialized[:-1].rstrip()
    try:
        redux = json.loads(serialized)
    except (RecursionError, TypeError, ValueError):
        return None
    if not isinstance(redux, dict):
        return None
    collection_state = redux.get("postCollection")
    models = (
        collection_state.get("models")
        if isinstance(collection_state, dict)
        else None
    )
    model = models.get(collection_id) if isinstance(models, dict) else None
    if not isinstance(model, dict) or model.get("id") != collection_id:
        return None

    title = model.get("title")
    description = model.get("description")
    post_ids = model.get("postIds")
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title) > 1000
        or not isinstance(description, str)
        or len(description) > 100_000
        or not isinstance(post_ids, list)
        or not 1 <= len(post_ids) <= 1000
        or not all(
            isinstance(post_id, str)
            and re.fullmatch(r"t3_[A-Za-z0-9]{2,16}", post_id) is not None
            for post_id in post_ids
        )
        or len(set(post_ids)) != len(post_ids)
        or not _valid_archived_collection_permalink(
            model.get("permalink"),
            subreddit,
            collection_id,
        )
    ):
        return None

    primary_post_id = model.get("primaryPostId")
    if primary_post_id is not None and primary_post_id not in post_ids:
        return None
    subreddit_id = model.get("subredditId")
    if not (
        isinstance(subreddit_id, str)
        and re.fullmatch(r"t5_[A-Za-z0-9]{2,16}", subreddit_id)
    ):
        return None

    return {
        "title": title.strip(),
        "description": description.strip(),
        "link_ids": post_ids,
        "_fetchaller_reddit_provenance": "wayback",
        "_fetchaller_reddit_archive_timestamp": timestamp,
    }


def _parse_collection_cdx(
    payload: object,
    *,
    subreddit: str,
    collection_id: str,
) -> list[tuple[str, str]]:
    """Validate CDX rows and return newest fixed Reddit originals first."""

    if not (
        isinstance(payload, list)
        and payload
        and isinstance(payload[0], list)
        and all(isinstance(field, str) for field in payload[0])
    ):
        return []
    required = ("timestamp", "original", "statuscode", "mimetype")
    header = payload[0]
    if header != list(required):
        return []
    indexes = {field: header.index(field) for field in required}
    candidates: list[tuple[str, str]] = []
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) != len(header):
            continue
        timestamp = row[indexes["timestamp"]]
        original = row[indexes["original"]]
        status = row[indexes["statuscode"]]
        mime = row[indexes["mimetype"]]
        if (
            not isinstance(timestamp, str)
            or _WAYBACK_TIMESTAMP.fullmatch(timestamp) is None
            or timestamp > "20240630235959"
            or status != "200"
            or mime not in {"text/html", "application/xhtml+xml"}
            or not _valid_archived_collection_permalink(
                original,
                subreddit,
                collection_id,
            )
        ):
            continue
        candidates.append((timestamp, original))
    return sorted(set(candidates), reverse=True)[
        :_MAX_COLLECTION_ARCHIVE_CANDIDATES
    ]


async def _fetch_wayback_collection(
    route: RedditRoute,
    *,
    session: wafer.AsyncSession,
    deadline: float,
) -> dict:
    """Recover Reddit-deleted collection metadata from an exact archive."""

    identity = _collection_identity(route)
    if identity is None:
        return {"error": "collection URL does not contain a valid UUID"}
    subreddit, collection_id = identity
    archived_path = (
        f"www.reddit.com/r/{subreddit}/collection/{collection_id}"
    )
    cdx_url = (
        f"{_WAYBACK_ORIGIN}/cdx/search/cdx?"
        + urlencode(
            [
                ("url", archived_path),
                ("output", "json"),
                ("fl", "timestamp,original,statuscode,mimetype"),
                ("filter", "statuscode:200"),
                ("filter", "mimetype:text/html"),
                ("collapse", "digest"),
                ("from", "2019"),
                ("to", "2024"),
                ("limit", "-10"),
            ]
        )
    )

    async def _get(url: str, accept: str):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise wafer.WaferTimeout(url, 0)
        return await session.get(
            url,
            headers={
                "Accept": accept,
                "Referer": "https://www.reddit.com/",
            },
            timeout=remaining,
        )

    try:
        cdx_response = await _get(cdx_url, "application/json")
    except (TimeoutError, wafer.WaferTimeout):
        return {"error": "archive lookup timed out"}
    except wafer.ResponseTooLarge:
        return {"error": "archive index exceeded the response limit"}
    except Exception:
        return {"error": "archive lookup failed"}
    if (
        str(getattr(cdx_response, "url", cdx_url)) != cdx_url
        or cdx_response.status_code != 200
    ):
        return {
            "error": (
                "archive index returned "
                f"HTTP {cdx_response.status_code}"
            )
        }
    try:
        cdx_payload = cdx_response.json()
    except Exception:
        return {"error": "archive index returned invalid JSON"}
    candidates = _parse_collection_cdx(
        cdx_payload,
        subreddit=subreddit,
        collection_id=collection_id,
    )
    if not candidates:
        return {"error": "no exact archived New Reddit snapshot was found"}

    for timestamp, original in candidates:
        snapshot_url = (
            f"{_WAYBACK_ORIGIN}/web/{timestamp}id_/{original}"
        )
        try:
            response = await _get(
                snapshot_url,
                "text/html, application/xhtml+xml",
            )
        except (TimeoutError, wafer.WaferTimeout):
            return {"error": "archived New Reddit snapshot timed out"}
        except wafer.ResponseTooLarge:
            return {"error": "archived New Reddit snapshot was too large"}
        except Exception:
            continue
        if (
            str(getattr(response, "url", snapshot_url)) != snapshot_url
            or response.status_code != 200
        ):
            continue
        content_type = str(
            response.headers.get("content-type") or ""
        ).lower()
        mime = content_type.partition(";")[0].strip()
        if mime not in {
            "text/html",
            "application/xhtml+xml",
        } and not mime.endswith("+html"):
            continue
        archive_html = response.text
        if len(archive_html) > _MAX_COLLECTION_ARCHIVE_HTML:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"error": "archived New Reddit snapshot timed out"}
        try:
            collection = await asyncio.wait_for(
                asyncio.to_thread(
                    _parse_archived_collection,
                    archive_html,
                    subreddit=subreddit,
                    collection_id=collection_id,
                    timestamp=timestamp,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            return {"error": "archived New Reddit snapshot parsing timed out"}
        if collection is not None:
            return {"data": collection}
    return {"error": "archived New Reddit snapshots lacked exact collection data"}


def _gilded_archive_identity(
    route: RedditRoute,
) -> tuple[list[str], bool, str | None] | None:
    """Return exact archive paths and whether the route requests comments only."""

    if (
        route.kind not in {
            "comment_listing",
            "multi_profile",
            "user_listing",
        }
        or "gilded" not in (route.label or "")
    ):
        return None
    try:
        parsed = urlparse(route.canonical_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").rstrip(".").casefold() != "www.reddit.com"
        or port not in (None, 443)
        or parsed.params
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(name not in {"after", "before", "count", "limit"} for name in query):
        return None
    if any(len(values) != 1 for values in query.values()):
        return None
    for name in ("after", "before"):
        value = (query.get(name) or [""])[0]
        if value and _THING_FULLNAME.fullmatch(value) is None:
            return None
    for name in ("count", "limit"):
        value = (query.get(name) or [""])[0]
        if value and (not value.isdigit() or int(value) > 1000):
            return None

    parts = [part for part in parsed.path.split("/") if part]
    comments_only = False
    username: str | None = None
    paths: list[str]
    if [part.casefold() for part in parts] == ["gilded"]:
        paths = ["www.reddit.com/gilded/"]
    elif [part.casefold() for part in parts] == ["comments", "gilded"]:
        comments_only = True
        paths = [
            "www.reddit.com/comments/gilded",
        ]
    elif (
        len(parts) == 3
        and parts[0].casefold() == "r"
        and route.subreddit is not None
        and parts[1].casefold() == route.subreddit.casefold()
        and parts[2].casefold() == "gilded"
    ):
        paths = [f"www.reddit.com/r/{route.subreddit}/gilded/"]
    elif (
        len(parts) == 4
        and parts[0].casefold() == "r"
        and route.subreddit is not None
        and parts[1].casefold() == route.subreddit.casefold()
        and [part.casefold() for part in parts[2:]]
        == ["comments", "gilded"]
    ):
        comments_only = True
        paths = [
            f"www.reddit.com/r/{route.subreddit}/comments/gilded/",
            f"www.reddit.com/r/{route.subreddit}/gilded/",
        ]
    elif (
        len(parts) == 3
        and parts[0].casefold() in {"user", "u"}
        and route.username is not None
        and parts[1].casefold() == route.username.casefold()
        and parts[2].casefold() == "gilded"
    ):
        username = route.username
        paths = [
            f"www.reddit.com/user/{route.username}/gilded/",
        ]
    elif (
        len(parts) in {5, 6}
        and parts[0].casefold() in {"user", "u"}
        and route.username is not None
        and parts[1].casefold() == route.username.casefold()
        and parts[2].casefold() == "m"
        and _DIRECTORY_USERNAME.fullmatch(parts[3])
        and [part.casefold() for part in parts[4:]]
        in (["gilded"], ["comments", "gilded"])
    ):
        comments_only = len(parts) == 6
        paths = [
            "www.reddit.com/user/"
            f"{route.username}/m/{parts[3]}/"
            + ("comments/gilded/" if comments_only else "gilded/")
        ]
    else:
        return None

    return paths, comments_only, username


def _valid_archived_gilded_original(
    value: object,
    archive_path: str,
) -> bool:
    if not isinstance(value, str):
        return False
    expected = urlparse("https://" + archive_path)
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    valid_origin = (
        (parsed.scheme == "https" and port in (None, 443))
        or (parsed.scheme == "http" and port in (None, 80))
    )
    return (
        valid_origin
        and (parsed.hostname or "").rstrip(".").casefold() == "www.reddit.com"
        and parsed.username is None
        and parsed.password is None
        and not parsed.params
        and not parsed.fragment
        and parsed.path.rstrip("/").casefold()
        == expected.path.rstrip("/").casefold()
        and parse_qs(parsed.query, keep_blank_values=True)
        == parse_qs(expected.query, keep_blank_values=True)
    )


def _parse_gilded_cdx(
    payload: object,
    *,
    archive_path: str,
) -> list[tuple[str, str]]:
    if not (
        isinstance(payload, list)
        and payload
        and payload[0] == [
            "timestamp",
            "original",
            "statuscode",
            "mimetype",
        ]
    ):
        return []
    candidates: list[tuple[str, str]] = []
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) != 4:
            continue
        timestamp, original, status, mime = row
        if (
            isinstance(timestamp, str)
            and _WAYBACK_TIMESTAMP.fullmatch(timestamp)
            and timestamp <= "20240630235959"
            and status == "200"
            and mime in {"text/html", "application/xhtml+xml"}
            and _valid_archived_gilded_original(original, archive_path)
        ):
            candidates.append((timestamp, original))
    return sorted(set(candidates), reverse=True)[
        :_MAX_GILDED_ARCHIVE_CANDIDATES
    ]


def _parse_archived_gilded(
    html: str,
    *,
    subreddit: str | None,
    username: str | None,
    comments_only: bool,
) -> tuple[list[str], dict[str, int | None], str | None] | None:
    """Extract only explicitly gilded Thing IDs from one archived listing."""

    if not isinstance(html, str) or len(html) > _MAX_GILDED_ARCHIVE_HTML:
        return None
    soup = BeautifulSoup(html, "lxml")
    fullnames: list[str] = []
    gilding_counts: dict[str, int | None] = {}
    seen: set[str] = set()
    for thing in soup.select(".thing[data-fullname]"):
        fullname = str(thing.get("data-fullname") or "")
        if (
            _THING_FULLNAME.fullmatch(fullname) is None
            or fullname in seen
            or (comments_only and not fullname.startswith("t1_"))
        ):
            continue
        classes = {
            str(value).casefold() for value in (thing.get("class") or [])
        }
        raw_gildings = str(thing.get("data-gildings") or "")
        explicitly_gilded = (
            "gilded" in classes
            or (raw_gildings.isdigit() and int(raw_gildings) > 0)
            or thing.select_one(".awardings-bar [data-award-id]") is not None
        )
        if not explicitly_gilded:
            continue
        item_subreddit = str(thing.get("data-subreddit") or "")
        if subreddit is not None and item_subreddit.casefold() != subreddit.casefold():
            continue
        item_author = str(thing.get("data-author") or "")
        if username is not None and item_author.casefold() != username.casefold():
            continue
        seen.add(fullname)
        fullnames.append(fullname)
        award_ids = {
            str(award.get("data-award-id"))
            for award in thing.select(".awardings-bar [data-award-id]")
            if award.get("data-award-id")
        }
        exact_count = max(
            int(raw_gildings) if raw_gildings.isdigit() else 0,
            len(award_ids),
        )
        gilding_counts[fullname] = exact_count or None
        if len(fullnames) > 100:
            return None
    if not fullnames:
        return None

    next_href: str | None = None
    next_link = soup.select_one(".next-button a[href]")
    if next_link is not None:
        href = str(next_link.get("href") or "")
        try:
            parsed = urlparse(href)
            values = parse_qs(parsed.query).get("after") or []
        except ValueError:
            values = []
        if len(values) == 1 and _THING_FULLNAME.fullmatch(values[0]):
            next_href = href
    return fullnames, gilding_counts, next_href


def _valid_archived_gilded_next_original(
    value: str,
    *,
    archive_path: str,
) -> bool:
    """Pin an archived next link to the same exact gilded listing."""

    expected = urlparse("https://" + archive_path)
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    after_values = query.get("after") or []
    count_values = query.get("count") or []
    return (
        (
            (parsed.scheme == "https" and port in (None, 443))
            or (parsed.scheme == "http" and port in (None, 80))
        )
        and (parsed.hostname or "").rstrip(".").casefold()
        == "www.reddit.com"
        and parsed.username is None
        and parsed.password is None
        and not parsed.params
        and not parsed.fragment
        and parsed.path.rstrip("/").casefold()
        == expected.path.rstrip("/").casefold()
        and set(query).issubset({"after", "count"})
        and all(len(values) == 1 for values in query.values())
        and len(after_values) == 1
        and _THING_FULLNAME.fullmatch(after_values[0]) is not None
        and (
            not count_values
            or (
                count_values[0].isdigit()
                and 0 < int(count_values[0]) <= 1000
            )
        )
    )


async def _fetch_wayback_gilded(
    route: RedditRoute,
    *,
    session: wafer.AsyncSession,
    queue: RedditRequestQueue | None,
    deadline: float,
) -> dict:
    """Recover retired gilded ordering, then hydrate every ID from Reddit."""

    identity = _gilded_archive_identity(route)
    if identity is None:
        return {"error": "Invalid Reddit gilded listing route."}
    archive_paths, comments_only, username = identity

    async def _get(url: str, accept: str):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise wafer.WaferTimeout(url, 0)
        return await session.get(
            url,
            headers={
                "Accept": accept,
                "Referer": "https://www.reddit.com/",
            },
            timeout=remaining,
        )

    for archive_path in archive_paths:
        pinned_candidates = [
            candidate
            for candidate in _PINNED_GILDED_ARCHIVE_CANDIDATES.get(
                archive_path,
                (),
            )
            if _valid_archived_gilded_original(candidate[1], archive_path)
        ]
        if pinned_candidates:
            candidates = pinned_candidates
        else:
            candidates = []
        archive_path_lower = archive_path.casefold().rstrip("/")
        if archive_path_lower == "www.reddit.com/comments/gilded":
            archive_from = archive_to = "201505"
        elif archive_path_lower == "www.reddit.com/gilded":
            archive_from = archive_to = "201705"
        else:
            archive_from, archive_to = "2018", "2024"
        if not candidates:
            cdx_url = (
                f"{_WAYBACK_ORIGIN}/cdx/search/cdx?"
                + urlencode(
                    [
                        ("url", archive_path),
                        ("output", "json"),
                        ("fl", "timestamp,original,statuscode,mimetype"),
                        ("filter", "statuscode:200"),
                        ("filter", "mimetype:text/html"),
                        ("collapse", "digest"),
                        ("from", archive_from),
                        ("to", archive_to),
                        ("limit", "-10"),
                    ]
                )
            )
            try:
                cdx_response = await _get(cdx_url, "application/json")
            except (TimeoutError, wafer.WaferTimeout):
                return {"error": "Reddit gilded archive lookup timed out."}
            except wafer.ResponseTooLarge:
                return {"error": "Reddit gilded archive index was too large."}
            except Exception:
                continue
            if (
                str(getattr(cdx_response, "url", cdx_url)) != cdx_url
                or cdx_response.status_code != 200
            ):
                continue
            try:
                cdx_payload = cdx_response.json()
            except Exception:
                continue
            candidates = _parse_gilded_cdx(
                cdx_payload,
                archive_path=archive_path,
            )
        for timestamp, original in candidates:
            canonical_query = parse_qs(
                urlparse(route.canonical_url).query,
                keep_blank_values=True,
            )
            requested_after = (canonical_query.get("after") or [None])[0]
            requested_before = (canonical_query.get("before") or [None])[0]
            requested_limit = 100
            if route.requests:
                values = parse_qs(urlparse(route.requests[0]).query).get(
                    "limit"
                ) or []
                if len(values) == 1 and values[0].isdigit():
                    requested_limit = min(100, max(1, int(values[0])))
            fullnames: list[str] = []
            archived_gildings: dict[str, int | None] = {}
            page_original = original
            archive_invalid = False
            for _page_index in range(_MAX_GILDED_ARCHIVE_PAGES):
                snapshot_url = (
                    f"{_WAYBACK_ORIGIN}/web/{timestamp}id_/{page_original}"
                )
                try:
                    snapshot = await _get(
                        snapshot_url,
                        "text/html, application/xhtml+xml",
                    )
                except (TimeoutError, wafer.WaferTimeout):
                    return {
                        "error": "Archived Reddit gilded listing timed out."
                    }
                except wafer.ResponseTooLarge:
                    return {
                        "error": "Archived Reddit gilded listing was too large."
                    }
                except Exception:
                    archive_invalid = _page_index == 0
                    break
                if (
                    str(getattr(snapshot, "url", snapshot_url)) != snapshot_url
                    or snapshot.status_code != 200
                ):
                    archive_invalid = _page_index == 0
                    break
                parsed = _parse_archived_gilded(
                    snapshot.text,
                    subreddit=route.subreddit,
                    username=username,
                    comments_only=comments_only,
                )
                if parsed is None:
                    archive_invalid = _page_index == 0
                    break
                page_fullnames, page_gildings, next_href = parsed
                if set(page_fullnames).intersection(fullnames):
                    archive_invalid = _page_index == 0
                    break
                fullnames.extend(page_fullnames)
                archived_gildings.update(page_gildings)

                if requested_after is not None and requested_after in fullnames:
                    available_after = len(fullnames) - (
                        fullnames.index(requested_after) + 1
                    )
                    enough = available_after > requested_limit
                elif requested_before is not None:
                    enough = requested_before in fullnames
                else:
                    enough = len(fullnames) > requested_limit
                if enough or next_href is None:
                    break

                next_original = urljoin(page_original, next_href)
                if not _valid_archived_gilded_next_original(
                    next_original,
                    archive_path=archive_path,
                ):
                    break
                page_original = next_original
            else:
                archive_invalid = True
            if archive_invalid:
                continue
            start = 0
            end = len(fullnames)
            if requested_after is not None:
                if requested_after not in fullnames:
                    continue
                start = fullnames.index(requested_after) + 1
            elif requested_before is not None:
                if requested_before not in fullnames:
                    continue
                end = fullnames.index(requested_before)
                start = max(0, end - requested_limit)
            bounded = fullnames[
                start : min(end, start + requested_limit)
            ]
            if not bounded:
                continue
            bounded_end = start + len(bounded)
            after = (
                bounded[-1]
                if bounded_end < len(fullnames)
                else None
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "error": "Reddit gilded current hydration timed out."
                }
            info_url = "https://www.reddit.com/api/info.json?" + urlencode(
                {
                    "raw_json": "1",
                    "limit": str(len(bounded)),
                    "id": ",".join(bounded),
                }
            )
            result = await fetch_reddit_json(
                info_url,
                session,
                queue,
                remaining,
            )
            if "error" in result:
                return {
                    "error": (
                        "Reddit gilded current hydration failed "
                        f"({_bounded_related_detail(result['error'])})"
                    )
                }
            listing = result.get("data")
            if not _is_listing_payload(
                listing,
                allowed_kinds=frozenset({"t1", "t3"}),
            ):
                continue
            children_by_fullname: dict[str, dict] = {}
            for child in listing["data"]["children"]:
                child_data = child["data"]
                fullname = child_data.get("name")
                if not isinstance(fullname, str):
                    fullname = f"{child['kind']}_{child_data['id']}"
                if fullname in children_by_fullname:
                    children_by_fullname = {}
                    break
                children_by_fullname[fullname] = child
            if set(children_by_fullname) != set(bounded):
                continue
            ordered_children = [
                children_by_fullname[fullname] for fullname in bounded
            ]
            if comments_only and any(
                child.get("kind") != "t1" for child in ordered_children
            ):
                continue
            archived_children: list[dict] = []
            for fullname, child in zip(bounded, ordered_children):
                archived_child = dict(child)
                archived_data = dict(child["data"])
                archived_data["_fetchaller_reddit_archived_gilded"] = True
                archived_count = archived_gildings[fullname]
                if archived_count is not None:
                    archived_data[
                        "_fetchaller_reddit_archived_gilding_count"
                    ] = archived_count
                archived_child["data"] = archived_data
                archived_children.append(archived_child)
            ordered = dict(listing)
            ordered_data = dict(listing["data"])
            ordered_data.update(
                {
                    "children": archived_children,
                    "dist": len(archived_children),
                    "after": after,
                    "before": (
                        bounded[0]
                        if start > 0
                        else None
                    ),
                }
            )
            ordered["_fetchaller_reddit_provenance"] = "wayback"
            ordered["_fetchaller_reddit_archive_timestamp"] = timestamp
            ordered["data"] = ordered_data
            return {"data": ordered}
    return {
        "error": (
            "No exact archived Reddit gilded listing with complete current "
            "hydration was available."
        )
    }


async def _fetch_archived_gold_directory(
    route: RedditRoute,
    *,
    session: wafer.AsyncSession,
    deadline: float,
) -> dict:
    """Recover the exact final public state of the retired gold directory."""

    parsed = urlparse(route.canonical_url)
    if (
        route.kind != "subreddit_directory"
        or route.label != "gold"
        or parsed.scheme != "https"
        or (parsed.hostname or "").rstrip(".").casefold() != "www.reddit.com"
        or parsed.port not in (None, 443)
        or parsed.path.rstrip("/").casefold() != "/subreddits/gold"
    ):
        return {"error": "Invalid Reddit gold-directory route."}
    snapshot_url = (
        f"{_WAYBACK_ORIGIN}/web/{_GOLD_DIRECTORY_ARCHIVE_TIMESTAMP}id_/"
        f"{_GOLD_DIRECTORY_ARCHIVE_ORIGINAL}"
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"error": "Archived Reddit gold directory timed out."}
    try:
        response = await session.get(
            snapshot_url,
            headers={
                "Accept": "text/html, application/xhtml+xml",
                "Referer": "https://www.reddit.com/",
            },
            timeout=remaining,
        )
    except (TimeoutError, wafer.WaferTimeout):
        return {"error": "Archived Reddit gold directory timed out."}
    except wafer.ResponseTooLarge:
        return {"error": "Archived Reddit gold directory was too large."}
    except Exception:
        return {"error": "Archived Reddit gold directory lookup failed."}
    if (
        str(getattr(response, "url", snapshot_url)) != snapshot_url
        or response.status_code != 200
        or len(response.text) > _MAX_GILDED_ARCHIVE_HTML
    ):
        return {"error": "Archived Reddit gold directory was unavailable."}
    content_type = str(response.headers.get("content-type") or "").lower()
    mime = content_type.partition(";")[0].strip()
    if mime not in {"text/html", "application/xhtml+xml"}:
        return {"error": "Archived Reddit gold directory was invalid."}
    soup = BeautifulSoup(response.text, "lxml")
    canonical = soup.select_one('link[rel~="canonical"][href]')
    canonical_href = str(canonical.get("href") or "") if canonical else ""
    site_table = soup.select_one("#siteTable.sitetable.linklisting")
    if (
        canonical_href.rstrip("/").casefold()
        != _GOLD_DIRECTORY_ARCHIVE_ORIGINAL.rstrip("/").casefold()
        or site_table is None
        or site_table.select_one("#noresults.error") is None
        or site_table.select_one(".thing[data-fullname]") is not None
    ):
        return {
            "error": (
                "Archived Reddit gold directory lacked an exact empty state."
            )
        }
    return {
        "data": {
            "kind": "Listing",
            "data": {
                "children": [],
                "dist": 0,
                "after": None,
                "before": None,
            },
            "_fetchaller_reddit_provenance": "wayback_gold_directory",
            "_fetchaller_reddit_archive_timestamp": (
                _GOLD_DIRECTORY_ARCHIVE_TIMESTAMP
            ),
        }
    }


async def fetch_mapped_reddit(
    route: RedditRoute,
    *,
    max_tokens: int,
    timeout: float,
    chars_per_token: int = 4,
    config: Config | None = None,
    queue: RedditRequestQueue | None = None,
    browser_solver=None,
) -> dict:
    """Fetch every JSON leg for a mapped URL under one overall deadline."""

    deadline = time.monotonic() + timeout
    payloads: list[object] = []
    if route.kind == "user_listing" and route.label == "gilded given":
        # Reddit's final published server source explicitly returned 403 for
        # show=given unless the requester was that same logged-in account (or
        # an admin): reddit-archive/reddit 753b174, listingcontroller.py
        # GET_listing. This is an account-private state, not a retired public
        # listing that can be reconstructed from somebody else's archive.
        return {
            "error": (
                "Reddit account-private gildings given are not publicly "
                "readable."
            )
        }
    session = await _get_session(browser_solver)

    if route.kind == "subreddit_directory" and route.label == "gold":
        archived = await _fetch_archived_gold_directory(
            route,
            session=session,
            deadline=deadline,
        )
        if "error" in archived:
            return archived
        schema_error = _payload_schema_error(route, 0, archived["data"])
        if schema_error is not None:
            return {"error": schema_error}
        return {
            "content": render_reddit_route(
                route,
                [archived["data"]],
                max_tokens=max_tokens,
                chars_per_token=chars_per_token,
            ),
            "content_type": "markdown",
            "url": route.canonical_url,
        }

    if (
        route.kind == "multi_profile"
        and "gilded" in (route.label or "")
        and len(route.requests) == 2
    ):
        remaining = deadline - time.monotonic()
        metadata = await fetch_reddit_json(
            route.requests[0],
            session,
            queue,
            remaining,
        )
        if "error" in metadata:
            return metadata
        schema_error = _payload_schema_error(route, 0, metadata["data"])
        if schema_error is not None:
            return {"error": schema_error}
        archived = await _fetch_wayback_gilded(
            route,
            session=session,
            queue=queue,
            deadline=deadline,
        )
        if "error" in archived:
            return archived
        schema_error = _payload_schema_error(route, 1, archived["data"])
        if schema_error is not None:
            return {"error": schema_error}
        scope_error = _multi_feed_scope_error(metadata["data"], archived["data"])
        if scope_error is not None:
            return {"error": scope_error}
        content = render_reddit_route(
            route,
            [metadata["data"], archived["data"]],
            max_tokens=max_tokens,
            chars_per_token=chars_per_token,
        )
        return {
            "content": content,
            "content_type": "markdown",
            "url": route.canonical_url,
        }

    if (
        route.kind in {"comment_listing", "user_listing"}
        and "gilded" in (route.label or "")
    ):
        result = await _fetch_wayback_gilded(
            route,
            session=session,
            queue=queue,
            deadline=deadline,
        )
        if "error" in result:
            return result
        schema_error = _payload_schema_error(route, 0, result["data"])
        if schema_error is not None:
            return {"error": schema_error}
        content = render_reddit_route(
            route,
            [result["data"]],
            max_tokens=max_tokens,
            chars_per_token=chars_per_token,
        )
        return {
            "content": content,
            "content_type": "markdown",
            "url": route.canonical_url,
        }

    if route.kind == "wiki_pages":
        if route.subreddit is None or len(route.requests) != 1:
            return {"error": "Invalid New Reddit wiki-page tree route."}
        result = await _fetch_reddit_html(
            route.requests[0],
            session,
            queue,
            deadline,
            auth_required_on_403=True,
        )
        if "error" in result:
            return result
        pages = (
            None
            if result.get("auth_required")
            else parse_reddit_wiki_pages_html(
                result["html"],
                route.subreddit,
            )
        )
        provenance = "ssr"
        anonymous_detail = "the anonymous page tree was unavailable"
        if pages is None:
            # New Reddit no longer server-renders the page tree on every wiki
            # document, but it still answers the same logged-out GraphQL route
            # the wiki UI uses. The HTML fetch above seeded the CSRF cookie it
            # needs, so this stays anonymous.
            tree = await _fetch_reddit_wiki_page_tree(
                route.subreddit,
                session,
                queue,
                deadline,
            )
            if "pages" in tree:
                pages = tree["pages"]
                provenance = "graphql"
            else:
                anonymous_detail = _bounded_related_detail(
                    tree.get("error", "the anonymous page tree was unavailable")
                )
        if pages is not None:
            payload = {
                "kind": "wikipagelisting",
                "data": pages,
                "_fetchaller_reddit_provenance": provenance,
            }
        else:
            # Anonymous-only by design: the SSR page tree and the GraphQL route
            # above are the whole contract, so an unavailable tree is an honest
            # failure rather than a prompt for credentials fetchaller never has.
            return {
                "error": (
                    "Reddit did not return a public wiki-page tree: "
                    f"{anonymous_detail}"
                )
            }
        schema_error = _payload_schema_error(route, 0, payload)
        if schema_error is not None:
            return {"error": schema_error}
        content = render_reddit_route(
            route,
            [payload],
            max_tokens=max_tokens,
            chars_per_token=chars_per_token,
        )
        return {
            "content": content,
            "content_type": "markdown",
            "url": route.canonical_url,
        }

    async def _collection_archive_fallback(current_detail: object) -> dict:
        archive = await _fetch_wayback_collection(
            route,
            session=session,
            deadline=deadline,
        )
        if "data" in archive:
            return archive
        current = _bounded_related_detail(current_detail)
        archived = _bounded_related_detail(
            archive.get("error", "archive recovery failed")
        )
        return {
            "error": (
                "Reddit collection is unavailable: current metadata "
                f"failed ({current}); archive recovery failed ({archived})"
            )
        }

    for index, request_url in enumerate(route.requests):
        global_comments_transfer = _global_comments_transfer(route, request_url)
        requested_global_comments_limit = None
        if global_comments_transfer is not None:
            request_url, requested_global_comments_limit = global_comments_transfer
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if route.kind in {"multi_profile", "user_profile", "live"} and payloads:
                payloads.extend(
                    {"_fetch_error": f"Request timed out ({timeout:g}s limit)"}
                    for _ in route.requests[index:]
                )
                break
            return {"error": f"Request timed out ({timeout:g}s limit)"}
        result = await fetch_reddit_json(
            request_url,
            session,
            queue,
            remaining,
            auth_required_on_403=route.kind == "moderators",
            account_private_on_403=(
                route.kind == "user_listing"
                and route.label in {"upvoted", "downvoted"}
            ),
        )
        if result.get("auth_required"):
            return {"error": _MODERATOR_AUTH_REQUIRED}
        if "error" in result:
            if route.kind in {"multi_profile", "user_profile", "live"}:
                payloads.append({"_fetch_error": result["error"]})
                continue
            if route.kind == "collection" and index == 0:
                result = await _collection_archive_fallback(result["error"])
                if "error" in result:
                    return result
            else:
                return result
        if (
            isinstance(result["data"], dict)
            and result["data"].get("_reddit_content_state")
        ):
            payloads.append(result["data"])
            continue
        schema_error = _payload_schema_error(
            route,
            index,
            result["data"],
        )
        if schema_error is not None:
            if (
                route.kind == "morechildren"
                and isinstance(result["data"], dict)
                and result["data"].get("success") is False
            ):
                return {
                    "error": "Reddit reported that comment expansion failed."
                }
            if route.kind in {"multi_profile", "user_profile", "live"}:
                payloads.append({"_fetch_error": schema_error})
                continue
            if route.kind == "collection":
                result = await _collection_archive_fallback(schema_error)
                if "error" in result:
                    return result
                schema_error = _payload_schema_error(
                    route,
                    index,
                    result["data"],
                )
                if schema_error is not None:
                    return {
                        "error": (
                            "Reddit collection is unavailable: archive "
                            "metadata returned an invalid link_ids value."
                        )
                    }
            else:
                return {"error": schema_error}
        if (
            route.kind == "moderators"
            and not (
                isinstance(result["data"], dict)
                and result["data"].get("_reddit_content_state")
            )
            and not valid_moderator_roster(result["data"])
        ):
            return {
                "error": (
                    "Reddit moderator roster returned an invalid response."
                )
            }
        if (
            (route.label or "").endswith("randomrising")
            and (
                route.kind != "multi_profile"
                or index == 1
            )
        ):
            sliced_randomrising = _slice_randomrising_page(
                result["data"],
                route,
            )
            if sliced_randomrising is None:
                return {
                    "error": (
                        "Reddit returned an invalid random-rising page."
                    )
                }
            result = dict(result)
            result["data"] = sliced_randomrising
        if requested_global_comments_limit is not None:
            sliced = _slice_global_comments_page(
                result["data"],
                requested_global_comments_limit,
            )
            if sliced is None:
                return {
                    "error": (
                        "Reddit returned an invalid global comment page."
                    )
                }
            result = dict(result)
            result["data"] = sliced
        result = dict(result)
        result["data"] = _restore_reverse_listing_after(
            result["data"],
            request_url,
        )
        if route.kind == "moderators" and route.subreddit is not None:
            result = await _paginate_anonymous_moderators(
                result["data"],
                subreddit=route.subreddit,
                session=session,
                queue=queue,
                deadline=deadline,
            )
            if result.get("auth_required"):
                return {"error": _MODERATOR_AUTH_REQUIRED}
            if "error" in result:
                return result
            if not valid_moderator_roster(result["data"]):
                return {
                    "error": (
                        "Reddit moderator roster returned an invalid response."
                    )
                }
        payloads.append(result["data"])

        # A suspended account serves its `about` payload and then 403s every
        # other profile source. Those 403s read as challenges, so the remaining
        # legs burn the whole deadline and the caller gets a timeout instead of
        # the one fact Reddit did return. Stop here and say why the rest are
        # missing rather than spending the budget to learn nothing.
        if (
            route.kind == "user_profile"
            and index == 0
            and isinstance(result["data"], dict)
            # Strictly ``is True``: nothing validates this field's type, and any
            # truthy value (Reddit's own string "false" included) would skip
            # every remaining activity leg and answer with fabricated errors.
            and (result["data"].get("data") or {}).get("is_suspended") is True
        ):
            payloads.extend(
                {"_fetch_error": "account is suspended; Reddit serves no public activity"}
                for _ in route.requests[index + 1 :]
            )
            break

    content_states = [
        str(payload["_reddit_content_state"])
        for payload in payloads
        if isinstance(payload, dict) and payload.get("_reddit_content_state")
    ]
    if content_states and len(content_states) == len(payloads):
        return {
            "content": "# Reddit\n\n" + "\n\n".join(dict.fromkeys(content_states)),
            "content_type": "markdown",
            "url": route.canonical_url,
        }
    payloads = [
        {
            "_fetch_error": payload["_reddit_content_state"],
            "_content_state": True,
        }
        if isinstance(payload, dict) and payload.get("_reddit_content_state")
        else payload
        for payload in payloads
    ]

    if (
        route.kind == "multi_profile"
        and len(payloads) >= 2
        and not (
            isinstance(payloads[0], dict)
            and payloads[0].get("_fetch_error")
        )
        and not (
            isinstance(payloads[1], dict)
            and payloads[1].get("_fetch_error")
        )
    ):
        scope_error = _multi_feed_scope_error(payloads[0], payloads[1])
        if scope_error is not None:
            return {"error": scope_error}

    if route.kind == "related" and payloads:
        source = payloads[0]
        source_children = (
            (source.get("data") or {}).get("children") or []
            if isinstance(source, dict)
            else []
        )
        source_data = (
            source_children[0].get("data") or {}
            if source_children and isinstance(source_children[0], dict)
            else {}
        )
        post_id = str(source_data.get("id") or "")
        subreddit = str(source_data.get("subreddit") or "")
        if not (
            re.fullmatch(r"[A-Za-z0-9]{2,16}", post_id)
            and re.fullmatch(r"[A-Za-z0-9_]{1,21}", subreddit)
        ):
            payloads.append(
                {
                    "_fetch_error": (
                        "Reddit did not return the source post needed to "
                        "resolve related posts."
                    )
                }
            )
        else:
            partial_url = (
                "https://www.reddit.com/svc/shreddit/pdp-right-rail/related/"
                f"{quote(subreddit, safe='')}/t3_{quote(post_id, safe='')}"
                "?render-mode=partial&"
                + urlencode({"referer": route.canonical_url})
            )
            partial = await _fetch_reddit_html(
                partial_url,
                session,
                queue,
                deadline,
            )
            if "error" in partial:
                payloads.append({"_fetch_error": partial["error"]})
            else:
                query = parse_qs(
                    urlparse(route.canonical_url).query,
                    keep_blank_values=True,
                )
                raw_limit = (query.get("limit") or [""])[0]
                requested_limit = (
                    int(raw_limit)
                    if raw_limit.isdigit() and int(raw_limit) > 0
                    else None
                )
                transfer_limit = max(5, min(500, max_tokens // 100))
                if requested_limit is not None:
                    transfer_limit = min(transfer_limit, requested_limit)
                related_listing = parse_reddit_related_html(
                    partial["html"],
                    transfer_limit,
                )
                if not related_listing.get("_related_partial_valid"):
                    payloads.append(
                        {
                            "_fetch_error": (
                                "Reddit returned an invalid New Reddit "
                                "related-post partial."
                            )
                        }
                    )
                elif not _is_listing_payload(
                    related_listing,
                    allowed_kinds=frozenset({"t3"}),
                ):
                    payloads.append(
                        {
                            "_fetch_error": (
                                "Reddit returned malformed related-post "
                                "cards."
                            )
                        }
                    )
                else:
                    invalid_count = related_listing.get(
                        "_related_partial_invalid_count",
                    )
                    if isinstance(invalid_count, int):
                        _add_related_notice(
                            related_listing,
                            invalid_count,
                            "New Reddit returned malformed related-post cards",
                        )
                    payloads.append(
                        await _enrich_related_listing(
                            related_listing,
                            session=session,
                            queue=queue,
                            deadline=deadline,
                        )
                    )

    if route.kind == "collection" and payloads and isinstance(payloads[0], dict):
        if not isinstance(payloads[0].get("link_ids"), list):
            return {
                "error": (
                    "Reddit collection is unavailable: metadata returned an "
                    "invalid link_ids value."
                )
            }
        link_ids = payloads[0]["link_ids"]
        for batch_start in range(0, len(link_ids), 100):
            batch = link_ids[batch_start : batch_start + 100]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "error": (
                        "Reddit collection is unavailable: current post "
                        f"hydration timed out ({timeout:g}s limit)."
                    )
                }
            info_url = "https://www.reddit.com/api/info.json?" + urlencode(
                {
                    "raw_json": "1",
                    "limit": str(len(batch)),
                    "id": ",".join(batch),
                }
            )
            result = await fetch_reddit_json(
                info_url,
                session,
                queue,
                remaining,
            )
            if "error" in result:
                detail = _bounded_related_detail(result["error"])
                return {
                    "error": (
                        "Reddit collection is unavailable: current post "
                        f"hydration failed ({detail})"
                    )
                }
            elif not _is_listing_payload(
                result["data"],
                allowed_kinds=frozenset({"t3"}),
            ):
                return {
                    "error": (
                        "Reddit collection is unavailable: current post "
                        "hydration returned an invalid response."
                    )
                }
            else:
                listing = result["data"]
                children = listing["data"]["children"]
                children_by_fullname: dict[str, dict] = {}
                for child in children:
                    child_data = child["data"]
                    fullname = child_data.get("name")
                    if not isinstance(fullname, str):
                        fullname = f"t3_{child_data['id']}"
                    if (
                        fullname not in batch
                        or fullname in children_by_fullname
                    ):
                        return {
                            "error": (
                                "Reddit collection is unavailable: current "
                                "post hydration substituted or duplicated "
                                "an archived post ID."
                            )
                        }
                    children_by_fullname[fullname] = child
                if set(children_by_fullname) != set(batch):
                    return {
                        "error": (
                            "Reddit collection is unavailable: current post "
                            "hydration omitted an archived post ID."
                        )
                    }
                ordered_listing = dict(listing)
                ordered_data = dict(listing["data"])
                ordered_data["children"] = [
                    children_by_fullname[fullname] for fullname in batch
                ]
                ordered_listing["data"] = ordered_data
                payloads.append(ordered_listing)

    if route.kind == "user_directory" and payloads:
        hydrated = await _hydrate_user_directory(
            payloads[0],
            session=session,
            queue=queue,
            deadline=deadline,
        )
        if "error" in hydrated:
            return hydrated
        payloads[0] = hydrated["data"]

    independent_kinds = {"multi_profile", "user_profile", "live"}
    if route.kind in independent_kinds and payloads and all(
        isinstance(payload, dict)
        and payload.get("_fetch_error")
        and not payload.get("_content_state")
        for payload in payloads
    ):
        errors = list(
            dict.fromkeys(str(payload["_fetch_error"]) for payload in payloads)
        )
        return {"error": "All Reddit data sources were unavailable: " + "; ".join(errors)}

    content = render_reddit_route(
        route,
        payloads,
        max_tokens=max_tokens,
        chars_per_token=chars_per_token,
    )
    return {
        "content": content,
        "content_type": "markdown",
        "url": route.canonical_url,
    }
