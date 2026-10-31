"""Reddit's app GraphQL reads, and their translation into fetchaller's shapes.

Since about 2026-10-20 Reddit refuses the REST reads for a thread and its
comments (``/comments/<id>``), ``/api/info``, ``/api/morechildren`` and
``/user/<name>/about`` to every logged-out client: the Android app's anonymous
token, an established anonymous web session and a fresh Chrome alike. The
official app never calls those paths. It reads the same data as *persisted*
GraphQL operations POSTed to ``gql-fed.reddit.com``, which the same anonymous
token is served. wafer carries those POSTs on its app route and returns
Reddit's GraphQL JSON untouched; choosing the operation and turning its answer
into something the renderers understand is content analysis, so it lives here.

Every converter below emits the REST shape the existing renderers and schema
validators already accept (``t1``/``more`` Things, a ``t2`` about payload), and
only from fields GraphQL actually returned. Fields REST had and GraphQL lacks
(a comment's ``controversiality``, an account's email-verified flag) are left
out rather than defaulted, so the renderer omits them instead of asserting a
value Reddit never sent. Each converted Thing carries
``_fetchaller_reddit_provenance: "graphql"``.

Persisted-query hashes come from the 2026.44.0 app (``gql_ops.tsv`` in the
wafer handoff). Only the hash and variables are sent, never query text: the app
never sends query text, and Reddit answers ``PersistedQueryNotFound`` when an
operation is retired, which surfaces as an error naming the operation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

GRAPHQL_URL = "https://gql-fed.reddit.com/"
PROVENANCE = "graphql"

# name -> sha256 of the app's persisted query document (app 2026.44.0).
PERSISTED_QUERIES: dict[str, str] = {
    # postInfoById { commentForest { trees { depth parentId node more } } }
    "PostComments": (
        "314f29c2494fd78c7844ef2c6b09acb4bef5b37ab50b4ddc01691d6d5c8433f8"
    ),
    # redditorInfoByName: karma {total fromPosts fromComments}, profile, ...
    "UserProfile": (
        "a05c628c5469c6b107c85988a8b1d0b79a2e7b6150ebd3a449c67e226e975ddc"
    ),
    # commentsByIds(ids: [ID!]!) -- one call for a whole fullname batch.
    "LiveCommentsByIds": (
        "51ab9f2e0eb87b6dac9aac02012ba7046049e516ec55ec442abfed0f19c6f311"
    ),
    # postsInfoByIds(ids: [ID!]!): /by_id answers one post but refuses a
    # list of them (and /api/info) to logged-out clients (2026-10-31).
    "PostsByIds": (
        "29a8d4c6e135fffb95fc4cd303c57ad926c6c5ff582f4c4577c8a69b7f322d91"
    ),
    # redditorsInfoByIds: the account-level ``cakeDayOn`` date. UserProfile
    # only dates the *profile*, and an account without one (``profile: null``,
    # e.g. u/Unidan) would otherwise have no creation date at all.
    "GetMatrixChatUsersByIds": (
        "1412e2c935de284ee35ec458baabc6920b530d87309da5eecdaa8b83f87f36c4"
    ),
}

# REST comment sorts and the app's CommentSort enum (com.reddit.type).
_COMMENT_SORT_ENUM = {
    "confidence": "CONFIDENCE",
    "top": "TOP",
    "new": "NEW",
    "controversial": "CONTROVERSIAL",
    "old": "OLD",
    "qa": "QA",
    "random": "RANDOM",
    "live": "LIVE",
}

_POST_ID_RE = re.compile(r"[A-Za-z0-9]{2,16}\Z")
_COMMENT_FULLNAME_RE = re.compile(r"t1_([A-Za-z0-9]{2,16})\Z")
_USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# The forest's "more" cursors are opaque, e.g.
# ``commenttree:6d35350d,::0:ex:,WcvsxUxoy:u\:$%]d3r|$q,...``. They round-trip
# through a URL query, so accept only bounded printable ASCII.
_CURSOR_RE = re.compile(r"[\x20-\x7e]{1,1024}\Z")
# Bound the work a single answer can cause, as the REST validators do.
_MAX_TREES = 20_000
_MAX_BATCH = 100


@dataclass(frozen=True)
class RedditGraphQLRequest:
    """One persisted GraphQL operation, hashable so routes stay frozen."""

    operation: str
    variables_json: str

    def __post_init__(self) -> None:
        if self.operation not in PERSISTED_QUERIES:
            raise ValueError(f"unknown Reddit GraphQL operation: {self.operation}")

    @property
    def variables(self) -> dict:
        return json.loads(self.variables_json)

    def payload(self) -> dict:
        return {
            "operationName": self.operation,
            "variables": self.variables,
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": PERSISTED_QUERIES[self.operation],
                }
            },
        }

    def __str__(self) -> str:
        return f"graphql:{self.operation} {self.variables_json}"


def graphql_request(operation: str, variables: dict) -> RedditGraphQLRequest:
    return RedditGraphQLRequest(
        operation,
        json.dumps(variables, sort_keys=True, separators=(",", ":")),
    )


def valid_comment_cursor(value: object) -> bool:
    return isinstance(value, str) and _CURSOR_RE.fullmatch(value) is not None


def post_comments_request(
    post_id: str,
    *,
    sort: str,
    count: int,
    max_depth: int,
    after: str | None = None,
    focused_comment_id: str | None = None,
    num_parents: int | None = None,
    truncate: int | None = None,
) -> RedditGraphQLRequest:
    """``PostComments`` for one thread, a focused permalink, or a next page.

    ``id``, ``includePremiumAvatarTreatment`` and ``includeConsistencyToken``
    are non-null in the app's document and must be sent. A focused permalink
    is never combined with ``after``: the cursor already names its position,
    and other clients saw nested pagination break when both were sent.
    """

    if not _POST_ID_RE.fullmatch(post_id):
        raise ValueError("invalid Reddit post id")
    variables: dict[str, object] = {
        "id": f"t3_{post_id}",
        "sortType": _COMMENT_SORT_ENUM.get(sort, "CONFIDENCE"),
        "count": max(1, int(count)),
        "maxDepth": max(1, int(max_depth)),
        "includeConsistencyToken": False,
        "includePremiumAvatarTreatment": False,
    }
    if after is not None:
        if not valid_comment_cursor(after):
            raise ValueError("invalid Reddit comment cursor")
        variables["after"] = after
    elif focused_comment_id is not None:
        if not _POST_ID_RE.fullmatch(focused_comment_id):
            raise ValueError("invalid Reddit comment id")
        variables["focusedCommentContext"] = {
            "commentId": f"t1_{focused_comment_id}",
            "isFocusedThreadOnly": True,
        }
        if num_parents is not None:
            variables["numParents"] = max(0, int(num_parents))
    if truncate is not None:
        variables["truncate"] = max(0, int(truncate))
    return graphql_request("PostComments", variables)


def user_profile_request(username: str) -> RedditGraphQLRequest:
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("invalid Reddit username")
    return graphql_request(
        "UserProfile",
        {"name": username, "includePremiumAvatarTreatment": False},
    )


def account_cake_day_request(account_id: str) -> RedditGraphQLRequest:
    if not _POST_ID_RE.fullmatch(account_id):
        raise ValueError("invalid Reddit account id")
    return graphql_request(
        "GetMatrixChatUsersByIds",
        {"userKindWithIds": [f"t2_{account_id}"]},
    )


def posts_by_ids_request(post_ids: list[str]) -> RedditGraphQLRequest:
    if not 1 <= len(post_ids) <= _MAX_BATCH or not all(
        _POST_ID_RE.fullmatch(value) for value in post_ids
    ):
        raise ValueError("invalid Reddit post id batch")
    return graphql_request(
        "PostsByIds",
        {"ids": [f"t3_{value}" for value in post_ids]},
    )


def comments_by_ids_request(comment_ids: list[str]) -> RedditGraphQLRequest:
    if not 1 <= len(comment_ids) <= _MAX_BATCH or not all(
        _POST_ID_RE.fullmatch(value) for value in comment_ids
    ):
        raise ValueError("invalid Reddit comment id batch")
    return graphql_request(
        "LiveCommentsByIds",
        {
            "ids": [f"t1_{value}" for value in comment_ids],
            "includePremiumAvatarTreatment": False,
        },
    )


# -- response helpers --------------------------------------------------------


def graphql_errors(payload: object) -> list[str]:
    """Bounded, value-free messages from a GraphQL ``errors`` array."""

    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    messages = []
    for error in errors[:5]:
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and message.strip():
            messages.append(" ".join(message.split())[:200])
        else:
            messages.append("unnamed error")
    return messages


def _epoch(value: object) -> float | None:
    """``2026-10-31T08:56:50.608000+0000`` -> Unix seconds."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    seconds = parsed.timestamp()
    return seconds if isfinite(seconds) and seconds > 0 else None


