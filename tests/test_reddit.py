"""New Reddit anonymous JSON routing, rendering, and tool parity."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from html import escape
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, quote, urlparse

import pytest
import wafer

from fetchaller.config import Config
from fetchaller.content.reddit import (
    RedditRoute,
    canonicalize_reddit_links,
    format_reddit_post,
    parse_reddit_related_html,
    parse_reddit_wiki_pages_html,
    render_reddit_route,
    route_reddit_url,
)
from fetchaller.tools.browse_reddit import (
    _get_session,
    _instrument_reddit_session,
    _reddit_json_transport_url,
    _validated_reddit_json_url,
    browse_reddit,
    close_session,
    fetch_reddit_json,
    format_reddit_http_error,
    reddit_session_audit,
)
from fetchaller.tools.reddit_auth import (
    RedditModeratorOAuth,
    reset_reddit_moderator_oauth,
)
from fetchaller.tools.reddit_fetch import (
    _hydrate_user_directory,
    _parse_archived_collection,
    _parse_archived_gilded,
    _parse_collection_cdx,
    _payload_schema_error,
    _restore_reverse_listing_after,
    fetch_mapped_reddit,
)
from fetchaller.tools.search_reddit import search_reddit


@pytest.fixture(autouse=True)
def _reset_moderator_oauth_state():
    reset_reddit_moderator_oauth()
    yield
    reset_reddit_moderator_oauth()


def _post(**overrides) -> dict:
    data = {
        "id": "post1",
        "title": "A compact Reddit post",
        "subreddit": "Python",
        "subreddit_name_prefixed": "r/Python",
        "author": "alice",
        "score": 123,
        "upvote_ratio": 0.96,
        "num_comments": 4,
        "created_utc": 1_700_000_000,
        "selftext": "Paragraph.\n\n- item\n\n```python\nprint('kept')\n```",
        "is_self": True,
        "permalink": "/r/Python/comments/post1/a_compact_reddit_post/",
        "url": "https://www.reddit.com/r/Python/comments/post1/a_compact_reddit_post/",
    }
    data.update(overrides)
    return {"kind": "t3", "data": data}


def _transport_url(url: str) -> str:
    transformed = _reddit_json_transport_url(url)
    assert transformed is not None
    return transformed


def _comment(comment_id: str, body: str, *, replies: list[dict] | None = None, **overrides) -> dict:
    data = {
        "id": comment_id,
        "author": "bob",
        "score": 9,
        "created_utc": 1_700_000_100,
        "body": body,
        "body_html": f'<div class="md"><p>{escape(body)}</p></div>',
        "replies": {"kind": "Listing", "data": {"children": replies or []}} if replies is not None else "",
    }
    data.update(overrides)
    return {"kind": "t1", "data": data}


def _thread(post: dict, comments: list[dict]) -> list[dict]:
    return [
        {"kind": "Listing", "data": {"children": [post]}},
        {"kind": "Listing", "data": {"children": comments}},
    ]


def _moderator_route() -> RedditRoute:
    return RedditRoute(
        "https://www.reddit.com/r/Python/about/moderators/",
        "moderators",
        (
            "https://www.reddit.com/r/Python/about/moderators.json?"
            "limit=500&raw_json=1",
        ),
        subreddit="Python",
    )


def _moderators_payload() -> dict:
    return {
        "kind": "UserList",
        "data": {
            "children": [
                {
                    "name": "exact_mod",
                    "mod_permissions": ["posts", "wiki"],
                    "date": 1_700_000_000,
                }
            ]
        },
    }


class TestRedditRouting:
    @pytest.mark.parametrize(
        ("url", "request_path"),
        [
            (
                "https://www.reddit.com/randomrising/",
                "/r/all/rising.json",
            ),
            (
                "https://www.reddit.com/r/Python/randomrising/",
                "/r/Python/rising.json",
            ),
        ],
    )
    def test_randomrising_uses_live_rising_source_with_distinct_scope(
        self,
        url,
        request_path,
    ):
        route = route_reddit_url(url)

        assert route is not None
        assert route.kind == "listing"
        assert route.label == "randomrising"
        assert urlparse(route.requests[0]).path == request_path
        assert parse_qs(urlparse(route.requests[0]).query)["limit"] == ["100"]

    def test_thread_and_comment_permalink_preserve_context(self):
        route = route_reddit_url(
            "https://old.reddit.com/r/Python/comments/abc123/title/def456/"
            "?context=5&sort=new&utm_source=test",
            max_tokens=800,
        )

        assert route is not None
        assert route.kind == "thread"
        assert route.canonical_url.startswith("https://www.reddit.com/")
        assert route.selected_comment_id == "def456"
        assert all("old.reddit.com" not in request for request in route.requests)
        parsed = urlparse(route.requests[0])
        params = parse_qs(parsed.query)
        assert parsed.path == "/r/Python/comments/abc123.json"
        assert params == {
            "raw_json": ["1"],
            "sort": ["new"],
            "limit": ["8"],
            "depth": ["1"],
            "comment": ["def456"],
            "context": ["5"],
        }
        assert "utm_source" not in route.requests[0]

    def test_thread_without_subreddit_maps_to_json(self):
        route = route_reddit_url("https://reddit.com/comments/abc123/")

        assert route is not None
        assert urlparse(route.requests[0]).path == "/comments/abc123.json"

    def test_percent_encoded_safe_path_parts_are_decoded_once(self):
        route = route_reddit_url("https://www.reddit.com/r/%50ython/wiki/getting%2Dstarted/")

        assert route is not None
        assert route.requests == (
            "https://www.reddit.com/r/Python/wiki/getting-started.json?raw_json=1",
        )

    def test_public_url_matrix(self):
        cases = {
            "https://www.reddit.com/r/Python/": ("listing", "/r/Python/hot.json"),
            "https://www.reddit.com/r/Python/top/?t=week": ("listing", "/r/Python/top.json"),
            "https://www.reddit.com/r/Python/comments/": ("comment_listing", "/r/Python/comments.json"),
            "https://www.reddit.com/r/Python/search/?q=asyncio": ("search", "/r/Python/search.json"),
            "https://www.reddit.com/search/?q=asyncio": ("search", "/search.json"),
            "https://www.reddit.com/user/spez/about/": ("user_about", "/user/spez/about.json"),
            "https://www.reddit.com/user/spez/overview/": ("user_listing", "/user/spez/overview.json"),
            "https://www.reddit.com/user/spez/submitted/": ("user_listing", "/user/spez/submitted.json"),
            "https://www.reddit.com/u/spez/comments/": ("user_listing", "/user/spez/comments.json"),
            "https://www.reddit.com/r/Python/about/": ("subreddit_about", "/r/Python/about.json"),
            "https://www.reddit.com/r/Python/about/rules/": ("rules", "/r/Python/about/rules.json"),
            "https://www.reddit.com/r/redditdev/wiki/oauth2/": ("wiki", "/r/redditdev/wiki/oauth2.json"),
            "https://www.reddit.com/r/redditdev/wiki/": ("wiki", "/r/redditdev/wiki/index.json"),
            "https://www.reddit.com/domain/github.com/": (
                "domain_listing",
                "/domain/github.com/hot.json",
            ),
            "https://www.reddit.com/domain/i.redd.it/top/?t=week": (
                "domain_listing",
                "/domain/i.redd.it/top.json",
            ),
        }

        for url, (kind, path) in cases.items():
            route = route_reddit_url(url)
            assert route is not None, url
            assert route.kind == kind, url
            assert urlparse(route.requests[0]).path == path, url

        domain_top = route_reddit_url("https://www.reddit.com/domain/i.redd.it/top/?t=week")
        assert domain_top is not None
        assert parse_qs(urlparse(domain_top.requests[0]).query)["t"] == ["week"]

    def test_invalid_domain_listing_falls_back_without_unsafe_mapping(self):
        route = route_reddit_url("https://www.reddit.com/domain/not_a_domain/")

        assert route is not None
        assert route.kind == "html_fallback"
        assert route.requests == ()

    def test_profile_root_uses_about_and_bounded_overview(self):
        route = route_reddit_url("https://www.reddit.com/user/spez/?after=t3_abc")

        assert route is not None
        assert route.kind == "user_profile"
        assert route.requests == (
            "https://www.reddit.com/user/spez/about.json?raw_json=1",
            "https://www.reddit.com/user/spez/overview.json?limit=250&raw_json=1&after=t3_abc",
            "https://www.reddit.com/user/spez/trophies.json?raw_json=1",
            "https://www.reddit.com/api/multi/user/spez.json?raw_json=1",
            "https://www.reddit.com/user/spez/moderated_subreddits.json?raw_json=1",
        )

    def test_invalid_sorts_cursors_and_tracking_are_discarded(self):
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/top/?t=decade&after=evil&before=t3_good"
            "&limit=999&utm_campaign=x"
        )

        assert route is not None
        params = parse_qs(urlparse(route.requests[0]).query)
        assert params == {
            "limit": ["250"],
            "raw_json": ["1"],
            "before": ["t3_good"],
        }

    def test_transfer_bounds_scale_to_the_public_thread_baseline(self):
        small = route_reddit_url(
            "https://www.reddit.com/comments/abc123/",
            max_tokens=800,
        )
        large = route_reddit_url(
            "https://www.reddit.com/comments/abc123/",
            max_tokens=250_000,
        )

        assert small is not None and large is not None
        assert parse_qs(urlparse(small.requests[0]).query)["limit"] == ["8"]
        large_params = parse_qs(urlparse(large.requests[0]).query)
        assert large_params["limit"] == ["500"]
        assert large_params["depth"] == ["10"]

    def test_lower_public_limits_depth_and_show_all_are_preserved(self):
        listing = route_reddit_url(
            "https://www.reddit.com/r/Python/top/"
            "?limit=7&show=all&t=week"
        )
        thread = route_reddit_url(
            "https://www.reddit.com/comments/abc123/"
            "?limit=6&depth=2"
        )
        wiki = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/revisions/"
            "?limit=4&show=all"
        )
        live = route_reddit_url(
            "https://www.reddit.com/live/abc123/?limit=3&show=all"
        )

        assert (
            listing is not None
            and thread is not None
            and wiki is not None
            and live is not None
        )
        assert parse_qs(urlparse(listing.requests[0]).query) == {
            "limit": ["7"],
            "raw_json": ["1"],
            "show": ["all"],
            "t": ["week"],
        }
        thread_params = parse_qs(urlparse(thread.requests[0]).query)
        assert thread_params["limit"] == ["6"]
        assert thread_params["depth"] == ["2"]
        wiki_params = parse_qs(urlparse(wiki.requests[0]).query)
        assert wiki_params["limit"] == ["4"]
        assert wiki_params["show"] == ["all"]
        live_params = parse_qs(urlparse(live.requests[1]).query)
        assert live_params["limit"] == ["3"]
        assert live_params["show"] == ["all"]

    def test_public_query_controls_that_change_results_are_preserved(self):
        search_query_id = "8d253a42-57c7-11f1-b49a-ae675b7b52c3"
        hot = route_reddit_url(
            "https://www.reddit.com/hot/?g=CA&limit=4&sr_detail=true"
        )
        duplicates = route_reddit_url(
            "https://www.reddit.com/duplicates/abc123/"
            "?sort=num_comments&crossposts_only=true&sr=Python&limit=3"
        )
        search = route_reddit_url(
            "https://www.reddit.com/r/Python/search/"
            "?q=asyncio&restrict_sr=false&category=dev"
            "&include_facets=true&type=link,user&sr_detail=false"
        )
        activity = route_reddit_url(
            "https://www.reddit.com/user/alice/gilded/"
            "?show=given&context=2"
        )
        directory = route_reddit_url(
            "https://www.reddit.com/subreddits/search/"
            "?q=python&sort=activity&show_users=true&typeahead_active=false"
            f"&search_query_id={search_query_id}&sr_detail=true"
        )

        assert (
            hot is not None
            and duplicates is not None
            and search is not None
            and activity is not None
            and directory is not None
        )
        assert parse_qs(urlparse(hot.requests[0]).query)["g"] == ["CA"]
        assert parse_qs(urlparse(hot.requests[0]).query)["sr_detail"] == ["true"]
        duplicate_params = parse_qs(urlparse(duplicates.requests[0]).query)
        assert duplicate_params["sort"] == ["num_comments"]
        assert duplicate_params["crossposts_only"] == ["true"]
        assert duplicate_params["sr"] == ["Python"]
        assert duplicate_params["limit"] == ["3"]
        search_params = parse_qs(urlparse(search.requests[0]).query)
        assert search_params["q"] == ["asyncio"]
        assert search_params["restrict_sr"] == ["false"]
        assert search_params["category"] == ["dev"]
        assert search_params["include_facets"] == ["true"]
        assert search_params["type"] == ["link,user"]
        assert search_params["sr_detail"] == ["false"]
        activity_params = parse_qs(urlparse(activity.requests[0]).query)
        assert activity_params["show"] == ["given"]
        assert activity_params["context"] == ["2"]
        directory_params = parse_qs(urlparse(directory.requests[0]).query)
        assert directory_params["q"] == ["python"]
        assert directory_params["sort"] == ["activity"]
        assert directory_params["show_users"] == ["true"]
        assert directory_params["typeahead_active"] == ["false"]
        assert directory_params["search_query_id"] == [search_query_id]
        assert directory_params["sr_detail"] == ["true"]

    def test_overlong_search_query_never_turns_into_an_unrelated_listing(self):
        route = route_reddit_url(
            "https://www.reddit.com/search/?q=" + "x" * 513
        )

        assert route is not None
        assert route.kind == "html_fallback"
        assert not route.requests

    def test_thread_representation_controls_are_typed_and_bounded(self):
        route = route_reddit_url(
            "https://www.reddit.com/comments/abc123/"
            "?threaded=false&showmore=true&showmedia=false"
            "&showtitle=false&showedits=true&truncate=999"
        )
        invalid = route_reddit_url(
            "https://www.reddit.com/comments/abc123/"
            "?threaded=maybe&showmore=yes"
        )

        assert route is not None and invalid is not None
        params = parse_qs(urlparse(route.requests[0]).query)
        assert params["threaded"] == ["false"]
        assert params["showmore"] == ["true"]
        assert params["showmedia"] == ["false"]
        assert params["showtitle"] == ["false"]
        assert params["showedits"] == ["true"]
        assert params["truncate"] == ["50"]
        invalid_params = parse_qs(urlparse(invalid.requests[0]).query)
        assert "threaded" not in invalid_params
        assert "showmore" not in invalid_params

    def test_limits_cannot_expand_the_budget_derived_transfer_bound(self):
        listing = route_reddit_url(
            "https://www.reddit.com/r/Python/?limit=999",
            max_tokens=800,
        )
        thread = route_reddit_url(
            "https://www.reddit.com/comments/abc123/?limit=999&depth=999",
            max_tokens=800,
        )

        assert listing is not None and thread is not None
        assert parse_qs(urlparse(listing.requests[0]).query)["limit"] == ["8"]
        thread_params = parse_qs(urlparse(thread.requests[0]).query)
        assert thread_params["limit"] == ["8"]
        assert thread_params["depth"] == ["1"]

    def test_thread_comment_query_preserves_selected_comment_context(self):
        route = route_reddit_url(
            "https://www.reddit.com/comments/abc123/"
            "?comment=def456&context=7"
        )

        assert route is not None
        assert route.selected_comment_id == "def456"
        assert parse_qs(urlparse(route.requests[0]).query) == {
            "sort": ["confidence"],
            "limit": ["250"],
            "depth": ["10"],
            "comment": ["def456"],
            "context": ["7"],
            "raw_json": ["1"],
        }

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.reddit.com/gallery/abc123?limit=2&depth=1&sort=new",
            (
                "https://www.reddit.com/r/Python/about/sticky/"
                "?limit=2&depth=1&sort=new"
            ),
        ],
    )
    def test_thread_like_routes_preserve_lower_caller_scope(self, url):
        route = route_reddit_url(url)

        assert route is not None
        params = parse_qs(urlparse(route.requests[0]).query)
        assert params["limit"] == ["2"]
        assert params["depth"] == ["1"]
        assert params["sort"] == ["new"]

    def test_profile_sorts_and_vote_histories_match_public_routes(self):
        for sort in ("hot", "new", "top", "controversial"):
            route = route_reddit_url(
                f"https://www.reddit.com/user/alice/overview/?sort={sort}"
            )
            assert route is not None
            assert parse_qs(urlparse(route.requests[0]).query)["sort"] == [sort]
        for invalid in ("best", "rising", "randomrising"):
            route = route_reddit_url(
                f"https://www.reddit.com/user/alice/overview/?sort={invalid}"
            )
            assert route is not None
            assert "sort" not in parse_qs(urlparse(route.requests[0]).query)
        for activity in ("upvoted", "downvoted"):
            route = route_reddit_url(
                f"https://www.reddit.com/user/alice/{activity}/"
            )
            assert route is not None
            assert route.kind == "user_listing"
            assert urlparse(route.requests[0]).path.endswith(f"/{activity}.json")

    def test_search_type_directory_sort_and_wiki_count_are_preserved(self):
        search = route_reddit_url(
            "https://www.reddit.com/search/?q=python&type=sr,user,link"
        )
        directory = route_reddit_url(
            "https://www.reddit.com/subreddits/search/?q=python&sort=activity"
        )
        wiki = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/revisions/page/"
            "?after=WikiRevision_8d253a42-57c7-11f1-b49a-ae675b7b52c3&count=40"
        )

        assert search is not None and directory is not None and wiki is not None
        assert parse_qs(urlparse(search.requests[0]).query)["type"] == [
            "sr,user,link"
        ]
        assert parse_qs(urlparse(directory.requests[0]).query)["sort"] == [
            "activity"
        ]
        assert parse_qs(urlparse(wiki.requests[0]).query)["count"] == ["40"]

    def test_by_id_accepts_comments_posts_and_mixed_validated_fullnames(self):
        valid = route_reddit_url(
            "https://www.reddit.com/by_id/t3_ab%20t1_cd,t3_ef/"
        )
        invalid = route_reddit_url("https://www.reddit.com/by_id/t1_ab,t2_cd/")

        assert valid is not None
        assert valid.kind == "listing"
        assert urlparse(valid.requests[0]).path == "/api/info.json"
        assert parse_qs(urlparse(valid.requests[0]).query)["id"] == ["t3_ab,t1_cd,t3_ef"]
        assert valid.label == "items by ID"
        assert invalid is not None and invalid.kind == "html_fallback"

    def test_morechildren_continuation_maps_to_bounded_anonymous_get(self):
        route = route_reddit_url(
            "https://www.reddit.com/api/morechildren?"
            "link_id=t3_abc123&children=def456,ghi789&sort=top&depth=4"
            "&id=more123&limit_children=false",
            max_tokens=1000,
        )

        assert route is not None
        assert route.kind == "morechildren"
        parsed = urlparse(route.requests[0])
        assert parsed.path == "/api/morechildren.json"
        assert parse_qs(parsed.query) == {
            "link_id": ["t3_abc123"],
            "children": ["def456,ghi789"],
            "sort": ["top"],
            "depth": ["2"],
            "id": ["more123"],
            "limit_children": ["false"],
            "api_type": ["json"],
            "raw_json": ["1"],
        }

    def test_explicit_json_and_unmapped_routes_stay_distinct(self):
        explicit = route_reddit_url("https://www.reddit.com/r/Python/hot.json?limit=1")
        fallback = route_reddit_url("https://www.reddit.com/settings/privacy/")

        assert explicit is not None and explicit.is_explicit_json and not explicit.requests
        assert fallback is not None and fallback.kind == "html_fallback" and not fallback.requests

    @pytest.mark.parametrize(
        ("url", "kind", "path"),
        [
            ("https://www.reddit.com/gallery/abc123", "thread", "/comments/abc123.json"),
            (
                "https://www.reddit.com/subreddits/popular/",
                "subreddit_directory",
                "/subreddits/popular.json",
            ),
            (
                "https://www.reddit.com/users/search/?q=alice",
                "user_directory",
                "/users/search.json",
            ),
            (
                "https://www.reddit.com/subreddits/search/?q=python",
                "subreddit_directory",
                "/subreddits/search.json",
            ),
            ("https://www.reddit.com/reddits/", "subreddit_directory", "/subreddits/popular.json"),
            ("https://www.reddit.com/r/Python/gilded/", "comment_listing", "/r/Python/gilded.json"),
            (
                "https://www.reddit.com/r/Python/about/moderators/",
                "moderators",
                "/r/Python/about/moderators.json",
            ),
            (
                "https://www.reddit.com/user/alice/m/public/top/?t=year",
                "multi_profile",
                "/api/multi/user/alice/m/public.json",
            ),
            ("https://www.reddit.com/duplicates/abc123/", "duplicates", "/duplicates/abc123.json"),
            (
                "https://www.reddit.com/r/Python/duplicates/abc123/title/",
                "duplicates",
                "/duplicates/abc123.json",
            ),
            ("https://www.reddit.com/related/abc123/", "related", "/api/info.json"),
            (
                "https://www.reddit.com/by_id/t3_ab,t3_cd/",
                "listing",
                "/by_id/t3_ab,t3_cd.json",
            ),
            (
                "https://www.reddit.com/r/Python/wiki/pages/",
                "wiki_pages",
                "/r/Python/wiki/pages/",
            ),
            (
                "https://www.reddit.com/r/Python/wiki/revisions/page/",
                "wiki_revisions",
                "/r/Python/wiki/revisions/page.json",
            ),
            (
                "https://www.reddit.com/r/Python/wiki/discussions/page/",
                "wiki_discussions",
                "/r/Python/wiki/discussions/page.json",
            ),
            (
                "https://www.reddit.com/live/abc123/",
                "live",
                "/live/abc123/about.json",
            ),
            (
                "https://www.reddit.com/live/abc123/about/",
                "live_about",
                "/live/abc123/about.json",
            ),
            (
                "https://www.reddit.com/live/abc123/discussions/?limit=2",
                "listing",
                "/live/abc123/discussions.json",
            ),
            (
                "https://www.reddit.com/live/abc123/contributors/",
                "live_contributors",
                "/live/abc123/contributors.json",
            ),
            (
                "https://www.reddit.com/live/abc123/updates/"
                "8d253a42-57c7-11f1-b49a-ae675b7b52c3/",
                "live_update",
                "/live/abc123/updates/8d253a42-57c7-11f1-b49a-ae675b7b52c3.json",
            ),
            (
                "https://www.reddit.com/r/Python/collection/"
                "36910c41-231f-45ea-8057-a4e061048541/",
                "collection",
                "/api/v1/collections/collection",
            ),
        ],
    )
    def test_old_reddit_public_surface_matrix_has_structured_mapping(self, url, kind, path):
        route = route_reddit_url(url)

        assert route is not None
        assert route.kind == kind
        assert route.requests
        assert urlparse(route.requests[0]).path == path
        for request in route.requests:
            if kind == "wiki_pages":
                assert request == (
                    "https://www.reddit.com/r/Python/wiki/pages/"
                )
            else:
                assert _validated_reddit_json_url(request) is not None
                assert parse_qs(urlparse(request).query).get(
                    "raw_json"
                ) == ["1"]
        if kind == "collection":
            params = parse_qs(urlparse(route.requests[0]).query)
            assert params["collection_id"] == [
                "36910c41-231f-45ea-8057-a4e061048541"
            ]
            assert params["include_links"] == ["true"]
        if kind == "multi_profile":
            assert urlparse(route.requests[1]).path == "/user/alice/m/public/top.json"

    def test_route_specific_opaque_cursors_round_trip(self):
        live_cursor = "LiveUpdate_8d253a42-57c7-11f1-b49a-ae675b7b52c3"
        wiki_cursor = "WikiRevision_0136a1c0-57c7-11f1-b49a-ae675b7b52c3"

        live = route_reddit_url(f"https://www.reddit.com/live/abc123/?after={live_cursor}")
        wiki = route_reddit_url(
            f"https://www.reddit.com/r/Python/wiki/revisions/?after={wiki_cursor}"
        )

        assert live is not None
        assert parse_qs(urlparse(live.requests[1]).query)["after"] == [live_cursor]
        assert wiki is not None
        assert parse_qs(urlparse(wiki.requests[0]).query)["after"] == [wiki_cursor]

    def test_conflicting_direction_cursors_never_reach_reddit_together(self):
        cases = [
            (
                "https://www.reddit.com/r/Python/?after=t3_next&before=t3_prev",
                0,
            ),
            (
                "https://www.reddit.com/live/abc123/?"
                "after=LiveUpdate_8d253a42-57c7-11f1-b49a-ae675b7b52c3&"
                "before=LiveUpdate_9d253a42-57c7-11f1-b49a-ae675b7b52c3",
                1,
            ),
            (
                "https://www.reddit.com/r/Python/wiki/revisions/?"
                "after=WikiRevision_0136a1c0-57c7-11f1-b49a-ae675b7b52c3&"
                "before=WikiRevision_1136a1c0-57c7-11f1-b49a-ae675b7b52c3",
                0,
            ),
        ]
        for url, request_index in cases:
            route = route_reddit_url(url)
            assert route is not None
            params = parse_qs(urlparse(route.requests[request_index]).query)
            assert "after" in params
            assert "before" not in params

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.reddit.com/r/Python/wiki/pages/extra/",
            "https://www.reddit.com/r/Python/wiki/discussions/",
            "https://www.reddit.com/live/a/updates/",
            "https://www.reddit.com/by_id/t3_ok,garbage/",
            "https://www.reddit.com/r/Python/comments/abc/title/comment/extra/",
        ],
    )
    def test_malformed_structured_tails_fall_back_instead_of_truncating(self, url):
        route = route_reddit_url(url)

        assert route is not None
        assert route.kind == "html_fallback"
        assert route.requests == ()

    def test_nested_wiki_revision_and_discussion_paths_are_preserved(self):
        revisions = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/revisions/"
            "getting.started/%E2%9C%93/"
        )
        discussions = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/discussions/"
            "getting.started/%E2%9C%93/"
        )

        assert revisions is not None
        assert urlparse(revisions.requests[0]).path == (
            "/r/Python/wiki/revisions/getting.started/%E2%9C%93.json"
        )
        assert discussions is not None
        assert urlparse(discussions.requests[0]).path == (
            "/r/Python/wiki/discussions/getting.started/%E2%9C%93.json"
        )
        assert revisions.label == "getting.started/✓"
        assert discussions.label == "getting.started/✓"

    def test_wiki_revision_comparison_fetches_both_public_revisions(self):
        route = route_reddit_url(
            "https://www.reddit.com/r/redditdev/wiki/index/"
            "?v=revision_one&v2=revision_two"
        )

        assert route is not None
        assert route.kind == "wiki_diff"
        assert len(route.requests) == 2
        assert [
            parse_qs(urlparse(request).query)
            for request in route.requests
        ] == [
            {"v": ["revision_one"], "raw_json": ["1"]},
            {"v": ["revision_two"], "raw_json": ["1"]},
        ]

        same = route_reddit_url(
            "https://www.reddit.com/r/redditdev/wiki/index/"
            "?v=revision_one&v2=revision_one"
        )
        assert same is not None
        assert same.kind == "wiki_diff"
        assert len(same.requests) == 1

    def test_unknown_c_path_is_not_reinterpreted_as_a_comment(self):
        # Reddit serves /c/{id} as a normal not-found page, even when {id} is
        # the ID of a real comment. Comment permalinks use the documented
        # /r/{subreddit}/comments/{post}/{slug}/{comment}/ form.
        route = route_reddit_url("https://www.reddit.com/c/ozc4si9")

        assert route is not None
        assert route.kind == "html_fallback"
        assert not route.requests

    def test_non_content_reddit_subdomain_is_not_reinterpreted_as_www(self):
        route = route_reddit_url("https://oauth.reddit.com/api/v1/me")

        assert route is not None
        assert route.kind == "html_fallback"
        assert route.canonical_url == "https://oauth.reddit.com/api/v1/me"
        assert not route.requests

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.reddit.com:8443/r/Python/",
            "https://www.reddit.com/r/Python;alternate/",
        ],
    )
    def test_nonstandard_reddit_authority_or_path_params_are_not_reinterpreted(
        self,
        url,
    ):
        route = route_reddit_url(url)

        assert route is not None
        assert route.kind == "html_fallback"
        assert route.canonical_url == url
        assert not route.requests

    @pytest.mark.parametrize(
        ("url", "kind", "path"),
        [
            (
                "https://www.reddit.com/comments/",
                "comment_listing",
                "/r/all/comments.json",
            ),
            ("https://www.reddit.com/gilded/", "comment_listing", "/gilded.json"),
            (
                "https://www.reddit.com/comments/gilded/",
                "comment_listing",
                "/comments/gilded.json",
            ),
            (
                "https://www.reddit.com/r/Python/comments/gilded/",
                "comment_listing",
                "/r/Python/comments/gilded.json",
            ),
            ("https://www.reddit.com/r/", "subreddit_directory", "/subreddits/popular.json"),
            (
                "https://www.reddit.com/subreddits/",
                "subreddit_directory",
                "/subreddits/popular.json",
            ),
            (
                "https://www.reddit.com/reddits/banned/",
                "subreddit_directory",
                "/subreddits/banned.json",
            ),
            ("https://www.reddit.com/users/", "user_directory", "/users/popular.json"),
            ("https://www.reddit.com/users/new/", "user_directory", "/users/new.json"),
            (
                "https://www.reddit.com/user/spez/trophies/",
                "trophies",
                "/user/spez/trophies.json",
            ),
            (
                "https://www.reddit.com/r/Python/about/sidebar/",
                "subreddit_about",
                "/r/Python/about.json",
            ),
            (
                "https://www.reddit.com/r/Python/about/sticky/?num=2",
                "thread",
                "/r/Python/about/sticky.json",
            ),
            (
                "https://www.reddit.com/user/alice/m/devops/comments/",
                "multi_profile",
                "/api/multi/user/alice/m/devops.json",
            ),
            (
                "https://www.reddit.com/user/alice/m/devops/search/?q=python",
                "multi_profile",
                "/api/multi/user/alice/m/devops.json",
            ),
        ],
    )
    def test_additional_public_surfaces_are_never_csr_fallback(self, url, kind, path):
        route = route_reddit_url(url)

        assert route is not None
        assert route.kind == kind
        assert urlparse(route.requests[0]).path == path

    def test_multireddit_search_is_restricted_to_exact_membership(self):
        route = route_reddit_url(
            "https://www.reddit.com/user/alice/m/devops/search/"
            "?q=python&sort=new&t=all&type=link&limit=3"
        )

        assert route is not None
        assert route.kind == "multi_profile"
        query = parse_qs(urlparse(route.requests[1]).query)
        assert query["restrict_sr"] == ["true"]
        assert query["q"] == ["python"]
        assert query["sort"] == ["new"]
        assert query["t"] == ["all"]
        assert query["type"] == ["link"]
        assert all("raw_json=1" in request for request in route.requests)

    @pytest.mark.parametrize("sort", ["random", "live"])
    def test_thread_preserves_all_public_comment_sorts(self, sort):
        route = route_reddit_url(
            f"https://www.reddit.com/comments/abc123/title/?sort={sort}"
        )

        assert route is not None
        assert parse_qs(urlparse(route.requests[0]).query)["sort"] == [sort]

    @pytest.mark.parametrize(
        "name",
        ["+foo", "foo+", "foo++bar", "foo+-bar", "foo--bar"],
    )
    def test_malformed_aggregate_subreddit_operators_are_rejected(self, name):
        route = route_reddit_url(f"https://www.reddit.com/r/{name}/")

        assert route is not None
        assert route.kind == "html_fallback"

    def test_aggregate_exclusion_route_preserves_origin_subreddits(self):
        route = route_reddit_url("https://www.reddit.com/r/all-funny+news/")
        assert route is not None
        output = render_reddit_route(
            route,
            [{"data": {"children": [_post(subreddit="Python")]}}],
            max_tokens=5000,
        )

        assert "r/Python" in output


class TestRedditThreadRendering:
    def test_listing_cards_preserve_image_gallery_and_video_urls(self):
        rendered = format_reddit_post(
            _post(
                is_self=False,
                selftext="",
                url="https://www.reddit.com/gallery/post1",
                gallery_data={"items": [{"media_id": "one"}]},
                media_metadata={
                    "one": {"s": {"u": "https://i.redd.it/one.jpg?x=1&amp;y=2"}},
                },
                secure_media={
                    "reddit_video": {
                        "fallback_url": "https://v.redd.it/clip/DASH_720.mp4",
                        "hls_url": "https://v.redd.it/clip/HLSPlaylist.m3u8",
                        "dash_url": "https://v.redd.it/clip/DASHPlaylist.mpd",
                    }
                },
            )["data"],
            1,
        )

        assert "https://www.reddit.com/gallery/post1" in rendered
        assert "https://i.redd.it/one.jpg?x=1&y=2" in rendered
        assert "https://v.redd.it/clip/DASH_720.mp4" in rendered
        assert "https://v.redd.it/clip/HLSPlaylist.m3u8" in rendered
        assert "https://v.redd.it/clip/DASHPlaylist.mpd" in rendered
        assert "https://www.reddit.com/r/Python/comments/post1/" in rendered

    def test_markdown_nesting_deleted_nodes_more_and_selected_comment(self):
        nested = _comment(
            "selected",
            "[deleted]",
            author="[deleted]",
            is_submitter=True,
            parent_id="t3_post1",
            permalink="/r/Python/comments/post1/title/selected/",
            replies=[
                _comment(
                    "child",
                    "> quote\n\n1. numbered\n\n[link](https://example.com)",
                    parent_id="t1_selected",
                    permalink="/r/Python/comments/post1/title/child/",
                )
            ],
        )
        more = {
            "kind": "more",
            "data": {"count": 2, "children": ["abc", "def"]},
        }
        payload = _thread(_post(locked=True, archived=True), [nested, more])
        route = RedditRoute(
            "https://www.reddit.com/r/Python/comments/post1/title/selected/",
            "thread",
            selected_comment_id="selected",
        )

        rendered = render_reddit_route(route, [payload], max_tokens=5000)

        assert "# A compact Reddit post" in rendered
        assert "123 score · 96% upvoted" in rendered
        assert "```python\nprint('kept')\n```" in rendered
        assert "**Locked · Archived**" in rendered
        assert "u/[deleted]" in rendered
        assert "**selected comment**" in rendered
        assert "↳ u/bob" in rendered
        assert "> quote\n\n1. numbered\n\n[link](https://example.com)" in rendered
        assert "Load 2 more replies:" in rendered
        assert "children=abc%2Cdef" in rendered
        assert "link_id=t3_post1" in rendered
        assert (
            "Permalink: "
            "https://www.reddit.com/r/Python/comments/post1/title/selected/"
        ) in rendered
        assert (
            "Parent context: "
            "https://www.reddit.com/r/Python/comments/post1/title/child/"
            "?context=3"
        ) in rendered
        assert "old.reddit.com" not in rendered

    def test_media_gallery_video_crosspost_poll_and_status_metadata(self):
        media_post = _post(
            is_self=False,
            selftext="Poll context",
            url="https://youtu.be/example",
            url_overridden_by_dest="https://youtu.be/example",
            over_18=True,
            spoiler=True,
            gallery_data={"items": [{"media_id": "one"}, {"media_id": "two"}]},
            media_metadata={
                "one": {"s": {"u": "https://i.redd.it/one.jpg?x=1&amp;y=2"}},
                "two": {
                    "s": {"mp4": "https://preview.redd.it/two.mp4"},
                    "hlsUrl": "https://v.redd.it/gallery/HLSPlaylist.m3u8",
                    "dashUrl": "https://v.redd.it/gallery/DASHPlaylist.mpd",
                },
            },
            secure_media={
                "reddit_video": {
                    "fallback_url": "https://v.redd.it/video/DASH_720.mp4?source=fallback",
                    "hls_url": "https://v.redd.it/video/HLSPlaylist.m3u8",
                    "dash_url": "https://v.redd.it/video/DASHPlaylist.mpd",
                },
                "oembed": {"provider_name": "YouTube", "title": "External clip"},
            },
            crosspost_parent_list=[{
                "title": "Original post",
                "subreddit_name_prefixed": "r/source",
                "permalink": "/r/source/comments/source1/original/",
            }],
            poll_data={
                "total_vote_count": 99,
                "options": [
                    {"text": "First", "vote_count": 60},
                    {"text": "Second", "vote_count": 39},
                ],
            },
        )

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/comments/post1/", "thread"),
            [_thread(media_post, [])],
            max_tokens=5000,
        )

        for expected in (
            "**NSFW · Spoiler**",
            "**Link:** https://youtu.be/example",
            "**Gallery (2 items):**",
            "https://i.redd.it/one.jpg?x=1&y=2",
            "MP4: https://v.redd.it/video/DASH_720.mp4?source=fallback",
            "HLS: https://v.redd.it/video/HLSPlaylist.m3u8",
            "DASH: https://v.redd.it/video/DASHPlaylist.mpd",
            "https://v.redd.it/gallery/HLSPlaylist.m3u8",
            "https://v.redd.it/gallery/DASHPlaylist.mpd",
            "**Media:** YouTube — External clip",
            "**Crosspost from r/source:** Original post",
            "**Poll · 99 votes:**",
            "- First — 60",
        ):
            assert expected in rendered

    def test_crosspost_preserves_parent_body_media_gallery_poll_and_status(self):
        parent = _post(
            title="Parent title",
            selftext="PARENT SUBSTANTIVE BODY",
            subreddit="source",
            subreddit_name_prefixed="r/source",
            permalink="/r/source/comments/source1/parent/",
            is_self=False,
            url="https://example.com/parent-link",
            stickied=True,
            is_original_content=True,
            gallery_data={
                "items": [{
                    "media_id": "parent-image",
                    "caption": "Parent caption",
                }]
            },
            media_metadata={
                "parent-image": {
                    "s": {"u": "https://i.redd.it/parent.jpg"},
                }
            },
            poll_data={
                "total_vote_count": 5,
                "voting_end_timestamp": 1,
                "options": [{"text": "Parent option", "vote_count": 5}],
            },
        )["data"]
        wrapper = _post(
            title="Wrapper",
            selftext="",
            crosspost_parent_list=[parent],
        )

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/comments/post1/", "thread"),
            [_thread(wrapper, [])],
            max_tokens=5000,
        )

        for expected in (
            "## Crosspost content",
            "PARENT SUBSTANTIVE BODY",
            "https://example.com/parent-link",
            "Parent caption",
            "https://i.redd.it/parent.jpg",
            "Parent option",
            "Closed:",
            "Stickied",
            "OC",
        ):
            assert expected in rendered

    def test_deep_reply_nesting_is_not_flattened(self):
        """Depth past 3 must stay distinguishable.

        Markdown runs out of heading levels at ``######``, so the heading alone
        cannot express depth beyond 3. The branch marker was capped at 3 too,
        which made every reply from depth 3 down render identically -- a real
        r/worldnews thread nests to depth 9, and its whole lower structure
        collapsed into one indistinguishable level.
        """

        deepest = _comment("c6", "deepest reply")
        node = deepest
        for index in range(5, 0, -1):
            node = _comment(f"c{index}", f"reply {index}", replies=[node])
        thread = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/post1/",
                "thread",
            ),
            [_thread(_post(), [node])],
            max_tokens=25000,
        )

        for depth in range(6):
            marker = "↳ " * depth
            assert f"{marker}u/bob" in thread, f"missing depth {depth}"
        # Heading levels still stop at six, so depth is carried by the marker.
        assert "###### ↳ ↳ ↳ ↳ ↳ u/bob" in thread
        assert "deepest reply" in thread

    def test_comment_body_leading_blank_lines_are_trimmed(self):
        """A body opening with blank lines must not render as an empty comment.

        Reddit returns bodies like ``"\\n\\n  \\n\\n\\nI made a GUI engine..."``.
        Rendered verbatim, the card's first line after its heading is blank, so
        the comment reads as having no body at all.
        """

        post = _post()
        comment = _comment("c1", "  \n\n\nI made a GUI game engine in pure Python.\n\n  \n")
        thread = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/post1/",
                "thread",
            ),
            [_thread(post, [comment])],
            max_tokens=5000,
        )

        heading = "### u/bob · 9 score ·"
        start = thread.index(heading)
        after_heading = thread[start:].split("\n", 1)[1]
        assert after_heading.startswith("\nI made a GUI game engine")
        assert "I made a GUI game engine in pure Python." in thread

    def test_comment_body_keeps_leading_code_block_indentation(self):
        """Only fully blank lines go -- an opening code block keeps its indent."""

        post = _post()
        comment = _comment("c1", "\n\n    pip install thing\n    run thing\n")
        thread = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/post1/",
                "thread",
            ),
            [_thread(post, [comment])],
            max_tokens=5000,
        )

        assert "    pip install thing" in thread

    def test_blank_only_comment_body_is_left_alone(self):
        """A body that is blank throughout is not replaced with a sentinel."""

        from fetchaller.content.reddit import _comment_body_text

        assert _comment_body_text("   \n\n  ") == "   \n\n  "
        assert _comment_body_text("") == "[deleted]"
        assert _comment_body_text(None) == "[deleted]"

    def test_event_and_live_chat_state_survive_listing_and_thread_rendering(self):
        post = _post(
            discussion_type="CHAT",
            event_is_live=True,
            event_start=1_700_000_000_000,
            event_end=1_700_003_600_000,
            poll_data={
                "options": [{"text": "Attend", "vote_count": 10}],
                "voting_end_timestamp": 1_700_003_600_000,
            },
        )

        listing = format_reddit_post(post["data"], 1)
        thread = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/post1/",
                "thread",
            ),
            [_thread(post, [])],
            max_tokens=1000,
        )

        for rendered in (listing, thread):
            assert "Live chat" in rendered
            assert "Live event" in rendered
            assert "starts 2023-11-14 22:13 UTC" in rendered
            assert "ends 2023-11-14 23:13 UTC" in rendered
        assert "Closed: 2023-11-14 23:13 UTC" in thread

    def test_rich_flair_legacy_gilding_and_listing_metadata_render_once(self):
        post = _post(
            stickied=True,
            pinned=True,
            distinguished="moderator",
            is_original_content=True,
            contest_mode=True,
            gilded=2,
            gildings={"gid_1": 1, "gid_2": 1},
            author_flair_text="",
            author_flair_richtext=[
                {"e": "emoji", "u": "https://emoji.redditmedia.com/author.png"},
                {"e": "text", "t": "Helper"},
            ],
        )

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/", "listing", subreddit="Python"),
            [{"data": {"children": [post]}}],
            max_tokens=5000,
        )

        assert rendered.count("score 123") == 1
        assert rendered.count("u/alice") == 1
        for expected in (
            "Stickied",
            "Pinned",
            "Moderator",
            "OC",
            "Contest mode",
            "2 awards",
            "author flair: Helper",
            "https://emoji.redditmedia.com/author.png",
        ):
            assert expected in rendered

    def test_hidden_scores_and_removed_state_match_public_ui(self):
        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/comments/post1/", "thread"),
            [
                _thread(
                    _post(hide_score=True, removed_by_category="moderator"),
                    [_comment("hidden", "Visible body", score_hidden=True)],
                )
            ],
            max_tokens=5000,
        )

        assert rendered.count("score hidden") == 2
        assert "123 score" not in rendered
        assert "**Removed**" in rendered
        assert "**Deleted**" not in rendered

        ups_only = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/", "listing", subreddit="Python", label="hot"),
            [{"data": {"children": [_post(score=None, ups=999)]}}],
            max_tokens=5000,
        )
        assert "score hidden" in ups_only
        assert "999" not in ups_only

    def test_collapsed_and_archived_comment_access_state_is_visible(self):
        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/post1/",
                "thread",
            ),
            [
                _thread(
                    _post(),
                    [
                        _comment(
                            "collapsed",
                            "Still public",
                            collapsed=True,
                            collapsed_because_crowd_control=True,
                            archived=True,
                        )
                    ],
                )
            ],
            max_tokens=5000,
        )

        assert "archived" in rendered
        assert "collapsed: crowd control" in rendered
        assert "Still public" in rendered

    def test_rich_comment_url_is_preserved_without_rendering_duplicate_html(self):
        body = "![gif](giphy|abc)"
        rich = _comment(
            "gif1",
            body,
            body_html=(
                '&lt;div class="md"&gt;&lt;p&gt;'
                '<a href="https://giphy.com/gifs/real-animation">GIF</a>'
                "&lt;/p&gt;&lt;/div&gt;"
            ),
            media_metadata={
                "clip": {
                    "hlsUrl": "https://v.redd.it/comment/HLSPlaylist.m3u8",
                    "dashUrl": "https://v.redd.it/comment/DASHPlaylist.mpd",
                }
            },
        )
        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/comments/post1/", "thread"),
            [_thread(_post(), [rich])],
            max_tokens=5000,
        )

        assert body in rendered
        assert "Media: https://giphy.com/gifs/real-animation" in rendered
        assert "Media: https://v.redd.it/comment/HLSPlaylist.m3u8" in rendered
        assert "Media: https://v.redd.it/comment/DASHPlaylist.mpd" in rendered
        assert 'class="md"' not in rendered

    def test_deleted_gallery_item_is_named_instead_of_silently_blank(self):
        post = _post(
            is_self=False,
            selftext="",
            gallery_data={
                "items": [
                    {
                        "media_id": "deleted",
                        "is_deleted": True,
                        "caption": "Former image",
                    }
                ]
            },
            media_metadata={"deleted": {"status": "failed"}},
        )

        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/post1/",
                "thread",
            ),
            [_thread(post, [])],
            max_tokens=1000,
        )

        assert "**Item 1** — Former image — unavailable" in rendered

    def test_token_budget_stops_at_comment_boundary(self):
        first_body = "FIRST-COMMENT " + "a" * 80
        second_body = "SECOND-COMMENT " + "b" * 800
        payload = _thread(_post(selftext="short"), [_comment("one", first_body), _comment("two", second_body)])

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/comments/post1/", "thread"),
            [payload],
            max_tokens=180,
            chars_per_token=4,
        )

        assert len(rendered) <= 720
        assert first_body in rendered
        assert "SECOND-COMMENT" not in rendered
        assert "comments/replies omitted due to output limit" in rendered
        assert "b" * 100 not in rendered

    def test_many_continuations_can_never_erase_the_source_post(self):
        more_nodes = [
            {
                "kind": "more",
                "data": {
                    "count": 10,
                    "children": [
                        f"child{node}{index}"
                        for index in range(10)
                    ],
                },
            }
            for node in range(12)
        ]
        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/post1/",
                "thread",
            ),
            [_thread(_post(title="SOURCE POST MUST REMAIN"), more_nodes)],
            max_tokens=180,
        )

        assert "SOURCE POST MUST REMAIN" in rendered
        assert "comments/replies omitted due to output limit" in rendered
        assert len(rendered) <= 180 * 4

    def test_authored_old_reddit_links_are_kept_usable(self):
        payload = _thread(
            _post(
                selftext=(
                    "[preferences](https://old.reddit.com/prefs/apps) "
                    "and [community](//old.reddit.com/r/Python/)"
                )
            ),
            [_comment("one", "See https://old.reddit.com/r/Python/")],
        )

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/comments/post1/", "thread"),
            [payload],
            max_tokens=5000,
        )

        assert "old.reddit.com" not in rendered
        assert "https://www.reddit.com/prefs/apps" in rendered
        assert "https://www.reddit.com/r/Python/" in rendered

    def test_link_canonicalization_cannot_cut_the_output_limit_marker(self):
        payload = _thread(
            _post(
                title="T" * 300,
                selftext="//old.reddit.com/r/Python/ " * 30,
            ),
            [],
        )

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/comments/post1/", "thread"),
            [payload],
            max_tokens=80,
            chars_per_token=4,
        )

        assert len(rendered) <= 320
        assert "old.reddit.com" not in rendered
        assert rendered.endswith("[Post content truncated at output limit]")

    def test_link_canonicalization_is_budgeted_before_comment_boundaries(self):
        payload = _thread(
            _post(selftext="short"),
            [
                _comment(
                    "one",
                    "COMMENT-BODY-END "
                    + "//old.reddit.com/r/Python/ " * 8,
                    author="unique_author",
                ),
                _comment("two", "SECOND-COMMENT"),
            ],
        )

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/comments/post1/", "thread"),
            [payload],
            max_tokens=149,
            chars_per_token=4,
        )

        assert len(rendered) <= 596
        assert "old.reddit.com" not in rendered
        assert "unique_author" not in rendered
        assert "COMMENT-BODY-END" not in rendered
        assert "SECOND-COMMENT" not in rendered
        assert "[2 comments/replies omitted due to output limit]" in rendered

    def test_lookalike_old_reddit_hostname_is_never_rewritten(self):
        payload = _thread(
            _post(
                is_self=False,
                url="https://old.reddit.com.evil.example/path",
                url_overridden_by_dest="https://old.reddit.com.evil.example/path",
            ),
            [],
        )

        rendered = render_reddit_route(
            RedditRoute("https://www.reddit.com/comments/post1/", "thread"),
            [payload],
            max_tokens=1000,
        )

        assert "https://old.reddit.com.evil.example/path" in rendered

    def test_old_reddit_text_inside_another_urls_path_or_query_is_not_rewritten(self):
        text = (
            "https://example.com/archive/old.reddit.com/r/Python/ "
            "https://example.com/?target=old.reddit.com/r/Python/ "
            "reader@old.reddit.com"
        )

        assert canonicalize_reddit_links(text) == text

    def test_protocol_relative_reddit_value_becomes_a_canonical_absolute_url(self):
        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/user/alice/trophies/",
                "trophies",
                username="alice",
            ),
            [
                {
                    "data": {
                        "trophies": [
                            {
                                "data": {
                                    "name": "Example",
                                    "url": "//old.reddit.com/r/Python/",
                                }
                            }
                        ]
                    }
                }
            ],
            max_tokens=1000,
        )

        assert "https://www.reddit.com/r/Python/" in rendered
        assert "www.reddit.com//old.reddit.com" not in rendered


class TestOtherRedditRenderers:
    def test_listing_and_comment_feed_have_canonical_links(self):
        listing = {"kind": "Listing", "data": {"children": [_post()], "after": "t3_next"}}
        comments = {
            "kind": "Listing",
            "data": {
                "children": [
                    _comment(
                        "feed1",
                        "A recent comment",
                        subreddit="Python",
                        subreddit_name_prefixed="r/Python",
                        link_title="Parent post",
                        permalink="/r/Python/comments/post1/title/feed1/",
                        archived=True,
                        controversiality=1,
                        collapsed=True,
                        edited=1_700_000_100,
                    )
                ]
            },
        }

        post_output = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/", "listing", subreddit="Python", label="hot"),
            [listing],
            max_tokens=5000,
        )
        comment_output = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/comments/",
                "comment_listing",
                subreddit="Python",
                label="comments",
            ),
            [comments],
            max_tokens=5000,
        )

        assert "https://www.reddit.com/r/Python/comments/post1/" in post_output
        assert (
            "[Next page: https://www.reddit.com/r/Python/?after=t3_next&count=1]"
            in post_output
        )
        assert "https://www.reddit.com/r/Python/comments/post1/title/feed1/" in comment_output
        assert "archived, controversial, collapsed" in comment_output
        assert "Edited: 2023-11-14" in comment_output
        assert "old.reddit.com" not in post_output + comment_output

    def test_by_id_listing_renders_comment_only_and_mixed_results(self):
        comment = _comment(
            "comment1",
            "COMMENT-ONLY BY-ID BODY",
            subreddit="Python",
            subreddit_name_prefixed="r/Python",
            link_title="Comment parent",
            permalink="/r/Python/comments/post1/title/comment1/",
        )
        comment_only = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/by_id/t1_comment1/",
                "listing",
                label="items by ID",
            ),
            [{"kind": "Listing", "data": {"children": [comment]}}],
            max_tokens=5000,
        )
        mixed = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/by_id/t1_comment1,t3_post1/",
                "listing",
                label="items by ID",
            ),
            [{"kind": "Listing", "data": {"children": [comment, _post()]}}],
            max_tokens=5000,
        )

        assert "COMMENT-ONLY BY-ID BODY" in comment_only
        assert "u/bob" in comment_only
        assert "https://www.reddit.com/r/Python/comments/post1/title/comment1/" in comment_only
        assert "1. **Comment parent**" in mixed
        assert "2. A compact Reddit post" in mixed

    def test_profile_about_rules_and_wiki_render_selected_fields_once(self):
        profile = {
            "kind": "t2",
            "data": {
                "name": "spez",
                "link_karma": 10,
                "comment_karma": 20,
                "created_utc": 1_000,
                "subreddit": {
                    "title": "Profile title",
                    "public_description": "Profile description",
                    "icon_img": "https://styles.redditmedia.com/avatar.png?x=1&amp;y=2",
                },
            },
        }
        overview = {"kind": "Listing", "data": {"children": [_post()]}}
        profile_output = render_reddit_route(
            RedditRoute("https://www.reddit.com/user/spez/", "user_profile", username="spez"),
            [profile, overview],
            max_tokens=5000,
        )
        rules_output = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/about/rules/", "rules", subreddit="Python"),
            [{"rules": [{"short_name": "Be kind", "description": "No insults"}], "site_rules": ["Remember the human"]}],
            max_tokens=5000,
        )
        wiki_output = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/redditdev/wiki/oauth2/",
                "wiki",
                subreddit="redditdev",
                label="oauth2",
            ),
            [{
                "data": {
                    "content_md": "UNIQUE WIKI BODY",
                    "revision_date": 1_700_000_000,
                    "revision_id": "revision-1",
                    "reason": "clarify wording",
                }
            }],
            max_tokens=5000,
        )
        about_output = render_reddit_route(
            RedditRoute("https://www.reddit.com/r/Python/about/", "subreddit_about", subreddit="Python"),
            [{
                "data": {
                    "display_name_prefixed": "r/Python",
                    "title": "Python",
                    "subscribers": 10,
                    "accounts_active": 2,
                    "public_description": "Short public description",
                    "description": "Full **Markdown** community description",
                }
            }],
            max_tokens=5000,
        )
        sparse_profile = render_reddit_route(
            RedditRoute("https://www.reddit.com/user/deleted/", "user_about", username="deleted"),
            [{"data": {"name": "deleted", "subreddit": None}}],
            max_tokens=5000,
        )

        assert "# u/spez" in profile_output
        assert "Post karma:** 10" in profile_output
        assert "https://styles.redditmedia.com/avatar.png?x=1&y=2" in profile_output
        assert "https://www.reddit.com/user/spez/" in profile_output
        assert "Recent activity" in profile_output
        assert "## 1. Be kind" in rules_output
        assert "Remember the human" in rules_output
        assert wiki_output.count("UNIQUE WIKI BODY") == 1
        assert "**Revision:** revision-1" in wiki_output
        assert "**Edit reason:** clarify wording" in wiki_output
        assert "Short public description" in about_output
        assert "Full **Markdown** community description" in about_output
        assert "- **Status:** Public" in about_output
        assert "# u/deleted" in sparse_profile

    def test_wiki_revision_comparison_renders_a_bounded_real_diff(self):
        route = RedditRoute(
            "https://www.reddit.com/r/redditdev/wiki/index/"
            "?v=revision_one&v2=revision_two",
            "wiki_diff",
            subreddit="redditdev",
            label="index",
        )
        left = {
            "data": {
                "content_md": "# Setup\n\nUse `/dev/api`.\n\n```python\nold()\n```",
                "revision_id": "revision_one",
                "revision_date": 1_700_000_000,
                "revision_by": {"data": {"name": "alice"}},
            }
        }
        right = {
            "data": {
                "content_md": "# Setup\n\nUse `/api/docs`.\n\n```python\nnew()\n```",
                "revision_id": "revision_two",
                "revision_date": 1_700_000_100,
                "revision_by": {"data": {"name": "bob"}},
                "reason": "update API links",
            }
        }

        rendered = render_reddit_route(
            route,
            [left, right],
            max_tokens=5000,
        )

        assert "# r/redditdev wiki diff · index" in rendered
        assert "revision `revision_one`" in rendered
        assert "u/alice" in rendered
        assert "revision `revision_two`" in rendered
        assert "u/bob" in rendered
        assert "reason: update API links" in rendered
        assert "-Use `/dev/api`." in rendered
        assert "+Use `/api/docs`." in rendered
        # The authored triple-backtick blocks require a longer outer fence.
        assert "````diff" in rendered

    def test_wiki_revision_comparison_names_an_unavailable_side(self):
        route = RedditRoute(
            "https://www.reddit.com/r/redditdev/wiki/index/"
            "?v=missing_revision&v2=present_revision",
            "wiki_diff",
            subreddit="redditdev",
            label="index",
        )
        present = {
            "data": {
                "content_md": "Current content.",
                "revision_id": "present_revision",
            }
        }

        rendered = render_reddit_route(
            route,
            [
                {
                    "_fetch_error": (
                        "This Reddit page or item was not found, was deleted, "
                        "or is unavailable."
                    )
                },
                present,
            ],
            max_tokens=5000,
        )

        assert "From revision unavailable" in rendered
        assert "not found" in rendered
        assert "**To:** revision `present_revision`" in rendered
        assert "No content differences returned." not in rendered

    def test_directory_moderators_duplicates_and_wiki_surfaces_render(self):
        directory_payload = {
            "data": {
                "children": [{
                    "kind": "t5",
                    "data": {
                        "display_name_prefixed": "r/Python",
                        "title": "Python",
                        "subscribers": 1_234_567,
                        "public_description": "News and help",
                        "url": "/r/Python/",
                    },
                }],
                "after": "t5_next",
            }
        }
        moderators_payload = {
            "data": {
                "children": [{
                    "kind": "t2",
                    "data": {"name": "mod_one", "mod_permissions": ["all"]},
                }]
            }
        }
        duplicates_payload = [
            {"data": {"children": [_post()]}},
            {"data": {"children": [_post(id="other", title="Other discussion")]}},
        ]
        wiki_pages_payload = {
            "kind": "wikipagelisting",
            "data": ["index", "faq/getting-started"],
        }
        wiki_revisions_payload = {
            "data": {
                "children": [{
                    "kind": "WikiRevision",
                    "data": {
                        "page": "faq",
                        "id": "revision-1",
                        "timestamp": 1_700_000_000,
                        "author": {"data": {"name": "editor"}},
                        "reason": "clarify setup",
                    },
                }],
                "after": "WikiRevision_0136a1c0-57c7-11f1-b49a-ae675b7b52c3",
            }
        }

        directory = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/subreddits/popular/",
                "subreddit_directory",
                label="popular",
            ),
            [directory_payload],
            max_tokens=5000,
        )
        moderators = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/about/moderators/",
                "moderators",
                subreddit="Python",
            ),
            [moderators_payload],
            max_tokens=5000,
        )
        duplicates = render_reddit_route(
            RedditRoute("https://www.reddit.com/duplicates/post1/", "duplicates"),
            [duplicates_payload],
            max_tokens=5000,
        )
        wiki_pages = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/wiki/pages/",
                "wiki_pages",
                subreddit="Python",
            ),
            [wiki_pages_payload],
            max_tokens=5000,
        )
        wiki_revisions = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/wiki/revisions/",
                "wiki_revisions",
                subreddit="Python",
                label="all pages",
            ),
            [wiki_revisions_payload],
            max_tokens=5000,
        )

        assert "1,234,567 subscribers" in directory
        assert "https://www.reddit.com/r/Python/" in directory
        assert "after=t5_next" in directory
        assert "u/mod_one" in moderators and "all" in moderators
        assert "A compact Reddit post" in duplicates and "Other discussion" in duplicates
        assert "/wiki/faq/getting-started/" in wiki_pages
        assert "revision-1" in wiki_revisions
        assert "u/editor" in wiki_revisions
        assert "after=WikiRevision_" in wiki_revisions

    def test_wiki_page_names_cannot_break_out_of_markdown_link_labels(self):
        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/wiki/pages/",
                "wiki_pages",
                subreddit="Python",
            ),
            [
                {
                    "kind": "wikipagelisting",
                    "data": ["faq/[label]", "faq\\backslash"],
                }
            ],
            max_tokens=1000,
        )

        assert r"[faq/\[label\]]" in rendered
        assert r"[faq\\backslash]" in rendered
        assert "/wiki/faq/%5Blabel%5D/" in rendered
        assert "/wiki/faq%5Cbackslash/" in rendered

    def test_live_full_focused_and_collection_render_real_shapes(self):
        cursor = "LiveUpdate_8d253a42-57c7-11f1-b49a-ae675b7b52c3"
        update = {
            "kind": "LiveUpdate",
            "data": {
                "id": "update-1",
                "author": "reporter",
                "created_utc": 1_700_000_000,
                "body": "A **live** update",
                "stricken": True,
                "embeds": [{"url": "https://example.com/report"}],
            },
        }
        listing = {"kind": "Listing", "data": {"children": [update], "after": cursor}}
        live_route = RedditRoute(
            "https://www.reddit.com/live/abc123/",
            "live",
        )
        full = render_reddit_route(
            live_route,
            [
                {
                    "kind": "LiveUpdateEvent",
                    "data": {
                        "title": "Breaking news",
                        "state": "live",
                        "viewer_count": 123,
                        "description": "Updates here",
                    },
                },
                listing,
            ],
            max_tokens=5000,
        )
        focused = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/live/abc123/updates/update-1/",
                "live_update",
            ),
            [listing],
            max_tokens=5000,
        )
        about = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/live/abc123/about/",
                "live_about",
            ),
            [
                {
                    "data": {
                        "title": "Breaking news",
                        "state": "live",
                        "viewer_count": 123,
                        "description": "Updates here",
                    }
                }
            ],
            max_tokens=5000,
        )
        contributors = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/live/abc123/contributors/",
                "live_contributors",
            ),
            [{"data": {"children": [{"name": "reporter"}]}}],
            max_tokens=5000,
        )
        partial_live = render_reddit_route(
            live_route,
            [{"_fetch_error": "details blocked"}, listing],
            max_tokens=5000,
        )
        collection = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/collection/collection_1/",
                "collection",
                subreddit="Python",
            ),
            [
                {
                    "title": "Learning Python",
                    "description": "Curated posts",
                    "link_ids": ["t3_post1"],
                },
                {"data": {"children": [_post()]}},
            ],
            max_tokens=5000,
        )

        for output in (full, focused):
            assert "u/reporter" in output
            assert "A **live** update" in output
            assert "[stricken]" in output
            assert "https://example.com/report" in output
            assert "Update ID: update-1" in output
        assert f"after={cursor}&count=1" in full
        assert "# Breaking news" in about
        assert "Updates here" in about
        assert "## Updates" not in about
        assert "u/reporter" in contributors
        assert "https://www.reddit.com/user/reporter/" in contributors
        assert "Live thread details unavailable: details blocked" in partial_live
        assert "A **live** update" in partial_live
        next_route = route_reddit_url(
            f"https://www.reddit.com/live/abc123/?after={cursor}&count=1"
        )
        assert next_route is not None
        next_params = parse_qs(urlparse(next_route.requests[1]).query)
        assert next_params["after"] == [cursor]
        assert next_params["count"] == ["1"]
        assert "# Learning Python" in collection
        assert "Curated posts" in collection
        assert "A compact Reddit post" in collection

    def test_profile_partial_failure_and_output_budget_are_explicit(self):
        profile = {
            "data": {
                "name": "alice",
                "link_karma": 10,
                "comment_karma": 20,
            }
        }
        partial = render_reddit_route(
            RedditRoute("https://www.reddit.com/user/alice/", "user_profile", username="alice"),
            [profile, {"_fetch_error": "overview unavailable"}],
            max_tokens=5000,
        )
        bounded = render_reddit_route(
            RedditRoute("https://www.reddit.com/user/alice/", "user_profile", username="alice"),
            [
                profile,
                {
                    "data": {
                        "children": [
                            _post(id=f"post{i}", title=f"POST-{i}", selftext="x" * 300)
                            for i in range(20)
                        ]
                    }
                },
            ],
            max_tokens=120,
        )

        assert "# u/alice" in partial
        assert "overview unavailable" in partial
        assert len(bounded) <= 480
        assert "omitted due to output limit" in bounded

    def test_user_directory_trophies_and_multireddit_metadata_render_real_shapes(self):
        user_directory = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/users/popular/",
                "user_directory",
                label="popular",
            ),
            [{
                "data": {
                    "children": [{
                        "kind": "t5",
                        "data": {
                            "name": "t5_3oy63",
                            "display_name_prefixed": "u/thisisinsider",
                            "title": "Insider",
                            "public_description": "News profile",
                            "icon_img": "https://styles.redditmedia.com/icon.png",
                            "url": "/user/thisisinsider/",
                            "link_karma": 16_483,
                            "comment_karma": 10_271,
                            "created_utc": 1_506_103_969,
                        },
                    }]
                }
            }],
            max_tokens=5000,
        )
        trophies = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/user/alice/trophies/",
                "trophies",
                username="alice",
            ),
            [{
                "kind": "TrophyList",
                "data": {
                    "trophies": [{
                        "kind": "t6",
                        "data": {
                            "name": "Verified Email",
                            "description": "Email confirmed",
                            "granted_at": 1_700_000_000,
                            "icon_70": "https://www.redditstatic.com/trophy.png",
                            "url": "/tb/verified/",
                        },
                    }]
                },
            }],
            max_tokens=5000,
        )
        metadata = {
            "kind": "LabeledMulti",
            "data": {
                "display_name": "DevOps",
                "owner": "alice",
                "visibility": "public",
                "num_subscribers": 42,
                "created_utc": 1_700_000_000,
                "description_md": "Operations communities",
                "subreddits": [{"name": "devops"}, {"name": "sysadmin"}],
            },
        }
        multi = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/user/alice/m/devops/",
                "multi_profile",
                username="alice",
                label="u/alice/m/devops · hot",
            ),
            [metadata, {"data": {"children": [_post(subreddit="devops")]}}],
            max_tokens=5000,
        )
        multi_partial = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/user/alice/m/devops/",
                "multi_profile",
                username="alice",
                label="devops",
            ),
            [metadata, {"_fetch_error": "feed unavailable"}],
            max_tokens=5000,
        )
        multi_comments = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/user/alice/m/devops/comments/",
                "multi_profile",
                username="alice",
                label="u/alice/m/devops · comments",
            ),
            [
                metadata,
                {
                    "data": {
                        "children": [
                            _comment(
                                "comment1",
                                "MULTIREDDIT COMMENT BODY",
                                subreddit="devops",
                                subreddit_name_prefixed="r/devops",
                                link_title="Parent post",
                                parent_id="t3_post1",
                                link_id="t3_post1",
                                permalink=(
                                    "/r/devops/comments/post1/title/comment1/"
                                ),
                            )
                        ]
                    }
                },
            ],
            max_tokens=5000,
        )

        assert "u/thisisinsider" in user_directory
        assert "t5_3oy63" not in user_directory
        assert "News profile" in user_directory
        assert "16,483 post karma" in user_directory
        assert "10,271 comment karma" in user_directory
        assert "Created: 2017-09-22 18:12 UTC" in user_directory
        assert (
            "Public activity: "
            "https://www.reddit.com/user/thisisinsider/overview/"
        ) in user_directory
        assert "Verified Email" in trophies and "Email confirmed" in trophies
        assert "https://www.reddit.com/tb/verified/" in trophies
        assert "DevOps" in multi
        assert "Operations communities" in multi
        assert "created 2023-11-14 22:13 UTC" in multi
        assert "r/devops" in multi and "r/sysadmin" in multi
        assert "A compact Reddit post" in multi
        assert "feed unavailable" in multi_partial
        assert "MULTIREDDIT COMMENT BODY" in multi_comments
        assert (
            "Permalink: https://www.reddit.com/r/devops/comments/"
            "post1/title/comment1/"
        ) in multi_comments
        assert (
            "Parent context: https://www.reddit.com/comments/post1/"
        ) in multi_comments

    def test_pagination_count_round_trip_and_duplicates_pagination(self):
        listing = {
            "data": {
                "children": [_post()],
                "dist": 25,
                "before": "t3_prev",
                "after": "t3_next",
            }
        }
        route = RedditRoute(
            "https://www.reddit.com/r/Python/?"
            "count=25&after=t3_old&limit=7&sr_detail=true",
            "listing",
            subreddit="Python",
            label="hot",
        )
        output = render_reddit_route(route, [listing], max_tokens=5000)
        duplicates = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/duplicates/post1/?count=25",
                "duplicates",
            ),
            [
                [
                    {"data": {"children": [_post()]}},
                    {
                        "data": {
                            "children": [_post(id="other")],
                            "dist": 25,
                            "after": "t3_next",
                        }
                    },
                ]
            ],
            max_tokens=5000,
        )

        assert "limit=7&sr_detail=true&before=t3_prev&count=0" in output
        assert "limit=7&sr_detail=true&after=t3_next&count=50" in output
        assert "sr_detail=true" in output
        assert "after=t3_old" not in output
        assert "after=t3_next&count=50" in duplicates

    def test_directory_pagination_retains_typed_search_context(self):
        search_query_id = "8d253a42-57c7-11f1-b49a-ae675b7b52c3"
        payload = {
            "data": {
                "children": [
                    {
                        "kind": "t5",
                        "data": {"display_name": "Python"},
                    }
                ],
                "after": "t5_next",
            }
        }
        route = RedditRoute(
            "https://www.reddit.com/subreddits/search/"
            f"?q=python&search_query_id={search_query_id}&sr_detail=true",
            "subreddit_directory",
            label="search",
        )

        rendered = render_reddit_route(route, [payload], max_tokens=1000)
        invalid = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/subreddits/search/"
                "?q=python&search_query_id=not-a-uuid&sr_detail=maybe",
                "subreddit_directory",
                label="search",
            ),
            [payload],
            max_tokens=1000,
        )

        assert f"search_query_id={search_query_id}" in rendered
        assert "sr_detail=true" in rendered
        assert "search_query_id" not in invalid
        assert "sr_detail" not in invalid

    def test_tight_budget_keeps_sanitized_pagination_reachable(self):
        payload = {
            "data": {
                "children": [
                    _post(id=f"post{index}", title="Long " + "x" * 250)
                    for index in range(5)
                ],
                "after": "t3_next",
            }
        }
        route = RedditRoute(
            "https://www.reddit.com/r/Python/?utm_source="
            + "tracking" * 1000,
            "listing",
            subreddit="Python",
            label="hot",
        )

        rendered = render_reddit_route(
            route,
            [payload],
            max_tokens=100,
        )

        assert len(rendered) <= 400
        assert "Next page:" in rendered
        assert "after=t3_next" in rendered
        assert "utm_source" not in rendered
        assert "items omitted due to output limit" in rendered

    def test_directory_status_moderator_date_and_legacy_awards_are_visible(self):
        directory = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/subreddits/banned/",
                "subreddit_directory",
                label="banned",
            ),
            [
                {
                    "data": {
                        "children": [
                            {
                                "kind": "t5",
                                "data": {
                                    "display_name": "restricted",
                                    "subreddit_type": "restricted",
                                    "quarantine": True,
                                    "over18": True,
                                    "created_utc": 1_700_000_000,
                                },
                            }
                        ]
                    }
                }
            ],
            max_tokens=2000,
        )
        moderators = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/r/Python/about/moderators/",
                "moderators",
                subreddit="Python",
            ),
            [
                {
                    "data": {
                        "children": [
                            {
                                "name": "alice",
                                "mod_permissions": ["posts"],
                                "date": 1_700_000_000,
                            }
                        ]
                    }
                }
            ],
            max_tokens=2000,
        )
        awards = format_reddit_post(
            _post(
                total_awards_received=0,
                gildings={"gid_1": 1, "gid_2": 1, "gid_3": 1},
            )["data"],
            1,
        )

        for label in ("Banned", "Restricted", "Quarantined", "NSFW", "Created:"):
            assert label in directory
        assert "posts · added 2023-11-14" in moderators
        assert "Silver, Gold, Platinum" in awards

    def test_subreddit_search_can_render_public_user_results(self):
        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/subreddits/search/"
                "?q=alice&show_users=true",
                "subreddit_directory",
                label='subreddit search · "alice"',
            ),
            [
                {
                    "data": {
                        "children": [
                            {
                                "kind": "t2",
                                "data": {
                                    "name": "alice",
                                    "link_karma": 123,
                                    "comment_karma": 456,
                                },
                            }
                        ]
                    }
                }
            ],
            max_tokens=1000,
        )

        assert "u/alice" in rendered
        assert "123 post karma" in rendered
        assert "https://www.reddit.com/user/alice/" in rendered
        assert "r/?" not in rendered

    def test_profile_partial_failures_and_extra_public_sections_are_explicit(self):
        route = RedditRoute(
            "https://www.reddit.com/user/alice/",
            "user_profile",
            username="alice",
        )
        rendered = render_reddit_route(
            route,
            [
                {"_fetch_error": "about blocked"},
                {"data": {"children": [_post()]}},
                {
                    "data": {
                        "trophies": [
                            {"data": {"name": "Verified Email"}}
                        ]
                    }
                },
                [
                    {
                        "data": {
                            "display_name": "Python feeds",
                            "path": "/user/alice/m/python/",
                        }
                    }
                ],
                {
                    "data": {
                        "children": [
                            {"kind": "t5", "data": {"display_name": "Python"}}
                        ]
                    }
                },
            ],
            max_tokens=5000,
        )

        assert "Profile details unavailable: about blocked" in rendered
        assert "Verified Email" in rendered
        assert "Python feeds" in rendered
        assert "r/Python" in rendered

    def test_morechildren_jquery_envelope_renders_comments(self):
        parent = _comment(
            "def456",
            "Expanded public reply",
            parent_id="t3_abc123",
        )
        child = _comment(
            "ghi789",
            "Nested expanded reply",
            parent_id="t1_def456",
        )
        more = {
            "kind": "more",
            "data": {
                "id": "more1",
                "link_id": "t3_abc123",
                "children": ["jkl012"],
                "count": 1,
            },
        }
        payload = {
            "jquery": [
                [0, 1, "attr", "insert_things"],
                [0, 1, "call", [[child, parent, more]]],
            ],
            "success": True,
        }
        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/api/morechildren?"
                "link_id=t3_abc123&children=def456",
                "morechildren",
            ),
            [payload],
            max_tokens=1000,
        )

        assert "# More Reddit comments" in rendered
        assert "Expanded public reply" in rendered
        assert "↳ u/bob" in rendered
        assert "u/bob" in rendered
        assert "children=jkl012" in rendered
        assert (
            "Permalink: https://www.reddit.com/comments/abc123/_/def456/"
        ) in rendered
        assert (
            "Parent context: https://www.reddit.com/comments/abc123/"
        ) in rendered
        assert (
            "Permalink: https://www.reddit.com/comments/abc123/_/ghi789/"
        ) in rendered
        assert "?context=3" in rendered

    async def test_morechildren_api_declared_failure_is_a_tool_error(self):
        route = route_reddit_url(
            "https://www.reddit.com/api/morechildren?"
            "link_id=t3_abc123&children=def456"
        )
        assert route is not None
        payload = {"jquery": [], "success": False}

        assert _payload_schema_error(route, 0, payload) == (
            "Reddit returned an invalid morechildren response."
        )
        session = _JsonSession([_JsonResponse(payload)])
        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert result == {
            "error": "Reddit reported that comment expansion failed."
        }
        assert "content" not in result

    @pytest.mark.parametrize(
        "payload",
        [
            {"jquery": []},
            {"jquery": [], "success": None},
            {"jquery": [], "success": "true"},
        ],
    )
    def test_morechildren_jquery_requires_explicit_boolean_success(self, payload):
        route = RedditRoute(
            "https://www.reddit.com/api/morechildren?"
            "link_id=t3_abc123&children=def456",
            "morechildren",
        )

        assert _payload_schema_error(route, 0, payload) is not None

    def test_mixed_search_results_render_posts_communities_and_users(self):
        payload = {
            "data": {
                "children": [
                    _post(id="post1"),
                    {
                        "kind": "t5",
                        "data": {
                            "display_name": "LearnPython",
                            "title": "Learning Python",
                            "subscribers": 100,
                            "url": "/r/learnpython/",
                        },
                    },
                    {
                        "kind": "t2",
                        "data": {
                            "name": "pythonista",
                            "link_karma": 12,
                            "comment_karma": 34,
                            "url": "/user/pythonista/",
                        },
                    },
                ],
                "after": "t3_next",
            }
        }
        rendered = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/search/?q=python&type=link,sr,user",
                "search",
                label="python",
            ),
            [payload],
            max_tokens=5000,
        )

        assert "A compact Reddit post" in rendered
        assert "r/LearnPython" in rendered
        assert "u/pythonista" in rendered
        assert "12 post karma" in rendered
        assert "Next page:" in rendered

    def test_related_array_renders_source_and_related_posts(self):
        payload = [
            {"data": {"children": [_post()]}},
            {
                "data": {
                    "children": [
                        _post(
                            id="related1",
                            title="A genuinely related post",
                            subreddit="learnpython",
                        )
                    ],
                    "dist": 1,
                    "after": "t3_next",
                }
            },
        ]
        output = render_reddit_route(
            RedditRoute(
                "https://www.reddit.com/related/post1/",
                "related",
            ),
            payload,
            max_tokens=5000,
        )

        assert "A compact Reddit post" in output
        assert "## Related posts" in output
        assert "A genuinely related post" in output
        assert "r/learnpython" in output
        assert "after=t3_next&count=1" in output

    def test_new_reddit_related_partial_parses_public_cards_not_ads(self):
        event = quote(
            json.dumps(
                {
                    "post": {
                        "id": "t3_related1",
                        "title": "A genuinely related post",
                        "subreddit_name": "learnpython",
                        "score": 42,
                        "number_comments": 7,
                        "created_timestamp": 1_700_000_000_000,
                        "type": "link",
                        "url": (
                            "https://www.reddit.com/r/learnpython/"
                            "comments/related1/title/"
                        ),
                    }
                }
            ),
            safe="",
        )
        promoted = quote(
            json.dumps(
                {
                    "post": {
                        "id": "t3_adpost",
                        "title": "Advertisement",
                        "subreddit_name": "ads",
                        "promoted": True,
                        "url": (
                            "https://www.reddit.com/r/ads/"
                            "comments/adpost/title/"
                        ),
                    }
                }
            ),
            safe="",
        )
        html = (
            f'<reddit-pdp-right-rail-post event-data="{event}">'
            '<a target="_blank" href="https://example.com/article">article</a>'
            "</reddit-pdp-right-rail-post>"
            f'<reddit-pdp-right-rail-post event-data="{promoted}">'
            "</reddit-pdp-right-rail-post>"
        )

        listing = parse_reddit_related_html(html, limit=10)
        children = listing["data"]["children"]

        assert listing["_related_partial_valid"] is True
        assert listing["_related_partial_invalid_count"] == 0
        assert len(children) == 1
        assert children[0]["data"] == {
            "id": "related1",
            "name": "t3_related1",
            "title": "A genuinely related post",
            "subreddit": "learnpython",
            "subreddit_name_prefixed": "r/learnpython",
            "author": "[unknown]",
            "score": 42,
            "num_comments": 7,
            "created_utc": 1_700_000_000,
            "permalink": "/r/learnpython/comments/related1/title/",
            "url": "https://example.com/article",
            "url_overridden_by_dest": "https://example.com/article",
            "is_self": False,
            "over_18": False,
            "archived": False,
        }

    def test_new_reddit_wiki_page_tree_parses_only_authoritative_right_rail(self):
        html = """
        <shreddit-app pagetype="community_wiki" routename="subreddit_wiki">
          <div id="canonical-url-updater"
               value="https://www.reddit.com/r/Python/wiki/pages/"></div>
          <a href="/r/Python/wiki/not-in-tree">Authored page link</a>
          <div id="wikis-right-rail-container">
            <div class="page-tree">
              <li title="index">
                <a href="/r/Python/wiki/index">index</a>
              </li>
              <li title="getting-started">
                <a href="https://www.reddit.com/r/python/wiki/faq/getting-started">
                  getting-started
                </a>
              </li>
              <li title="duplicate">
                <a href="/r/Python/wiki/INDEX/">duplicate</a>
              </li>
            </div>
          </div>
        </shreddit-app>
        """

        assert parse_reddit_wiki_pages_html(html, "Python") == [
            "index",
            "faq/getting-started",
        ]

    def test_new_reddit_wiki_tree_cannot_be_spliced_from_decoy_shells(self):
        html = """
        <div id="canonical-url-updater"
             value="https://www.reddit.com/r/Python/wiki/pages/"></div>
        <div id="wikis-right-rail-container">
          <div class="page-tree">
            <a href="/r/Python/wiki/decoy">decoy</a>
          </div>
        </div>
        <shreddit-app pagetype="community_wiki" routename="subreddit_wiki">
          <p>Matching route shell without its own canonical tree.</p>
        </shreddit-app>
        """

        assert parse_reddit_wiki_pages_html(html, "Python") is None

    @pytest.mark.parametrize(
        "html",
        [
            "",
            "<title>Reddit - Please wait for verification</title>",
            (
                '<shreddit-app pagetype="community_wiki" '
                'routename="subreddit_wiki">'
                '<div id="canonical-url-updater" '
                'value="https://www.reddit.com/r/Python/wiki/pages/"></div>'
                "</shreddit-app>"
            ),
            (
                '<shreddit-app pagetype="community_wiki" '
                'routename="subreddit_wiki">'
                '<div id="canonical-url-updater" '
                'value="https://www.reddit.com/r/Other/wiki/pages/"></div>'
                '<div id="wikis-right-rail-container">'
                '<div class="page-tree">'
                '<a href="/r/Python/wiki/index">index</a>'
                "</div></div></shreddit-app>"
            ),
            (
                '<shreddit-app pagetype="community_wiki" '
                'routename="subreddit_wiki">'
                '<div id="canonical-url-updater" '
                'value="https://www.reddit.com:bad/r/Python/wiki/pages/"></div>'
                '<div id="wikis-right-rail-container">'
                '<div class="page-tree">'
                '<a href="/r/Python/wiki/index">index</a>'
                "</div></div></shreddit-app>"
            ),
        ],
    )
    def test_new_reddit_wiki_page_tree_rejects_shells_and_wrong_documents(
        self,
        html,
    ):
        assert parse_reddit_wiki_pages_html(html, "Python") is None

    @pytest.mark.parametrize(
        "href",
        [
            "https://old.reddit.com/r/Python/wiki/index",
            "https://www.reddit.com.evil.example/r/Python/wiki/index",
            "https://user@www.reddit.com/r/Python/wiki/index",
            "https://www.reddit.com:bad/r/Python/wiki/index",
            "/r/Other/wiki/index",
            "/r/Python/wiki/index?show_source=1",
            "/r/Python/wiki/index#section",
            "/r/Python/wiki/%2e%2e/private",
            "/r/Python/wiki/faq%2Fprivate",
            "/r/Python/wiki//hidden",
            "/r/Python/wiki/hidden//",
            "/r/Python/wiki/page%00name",
            "javascript:alert(1)",
        ],
    )
    def test_new_reddit_wiki_page_tree_rejects_ambiguous_or_foreign_links(
        self,
        href,
    ):
        html = f"""
        <shreddit-app pagetype="community_wiki" routename="subreddit_wiki">
          <div id="canonical-url-updater"
               value="https://www.reddit.com/r/Python/wiki/pages/"></div>
          <div id="wikis-right-rail-container">
            <div class="page-tree"><a href="{escape(href)}">page</a></div>
          </div>
        </shreddit-app>
        """

        assert parse_reddit_wiki_pages_html(html, "Python") is None


class _JsonResponse:
    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        headers: dict | None = None,
        url: str | None = None,
        history: list | None = None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        if url is not None:
            self.url = url
        if history is not None:
            self.history = history

    def json(self):
        return self.payload


class _NonJsonResponse(_JsonResponse):
    def json(self):
        raise ValueError("not JSON")


class _HtmlResponse(_JsonResponse):
    def __init__(
        self,
        html: str,
        status_code: int = 200,
        headers: dict | None = None,
        url: str | None = None,
    ):
        super().__init__(
            None,
            status_code,
            {"content-type": "text/html; charset=UTF-8", **(headers or {})},
        )
        self.text = html
        if url is not None:
            self.url = url


_CSRF_TOKEN = "3bc4389fce35d758de8213f3f6650ab2"
_GRAPHQL_URL = "https://www.reddit.com/svc/shreddit/graphql"


def _wiki_tree_node(
    path: str,
    *,
    present: bool = True,
    **overrides,
) -> dict:
    parts = path.split("/")
    node = {
        "name": parts[-1],
        "parent": "/".join(parts[:-1]) or None,
        "path": path,
        "pageTitle": None,
        "isPagePresent": present,
        "depth": len(parts) - 1,
    }
    node.update(overrides)
    return node


def _wiki_tree_response(
    nodes: list[dict],
    *,
    subreddit: str = "Python",
    status_code: int = 200,
    headers: dict | None = None,
    **subreddit_overrides,
) -> _JsonResponse:
    """Mirror the live anonymous ``WikiPageRevisionsV2`` response shape."""

    community = {
        "__typename": "Subreddit",
        "id": "t5_2qh0y",
        "name": subreddit,
        "prefixedName": f"r/{subreddit}",
        "wiki": {
            "page": {"pageTitle": None, "name": "index", "isRevisable": False},
            "index": {"pageTree": nodes},
        },
    }
    community.update(subreddit_overrides)
    return _JsonResponse(
        {"data": {"subreddit": community}},
        status_code,
        {"content-type": "application/json; charset=utf-8", **(headers or {})},
        url=_GRAPHQL_URL,
    )


class _JsonSession:
    def __init__(
        self,
        responses: list[_JsonResponse],
        *,
        csrf_token: str | None = _CSRF_TOKEN,
    ):
        self.responses = list(responses)
        self.calls: list[str] = []
        self.request_details: list[tuple[str, str, dict]] = []
        self.cookie_lookups: list[tuple[str, str]] = []
        self._csrf_token = csrf_token

    async def get(self, url: str, **kwargs):
        self.calls.append(url)
        self.request_details.append(("GET", url, kwargs))
        return self.responses.pop(0)

    async def post(self, url: str, **kwargs):
        self.calls.append(url)
        self.request_details.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get_cookie(self, name: str, url: str) -> str | None:
        self.cookie_lookups.append((name, url))
        return self._csrf_token if name == "csrf_token" else None


class TestRedditTransportAndTools:
    async def test_account_private_activity_403_does_not_poison_next_route(
        self,
    ):
        class ImmediateQueue:
            def __init__(self):
                self.backoffs = []

            async def enqueue(self, callback, *_args, **_kwargs):
                return await callback()

            def set_backoff(self, status_code, retry_after=None):
                self.backoffs.append((status_code, retry_after))

        session = _JsonSession([
            _JsonResponse(
                {"message": "Forbidden", "error": 403},
                status_code=403,
            ),
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [_post()]},
            }),
        ])
        queue = ImmediateQueue()
        private_route = route_reddit_url(
            "https://www.reddit.com/user/spez/upvoted/"
        )
        public_route = route_reddit_url(
            "https://www.reddit.com/r/Python/?limit=1"
        )
        assert private_route is not None
        assert public_route is not None

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            private = await fetch_mapped_reddit(
                private_route,
                max_tokens=5_000,
                timeout=30,
                queue=queue,
            )
            public = await fetch_mapped_reddit(
                public_route,
                max_tokens=5_000,
                timeout=30,
                queue=queue,
            )

        assert private == {
            "error": (
                "Reddit account-private activity is not publicly readable."
            )
        }
        assert "error" not in public
        assert "A compact Reddit post" in public["content"]
        assert queue.backoffs == []

    async def test_gildings_given_is_exact_account_private_access_state(
        self,
    ):
        route = route_reddit_url(
            "https://www.reddit.com/user/spez/gilded/"
            "?show=given&limit=2"
        )
        assert route is not None
        assert route.label == "gilded given"

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(),
        ) as get_session:
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert result == {
            "error": (
                "Reddit account-private gildings given are not publicly "
                "readable."
            )
        }
        get_session.assert_not_awaited()

    async def test_randomrising_uses_exact_live_rising_pool_and_scope(
        self,
    ):
        first = _post(id="first", name="t3_first", title="FIRST")
        second = _post(id="second", name="t3_second", title="SECOND")
        session = _JsonSession([
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [first, second]},
            }),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/randomrising/?limit=2"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" not in result
        assert result["content"].startswith("# r/Python · randomrising")
        assert "FIRST" in result["content"]
        assert "SECOND" in result["content"]
        assert (
            urlparse(session.calls[0]).path
            == "/r/Python/rising.json"
        )

    async def test_randomrising_cursor_uses_one_stable_shuffled_rising_pool(
        self,
    ):
        posts = [
            _post(
                id=f"post{index}",
                name=f"t3_post{index}",
                title=f"POST {index}",
                permalink=(
                    f"/r/Python/comments/post{index}/post_{index}/"
                ),
            )
            for index in range(1, 7)
        ]
        first_payload = {
            "kind": "Listing",
            "data": {"children": posts},
        }
        second_payload = {
            "kind": "Listing",
            "data": {"children": list(reversed(posts))},
        }
        session = _JsonSession([
            _JsonResponse(first_payload),
            _JsonResponse(second_payload),
            _JsonResponse(first_payload),
        ])
        first_route = route_reddit_url(
            "https://www.reddit.com/r/Python/randomrising/?limit=2"
        )
        assert first_route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            first_result = await fetch_mapped_reddit(
                first_route,
                max_tokens=5_000,
                timeout=30,
            )
            cursor = re.search(
                r"[?&]after=(t3_[A-Za-z0-9]+)",
                first_result["content"],
            )
            assert cursor is not None
            second_route = route_reddit_url(
                "https://www.reddit.com/r/Python/randomrising/"
                f"?limit=2&after={cursor.group(1)}&count=2"
            )
            assert second_route is not None
            second_result = await fetch_mapped_reddit(
                second_route,
                max_tokens=5_000,
                timeout=30,
            )
            previous = re.search(
                r"\[Previous page: (https://www\.reddit\.com/[^\]]+)\]",
                second_result["content"],
            )
            assert previous is not None
            assert parse_qs(urlparse(previous.group(1)).query)["count"] == ["0"]
            previous_route = route_reddit_url(previous.group(1))
            assert previous_route is not None
            previous_result = await fetch_mapped_reddit(
                previous_route,
                max_tokens=5_000,
                timeout=30,
            )

        first_ids = set(
            re.findall(r"/comments/(post\d+)/", first_result["content"])
        )
        second_ids = set(
            re.findall(r"/comments/(post\d+)/", second_result["content"])
        )
        previous_ids = set(
            re.findall(r"/comments/(post\d+)/", previous_result["content"])
        )
        assert len(first_ids) == len(second_ids) == 2
        assert first_ids.isdisjoint(second_ids)
        assert previous_ids == first_ids
        next_round_trip = re.search(
            r"\[Next page: (https://www\.reddit\.com/[^\]]+)\]",
            previous_result["content"],
        )
        assert next_round_trip is not None
        assert parse_qs(urlparse(next_round_trip.group(1)).query)["count"] == [
            "2"
        ]

    async def test_global_comments_small_limit_overfetches_without_skipping(
        self,
    ):
        def comment_page(start: int) -> dict:
            return {
                "kind": "Listing",
                "data": {
                    "children": [
                        _comment(
                            f"c{index:02d}",
                            f"GLOBAL COMMENT {index:02d}",
                            name=f"t1_c{index:02d}",
                            subreddit="Python",
                            subreddit_name_prefixed="r/Python",
                            link_title=f"Parent {index:02d}",
                            permalink=(
                                "/r/Python/comments/post1/title/"
                                f"c{index:02d}/"
                            ),
                        )
                        for index in range(start, start + 25)
                    ],
                    "after": f"t1_c{start + 24:02d}",
                },
            }

        session = _JsonSession([
            _JsonResponse(comment_page(0)),
            _JsonResponse(comment_page(1)),
        ])
        first_route = route_reddit_url(
            "https://www.reddit.com/comments/?limit=1"
        )
        second_route = route_reddit_url(
            "https://www.reddit.com/comments/"
            "?limit=1&after=t1_c00&count=1"
        )
        assert first_route is not None
        assert second_route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            first = await fetch_mapped_reddit(
                first_route,
                max_tokens=5_000,
                timeout=30,
            )
            second = await fetch_mapped_reddit(
                second_route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" not in first
        assert "GLOBAL COMMENT 00" in first["content"]
        assert "GLOBAL COMMENT 01" not in first["content"]
        assert (
            "https://www.reddit.com/comments/"
            "?limit=1&after=t1_c00&count=1"
        ) in first["content"]
        assert "error" not in second
        assert "GLOBAL COMMENT 01" in second["content"]
        assert "GLOBAL COMMENT 02" not in second["content"]
        assert (
            "https://www.reddit.com/comments/"
            "?limit=1&after=t1_c01&count=2"
        ) in second["content"]

        first_query = parse_qs(urlparse(session.calls[0]).query)
        second_query = parse_qs(urlparse(session.calls[1]).query)
        assert first_query["limit"] == ["25"]
        assert second_query["limit"] == ["25"]
        assert second_query["after"] == ["c00"]

    async def test_multi_comments_reverse_page_restores_next_round_trip(
        self,
    ):
        metadata = {
            "kind": "LabeledMulti",
            "data": {
                "display_name": "DevOps",
                "owner": "alice",
                "visibility": "public",
                "subreddits": [{"name": "devops"}],
            },
        }

        def comment(comment_id: str) -> dict:
            return _comment(
                comment_id,
                f"MULTI COMMENT {comment_id}",
                name=f"t1_{comment_id}",
                subreddit="devops",
                subreddit_name_prefixed="r/devops",
                link_title=f"Parent {comment_id}",
                parent_id=f"t3_p{comment_id}",
                link_id=f"t3_p{comment_id}",
                permalink=(
                    f"/r/devops/comments/p{comment_id}/title/{comment_id}/"
                ),
            )

        def listing(
            ids: tuple[str, ...],
            *,
            before: str | None,
            after: str | None,
        ) -> dict:
            return {
                "kind": "Listing",
                "data": {
                    "children": [comment(comment_id) for comment_id in ids],
                    "dist": len(ids),
                    "before": before,
                    "after": after,
                },
            }

        first_payload = listing(
            ("c01", "c02", "c03"),
            before=None,
            after="t1_c03",
        )
        second_payload = listing(
            ("c04", "c05", "c06"),
            before="t1_c04",
            after="t1_c06",
        )
        # This is the exact native-JSON gap: a valid `before` request returns
        # the correct first page but Reddit omits its forward cursor.
        native_reverse_payload = listing(
            ("c01", "c02", "c03"),
            before=None,
            after=None,
        )
        session = _JsonSession(
            [
                _JsonResponse(metadata),
                _JsonResponse(first_payload),
                _JsonResponse(metadata),
                _JsonResponse(second_payload),
                _JsonResponse(metadata),
                _JsonResponse(native_reverse_payload),
                _JsonResponse(metadata),
                _JsonResponse(second_payload),
            ]
        )
        first_route = route_reddit_url(
            "https://www.reddit.com/user/alice/m/devops/comments/?limit=3"
        )
        assert first_route is not None

        async def fetch(route: RedditRoute) -> dict:
            return await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            first = await fetch(first_route)
            first_next = re.search(
                r"\[Next page: (https://www\.reddit\.com/[^\]]+)\]",
                first["content"],
            )
            assert first_next is not None
            second_route = route_reddit_url(first_next.group(1))
            assert second_route is not None
            second = await fetch(second_route)
            previous_link = re.search(
                r"\[Previous page: (https://www\.reddit\.com/[^\]]+)\]",
                second["content"],
            )
            assert previous_link is not None
            previous_route = route_reddit_url(previous_link.group(1))
            assert previous_route is not None
            previous = await fetch(previous_route)
            restored_next = re.search(
                r"\[Next page: (https://www\.reddit\.com/[^\]]+)\]",
                previous["content"],
            )
            assert restored_next is not None
            restored_next_query = parse_qs(
                urlparse(restored_next.group(1)).query
            )
            assert restored_next_query["after"] == ["t1_c03"]
            assert restored_next_query["count"] == ["3"]
            round_trip_route = route_reddit_url(restored_next.group(1))
            assert round_trip_route is not None
            round_trip = await fetch(round_trip_route)

        def ids(result: dict) -> set[str]:
            return set(
                re.findall(
                    r"/comments/pc\d+/title/(c\d+)/",
                    result["content"],
                )
            )

        assert ids(first) == {"c01", "c02", "c03"}
        assert ids(second) == {"c04", "c05", "c06"}
        assert ids(previous) == ids(first)
        assert ids(round_trip) == ids(second)
        assert len(session.responses) == 0

    def test_reverse_cursor_restoration_is_exact_and_supports_wrapped_listings(
        self,
    ):
        source = {
            "kind": "Listing",
            "data": {
                "children": [
                    _post(id="source", name="t3_source"),
                ],
                "after": None,
            },
        }
        duplicates = {
            "kind": "Listing",
            "data": {
                "children": [
                    _post(id="first", name="t3_first"),
                    _post(id="last", name="t3_last"),
                ],
                "after": None,
            },
        }
        payload = [source, duplicates]
        restored = _restore_reverse_listing_after(
            payload,
            "https://www.reddit.com/duplicates/source/"
            "?limit=2&before=t3_later&count=0",
        )

        assert restored is not payload
        assert restored[0] is source
        assert restored[1]["data"]["after"] == "t3_last"
        assert duplicates["data"]["after"] is None

        composite_negative_pages = (
            {
                "kind": "Listing",
                "data": {
                    "children": [
                        _post(id="native", name="t3_native"),
                    ],
                    "after": "t3_native",
                },
            },
            {
                "kind": "Listing",
                "data": {
                    "children": [],
                    "after": None,
                },
            },
            {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t3",
                            "data": {"name": "t1_wrongkind"},
                        }
                    ],
                    "after": None,
                },
            },
        )
        for final_listing in composite_negative_pages:
            wrapped = [source, final_listing]
            unchanged = _restore_reverse_listing_after(
                wrapped,
                "https://www.reddit.com/duplicates/source/"
                "?limit=2&before=t3_later&count=0",
            )
            assert unchanged is wrapped
            assert source["data"]["after"] is None
            assert final_listing["data"]["after"] in {
                None,
                "t3_native",
            }

        community = {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t5",
                        "data": {
                            "name": "t5_python",
                            "display_name": "Python",
                        },
                    }
                ],
                "after": None,
            },
        }
        restored_community = _restore_reverse_listing_after(
            community,
            "https://www.reddit.com/subreddits/popular/"
            "?limit=1&before=t5_later&count=0",
        )
        assert restored_community["data"]["after"] == "t5_python"

        for unsafe_url in (
            "https://www.reddit.com/duplicates/source/?before=bad&count=0",
            (
                "https://www.reddit.com/duplicates/source/"
                "?before=t3_later&before=t3_other&count=0"
            ),
            (
                "https://www.reddit.com/duplicates/source/"
                "?before=t3_later&after=t3_other&count=0"
            ),
        ):
            assert (
                _restore_reverse_listing_after(payload, unsafe_url)
                is payload
            )

        already_forward = {
            "kind": "Listing",
            "data": {
                "children": [
                    _post(id="first", name="t3_first"),
                ],
                "after": "t3_native",
            },
        }
        assert (
            _restore_reverse_listing_after(
                already_forward,
                "https://www.reddit.com/new/"
                "?before=t3_later&count=0",
            )
            is already_forward
        )

    async def test_global_comments_overfetch_requires_visible_t1_fullname(
        self,
    ):
        malformed = _comment(
            "comment1",
            "body",
            subreddit="Python",
            permalink="/r/Python/comments/post1/title/comment1/",
        )
        session = _JsonSession([
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [malformed]},
            }),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/comments/?limit=1"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert result == {
            "error": "Reddit returned an invalid global comment page."
        }

    async def test_multireddit_feed_rejects_items_outside_metadata_membership(
        self,
    ):
        metadata = {
            "kind": "LabeledMulti",
            "data": {
                "display_name": "devops",
                "owner": "alice",
                "subreddits": [{"name": "devops"}, {"name": "sysadmin"}],
            },
        }
        session = _JsonSession([
            _JsonResponse(metadata),
            _JsonResponse({
                "kind": "Listing",
                "data": {
                    "children": [
                        _post(subreddit="unrelated", subreddit_name_prefixed="r/unrelated")
                    ]
                },
            }),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/user/alice/m/devops/search/?q=python"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert result == {
            "error": (
                "Reddit returned an item outside the requested "
                "multireddit communities."
            )
        }

    async def test_multireddit_feed_accepts_case_insensitive_exact_membership(
        self,
    ):
        metadata = {
            "kind": "LabeledMulti",
            "data": {
                "display_name": "devops",
                "owner": "alice",
                "subreddits": [{"name": "DevOps"}, {"name": "sysadmin"}],
            },
        }
        session = _JsonSession([
            _JsonResponse(metadata),
            _JsonResponse({
                "kind": "Listing",
                "data": {
                    "children": [
                        _post(subreddit="devops", subreddit_name_prefixed="r/devops")
                    ]
                },
            }),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/user/alice/m/devops/new/?limit=1"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" not in result
        assert "A compact Reddit post" in result["content"]

    def test_archived_gilded_parser_requires_awarded_exact_scope_things(self):
        html = """
        <div class="thing gilded" data-fullname="t1_comment1"
             data-subreddit="Python"></div>
        <div class="thing gilded" data-fullname="t3_post1"
             data-subreddit="Python" data-gildings="2"></div>
        <div class="thing" data-fullname="t3_notgilded"
             data-subreddit="Python" data-gildings="0"></div>
        <div class="thing gilded" data-fullname="t3_foreign"
             data-subreddit="learnpython" data-gildings="1"></div>
        <span class="next-button">
          <a href="https://www.reddit.com/r/Python/gilded/?after=t3_post1">
            next
          </a>
        </span>
        """

        assert _parse_archived_gilded(
            html,
            subreddit="Python",
            username=None,
            comments_only=False,
        ) == (
            ["t1_comment1", "t3_post1"],
            {"t1_comment1": None, "t3_post1": 2},
            "https://www.reddit.com/r/Python/gilded/?after=t3_post1",
        )
        assert _parse_archived_gilded(
            html,
            subreddit="Python",
            username=None,
            comments_only=True,
        ) == (
            ["t1_comment1"],
            {"t1_comment1": None},
            "https://www.reddit.com/r/Python/gilded/?after=t3_post1",
        )
        assert _parse_archived_gilded(
            '<div class="thing" data-fullname="t3_post1"></div>',
            subreddit=None,
            username=None,
            comments_only=False,
        ) is None

    async def test_retired_gilded_uses_archived_order_and_current_hydration(
        self,
    ):
        timestamp = "20230530081438"
        original = "https://www.reddit.com/r/Python/gilded/"
        archive_html = """
        <div class="thing gilded" data-fullname="t1_comment1"
             data-subreddit="Python" data-gildings="1"></div>
        <div class="thing gilded" data-fullname="t3_post1"
             data-subreddit="Python" data-gildings="2"></div>
        <span class="next-button">
          <a href="https://www.reddit.com/r/Python/gilded/?count=2&amp;after=t3_post1">
            next
          </a>
        </span>
        """
        next_archive_html = """
        <div class="thing gilded" data-fullname="t1_comment2"
             data-subreddit="Python"></div>
        """
        comment = _comment(
            "comment1",
            "Current hydrated comment body",
            parent_id="t3_post1",
            permalink="/r/Python/comments/post1/title/comment1/",
            gilded=1,
        )
        post = _post(gilded=2)
        session = _JsonSession([
            _JsonResponse([
                ["timestamp", "original", "statuscode", "mimetype"],
                [timestamp, original, "200", "text/html"],
            ]),
            _HtmlResponse(archive_html),
            _HtmlResponse(next_archive_html),
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [comment, post]},
            }),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/gilded/?limit=2",
            max_tokens=5_000,
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" not in result
        assert result["content"].startswith("# r/Python · gilded")
        assert (
            "Gilded ordering source: archived Reddit snapshot "
            "(Wayback, 2023-05-30). Item details: current Reddit API."
        ) in result["content"]
        assert "Current hydrated comment body" in result["content"]
        assert "A compact Reddit post" in result["content"]
        assert (
            "Archived gilding evidence: 1 gilding in the exact archived "
            "Reddit snapshot"
        ) in result["content"]
        assert (
            "Archived gilding evidence: 2 gildings in the exact archived "
            "Reddit snapshot"
        ) in result["content"]
        assert "[Next page:" in result["content"]
        assert "/r/Python/gilded.json" not in "\n".join(session.calls)
        assert "/api/info.json?" in session.calls[-1]

    async def test_archived_gilded_next_previous_round_trip_preserves_count(
        self,
    ):
        timestamp = "20230530081438"
        original = "https://www.reddit.com/r/Python/gilded/"
        archive_html = "\n".join(
            (
                f'<div class="thing gilded" data-fullname="t1_comment{index}" '
                'data-subreddit="Python" data-gildings="1"></div>'
            )
            for index in range(1, 5)
        )

        def current(index: int) -> dict:
            return _comment(
                f"comment{index}",
                f"CURRENT GILDED COMMENT {index}",
                name=f"t1_comment{index}",
                parent_id="t3_post1",
                link_id="t3_post1",
                subreddit="Python",
                subreddit_name_prefixed="r/Python",
                permalink=(
                    "/r/Python/comments/post1/title/"
                    f"comment{index}/"
                ),
                gilded=0,
            )

        cdx = _JsonResponse([
            ["timestamp", "original", "statuscode", "mimetype"],
            [timestamp, original, "200", "text/html"],
        ])
        session = _JsonSession([
            cdx,
            _HtmlResponse(archive_html),
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [current(1), current(2)]},
            }),
            cdx,
            _HtmlResponse(archive_html),
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [current(3), current(4)]},
            }),
            cdx,
            _HtmlResponse(archive_html),
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [current(1), current(2)]},
            }),
        ])
        first_route = route_reddit_url(
            "https://www.reddit.com/r/Python/gilded/?limit=2"
        )
        assert first_route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            first = await fetch_mapped_reddit(
                first_route,
                max_tokens=5_000,
                timeout=30,
            )
            next_link = re.search(
                r"\[Next page: (https://www\.reddit\.com/[^\]]+)\]",
                first["content"],
            )
            assert next_link is not None
            second_route = route_reddit_url(next_link.group(1))
            assert second_route is not None
            second = await fetch_mapped_reddit(
                second_route,
                max_tokens=5_000,
                timeout=30,
            )
            previous_link = re.search(
                r"\[Previous page: (https://www\.reddit\.com/[^\]]+)\]",
                second["content"],
            )
            assert previous_link is not None
            assert parse_qs(
                urlparse(previous_link.group(1)).query
            )["count"] == ["0"]
            previous_route = route_reddit_url(previous_link.group(1))
            assert previous_route is not None
            previous = await fetch_mapped_reddit(
                previous_route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "CURRENT GILDED COMMENT 1" in first["content"]
        assert "CURRENT GILDED COMMENT 2" in first["content"]
        assert "CURRENT GILDED COMMENT 3" in second["content"]
        assert "CURRENT GILDED COMMENT 4" in second["content"]
        assert previous["content"].count("CURRENT GILDED COMMENT") == 2
        assert "CURRENT GILDED COMMENT 1" in previous["content"]
        assert "CURRENT GILDED COMMENT 2" in previous["content"]
        assert "[Previous page:" not in previous["content"]
        next_round_trip = re.search(
            r"\[Next page: (https://www\.reddit\.com/[^\]]+)\]",
            previous["content"],
        )
        assert next_round_trip is not None
        assert parse_qs(
            urlparse(next_round_trip.group(1)).query
        )["count"] == ["2"]

    @pytest.mark.parametrize(
        ("url", "timestamp", "original", "heading"),
        [
            (
                "https://www.reddit.com/gilded/?limit=2",
                "20170523232153",
                "https://www.reddit.com/gilded/",
                "# Reddit · gilded",
            ),
            (
                "https://www.reddit.com/comments/gilded/?limit=2",
                "20150509040515",
                "http://www.reddit.com:80/comments/gilded",
                "# Reddit · comments gilded",
            ),
        ],
    )
    async def test_global_gilded_surfaces_use_exact_pinned_historical_identity(
        self,
        url,
        timestamp,
        original,
        heading,
    ):
        archive_html = """
        <div class="thing gilded" data-fullname="t1_comment1"
             data-gildings="1"></div>
        <div class="thing gilded" data-fullname="t1_comment2"
             data-gildings="1"></div>
        """
        comments = [
            _comment(
                f"comment{index}",
                f"Current comment {index}",
                name=f"t1_comment{index}",
                parent_id="t3_post1",
                link_id="t3_post1",
                subreddit="Python",
                subreddit_name_prefixed="r/Python",
                permalink=(
                    "/r/Python/comments/post1/title/"
                    f"comment{index}/"
                ),
                gilded=0 if index == 1 else 1,
            )
            for index in (1, 2)
        ]
        snapshot_url = (
            f"https://web.archive.org/web/{timestamp}id_/{original}"
        )
        session = _JsonSession([
            _HtmlResponse(archive_html, url=snapshot_url),
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": comments},
            }),
        ])
        route = route_reddit_url(url, max_tokens=5_000)
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" not in result
        assert result["content"].startswith(heading)
        assert result["content"].count("Permalink:") == 2
        assert result["content"].count("Parent context:") == 2
        assert result["content"].count("Archived gilding evidence:") == 2
        assert session.calls[0] == snapshot_url
        assert all("/cdx/" not in call for call in session.calls)
        if "comments/gilded" in url:
            assert all(
                "/web/" not in call or "/comments/gilded" in call
                for call in session.calls
            )

    async def test_gilded_archive_boundary_never_emits_unusable_next_page(
        self,
    ):
        archive_html = """
        <div class="thing gilded" data-fullname="t1_comment1"></div>
        <div class="thing gilded" data-fullname="t1_comment2"></div>
        <div class="thing gilded" data-fullname="t1_comment3"></div>
        <span class="next-button">
          <a href="https://www.reddit.com/gilded/?count=3&amp;after=t1_comment3">
            next
          </a>
        </span>
        """
        unavailable_next = (
            "https://web.archive.org/web/20170523232153id_/"
            "https://www.reddit.com/gilded/?count=3&after=t1_comment3"
        )
        current = _comment(
            "comment3",
            "CURRENT FINAL ARCHIVED GILDED ITEM",
            name="t1_comment3",
            parent_id="t3_post1",
            link_id="t3_post1",
            subreddit="Python",
            subreddit_name_prefixed="r/Python",
            permalink="/r/Python/comments/post1/title/comment3/",
            gilded=0,
        )
        session = _JsonSession([
            _HtmlResponse(
                archive_html,
                url=(
                    "https://web.archive.org/web/20170523232153id_/"
                    "https://www.reddit.com/gilded/"
                ),
            ),
            _HtmlResponse("", status_code=404, url=unavailable_next),
            _JsonResponse({
                "kind": "Listing",
                "data": {"children": [current]},
            }),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/gilded/"
            "?limit=2&after=t1_comment2&count=2"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" not in result
        assert "CURRENT FINAL ARCHIVED GILDED ITEM" in result["content"]
        assert "Archived gilding evidence: Gilded in the exact" in result["content"]
        assert "[Next page:" not in result["content"]
        assert unavailable_next in session.calls

    async def test_retired_gold_directory_preserves_exact_archived_empty_state(
        self,
    ):
        timestamp = "20180823171238"
        original = "https://www.reddit.com/subreddits/gold/"
        snapshot_url = (
            f"https://web.archive.org/web/{timestamp}id_/{original}"
        )
        html = """
        <html>
          <head>
            <link rel="canonical"
                  href="https://www.reddit.com/subreddits/gold/">
          </head>
          <body>
            <div id="siteTable" class="sitetable linklisting">
              <p id="noresults" class="error">
                there doesn't seem to be anything here
              </p>
            </div>
          </body>
        </html>
        """
        session = _JsonSession([
            _HtmlResponse(html, url=snapshot_url),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/subreddits/gold/?limit=3"
        )
        assert route is not None
        assert route.kind == "subreddit_directory"
        assert route.label == "gold"

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" not in result
        assert result["content"].startswith(
            "# Reddit communities · gold\n\n0 items returned"
        )
        assert (
            "Directory state source: exact archived Reddit snapshot "
            "(Wayback, 2018-08-23). No gold-only communities were listed."
        ) in result["content"]
        assert session.calls == [snapshot_url]

    @pytest.mark.parametrize(
        "html",
        [
            '<link rel="canonical" href="https://www.reddit.com/subreddits/gold/">',
            """
            <link rel="canonical"
                  href="https://www.reddit.com/subreddits/popular/">
            <div id="siteTable" class="sitetable linklisting">
              <p id="noresults" class="error">empty</p>
            </div>
            """,
            """
            <link rel="canonical"
                  href="https://www.reddit.com/subreddits/gold/">
            <div id="siteTable" class="sitetable linklisting">
              <div class="thing" data-fullname="t5_substitute"></div>
              <p id="noresults" class="error">empty</p>
            </div>
            """,
        ],
    )
    async def test_retired_gold_directory_rejects_inexact_archive(
        self,
        html,
    ):
        timestamp = "20180823171238"
        original = "https://www.reddit.com/subreddits/gold/"
        snapshot_url = (
            f"https://web.archive.org/web/{timestamp}id_/{original}"
        )
        session = _JsonSession([
            _HtmlResponse(html, url=snapshot_url),
        ])
        route = route_reddit_url(original)
        assert route is not None

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert "error" in result

    async def test_retired_gilded_rejects_substituted_current_hydration(self):
        timestamp = "20230530081438"
        original = "https://www.reddit.com/r/Python/gilded/"
        archive_html = """
        <div class="thing gilded" data-fullname="t3_post1"
             data-subreddit="Python" data-gildings="1"></div>
        """
        session = _JsonSession([
            _JsonResponse([
                ["timestamp", "original", "statuscode", "mimetype"],
                [timestamp, original, "200", "text/html"],
            ]),
            _HtmlResponse(archive_html),
            _JsonResponse({
                "kind": "Listing",
                "data": {
                    "children": [_post(id="substitute", name="t3_substitute")]
                },
            }),
        ])
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/gilded/?limit=1",
            max_tokens=5_000,
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5_000,
                timeout=30,
            )

        assert result == {
            "error": (
                "No exact archived Reddit gilded listing with complete current "
                "hydration was available."
            )
        }

    def test_user_directory_routes_bound_each_fully_hydrated_page_to_one_user(
        self,
    ):
        for url in (
            "https://www.reddit.com/users/",
            "https://www.reddit.com/users/popular/?limit=100",
            "https://www.reddit.com/users/new/?limit=25",
            "https://www.reddit.com/users/search/?q=python&limit=50",
        ):
            route = route_reddit_url(url, max_tokens=25_000)
            assert route is not None
            assert route.kind == "user_directory"
            assert parse_qs(urlparse(route.requests[0]).query)["limit"] == ["1"]

    async def test_user_directory_hydrates_exact_account_karma_and_creation(
        self,
    ):
        directory = {
            "kind": "Listing",
            "data": {
                "after": "t5_next",
                "dist": 1,
                "children": [{
                    "kind": "t5",
                    "data": {
                        "name": "t5_3oy63",
                        "display_name_prefixed": "u/thisisinsider",
                        "title": "Business Insider",
                        "created_utc": 1_507_123_989,
                        "url": "/user/thisisinsider/",
                    },
                }],
            },
        }
        about = {
            "kind": "t2",
            "data": {
                "id": "epphs8c",
                "name": "thisisinsider",
                "link_karma": 16_483,
                "comment_karma": 10_271,
                "created_utc": 1_506_103_969,
            },
        }
        session = _JsonSession([_JsonResponse(directory), _JsonResponse(about)])
        route = route_reddit_url(
            "https://www.reddit.com/users/popular/?limit=100",
            max_tokens=25_000,
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=25_000,
                timeout=30,
            )

        assert "error" not in result
        assert "16,483 post karma" in result["content"]
        assert "10,271 comment karma" in result["content"]
        assert "Created: 2017-09-22 18:12 UTC" in result["content"]
        assert (
            "Public activity: "
            "https://www.reddit.com/user/thisisinsider/overview/"
        ) in result["content"]
        assert "[Next page:" in result["content"]
        assert session.calls == [
            _transport_url(route.requests[0]),
            _transport_url(
                "https://www.reddit.com/user/thisisinsider/"
                "about.json?raw_json=1"
            ),
        ]

    async def test_user_search_hydrates_t2_name_identity(self):
        directory = {
            "kind": "Listing",
            "data": {
                "children": [{
                    "kind": "t2",
                    "data": {"name": "python"},
                }],
            },
        }
        about = {
            "kind": "t2",
            "data": {
                "name": "python",
                "link_karma": 1,
                "comment_karma": 2,
                "created_utc": 1_700_000_000,
            },
        }
        session = _JsonSession([_JsonResponse(about)])
        with patch(
            "fetchaller.tools.browse_reddit.reddit_limiter.wait",
            AsyncMock(),
        ):
            result = await _hydrate_user_directory(
                directory,
                session=session,
                queue=None,
                deadline=time.monotonic() + 10,
            )

        assert "error" not in result
        card = result["data"]["data"]["children"][0]["data"]
        assert card["name"] == "python"
        assert card["link_karma"] == 1
        assert card["comment_karma"] == 2

    @pytest.mark.parametrize(
        "about",
        [
            {},
            {"kind": "t2", "data": {"name": "substituted"}},
            {
                "kind": "t2",
                "data": {
                    "name": "alice",
                    "comment_karma": 1,
                    "created_utc": 1_700_000_000,
                },
            },
            {
                "kind": "t2",
                "data": {
                    "name": "alice",
                    "link_karma": 1,
                    "comment_karma": 2,
                    "created_utc": 0,
                },
            },
        ],
    )
    async def test_user_directory_rejects_substituted_or_incomplete_hydration(
        self,
        about,
    ):
        directory = {
            "kind": "Listing",
            "data": {
                "children": [{
                    "kind": "t5",
                    "data": {
                        "display_name_prefixed": "u/alice",
                        "url": "/user/alice/",
                    },
                }],
            },
        }
        session = _JsonSession([_JsonResponse(about)])

        with patch(
            "fetchaller.tools.browse_reddit.reddit_limiter.wait",
            AsyncMock(),
        ):
            result = await _hydrate_user_directory(
                directory,
                session=session,
                queue=None,
                deadline=time.monotonic() + 10,
            )

        assert result == {
            "error": (
                "Reddit user directory profile hydration returned a "
                "substituted or incomplete account."
            )
        }

    async def test_user_directory_rejects_missing_identity_and_timeout(self):
        missing_identity = {
            "kind": "Listing",
            "data": {"children": [{"kind": "t5", "data": {"name": "t5_bad"}}]},
        }
        result = await _hydrate_user_directory(
            missing_identity,
            session=_JsonSession([]),
            queue=None,
            deadline=time.monotonic() + 10,
        )
        assert "lacked an exact username" in result["error"]

        directory = {
            "kind": "Listing",
            "data": {
                "children": [{
                    "kind": "t5",
                    "data": {"display_name_prefixed": "u/alice"},
                }],
            },
        }
        result = await _hydrate_user_directory(
            directory,
            session=_JsonSession([]),
            queue=None,
            deadline=time.monotonic() - 1,
        )
        assert result == {
            "error": "Reddit user directory profile hydration timed out."
        }

    async def test_wiki_pages_fetches_and_renders_canonical_new_reddit_ssr_tree(
        self,
    ):
        html = """
        <shreddit-app pagetype="community_wiki" routename="subreddit_wiki">
          <div id="canonical-url-updater"
               value="https://www.reddit.com/r/Python/wiki/pages/"></div>
          <div id="wikis-right-rail-container">
            <div class="page-tree">
              <a href="/r/Python/wiki/index">index</a>
              <a href="/r/Python/wiki/faq/getting-started">
                getting-started
              </a>
            </div>
          </div>
        </shreddit-app>
        """
        session = _JsonSession([_HtmlResponse(html)])
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.reddit_limiter.wait",
                AsyncMock(),
            ) as wait,
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert result["content_type"] == "markdown"
        assert "# Wiki pages for r/Python" in result["content"]
        assert "2 pages returned" in result["content"]
        assert "/wiki/index/" in result["content"]
        assert "/wiki/faq/getting-started/" in result["content"]
        assert session.calls == [
            "https://www.reddit.com/r/Python/wiki/pages/"
        ]
        assert session.request_details[0][2]["headers"]["Accept"].startswith(
            "text/html"
        )
        assert "Authorization" not in session.request_details[0][2]["headers"]
        assert wait.await_count == 1

    @pytest.mark.parametrize(
        "response",
        [
            _HtmlResponse(
                "<title>Reddit - Please wait for verification</title>"
            ),
            _HtmlResponse(
                """
                <shreddit-app pagetype="community_wiki"
                              routename="subreddit_wiki">
                  <div id="canonical-url-updater"
                       value="https://www.reddit.com/r/Python/wiki/pages/">
                  </div>
                  <main>An unknown error occurred</main>
                </shreddit-app>
                """
            ),
        ],
    )
    async def test_wiki_pages_rejects_shell_or_error_as_zero_page_success(
        self,
        response,
    ):
        session = _JsonSession(
            [
                response,
                _JsonResponse(
                    {"data": {"subreddit": None}},
                    headers={"content-type": "application/json"},
                    url=_GRAPHQL_URL,
                ),
            ]
        )
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert result == {
            "error": (
                "Reddit did not return a public wiki-page tree: Reddit "
                "returned an invalid anonymous wiki page tree for r/Python."
            )
        }
        assert "content" not in result

    async def test_wiki_pages_uses_shared_queue_and_applies_retry_after(self):
        session = _JsonSession(
            [
                _HtmlResponse(
                    "",
                    status_code=429,
                    headers={"retry-after": "13"},
                )
            ]
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback()

        queue.enqueue = AsyncMock(side_effect=enqueue)
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                queue=queue,
            )

        assert result == {
            "error": "Rate limited by Reddit. Retry after 13s."
        }
        queue.enqueue.assert_awaited_once()
        queue.set_backoff.assert_called_once_with(429, retry_after=13.0)

    async def test_wiki_pages_exact_anonymous_403_uses_oauth_without_backoff(
        self,
    ):
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None
        session = _JsonSession(
            [
                _HtmlResponse(
                    "",
                    status_code=403,
                    url=route.requests[0],
                ),
                _JsonResponse(
                    {"errors": [{"message": "unauthorized"}]},
                    status_code=500,
                    headers={"content-type": "application/json"},
                    url=_GRAPHQL_URL,
                ),
            ]
        )
        manager = Mock()
        manager.fetch_wiki_pages = AsyncMock(
            return_value={
                "data": {
                    "kind": "wikipagelisting",
                    "data": ["index", "faq"],
                }
            }
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback()

        queue.enqueue = AsyncMock(side_effect=enqueue)

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.get_reddit_moderator_oauth",
                return_value=manager,
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                config=Config(reddit_access_token="direct-token"),
                queue=queue,
            )

        assert result["content_type"] == "markdown"
        assert "Source: exact Reddit OAuth" in result["content"]
        manager.fetch_wiki_pages.assert_awaited_once()
        queue.set_backoff.assert_not_called()

    @pytest.mark.parametrize(
        "response",
        [
            _JsonResponse(
                {"reason": "unknown_account_boundary"},
                status_code=403,
                url="https://www.reddit.com/r/Python/wiki/pages/",
            ),
            _HtmlResponse(
                "",
                status_code=403,
                url="https://www.reddit.com/r/Python/wiki/index/",
            ),
        ],
        ids=("structured", "different-route"),
    )
    async def test_wiki_pages_403_fails_closed_outside_exact_auth_boundary(
        self,
        response,
    ):
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None
        session = _JsonSession([response])
        manager = Mock()
        manager.fetch_wiki_pages = AsyncMock()
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback()

        queue.enqueue = AsyncMock(side_effect=enqueue)

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.get_reddit_moderator_oauth",
                return_value=manager,
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                config=Config(reddit_access_token="direct-token"),
                queue=queue,
            )

        assert result == {"error": "Reddit returned HTTP 403."}
        manager.fetch_wiki_pages.assert_not_awaited()
        queue.set_backoff.assert_called_once_with(403, retry_after=None)

    @pytest.mark.parametrize(
        "response_url",
        [
            "https://old.reddit.com/r/Python/wiki/pages/",
            "https://www.reddit.com.evil.example/r/Python/wiki/pages/",
            "https://user@www.reddit.com/r/Python/wiki/pages/",
            "https://www.reddit.com:444/r/Python/wiki/pages/",
        ],
    )
    async def test_wiki_pages_rejects_html_redirected_off_fixed_new_origin(
        self,
        response_url,
    ):
        session = _JsonSession(
            [
                _HtmlResponse(
                    """
                    <shreddit-app pagetype="community_wiki"
                                  routename="subreddit_wiki">
                      <div id="canonical-url-updater"
                           value="https://www.reddit.com/r/Python/wiki/pages/">
                      </div>
                      <div id="wikis-right-rail-container">
                        <div class="page-tree">
                          <a href="/r/Python/wiki/index">index</a>
                        </div>
                      </div>
                    </shreddit-app>
                    """,
                    url=response_url,
                )
            ]
        )
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert result == {
            "error": "New Reddit HTML fetch left the fixed Reddit origin."
        }

    async def test_off_origin_html_response_cannot_poison_reddit_backoff(
        self,
    ):
        session = _JsonSession(
            [
                _HtmlResponse(
                    "",
                    status_code=429,
                    headers={"retry-after": "300"},
                    url="https://evil.example/r/Python/wiki/pages/",
                )
            ]
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback()

        queue.enqueue = AsyncMock(side_effect=enqueue)
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                queue=queue,
            )

        assert result == {
            "error": "New Reddit HTML fetch left the fixed Reddit origin."
        }
        queue.set_backoff.assert_not_called()

    async def test_wiki_pages_uses_exact_oauth_fallback_after_new_ssr_gap(
        self,
    ):
        session = _JsonSession(
            [
                _HtmlResponse(
                    """
                    <shreddit-app pagetype="community_wiki"
                                  routename="subreddit_wiki">
                      <div id="canonical-url-updater"
                           value="https://www.reddit.com/r/Python/wiki/pages/">
                      </div>
                      <main>An unknown error occurred</main>
                    </shreddit-app>
                    """
                ),
                _JsonResponse(
                    {"data": {"subreddit": {"__typename": "Subreddit"}}},
                    headers={"content-type": "application/json"},
                    url=_GRAPHQL_URL,
                ),
            ]
        )
        manager = Mock()
        manager.fetch_wiki_pages = AsyncMock(
            return_value={
                "data": {
                    "kind": "wikipagelisting",
                    "data": ["index", "faq", "config/sidebar"],
                }
            }
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback()

        queue.enqueue = AsyncMock(side_effect=enqueue)
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.reddit_limiter.wait",
                AsyncMock(),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.get_reddit_moderator_oauth",
                return_value=manager,
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                config=Config(reddit_access_token="direct-token"),
                queue=queue,
            )

        assert result["content_type"] == "markdown"
        assert "3 pages returned" in result["content"]
        assert "/wiki/faq/" in result["content"]
        assert "/wiki/config/sidebar/" in result["content"]
        manager.fetch_wiki_pages.assert_awaited_once()
        subreddit, used_session, used_queue, remaining = (
            manager.fetch_wiki_pages.await_args.args
        )
        assert subreddit == "Python"
        assert used_session is session
        assert used_queue is queue
        assert 0 < remaining <= 10
        assert session.calls == [
            "https://www.reddit.com/r/Python/wiki/pages/",
            _GRAPHQL_URL,
        ]

    async def test_wiki_pages_rejects_malformed_oauth_index_not_empty_success(
        self,
    ):
        session = _JsonSession(
            [
                _HtmlResponse("<html></html>"),
                _JsonResponse(
                    {"data": {"subreddit": None}},
                    headers={"content-type": "application/json"},
                    url=_GRAPHQL_URL,
                ),
            ]
        )
        manager = Mock()
        manager.fetch_wiki_pages = AsyncMock(
            return_value={
                "data": {
                    "kind": "wikipagelisting",
                    "data": ["index", "../private"],
                }
            }
        )
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.reddit_limiter.wait",
                AsyncMock(),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.get_reddit_moderator_oauth",
                return_value=manager,
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                config=Config(reddit_access_token="direct-token"),
            )

        assert result == {
            "error": "Reddit returned an invalid wiki pages response."
        }
        assert "content" not in result

    async def test_wiki_pages_does_not_start_oauth_after_ssr_uses_deadline(
        self,
    ):
        session = _JsonSession([_HtmlResponse("<html></html>")])
        manager = Mock()
        manager.fetch_wiki_pages = AsyncMock()
        clock = Mock()
        # deadline, anonymous page-tree budget check, OAuth budget check.
        clock.monotonic.side_effect = [100.0, 111.0, 111.0]
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch._fetch_reddit_html",
                AsyncMock(return_value={"html": "<html></html>"}),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.get_reddit_moderator_oauth",
                return_value=manager,
            ),
            patch(
                "fetchaller.tools.reddit_fetch.time",
                clock,
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                config=Config(reddit_access_token="direct-token"),
            )

        assert result == {"error": "Request timed out (10s limit)"}
        manager.fetch_wiki_pages.assert_not_awaited()

    async def test_wiki_pages_does_not_send_oauth_when_new_ssr_is_complete(
        self,
    ):
        html = """
        <shreddit-app pagetype="community_wiki" routename="subreddit_wiki">
          <div id="canonical-url-updater"
               value="https://www.reddit.com/r/Python/wiki/pages/"></div>
          <div id="wikis-right-rail-container">
            <div class="page-tree">
              <a href="/r/Python/wiki/index">index</a>
            </div>
          </div>
        </shreddit-app>
        """
        session = _JsonSession([_HtmlResponse(html)])
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.reddit_limiter.wait",
                AsyncMock(),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.get_reddit_moderator_oauth",
            ) as get_oauth,
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                config=Config(reddit_access_token="direct-token"),
            )

        assert "1 pages returned" in result["content"]
        get_oauth.assert_not_called()

    async def _anonymous_wiki_pages(
        self,
        graphql_response,
        *,
        csrf_token: str | None = _CSRF_TOKEN,
        queue=None,
        config=None,
        oauth=False,
    ):
        """Drive the wiki index past an SSR gap onto the anonymous page tree."""

        responses = [_HtmlResponse("<html></html>")]
        if graphql_response is not None:
            responses.append(graphql_response)
        session = _JsonSession(responses, csrf_token=csrf_token)
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.reddit_limiter.wait",
                AsyncMock(),
            ),
            patch(
                "fetchaller.tools.reddit_fetch.get_reddit_moderator_oauth",
            ) as get_oauth,
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5000,
                timeout=10,
                config=config,
                queue=queue,
            )
        return result, session, get_oauth

    async def test_wiki_pages_uses_anonymous_page_tree_without_oauth(self):
        result, session, get_oauth = await self._anonymous_wiki_pages(
            _wiki_tree_response(
                [
                    _wiki_tree_node("config", present=False),
                    _wiki_tree_node("config/sidebar"),
                    _wiki_tree_node("faq"),
                    _wiki_tree_node("index"),
                ]
            ),
            config=Config(reddit_access_token="direct-token"),
        )

        assert result["content_type"] == "markdown"
        assert "Source: anonymous Reddit wiki page tree" in result["content"]
        # ``config`` is a namespace parent, not a page Reddit will serve.
        assert "3 pages returned" in result["content"]
        assert "/wiki/config/sidebar/" in result["content"]
        assert "/wiki/config/)" not in result["content"]
        get_oauth.assert_not_called()

        method, url, kwargs = session.request_details[1]
        assert (method, url) == ("POST", _GRAPHQL_URL)
        assert kwargs["json"] == {
            "operation": "WikiPageRevisionsV2",
            "variables": {"subredditName": "Python", "wikiPageName": "index"},
            "csrf_token": _CSRF_TOKEN,
        }
        assert kwargs["headers"]["Origin"] == "https://www.reddit.com"
        assert kwargs["headers"]["Referer"] == (
            "https://www.reddit.com/r/Python/wiki/pages/"
        )
        assert "Authorization" not in kwargs["headers"]
        assert session.cookie_lookups == [
            ("csrf_token", "https://www.reddit.com/")
        ]

    async def test_wiki_pages_anonymous_tree_allows_genuinely_empty_wiki(self):
        result, _session, _get_oauth = await self._anonymous_wiki_pages(
            _wiki_tree_response([_wiki_tree_node("config", present=False)])
        )

        assert result["content_type"] == "markdown"
        assert "0 pages returned" in result["content"]

    @pytest.mark.parametrize(
        "nodes",
        [
            pytest.param(
                [_wiki_tree_node("index", depth=3)],
                id="depth-disagrees",
            ),
            pytest.param(
                [_wiki_tree_node("config/sidebar", parent=None)],
                id="parent-disagrees",
            ),
            pytest.param(
                [_wiki_tree_node("config/sidebar", name="index")],
                id="name-disagrees",
            ),
            pytest.param(
                [_wiki_tree_node("index"), _wiki_tree_node("Index")],
                id="duplicate-path",
            ),
            pytest.param(
                [_wiki_tree_node("../private")],
                id="traversal",
            ),
            pytest.param(
                [_wiki_tree_node("index", isPagePresent="true")],
                id="non-bool-presence",
            ),
            pytest.param(
                [_wiki_tree_node("index\nfaq")],
                id="control-character",
            ),
        ],
    )
    async def test_wiki_pages_rejects_malformed_anonymous_tree(self, nodes):
        result, _session, _get_oauth = await self._anonymous_wiki_pages(
            _wiki_tree_response(nodes)
        )

        assert result == {
            "error": (
                "Reddit did not return a public wiki-page tree: Reddit "
                "returned an invalid anonymous wiki page tree for r/Python."
            )
        }

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"name": "AskReddit"}, id="other-subreddit"),
            pytest.param({"__typename": "Redditor"}, id="wrong-typename"),
            pytest.param({"prefixedName": "r/AskReddit"}, id="prefix-mismatch"),
        ],
    )
    async def test_wiki_pages_rejects_anonymous_tree_for_other_identity(
        self,
        overrides,
    ):
        result, _session, _get_oauth = await self._anonymous_wiki_pages(
            _wiki_tree_response([_wiki_tree_node("index")], **overrides)
        )

        assert "content" not in result
        assert "invalid anonymous wiki page tree" in result["error"]

    async def test_wiki_pages_rejects_anonymous_graphql_errors(self):
        response = _wiki_tree_response([_wiki_tree_node("index")])
        response.payload["errors"] = [{"message": "forbidden"}]
        result, _session, _get_oauth = await self._anonymous_wiki_pages(response)

        assert "content" not in result
        assert "invalid anonymous wiki page tree" in result["error"]

    async def test_wiki_pages_rejects_non_json_anonymous_response(self):
        result, _session, _get_oauth = await self._anonymous_wiki_pages(
            _HtmlResponse("<html></html>", url=_GRAPHQL_URL)
        )

        assert result == {
            "error": (
                "Reddit did not return a public wiki-page tree: Reddit wiki "
                "page index returned a non-JSON response."
            )
        }

    async def test_wiki_pages_rejects_anonymous_response_off_fixed_route(self):
        result, _session, _get_oauth = await self._anonymous_wiki_pages(
            _wiki_tree_response(
                [_wiki_tree_node("index")],
            ),
        )
        assert "content" in result

        redirected = _wiki_tree_response([_wiki_tree_node("index")])
        redirected.url = "https://www.reddit.com/login/"
        result, _session, _get_oauth = await self._anonymous_wiki_pages(
            redirected
        )

        assert result == {
            "error": (
                "Reddit did not return a public wiki-page tree: Reddit wiki "
                "page index left its fixed anonymous route."
            )
        }

    async def test_wiki_pages_reports_missing_csrf_without_sending_post(self):
        result, session, _get_oauth = await self._anonymous_wiki_pages(
            None,
            csrf_token=None,
        )

        assert result == {
            "error": (
                "Reddit did not return a public wiki-page tree: Reddit did "
                "not issue the CSRF token its anonymous wiki page index "
                "requires."
            )
        }
        assert session.calls == [
            "https://www.reddit.com/r/Python/wiki/pages/"
        ]

    async def test_wiki_pages_rejects_non_hex_csrf_without_sending_post(self):
        result, session, _get_oauth = await self._anonymous_wiki_pages(
            None,
            csrf_token="not-a-real-token",
        )

        assert "CSRF token" in result["error"]
        assert session.calls == [
            "https://www.reddit.com/r/Python/wiki/pages/"
        ]

    async def test_wiki_pages_anonymous_429_applies_shared_backoff(self):
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback()

        queue.enqueue = AsyncMock(side_effect=enqueue)
        result, _session, _get_oauth = await self._anonymous_wiki_pages(
            _wiki_tree_response(
                [_wiki_tree_node("index")],
                status_code=429,
                headers={"retry-after": "11"},
            ),
            queue=queue,
        )

        assert "content" not in result
        assert "HTTP 429" in result["error"]
        queue.set_backoff.assert_called_once_with(429, retry_after=11.0)

    async def test_wiki_page_index_oauth_uses_only_fixed_host_and_bearer_header(
        self,
    ):
        session = _JsonSession(
            [
                _JsonResponse(
                    {
                        "kind": "wikipagelisting",
                        "data": ["index", "faq"],
                    }
                )
            ]
        )
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "Python",
            session,
            None,
            10,
        )

        assert result == {
            "data": {
                "kind": "wikipagelisting",
                "data": ["index", "faq"],
            }
        }
        assert session.calls == [
            "https://oauth.reddit.com/r/Python/wiki/pages/?raw_json=1"
        ]
        request = session.request_details[0][2]
        assert request["headers"]["Authorization"] == "Bearer direct-token"
        assert request["headers"]["Accept"] == "application/json"
        assert request["max_response_size"] == 2 * 1024 * 1024
        assert "direct-token" not in repr(result)

    async def test_wiki_page_index_oauth_names_missing_wikiread_scope(self):
        session = _JsonSession(
            [_JsonResponse({}, status_code=403)]
        )
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "Python",
            session,
            None,
            10,
        )

        assert result == {
            "error": (
                "Reddit wiki page index access was forbidden; verify the "
                "account and OAuth wikiread scope."
            )
        }

    async def test_wiki_page_index_oauth_refreshes_rejected_token_once(self):
        session = _JsonSession(
            [
                _JsonResponse({}, status_code=401),
                _JsonResponse(
                    {"access_token": "replacement", "expires_in": 3600}
                ),
                _JsonResponse(
                    {
                        "kind": "wikipagelisting",
                        "data": ["index", "faq"],
                    }
                ),
            ]
        )
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            access_token="rejected-token",
        )

        result = await manager.fetch_wiki_pages(
            "Python",
            session,
            None,
            10,
        )

        assert result == {
            "data": {
                "kind": "wikipagelisting",
                "data": ["index", "faq"],
            }
        }
        assert [
            (method, url)
            for method, url, _kwargs in session.request_details
        ] == [
            (
                "GET",
                "https://oauth.reddit.com/r/Python/wiki/pages/?raw_json=1",
            ),
            ("POST", "https://www.reddit.com/api/v1/access_token"),
            (
                "GET",
                "https://oauth.reddit.com/r/Python/wiki/pages/?raw_json=1",
            ),
        ]
        assert (
            session.request_details[0][2]["headers"]["Authorization"]
            == "Bearer rejected-token"
        )
        assert (
            session.request_details[2][2]["headers"]["Authorization"]
            == "Bearer replacement"
        )

    async def test_wiki_page_index_oauth_queue_timeout_is_bounded(self):
        queue = AsyncMock()
        queue.enqueue.side_effect = TimeoutError
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "Python",
            _JsonSession([]),
            queue,
            3,
        )

        assert result == {
            "error": "Reddit wiki page index request timed out."
        }

    async def test_wiki_page_index_oauth_429_applies_shared_backoff(self):
        session = _JsonSession(
            [_JsonResponse({}, status_code=429, headers={"retry-after": "19"})]
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback()

        queue.enqueue = AsyncMock(side_effect=enqueue)
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "Python",
            session,
            queue,
            10,
        )

        assert result == {
            "error": (
                "Reddit wiki page index was rate limited. Retry after 19s."
            )
        }
        queue.set_backoff.assert_called_once_with(429, retry_after=19.0)

    async def test_wiki_page_index_oauth_rejects_invalid_subreddit_before_send(
        self,
    ):
        session = _JsonSession([])
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "../secrets",
            session,
            None,
            10,
        )

        assert result == {"error": "Invalid subreddit name"}
        assert session.calls == []

    async def test_wiki_page_index_transport_cannot_leak_oauth_secrets(
        self,
        caplog,
        capsys,
    ):
        class ExplodingSession:
            async def get(self, *_args, **_kwargs):
                raise RuntimeError(
                    "direct-token client-secret refresh-token must stay private"
                )

        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "Python",
            ExplodingSession(),
            None,
            10,
        )
        captured = capsys.readouterr()
        combined = (
            repr(result)
            + caplog.text
            + captured.out
            + captured.err
            + repr(manager)
        )

        assert result == {
            "error": "Reddit wiki page index request failed."
        }
        assert "direct-token" not in combined
        assert "client-secret" not in combined
        assert "refresh-token" not in combined

    async def test_wiki_page_index_oauth_rejects_non_json_response(self):
        session = _JsonSession([_NonJsonResponse(None)])
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "Python",
            session,
            None,
            10,
        )

        assert result == {
            "error": "Reddit wiki page index returned an invalid response."
        }

    @pytest.mark.parametrize(
        ("url", "history"),
        [
            (
                "https://oauth.reddit.com/r/Other/wiki/pages/?raw_json=1",
                [],
            ),
            (
                "https://evil.example/r/Python/wiki/pages/?raw_json=1",
                [],
            ),
            (
                "https://oauth.reddit.com/r/Python/wiki/pages/?raw_json=1",
                [object()],
            ),
        ],
    )
    async def test_wiki_page_index_oauth_rejects_every_redirect(
        self,
        url,
        history,
    ):
        session = _JsonSession(
            [
                _JsonResponse(
                    {
                        "kind": "wikipagelisting",
                        "data": ["index"],
                    },
                    url=url,
                    history=history,
                )
            ]
        )
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_wiki_pages(
            "Python",
            session,
            None,
            10,
        )

        assert result == {
            "error": (
                "Reddit wiki page index request left its exact endpoint."
            )
        }

    def test_structured_access_states_do_not_request_backoff(self):
        cases = [
            (403, {"reason": "quarantined"}, "email-verified", False),
            (403, {"data": {"reason": "private"}}, "private Reddit community", False),
            (403, {"reason": "gated"}, "requires account access", False),
            (404, {"reason": "banned"}, "has been banned", False),
            (404, {}, "not found", False),
            (403, {}, "Access forbidden", True),
        ]
        for status, payload, text, backoff in cases:
            message, should_backoff = format_reddit_http_error(status, payload)
            assert text in message
            assert should_backoff is backoff

    @pytest.mark.parametrize(
        ("status", "payload", "message"),
        [
            (403, {"reason": "private"}, "private Reddit community"),
            (403, {"reason": "quarantined"}, "quarantined"),
            (404, {"reason": "banned"}, "has been banned"),
            (404, {}, "not found"),
        ],
    )
    async def test_fetch_reddit_json_maps_content_states_without_poisoning_queue(
        self,
        status,
        payload,
        message,
    ):
        session = _JsonSession([_JsonResponse(payload, status_code=status)])
        queue = AsyncMock()

        # No queue wrapper here: we specifically verify the status mapper and
        # queue backoff side effect by invoking the callback through a tiny
        # passthrough enqueue implementation.
        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue.side_effect = enqueue
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/private/about.json",
            session,
            queue,
        )

        assert message in result["data"]["_reddit_content_state"]
        assert "error" not in result
        queue.set_backoff.assert_not_called()

    @pytest.mark.parametrize(
        ("status", "payload", "message"),
        [
            (403, {"reason": "private"}, "private Reddit community"),
            (403, {"reason": "quarantined"}, "quarantined"),
            (404, {"reason": "banned"}, "has been banned"),
            (404, {}, "not found"),
        ],
    )
    async def test_mapped_content_states_are_successful_readable_content(
        self,
        status,
        payload,
        message,
    ):
        session = _JsonSession([_JsonResponse(payload, status_code=status)])
        route = RedditRoute(
            "https://www.reddit.com/r/example/about/",
            "subreddit_about",
            ("https://www.reddit.com/r/example/about.json",),
            subreddit="example",
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(route, max_tokens=1000, timeout=10)

        assert "error" not in result
        assert message in result["content"]

    async def test_retry_after_is_applied_to_shared_queue(self):
        session = _JsonSession(
            [_JsonResponse({}, status_code=429, headers={"retry-after": "137"})]
        )
        queue = Mock()
        queue.enqueue = AsyncMock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue.side_effect = enqueue
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            session,
            queue,
        )

        assert result == {
            "error": (
                "Rate limited. Reddit allows ~10 requests/min. "
                "Retry after 137s."
            )
        }
        queue.set_backoff.assert_called_once_with(429, retry_after=137.0)

    async def test_retry_after_is_applied_to_unstructured_403(self):
        session = _JsonSession(
            [_JsonResponse({}, status_code=403, headers={"retry-after": "42"})]
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue = AsyncMock(side_effect=enqueue)
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            session,
            queue,
        )

        assert "HTTP 403" in result["error"]
        # Retry-After still wins; default_delay is only the fallback, and an
        # unrecognised 403 keeps the conservative one.
        queue.set_backoff.assert_called_once_with(
            403, retry_after=42.0, default_delay=300.0
        )

    async def test_moderator_403_is_an_auth_boundary_not_queue_backoff(self):
        session = _JsonSession([_JsonResponse({}, status_code=403)])
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue = AsyncMock(side_effect=enqueue)
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/about/moderators.json",
            session,
            queue,
            auth_required_on_403=True,
        )

        assert result == {"auth_required": True}
        queue.set_backoff.assert_not_called()

    async def test_structured_unknown_403_cannot_trigger_moderator_oauth(self):
        session = _JsonSession(
            [_JsonResponse({"reason": "some_new_reason"}, status_code=403)]
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue = AsyncMock(side_effect=enqueue)
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/about/moderators.json",
            session,
            queue,
            auth_required_on_403=True,
        )

        assert result == {"error": "Access forbidden by Reddit (HTTP 403)."}
        # Unrecognised 403 -> conservative fallback, and the generic message is
        # kept (the session-gate wording must not be claimed without evidence).
        queue.set_backoff.assert_called_once_with(
            403, retry_after=None, default_delay=300.0
        )

    async def test_nonexistent_subreddit_redirect_is_a_not_found_content_state(self):
        source = "https://www.reddit.com/r/zznotfound987654321/hot.json?raw_json=1"
        target = (
            "https://www.reddit.com/subreddits/search.json"
            "?q=zznotfound987654321"
        )
        session = _JsonSession(
            [_JsonResponse(None, status_code=302, headers={"location": target})]
        )
        queue = AsyncMock()

        async def enqueue(callback, *args, **_kwargs):
            return await callback(*args)

        queue.enqueue.side_effect = enqueue
        result = await fetch_reddit_json(source, session, queue)

        assert result == {
            "data": {"_reddit_content_state": "Reddit content not found."}
        }
        assert session.calls == [_transport_url(source)]
        assert queue.enqueue.await_count == 1

    async def test_nonexistent_subreddit_redirect_renders_readable_mapped_content(self):
        source = (
            "https://www.reddit.com/r/zznotfound987654321/"
            "hot.json?limit=250&raw_json=1"
        )
        target = (
            "https://www.reddit.com/subreddits/search.json"
            "?q=zznotfound987654321"
        )
        session = _JsonSession(
            [_JsonResponse(None, status_code=302, headers={"location": target})]
        )
        route = RedditRoute(
            "https://www.reddit.com/r/zznotfound987654321/",
            "listing",
            (source,),
            subreddit="zznotfound987654321",
        )

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert "error" not in result
        assert "Reddit content not found." in result["content"]

    async def test_search_redirect_with_extra_query_is_not_misclassified(self):
        source = "https://www.reddit.com/r/Python/hot.json"
        target = (
            "https://www.reddit.com/subreddits/search.json"
            "?q=Python&extra=unexpected"
        )
        payload = {"kind": "Listing", "data": {"children": []}}
        session = _JsonSession(
            [
                _JsonResponse(None, 302, {"location": target}),
                _JsonResponse(payload),
            ]
        )

        result = await fetch_reddit_json(source, session)

        assert result == {"error": "Reddit returned an unsafe JSON redirect."}
        assert session.calls == [_transport_url(source)]

    async def test_missing_subreddit_redirect_matches_immediately_preceding_hop(
        self,
    ):
        source = "https://www.reddit.com/r/Python/hot.json"
        intermediate = "https://www.reddit.com/r/Rust/hot.json"
        target = "https://www.reddit.com/subreddits/search.json?q=Python"
        payload = {"kind": "Listing", "data": {"children": []}}
        session = _JsonSession(
            [
                _JsonResponse(None, 302, {"location": intermediate}),
                _JsonResponse(None, 302, {"location": target}),
                _JsonResponse(payload),
            ]
        )

        result = await fetch_reddit_json(source, session)

        assert result == {"error": "Reddit returned an unsafe JSON redirect."}
        assert session.calls == [_transport_url(source)]

    async def test_safe_same_origin_json_redirect_is_followed_under_one_deadline(self):
        source = "https://www.reddit.com/r/Python/hot.json?limit=250&raw_json=1"
        target = "https://www.reddit.com/r/Python/hot.json?raw_json=1&limit=250"
        payload = {"kind": "Listing", "data": {"children": []}}
        session = _JsonSession(
            [
                _JsonResponse(None, status_code=302, headers={"location": target}),
                _JsonResponse(payload),
            ]
        )
        queue = AsyncMock()

        async def enqueue(callback, *args, **_kwargs):
            return await callback(*args)

        queue.enqueue.side_effect = enqueue
        result = await fetch_reddit_json(source, session, queue)

        assert result == {"data": payload}
        assert session.calls == [
            _transport_url(source),
            _transport_url(target),
        ]
        assert queue.enqueue.await_count == 2
        timeouts = [
            details[2]["timeout"] for details in session.request_details
        ]
        assert timeouts[1] <= timeouts[0]

    async def test_sticky_redirect_keeps_same_subreddit_and_bounded_thread_scope(self):
        source = (
            "https://www.reddit.com/r/Python/about/sticky.json?"
            "sort=new&limit=2&depth=1&num=1&raw_json=1"
        )
        redirect = (
            "https://www.reddit.com/r/Python/comments/abc123/"
            "sticky_title/.json"
        )
        expected_target = (
            f"{redirect}?sort=new&limit=2&depth=1&raw_json=1"
        )
        payload = _thread(_post(id="abc123"), [])
        session = _JsonSession(
            [
                _JsonResponse(
                    None,
                    status_code=302,
                    headers={"location": redirect},
                ),
                _JsonResponse(payload),
            ]
        )

        result = await fetch_reddit_json(source, session)

        assert result == {"data": payload}
        assert session.calls == [
            _transport_url(source),
            _transport_url(expected_target),
        ]

    async def test_sticky_redirect_cannot_cross_to_another_subreddit(self):
        source = (
            "https://www.reddit.com/r/Python/about/sticky.json?"
            "limit=2&raw_json=1"
        )
        redirect = (
            "https://www.reddit.com/r/Rust/comments/abc123/"
            "sticky_title/.json"
        )
        session = _JsonSession(
            [
                _JsonResponse(
                    None,
                    status_code=302,
                    headers={"location": redirect},
                )
            ]
        )

        result = await fetch_reddit_json(source, session)

        assert result == {"error": "Reddit returned an unsafe JSON redirect."}
        assert session.calls == [_transport_url(source)]

    async def test_same_origin_redirect_cannot_substitute_another_route(self):
        source = "https://www.reddit.com/r/Python/hot.json?raw_json=1"
        target = "https://www.reddit.com/r/Python/new.json?raw_json=1"
        session = _JsonSession(
            [_JsonResponse(None, status_code=302, headers={"location": target})]
        )

        result = await fetch_reddit_json(source, session)

        assert result == {"error": "Reddit returned an unsafe JSON redirect."}
        assert session.calls == [_transport_url(source)]

    async def test_redirected_moderator_403_cannot_trigger_oauth(self):
        source = (
            "https://www.reddit.com/r/Python/about/moderators.json?"
            "raw_json=1&limit=500"
        )
        equivalent_target = (
            "https://www.reddit.com/r/Python/about/moderators.json?"
            "limit=500&raw_json=1"
        )
        session = _JsonSession(
            [
                _JsonResponse(
                    None,
                    status_code=302,
                    headers={"location": equivalent_target},
                ),
                _JsonResponse({}, status_code=403),
            ]
        )

        with patch("fetchaller.tools.browse_reddit.reddit_limiter.defer"):
            result = await fetch_reddit_json(
                source,
                session,
                auth_required_on_403=True,
            )

        assert result == {"error": "Access forbidden by Reddit (HTTP 403)."}
        assert session.calls == [
            _transport_url(source),
            _transport_url(equivalent_target),
        ]

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            (
                _JsonResponse(None, status_code=302),
                "without a Location",
            ),
            (
                _JsonResponse(
                    None,
                    status_code=302,
                    headers={
                        "location": "https://evil.example/steal.json"
                    },
                ),
                "unsafe JSON redirect",
            ),
            (
                _JsonResponse(
                    None,
                    status_code=302,
                    headers={
                        "location": "https://oauth.reddit.com/api/v1/me.json"
                    },
                ),
                "unsafe JSON redirect",
            ),
        ],
    )
    async def test_reddit_json_redirect_rejects_missing_or_cross_origin_target(
        self,
        response,
        message,
    ):
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            _JsonSession([response]),
        )

        assert message in result["error"]

    async def test_reddit_json_redirect_loop_is_detected(self):
        first = "https://www.reddit.com/r/Python/hot.json?limit=250&raw_json=1"
        second = "https://www.reddit.com/r/Python/hot.json?raw_json=1&limit=250"
        session = _JsonSession(
            [
                _JsonResponse(None, 302, {"location": second}),
                _JsonResponse(None, 302, {"location": first}),
            ]
        )

        result = await fetch_reddit_json(first, session)

        assert result == {"error": "Reddit JSON redirect loop detected."}
        assert session.calls == [
            _transport_url(first),
            _transport_url(second),
        ]

    async def test_reddit_json_redirect_count_is_bounded(self):
        query_orders = [
            "a=1&b=2&c=3&d=4&e=5&f=6&g=7",
            "b=2&c=3&d=4&e=5&f=6&g=7&a=1",
            "c=3&d=4&e=5&f=6&g=7&a=1&b=2",
            "d=4&e=5&f=6&g=7&a=1&b=2&c=3",
            "e=5&f=6&g=7&a=1&b=2&c=3&d=4",
            "f=6&g=7&a=1&b=2&c=3&d=4&e=5",
            "g=7&a=1&b=2&c=3&d=4&e=5&f=6",
        ]
        source = f"https://www.reddit.com/r/Python/hot.json?{query_orders[0]}"
        responses = [
            _JsonResponse(
                None,
                302,
                {
                    "location": (
                        "https://www.reddit.com/r/Python/hot.json?"
                        f"{query_orders[index + 1]}"
                    )
                },
            )
            for index in range(6)
        ]
        session = _JsonSession(responses)

        result = await fetch_reddit_json(source, session)

        assert result == {"error": "Too many Reddit JSON redirects."}
        assert len(session.calls) == 6

    async def test_non_json_success_after_redirect_is_not_false_success(self):
        source = "https://www.reddit.com/r/Python/hot.json?limit=250&raw_json=1"
        target = "https://www.reddit.com/r/Python/hot.json?raw_json=1&limit=250"
        session = _JsonSession(
            [
                _JsonResponse(None, 302, {"location": target}),
                _NonJsonResponse("<html>not json</html>"),
            ]
        )

        result = await fetch_reddit_json(source, session)

        assert result == {"error": "Reddit returned a non-JSON response."}

    async def test_fetch_mapped_by_id_preserves_comment_only_result(self):
        route = route_reddit_url("https://www.reddit.com/by_id/t1_comment1/")
        assert route is not None
        session = _JsonSession(
            [
                _JsonResponse(
                    {
                        "kind": "Listing",
                        "data": {
                            "children": [
                                _comment(
                                    "comment1",
                                    "FETCHED BY-ID COMMENT",
                                    subreddit="Python",
                                    subreddit_name_prefixed="r/Python",
                                    link_title="Fetched parent",
                                    permalink=(
                                        "/r/Python/comments/post1/title/comment1/"
                                    ),
                                )
                            ]
                        },
                    }
                )
            ]
        )

        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(route, max_tokens=1000, timeout=10)

        assert "FETCHED BY-ID COMMENT" in result["content"]
        assert result["url"] == "https://www.reddit.com/by_id/t1_comment1/"
        assert session.calls == [_transport_url(route.requests[0])]

    async def test_fetch_mapped_related_uses_new_reddit_partial_and_lower_limit(self):
        route = route_reddit_url(
            "https://www.reddit.com/related/post1/?limit=1"
        )
        assert route is not None
        source = {
            "kind": "Listing",
            "data": {
                "children": [
                    _post(id="post1", subreddit="Python")
                ]
            },
        }

        def related_event(post_id: str, title: str) -> str:
            return quote(
                json.dumps(
                    {
                        "post": {
                            "id": f"t3_{post_id}",
                            "title": title,
                            "subreddit_name": "learnpython",
                            "score": 10,
                            "number_comments": 2,
                            "created_timestamp": 1_700_000_000_000,
                            "type": "link",
                            "url": (
                                "https://www.reddit.com/r/learnpython/"
                                f"comments/{post_id}/title/"
                            ),
                        }
                    }
                ),
                safe="",
            )

        html = (
            "<aside>"
            f'<reddit-pdp-right-rail-post event-data="'
            f'{related_event("related1", "First related")}">'
            "</reddit-pdp-right-rail-post>"
            f'<reddit-pdp-right-rail-post event-data="'
            f'{related_event("related2", "Second related")}">'
            "</reddit-pdp-right-rail-post>"
            "</aside>"
        )
        session = _JsonSession(
            [
                _JsonResponse(source),
                _HtmlResponse(
                    html,
                    headers={
                        "content-type": (
                            "text/vnd.reddit.partial+html; charset=utf-8"
                        )
                    },
                ),
                _JsonResponse(
                    {
                        "data": {
                            "children": [
                                _post(
                                    id="related1",
                                    title="First related",
                                    author="real_author",
                                    subreddit="learnpython",
                                    subreddit_name_prefixed="r/learnpython",
                                    permalink=(
                                        "/r/learnpython/comments/"
                                        "related1/title/"
                                    ),
                                )
                            ]
                        }
                    }
                ),
            ]
        )
        queue = AsyncMock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue.side_effect = enqueue
        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                queue=queue,
            )

        assert "First related" in result["content"]
        assert "Second related" not in result["content"]
        assert "u/real_author" in result["content"]
        assert "u/[unknown]" not in result["content"]
        assert "1 items returned" in result["content"]
        assert session.calls[0] == _transport_url(route.requests[0])
        assert session.calls[1].startswith(
            "https://www.reddit.com/svc/shreddit/"
            "pdp-right-rail/related/Python/t3_post1?"
        )
        assert session.calls[2].startswith(
            "https://api.reddit.com/api/info.json?"
        )
        assert parse_qs(urlparse(session.calls[2]).query)["id"] == [
            "t3_related1"
        ]
        assert "old.reddit.com" not in result["content"]
        assert queue.enqueue.await_count == 3

    async def test_fetch_mapped_related_names_partial_failure(self):
        route = route_reddit_url("https://www.reddit.com/related/post1/")
        assert route is not None
        source = {
            "kind": "Listing",
            "data": {
                "children": [
                    _post(id="post1", subreddit="Python")
                ]
            },
        }
        session = _JsonSession(
            [
                _JsonResponse(source),
                _HtmlResponse("", status_code=500),
            ]
        )
        queue = AsyncMock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue.side_effect = enqueue
        with patch(
            "fetchaller.tools.reddit_fetch._get_session",
            AsyncMock(return_value=session),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                queue=queue,
            )

        assert "Related posts unavailable" in result["content"]
        assert "HTTP 500" in result["content"]
        assert "0 items returned" in result["content"]

    async def test_fetch_mapped_related_rejects_block_page_with_http_200(self):
        route = route_reddit_url("https://www.reddit.com/related/post1/")
        assert route is not None
        source = {
            "data": {
                "children": [_post(id="post1", subreddit="Python")]
            }
        }
        session = _JsonSession(
            [
                _JsonResponse(source),
                _HtmlResponse("<html><title>Blocked</title></html>"),
            ]
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert "Related posts unavailable" in result["content"]
        assert "invalid New Reddit related-post partial" in result["content"]
        assert len(session.calls) == 2

    async def test_fetch_mapped_related_preserves_cards_and_names_enrichment_failure(
        self,
    ):
        route = route_reddit_url("https://www.reddit.com/related/post1/")
        assert route is not None
        source = {
            "data": {
                "children": [_post(id="post1", subreddit="Python")]
            }
        }
        event = quote(
            json.dumps(
                {
                    "post": {
                        "id": "t3_related1",
                        "title": "Still visible",
                        "subreddit_name": "learnpython",
                        "score": 10,
                        "number_comments": 2,
                        "created_timestamp": 1_700_000_000_000,
                        "type": "self",
                        "url": (
                            "https://www.reddit.com/r/learnpython/"
                            "comments/related1/title/"
                        ),
                    }
                }
            ),
            safe="",
        )
        session = _JsonSession(
            [
                _JsonResponse(source),
                _HtmlResponse(
                    '<aside aria-label="Related Posts Section">'
                    f'<reddit-pdp-right-rail-post event-data="{event}">'
                    "</reddit-pdp-right-rail-post></aside>"
                ),
                _JsonResponse({}, status_code=500),
            ]
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert "Still visible" in result["content"]
        assert "u/[unknown]" in result["content"]
        assert "1 related post detail unavailable" in result["content"]
        assert "Reddit returned HTTP 500" in result["content"]
        assert len(session.calls) == 3

    async def test_fetch_mapped_related_names_malformed_partial_cards(self):
        route = route_reddit_url("https://www.reddit.com/related/post1/")
        assert route is not None
        source = {
            "data": {
                "children": [_post(id="post1", subreddit="Python")]
            }
        }
        session = _JsonSession(
            [
                _JsonResponse(source),
                _HtmlResponse(
                    "<aside aria-label=\"Related Posts Section\">"
                    "<reddit-pdp-right-rail-post event-data=\"not-json\">"
                    "</reddit-pdp-right-rail-post></aside>"
                ),
            ]
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert "0 items returned" in result["content"]
        assert "1 related post detail unavailable" in result["content"]
        assert "malformed related-post cards" in result["content"]
        assert len(session.calls) == 2

    async def test_moderator_auth_requirement_is_explicit_without_fabrication(self):
        session = _JsonSession([_JsonResponse({}, status_code=403)])

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                _moderator_route(),
                max_tokens=1000,
                timeout=10,
            )

        assert "requires user-context OAuth" in result["error"]
        assert "No moderator names were guessed or reconstructed" in result["error"]
        assert session.calls == [
            _transport_url(
                "https://www.reddit.com/r/Python/about/moderators.json?"
                "limit=500&raw_json=1"
            )
        ]
        assert "Authorization" not in session.request_details[0][2]["headers"]

    async def test_anonymous_moderator_success_never_uses_configured_token(self):
        session = _JsonSession([_JsonResponse(_moderators_payload())])
        config = Config(reddit_access_token="direct-token")

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                _moderator_route(),
                max_tokens=1000,
                timeout=10,
                config=config,
            )

        assert "u/exact_mod" in result["content"]
        assert len(session.request_details) == 1
        assert "Authorization" not in session.request_details[0][2]["headers"]

    async def test_anonymous_moderator_roster_merges_every_public_page(self):
        first = _moderators_payload()
        first["data"]["after"] = "t2_next"
        second = {
            "kind": "UserList",
            "data": {
                "children": [
                    {
                        "name": "second_mod",
                        "mod_permissions": ["wiki"],
                        "date": 1_700_000_001,
                    }
                ],
                "after": None,
            },
        }
        session = _JsonSession(
            [_JsonResponse(first), _JsonResponse(second)]
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                _moderator_route(),
                max_tokens=1000,
                timeout=10,
            )

        assert "u/exact_mod" in result["content"]
        assert "u/second_mod" in result["content"]
        assert session.calls == [
            *map(_transport_url, _moderator_route().requests),
            _transport_url(
                "https://www.reddit.com/r/Python/about/moderators.json?"
                "limit=500&raw_json=1&after=t2_next"
            ),
        ]
        assert all(
            "Authorization" not in details[2]["headers"]
            for details in session.request_details
        )

    async def test_anonymous_moderator_pagination_rejects_bad_cursor(self):
        payload = _moderators_payload()
        payload["data"]["after"] = "unsafe/cursor"
        session = _JsonSession([_JsonResponse(payload)])

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                _moderator_route(),
                max_tokens=1000,
                timeout=10,
            )

        assert result == {
            "error": (
                "Reddit moderator roster returned an invalid pagination "
                "cursor."
            )
        }
        assert session.calls == [
            _transport_url(request)
            for request in _moderator_route().requests
        ]

    async def test_later_anonymous_moderator_403_can_cross_exact_oauth_boundary(
        self,
    ):
        first = _moderators_payload()
        first["data"]["after"] = "t2_next"
        session = _JsonSession(
            [
                _JsonResponse(first),
                _JsonResponse({}, status_code=403),
                _JsonResponse(_moderators_payload()),
            ]
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                _moderator_route(),
                max_tokens=1000,
                timeout=10,
                config=Config(reddit_access_token="direct-token"),
            )

        assert "u/exact_mod" in result["content"]
        assert len(session.calls) == 3
        assert "Authorization" not in session.request_details[0][2]["headers"]
        assert "Authorization" not in session.request_details[1][2]["headers"]
        assert (
            session.request_details[2][2]["headers"]["Authorization"]
            == "Bearer direct-token"
        )

    async def test_direct_token_is_sent_only_to_exact_oauth_moderator_route(self):
        session = _JsonSession(
            [
                _JsonResponse({}, status_code=403),
                _JsonResponse(_moderators_payload()),
            ]
        )
        config = Config(
            reddit_access_token="direct-token",
            reddit_user_agent="test-agent",
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                _moderator_route(),
                max_tokens=1000,
                timeout=10,
                config=config,
            )

        assert "u/exact_mod" in result["content"]
        assert "posts, wiki" in result["content"]
        anonymous, authenticated = session.request_details
        assert anonymous[1].startswith("https://api.reddit.com/")
        assert "Authorization" not in anonymous[2]["headers"]
        assert (
            authenticated[1]
            == "https://oauth.reddit.com/r/Python/about/moderators"
            "?limit=500&raw_json=1"
        )
        assert authenticated[2]["headers"] == {
            "Accept": "application/json",
            "Authorization": "Bearer direct-token",
            "User-Agent": "test-agent",
        }
        assert authenticated[2]["max_response_size"] == 2 * 1024 * 1024

    async def test_refresh_flow_uses_exact_form_basic_auth_and_reuses_token(self):
        session = _JsonSession(
            [
                _JsonResponse({"access_token": "refreshed-token", "expires_in": 3600}),
                _JsonResponse(_moderators_payload()),
                _JsonResponse(_moderators_payload()),
            ]
        )
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            user_agent="test-agent",
        )

        first = await manager.fetch_moderators("Python", session, None, 10)
        second = await manager.fetch_moderators("Python", session, None, 10)

        assert first == second == {"data": _moderators_payload()}
        assert [method for method, _url, _kwargs in session.request_details] == [
            "POST",
            "GET",
            "GET",
        ]
        token_request = session.request_details[0]
        expected_basic = base64.b64encode(b"client-id:client-secret").decode()
        assert token_request[1] == "https://www.reddit.com/api/v1/access_token"
        assert token_request[2]["headers"]["Authorization"] == f"Basic {expected_basic}"
        assert token_request[2]["form"] == {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-token",
        }
        assert token_request[2]["max_response_size"] == 64 * 1024
        assert all(
            details[2]["headers"]["Authorization"] == "Bearer refreshed-token"
            for details in session.request_details[1:]
        )
        timeouts = [
            details[2]["timeout"] for details in session.request_details
        ]
        assert all(0 < timeout <= 10 for timeout in timeouts)
        assert timeouts[1] <= timeouts[0]

    async def test_moderator_pagination_merges_every_exact_roster_page(self):
        first_page = _moderators_payload()
        first_page["data"]["after"] = "t2_next"
        second_page = {
            "kind": "UserList",
            "data": {
                "children": [
                    {
                        "name": "second_mod",
                        "mod_permissions": ["all"],
                    }
                ],
                "after": None,
            },
        }
        session = _JsonSession(
            [_JsonResponse(first_page), _JsonResponse(second_page)]
        )
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_moderators("Python", session, None, 10)

        assert [
            child["name"] for child in result["data"]["data"]["children"]
        ] == ["exact_mod", "second_mod"]
        assert result["data"]["data"]["after"] is None
        assert session.calls == [
            "https://oauth.reddit.com/r/Python/about/moderators"
            "?limit=500&raw_json=1",
            "https://oauth.reddit.com/r/Python/about/moderators"
            "?limit=500&raw_json=1&after=t2_next",
        ]

    async def test_moderator_pagination_rejects_repeated_cursor(self):
        page = _moderators_payload()
        page["data"]["after"] = "t2_repeat"
        session = _JsonSession(
            [_JsonResponse(page), _JsonResponse(page)]
        )
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_moderators("Python", session, None, 10)

        assert result == {
            "error": (
                "Reddit moderator roster returned an invalid pagination cursor."
            )
        }

    async def test_moderator_pagination_has_explicit_page_cap(self):
        first = _moderators_payload()
        first["data"]["after"] = "t2_first"
        second = _moderators_payload()
        second["data"]["after"] = "t2_second"
        session = _JsonSession(
            [_JsonResponse(first), _JsonResponse(second)]
        )
        manager = RedditModeratorOAuth(access_token="direct-token")

        with patch(
            "fetchaller.tools.reddit_auth._MAX_ROSTER_PAGES",
            2,
        ):
            result = await manager.fetch_moderators(
                "Python",
                session,
                None,
                10,
            )

        assert result == {
            "error": (
                "Reddit moderator roster exceeded the bounded pagination limit."
            )
        }

    async def test_short_lived_refresh_token_is_never_cached_past_expiry(self):
        session = _JsonSession(
            [
                _JsonResponse({"access_token": "first-token", "expires_in": 0.5}),
                _JsonResponse(_moderators_payload()),
                _JsonResponse({"access_token": "second-token", "expires_in": 0.5}),
                _JsonResponse(_moderators_payload()),
            ]
        )
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )

        with patch(
            "fetchaller.tools.reddit_auth.time.monotonic",
            return_value=100.0,
        ):
            first = await manager.fetch_moderators("Python", session, None, 10)
            assert manager._expires_at <= 100.5
        with patch(
            "fetchaller.tools.reddit_auth.time.monotonic",
            return_value=100.6,
        ):
            second = await manager.fetch_moderators("Python", session, None, 10)

        assert first == second == {"data": _moderators_payload()}
        assert [
            method for method, _url, _kwargs in session.request_details
        ] == ["POST", "GET", "POST", "GET"]

    async def test_refresh_lock_wait_is_bounded_by_each_callers_deadline(self):
        class BlockingSession:
            def __init__(self):
                self.refresh_started = asyncio.Event()
                self.release_refresh = asyncio.Event()

            async def post(self, *_args, **_kwargs):
                self.refresh_started.set()
                await self.release_refresh.wait()
                return _JsonResponse(
                    {"access_token": "shared-token", "expires_in": 3600}
                )

            async def get(self, *_args, **_kwargs):
                return _JsonResponse(_moderators_payload())

        session = BlockingSession()
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )
        first = asyncio.create_task(
            manager.fetch_moderators("Python", session, None, 10)
        )
        await session.refresh_started.wait()

        started = asyncio.get_running_loop().time()
        second = await manager.fetch_moderators("Python", session, None, 0.01)
        elapsed = asyncio.get_running_loop().time() - started
        session.release_refresh.set()
        first_result = await first

        assert second == {
            "error": "Reddit OAuth authentication timed out."
        }
        assert elapsed < 0.2
        assert first_result == {"data": _moderators_payload()}

    async def test_rejected_direct_token_refreshes_and_retries_exactly_once(self):
        session = _JsonSession(
            [
                _JsonResponse({}, status_code=401),
                _JsonResponse({"access_token": "replacement", "expires_in": 3600}),
                _JsonResponse(_moderators_payload()),
            ]
        )
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            access_token="expired-direct-token",
        )

        result = await manager.fetch_moderators("Python", session, None, 10)

        assert result == {"data": _moderators_payload()}
        assert [method for method, _url, _kwargs in session.request_details] == [
            "GET",
            "POST",
            "GET",
        ]
        assert (
            session.request_details[0][2]["headers"]["Authorization"]
            == "Bearer expired-direct-token"
        )
        assert (
            session.request_details[2][2]["headers"]["Authorization"]
            == "Bearer replacement"
        )

    async def test_concurrent_moderator_calls_deduplicate_refresh(self):
        class BlockingRefreshSession(_JsonSession):
            def __init__(self):
                super().__init__(
                    [
                        _JsonResponse(_moderators_payload()),
                        _JsonResponse(_moderators_payload()),
                    ]
                )
                self.refresh_started = asyncio.Event()
                self.release_refresh = asyncio.Event()
                self.post_calls = 0

            async def post(self, url: str, **kwargs):
                self.post_calls += 1
                self.calls.append(url)
                self.request_details.append(("POST", url, kwargs))
                self.refresh_started.set()
                await self.release_refresh.wait()
                return _JsonResponse(
                    {"access_token": "shared-token", "expires_in": 3600}
                )

        session = BlockingRefreshSession()
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )

        first = asyncio.create_task(
            manager.fetch_moderators("Python", session, None, 10)
        )
        await session.refresh_started.wait()
        second = asyncio.create_task(
            manager.fetch_moderators("Python", session, None, 10)
        )
        await asyncio.sleep(0)
        session.release_refresh.set()
        results = await asyncio.gather(first, second)

        assert results == [
            {"data": _moderators_payload()},
            {"data": _moderators_payload()},
        ]
        assert session.post_calls == 1

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            (_JsonResponse({}, status_code=400), "credentials were rejected"),
            (_JsonResponse({"access_token": "bad token", "expires_in": 3600}), "invalid response"),
            (_JsonResponse({"access_token": "token", "expires_in": 0}), "invalid response"),
            (_JsonResponse([], status_code=200), "invalid response"),
            (_JsonResponse({"error": "invalid_grant"}), "credentials were rejected"),
        ],
    )
    async def test_refresh_failures_are_bounded_and_sanitized(self, response, message):
        session = _JsonSession([response])
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )

        result = await manager.fetch_moderators("Python", session, None, 10)

        assert message in result["error"]
        assert "client-secret" not in result["error"]
        assert "refresh-token" not in result["error"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"error": "forbidden"},
            {"data": []},
            {"data": {"children": []}},
            {"kind": "Listing", "data": {"children": []}},
            {"data": {"children": "not-a-list"}},
            {"data": {"children": [None]}},
            {
                "kind": "UserList",
                "data": {
                    "children": [
                        {
                            "kind": "t2",
                            "data": {
                                "name": "wrapped_mod",
                                "mod_permissions": ["all"],
                            },
                        }
                    ]
                },
            },
            {
                "kind": "UserList",
                "data": {
                    "children": [
                        {
                            "name": "outer_mod",
                            "mod_permissions": ["posts"],
                            "data": {
                                "name": "nested_mod",
                                "mod_permissions": ["all"],
                            },
                        }
                    ]
                },
            },
            {"data": {"children": [{"name": "", "mod_permissions": []}]}},
            {"data": {"children": [{"name": "bad name", "mod_permissions": []}]}},
            {"data": {"children": [{"name": "mod", "mod_permissions": "all"}]}},
            {
                "data": {
                    "children": [
                        {
                            "name": "mod",
                            "mod_permissions": [],
                            "date": "not-a-timestamp",
                        }
                    ]
                }
            },
        ],
    )
    async def test_malformed_roster_can_never_be_rendered_as_an_empty_success(
        self,
        payload,
    ):
        session = _JsonSession([_JsonResponse(payload)])
        manager = RedditModeratorOAuth(access_token="direct-token")

        result = await manager.fetch_moderators("Python", session, None, 10)

        assert result == {
            "error": "Reddit moderator roster returned an invalid response."
        }

    def test_renderer_rejects_hybrid_moderator_child_without_dropping_it(self):
        rendered = render_reddit_route(
            _moderator_route(),
            [
                {
                    "kind": "UserList",
                    "data": {
                        "children": [
                            {
                                "name": "outer_mod",
                                "mod_permissions": ["posts"],
                                "data": {
                                    "name": "nested_mod",
                                    "mod_permissions": ["all"],
                                },
                            }
                        ]
                    },
                }
            ],
            max_tokens=1000,
        )

        assert "invalid response" in rendered
        assert "u/outer_mod" not in rendered
        assert "u/nested_mod" not in rendered

    async def test_transport_exception_cannot_leak_oauth_secrets(
        self,
        caplog,
        capsys,
    ):
        class ExplodingSession:
            async def post(self, *_args, **_kwargs):
                raise RuntimeError(
                    "client-secret refresh-token direct-token should never escape"
                )

        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            access_token=None,
        )

        result = await manager.fetch_moderators(
            "Python",
            ExplodingSession(),
            None,
            10,
        )
        captured = capsys.readouterr()
        combined = result["error"] + caplog.text + captured.out + captured.err + repr(manager)

        assert result == {"error": "Reddit OAuth authentication failed."}
        assert "client-secret" not in combined
        assert "refresh-token" not in combined
        assert "direct-token" not in combined

    async def test_oauth_queue_timeout_is_a_clean_bounded_error(self):
        queue = AsyncMock()
        queue.enqueue.side_effect = TimeoutError
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )

        result = await manager.fetch_moderators("Python", _JsonSession([]), queue, 3)

        assert result == {"error": "Reddit OAuth authentication timed out."}

    async def test_oauth_429_applies_shared_queue_backoff(self):
        session = _JsonSession(
            [_JsonResponse({}, status_code=429, headers={"retry-after": "17"})]
        )
        queue = Mock()

        async def enqueue(callback, *_args, **_kwargs):
            return await callback(*_args)

        queue.enqueue = AsyncMock(side_effect=enqueue)
        manager = RedditModeratorOAuth(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )

        result = await manager.fetch_moderators("Python", session, queue, 10)

        assert "Retry after 17s" in result["error"]
        queue.set_backoff.assert_called_once_with(429, retry_after=17.0)

    async def test_non_moderator_route_remains_anonymous_when_oauth_is_configured(self):
        session = _JsonSession([_JsonResponse({}, status_code=403)])
        config = Config(reddit_access_token="must-not-leak")
        route = RedditRoute(
            "https://www.reddit.com/r/Python/",
            "listing",
            ("https://www.reddit.com/r/Python/hot.json?raw_json=1",),
            subreddit="Python",
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.defer",
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
                config=config,
            )

        assert result == {"error": "Access forbidden by Reddit (HTTP 403)."}
        assert len(session.request_details) == 1
        assert "Authorization" not in session.request_details[0][2]["headers"]

    async def test_shared_session_upgrades_solver_and_seeds_over18_cookie(self):
        created = []

        class FakeSession:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cookies = []
                created.append(self)

            def add_cookie(self, value, url):
                self.cookies.append((value, url))

        solver = object()
        await close_session()
        with patch(
            "fetchaller.tools.browse_reddit.wafer.AsyncSession",
            side_effect=FakeSession,
        ):
            initial = await _get_session()
            upgraded = await _get_session(solver)
            reused = await _get_session()
        await close_session()

        assert initial is not upgraded
        assert upgraded is reused
        assert upgraded.kwargs["browser_solver"] is solver
        assert upgraded.kwargs["max_rotations"] == 2
        assert all(
            session.cookies
            == [
                (
                    "over18=1; Domain=.reddit.com; Path=/; Secure; SameSite=Lax",
                    "https://www.reddit.com/",
                )
            ]
            for session in created
        )

    async def test_reddit_session_audit_counts_hydration_and_network_bootstrap(
        self,
    ):
        class FakeSession:
            _cookie_scopes = {
                ("loid", "reddit.com", "/"): False,
                ("token_v2", "reddit.com", "/"): False,
                ("unrelated", "example.com", "/"): False,
            }

            async def _reddit_bootstrap_on_client(self, *_args, **_kwargs):
                return True

        session = FakeSession()
        _instrument_reddit_session(session)

        assert reddit_session_audit() == {
            "hydrated_cookie_count": 2,
            "hydrated_anonymous": 1,
            "bootstrap_instrumented": 1,
            "bootstrap_network_attempts": 0,
        }
        assert await session._reddit_bootstrap_on_client() is True
        assert reddit_session_audit()["bootstrap_network_attempts"] == 1
        await close_session()

    async def test_reddit_session_audit_reads_locked_wafer_cookie_jar(self):
        class Cookie:
            def __init__(self, name: str, domain: str):
                self.name = name
                self.domain = domain

        class Jar:
            def get_all(self):
                return [
                    Cookie("loid", "reddit.com"),
                    Cookie("csv", ".reddit.com"),
                    Cookie("unrelated", "reddit.com"),
                    Cookie("token_v2", "example.com"),
                ]

        class Client:
            cookie_jar = Jar()

        class LockedWaferSession:
            _client = Client()

            async def _reddit_bootstrap_on_client(self, *_args, **_kwargs):
                return True

        session = LockedWaferSession()
        _instrument_reddit_session(session)

        assert reddit_session_audit() == {
            "hydrated_cookie_count": 2,
            "hydrated_anonymous": 1,
            "bootstrap_instrumented": 1,
            "bootstrap_network_attempts": 0,
        }
        await close_session()

    async def test_reddit_session_audit_fails_closed_without_bootstrap_hook(
        self,
    ):
        class IncompatibleSession:
            _cookie_scopes = {
                ("loid", "reddit.com", "/"): False,
                ("token_v2", "reddit.com", "/"): False,
            }

        _instrument_reddit_session(IncompatibleSession())

        assert reddit_session_audit() == {
            "hydrated_cookie_count": 2,
            "hydrated_anonymous": 1,
            "bootstrap_instrumented": 0,
            "bootstrap_network_attempts": 0,
        }
        await close_session()

    def test_archived_collection_parser_requires_exact_redux_identity(self):
        collection_id = "36910c41-231f-45ea-8057-a4e061048541"
        model = {
            "id": collection_id,
            "title": "A real archived collection",
            "description": "Preserved metadata",
            "postIds": ["t3_post1", "t3_post2"],
            "primaryPostId": "t3_post1",
            "subredditId": "t5_python",
            "permalink": (
                "https://www.reddit.com/r/Python/collection/"
                f"{collection_id}"
            ),
        }
        html = (
            "<!doctype html><html><body><script>"
            "window.___r = "
            + json.dumps(
                {"postCollection": {"models": {collection_id: model}}}
            )
            + ";</script></body></html>"
        )

        parsed = _parse_archived_collection(
            html,
            subreddit="Python",
            collection_id=collection_id,
            timestamp="20230206225353",
        )

        assert parsed == {
            "title": "A real archived collection",
            "description": "Preserved metadata",
            "link_ids": ["t3_post1", "t3_post2"],
            "_fetchaller_reddit_provenance": "wayback",
            "_fetchaller_reddit_archive_timestamp": "20230206225353",
        }
        wrong_identity = json.loads(json.dumps(model))
        wrong_identity["permalink"] = (
            "https://www.reddit.com/r/Other/collection/" + collection_id
        )
        wrong_html = (
            "<script>window.___r = "
            + json.dumps(
                {
                    "postCollection": {
                        "models": {collection_id: wrong_identity}
                    }
                }
            )
            + ";</script>"
        )
        assert (
            _parse_archived_collection(
                wrong_html,
                subreddit="Python",
                collection_id=collection_id,
                timestamp="20230206225353",
            )
            is None
        )
        assert (
            _parse_archived_collection(
                "<h1>Welcome to Reddit</h1>",
                subreddit="Python",
                collection_id=collection_id,
                timestamp="20230206225353",
            )
            is None
        )
        assert (
            _parse_archived_collection(
                "<i>x</i>" * 500_000,
                subreddit="Python",
                collection_id=collection_id,
                timestamp="20230206225353",
            )
            is None
        )
        assert (
            _parse_archived_collection(
                html + html,
                subreddit="Python",
                collection_id=collection_id,
                timestamp="20230206225353",
            )
            is None
        )
        unhashable_ids = json.loads(json.dumps(model))
        unhashable_ids["postIds"] = [{"id": "post1"}]
        assert (
            _parse_archived_collection(
                "<script>window.___r = "
                + json.dumps(
                    {
                        "postCollection": {
                            "models": {
                                collection_id: unhashable_ids,
                            }
                        }
                    }
                )
                + ";</script>",
                subreddit="Python",
                collection_id=collection_id,
                timestamp="20230206225353",
            )
            is None
        )

    def test_collection_cdx_parser_rejects_wrong_hosts_and_post_removal_dates(
        self,
    ):
        collection_id = "36910c41-231f-45ea-8057-a4e061048541"
        original = (
            "https://www.reddit.com/r/Python/collection/" + collection_id
        )
        payload = [
            ["timestamp", "original", "statuscode", "mimetype"],
            ["20230206225353", original, "200", "text/html"],
            [
                "20230206225354",
                "https://evil.example/r/Python/collection/" + collection_id,
                "200",
                "text/html",
            ],
            ["20250101000000", original, "200", "text/html"],
            ["20230206225355", original, "302", "text/html"],
        ]

        assert _parse_collection_cdx(
            payload,
            subreddit="Python",
            collection_id=collection_id,
        ) == [("20230206225353", original)]
        assert (
            _parse_collection_cdx(
                [
                    [
                        "timestamp",
                        "original",
                        "statuscode",
                        "mimetype",
                        "unexpected",
                    ],
                    [
                        "20230206225353",
                        original,
                        "200",
                        "text/html",
                        "value",
                    ],
                ],
                subreddit="Python",
                collection_id=collection_id,
            )
            == []
        )

    def test_collection_transport_allowlist_is_exact(self):
        endpoint = (
            "https://www.reddit.com/api/v1/collections/collection?"
            "collection_id=36910c41-231f-45ea-8057-a4e061048541"
            "&include_links=true&raw_json=1"
        )

        assert _validated_reddit_json_url(endpoint) is not None
        assert _validated_reddit_json_url(endpoint + "&extra=true") is None
        assert (
            _validated_reddit_json_url(
                endpoint.replace("include_links=true", "include_links=false")
            )
            is None
        )
        assert (
            _validated_reddit_json_url(
                endpoint.replace(
                    "36910c41-231f-45ea-8057-a4e061048541",
                    "not-a-uuid",
                )
            )
            is None
        )

    def test_json_transport_uses_api_origin_without_expanding_public_input(self):
        canonical = (
            "https://www.reddit.com/r/Python/hot.json?"
            "limit=25&raw_json=1"
        )
        transport = (
            "https://api.reddit.com/r/Python/hot.json?"
            "limit=25&raw_json=1"
        )

        assert _validated_reddit_json_url(canonical) is not None
        assert _reddit_json_transport_url(canonical) == transport
        assert _validated_reddit_json_url(transport) is None
        assert _validated_reddit_json_url(transport, transport=True) is not None

    async def test_removed_collection_recovers_exact_archive_and_current_posts(
        self,
    ):
        collection_id = "36910c41-231f-45ea-8057-a4e061048541"
        original = (
            "https://www.reddit.com/r/YUROP/collection/" + collection_id
        )
        model = {
            "id": collection_id,
            "title": "Vendredo sen la angla lingvo",
            "description": "Archived collection metadata",
            "postIds": ["t3_post1", "t3_post2"],
            "primaryPostId": "t3_post1",
            "subredditId": "t5_2wivw",
            "permalink": original,
        }
        archive_html = (
            "<!doctype html><html><body><script>"
            "window.___r = "
            + json.dumps(
                {"postCollection": {"models": {collection_id: model}}}
            )
            + ";</script></body></html>"
        )
        current_posts = {
            "kind": "Listing",
            "data": {
                "children": [
                    _post(id="post1", title="Current post one"),
                    _post(id="post2", title="Current post two"),
                ]
            },
        }
        session = _JsonSession(
            [
                _JsonResponse({}, status_code=500),
                _JsonResponse(
                    [
                        [
                            "timestamp",
                            "original",
                            "statuscode",
                            "mimetype",
                        ],
                        [
                            "20230206225353",
                            original,
                            "200",
                            "text/html",
                        ],
                    ]
                ),
                _HtmlResponse(archive_html),
                _JsonResponse(current_posts),
            ]
        )
        route = route_reddit_url(original + "/")
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=10_000,
                timeout=10,
            )

        assert result["content_type"] == "markdown"
        assert "# Vendredo sen la angla lingvo" in result["content"]
        assert (
            "Metadata source: archived New Reddit snapshot "
            "(Wayback, 2023-02-06)."
        ) in result["content"]
        assert "Post details: current Reddit API." in result["content"]
        assert "Current post one" in result["content"]
        assert "Current post two" in result["content"]
        assert "2 items returned" in result["content"]
        assert len(session.calls) == 4
        assert urlparse(session.calls[1]).hostname == "web.archive.org"
        assert session.calls[2] == (
            "https://web.archive.org/web/20230206225353id_/" + original
        )
        assert parse_qs(urlparse(session.calls[3]).query)["id"] == [
            "t3_post1,t3_post2"
        ]

    @pytest.mark.parametrize(
        "children",
        [
            [_post(id="post1", title="Current post one")],
            [
                _post(id="post1", title="Current post one"),
                _post(id="other", title="Substituted post"),
            ],
            [
                _post(id="post1", title="Current post one"),
                _post(id="post1", title="Duplicate post"),
            ],
        ],
    )
    async def test_collection_rejects_incomplete_or_substituted_hydration(
        self,
        children,
    ):
        session = _JsonSession(
            [
                _JsonResponse(
                    {
                        "title": "Collection",
                        "description": "",
                        "link_ids": ["t3_post1", "t3_post2"],
                    }
                ),
                _JsonResponse(
                    {
                        "kind": "Listing",
                        "data": {"children": children},
                    }
                ),
            ]
        )
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/collection/"
            "33333333-3333-4333-8333-333333333333/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=10_000,
                timeout=10,
            )

        assert result["error"].startswith(
            "Reddit collection is unavailable: current post hydration "
        )
        assert "content" not in result

    async def test_collection_batches_every_valid_id_and_fails_later_error(self):
        ids = [f"t3_p{index}" for index in range(205)]
        first_batch = {
            "data": {
                "children": [
                    _post(id=f"p{index}", title=f"Post {index}")
                    for index in range(100)
                ]
            }
        }
        third_batch = {
            "data": {
                "children": [
                    _post(id=f"p{index}", title=f"Post {index}")
                    for index in range(200, 205)
                ]
            }
        }
        session = _JsonSession(
            [
                _JsonResponse(
                    {
                        "title": "Large collection",
                        "link_ids": ids,
                    }
                ),
                _JsonResponse(first_batch),
                _JsonResponse({}, status_code=500),
                _JsonResponse(third_batch),
            ]
        )
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/collection/"
            "11111111-1111-4111-8111-111111111111/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=250_000,
                timeout=10,
            )

        batch_sizes = [
            len(parse_qs(urlparse(url).query)["id"][0].split(","))
            for url in session.calls[1:]
        ]
        assert batch_sizes == [100, 100]
        assert result["error"].startswith(
            "Reddit collection is unavailable: current post hydration failed "
        )
        assert "content" not in result

    @pytest.mark.parametrize(
        "response",
        [
            _JsonResponse({}, status_code=500),
            _NonJsonResponse(None),
            _JsonResponse({}),
        ],
    )
    async def test_collection_metadata_failure_is_explicit_not_an_empty_collection(
        self,
        response,
    ):
        session = _JsonSession([response])
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/collection/"
            "22222222-2222-4222-8222-222222222222/"
        )
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert result["error"].startswith(
            "Reddit collection is unavailable: "
        )
        assert len(result["error"]) < 400
        assert "content" not in result
        assert "0 items returned" not in result["error"]
        assert session.calls[0] == _transport_url(route.requests[0])
        assert len(session.calls) == 2
        archive_query = urlparse(session.calls[1])
        assert archive_query.scheme == "https"
        assert archive_query.netloc == "web.archive.org"
        assert archive_query.path == "/cdx/search/cdx"
        assert parse_qs(archive_query.query)["url"] == [
            "www.reddit.com/r/Python/collection/"
            "22222222-2222-4222-8222-222222222222"
        ]

    async def test_all_independent_profile_legs_failing_is_a_tool_error(self):
        session = _JsonSession(
            [_JsonResponse({}, status_code=500) for _ in range(5)]
        )
        route = route_reddit_url("https://www.reddit.com/user/alice/")
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=5000,
                timeout=10,
            )

        assert result["error"].startswith("All Reddit data sources were unavailable")

    async def test_queue_timeout_becomes_a_clean_tool_error(self):
        queue = AsyncMock()
        queue.enqueue.side_effect = TimeoutError

        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json?limit=1",
            _JsonSession([]),
            queue,
            timeout=3,
        )

        assert result == {"error": "Request timed out (3s limit)"}

    async def test_direct_mapped_fetch_uses_process_wide_limiter_for_every_leg(self):
        profile = {
            "kind": "t2",
            "data": {"name": "spez", "link_karma": 1, "comment_karma": 2},
        }
        overview = {"kind": "Listing", "data": {"children": []}}
        session = _JsonSession([_JsonResponse(profile), _JsonResponse(overview)])
        route = RedditRoute(
            "https://www.reddit.com/user/spez/",
            "user_profile",
            (
                "https://www.reddit.com/user/spez/about.json",
                "https://www.reddit.com/user/spez/overview.json?limit=5",
            ),
            username="spez",
        )

        with (
            patch("fetchaller.tools.reddit_fetch._get_session", AsyncMock(return_value=session)),
            patch("fetchaller.tools.browse_reddit.reddit_limiter.wait", AsyncMock()) as wait,
        ):
            result = await fetch_mapped_reddit(route, max_tokens=1000, timeout=10)

        assert result["content_type"] == "markdown"
        assert "# u/spez" in result["content"]
        assert wait.await_count == 2

    async def test_browse_and_search_keep_current_tools_and_new_links(self):
        browse_session = _JsonSession([_JsonResponse({"data": {"children": [_post()], "after": "t3_next"}})])
        search_session = _JsonSession([_JsonResponse({"data": {"children": [_post()], "after": None}})])

        with patch("fetchaller.tools.browse_reddit._get_session", AsyncMock(return_value=browse_session)):
            browsed = await browse_reddit("Python", limit=1)
        with patch("fetchaller.tools.search_reddit._get_session", AsyncMock(return_value=search_session)):
            searched = await search_reddit("asyncio", limit=1)

        assert "A compact Reddit post" in browsed["content"]
        assert "[Next page: after=t3_next]" in browsed["content"]
        assert "A compact Reddit post" in searched["content"]
        assert "r/Python" in searched["content"]
        assert "https://www.reddit.com/" in browsed["content"] + searched["content"]
        assert "old.reddit.com" not in browsed["content"] + searched["content"]

    async def test_browse_and_search_reject_unreachable_input_cursors(self):
        browse_session = _JsonSession([])
        search_session = _JsonSession([])

        with patch(
            "fetchaller.tools.browse_reddit._get_session",
            AsyncMock(return_value=browse_session),
        ):
            browsed = await browse_reddit(
                "Python",
                after="unsafe/cursor",
            )
        with patch(
            "fetchaller.tools.search_reddit._get_session",
            AsyncMock(return_value=search_session),
        ):
            searched = await search_reddit(
                "asyncio",
                after="unsafe/cursor",
            )
            overlong = await search_reddit("x" * 513)

        assert browsed == {"error": "Invalid Reddit pagination cursor"}
        assert searched == {"error": "Invalid Reddit pagination cursor"}
        assert overlong == {"error": "Query must be 512 characters or fewer"}
        assert not browse_session.calls
        assert not search_session.calls

    async def test_browse_and_search_name_invalid_returned_cursors(self):
        payload = {
            "data": {
                "children": [_post()],
                "after": "unsafe/cursor",
            }
        }
        browse_session = _JsonSession([_JsonResponse(payload)])
        search_session = _JsonSession([_JsonResponse(payload)])

        with (
            patch(
                "fetchaller.tools.browse_reddit._get_session",
                AsyncMock(return_value=browse_session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            browsed = await browse_reddit("Python", limit=1)
            with patch(
                "fetchaller.tools.search_reddit._get_session",
                AsyncMock(return_value=search_session),
            ):
                searched = await search_reddit("asyncio", limit=1)

        marker = (
            "Next page unavailable: Reddit returned an invalid pagination "
            "cursor"
        )
        assert marker in browsed["content"]
        assert marker in searched["content"]
        assert "after=unsafe" not in browsed["content"] + searched["content"]

    async def test_direct_browse_and_search_share_process_limiter(self):
        browse_session = _JsonSession([_JsonResponse({"data": {"children": [], "after": None}})])
        search_session = _JsonSession([_JsonResponse({"data": {"children": [], "after": None}})])

        with (
            patch("fetchaller.tools.browse_reddit._get_session", AsyncMock(return_value=browse_session)),
            patch("fetchaller.tools.browse_reddit.reddit_limiter.wait", AsyncMock()) as wait,
        ):
            await browse_reddit("Python", limit=1)
            with patch(
                "fetchaller.tools.search_reddit._get_session",
                AsyncMock(return_value=search_session),
            ):
                await search_reddit("asyncio", limit=1)

        assert wait.await_count == 2

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            [],
            {"data": []},
            {"data": {"children": [None]}},
            {"data": {"children": [{"kind": "t3", "data": []}]}},
            {"data": {"children": [{"kind": "t3", "data": {}}]}},
            {"data": {"children": [{"kind": "t1", "data": _post()["data"]}]}},
            {
                "data": {
                    "children": [
                        _post(gallery_data={"items": [{"media_id": []}]})
                    ]
                }
            },
        ],
    )
    async def test_direct_tools_never_render_malformed_json_as_empty(
        self,
        payload,
    ):
        browse_session = _JsonSession([_JsonResponse(payload)])
        search_session = _JsonSession([_JsonResponse(payload)])

        with (
            patch(
                "fetchaller.tools.browse_reddit._get_session",
                AsyncMock(return_value=browse_session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            browsed = await browse_reddit("Python", limit=1)
            with patch(
                "fetchaller.tools.search_reddit._get_session",
                AsyncMock(return_value=search_session),
            ):
                searched = await search_reddit("asyncio", limit=1)

        assert browsed == {
            "error": "Reddit returned an invalid listing response"
        }
        assert searched == {
            "error": "Reddit returned an invalid search response"
        }

    @pytest.mark.parametrize(
        ("route", "index", "payload"),
        [
            (
                RedditRoute("https://www.reddit.com/hot/", "listing"),
                0,
                {"data": {"children": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/comments/post1/", "thread"),
                0,
                _thread(_post(), []),
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/duplicates/post1/",
                    "duplicates",
                ),
                0,
                _thread(_post(), []),
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/", "user_profile"),
                0,
                {"data": {"name": "alice"}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/", "user_profile"),
                1,
                {"data": {"children": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/", "user_profile"),
                2,
                {"data": {"trophies": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/", "user_profile"),
                3,
                [],
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/", "user_profile"),
                4,
                {"data": {"children": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/about/", "user_about"),
                0,
                {"data": {"name": "alice"}},
            ),
            (
                RedditRoute("https://www.reddit.com/r/Python/about/", "subreddit_about"),
                0,
                {"data": {"display_name": "Python"}},
            ),
            (
                RedditRoute("https://www.reddit.com/r/Python/about/rules/", "rules"),
                0,
                {"rules": [], "site_rules": []},
            ),
            (
                RedditRoute("https://www.reddit.com/r/Python/wiki/index/", "wiki"),
                0,
                {"data": {"content_md": ""}},
            ),
            (
                RedditRoute("https://www.reddit.com/r/Python/wiki/index/", "wiki_diff"),
                1,
                {"data": {"content_md": ""}},
            ),
            (
                RedditRoute("https://www.reddit.com/r/Python/wiki/pages/", "wiki_pages"),
                0,
                {"kind": "wikipagelisting", "data": ["index"]},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/r/Python/wiki/revisions/",
                    "wiki_revisions",
                ),
                0,
                {
                    "data": {
                        "children": [
                            {
                                "id": (
                                    "0136a1c0-57c7-11f1-b49a-"
                                    "ae675b7b52c3"
                                ),
                                "page": "index",
                                "timestamp": 1_700_000_000,
                                "author": {
                                    "kind": "t2",
                                    "data": {"name": "editor"},
                                },
                            }
                        ]
                    }
                },
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/r/Python/wiki/discussions/index/",
                    "wiki_discussions",
                ),
                0,
                {"data": {"children": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/trophies/", "trophies"),
                0,
                {"data": {"trophies": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/m/code/about/", "multi_about"),
                0,
                {"data": {"display_name": "code", "subreddits": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/m/code/", "multi_profile"),
                0,
                {"data": {"display_name": "code", "subreddits": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/user/alice/m/code/", "multi_profile"),
                1,
                {"data": {"children": []}},
            ),
            (
                RedditRoute("https://www.reddit.com/related/post1/", "related"),
                0,
                {"data": {"children": [_post()]}},
            ),
            (
                RedditRoute("https://www.reddit.com/live/abc123/", "live"),
                0,
                {"data": {"title": "Live thread"}},
            ),
            (
                RedditRoute("https://www.reddit.com/live/abc123/", "live"),
                1,
                {"data": {"children": []}},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/live/abc123/about/",
                    "live_about",
                ),
                0,
                {"data": {"title": "Live thread"}},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/live/abc123/contributors/",
                    "live_contributors",
                ),
                0,
                {"data": {"children": [{"name": "reporter"}]}},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/api/morechildren",
                    "morechildren",
                ),
                0,
                {"jquery": [], "success": True},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/r/Python/collection/"
                    "44444444-4444-4444-8444-444444444444/",
                    "collection",
                ),
                0,
                {
                    "title": "Collection",
                    "description": "",
                    "link_ids": ["t3_post1"],
                },
            ),
        ],
    )
    def test_every_mapped_schema_accepts_its_valid_empty_shape(
        self,
        route,
        index,
        payload,
    ):
        assert _payload_schema_error(route, index, payload) is None

    def test_removed_post_with_false_poll_data_keeps_the_whole_listing(self):
        """``"poll_data": false`` must not discard a user's activity.

        Reddit sends ``false`` rather than ``null`` for structured fields on
        moderator-removed posts. Rejecting that failed the post, and because
        every child must validate, two removed posts among a hundred wiped out
        u/bh-alienux's entire Recent activity section.
        """

        route = RedditRoute(
            "https://www.reddit.com/user/bh-alienux/",
            "user_profile",
            username="bh-alienux",
        )
        # Exactly the shape Reddit returned for this post: false poll_data,
        # null media. Only the false-for-absent case is being relaxed.
        removed = _post(
            id="1v3in15",
            name="t3_1v3in15",
            title="[ Removed by moderator ]",
            poll_data=False,
            media=None,
            secure_media=None,
        )
        listing = {
            "kind": "Listing",
            "data": {"children": [_post(), removed, _post(id="ok2", name="t3_ok2")]},
        }

        assert _payload_schema_error(route, 1, listing) is None

    @pytest.mark.parametrize("value", [None, False, {}])
    def test_absent_structured_post_fields_are_accepted(self, value):
        route = RedditRoute("https://www.reddit.com/hot/", "listing")
        listing = {
            "kind": "Listing",
            "data": {"children": [_post(poll_data=value, gallery_data=value)]},
        }

        assert _payload_schema_error(route, 0, listing) is None

    @pytest.mark.parametrize("value", ["poll", 5, []])
    def test_malformed_structured_post_fields_are_still_rejected(self, value):
        route = RedditRoute("https://www.reddit.com/hot/", "listing")
        listing = {
            "kind": "Listing",
            "data": {"children": [_post(poll_data=value)]},
        }

        assert _payload_schema_error(route, 0, listing) is not None

    def test_empty_moderated_subreddits_is_not_malformed(self):
        """Reddit answers ``{}`` for an account that moderates nothing.

        Confirmed live on both bh-alienux and AutoModerator, while an actual
        moderator gets a ModeratedList. An empty result is not an invalid one.
        """

        route = RedditRoute(
            "https://www.reddit.com/user/bh-alienux/",
            "user_profile",
            username="bh-alienux",
        )

        assert _payload_schema_error(route, 4, {}) is None
        assert _payload_schema_error(route, 4, {"kind": "ModeratedList", "data": []}) is None
        assert _payload_schema_error(route, 4, {"unexpected": "shape"}) is not None

    @pytest.mark.parametrize(
        ("route", "payload"),
        [
            (
                RedditRoute("https://www.reddit.com/hot/", "listing"),
                {"data": {"children": [{"kind": "t3", "data": {}}]}},
            ),
            (
                RedditRoute("https://www.reddit.com/hot/", "listing"),
                {
                    "data": {
                        "children": [
                            _post(
                                gallery_data={
                                    "items": [{"media_id": []}]
                                }
                            )
                        ]
                    }
                },
            ),
            (
                RedditRoute("https://www.reddit.com/comments/post1/", "thread"),
                [
                    {"data": {"children": []}},
                    {"data": {"children": []}},
                ],
            ),
            (
                RedditRoute("https://www.reddit.com/comments/post1/", "thread"),
                _thread(
                    _post(),
                    [
                        _comment(
                            "comment1",
                            "root",
                            replies=[
                                {
                                    "kind": "t1",
                                    "data": {},
                                }
                            ],
                        )
                    ],
                ),
            ),
            (
                RedditRoute("https://www.reddit.com/search/", "search"),
                {"data": {"children": [{"kind": "mystery", "data": {}}]}},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/user/alice/trophies/",
                    "trophies",
                ),
                {"data": {"trophies": [{"data": {}}]}},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/user/alice/m/code/about/",
                    "multi_about",
                ),
                {
                    "data": {
                        "display_name": "code",
                        "subreddits": [{}],
                    }
                },
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/r/Python/about/rules/",
                    "rules",
                ),
                {"rules": [{}], "site_rules": []},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/r/Python/wiki/pages/",
                    "wiki_pages",
                ),
                {
                    "kind": "wikipagelisting",
                    "data": ["../private"],
                },
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/live/abc123/contributors/",
                    "live_contributors",
                ),
                {"data": {"children": [{}]}},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/api/morechildren",
                    "morechildren",
                ),
                {
                    "json": {
                        "data": {
                            "things": [{"kind": "t1", "data": {}}]
                        }
                    }
                },
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/r/Python/collection/id/",
                    "collection",
                ),
                {"link_ids": ["t1_not_a_post"]},
            ),
            (
                RedditRoute(
                    "https://www.reddit.com/related/post1/",
                    "related",
                ),
                {"data": {"children": []}},
            ),
        ],
    )
    def test_mapped_schemas_reject_semantically_malformed_2xx(
        self,
        route,
        payload,
    ):
        error = _payload_schema_error(route, 0, payload)

        assert error is not None
        assert "invalid" in error

    @pytest.mark.parametrize(
        ("kind", "index"),
        [
            ("listing", 0),
            ("thread", 0),
            ("duplicates", 0),
            ("user_profile", 0),
            ("user_profile", 1),
            ("user_profile", 2),
            ("user_profile", 3),
            # index 4 (moderated communities) is deliberately absent: Reddit
            # answers a bare `{}` for an account that moderates nothing, so an
            # empty object is a real result there rather than an unstructured
            # 2xx. See test_empty_moderated_subreddits_is_not_malformed.
            ("user_about", 0),
            ("subreddit_about", 0),
            ("rules", 0),
            ("wiki", 0),
            ("wiki_diff", 0),
            ("wiki_pages", 0),
            ("wiki_discussions", 0),
            ("trophies", 0),
            ("multi_about", 0),
            ("multi_profile", 0),
            ("multi_profile", 1),
            ("related", 0),
            ("live", 0),
            ("live", 1),
            ("live_about", 0),
            ("live_contributors", 0),
            ("live_update", 0),
            ("morechildren", 0),
            ("collection", 0),
        ],
    )
    def test_every_mapped_schema_rejects_an_unstructured_2xx(
        self,
        kind,
        index,
    ):
        route = RedditRoute("https://www.reddit.com/example/", kind)

        error = _payload_schema_error(route, index, {})

        assert error is not None
        assert "invalid" in error

    async def test_mapped_invalid_listing_is_an_error_not_zero_items(self):
        session = _JsonSession([_JsonResponse({})])
        route = RedditRoute(
            "https://www.reddit.com/hot/",
            "listing",
            ("https://www.reddit.com/hot.json",),
        )

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert result == {
            "error": "Reddit returned an invalid listing response."
        }

    async def test_multi_source_profile_names_only_the_malformed_leg(self):
        session = _JsonSession(
            [
                _JsonResponse({}),
                _JsonResponse({"data": {"children": []}}),
                _JsonResponse({"data": {"trophies": []}}),
                _JsonResponse([]),
                _JsonResponse({"data": {"children": []}}),
            ]
        )
        route = route_reddit_url("https://www.reddit.com/user/alice/")
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert "error" not in result
        assert "Profile details unavailable" in result["content"]
        assert "invalid user profile details response" in result["content"]
        assert "Recent activity" in result["content"]

    async def test_user_profile_accepts_real_moderated_list_shape(self):
        session = _JsonSession(
            [
                _JsonResponse({"data": {"name": "spez"}}),
                _JsonResponse({"data": {"children": []}}),
                _JsonResponse({"data": {"trophies": []}}),
                _JsonResponse([]),
                _JsonResponse(
                    {
                        "kind": "ModeratedList",
                        "data": [
                            {
                                "display_name": "announcements",
                                "name": "t5_2r0ij",
                            },
                            {
                                "display_name_prefixed": "r/reddit",
                                "name": "t5_2qh1i",
                            },
                        ],
                    }
                ),
            ]
        )
        route = route_reddit_url("https://www.reddit.com/user/spez/")
        assert route is not None

        with (
            patch(
                "fetchaller.tools.reddit_fetch._get_session",
                AsyncMock(return_value=session),
            ),
            patch(
                "fetchaller.tools.browse_reddit.reddit_limiter.wait",
                AsyncMock(),
            ),
        ):
            result = await fetch_mapped_reddit(
                route,
                max_tokens=1000,
                timeout=10,
            )

        assert "error" not in result
        assert "## Moderated communities" in result["content"]
        assert "**r/announcements**" in result["content"]
        assert "**r/reddit**" in result["content"]
        assert "[Unavailable:" not in result["content"]


class TestLegacyNavigationRendering:
    def test_user_activity_preserves_user_sort_and_time_scope(self):
        route = route_reddit_url(
            "https://www.reddit.com/user/spez/comments/"
            "?sort=top&t=week"
        )
        assert route is not None
        rendered = render_reddit_route(
            route,
            [{"kind": "Listing", "data": {"children": [_comment(
                "comment1",
                "Activity body",
                parent_id="t3_post1",
                permalink=(
                    "/r/Python/comments/post1/title/comment1/"
                ),
            )]}}],
            max_tokens=5_000,
        )

        assert rendered.startswith(
            "# u/spez · comments · sort top · time week"
        )
        assert "Permalink: https://www.reddit.com/r/Python/comments/" in rendered
        assert "Parent context: https://www.reddit.com/comments/post1/" in rendered

    def test_search_preserves_query_sort_time_and_type_scope(self):
        route = route_reddit_url(
            "https://www.reddit.com/r/Python/search/"
            "?q=asyncio&sort=new&t=week&type=link%2Csr%2Cuser"
        )
        assert route is not None
        rendered = render_reddit_route(
            route,
            [{"kind": "Listing", "data": {"children": [_post()]}}],
            max_tokens=5_000,
        )

        assert rendered.startswith(
            "# r/Python · asyncio · sort new · time week · "
            "type link,sr,user"
        )

    def test_live_subroutes_preserve_root_and_exact_requested_urls(self):
        about_route = route_reddit_url(
            "https://www.reddit.com/live/abc123/about/"
        )
        contributors_route = route_reddit_url(
            "https://www.reddit.com/live/abc123/contributors/"
        )
        update_route = route_reddit_url(
            "https://www.reddit.com/live/abc123/updates/update1/"
        )
        assert about_route is not None
        assert contributors_route is not None
        assert update_route is not None

        about = render_reddit_route(
            about_route,
            [{"data": {
                "title": "Live title",
                "state": "live",
                "description": "Live description",
            }}],
            max_tokens=5_000,
        )
        contributors = render_reddit_route(
            contributors_route,
            [{"data": {"children": [{"name": "reporter"}]}}],
            max_tokens=5_000,
        )
        update = render_reddit_route(
            update_route,
            [{"data": {"children": [{
                "kind": "LiveUpdate",
                "data": {
                    "id": "update1",
                    "author": "reporter",
                    "created_utc": 1_700_000_000,
                    "body": "Update body",
                },
            }]}}],
            max_tokens=5_000,
        )

        for rendered in (about, contributors, update):
            assert "**Live thread:** https://www.reddit.com/live/abc123/" in rendered
        assert (
            "**Requested route:** "
            "https://www.reddit.com/live/abc123/about/"
        ) in about
        assert (
            "**Requested route:** "
            "https://www.reddit.com/live/abc123/contributors/"
        ) in contributors
        assert (
            "**Requested route:** "
            "https://www.reddit.com/live/abc123/updates/update1/"
        ) in update


class TestSuspendedFlagRequiresARealBoolean:
    """``is_suspended`` is never type-validated, so only ``True`` may act on it.

    Nothing between Reddit's JSON and these two call sites constrains the
    field's type. Both used a plain truthiness test, so any truthy non-boolean
    -- Reddit's own string ``"false"`` being the obvious one -- would both
    label a live account suspended and, on the fetch side, skip every remaining
    profile leg and answer with fabricated ``_fetch_error`` payloads.
    """

    @pytest.mark.parametrize("value", ["false", "0", "no", 1, [1], {"a": 1}])
    def test_truthy_non_boolean_never_labels_an_account_suspended(self, value):
        from fetchaller.content.reddit import _render_user_about

        rendered = _render_user_about({"data": {"name": "alice", "is_suspended": value}})
        assert "# u/alice" in rendered
        assert "Suspended account" not in rendered

    def test_real_suspension_is_still_reported(self):
        from fetchaller.content.reddit import _render_user_about

        rendered = _render_user_about({"data": {"name": "banned", "is_suspended": True}})
        assert "**Suspended account**" in rendered

    @pytest.mark.parametrize("value", ["false", "0", 1, [1], {"a": 1}])
    def test_fetch_shortcut_only_fires_on_a_real_boolean(self, value):
        """Mirrors the guard in reddit_fetch's leg loop."""

        payload = {"data": {"name": "alice", "is_suspended": value}}
        assert ((payload.get("data") or {}).get("is_suspended") is True) is False

    def test_fetch_shortcut_fires_for_true(self):
        payload = {"data": {"name": "banned", "is_suspended": True}}
        assert ((payload.get("data") or {}).get("is_suspended") is True) is True


