"""Reddit reads served to logged-out clients only over the app's GraphQL.

Since about 2026-10-20 Reddit refuses a thread's JSON, the comment streams,
``/api/info``, ``/api/morechildren`` and ``/user/<name>/about`` to every
logged-out client, and wafer raises ``RequestBlocked`` for them. These tests
pin the replacement reads and their translation, and that a refusal is final.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import wafer

from fetchaller.content.reddit import COMMENT_STREAM_REFUSED, route_reddit_url
from fetchaller.content.reddit_graphql import (
    account_cake_day,
    comment_forest_listing,
    post_comments_listing,
    user_profile_about,
    user_profile_request,
)
from fetchaller.tools.browse_reddit import (
    REDDIT_REFUSED_MESSAGE,
    fetch_reddit_graphql,
    fetch_reddit_json,
)
from fetchaller.tools.reddit_fetch import _is_comment_listing_payload, fetch_mapped_reddit

GQL_URL = "https://gql-fed.reddit.com/"


class _Response:
    def __init__(self, payload, status_code=200, headers=None, emulation=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        if emulation is not None:
            self.emulation = emulation

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _gql(payload, status_code=200, headers=None):
    return _Response(payload, status_code, headers, emulation="reddit_app")


class _Session:
    """Answers GETs and POSTs in order; an Exception entry is raised."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict]] = []

    async def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get(self, url, **kwargs):
        return await self._next("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return await self._next("POST", url, kwargs)


def _node(comment_id, body="body", **overrides):
    node = {
        "__typename": "Comment",
        "id": f"t1_{comment_id}",
        "createdAt": "2023-11-14T22:15:00.000000+0000",
        "editedAt": None,
        "authorInfo": {"__typename": "Redditor", "name": "bob"},
        "content": {"markdown": body, "richtextMedia": []},
        "score": 9,
        "isScoreHidden": False,
        "permalink": f"/r/Python/comments/abc123/t/{comment_id}/",
        "awardings": [],
    }
    node.update(overrides)
    return node


def _tree(node=None, *, depth=0, parent=None, more=None):
    return {"depth": depth, "parentId": parent, "node": node, "more": more}


def _forest(trees, *, title="The post", count=None):
    return {
        "data": {
            "postInfoById": {
                "__typename": "SubredditPost",
                "id": "t3_abc123",
                "title": title,
                "subreddit": {"name": "Python"},
                "commentForest": {
                    "trees": trees,
                    "pageInfo": {
                        "commentCount": len(trees) if count is None else count,
                        "hasNextPage": False,
                    },
                },
            }
        }
    }


def _post_listing(**overrides):
    data = {
        "id": "abc123",
        "name": "t3_abc123",
        "title": "The post",
        "subreddit": "Python",
        "author": "alice",
        "score": 42,
        "upvote_ratio": 0.93,
        "num_comments": 3,
        "created_utc": 1_700_000_000,
        "permalink": "/r/Python/comments/abc123/the_post/",
        "is_self": True,
        "selftext": "POST BODY",
    }
    data.update(overrides)
    return {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": data}]}}


async def _run_now(callback, *args, **_kwargs):
    return await callback(*args)


@pytest.fixture
def no_limiter():
    with patch("fetchaller.tools.browse_reddit.reddit_limiter.wait", AsyncMock()):
        yield


def _patched_session(session):
    return patch(
        "fetchaller.tools.reddit_fetch._get_session",
        AsyncMock(return_value=session),
    )


class TestCommentForestTranslation:
    def test_flat_preorder_forest_nests_replies_under_their_parents(self):
        listing = comment_forest_listing(
            {
                "trees": [
                    _tree(_node("aa", "top")),
                    _tree(_node("bb", "reply"), depth=1, parent="t1_aa"),
                    _tree(_node("cc", "second top")),
                    # A child whose parent is not in this answer (a nested
                    # cursor's page) stays at the top with its real parent.
                    _tree(_node("dd", "orphan"), depth=3, parent="t1_zz"),
                ]
            },
            post_id="abc123",
        )

        assert listing is not None and _is_comment_listing_payload(listing)
        top = listing["data"]["children"]
        assert [child["data"]["id"] for child in top] == ["aa", "cc", "dd"]
        reply = top[0]["data"]["replies"]["data"]["children"][0]["data"]
        assert reply["id"] == "bb" and reply["parent_id"] == "t1_aa"
        assert top[0]["data"]["parent_id"] == "t3_abc123"
        assert top[2]["data"]["parent_id"] == "t1_zz"
        assert top[1]["data"]["replies"] == ""

    def test_comment_fields_map_only_what_graphql_returned(self):
        node = _node(
            "aa",
            "Quote > kept raw & unescaped",
            editedAt="2023-11-15T00:00:00.000000+0000",
            isOP=True,
            isStickied=True,
            isLocked=True,
            isArchived=True,
            distinguishedAs="MODERATOR",
            isInitiallyCollapsed=True,
            initiallyCollapsedReason="LOW_SCORE",
            authorFlair={
                "text": "Pythonista",
                "richtext": '[{"e":"text","t":"Pythonista"},{"e":"emoji","u":"https://e.example/x.png","a":":x:"}]',
            },
            awardings=[
                {"award": {"name": "Bravo"}, "total": 2},
                {"award": {"name": "Regret"}, "total": 1.0},
            ],
            content={
                "markdown": "Quote > kept raw & unescaped",
                "richtextMedia": [
                    {"__typename": "ImageAsset", "id": "img1", "url": "https://i.redd.it/a.png"},
                    {"__typename": "VideoAsset", "id": "v1", "hlsUrl": "https://v.redd.it/x/hls.m3u8"},
                ],
            },
        )
        data = comment_forest_listing({"trees": [_tree(node)]}, post_id="abc123")[
            "data"
        ]["children"][0]["data"]

        assert data["body"] == "Quote > kept raw & unescaped"
        assert data["score"] == 9
        assert data["created_utc"] == 1_700_000_100
        assert data["edited"] == 1_700_006_400
        assert data["is_submitter"] is True
        assert data["stickied"] is True and data["locked"] is True
        assert data["archived"] is True
        assert data["distinguished"] == "moderator"
        assert data["collapsed"] is True
        assert data["collapsed_reason_code"] == "low score"
        assert data["author_flair_text"] == "Pythonista"
        assert data["author_flair_richtext"][1]["u"] == "https://e.example/x.png"
        assert data["total_awards_received"] == 3
        assert data["all_awardings"] == [
            {"name": "Bravo", "count": 2},
            {"name": "Regret", "count": 1},
        ]
        assert data["media_metadata"] == {
            "img1": {"s": {"u": "https://i.redd.it/a.png"}},
            "v1": {"hlsUrl": "https://v.redd.it/x/hls.m3u8"},
        }
        # REST had controversiality; GraphQL does not, so it is not invented.
        assert "controversiality" not in data
        assert "upvote_ratio" not in data

    def test_deleted_removed_and_unknown_comments_say_what_reddit_said(self):
        listing = comment_forest_listing(
            {
                "trees": [
                    _tree({"__typename": "DeletedComment", "id": "t1_d1",
                           "removedByCategory": "AUTHOR"}),
                    _tree({"__typename": "DeletedComment", "id": "t1_d2",
                           "removedByCategory": "MODERATOR"}),
                    _tree(_node("r1", content={"markdown": ""}, isRemoved=True)),
                    _tree({"__typename": "FutureComment", "id": "t1_f1"}),
                ]
            },
            post_id="abc123",
        )
        bodies = [
            (child["data"]["author"], child["data"]["body"])
            for child in listing["data"]["children"]
        ]
        assert bodies[0] == ("[deleted]", "[deleted]")
        assert bodies[1] == ("[deleted]", "[removed]")
        assert bodies[2][1] == "[removed]"
        assert "FutureComment" in bodies[3][1]

    def test_more_stubs_carry_their_cursor_and_count(self):
        listing = comment_forest_listing(
            {
                "trees": [
                    _tree(_node("aa")),
                    _tree(depth=1, parent="t1_aa",
                          more={"count": 4, "cursor": "commenttree:1:d1:t1_aa"}),
                    _tree(more={"count": 2.0e1, "cursor": "commenttree:0:ex:,W"}),
                ]
            },
            post_id="abc123",
        )
        nested = listing["data"]["children"][0]["data"]["replies"]["data"]["children"][0]
        top_more = listing["data"]["children"][1]
        assert nested["kind"] == "more"
        assert nested["data"]["cursor"] == "commenttree:1:d1:t1_aa"
        assert nested["data"]["parent_id"] == "t1_aa"
        assert top_more["data"]["count"] == 20
        assert top_more["data"]["parent_id"] == "t3_abc123"

    def test_malformed_forest_is_rejected_not_rendered_empty(self):
        assert comment_forest_listing({"trees": "x"}, post_id="abc123") is None
        assert comment_forest_listing({"trees": [_tree()]}, post_id="abc123") is None
        assert comment_forest_listing(
            {"trees": [_tree({"__typename": "Comment", "id": "bad"})]},
            post_id="abc123",
        ) is None

    def test_missing_post_and_errors_are_states_not_empty_threads(self):
        assert post_comments_listing(
            {"data": {"postInfoById": None}}, post_id="abc123"
        ) == {"content_state": "Reddit post not found."}
        assert post_comments_listing(
            {"errors": [{"message": "PersistedQueryNotFound"}]}, post_id="abc123"
        ) == {"error": "Reddit's PostComments read failed: PersistedQueryNotFound"}


class TestUserProfileTranslation:
    def test_profile_maps_karma_split_and_dates(self):
        payload = {
            "data": {
                "redditorInfoByName": {
                    "__typename": "Redditor",
                    "id": "t2_1w72",
                    "name": "spez",
                    "isEmployee": True,
                    "karma": {"fromPosts": 184762, "fromComments": 7.5662e5, "total": 941382},
                    "contributionStats": {"postCount": 851, "commentCount": 3228},
                    "profile": {
                        "createdAt": "2005-06-06T04:00:00.000000+0000",
                        "title": "spez",
                        "publicDescriptionText": "Reddit CEO",
                        "styles": {"legacyIcon": {"url": "https://styles.redditmedia.com/p.png"}},
                    },
                    "profileInfo": {"__typename": "Profile"},
                }
            }
        }
        converted = user_profile_about(payload, "SPEZ")

        assert "needs_cake_day" not in converted
        about = converted["data"]["data"]
        assert about["link_karma"] == 184762
        assert about["comment_karma"] == 756620
        assert about["total_karma"] == 941382
        assert about["created_utc"] == 1118030400
        assert about["is_employee"] is True
        assert about["subreddit"]["public_description"] == "Reddit CEO"
        # Not in the app's profile document, so never asserted.
        assert "verified" not in about and "is_gold" not in about
        assert "is_suspended" not in about

    @pytest.mark.parametrize(
        ("info", "state"),
        [
            (None, "Reddit user u/ghost not found."),
            ({"__typename": "DeletedRedditor"}, "Reddit account u/ghost was deleted."),
            (
                {"__typename": "UnavailableRedditor"},
                "Reddit reports u/ghost as unavailable and serves no profile for it.",
            ),
        ],
    )
    def test_absent_accounts_are_content_states(self, info, state):
        assert user_profile_about(
            {"data": {"redditorInfoByName": info}}, "ghost"
        ) == {"content_state": state}

    def test_substituted_account_is_an_error(self):
        converted = user_profile_about(
            {"data": {"redditorInfoByName": {"__typename": "Redditor", "name": "other"}}},
            "ghost",
        )
        assert converted == {
            "error": "Reddit's UserProfile read substituted another account."
        }

    def test_profileless_account_asks_for_its_cake_day(self):
        converted = user_profile_about(
            {
                "data": {
                    "redditorInfoByName": {
                        "__typename": "Redditor",
                        "id": "t2_3n71t",
                        "name": "Unidan",
                        "karma": {"fromPosts": 164836, "fromComments": 2338471.0, "total": 2503307.0},
                        "profile": None,
                        "profileInfo": None,
                    }
                }
            },
            "Unidan",
        )
        assert converted["needs_cake_day"] == "3n71t"
        assert "created_utc" not in converted["data"]["data"]
        assert account_cake_day(
            {"data": {"redditorsInfoByIds": [{"id": "t2_3n71t", "cakeDayOn": "2009-09-23"}]}},
            "3n71t",
        ) == "2009-09-23"
        assert account_cake_day(
            {"data": {"redditorsInfoByIds": [{"id": "t2_other", "cakeDayOn": "2009-09-23"}]}},
            "3n71t",
        ) is None


class TestRefusedReadsAreFinal:
    async def test_request_blocked_is_not_the_session_gate_and_never_backs_off(
        self, no_limiter
    ):
        blocked = wafer.RequestBlocked(
            "reddit", "https://www.reddit.com/r/Python/comments.json", 403
        )
        session = _Session([blocked])
        with patch("fetchaller.tools.browse_reddit.reddit_limiter.defer") as defer:
            result = await fetch_reddit_json(
                "https://www.reddit.com/r/Python/comments.json?limit=5",
                session,
                timeout=10,
            )

        assert result == {"error": REDDIT_REFUSED_MESSAGE, "reddit_refused": True}
        assert len(session.calls) == 1  # no gate retry
        defer.assert_not_called()

    async def test_returned_block_under_zero_rotations_is_treated_the_same(
        self, no_limiter
    ):
        gate = _Response(None, 403, emulation="reddit_app")
        gate.challenge_type = "reddit"
        session = _Session([gate])
        queue = AsyncMock()
        queue.enqueue.side_effect = _run_now
        result = await fetch_reddit_json(
            "https://www.reddit.com/user/spez/about.json",
            session,
            queue,
            timeout=10,
        )

        assert result["reddit_refused"] is True
        queue.set_backoff.assert_not_called()

    async def test_comment_stream_refusal_is_reported_as_reddits_answer(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/r/Python/comments/?limit=5")
        session = _Session(
            [wafer.RequestBlocked("reddit", "https://www.reddit.com/", 403)]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=2000, timeout=10)

        assert result == {"error": COMMENT_STREAM_REFUSED}
        assert "0 items returned" not in result["error"]


class TestGraphQLTransport:
    async def test_post_is_a_persisted_operation_with_no_headers(self, no_limiter):
        session = _Session([_gql({"data": {"redditorInfoByName": None}})])
        result = await fetch_reddit_graphql(user_profile_request("spez"), session, timeout=10)

        assert result == {"data": {"data": {"redditorInfoByName": None}}}
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("POST", GQL_URL)
        assert "headers" not in kwargs
        assert kwargs["json"] == {
            "operationName": "UserProfile",
            "variables": {"name": "spez", "includePremiumAvatarTreatment": False},
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": (
                        "a05c628c5469c6b107c85988a8b1d0b79a2e7b6150ebd3a449c67e226e975ddc"
                    ),
                }
            },
        }

    async def test_answer_not_carried_by_the_app_route_is_an_error(self, no_limiter):
        session = _Session([_Response({"data": {}}, 401)])
        result = await fetch_reddit_graphql(user_profile_request("spez"), session, timeout=10)

        assert "needs wafer's Reddit app route" in result["error"]
        assert "HTTP 401" in result["error"]

    async def test_errors_without_data_name_the_operation(self, no_limiter):
        session = _Session(
            [_gql({"errors": [{"message": "PersistedQueryNotFound"}], "data": None})]
        )
        result = await fetch_reddit_graphql(user_profile_request("spez"), session, timeout=10)

        assert result == {
            "error": "Reddit's UserProfile read failed: PersistedQueryNotFound."
        }

    async def test_rate_limit_backs_off_the_shared_queue(self, no_limiter):
        session = _Session([_gql(None, 429, {"retry-after": "12"})])
        queue = AsyncMock()
        queue.enqueue.side_effect = _run_now
        result = await fetch_reddit_graphql(
            user_profile_request("spez"), session, queue, timeout=10
        )

        assert result["error"].startswith("Rate limited.")
        queue.set_backoff.assert_called_once_with(429, retry_after=12.0)

    async def test_raised_rate_limit_backs_off_like_a_429(self, no_limiter):
        queue = AsyncMock()
        queue.enqueue.side_effect = _run_now
        graphql = await fetch_reddit_graphql(
            user_profile_request("spez"),
            _Session([wafer.RateLimited(GQL_URL, retry_after=30.0)]),
            queue,
            timeout=10,
        )
        rest = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json?limit=1",
            _Session([wafer.RateLimited("https://www.reddit.com/", retry_after=None)]),
            queue,
            timeout=10,
        )

        assert graphql["error"].startswith("Rate limited.")
        assert "Retry after 30s." in graphql["error"]
        assert "Retry after 60s." in rest["error"]
        assert [call.args for call in queue.set_backoff.call_args_list] == [
            (429,),
            (429,),
        ]

    async def test_token_the_app_route_was_refused_is_named(self, no_limiter):
        session = _Session(
            [wafer.TokenMintFailed("no token", stage="reddit_app", status_code=403)]
        )
        result = await fetch_reddit_graphql(user_profile_request("spez"), session, timeout=10)

        assert result == {
            "error": (
                "Reddit's UserProfile read needs wafer's anonymous Reddit app "
                "token, which Reddit did not grant (HTTP 403); retry later."
            )
        }

    async def test_blocked_graphql_is_a_refusal(self, no_limiter):
        session = _Session([wafer.RequestBlocked("reddit", GQL_URL, 403)])
        result = await fetch_reddit_graphql(user_profile_request("spez"), session, timeout=10)
        assert result["reddit_refused"] is True