def _count(value: object) -> int | None:
    """A whole, non-negative count. Reddit sends large karma as ``2.5e6``."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not isfinite(value) or value != int(value) or value < 0:
        return None
    return int(value)


def _string(value: object, maximum: int = 100_000) -> str | None:
    return value if isinstance(value, str) and len(value) <= maximum else None


def _true(value: object) -> bool:
    return value is True


def _enum_text(value: object) -> str | None:
    """``LOW_SCORE`` -> ``low score``; anything else -> None."""

    if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z_]{0,63}", value):
        if value.startswith("UNKNOWN"):
            return None
        return value.lower().replace("_", " ")
    return None


def _flair_richtext(value: object) -> list[dict] | None:
    """Author flair richtext: a JSON string or list of ``{e,t|u}`` parts."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, list):
        return None
    parts = []
    for item in value[:50]:
        if not isinstance(item, dict):
            continue
        part = {
            key: item[key]
            for key in ("e", "t", "u", "a")
            if isinstance(item.get(key), str)
        }
        if part:
            parts.append(part)
    return parts or None


def _media_metadata(content: dict) -> dict | None:
    """Comment media (``richtextMedia``) in REST ``media_metadata`` form."""

    media = content.get("richtextMedia")
    if not isinstance(media, list):
        return None
    metadata: dict[str, dict] = {}
    for index, asset in enumerate(media[:50]):
        if not isinstance(asset, dict):
            continue
        source = {
            key: asset[name]
            for key, name in (("u", "url"), ("mp4", "mp4Url"))
            if isinstance(asset.get(name), str)
        }
        entry: dict[str, object] = {"s": source} if source else {}
        for name in ("hlsUrl", "dashUrl"):
            if isinstance(asset.get(name), str):
                entry[name] = asset[name]
        if entry:
            asset_id = asset.get("id")
            key = asset_id if isinstance(asset_id, str) and asset_id else str(index)
            metadata[key] = entry
    return metadata or None