class TestDuplicatesRendersEachPostOnce:
    """Reddit's duplicates listing repeats a post; the render must not.

    Observed live across three separate parity runs hours apart: the same
    crosspost (``t3_1v7xe5l``) arrived twice in one response with different
    cached upvote ratios, consistent with a shard merge. "Other discussions"
    is a set of distinct posts, so rendering it twice shows the reader a
    duplicate and inflates the count.

    The count also used to come from the unfiltered children, so a non-``t3``
    entry inflated it past the cards actually rendered.
    """

    @staticmethod
    def _payload(children):
        source = {"kind": "Listing", "data": {"children": [{
            "kind": "t3",
            "data": {
                "name": "t3_src", "id": "src", "title": "Source post",
                "subreddit": "worldnews", "author": "someone", "score": 10,
                "num_comments": 5, "permalink": "/r/worldnews/comments/src/x/",
                "created_utc": 1700000000.0,
            },
        }]}}
        return [source, {"kind": "Listing", "data": {"children": children}}]

    @staticmethod
    def _child(name, ratio):
        return {"kind": "t3", "data": {
            "name": f"t3_{name}", "id": name, "title": "Same headline",
            "subreddit": "EndlessWar", "author": "drobizg81", "score": 0,
            "upvote_ratio": ratio, "num_comments": 3,
            "permalink": f"/r/EndlessWar/comments/{name}/x/",
            "created_utc": 1700000000.0,
        }}

    def _render(self, payload):
        from fetchaller.content.reddit import _render_duplicates, route_reddit_url

        route = route_reddit_url(
            "https://www.reddit.com/r/worldnews/duplicates/src/x/?limit=5"
        )
        return _render_duplicates(payload, route, 100_000)

    def test_repeated_post_id_is_rendered_once_and_counted_once(self):
        payload = self._payload([
            self._child("aaa", 0.41),
            self._child("aaa", 0.29),   # same id, different cached ratio
            self._child("bbb", 1.0),
        ])
        rendered = self._render(payload)
        assert "2 items returned" in rendered
        assert rendered.count("/r/EndlessWar/comments/aaa/") == 1

    def test_non_t3_children_do_not_inflate_the_count(self):
        payload = self._payload([
            self._child("aaa", 0.41),
            {"kind": "t1", "data": {"name": "t1_zzz", "body": "a comment"}},
        ])
        assert "1 items returned" in self._render(payload)

    def test_distinct_posts_are_all_kept(self):
        payload = self._payload([
            self._child("aaa", 0.4), self._child("bbb", 0.5), self._child("ccc", 0.6),
        ])
        rendered = self._render(payload)
        assert "3 items returned" in rendered
        for slug in ("aaa", "bbb", "ccc"):
            assert f"/r/EndlessWar/comments/{slug}/" in rendered