class TestThreadOverGraphQL:
    async def test_thread_renders_post_from_by_id_and_nested_comments(self, no_limiter):
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/comments/abc123/the_post/"
        )
        forest = _forest(
            [
                _tree(_node("aa", "TOP COMMENT")),
                _tree(_node("bb", "NESTED REPLY"), depth=1, parent="t1_aa"),
                _tree(depth=1, parent="t1_aa",
                      more={"count": 3, "cursor": "commenttree:x,::1:d1:t1_aa"}),
                _tree(more={"count": 7, "cursor": "commenttree:y,::0:ex:,Wq$%"}),
            ]
        )
        session = _Session([_Response(_post_listing()), _gql(forest)])
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=5000, timeout=10)

        content = result["content"]
        assert "POST BODY" in content
        assert "42 score · 93% upvoted" in content
        assert "u/bob · 9 score" in content
        assert content.index("TOP COMMENT") < content.index("NESTED REPLY")
        assert "### ↳ u/bob" in content  # depth 1 marker
        load_more = [line for line in content.splitlines() if line.startswith("[Load")]
        assert len(load_more) == 2
        nested_url = load_more[0].split(": ", 1)[1].rstrip("]")
        top_url = load_more[1].split(": ", 1)[1].rstrip("]")
        assert load_more[0].startswith("[Load 3 more replies: ")
        assert load_more[1].startswith("[Load 7 more comments: ")
        assert parse_qs(urlparse(top_url).query)["after"] == [
            "commenttree:y,::0:ex:,Wq$%"
        ]
        # The continuation is a URL this module routes back to PostComments.
        follow = route_reddit_url(nested_url)
        assert follow.requests[-1].variables["after"] == "commenttree:x,::1:d1:t1_aa"
        assert [call[1] for call in session.calls] == [
            "https://api.reddit.com/by_id/t3_abc123.json?raw_json=1",
            GQL_URL,
        ]

    async def test_after_page_heads_itself_without_rereading_the_post(self, no_limiter):
        route = route_reddit_url(
            "https://www.reddit.com/comments/abc123/?after=commenttree%3Ay"
        )
        session = _Session([_gql(_forest([_tree(_node("cc", "LATER COMMENT"))]))])
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=5000, timeout=10)

        content = result["content"]
        assert content.startswith("# The post\n\nr/Python · more comments")
        assert "LATER COMMENT" in content
        assert len(session.calls) == 1

    async def test_missing_post_stops_before_the_comment_read(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        session = _Session([_Response({"kind": "Listing", "data": {"children": []}})])
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=5000, timeout=10)

        assert result["content"] == "# Reddit thread\n\nPost not found."
        assert len(session.calls) == 1

    async def test_sticky_resolves_the_redirect_and_keeps_caller_scope(self, no_limiter):
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/about/sticky/?limit=2&depth=1&sort=new"
        )
        redirect = _Response(
            None,
            302,
            {
                "location": (
                    "https://www.reddit.com/r/Python/comments/abc123/daily_thread/.json"
                )
            },
        )
        session = _Session(
            [redirect, _Response(_post_listing()), _gql(_forest([_tree(_node("aa"))]))]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=5000, timeout=10)

        assert "POST BODY" in result["content"]
        assert result["url"].startswith(
            "https://www.reddit.com/r/Python/comments/abc123/"
        )
        assert [call[1] for call in session.calls][:2] == [
            "https://api.reddit.com/r/Python/about/sticky.json?num=1&raw_json=1",
            "https://api.reddit.com/by_id/t3_abc123.json?raw_json=1",
        ]
        variables = session.calls[2][2]["json"]["variables"]
        assert (variables["count"], variables["maxDepth"], variables["sortType"]) == (
            2,
            1,
            "NEW",
        )

    async def test_refused_comment_read_is_reported_not_retried(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        session = _Session(
            [_Response(_post_listing()), wafer.RequestBlocked("reddit", GQL_URL, 403)]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=5000, timeout=10)

        assert result["error"] == f"Reddit thread comments: {REDDIT_REFUSED_MESSAGE}"
        assert len(session.calls) == 2


class TestProfilesOverGraphQL:
    async def test_profileless_account_is_dated_by_its_cake_day(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/user/Unidan/about/")
        session = _Session(
            [
                _gql(
                    {
                        "data": {
                            "redditorInfoByName": {
                                "__typename": "Redditor",
                                "id": "t2_3n71t",
                                "name": "Unidan",
                                "karma": {
                                    "fromPosts": 164836,
                                    "fromComments": 2338471.0,
                                    "total": 2503307.0,
                                },
                                "profile": None,
                            }
                        }
                    }
                ),
                _gql(
                    {
                        "data": {
                            "redditorsInfoByIds": [
                                {"id": "t2_3n71t", "cakeDayOn": "2009-09-23"}
                            ]
                        }
                    }
                ),
            ]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=2000, timeout=10)

        content = result["content"]
        assert "- **Post karma:** 164836" in content
        assert "- **Comment karma:** 2338471" in content
        assert "- **Created:** 2009-09-23" in content
        assert [call[2]["json"]["operationName"] for call in session.calls] == [
            "UserProfile",
            "GetMatrixChatUsersByIds",
        ]

    async def test_missing_user_is_a_content_state(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/user/ghost/about/")
        session = _Session([_gql({"data": {"redditorInfoByName": None}})])
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=2000, timeout=10)

        assert result["content"] == "# Reddit\n\nReddit user u/ghost not found."


class TestMorechildrenOverGraphQL:
    async def test_named_comments_are_read_in_one_batch_and_gaps_are_named(
        self, no_limiter
    ):
        route = route_reddit_url(
            "https://www.reddit.com/api/morechildren?link_id=t3_abc123"
            "&children=aa,gone"
        )
        session = _Session(
            [_gql({"data": {"commentsByIds": [_node("aa", "EXPANDED"), None]}})]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=2000, timeout=10)

        content = result["content"]
        assert "EXPANDED" in content
        assert "1 comments returned" in content
        assert "their own replies are not included" in content
        assert "t1_gone" in content
        assert len(session.calls) == 1


async def test_deadline_is_respected_before_any_graphql_post():
    session = _Session([])
    result = await fetch_reddit_graphql(user_profile_request("spez"), session, timeout=0)
    assert result["error"].startswith("Request timed out")
    assert session.calls == []
    assert time.monotonic() > 0


class TestMoreCommentsMode:
    """redditComments="more": Reddit caps a read at 200 comments, so a fuller
    thread is the forest's continuations followed in turn -- bounded."""

    @staticmethod
    def _page(*trees):
        return _gql(_forest(list(trees)))

    async def test_fast_reads_once_and_points_at_more(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        session = _Session(
            [
                _Response(_post_listing()),
                self._page(_tree(_node("aa")), _tree(more={"count": 9, "cursor": "c:top1"})),
            ]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(route, max_tokens=5000, timeout=60)

        assert len(session.calls) == 2
        assert 'fetch with redditComments "more"' in result["content"]
        assert "[Load 9 more comments: " in result["content"]

    async def test_more_follows_the_top_level_page_first_then_replies(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        session = _Session(
            [
                _Response(_post_listing()),
                self._page(
                    _tree(_node("aa", "FIRST TOP")),
                    _tree(depth=1, parent="t1_aa", more={"count": 2, "cursor": "c:aa"}),
                    _tree(more={"count": 1, "cursor": "c:top1"}),
                ),
                # top-level page
                self._page(_tree(_node("bb", "SECOND TOP"))),
                # replies under aa
                self._page(_tree(_node("cc", "REPLY TO FIRST"), depth=1, parent="t1_aa")),
            ]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(
                route, max_tokens=50_000, timeout=60, reddit_comments="more"
            )

        content = result["content"]
        afters = [call[2]["json"]["variables"].get("after") for call in session.calls[1:]]
        assert afters == [None, "c:top1", "c:aa"]
        assert all(
            call[2]["json"]["variables"]["count"] == 200 for call in session.calls[2:]
        )
        assert "focusedCommentContext" not in session.calls[2][2]["json"]["variables"]
        # Spliced where the stubs were: the reply nests under its parent and
        # the second top-level comment follows the first's thread.
        assert (
            content.index("FIRST TOP")
            < content.index("REPLY TO FIRST")
            < content.index("SECOND TOP")
        )
        assert "#### ↳ u/bob" in content
        assert "[Load" not in content
        assert (
            "[redditComments=more: followed 2 continuation reads; every comment "
            "Reddit lists here is loaded]"
        ) in content

    async def test_more_stops_at_the_read_cap_and_keeps_the_links(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        pages = [
            self._page(_tree(_node(f"p{i}", f"PAGE {i}")), _tree(more={"count": 50, "cursor": f"c:{i + 1}"}))
            for i in range(7)
        ]
        session = _Session([_Response(_post_listing()), *pages])
        with _patched_session(session):
            result = await fetch_mapped_reddit(
                route, max_tokens=50_000, timeout=120, reddit_comments="more"
            )

        assert len(session.calls) == 2 + 5
        assert "PAGE 5" in result["content"] and "PAGE 6" not in result["content"]
        assert "stopped because the 5-read limit was reached; the rest are linked below" in (
            result["content"]
        )
        assert "[Load 50 more comments: " in result["content"]

    async def test_more_never_reads_past_a_full_budget(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        session = _Session(
            [
                _Response(_post_listing()),
                self._page(
                    _tree(_node("aa", "x" * 3000)),
                    _tree(more={"count": 50, "cursor": "c:top1"}),
                ),
            ]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(
                route, max_tokens=500, timeout=60, reddit_comments="more"
            )

        assert len(session.calls) == 2
        assert "stopped because the output budget is full" in result["content"]

    async def test_more_stops_before_the_callers_timeout(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        session = _Session(
            [
                _Response(_post_listing()),
                self._page(_tree(_node("aa")), _tree(more={"count": 50, "cursor": "c:top1"})),
            ]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(
                route, max_tokens=50_000, timeout=5, reddit_comments="more"
            )

        assert len(session.calls) == 2
        assert "stopped because the request's timeout is near" in result["content"]

    async def test_focused_permalink_expands_only_that_thread(self, no_limiter):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/_/aa/")
        session = _Session(
            [
                _Response(_post_listing()),
                self._page(
                    _tree(_node("aa", "FOCUSED")),
                    _tree(depth=1, parent="t1_aa", more={"count": 1, "cursor": "c:aa"}),
                    _tree(more={"count": 40, "cursor": "c:rest-of-post"}),
                ),
                self._page(_tree(_node("cc", "FOCUSED REPLY"), depth=1, parent="t1_aa")),
            ]
        )
        with _patched_session(session):
            result = await fetch_mapped_reddit(
                route, max_tokens=50_000, timeout=60, reddit_comments="more"
            )

        assert [call[2]["json"]["variables"].get("after") for call in session.calls[2:]] == [
            "c:aa"
        ]
        assert "FOCUSED REPLY" in result["content"]

    async def test_unknown_mode_is_rejected(self):
        route = route_reddit_url("https://www.reddit.com/comments/abc123/")
        result = await fetch_mapped_reddit(
            route, max_tokens=5000, timeout=10, reddit_comments="all"
        )
        assert result == {"error": 'redditComments must be "fast" or "more".'}


class TestLiveShapes:
    """Shapes seen live on 2026-10-31 that the first fixtures did not have."""

    def test_deleted_comments_carry_no_id_and_are_placed_by_depth(self):
        # The app's deletedCommentFragment selects no id.
        listing = comment_forest_listing(
            {
                "trees": [
                    _tree({"__typename": "DeletedComment", "removedByCategory": "DELETED",
                           "createdAt": "2026-10-30T15:30:42.876000+0000"}),
                    _tree(_node("bb", "REPLY TO DELETED"), depth=1, parent="t1_dd"),
                    _tree({"__typename": "DeletedComment", "removedByCategory": "MODERATOR"}),
                    _tree(_node("cc", "NEXT TOP")),
                ]
            },
            post_id="abc123",
        )

        assert listing is not None and _is_comment_listing_payload(listing)
        top = listing["data"]["children"]
        assert [child["data"]["body"] for child in top] == ["[deleted]", "[removed]", "NEXT TOP"]
        # Its reply named it, so the deleted comment gets its real id back.
        assert top[0]["data"]["id"] == "dd"
        reply = top[0]["data"]["replies"]["data"]["children"][0]["data"]
        assert reply["body"] == "REPLY TO DELETED" and reply["parent_id"] == "t1_dd"
        # One with no replies keeps no id, and so no invented permalink.
        assert "id" not in top[1]["data"] and "permalink" not in top[1]["data"]

    def test_a_comment_node_without_an_id_is_still_malformed(self):
        assert comment_forest_listing(
            {"trees": [_tree({"__typename": "Comment", "content": {"markdown": "x"}})]},
            post_id="abc123",
        ) is None

    def test_posts_by_ids_converts_card_fields_and_skips_missing(self):
        from fetchaller.content.reddit_graphql import posts_by_ids_things
        from fetchaller.tools.reddit_fetch import _is_post_data

        node = {
            "__typename": "SubredditPost",
            "id": "t3_1wg3ez4",
            "postTitle": "Trump may allow Ukraine",
            "authorInfo": {"__typename": "Redditor", "name": "pravda_eng_official"},
            "createdAt": "2026-09-14T13:26:47.548000+0000",
            "editedAt": None,
            "score": 352,
            "upvoteRatio": 0.8636363636363636,
            "commentCount": 93,
            "permalink": "/r/worldnews/comments/1wg3ez4/trump_may_allow/",
            "subreddit": {"name": "worldnews"},
            "isSelfPost": False,
            "url": "https://www.pravda.com.ua/eng/news/2026/09/14/8053336/",
            "domain": "pravda.com.ua",
            "content": {},
            "isNsfw": True,
            "flair": {"text": "Russia/Ukraine", "richtext": None},
            "authorFlair": {
                "richtext": '[{"a":":verified:","e":"emoji","u":"https://emoji.redditmedia.com/v"},'
                '{"e":"text","t":" Ukrainska Pravda"}]'
            },
            "gallery": {"items": [{"id": "m1", "caption": "One",
                                   "media": {"__typename": "ImageAsset", "url": "https://i.redd.it/1.jpg"}}]},
            "media": {"streaming": {"hlsUrl": "https://v.redd.it/x/HLS.m3u8", "dashUrl": None}},
        }
        result = posts_by_ids_things(
            {"data": {"postsInfoByIds": [node, None]}},
            ["t3_1wg3ez4", "t3_gone1"],
        )

        assert set(result["things"]) == {"t3_1wg3ez4"}
        data = result["things"]["t3_1wg3ez4"]["data"]
        assert _is_post_data(data)
        assert data["title"] == "Trump may allow Ukraine"
        assert (data["score"], data["upvote_ratio"], data["num_comments"]) == (352, 0.8636363636363636, 93)
        assert data["author"] == "pravda_eng_official"
        assert data["subreddit"] == "worldnews" and data["is_self"] is False
        assert data["over_18"] is True
        assert data["link_flair_text"] == "Russia/Ukraine"
        assert data["author_flair_richtext"][1]["t"] == " Ukrainska Pravda"
        assert data["media_metadata"] == {"m1": {"s": {"u": "https://i.redd.it/1.jpg"}}}
        assert data["gallery_data"] == {"items": [{"media_id": "m1", "caption": "One"}]}
        assert data["secure_media"] == {"reddit_video": {"hls_url": "https://v.redd.it/x/HLS.m3u8"}}
        assert "selftext" not in data

    def test_substituted_post_is_an_error(self):
        from fetchaller.content.reddit_graphql import posts_by_ids_things

        result = posts_by_ids_things(
            {"data": {"postsInfoByIds": [{"id": "t3_other1", "postTitle": "x"}]}},
            ["t3_asked1"],
        )
        assert result == {
            "error": "Reddit's post lookup returned an invalid or substituted post."
        }


class TestRedditSortOption:
    """fetch's optional ``redditSort``: best (default), top, new, old,
    controversial, qa."""

    def test_best_is_reddits_confidence_and_the_default(self):
        from fetchaller.content.reddit import route_reddit_url_with_sort

        default = route_reddit_url_with_sort("https://www.reddit.com/comments/abc123/")
        best = route_reddit_url_with_sort(
            "https://www.reddit.com/comments/abc123/", sort="best"
        )
        url_best = route_reddit_url("https://www.reddit.com/comments/abc123/?sort=best")
        for route in (default, best, url_best):
            assert route.requests[-1].variables["sortType"] == "CONFIDENCE"

    @pytest.mark.parametrize(
        ("sort", "enum"),
        [("top", "TOP"), ("new", "NEW"), ("old", "OLD"),
         ("controversial", "CONTROVERSIAL"), ("qa", "QA")],
    )
    def test_option_wins_over_the_url_and_stays_on_the_canonical_url(self, sort, enum):
        from fetchaller.content.reddit import route_reddit_url_with_sort

        route = route_reddit_url_with_sort(
            "https://www.reddit.com/r/Python/comments/abc123/title/?sort=new&limit=3",
            sort=sort,
        )
        assert route.kind == "thread"
        assert route.requests[-1].variables["sortType"] == enum
        assert route.requests[-1].variables["count"] == 3
        assert parse_qs(urlparse(route.canonical_url).query)["sort"] == [sort]

    def test_option_never_resorts_a_listing(self):
        from fetchaller.content.reddit import route_reddit_url_with_sort

        listing = route_reddit_url_with_sort(
            "https://www.reddit.com/r/Python/top/?t=week", sort="new"
        )
        assert listing.kind == "listing"
        assert urlparse(listing.requests[0]).path == "/r/Python/top.json"

    def test_url_sorts_outside_the_option_still_work(self):
        for sort in ("random", "live", "confidence"):
            route = route_reddit_url(f"https://www.reddit.com/comments/abc123/?sort={sort}")
            assert route.requests[-1].variables["sortType"] == sort.upper()

    async def test_fetch_rejects_a_sort_outside_the_option(self):
        from fetchaller.tools.fetch import fetch_url

        result = await fetch_url(
            "https://www.reddit.com/comments/abc123/", reddit_sort="random"
        )
        assert result == {
            "error": "redditSort must be one of: best, top, new, old, controversial, qa."
        }

    def test_server_publishes_and_validates_the_option(self):
        from fetchaller import server

        assert server._TOOL_ENUMS[("fetch", "redditSort")] == {
            "best", "top", "new", "old", "controversial", "qa",
        }
        assert "redditSort" in server._TOOL_ARGUMENTS["fetch"]