def _awards(awardings: object) -> tuple[int, list[dict]]:
    if not isinstance(awardings, list):
        return 0, []
    total = 0
    named: list[dict] = []
    for awarding in awardings[:50]:
        if not isinstance(awarding, dict):
            continue
        count = _count(awarding.get("total"))
        if not count:
            continue
        total += count
        award = awarding.get("award")
        name = award.get("name") if isinstance(award, dict) else None
        if isinstance(name, str) and name:
            named.append({"name": name, "count": count})
    return total, named


def comment_data(
    node: dict,
    *,
    post_id: str,
    parent_fullname: str | None,
    subreddit: str | None = None,
) -> dict | None:
    """One GraphQL ``Comment``/``DeletedComment`` as REST ``t1`` data.

    A ``Comment`` without a usable id returns None, which the caller treats
    as a malformed answer. A ``DeletedComment`` has no id at all -- the app's
    fragment for it selects none (seen live 2026-10-31) -- so it is returned
    without one rather than given an invented identity; the forest walk
    recovers it from a reply's ``parentId`` when it has replies.
    """

    typename = node.get("__typename")
    fullname = node.get("id")
    match = (
        _COMMENT_FULLNAME_RE.fullmatch(fullname)
        if isinstance(fullname, str)
        else None
    )
    if match is None and (typename == "Comment" or fullname is not None):
        return None
    data: dict[str, object] = {"link_id": f"t3_{post_id}"}
    if match is not None:
        data["id"] = match.group(1)
        data["name"] = fullname
    data.update({
        "parent_id": parent_fullname or f"t3_{post_id}",
        "_fetchaller_reddit_provenance": PROVENANCE,
    })
    created = _epoch(node.get("createdAt"))
    if created is not None:
        data["created_utc"] = created
    removed_by = _enum_text(node.get("removedByCategory"))
    if removed_by is not None:
        data["removed_by_category"] = removed_by.replace(" ", "_")
    if node.get("isInitiallyCollapsed") is True:
        data["collapsed"] = True
        reason = _enum_text(node.get("initiallyCollapsedReason"))
        if reason is not None:
            data["collapsed_reason_code"] = reason
    if subreddit:
        data["subreddit"] = subreddit
        data["subreddit_name_prefixed"] = f"r/{subreddit}"

    if typename == "DeletedComment":
        # Reddit's own convention: the author deleting it reads "[deleted]",
        # any other removal "[removed]". Nothing else about it is returned.
        data["author"] = "[deleted]"
        data["body"] = (
            "[deleted]"
            if removed_by in (None, "deleted", "author")
            else "[removed]"
        )
        return data
    if typename != "Comment":
        data["author"] = "[unknown]"
        data["body"] = (
            "[Reddit returned a comment of a type this client does not read: "
            f"{typename if isinstance(typename, str) else 'unnamed'}]"
        )[:300]
        return data

    author = node.get("authorInfo")
    author_name = author.get("name") if isinstance(author, dict) else None
    data["author"] = (
        author_name
        if isinstance(author_name, str) and author_name
        else "[deleted]"
    )
    content = node.get("content")
    content = content if isinstance(content, dict) else {}
    body = _string(content.get("markdown"))
    if not body:
        body = "[removed]" if node.get("isRemoved") is True else ""
    data["body"] = body
    score = node.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        data["score"] = score
    if node.get("isScoreHidden") is True:
        data["score_hidden"] = True
    edited = _epoch(node.get("editedAt"))
    data["edited"] = edited if edited is not None else False
    permalink = _string(node.get("permalink"), 2048)
    if permalink and permalink.startswith("/"):
        data["permalink"] = permalink
    for rest_name, gql_name in (
        ("is_submitter", "isOP"),
        ("stickied", "isStickied"),
        ("locked", "isLocked"),
        ("archived", "isArchived"),
    ):
        if _true(node.get(gql_name)):
            data[rest_name] = True
    distinguished = _enum_text(node.get("distinguishedAs"))
    if distinguished is not None:
        data["distinguished"] = distinguished.replace(" ", "_")
    flair = node.get("authorFlair")
    if isinstance(flair, dict):
        text = _string(flair.get("text"), 500)
        if text:
            data["author_flair_text"] = text
        richtext = _flair_richtext(flair.get("richtext"))
        if richtext:
            data["author_flair_richtext"] = richtext
    total, named = _awards(node.get("awardings"))
    if total:
        data["total_awards_received"] = total
        data["all_awardings"] = named
    media = _media_metadata(content)
    if media:
        data["media_metadata"] = media
    return data