class TestStateFlagsRequireRealBooleans:
    """Reddit state flags become factual claims, so only ``true`` may set them.

    Nothing between Reddit's JSON and these renderers constrains the type of
    ``over_18``/``locked``/``archived``/``stickied``/``hide_score`` -- and
    Reddit has already been observed sending ``false`` where a mapping was
    declared (``poll_data``). Under a plain truthiness test the string
    ``"false"`` labelled an ordinary post ``[NSFW, Locked, Archived, Stickied]``
    and, via ``hide_score``, replaced a real score with "score hidden".
    """

    BASE = {
        "name": "t3_a", "id": "a", "title": "T", "subreddit": "x",
        "author": "u", "score": 5, "num_comments": 1,
        "permalink": "/r/x/comments/a/t/", "created_utc": 1700000000.0,
    }

    @pytest.mark.parametrize("bogus", ["false", "0", "no", 1, [1], {"k": 1}])
    def test_truthy_non_boolean_never_labels_a_post(self, bogus):
        from fetchaller.content.reddit import format_reddit_post

        data = {
            **self.BASE,
            "over_18": bogus, "locked": bogus,
            "archived": bogus, "stickied": bogus, "spoiler": bogus,
        }
        rendered = format_reddit_post(data, 1)
        for label in ("NSFW", "Locked", "Archived", "Stickied", "Spoiler"):
            assert label not in rendered, (label, bogus)

    def test_real_booleans_still_label(self):
        from fetchaller.content.reddit import format_reddit_post

        rendered = format_reddit_post(
            {**self.BASE, "over_18": True, "locked": True, "archived": True}, 1
        )
        for label in ("NSFW", "Locked", "Archived"):
            assert label in rendered

    @pytest.mark.parametrize("bogus", ["false", "0", [1]])
    def test_truthy_non_boolean_never_hides_a_real_score(self, bogus):
        from fetchaller.content.reddit import format_reddit_post

        for field in ("hide_score", "score_hidden"):
            rendered = format_reddit_post({**self.BASE, field: bogus}, 1)
            assert "score 5" in rendered, (field, bogus)
            assert "score hidden" not in rendered, (field, bogus)

    def test_real_hidden_score_is_still_hidden(self):
        from fetchaller.content.reddit import format_reddit_post

        for field in ("hide_score", "score_hidden"):
            assert "score hidden" in format_reddit_post(
                {**self.BASE, field: True}, 1
            )

    def test_edited_keeps_its_timestamp_semantics(self):
        """Reddit sends ``edited`` as ``false`` OR an edit timestamp.

        It is deliberately excluded from the strict-boolean rule: a numeric
        timestamp is exactly how Reddit says "this was edited".
        """

        from fetchaller.content.reddit import format_reddit_post

        assert "Edited" in format_reddit_post(
            {**self.BASE, "edited": 1700000900.0}, 1
        )
        assert "Edited" not in format_reddit_post({**self.BASE, "edited": False}, 1)


