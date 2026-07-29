"""Independent Old Reddit surface contract and end-to-end fixture gate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from fetchaller.content.reddit import (
    REDDIT_ROUTE_KINDS,
    render_reddit_route,
    route_reddit_url,
)
from fetchaller.tools.reddit_fetch import (
    _payload_schema_error,
    fetch_mapped_reddit,
)
from scripts.reddit_parity import (
    DEFAULT_CORPUS,
    LEGACY_SURFACE_VARIANTS,
    SEMANTIC_VALIDATOR_IDS,
    _legacy_variant_inventory_error,
    load_corpus,
)

ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONTRACT = ROOT / "baselines" / "reddit-legacy-contract-v1.json"


def _load_legacy_contract() -> dict:
    payload = json.loads(LEGACY_CONTRACT.read_text())
    assert payload["schema_version"] == 1
    assert payload["contract_id"] == "old-reddit-public-read-surface-v1"
    return payload


def test_versioned_legacy_contract_is_independent_and_complete():
    contract = _load_legacy_contract()

    assert contract["source_revision"] == "b3a7279d8b77f98421095412c4d6a78f0376f67d"
    assert all(
        command.startswith(f"git show {contract['source_revision'][:8]}:")
        for command in contract["derivation"]
    )
    assert set(contract["representations"]) == {
        "normal",
        "explicit_json",
        "raw",
        "fallback",
    }
    assert set(contract["access_states"]) == {
        "banned",
        "forbidden",
        "gated",
        "not_found",
        "private",
        "quarantined",
    }
    assert contract["required_public_fields"]
    assert contract["output_semantics"]
    variants = contract["surface_variants"]
    assert len(variants) == 94
    assert len({variant["id"] for variant in variants}) == 94
    assert tuple(variants) == LEGACY_SURFACE_VARIANTS


def test_independent_legacy_contract_detects_omissions_from_corpus_and_router():
    """Neither current inventory can redefine the independent legacy baseline."""

    contract = _load_legacy_contract()
    legacy_kinds = {surface["kind"] for surface in contract["surfaces"]}
    corpus = load_corpus(DEFAULT_CORPUS)
    legacy_variants = {
        variant["id"]: variant for variant in contract["surface_variants"]
    }

    assert legacy_kinds == {entry.kind for entry in corpus if entry.kind}
    assert legacy_kinds == REDDIT_ROUTE_KINDS
    assert {tool["name"] for tool in contract["tools"]} == {
        entry.tool for entry in corpus if entry.tool
    }
    assert set(contract["access_states"]) == {
        entry.access_state for entry in corpus if entry.access_state
    }
    assert any(entry.raw for entry in corpus)
    assert set(legacy_variants) == {entry.id for entry in corpus}
    assert set(legacy_variants) == SEMANTIC_VALIDATOR_IDS
    for entry in corpus:
        variant = legacy_variants[entry.id]
        assert entry.kind == variant.get("kind")
        assert entry.tool == variant.get("tool")


def test_independent_legacy_matrix_detects_one_same_kind_variant_deletion():
    corpus = load_corpus(DEFAULT_CORPUS)
    mutated = [
        entry
        for entry in corpus
        if entry.id != "listing_subreddit_randomrising"
    ]

    error = _legacy_variant_inventory_error(mutated)

    assert error is not None
    assert "listing_subreddit_randomrising" in error


@pytest.mark.parametrize(
    "variant",
    _load_legacy_contract()["surface_variants"],
    ids=lambda variant: variant["id"],
)
def test_every_independent_legacy_variant_routes_to_its_exact_kind(variant):
    """Prove all 94 independently inventoried route/tool variants exist."""

    if tool := variant.get("tool"):
        assert tool in {"browse_reddit", "search_reddit"}
        assert variant["arguments"]
        return

    route = route_reddit_url(variant["example_url"])

    assert route is not None
    assert route.kind == variant["kind"]


@pytest.mark.parametrize(
    "surface",
    _load_legacy_contract()["surfaces"],
    ids=lambda surface: surface["id"],
)
def test_every_independent_legacy_surface_routes_to_its_contract(surface):
    """Parametrized audit of every legacy URL family, independent of the corpus."""

    route = route_reddit_url(surface["example_url"])

    assert route is not None
    assert route.kind == surface["kind"]
    assert surface["url_shape"]
    assert surface["required_fields"]
    assert surface["output_semantics"]
    if surface["representation"] == "normal":
        assert route.requests
    elif surface["representation"] == "explicit_json":
        assert route.is_explicit_json
    else:
        assert surface["representation"] == "fallback"
        assert not route.requests


class _JsonResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: object):
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _JsonSession:
    def __init__(self, payload: object):
        self._response = _JsonResponse(payload)
        self.calls: list[str] = []

    async def get(self, url: str, **_kwargs) -> _JsonResponse:
        self.calls.append(url)
        return self._response


_END_TO_END_FIXTURES = [
    pytest.param(
        "https://www.reddit.com/user/alice/m/public/about/",
        "multi_about",
        {
            "data": {
                "display_name": "public",
                "description_md": "A public multireddit",
                "subreddits": [{"name": "Python"}],
            }
        },
        ("# public", "owner u/alice", "A public multireddit", "r/Python"),
        id="multi_about",
    ),
    pytest.param(
        "https://www.reddit.com/r/Python/wiki/discussions/index/",
        "wiki_discussions",
        {
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "abc123",
                            "name": "t3_abc123",
                            "title": "Discuss the Python wiki",
                            "author": "alice",
                            "subreddit": "Python",
                            "permalink": (
                                "/r/Python/comments/abc123/"
                                "discuss_the_python_wiki/"
                            ),
                            "score": 7,
                            "num_comments": 3,
                            "created_utc": 1_700_000_000,
                        },
                    }
                ],
                "after": "t3_abc123",
            }
        },
        (
            "# r/Python · wiki discussions",
            "Discuss the Python wiki",
            "?after=t3_abc123&count=1",
        ),
        id="wiki_discussions",
    ),
    pytest.param(
        "https://www.reddit.com/live/abc123/about/",
        "live_about",
        {
            "data": {
                "title": "Python release live",
                "state": "live",
                "viewer_count": 123,
                "description": "Release updates",
                "resources": "https://python.org/",
            }
        },
        (
            "# Python release live",
            "**State:** live",
            "**Viewers:** 123",
            "Release updates",
        ),
        id="live_about",
    ),
]


@pytest.mark.parametrize(
    ("url", "expected_kind", "payload", "markers"),
    _END_TO_END_FIXTURES,
)
async def test_missing_legacy_fixtures_cover_route_schema_renderer_and_mcp(
    url,
    expected_kind,
    payload,
    markers,
):
    """Exercise all four layers for every formerly missing fixture."""

    route = route_reddit_url(url)
    assert route is not None
    assert route.kind == expected_kind
    assert route.requests
    assert _payload_schema_error(route, 0, payload) is None

    rendered = render_reddit_route(route, [payload], max_tokens=5_000)
    assert all(marker in rendered for marker in markers)

    session = _JsonSession(payload)
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
            timeout=10,
        )

    assert result["content_type"] == "markdown"
    assert result["url"] == route.canonical_url
    assert result["content"] == rendered
    assert session.calls == list(route.requests)