def _more_data(entry: dict, *, post_id: str, parent_fullname: str | None) -> dict | None:
    more = entry["more"]
    count = _count(more.get("count"))
    cursor = more.get("cursor")
    if not valid_comment_cursor(cursor):
        cursor = None
    data: dict[str, object] = {
        "children": [],
        "parent_id": parent_fullname or f"t3_{post_id}",
        "link_id": f"t3_{post_id}",
        "_fetchaller_reddit_provenance": PROVENANCE,
    }
    if count is not None:
        data["count"] = count
    if cursor is not None:
        data["cursor"] = cursor
    if more.get("isTooDeepForCount") is True:
        data["too_deep"] = True
    return data


def comment_forest_listing(
    forest: object,
    *,
    post_id: str,
    subreddit: str | None = None,
) -> dict | None:
    """A ``CommentForest`` as a REST comment Listing with nested replies.

    The forest is a flat pre-order list; each entry names its ``parentId`` (a
    comment fullname, or null for a top-level entry) and holds either a
    ``node`` or a ``more`` stub. Entries whose parent is not in this answer --
    the children of a nested cursor, say -- are kept at the top level with
    their real ``parent_id``, so the renderer still links their context.
    Returns None for an answer that is not a forest at all.
    """

    if not isinstance(forest, dict) or not _POST_ID_RE.fullmatch(post_id):
        return None
    trees = forest.get("trees")
    if not isinstance(trees, list) or len(trees) > _MAX_TREES:
        return None
    top: list[dict] = []
    comments: dict[str, dict] = {}
    # The forest is pre-order: an entry's parent is the latest comment one
    # level up. That places the replies of an id-less DeletedComment, and the
    # first such reply's parentId tells us the deleted comment's id.
    by_depth: list[dict | None] = []
    for entry in trees:
        if not isinstance(entry, dict):
            return None
        depth = entry.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 1000:
            return None
        parent = entry.get("parentId")
        parent_fullname = (
            parent
            if isinstance(parent, str) and _COMMENT_FULLNAME_RE.fullmatch(parent)
            else None
        )
        container = comments.get(parent_fullname) if parent_fullname else None
        if container is None and parent_fullname and 0 < depth <= len(by_depth):
            above = by_depth[depth - 1]
            if above is not None and "name" not in above:
                match = _COMMENT_FULLNAME_RE.fullmatch(parent_fullname)
                above["id"] = match.group(1)
                above["name"] = parent_fullname
                comments[parent_fullname] = above
                container = above
        node = entry.get("node")
        if isinstance(node, dict):
            data = comment_data(
                node,
                post_id=post_id,
                parent_fullname=parent_fullname,
                subreddit=subreddit,
            )
            if data is None:
                return None
            data["replies"] = ""
            thing = {"kind": "t1", "data": data}
        elif isinstance(entry.get("more"), dict):
            data = _more_data(entry, post_id=post_id, parent_fullname=parent_fullname)
            if data is None:
                return None
            thing = {"kind": "more", "data": data}
        else:
            return None
        if container is None:
            top.append(thing)
        else:
            replies = container["replies"]
            if not replies:
                replies = {"kind": "Listing", "data": {"children": []}}
                container["replies"] = replies
            replies["data"]["children"].append(thing)
        if thing["kind"] == "t1":
            del by_depth[depth:]
            by_depth.extend([None] * (depth - len(by_depth)))
            by_depth.append(thing["data"])
            if "name" in thing["data"]:
                comments[str(thing["data"]["name"])] = thing["data"]
    return {
        "kind": "Listing",
        "data": {"children": top, "after": None, "before": None},
        "_fetchaller_reddit_provenance": PROVENANCE,
    }