class TestAboutRenderersSurviveMalformedPayloads:
    """A malformed field must degrade, never crash the whole render."""

    @pytest.mark.parametrize("bogus", ["false", ["x"], 7, "a string"])
    def test_non_mapping_profile_subreddit_does_not_crash(self, bogus):
        """``data.get("subreddit") or {}`` only rescued *falsy* values.

        A truthy non-mapping reached ``.get`` and raised AttributeError out of
        the entire user profile render.
        """

        from fetchaller.content.reddit import _render_user_about

        rendered = _render_user_about({"data": {"name": "alice", "subreddit": bogus}})
        assert "# u/alice" in rendered

    def test_profile_subreddit_mapping_still_renders(self):
        from fetchaller.content.reddit import _render_user_about

        rendered = _render_user_about({
            "data": {
                "name": "alice",
                "subreddit": {"title": "Alice's page", "public_description": "hi"},
            }
        })
        assert "Alice's page" in rendered and "hi" in rendered

    @pytest.mark.parametrize("bogus", ["unknown", None, "", [1], True])
    def test_unusable_subscriber_counts_are_omitted_not_fatal(self, bogus):
        """``int()`` on a non-numeric value raised ValueError out of the render.

        ``True`` is included because ``bool`` subclasses ``int`` and would
        otherwise render as "1 subscribers".
        """

        from fetchaller.content.reddit import _render_subreddit_about

        rendered = _render_subreddit_about(
            {"data": {"display_name": "x", "subscribers": bogus, "accounts_active": bogus}}
        )
        assert "# r/x" in rendered
        assert "Subscribers" not in rendered
        assert "Active now" not in rendered

    def test_real_counts_are_formatted(self):
        from fetchaller.content.reddit import _render_subreddit_about

        rendered = _render_subreddit_about(
            {"data": {"display_name": "x", "subscribers": 1234567}}
        )
        assert "- **Subscribers:** 1,234,567" in rendered

    @pytest.mark.parametrize("bogus", ["false", "0", [1]])
    def test_truthy_non_boolean_never_marks_a_subreddit_nsfw(self, bogus):
        from fetchaller.content.reddit import _render_subreddit_about

        rendered = _render_subreddit_about(
            {"data": {"display_name": "x", "over18": bogus}}
        )
        assert "NSFW" not in rendered

    def test_real_nsfw_subreddit_is_marked(self):
        from fetchaller.content.reddit import _render_subreddit_about

        assert "NSFW" in _render_subreddit_about(
            {"data": {"display_name": "x", "over18": True}}
        )