def post_comments_listing(payload: object, *, post_id: str) -> dict:
    """``{"data": listing}``, a content state, or ``{"error": ...}``."""

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "postInfoById" not in data:
        errors = graphql_errors(payload)
        return {
            "error": "Reddit's PostComments read failed"
            + (f": {'; '.join(errors)}" if errors else ".")
        }
    post = data.get("postInfoById")
    if post is None:
        return {"content_state": "Reddit post not found."}
    if not isinstance(post, dict):
        return {"error": "Reddit returned an invalid comment forest."}
    subreddit = None
    community = post.get("subreddit")
    if isinstance(community, dict) and isinstance(community.get("name"), str):
        subreddit = community["name"]
    forest = post.get("commentForest")
    if forest is None:
        listing = {
            "kind": "Listing",
            "data": {"children": [], "after": None, "before": None},
            "_fetchaller_reddit_provenance": PROVENANCE,
        }
    else:
        listing = comment_forest_listing(forest, post_id=post_id, subreddit=subreddit)
        if listing is None:
            return {"error": "Reddit returned an invalid comment forest."}
    page = forest.get("pageInfo") if isinstance(forest, dict) else None
    count = _count(page.get("commentCount")) if isinstance(page, dict) else None
    if count is not None:
        listing["_comment_count"] = count
    # Enough to head a "load more" page without re-reading the post.
    about: dict[str, str] = {"id": post_id}
    title = _string(post.get("title"), 1000)
    if title:
        about["title"] = title
    if subreddit and re.fullmatch(r"[A-Za-z0-9_]{1,21}", subreddit):
        about["subreddit"] = subreddit
    listing["_post"] = about
    return {"data": listing}