class TestRenderedCountsMatchRenderedItems:
    """Every "N items returned" must count the collection actually rendered.

    The duplicates listing shipped with ``len(unfiltered)`` beside a filtered
    comprehension, so unrendered children inflated the count. The same shape
    existed in the related, live-update, and multireddit-feed renderers, where
    it also left gaps in the visible numbering (1, 3, 4...) because
    ``enumerate`` ran over the unfiltered list.
    """

    @staticmethod
    def _post(name):
        return {"kind": "t3", "data": {
            "name": f"t3_{name}", "id": name, "title": f"Post {name}",
            "subreddit": "x", "author": "u", "score": 1, "num_comments": 0,
            "permalink": f"/r/x/comments/{name}/t/", "created_utc": 1700000000.0,
        }}

    @staticmethod
    def _noise():
        return {"kind": "t9", "data": {"name": "t9_zz"}}

    def test_related_counts_and_numbers_only_rendered_posts(self):
        from fetchaller.content.reddit import _render_related, route_reddit_url

        route = route_reddit_url("https://www.reddit.com/r/x/comments/abc/t/related/")
        payload = [
            {"kind": "Listing", "data": {"children": [self._post("src")]}},
            {"kind": "Listing", "data": {"children": [
                self._post("aaa"), self._noise(), self._post("bbb"),
            ]}},
        ]
        rendered = _render_related(payload, route, 100_000)
        assert "2 items returned" in rendered
        assert "1. Post aaa" in rendered and "2. Post bbb" in rendered
        assert "3. " not in rendered

    def test_live_update_listing_counts_only_live_updates(self):
        from fetchaller.content.reddit import (
            _render_live_update_listing,
            route_reddit_url,
        )

        route = route_reddit_url("https://www.reddit.com/live/abc123/updates/u1/")
        payload = {"kind": "Listing", "data": {"children": [
            {"kind": "LiveUpdate", "data": {"id": "u1", "body": "first"}},
            self._noise(),
            {"kind": "LiveUpdate", "data": {"id": "u2", "body": "second"}},
        ]}}
        rendered = _render_live_update_listing(payload, route, 100_000)
        assert "2 updates returned" in rendered
        assert "1." in rendered and "2." in rendered
        assert "3." not in rendered

    def test_multireddit_feed_counts_only_rendered_kinds(self):
        """The feed renders ``t3``/``t1`` with no ``else``.

        Any other kind was silently dropped from the output yet still counted
        in the "N items returned" header computed above the loop.
        """

        from fetchaller.content.reddit import (
            _render_multi_profile,
            route_reddit_url,
        )

        route = route_reddit_url("https://www.reddit.com/user/someone/m/mymulti/")
        metadata = {"kind": "LabeledMulti", "data": {
            "name": "mymulti", "display_name": "My Multi",
            "path": "/user/someone/m/mymulti/", "subreddits": [],
        }}
        listing = {"kind": "Listing", "data": {"children": [
            self._post("aaa"), self._noise(), self._post("bbb"),
        ]}}
        rendered = _render_multi_profile([metadata, listing], route, 100_000)
        assert "2 items returned" in rendered
        assert "1. Post aaa" in rendered and "2. Post bbb" in rendered
        assert "3. " not in rendered


class TestRedditSessionGateGetsAShortPause:
    """A recognised anonymous-session gate must not impose the block backoff.

    Reddit answers an opaque 403 ("You've been blocked by network security")
    when the anonymous session needs re-verifying. wafer recognises that page
    and tags the response ``challenge_type="reddit"``, then re-runs its
    verification and re-establishes cookies -- measured at 1.9s from a cold
    cookie cache. Treating it like any other 403 parked *every* queued Reddit
    request behind the configured five-minute block backoff, turning a
    two-second self-healing blip into a five-minute outage. That is what made
    the block look persistent to callers even though a manual retry worked.

    An opaque 403 wafer could NOT identify keeps the conservative delay.
    """

    @staticmethod
    def _queue():
        queue = Mock()

        async def enqueue(callback, *args, **kwargs):
            return await callback(*args)

        queue.enqueue = AsyncMock(side_effect=enqueue)
        return queue

    @staticmethod
    def _gate_response(retry_after: str | None = None):
        response = _JsonResponse(
            None,
            status_code=403,
            headers={"retry-after": retry_after} if retry_after else {},
        )
        response.challenge_type = "reddit"
        return response

    async def test_recognised_gate_uses_the_short_pause_and_retries_once(self):
        from fetchaller.tools.browse_reddit import _REDDIT_SESSION_GATE_BACKOFF

        queue = self._queue()
        session = _JsonSession(
            [
                self._gate_response(),
                _JsonResponse({"kind": "Listing", "data": {"children": []}}),
            ]
        )
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            session,
            queue,
        )

        queue.set_backoff.assert_called_once_with(
            403, retry_after=None, default_delay=_REDDIT_SESSION_GATE_BACKOFF
        )
        assert _REDDIT_SESSION_GATE_BACKOFF < 60
        assert result == {
            "data": {"kind": "Listing", "data": {"children": []}}
        }
        assert len(session.calls) == 2

    async def test_raised_gate_response_reaches_the_same_bounded_retry(self):
        from fetchaller.tools.browse_reddit import _REDDIT_SESSION_GATE_BACKOFF

        queue = self._queue()
        gate = self._gate_response()
        session = Mock()
        session.get = AsyncMock(
            side_effect=[
                wafer.ChallengeDetected(
                    "reddit",
                    "https://www.reddit.com/r/Python/hot.json",
                    403,
                    response=gate,
                ),
                _JsonResponse(
                    {"kind": "Listing", "data": {"children": []}}
                ),
            ]
        )

        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            session,
            queue,
        )

        queue.set_backoff.assert_called_once_with(
            403,
            retry_after=None,
            default_delay=_REDDIT_SESSION_GATE_BACKOFF,
        )
        assert result == {
            "data": {"kind": "Listing", "data": {"children": []}}
        }
        assert session.get.await_count == 2

    async def test_repeated_recognised_gate_returns_an_error_without_looping(self):
        queue = self._queue()
        session = _JsonSession(
            [self._gate_response(), self._gate_response()]
        )

        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            session,
            queue,
        )

        assert "session gate" in result["error"]
        assert "retry" in result["error"].lower()
        assert len(session.calls) == 2
        assert queue.set_backoff.call_count == 2

    async def test_unrecognised_403_keeps_the_conservative_delay(self):
        from fetchaller.tools.browse_reddit import _OPAQUE_403_BACKOFF

        queue = self._queue()
        response = _JsonResponse(None, status_code=403)  # no challenge_type
        result = await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            _JsonSession([response]),
            queue,
        )

        queue.set_backoff.assert_called_once_with(
            403, retry_after=None, default_delay=_OPAQUE_403_BACKOFF
        )
        assert _OPAQUE_403_BACKOFF == 300.0
        # The session-gate wording must not be claimed without evidence.
        assert "session gate" not in result["error"]

    async def test_retry_after_still_overrides_the_short_pause(self):
        """A server-supplied Retry-After is authoritative over either default."""

        queue = self._queue()
        await fetch_reddit_json(
            "https://www.reddit.com/r/Python/hot.json",
            _JsonSession([self._gate_response(retry_after="90")]),
            queue,
        )

        assert queue.set_backoff.call_args.kwargs["retry_after"] == 90.0

    def test_short_pause_cannot_shorten_an_existing_backoff(self):
        """The queue takes the max, so a gate pause never undoes a 429 wall."""

        from fetchaller.queue.reddit_queue import QueueConfig, RedditRequestQueue

        queue = RedditRequestQueue(QueueConfig())
        queue.set_backoff(429)  # long rate-limit wall
        long_until = queue._backoff_until
        queue.set_backoff(403, default_delay=5.0)  # short gate pause
        assert queue._backoff_until == long_until