def comments_by_ids_things(payload: object, fullnames: list[str]) -> dict:
    """``commentsByIds`` as ``{"things": {fullname: t1 Thing}}`` or an error.

    Unknown or deleted ids come back as ``null`` and are simply absent from
    the mapping; the caller reports them.
    """

    data = payload.get("data") if isinstance(payload, dict) else None
    comments = data.get("commentsByIds") if isinstance(data, dict) else None
    if not isinstance(comments, list):
        errors = graphql_errors(payload)
        return {
            "error": "Reddit's comment lookup failed"
            + (f": {'; '.join(errors)}" if errors else ".")
        }
    things: dict[str, dict] = {}
    for node in comments[: len(fullnames)]:
        if not isinstance(node, dict):
            continue
        fullname = node.get("id")
        if not isinstance(fullname, str) or fullname not in fullnames:
            return {"error": "Reddit's comment lookup substituted a comment ID."}
        permalink = node.get("permalink")
        post_match = (
            re.match(
                r"/r/([A-Za-z0-9_]{1,21})/comments/([A-Za-z0-9]{2,16})/",
                permalink,
            )
            if isinstance(permalink, str)
            else None
        )
        if post_match is None:
            return {"error": "Reddit's comment lookup returned no post for a comment."}
        converted = comment_data(
            node,
            post_id=post_match.group(2),
            parent_fullname=None,
            subreddit=post_match.group(1),
        )
        if converted is None:
            return {"error": "Reddit returned an invalid comment."}
        # commentsByIds does not name a comment's parent, so do not claim the
        # post is its parent: drop the placeholder rather than link wrongly.
        converted.pop("parent_id", None)
        things[fullname] = {"kind": "t1", "data": converted}
    return {"things": things}


def user_profile_about(payload: object, username: str) -> dict:
    """``UserProfile`` as a REST ``t2`` about payload, a state, or an error.

    REST ``about.json`` fields with a GraphQL source: name, post/comment/total
    karma, created, employee, avatar, profile title and description. REST's
    ``verified`` (email verified) and ``is_gold`` have no field in the app's
    profile document and are not reported. Nor is suspension: no answer seen
    so far names it (``UnavailableProfileReason`` covers age, legal and
    blocked states only), so it is never inferred from a missing profile.

    The returned ``t2`` data lacks ``created_utc`` when the account has no
    profile; ``needs_cake_day`` then asks the caller for the account-level
    date (``account_cake_day``).
    """

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "redditorInfoByName" not in data:
        errors = graphql_errors(payload)
        return {
            "error": "Reddit's UserProfile read failed"
            + (f": {'; '.join(errors)}" if errors else ".")
        }
    info = data.get("redditorInfoByName")
    if info is None:
        return {"content_state": f"Reddit user u/{username} not found."}
    if not isinstance(info, dict):
        return {"error": "Reddit returned an invalid user profile."}
    typename = info.get("__typename")
    if typename == "DeletedRedditor":
        return {"content_state": f"Reddit account u/{username} was deleted."}
    if typename == "UnavailableRedditor":
        return {
            "content_state": (
                f"Reddit reports u/{username} as unavailable and serves no "
                "profile for it."
            )
        }
    if typename != "Redditor":
        return {"error": "Reddit returned an invalid user profile."}
    name = info.get("name")
    if not isinstance(name, str) or name.casefold() != username.casefold():
        return {"error": "Reddit's UserProfile read substituted another account."}

    about: dict[str, object] = {
        "name": name,
        "_fetchaller_reddit_provenance": PROVENANCE,
    }
    account_id = info.get("id")
    if isinstance(account_id, str) and re.fullmatch(r"t2_[A-Za-z0-9]{2,16}", account_id):
        about["id"] = account_id[3:]
    karma = info.get("karma")
    if isinstance(karma, dict):
        for rest_name, gql_name in (
            ("link_karma", "fromPosts"),
            ("comment_karma", "fromComments"),
            ("total_karma", "total"),
        ):
            value = _count(karma.get(gql_name))
            if value is not None:
                about[rest_name] = value
    if info.get("isEmployee") is True:
        about["is_employee"] = True
    stats = info.get("contributionStats")
    if isinstance(stats, dict):
        for rest_name, gql_name in (
            ("post_count", "postCount"),
            ("comment_count", "commentCount"),
        ):
            value = _count(stats.get(gql_name))
            if value is not None:
                about[rest_name] = value

    profile = info.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    created = _epoch(profile.get("createdAt"))
    if created is not None:
        about["created_utc"] = created
    subreddit: dict[str, object] = {}
    title = _string(profile.get("title"), 500)
    if title:
        subreddit["title"] = title
    description = _string(profile.get("publicDescriptionText"), 10_000)
    if description:
        subreddit["public_description"] = description
    if profile.get("isNsfw") is True:
        subreddit["over_18"] = True
    styles = profile.get("styles")
    if isinstance(styles, dict):
        legacy = styles.get("legacyIcon")
        icon = (
            legacy.get("url") if isinstance(legacy, dict) else None
        ) or styles.get("icon")
        if isinstance(icon, str) and icon.startswith("https://"):
            subreddit["icon_img"] = icon
    if subreddit:
        about["subreddit"] = subreddit
    snoovatar = info.get("snoovatarIcon")
    if isinstance(snoovatar, dict) and isinstance(snoovatar.get("url"), str):
        if snoovatar["url"].startswith("https://"):
            about["snoovatar_img"] = snoovatar["url"]

    unavailable = info.get("profileInfo")
    if (
        isinstance(unavailable, dict)
        and unavailable.get("__typename") == "UnavailableProfile"
    ):
        about["profile_unavailable"] = (
            _enum_text(unavailable.get("unavailableReason")) or "unavailable"
        )
    result: dict[str, object] = {"data": {"kind": "t2", "data": about}}
    if created is None and "id" in about:
        result["needs_cake_day"] = about["id"]
    return result