class TestEveryStateFlagIsStrict:
    """No renderer may state a fact off a truthy non-boolean.

    The first sweep caught over_18/locked/archived/stickied/hide_score but
    missed seven more. The worst was ``promoted``: it does not mislabel a post,
    it ``continue``s past it, so a truthy non-boolean silently DROPPED a real
    post from the related listing -- data loss rather than a wrong label.
    """

    BASE = {
        "name": "t3_a", "id": "a", "title": "T", "subreddit": "x",
        "author": "u", "score": 5, "num_comments": 1,
        "permalink": "/r/x/comments/a/t/", "created_utc": 1700000000.0,
    }
    BOGUS = ["false", "0", "no", 1, [1], {"k": 1}]

    @pytest.mark.parametrize("bogus", BOGUS)
    def test_post_labels_require_real_booleans(self, bogus):
        from fetchaller.content.reddit import format_reddit_post

        rendered = format_reddit_post(
            {**self.BASE, "is_original_content": bogus, "contest_mode": bogus}, 1
        )
        assert "OC" not in rendered
        assert "Contest mode" not in rendered

    def test_post_labels_still_appear_for_true(self):
        from fetchaller.content.reddit import format_reddit_post

        rendered = format_reddit_post(
            {**self.BASE, "is_original_content": True, "contest_mode": True}, 1
        )
        assert "OC" in rendered and "Contest mode" in rendered

    @pytest.mark.parametrize("bogus", BOGUS)
    def test_subreddit_directory_status_requires_real_booleans(self, bogus):
        from fetchaller.content.reddit import _render_subreddit_directory_item

        rendered = _render_subreddit_directory_item(
            {"display_name": "x", "quarantine": bogus, "over18": bogus}, 1, None
        )
        assert "Quarantined" not in rendered
        assert "NSFW" not in rendered

    def test_subreddit_directory_status_still_marks_true(self):
        from fetchaller.content.reddit import _render_subreddit_directory_item

        rendered = _render_subreddit_directory_item(
            {"display_name": "x", "quarantine": True, "over18": True}, 1, None
        )
        assert "Quarantined" in rendered and "NSFW" in rendered

    @staticmethod
    def _related_html(promoted):
        """One New Reddit related-post card carrying an embedded post blob."""

        import json as _json
        from urllib.parse import quote as _quote

        event = {"post": {
            "id": "t3_abc123", "title": "A real post",
            "subreddit_name": "worldnews",
            "url": "https://www.reddit.com/r/worldnews/comments/abc123/a_real_post/",
            "score": 10, "number_comments": 2, "type": "link",
            "created_timestamp": 1700000000000, "promoted": promoted,
        }}
        blob = _quote(_json.dumps(event))
        return (
            "<html><aside aria-label='Related Posts Section'>"
            f"<reddit-pdp-right-rail-post event-data=\"{blob}\">"
            "</reddit-pdp-right-rail-post></aside></html>"
        )

    @pytest.mark.parametrize("bogus", ["false", "0", 1, [1]])
    def test_promoted_non_boolean_never_drops_a_real_post(self, bogus):
        """``promoted`` gates a ``continue``, so a wrong read loses the post."""

        from fetchaller.content.reddit import parse_reddit_related_html

        parsed = parse_reddit_related_html(self._related_html(bogus), 5)
        children = parsed["data"]["children"]
        assert len(children) == 1, f"post dropped for promoted={bogus!r}"
        assert children[0]["data"]["name"] == "t3_abc123"

    def test_genuinely_promoted_post_is_still_dropped(self):
        from fetchaller.content.reddit import parse_reddit_related_html

        parsed = parse_reddit_related_html(self._related_html(True), 5)
        assert parsed["data"]["children"] == []

    def test_no_bare_truthiness_remains_on_reddit_state_flags(self):
        """Structural guard so the next flag added does not repeat this.

        ``edited`` (false-or-timestamp) and ``controversiality`` (int 0/1) are
        intentionally excluded -- truthiness is their correct semantics.
        """

        import re as _re
        from pathlib import Path

        allowed = {
            "edited", "controversiality", "errors", "title", "reason",
            "revision_id", "revision_date", "granted_at", "visibility",
            "state", "public_description", "body",
        }
        source = Path("src/fetchaller/content/reddit.py").read_text()
        offenders = []
        for number, line in enumerate(source.splitlines(), 1):
            match = _re.match(r'\s+if [a-z_]*\.?get\("([a-z_0-9]+)"\):\s*$', line)
            if match and match.group(1) not in allowed:
                offenders.append(f"{number}: {line.strip()}")
        assert not offenders, (
            "bare truthiness on a Reddit field; use _flag(data, name) or "
            "`is True` if it becomes a factual claim:\n  " + "\n  ".join(offenders)
        )


class TestRemainingFilteredNumbering:
    """Skipped entries must not burn their index or inflate a count.

    Three sites still enumerated before filtering after the first sweep, so an
    unusable entry left a gap in the visible numbering (1, 3, 4...) and, where
    a count was shown, overstated it.
    """

    def test_multireddit_communities_number_contiguously(self):
        from fetchaller.content.reddit import _multi_community_sections

        sections = _multi_community_sections({"data": {"subreddits": [
            None, {"name": "python"}, {"no": "name"}, {"name": "rust"},
        ]}})
        headings = [text.split("\n")[0] for text, _ in sections]
        assert headings == ["1. **r/python**", "2. **r/rust**"]

    def test_profile_recent_activity_numbers_contiguously(self):
        from fetchaller.content.reddit import render_reddit_route, route_reddit_url

        route = route_reddit_url("https://www.reddit.com/user/someone/")
        post = {"kind": "t3", "data": {
            "name": "t3_p1", "id": "p1", "title": "A post", "subreddit": "x",
            "author": "someone", "score": 1, "num_comments": 0,
            "permalink": "/r/x/comments/p1/a/", "created_utc": 1700000000.0,
        }}
        payloads = [
            {"kind": "t2", "data": {"name": "someone"}},
            {"kind": "Listing", "data": {"children": [
                {"kind": "t9", "data": {"name": "t9_junk"}}, post,
            ]}},
            {}, [], {},
        ]
        rendered = render_reddit_route(route, payloads, max_tokens=5000)
        assert "1. A post" in rendered
        assert "2. " not in rendered

    def test_verified_requires_a_real_boolean(self):
        from fetchaller.content.reddit import _render_user_about

        for bogus in ("false", "0", [1], 1):
            assert "Verified" not in _render_user_about(
                {"data": {"name": "a", "verified": bogus}}
            ), bogus
        assert "Verified" in _render_user_about(
            {"data": {"name": "a", "verified": True}}
        )