def account_cake_day(payload: object, account_id: str) -> str | None:
    """The account's ``cakeDayOn`` (``YYYY-MM-DD``), or None."""

    data = payload.get("data") if isinstance(payload, dict) else None
    users = data.get("redditorsInfoByIds") if isinstance(data, dict) else None
    if not isinstance(users, list):
        return None
    for user in users[:5]:
        if not isinstance(user, dict) or user.get("id") != f"t2_{account_id}":
            continue
        value = user.get("cakeDayOn")
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None
            return value
    return None


def _media_url(asset: object) -> str | None:
    if not isinstance(asset, dict):
        return None
    for key in ("url", "mp4Url"):
        value = asset.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None


def post_data(node: dict) -> dict | None:
    """One GraphQL post as the REST ``t3`` fields a listing card renders.

    Built for ``PostsByIds`` answers (several posts in one read, where
    ``/by_id`` now refuses a list). Only fields the answer carries are set;
    a thread's own post still comes from ``/by_id`` with every REST field.
    """

    fullname = node.get("id")
    if not isinstance(fullname, str):
        return None
    match = re.fullmatch(r"t3_([A-Za-z0-9]{2,16})", fullname)
    title = node.get("postTitle")
    if match is None or not isinstance(title, str) or not title.strip():
        return None
    data: dict[str, object] = {
        "id": match.group(1),
        "name": fullname,
        "title": title,
        "_fetchaller_reddit_provenance": PROVENANCE,
    }
    author = node.get("authorInfo")
    name = author.get("name") if isinstance(author, dict) else None
    data["author"] = name if isinstance(name, str) and name else "[deleted]"
    for rest_name, gql_name in (("num_comments", "commentCount"), ("score", "score")):
        value = node.get(gql_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            data[rest_name] = value
    ratio = node.get("upvoteRatio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= ratio <= 1:
        data["upvote_ratio"] = ratio
    created = _epoch(node.get("createdAt"))
    if created is not None:
        data["created_utc"] = created
    edited = _epoch(node.get("editedAt"))
    data["edited"] = edited if edited is not None else False
    permalink = _string(node.get("permalink"), 2048)
    if permalink and permalink.startswith("/"):
        data["permalink"] = permalink
    community = node.get("subreddit")
    if isinstance(community, dict) and isinstance(community.get("name"), str):
        data["subreddit"] = community["name"]
        data["subreddit_name_prefixed"] = f"r/{community['name']}"
    is_self = node.get("isSelfPost") is True
    data["is_self"] = is_self
    url = _string(node.get("url"), 4096)
    if url:
        data["url"] = url
    domain = _string(node.get("domain"), 255)
    if domain:
        data["domain"] = domain
    content = node.get("content")
    markdown = content.get("markdown") if isinstance(content, dict) else None
    if is_self and isinstance(markdown, str):
        data["selftext"] = markdown
    for rest_name, gql_name in (
        ("hide_score", "isScoreHidden"),
        ("over_18", "isNsfw"),
        ("spoiler", "isSpoiler"),
        ("locked", "isLocked"),
        ("archived", "isArchived"),
        ("stickied", "isStickied"),
        ("contest_mode", "isContestMode"),
    ):
        if node.get(gql_name) is True:
            data[rest_name] = True
    distinguished = _enum_text(node.get("distinguishedAs"))
    if distinguished is not None:
        data["distinguished"] = distinguished.replace(" ", "_")
    removed_by = _enum_text(node.get("removedByCategory"))
    if removed_by is not None:
        data["removed_by_category"] = removed_by.replace(" ", "_")
    for prefix, gql_name in (("link", "flair"), ("author", "authorFlair")):
        flair = node.get(gql_name)
        if not isinstance(flair, dict):
            continue
        text = _string(flair.get("text"), 500)
        if text:
            data[f"{prefix}_flair_text"] = text
        richtext = _flair_richtext(flair.get("richtext"))
        if richtext:
            data[f"{prefix}_flair_richtext"] = richtext
    total, named = _awards(node.get("awardings"))
    if total:
        data["total_awards_received"] = total
        data["all_awardings"] = named
    gallery = node.get("gallery")
    items = gallery.get("items") if isinstance(gallery, dict) else None
    if isinstance(items, list) and items:
        metadata: dict[str, dict] = {}
        gallery_items: list[dict] = []
        for index, item in enumerate(items[:100]):
            if not isinstance(item, dict):
                continue
            media_id = item.get("id") if isinstance(item.get("id"), str) else str(index)
            entry: dict[str, object] = {"media_id": media_id}
            source = _media_url(item.get("media"))
            if source:
                metadata[media_id] = {"s": {"u": source}}
            caption = _string(item.get("caption"), 1000)
            if caption:
                entry["caption"] = caption
            outbound = _string(item.get("outboundUrl"), 4096)
            if outbound:
                entry["outbound_url"] = outbound
            gallery_items.append(entry)
        data["gallery_data"] = {"items": gallery_items}
        data["media_metadata"] = metadata
    media = node.get("media")
    streaming = media.get("streaming") if isinstance(media, dict) else None
    if isinstance(streaming, dict):
        video = {
            rest_name: streaming[gql_name]
            for rest_name, gql_name in (("hls_url", "hlsUrl"), ("dash_url", "dashUrl"))
            if isinstance(streaming.get(gql_name), str)
        }
        if video:
            data["secure_media"] = {"reddit_video": video}
    event = node.get("postEventInfo")
    if isinstance(event, dict):
        for rest_name, gql_name in (("event_start", "startsAt"), ("event_end", "endsAt")):
            when = _epoch(event.get(gql_name))
            if when is not None:
                data[rest_name] = when
    return data


def posts_by_ids_things(payload: object, fullnames: list[str]) -> dict:
    """``postsInfoByIds`` as ``{"things": {fullname: t3 Thing}}`` or an error.

    A post Reddit returns nothing for is ``null`` and is absent here; the
    caller names it.
    """

    data = payload.get("data") if isinstance(payload, dict) else None
    posts = data.get("postsInfoByIds") if isinstance(data, dict) else None
    if not isinstance(posts, list):
        errors = graphql_errors(payload)
        return {
            "error": "Reddit's post lookup failed"
            + (f": {'; '.join(errors)}" if errors else ".")
        }
    things: dict[str, dict] = {}
    for node in posts[: len(fullnames)]:
        if not isinstance(node, dict):
            continue
        converted = post_data(node)
        if converted is None or converted["name"] not in fullnames:
            return {"error": "Reddit's post lookup returned an invalid or substituted post."}
        things[str(converted["name"])] = {"kind": "t3", "data": converted}
    return {"things": things}
