"""Checked-in New Reddit live-parity corpus contract."""

import copy
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fetchaller.content.reddit import (
    REDDIT_ROUTE_KINDS,
    RedditRoute,
    render_reddit_route,
    route_reddit_url,
)
from scripts.reddit_parity import (
    _REDDIT_CREDENTIAL_ENV,
    _REQUIRED_DISCOVERY_STAGES,
    _REQUIRED_INFRASTRUCTURE_STAGES,
    DEFAULT_CORPUS,
    SEMANTIC_VALIDATOR_IDS,
    CorpusEntry,
    _audit_browser_dispatch,
    _audit_browser_egress,
    _audit_harness_identity,
    _audit_process_identity,
    _audit_reddit_cookie_cache,
    _audit_reddit_session,
    _call,
    _capture_harness_identity,
    _configure_fresh_cache,
    _counted_output_error,
    _discover_live_targets,
    _discovery_call,
    _exit_code,
    _materialize_entry,
    _oauth_forwarding,
    _prepare_empty_evidence_directory,
    _semantic_contract_error,
    _valid_raw_new_reddit_html,
    _validated_fetch_next_page,
    _validated_fetch_previous_page,
    eligible,
    load_corpus,
    main_async,
)


def _passed_release_infrastructure():
    return [
        SimpleNamespace(id=audit_id, stage=stage, status="passed")
        for audit_id, stages in _REQUIRED_INFRASTRUCTURE_STAGES.items()
        for stage in stages
    ]


def _passed_release_discovery():
    return [
        SimpleNamespace(id=discovery_id, stage=stage, status="passed")
        for discovery_id, stages in _REQUIRED_DISCOVERY_STAGES.items()
        for stage in stages
    ]


def test_reddit_parity_corpus_covers_every_declared_route_representation():
    entries = load_corpus(DEFAULT_CORPUS)

    assert {entry.kind for entry in entries if entry.kind} == REDDIT_ROUTE_KINDS
    assert {entry.tool for entry in entries if entry.tool} == {
        "browse_reddit",
        "search_reddit",
    }

    assert any(entry.raw for entry in entries)
    assert any(entry.kind == "explicit_json" for entry in entries)
    assert all(entry.required_any for entry in entries)
    assert {entry.id for entry in entries} == SEMANTIC_VALIDATOR_IDS
    assert {entry.access_state for entry in entries if entry.access_state} == {
        "banned", "forbidden", "gated", "not_found", "private", "quarantined",
    }
    assert all(entry.id for entry in entries)
    assert {
        entry.id for entry in entries if entry.live == "fixture_only"
    } == {
        "access_banned",
        "access_forbidden",
        "access_gated",
        "access_not_found",
        "access_private",
        "access_quarantined",
        # Reddit withdrew both of these from anonymous callers: the global
        # comment firehose answers 200-with-zero-children, and moderator
        # rosters 403 or redirect to login.
        "comments_global",
        "moderators",
    }
    assert all(
        entry.offline_reason
        for entry in entries
        if entry.live == "fixture_only"
    )
    assert {
        entry.discovery for entry in entries if entry.discovery
    } == {
        "collection",
        "duplicates",
        "duplicates_subreddit",
        "live",
        "live_about",
        "live_contributors",
        "live_update",
        "morechildren",
        "multi_about",
        "multi_profile",
        "related",
        "related_subreddit",
        "thread",
        "thread_gallery",
        "thread_global_permalink",
        "listing_by_id_comment",
        "listing_by_id_mixed",
        "wiki_diff",
    }


def test_process_identity_audit_requires_one_exact_pid_and_source_pair(
    tmp_path,
):
    log = tmp_path / "server.stderr.log"
    identity = (
        "PROCESS_IDENTITY pid=123 "
        "fetchaller_source=/app/fetchaller/server.py "
        f"fetchaller_sha256={'a' * 64} "
        "wafer_source=/app/wafer/__init__.py "
        f"wafer_sha256={'b' * 64}"
    )
    end_identity = identity.replace(
        "PROCESS_IDENTITY ",
        "PROCESS_IDENTITY_END ",
    )
    log.write_text(f"[startup] {identity}\n[shutdown] {end_identity}\n")

    passed = _audit_process_identity(log, "cold")
    assert passed.status == "passed"
    assert "pid=123" in passed.detail
    assert "/app/fetchaller/server.py" in passed.detail

    log.write_text(f"{identity}\n{identity}\n{end_identity}\n")
    duplicate = _audit_process_identity(log, "cold")
    assert duplicate.status == "failed"
    assert "start=2 end=1" in duplicate.detail

    changed_end = end_identity.replace(
        f"wafer_sha256={'b' * 64}",
        f"wafer_sha256={'c' * 64}",
    )
    log.write_text(f"{identity}\n{changed_end}\n")
    changed = _audit_process_identity(log, "cold")
    assert changed.status == "failed"
    assert changed.detail == (
        "process source identity changed during run: wafer_sha256"
    )

    expected_packages = {
        "fetchaller": {
            "source": "/app/fetchaller/server.py",
            "sha256": "a" * 64,
        },
        "wafer": {
            "source": "/app/wafer/__init__.py",
            "sha256": "b" * 64,
        },
    }
    log.write_text(f"{identity}\n{end_identity}\n")
    matched = _audit_process_identity(
        log,
        "cold",
        expected_packages=expected_packages,
    )
    assert matched.status == "passed"

    stale_packages = copy.deepcopy(expected_packages)
    stale_packages["wafer"]["sha256"] = "c" * 64
    stale = _audit_process_identity(
        log,
        "cold",
        expected_packages=stale_packages,
    )
    assert stale.status == "failed"
    assert stale.detail.endswith("wafer_sha256")

    container_paths = copy.deepcopy(expected_packages)
    container_paths["fetchaller"]["source"] = (
        "/different/container/fetchaller/server.py"
    )
    container_paths["wafer"]["source"] = (
        "/different/container/wafer/__init__.py"
    )
    container_match = _audit_process_identity(
        log,
        "cold",
        expected_packages=container_paths,
        compare_paths=False,
    )
    assert container_match.status == "passed"


def test_harness_identity_covers_actual_selected_corpus_and_every_source(
    tmp_path,
):
    corpus = tmp_path / "selected-corpus.json"
    corpus.write_bytes(DEFAULT_CORPUS.read_bytes())
    start = _capture_harness_identity(corpus)

    assert start["files"]["corpus"]["path"] == str(corpus)
    assert start["files"]["runner"]["path"].endswith(
        "/scripts/reddit_parity.py"
    )
    assert start["files"]["smoke_test"]["path"].endswith(
        "/scripts/smoke_test.py"
    )
    assert start["files"]["legacy_contract"]["path"].endswith(
        "/baselines/reddit-legacy-contract-v1.json"
    )
    assert start["packages"]["fetchaller"]["source"].endswith(
        "/fetchaller/server.py"
    )
    assert start["packages"]["wafer"]["source"].endswith(
        "/wafer/__init__.py"
    )
    assert _audit_harness_identity(start, copy.deepcopy(start)).status == (
        "passed"
    )

    for section, name in (
        ("files", "runner"),
        ("files", "smoke_test"),
        ("files", "corpus"),
        ("files", "legacy_contract"),
        ("packages", "fetchaller"),
        ("packages", "wafer"),
    ):
        end = copy.deepcopy(start)
        end[section][name]["sha256"] = "0" * 64
        evidence = _audit_harness_identity(start, end)
        assert evidence.status == "failed"
        assert f"{section}.{name}" in evidence.detail

    corpus.write_text(corpus.read_text() + "\n")
    changed_corpus = _capture_harness_identity(corpus)
    evidence = _audit_harness_identity(start, changed_corpus)
    assert evidence.status == "failed"
    assert "files.corpus" in evidence.detail


def test_release_evidence_directory_must_start_empty(tmp_path):
    new = tmp_path / "new"
    assert _prepare_empty_evidence_directory(new) == new
    assert new.is_dir()
    _prepare_empty_evidence_directory(new)

    (new / "stale.txt").write_text("old evidence")
    try:
        _prepare_empty_evidence_directory(new)
    except ValueError as exc:
        assert "must start empty" in str(exc)
    else:
        raise AssertionError("stale evidence directory was accepted")

    not_a_directory = tmp_path / "output-file"
    not_a_directory.write_text("not a directory")
    try:
        _prepare_empty_evidence_directory(not_a_directory)
    except ValueError as exc:
        assert "must start empty" in str(exc)
    else:
        raise AssertionError("evidence output file was accepted")

    symlink = tmp_path / "output-link"
    symlink.symlink_to(tmp_path / "empty-target", target_is_directory=True)
    try:
        _prepare_empty_evidence_directory(symlink)
    except ValueError as exc:
        assert "must not be a symlink" in str(exc)
    else:
        raise AssertionError("symlink evidence directory was accepted")


async def test_stale_evidence_fails_main_before_server_launch_or_write(
    tmp_path,
):
    output = tmp_path / "evidence"
    output.mkdir()
    sentinel = output / "stale-artifact.txt"
    sentinel.write_text("do not overwrite")
    server_parameters = Mock()
    args = SimpleNamespace(
        corpus=DEFAULT_CORPUS,
        output=output,
    )

    with patch(
        "scripts.reddit_parity._stdio_server_parameters",
        server_parameters,
    ):
        try:
            await main_async(args)
        except ValueError as exc:
            assert "must start empty" in str(exc)
        else:
            raise AssertionError("stale release evidence was accepted")

    server_parameters.assert_not_called()
    assert sentinel.read_text() == "do not overwrite"
    assert set(output.iterdir()) == {sentinel}


def test_every_live_corpus_entry_rejects_its_heading_or_marker_alone():
    for entry in load_corpus(DEFAULT_CORPUS):
        if entry.live == "fixture_only" or entry.expect_error:
            continue
        thin_shell = entry.required_any[0]
        assert _semantic_contract_error(entry, thin_shell) is not None, entry.id


def test_result_starved_routes_do_not_claim_pagination():
    """Two routes cannot honestly assert a second page, so they must not.

    ``pagination`` means "following this route's cursor yields another valid
    page". Both of these are result-starved, and a live run proved it:

    * ``duplicates_subreddit`` asks for ``limit=5`` against a discovered post.
      The post that run found had four crossposts, so Reddit issued no cursor
      at all -- "missing validated pagination cursor". Its sibling
      ``duplicates`` never claimed pagination for exactly this reason.
    * ``user_directory_search`` searches ``q=python``, which matches one
      account. Reddit still emits an ``after`` token, and following it returns
      "0 users returned" -- so the follow-up has no item identities.

    Neither is a renderer defect: both pages were correct. Keep the assertion
    off unless the corpus moves to a target whose second page is guaranteed.
    """

    starved = {"duplicates_subreddit", "user_directory_search"}
    by_id = {entry.id: entry for entry in load_corpus(DEFAULT_CORPUS)}
    for entry_id in starved:
        assert entry_id in by_id, f"{entry_id} vanished from the corpus"
        assert not by_id[entry_id].pagination, entry_id
        assert not by_id[entry_id].pagination_round_trip, entry_id
    # The assertion must stay broadly enforced everywhere else.
    asserting = [e.id for e in by_id.values() if e.pagination]
    assert len(asserting) >= 40, f"pagination coverage collapsed to {len(asserting)}"


def test_gilded_identity_check_survives_the_non_user_scoped_feeds():
    """The gilded routes must reach their identity check without crashing.

    ``test_every_live_corpus_entry_rejects_its_heading_or_marker_alone`` feeds a
    thin shell, so these routes fail the populated-count gate and return long
    before the identity check runs. That hid a checker crash: the expected
    heading was built as a mapping literal, so the user-scoped value called
    ``_entry_username()`` for the global and subreddit feeds too and raised
    ``ValueError`` on their non-user paths. A live run only reached it once a
    feed rendered real cards, and it aborted the whole gate mid-run rather than
    failing one route.

    So drive each gilded route past the count gate with one complete card.
    """

    card = (
        "1. **A gilded comment**\n"
        "   r/Python · u/someone · 123 score · 2026-01-01 12:00 UTC\n"
        "   body text here\n"
        "Permalink: https://www.reddit.com/r/Python/comments/abc123/some_slug/def456/\n"
    )
    gilded = [
        entry
        for entry in load_corpus(DEFAULT_CORPUS)
        if entry.id.startswith("gilded_") or entry.id == "user_gilded"
    ]
    assert gilded, "corpus lost its gilded routes"
    for entry in gilded:
        text = f"{entry.required_any[0]}\n\n1 items returned\n\n{card}"
        assert _counted_output_error(entry, text) is None, entry.id
        # A contract error (or None) is fine; an exception is not.
        _semantic_contract_error(entry, text)


def test_activity_gate_requires_every_comment_body_and_exact_navigation():
    entry = CorpusEntry(
        id="user_listing",
        live="stable",
        kind="user_listing",
        url="https://www.reddit.com/user/AutoModerator/comments/",
        required_any=("u/AutoModerator",),
    )
    route = RedditRoute(
        entry.url,
        "user_listing",
        username="AutoModerator",
        label="comments",
    )
    payload = {
        "kind": "Listing",
        "data": {
            "after": "t1_next123",
            "dist": 1,
            "children": [{
                "kind": "t1",
                "data": {
                    "id": "comment1",
                    "name": "t1_comment1",
                    "link_id": "t3_post1",
                    "parent_id": "t3_post1",
                    "subreddit": "Python",
                    "subreddit_name_prefixed": "r/Python",
                    "author": "AutoModerator",
                    "author_flair_text": "Community helper",
                    "author_flair_richtext": [{
                        "e": "emoji",
                        "u": "https://styles.redditmedia.com/helper.png",
                    }],
                    "score": 7,
                    "created_utc": 1_700_000_000,
                    "body": " Public activity body.",
                    "link_title": "A moderated thread",
                    "permalink": (
                        "/r/Python/comments/post1/a_moderated_thread/comment1/"
                    ),
                },
            }],
        },
    }
    valid = render_reddit_route(route, [payload], max_tokens=5_000)
    assert "author flair: Community helper" in valid
    assert "Media: https://styles.redditmedia.com/helper.png" in valid
    assert _semantic_contract_error(entry, valid) is None

    bodyless = valid.replace("Public activity body.\n\n", "")
    assert "substantive body" in _semantic_contract_error(entry, bodyless)

    post_only_permalink = valid.replace(
        (
            "Permalink: https://www.reddit.com/r/Python/comments/"
            "post1/a_moderated_thread/comment1/"
        ),
        "Permalink: https://www.reddit.com/r/Python/comments/post1/",
    )
    assert "comment permalink" in _semantic_contract_error(
        entry,
        post_only_permalink,
    )

    permalink_url = (
        "https://www.reddit.com/r/Python/comments/"
        "post1/a_moderated_thread/comment1/"
    )
    identical_parent = valid.replace(
        "Parent context: https://www.reddit.com/comments/post1/",
        f"Parent context: {permalink_url}",
    )
    assert "parent context" in _semantic_contract_error(
        entry,
        identical_parent,
    )


def test_gallery_gate_matches_renderer_and_requires_exact_media_inventory():
    entry = CorpusEntry(
        id="thread_gallery",
        live="unstable",
        kind="thread",
        url="https://www.reddit.com/gallery/post1",
        required_any=("**Gallery (", "## Comments"),
    )
    route = route_reddit_url(entry.url)
    assert route is not None
    post = {
        "kind": "Listing",
        "data": {
            "children": [{
                "kind": "t3",
                "data": {
                    "id": "post1",
                    "name": "t3_post1",
                    "title": "Current gallery",
                    "subreddit": "Python",
                    "subreddit_name_prefixed": "r/Python",
                    "author": "alice",
                    "score": 7,
                    "num_comments": 0,
                    "created_utc": 1_700_000_000,
                    "permalink": (
                        "/r/Python/comments/post1/current_gallery/"
                    ),
                    "url": "https://www.reddit.com/gallery/post1",
                    "is_gallery": True,
                    "gallery_data": {
                        "items": [
                            {"media_id": "one"},
                            {"media_id": "two"},
                        ]
                    },
                    "media_metadata": {
                        "one": {
                            "s": {"u": "https://i.redd.it/one.jpg"}
                        },
                        "two": {
                            "s": {
                                "u": "https://preview.redd.it/two.jpg"
                            }
                        },
                    },
                },
            }],
        },
    }
    comments = {"kind": "Listing", "data": {"children": []}}

    rendered = render_reddit_route(
        route,
        [[post, comments]],
        max_tokens=5_000,
    )

    assert "**Gallery (2 items):**" in rendered
    assert _semantic_contract_error(entry, rendered) is None
    missing_media = rendered.replace(
        "  - https://preview.redd.it/two.jpg\n",
        "",
    )
    assert "exact complete unique media inventory" in (
        _semantic_contract_error(entry, missing_media)
    )


def test_oauth_and_fixture_routes_are_explicitly_not_live_passes_without_authority(monkeypatch):
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_REFRESH_TOKEN", raising=False)
    entries = {entry.id: entry for entry in load_corpus(DEFAULT_CORPUS)}

    assert eligible(entries["moderators"], include_unstable=False)[0] is False
    # The wiki page index is served by New Reddit's own anonymous page tree,
    # so it must stay a required public route with no credentials at all.
    assert eligible(entries["wiki_pages"], include_unstable=False) == (True, "")
    assert entries["wiki_pages"].live == "stable"
    assert entries["wiki_pages"].oauth_scopes == ()
    assert eligible(entries["live"], include_unstable=False)[0] is False
    assert eligible(entries["access_private"], include_unstable=False)[0] is False
    assert eligible(entries["live"], include_unstable=True) == (True, "")
    assert eligible(entries["access_private"], include_unstable=True)[0] is False
    assert eligible(entries["listing"], include_unstable=False) == (True, "")


def test_partial_oauth_configuration_never_unlocks_oauth_entries(monkeypatch):
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "only-one-value")
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_REFRESH_TOKEN", raising=False)
    entries = {entry.id: entry for entry in load_corpus(DEFAULT_CORPUS)}

    assert eligible(entries["moderators"], include_unstable=False)[0] is False
    assert entries["moderators"].live == "fixture_only"
    assert eligible(entries["wiki_pages"], include_unstable=False) == (True, "")
    # A partial (or absent) OAuth configuration must never be required, because
    # no corpus entry is OAuth-gated any more.
    assert all(entry.oauth_scopes == () for entry in entries.values())


def test_strict_release_mode_rejects_unrun_required_targets():
    skipped = [SimpleNamespace(status="not_run")]
    passed = [SimpleNamespace(status="passed")]
    failed = [SimpleNamespace(status="failed")]

    assert _exit_code(passed, strict=False) == 0
    assert _exit_code(skipped, strict=False) == 0
    assert _exit_code(passed, strict=True) == 1
    assert _exit_code(skipped, strict=True) == 1
    assert _exit_code(failed, strict=False) == 1


def test_strict_policy_ignores_only_inherently_nonpublic_fixture_evidence():
    entries = [
        CorpusEntry(
            id="stable",
            live="stable",
            kind="listing",
            url="https://www.reddit.com/r/Python/",
            required_any=("r/Python",),
        ),
        CorpusEntry(
            id="fixture",
            live="fixture_only",
            kind="live_about",
            url="https://www.reddit.com/live/abc123/about/",
            required_any=("Live",),
        ),
        CorpusEntry(
            id="oauth",
            live="oauth_required",
            kind="moderators",
            url="https://www.reddit.com/r/Python/about/moderators/",
            required_any=("Source: exact Reddit OAuth",),
            oauth_scopes=("read",),
        ),
    ]
    records = [
        *[
            SimpleNamespace(id="stable", stage=stage, status="passed")
            for stage in ("cold", "warm", "recreated")
        ],
        *[
            SimpleNamespace(id="fixture", stage=stage, status="not_run")
            for stage in ("cold", "warm", "recreated")
        ],
        *[
            SimpleNamespace(id="oauth", stage=stage, status="passed")
            for stage in ("cold", "warm", "recreated")
        ],
        *_passed_release_infrastructure(),
    ]

    assert _exit_code(records, strict=True, entries=entries) == 0
    inconsistent_optional = [
        SimpleNamespace(
            id=record.id,
            stage=record.stage,
            status=(
                "passed"
                if record.id == "fixture" and record.stage == "warm"
                else record.status
            ),
        )
        for record in records
    ]
    assert (
        _exit_code(
            inconsistent_optional,
            strict=True,
            entries=entries,
        )
        == 1
    )
    oauth_not_run = [
        SimpleNamespace(
            id=record.id,
            stage=record.stage,
            status="not_run" if record.id == "oauth" else record.status,
        )
        for record in records
    ]
    assert (
        _exit_code(
            oauth_not_run,
            strict=True,
            entries=entries,
        )
        == 1
    )
    assert (
        _exit_code(
            records,
            strict=False,
            entries=entries,
            require_oauth=True,
        )
        == 0
    )
    assert (
        _exit_code(
            oauth_not_run,
            strict=False,
            entries=entries,
            require_oauth=True,
        )
        == 1
    )


def test_strict_composed_policy_requires_selected_unstable_live_evidence():
    entries = [
        CorpusEntry(
            id="unstable",
            live="unstable",
            kind="thread",
            url="https://www.reddit.com/comments/abc123/",
            required_any=("comments",),
        )
    ]
    records = [
        *[
            SimpleNamespace(id="unstable", stage=stage, status="not_run")
            for stage in ("cold", "warm", "recreated")
        ],
        *_passed_release_infrastructure(),
    ]

    assert _exit_code(records, strict=True, entries=entries) == 0
    assert (
        _exit_code(
            records,
            strict=True,
            entries=entries,
            include_unstable=True,
        )
        == 1
    )


def test_strict_release_requires_exact_route_and_infrastructure_stage_shape():
    entries = [
        CorpusEntry(
            id="stable",
            live="stable",
            kind="listing",
            url="https://www.reddit.com/r/Python/",
            required_any=("r/Python",),
        )
    ]
    routes = [
        SimpleNamespace(id="stable", stage=stage, status="passed")
        for stage in ("cold", "warm", "recreated")
    ]
    infrastructure = _passed_release_infrastructure()
    complete = [*routes, *infrastructure]
    assert _exit_code(complete, strict=True, entries=entries) == 0

    assert (
        _exit_code(
            [
                record
                for record in complete
                if not (
                    record.id == "stable"
                    and record.stage == "warm"
                )
            ],
            strict=True,
            entries=entries,
        )
        == 1
    )
    assert (
        _exit_code(
            [*complete, routes[0]],
            strict=True,
            entries=entries,
        )
        == 1
    )
    wrong_stage_routes = [
        SimpleNamespace(
            id=record.id,
            stage=(
                "unexpected"
                if record.id == "stable" and record.stage == "warm"
                else record.stage
            ),
            status=record.status,
        )
        for record in complete
    ]
    assert (
        _exit_code(
            wrong_stage_routes,
            strict=True,
            entries=entries,
        )
        == 1
    )

    for audit_id in _REQUIRED_INFRASTRUCTURE_STAGES:
        missing = [
            record for record in complete if record.id != audit_id
        ]
        assert (
            _exit_code(missing, strict=True, entries=entries) == 1
        ), audit_id
    assert (
        _exit_code(
            [*complete, infrastructure[0]],
            strict=True,
            entries=entries,
        )
        == 1
    )
    wrong_audit_stage = [
        SimpleNamespace(
            id=record.id,
            stage=(
                "wrong"
                if record is infrastructure[0]
                else record.stage
            ),
            status=record.status,
        )
        for record in complete
    ]
    assert (
        _exit_code(
            wrong_audit_stage,
            strict=True,
            entries=entries,
        )
        == 1
    )


def test_strict_unstable_release_requires_exact_discovery_record_shape():
    entries = [
        CorpusEntry(
            id="unstable",
            live="unstable",
            kind="thread",
            url="https://www.reddit.com/comments/abc123/",
            required_any=("comments",),
        )
    ]
    routes = [
        SimpleNamespace(id="unstable", stage=stage, status="passed")
        for stage in ("cold", "warm", "recreated")
    ]
    infrastructure = _passed_release_infrastructure()
    discovery = _passed_release_discovery()
    complete = [*routes, *infrastructure, *discovery]

    assert (
        _exit_code(
            complete,
            strict=True,
            entries=entries,
            include_unstable=True,
        )
        == 0
    )
    for discovery_id in _REQUIRED_DISCOVERY_STAGES:
        missing = [
            record for record in complete if record.id != discovery_id
        ]
        assert (
            _exit_code(
                missing,
                strict=True,
                entries=entries,
                include_unstable=True,
            )
            == 1
        ), discovery_id

    assert (
        _exit_code(
            [*complete, discovery[0]],
            strict=True,
            entries=entries,
            include_unstable=True,
        )
        == 1
    )
    wrong_stage = [
        SimpleNamespace(
            id=record.id,
            stage="wrong" if record is discovery[0] else record.stage,
            status=record.status,
        )
        for record in complete
    ]
    assert (
        _exit_code(
            wrong_stage,
            strict=True,
            entries=entries,
            include_unstable=True,
        )
        == 1
    )
    not_run = [
        SimpleNamespace(
            id=record.id,
            stage=record.stage,
            status="not_run" if record is discovery[0] else record.status,
        )
        for record in complete
    ]
    assert (
        _exit_code(
            not_run,
            strict=True,
            entries=entries,
            include_unstable=True,
        )
        == 1
    )


def test_strict_release_accepts_only_complete_94_route_stage_matrix():
    entries = load_corpus(DEFAULT_CORPUS)
    records = [
        SimpleNamespace(
            id=entry.id,
            stage=stage,
            status=(
                "passed"
                if entry.live in {
                    "stable",
                    "unstable",
                    "oauth_required",
                }
                else "not_run"
            ),
        )
        for entry in entries
        for stage in ("cold", "warm", "recreated")
    ]
    complete = [
        *records,
        *_passed_release_infrastructure(),
        *_passed_release_discovery(),
    ]

    assert len(entries) == 94
    assert (
        _exit_code(
            complete,
            strict=True,
            entries=entries,
            include_unstable=True,
        )
        == 0
    )
    missing_one_same_kind_variant = [
        record
        for record in complete
        if record.id != "listing_subreddit_randomrising"
    ]
    assert (
        _exit_code(
            missing_one_same_kind_variant,
            strict=True,
            entries=entries,
            include_unstable=True,
        )
        == 1
    )


def test_docker_parity_runner_injects_a_verified_fresh_cache_bind(tmp_path):
    parameters = SimpleNamespace(
        command="docker",
        args=["run", "--rm", "-i", "fetchaller-mcp:candidate"],
        env={},
    )

    mapping = _configure_fresh_cache(parameters, tmp_path, None)

    assert mapping == {
        "mode": "docker_bind",
        "host_cache_dir": str(tmp_path),
        "server_cache_dir": "/app/data/reddit-parity-cache",
        "runtime_owner": f"{os.getuid()}:{os.getgid()}",
    }
    assert parameters.args[:10] == [
        "run",
        "--mount",
        f"type=bind,src={tmp_path},dst=/app/data/reddit-parity-cache",
        "--env",
        "WAFER_CACHE_DIR=/app/data/reddit-parity-cache",
        "--env",
        f"PUID={os.getuid()}",
        "--env",
        f"PGID={os.getgid()}",
        "--rm",
    ]


def test_docker_parity_runner_rejects_ambient_cache_or_credential_configuration(tmp_path):
    parameters = SimpleNamespace(
        command="docker",
        args=["run", "--env", "WAFER_CACHE_DIR=/cookies", "image"],
        env={},
    )

    try:
        _configure_fresh_cache(parameters, tmp_path, None)
    except ValueError as exc:
        assert "must not set parity-managed" in str(exc)
    else:
        raise AssertionError("ambient Docker cache configuration was accepted")

    no_host_oauth = SimpleNamespace(
        command="docker",
        args=["run", "--env=REDDIT_ACCESS_TOKEN=leak", "image"],
        env={},
    )
    partial_oauth = SimpleNamespace(
        command="docker",
        args=["run", "-eREDDIT_CLIENT_SECRET=leak", "image"],
        env={},
    )
    unused_oauth = SimpleNamespace(
        command="docker",
        args=["run", "--env", "REDDIT_REFRESH_TOKEN=leak", "image"],
        env={},
    )
    for parameters in (no_host_oauth, partial_oauth, unused_oauth):
        try:
            _configure_fresh_cache(parameters, tmp_path, None)
        except ValueError as exc:
            assert "must not set parity-managed" in str(exc)
        else:
            raise AssertionError("Docker credential value was accepted into evidence argv")

    unmanaged_ownership = SimpleNamespace(
        command="docker",
        args=["run", "--env=PUID=99", "image"],
        env={},
    )
    try:
        _configure_fresh_cache(unmanaged_ownership, tmp_path, None)
    except ValueError as exc:
        assert "must not set parity-managed" in str(exc)
    else:
        raise AssertionError("ambient Docker ownership override was accepted")


def test_docker_parity_cache_must_be_entrypoint_owned(tmp_path):
    parameters = SimpleNamespace(
        command="docker",
        args=["run", "image"],
        env={},
    )

    for target in (
        tmp_path,
        type(tmp_path)("/app/data"),
        type(tmp_path)("/app/data/../outside"),
    ):
        try:
            _configure_fresh_cache(parameters, tmp_path, target)
        except ValueError as exc:
            assert "child of /app/data" in str(exc)
        else:
            raise AssertionError(f"unsafe Docker cache target was accepted: {target}")


def test_cache_audit_requires_a_readable_owner_only_unexpired_reddit_cache(
    tmp_path,
):
    cache_file = tmp_path / "reddit.com.json"
    cache_file.write_text(
        json.dumps(
            [
                {
                    "name": "opaque-cookie-name",
                    "raw": "opaque-cookie-name=redacted",
                    "url": "https://www.reddit.com/",
                    "expires": 4_102_444_800,
                    "last_used": 1,
                }
            ]
        )
    )
    cache_file.chmod(0o600)

    evidence = _audit_reddit_cookie_cache(tmp_path, "after_warm")

    assert evidence.status == "passed"
    assert "redacted" not in evidence.detail
    assert "1 unexpired owner-only" in evidence.detail

    cache_file.chmod(0o644)
    assert _audit_reddit_cookie_cache(tmp_path, "after_warm").status == "failed"


def test_recreated_browser_audit_rejects_any_hidden_solver_egress(tmp_path):
    audit_log = tmp_path / "server.stderr.log"
    audit_log.write_text(
        "startup\nBROWSER_EGRESS_SUMMARY allowed=0 denied=0\n"
    )
    assert (
        _audit_browser_egress(
            audit_log,
            "recreated_shutdown",
            require_zero=True,
        ).status
        == "passed"
    )

    audit_log.write_text(
        "BROWSER_EGRESS_SUMMARY allowed=1 denied=0\n"
    )
    evidence = _audit_browser_egress(
        audit_log,
        "recreated_shutdown",
        require_zero=True,
    )
    assert evidence.status == "failed"
    assert "opened guarded-browser connections" in evidence.detail


def test_browser_dispatch_audit_requires_zero_reddit_solver_calls(tmp_path):
    audit_log = tmp_path / "server.stderr.log"
    audit_log.write_text(
        "BROWSER_DISPATCH_SUMMARY total=3 reddit=0\n"
    )
    passed = _audit_browser_dispatch(audit_log, "cold_warm_shutdown")
    assert passed.status == "passed"
    assert "zero Reddit" in passed.detail

    audit_log.write_text(
        "BROWSER_DISPATCH_SUMMARY total=3 reddit=1\n"
    )
    failed = _audit_browser_dispatch(audit_log, "cold_warm_shutdown")
    assert failed.status == "failed"
    assert "1 Reddit dispatches" in failed.detail


def test_recreated_reddit_audit_requires_hydration_without_http_verification(
    tmp_path,
):
    audit_log = tmp_path / "server.stderr.log"
    audit_log.write_text(
        "REDDIT_SESSION_AUDIT hydrated_anonymous=1 "
        "hydrated_cookie_count=4 bootstrap_instrumented=1 "
        "bootstrap_network_attempts=0\n"
    )
    evidence = _audit_reddit_session(
        audit_log,
        "recreated_shutdown",
        expect_hydrated=True,
        require_no_bootstrap=True,
    )
    assert evidence.status == "passed"
    assert "hydrated_cookie_count=4" in evidence.detail

    audit_log.write_text(
        "REDDIT_SESSION_AUDIT hydrated_anonymous=1 "
        "hydrated_cookie_count=4 bootstrap_instrumented=1 "
        "bootstrap_network_attempts=1\n"
    )
    evidence = _audit_reddit_session(
        audit_log,
        "recreated_shutdown",
        expect_hydrated=True,
        require_no_bootstrap=True,
    )
    assert evidence.status == "failed"
    assert "reran pure-HTTP verification" in evidence.detail

    audit_log.write_text(
        "REDDIT_SESSION_AUDIT hydrated_anonymous=1 "
        "hydrated_cookie_count=4 bootstrap_instrumented=0 "
        "bootstrap_network_attempts=0\n"
    )
    evidence = _audit_reddit_session(
        audit_log,
        "recreated_shutdown",
        expect_hydrated=True,
        require_no_bootstrap=True,
    )
    assert evidence.status == "failed"
    assert "counter was not instrumented" in evidence.detail


def test_docker_parity_runner_forwards_oauth_by_name_not_secret_value(monkeypatch, tmp_path):
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "secret-must-never-appear")
    # The production corpus has no OAuth-gated route any more, but the
    # forwarding rule must still hold for any corpus that does: the credential
    # travels to the container by variable name, never by value.
    entries = [
        CorpusEntry(
            id="oauth_route",
            live="oauth_required",
            kind="moderators",
            url="https://www.reddit.com/r/Python/about/moderators/",
            required_any=("Source: exact Reddit OAuth",),
            oauth_scopes=("read",),
        )
    ]
    mode, names = _oauth_forwarding(entries)
    parameters = SimpleNamespace(command="docker", args=["run", "image"], env={})

    _configure_fresh_cache(parameters, tmp_path, None, names)

    assert mode == "direct_token"
    assert names == ("REDDIT_ACCESS_TOKEN",)
    assert "REDDIT_ACCESS_TOKEN" in parameters.args
    assert all("secret-must-never-appear" not in argument for argument in parameters.args)


def test_direct_parity_runner_strips_partial_or_unused_oauth(monkeypatch, tmp_path):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "partial-client")
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_REFRESH_TOKEN", raising=False)
    parameters = SimpleNamespace(
        command="/usr/bin/python3",
        args=["-m", "fetchaller.main"],
        env={
            "PATH": "/usr/bin",
            "REDDIT_CLIENT_ID": "partial-client",
            "REDDIT_ACCESS_TOKEN": "unused-token",
        },
    )

    mapping = _configure_fresh_cache(parameters, tmp_path, None)

    assert mapping["mode"] == "direct_env"
    assert parameters.env["PATH"] == "/usr/bin"
    assert parameters.env["WAFER_CACHE_DIR"] == str(tmp_path)
    assert not set(_REDDIT_CREDENTIAL_ENV).intersection(parameters.env)


class _Session:
    def __init__(self, *texts: str):
        self._texts = iter(texts)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=next(self._texts))],
        )


async def test_parity_runner_accepts_only_exact_declared_access_state_error(
    tmp_path,
):
    entry = CorpusEntry(
        id="user_upvoted_private",
        live="stable",
        kind="user_listing",
        url="https://www.reddit.com/user/spez/upvoted/",
        access_state="private",
        expect_error=True,
        required_any=("account-private activity",),
    )

    class ErrorSession:
        def __init__(self, is_error, text):
            self.is_error = is_error
            self.text = text

        async def call_tool(self, _name, _arguments):
            return SimpleNamespace(
                isError=self.is_error,
                content=[SimpleNamespace(text=self.text)],
            )

    passed = await _call(
        ErrorSession(
            True,
            "Error: Reddit account-private activity is not publicly readable.",
        ),
        entry,
        tmp_path,
        "cold",
    )
    wrong_marker = await _call(
        ErrorSession(True, "Error: unrelated failure"),
        entry,
        tmp_path,
        "warm",
    )
    false_success = await _call(
        ErrorSession(False, "Reddit account-private activity"),
        entry,
        tmp_path,
        "recreated",
    )

    assert passed.status == "passed"
    assert wrong_marker.status == "failed"
    assert false_success.status == "failed"
    for stage, suffix in (
        ("challenge", "\nPlease wait for verification"),
        ("login", "\n# Log in"),
        ("truncated", "\n[Truncated at ~100 tokens]"),
        ("partial", "\n[Unavailable: transport failed]"),
    ):
        mutated = await _call(
            ErrorSession(
                True,
                "Error: Reddit account-private activity is not publicly "
                f"readable.{suffix}",
            ),
            entry,
            tmp_path,
            stage,
        )
        assert mutated.status == "failed", stage

    wrong_state = await _call(
        ErrorSession(
            True,
            "Error: Reddit account-private activity is not publicly readable.",
        ),
        replace(entry, access_state="forbidden"),
        tmp_path,
        "wrong-state",
    )
    assert wrong_state.status == "failed"
    assert "exact contract" in wrong_state.detail


async def test_dynamic_discovery_materializes_every_opaque_public_route(tmp_path):
    post = {
        "kind": "t3",
        "data": {
            "id": "post123",
            "name": "t3_post123",
            "title": "Current post",
            "permalink": "/r/AskReddit/comments/post123/current_post/",
            "num_comments": 100,
        },
    }
    thread = [
        {"kind": "Listing", "data": {"children": [post]}},
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "comment0",
                            "name": "t1_comment0",
                            "body": "Current exact comment",
                        },
                    },
                    {
                        "kind": "more",
                        "data": {
                            "id": "more123",
                            "children": ["comment1", "comment2"],
                            "count": 2,
                        },
                    }
                ]
            },
        },
    ]
    link_post = {
        "kind": "t3",
        "data": {
            "id": "link123",
            "name": "t3_link123",
            "title": "Current link post",
            "subreddit": "worldnews",
            "permalink": (
                "/r/worldnews/comments/link123/current_link_post/"
            ),
            "is_self": False,
            "num_comments": 200,
        },
    }
    gallery_post = {
        "kind": "t3",
        "data": {
            "id": "gallery1",
            "name": "t3_gallery1",
            "title": "Current gallery",
            "is_gallery": True,
            "gallery_data": {
                "items": [
                    {"media_id": "media1"},
                    {"media_id": "media2"},
                ],
            },
        },
    }
    revision_one = "0136a1c0-57c7-11f1-b49a-ae675b7b52c3"
    revision_two = "12345678-1234-1234-1234-123456789abc"
    session = _Session(
        json.dumps({"kind": "Listing", "data": {"children": [post]}}),
        json.dumps(thread),
        json.dumps({
            "kind": "Listing",
            "data": {"children": [link_post]},
        }),
        # Discovery now probes a link candidate for real crossposts before
        # building the duplicates targets: a post with none renders a correct
        # "0 items returned" that the gate scored as a product failure. This
        # first candidate has them, so exactly one probe is consumed.
        "## Other discussions\n\n2 items returned\n",
        json.dumps({
            "kind": "Listing",
            "data": {"children": [gallery_post]},
        }),
        f"revision `{revision_one}`\nrevision `{revision_two}`",
        "https://www.reddit.com/user/the-magic-sword/m/pathfinderstarfinder_2e/",
        (
            "https://www.reddit.com/live/18hnzysb1elcs/updates/"
            "ecf7aa3e-5567-11f1-87f8-660b88d038df"
        ),
        (
            "# Deprecating Post Collections, Mark as OC, and Community "
            "Content Tags\n\nseveral mod-oriented features will be removed "
            "next month\n\nhttps://www.reddit.com/r/YUROP/collection/"
            "36910c41-231f-45ea-8057-a4e061048541"
        ),
    )

    targets, records = await _discover_live_targets(session, tmp_path)

    assert len(targets) == 18
    assert all(record.status == "passed" for record in records)
    assert {
        record.id: record.stage for record in records
    } == {
        discovery_id: "discovery"
        for discovery_id in _REQUIRED_DISCOVERY_STAGES
    }
    assert targets["thread"] == (
        "https://www.reddit.com/r/AskReddit/comments/post123/"
    )
    assert targets["thread_global_permalink"] == (
        "https://www.reddit.com/comments/post123/current_post/?limit=2"
    )
    assert targets["thread_gallery"] == (
        "https://www.reddit.com/gallery/gallery1"
        "?limit=2&depth=1&sort=new"
    )
    assert targets["listing_by_id_comment"].endswith(
        "/by_id/t1_comment0/"
    )
    assert targets["listing_by_id_mixed"].endswith(
        "/by_id/t1_comment0,t3_post123/"
    )
    assert targets["duplicates_subreddit"].startswith(
        "https://www.reddit.com/r/worldnews/duplicates/link123/"
    )
    assert targets["related_subreddit"].startswith(
        "https://www.reddit.com/r/worldnews/related/link123/"
    )
    assert "children=comment1%2Ccomment2" in targets["morechildren"]
    assert targets["multi_about"].endswith(
        "/m/pathfinderstarfinder_2e/about/"
    )
    assert targets["live_update"].endswith(
        "/updates/ecf7aa3e-5567-11f1-87f8-660b88d038df/"
    )
    assert targets["collection"].startswith("https://www.reddit.com/")
    entries = {
        entry.id: entry for entry in load_corpus(DEFAULT_CORPUS)
    }
    for entry_id in (
        "thread",
        "thread_global_permalink",
        "thread_gallery",
        "listing_by_id_comment",
        "listing_by_id_mixed",
        "wiki_diff",
        "multi_about",
        "multi_profile",
        "duplicates",
        "duplicates_subreddit",
        "related",
        "related_subreddit",
        "morechildren",
        "live",
        "live_about",
        "live_contributors",
        "live_update",
        "collection",
    ):
        assert _materialize_entry(entries[entry_id], targets) is not None


async def test_parity_runner_rejects_a_semantically_wrong_json_response(tmp_path):
    entry = CorpusEntry(
        id="json",
        live="stable",
        kind="explicit_json",
        url="https://www.reddit.com/r/Python/hot.json?limit=1",
        required_any=('"kind"',),
    )

    evidence = await _call(_Session('{"data": []}'), entry, tmp_path, "cold")

    assert evidence.status == "failed"
    assert "semantic marker" in evidence.detail
    assert (tmp_path / "cold-json.txt").read_text() == '{"data": []}'


async def test_parity_runner_rejects_garbage_explicit_json_with_a_kind_key(tmp_path):
    entry = CorpusEntry(
        id="json",
        live="stable",
        kind="explicit_json",
        url="https://www.reddit.com/r/Python/hot.json?limit=1",
        required_any=('"kind"',),
    )

    evidence = await _call(_Session('{"kind":"garbage"}'), entry, tmp_path, "cold")

    assert evidence.status == "failed"
    assert "nonempty coherent Reddit post listing" in evidence.detail


async def test_parity_runner_requires_a_real_post_in_explicit_json(tmp_path):
    entry = CorpusEntry(
        id="json",
        live="stable",
        kind="explicit_json",
        url="https://www.reddit.com/r/Python/hot.json?limit=1",
        required_any=('"kind"',),
    )
    valid = json.dumps(
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "post123",
                            "name": "t3_post123",
                            "title": "A current post",
                            "subreddit": "Python",
                            "permalink": (
                                "/r/Python/comments/post123/"
                                "a_current_post/"
                            ),
                        },
                    }
                ]
            },
        }
    )

    evidence = await _call(_Session(valid), entry, tmp_path, "cold")
    assert evidence.status == "passed"

    for shell in (
        '{"kind":"Listing","data":{"children":[]}}',
        '{"kind":"Listing","data":{"children":[{}]}}',
    ):
        evidence = await _call(_Session(shell), entry, tmp_path, "cold")
        assert evidence.status == "failed"
        assert "nonempty coherent Reddit post listing" in evidence.detail


async def test_parity_runner_requires_post_link_in_known_nonempty_listing(
    tmp_path,
):
    entry = CorpusEntry(
        id="listing",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/",
        required_any=("r/Python",),
    )

    evidence = await _call(
        _Session("# r/Python · hot\n\n0 items returned"),
        entry,
        tmp_path,
        "cold",
    )

    assert evidence.status == "failed"
    assert "known-nonempty listing" in evidence.detail


async def test_parity_runner_rejects_classic_reddit_block_as_raw_html(tmp_path):
    entry = CorpusEntry(
        id="raw",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/",
        raw=True,
        required_any=("<html",),
    )

    evidence = await _call(
        _Session("<html><body>whoa there, pardner!</body></html>"),
        entry,
        tmp_path,
        "cold",
    )

    assert evidence.status == "failed"
    assert "semantic New Reddit" in evidence.detail


def test_raw_new_reddit_requires_a_substantive_python_post():
    post = (
        '<shreddit-post id="t3_abc123" '
        'post-title="Current Python post" '
        'permalink="/r/Python/comments/abc123/current_python_post/">'
        "</shreddit-post>"
    )
    empty_shell = (
        "<html><shreddit-app><shreddit-feed></shreddit-feed>"
        "</shreddit-app></html>"
    )
    substantive = (
        "<html><shreddit-app><shreddit-feed>"
        f"{post}</shreddit-feed></shreddit-app></html>"
    )

    assert not _valid_raw_new_reddit_html(empty_shell)
    assert _valid_raw_new_reddit_html(substantive)
    assert not _valid_raw_new_reddit_html(
        f"<html><shreddit-app><shreddit-feed></shreddit-feed>"
        f"{post}</shreddit-app></html>"
    )
    assert not _valid_raw_new_reddit_html(
        f"<html><shreddit-app><shreddit-feed></shreddit-feed>"
        f"</shreddit-app>{post}</html>"
    )
    assert not _valid_raw_new_reddit_html(
        substantive.replace("/r/Python/", "/r/Rust/")
    )
    assert not _valid_raw_new_reddit_html(
        substantive.replace('post-title="Current Python post" ', "")
    )


def test_every_counted_output_family_rejects_inflated_counts_and_missing_cards():
    post = (
        "1 items returned\n\n"
        "1. Current post\n"
        "   r/Python · 7 score · 3 comments · u/alice\n"
        "   https://www.reddit.com/r/Python/comments/post01/current_post/"
    )
    activity_comment = (
        "1 items returned\n\n"
        "1. **Current thread**\n"
        "   r/Python · u/alice · 7 score · 2023-11-14 22:13 UTC\n\n"
        "Current comment body.\n\n"
        "Permalink: https://www.reddit.com/r/Python/comments/"
        "post01/current_thread/comment01/\n"
        "Parent context: https://www.reddit.com/comments/post01/"
    )
    nested_comment = (
        "1 comments returned\n\n"
        "### u/alice · 7 score · 2023-11-14 22:13 UTC\n\n"
        "Current comment body.\n\n"
        "Permalink: https://www.reddit.com/r/Python/comments/"
        "post01/current_thread/comment01/\n"
        "Parent context: https://www.reddit.com/comments/post01/"
    )
    community = (
        "1 items returned\n\n"
        "1. **r/Python** — Python\n"
        "   1,000 subscribers\n"
        "   Created: 2008-01-01 00:00 UTC\n"
        "   Public\n"
        "   https://www.reddit.com/r/Python/"
    )
    user = (
        "1 users returned\n\n"
        "1. **u/alice** · 10 post karma · 20 comment karma\n"
        "   Created: 2020-01-01 00:00 UTC\n"
        "   https://www.reddit.com/user/alice/\n"
        "   Public activity: https://www.reddit.com/user/alice/overview/"
    )
    moderator = (
        "1 moderators returned\n\n"
        "1. **u/alice** · all\n"
        "   https://www.reddit.com/user/alice/"
    )
    wiki_page = (
        "1 pages returned\n\n"
        "1. [index](https://www.reddit.com/r/Python/wiki/index)"
    )
    wiki_revision = (
        "1 revisions returned\n\n"
        "1. **index** · 2023-11-14 22:13 UTC · u/alice\n"
        "   revision: 12345678-1234-1234-1234-123456789abc"
    )
    trophy = "1 trophies returned\n\n1. **Verified Email**"
    multi_community = (
        "1 communities returned\n\n"
        "1. **r/Python**\n"
        "   https://www.reddit.com/r/Python/"
    )
    live_contributor = (
        "1 contributors returned\n\n"
        "1. **u/alice**\n"
        "   https://www.reddit.com/user/alice/"
    )
    live_update = (
        "1 updates returned\n\n"
        "1. **u/alice · 2023-11-14 22:13 UTC**\n\n"
        "Current update body.\n\n"
        "Update ID: 12345678-1234-1234-1234-123456789abc"
    )
    mixed_by_id = (
        "2 items returned\n\n"
        + activity_comment.replace("1 items returned\n\n", "")
        + "\n\n2. Current post\n"
        "   r/Python · 7 score · 3 comments · u/bob\n"
        "   https://www.reddit.com/r/Python/comments/post02/current_post/"
    )
    tool_post = (
        "r/Python · new · 1 posts\n"
        "1. Current post\n"
        "   r/Python · 7 score · 3 comments · u/alice\n"
        "   https://www.reddit.com/r/Python/comments/post01/current_post/"
    )

    cases = (
        ("listing", post, "1 items returned", "2 items returned", "post01"),
        (
            "comment_listing",
            activity_comment,
            "1 items returned",
            "2 items returned",
            "comment01",
        ),
        (
            "morechildren",
            nested_comment,
            "1 comments returned",
            "2 comments returned",
            "comment01",
        ),
        (
            "subreddit_directory",
            community,
            "1 items returned",
            "2 items returned",
            "/r/Python/",
        ),
        (
            "user_directory",
            user,
            "1 users returned",
            "2 users returned",
            "/user/alice/",
        ),
        (
            "moderators",
            moderator,
            "1 moderators returned",
            "2 moderators returned",
            "/user/alice/",
        ),
        (
            "wiki_pages",
            wiki_page,
            "1 pages returned",
            "2 pages returned",
            "/wiki/index",
        ),
        (
            "wiki_revisions",
            wiki_revision,
            "1 revisions returned",
            "2 revisions returned",
            "12345678-1234-1234-1234-123456789abc",
        ),
        (
            "trophies",
            trophy,
            "1 trophies returned",
            "2 trophies returned",
            "Verified Email",
        ),
        (
            "multi_about",
            multi_community,
            "1 communities returned",
            "2 communities returned",
            "/r/Python/",
        ),
        (
            "multi_profile",
            f"## Feed\n\n{post}\n\n## Communities",
            "1 items returned",
            "2 items returned",
            "post01",
        ),
        (
            "duplicates",
            f"## Other discussions\n\n{post}",
            "1 items returned",
            "2 items returned",
            "post01",
        ),
        (
            "live_contributors",
            live_contributor,
            "1 contributors returned",
            "2 contributors returned",
            "/user/alice/",
        ),
        (
            "live_update",
            live_update,
            "1 updates returned",
            "2 updates returned",
            "12345678-1234-1234-1234-123456789abc",
        ),
        (
            "listing_by_id_comment",
            activity_comment,
            "1 items returned",
            "2 items returned",
            "comment01",
        ),
        (
            "listing_by_id_mixed",
            mixed_by_id,
            "2 items returned",
            "3 items returned",
            "post02",
        ),
        (
            "browse_page_1",
            tool_post,
            "1 posts",
            "2 posts",
            "post01",
        ),
        (
            "gilded_global",
            activity_comment,
            "1 items returned",
            "2 items returned",
            "comment01",
        ),
        (
            "user_overview",
            activity_comment,
            "1 items returned",
            "2 items returned",
            "comment01",
        ),
    )
    for entry_id, text, count, inflated_count, identity in cases:
        entry = CorpusEntry(id=entry_id, live="stable")
        assert _counted_output_error(entry, text) is None, entry_id
        assert (
            _counted_output_error(entry, text.replace(count, inflated_count))
            is not None
        ), entry_id
        assert (
            _counted_output_error(entry, text.replace(identity, ""))
            is not None
        ), entry_id
    assert (
        _counted_output_error(
            CorpusEntry(id="listing", live="stable"),
            post.replace("u/alice", "u/[deleted]"),
        )
        is None
    )
    # Reddit awards the same trophy more than once, and the awards are really
    # distinct: redtaboo holds two "Beta Team" trophies whose descriptions and
    # links differ ("Ask Me Anything app" vs "IndexTank Search"), and two
    # "RedditGifts 2009-2022" that differ only by internal id. Deduplicating by
    # name would force a faithful trophy case to drop real awards, so a
    # repeated name must be accepted as long as the count still matches.
    assert _counted_output_error(
        CorpusEntry(id="trophies", live="stable"),
        (
            "2 trophies returned\n\n"
            "1. **Beta Team**\n"
            "   Ask Me Anything app\n\n"
            "2. **Beta Team**\n"
            "   IndexTank Search"
        ),
    ) is None
    # An inflated count with a missing card is still a failure.
    assert _counted_output_error(
        CorpusEntry(id="trophies", live="stable"),
        "3 trophies returned\n\n1. **Beta Team**\n\n2. **Beta Team**",
    ) is not None


async def test_parity_runner_rejects_redirected_reddit_login_welcome_shell(
    tmp_path,
):
    entry = CorpusEntry(
        id="html_fallback",
        live="unstable",
        kind="html_fallback",
        url="https://www.reddit.com/premium/",
        required_any=("# Bringing you the best of Reddit!",),
    )
    login_shell = (
        "[Redirected to: https://www.reddit.com/login/?dest="
        "https%3A%2F%2Fwww.reddit.com%2Fpremium%2F]\n\n"
        "# Welcome to Reddit\n"
    )

    evidence = await _call(
        _Session(login_shell),
        entry,
        tmp_path,
        "cold",
    )

    assert evidence.status == "failed"
    assert "login page response" in evidence.detail
    assert (tmp_path / "cold-html_fallback.txt").read_text() == login_shell


async def test_public_html_fallback_requires_multiple_substantive_markers(
    tmp_path,
):
    entry = CorpusEntry(
        id="html_fallback",
        live="unstable",
        kind="html_fallback",
        url="https://www.reddit.com/premium/",
        required_any=("# Bringing you the best of Reddit!",),
    )
    premium = (
        "# Bringing you the best of Reddit!\n\n"
        "Ads-free browsing\n\nHigher Rate Limits\n"
    )

    assert (
        await _call(_Session(premium), entry, tmp_path, "cold")
    ).status == "passed"

    weak_shell = "# Bringing you the best of Reddit!\n"
    evidence = await _call(
        _Session(weak_shell),
        entry,
        tmp_path,
        "warm",
    )
    assert evidence.status == "failed"
    assert "substantive public Reddit Premium" in evidence.detail


async def test_collection_gate_requires_archive_provenance_and_current_posts(
    tmp_path,
):
    entry = CorpusEntry(
        id="collection",
        live="unstable",
        kind="collection",
        url=(
            "https://www.reddit.com/r/YUROP/collection/"
            "36910c41-231f-45ea-8057-a4e061048541/"
        ),
        required_any=(
            "Metadata source: archived New Reddit snapshot",
            "Post details: current Reddit API.",
            "## Posts",
        ),
    )
    posts = "\n\n".join(
        (
            f"{index}. Current post {index}\n"
            f"   score {index} · 90% upvoted · {index} comments · "
            "u/alice · 1y\n"
            f"   https://www.reddit.com/r/YUROP/comments/post{index}/"
        )
        for index in range(1, 29)
    )
    valid = (
        "# Preserved collection\n\n"
        "Metadata source: archived New Reddit snapshot "
        "(Wayback, 2023-02-06). Post details: current Reddit API.\n\n"
        "## Posts\n\n28 items returned\n\n"
        f"{posts}\n"
    )
    assert (
        await _call(_Session(valid), entry, tmp_path, "cold")
    ).status == "passed"

    url_only_shells = "\n".join(
        (
            f"https://www.reddit.com/r/YUROP/comments/post{index}/"
            if index > 1
            else (
                "1. Only substantive post\n"
                "   score 1 · 90% upvoted · 1 comments · u/alice · 1y\n"
                "   https://www.reddit.com/r/YUROP/comments/post1/"
            )
        )
        for index in range(1, 29)
    )
    shallow_hydration = (
        "# Preserved collection\n\n"
        "Metadata source: archived New Reddit snapshot "
        "(Wayback, 2023-02-06). Post details: current Reddit API.\n\n"
        "## Posts\n\n28 items returned\n\n"
        f"{url_only_shells}\n"
    )
    evidence = await _call(
        _Session(shallow_hydration),
        entry,
        tmp_path,
        "shallow",
    )
    assert evidence.status == "failed"
    assert "28 substantive current post cards" in evidence.detail

    for stage, invalid in (
        (
            "warm",
            "# Preserved collection\n\n## Posts\n\n"
            "1 items returned\n\n"
            "1. [Post](https://www.reddit.com/r/YUROP/comments/post1/)\n",
        ),
        (
            "recreated",
            "# Preserved collection\n\n"
            "Metadata source: archived New Reddit snapshot "
            "(Wayback, 2023-02-06). Post details: current Reddit API.\n\n"
            "## Posts\n\n0 items returned\n",
        ),
    ):
        evidence = await _call(
            _Session(invalid),
            entry,
            tmp_path,
            stage,
        )
        assert evidence.status == "failed"


async def test_parity_runner_uses_large_bounded_raw_budget_and_rejects_truncation(
    tmp_path,
):
    entry = CorpusEntry(
        id="raw",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/",
        raw=True,
        required_any=("<html",),
    )
    session = _Session(
        "<html><shreddit-app><shreddit-feed></shreddit-feed></shreddit-app>"
        "[Truncated at ~25000 tokens]"
    )

    evidence = await _call(session, entry, tmp_path, "cold")

    assert evidence.status == "failed"
    assert "truncation marker" in evidence.detail
    assert session.calls == [
        (
            "fetch",
            {
                "url": "https://www.reddit.com/r/Python/",
                "raw": True,
                "timeout": 90,
                "maxTokens": 250_000,
            },
        )
    ]


async def test_parity_runner_does_not_count_anonymous_wiki_ssr_as_oauth_evidence(tmp_path):
    entry = CorpusEntry(
        id="wiki_pages",
        live="oauth_required",
        kind="wiki_pages",
        url="https://www.reddit.com/r/Python/wiki/pages/",
        required_any=("Source: exact Reddit OAuth",),
        oauth_scopes=("wikiread",),
    )

    evidence = await _call(
        _Session("# Wiki pages for r/Python\n\n2 pages returned"),
        entry,
        tmp_path,
        "cold",
    )

    assert evidence.status == "failed"
    assert "semantic marker" in evidence.detail


async def test_parity_runner_records_and_rejects_a_blocked_response(tmp_path):
    entry = CorpusEntry(
        id="listing",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/",
        required_any=("r/Python",),
    )

    evidence = await _call(
        _Session("r/Python\n\nPlease wait for verification"), entry, tmp_path, "cold"
    )

    assert evidence.status == "failed"
    assert "blocked/challenge" in evidence.detail
    assert (tmp_path / "cold-listing.txt").exists()


async def test_parity_runner_rejects_named_composite_partial_failure(tmp_path):
    entry = CorpusEntry(
        id="profile",
        live="stable",
        kind="user_profile",
        url="https://www.reddit.com/user/spez/",
        required_any=("u/spez",),
    )

    evidence = await _call(
        _Session(
            "# u/spez\n\n"
            "## Moderated communities\n\n"
            "[Unavailable: Reddit returned an invalid moderated communities "
            "response.]"
        ),
        entry,
        tmp_path,
        "cold",
    )

    assert evidence.status == "failed"
    assert "partial failure" in evidence.detail


async def test_parity_runner_rejects_morechildren_api_failure_mcp_error(
    tmp_path,
):
    entry = CorpusEntry(
        id="morechildren",
        live="fixture_only",
        kind="morechildren",
        url=(
            "https://www.reddit.com/api/morechildren?"
            "link_id=t3_abc123&children=def456"
        ),
        required_any=("Comment",),
    )

    class ErrorSession:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(
                isError=True,
                content=[
                    SimpleNamespace(
                        text=(
                            "Error: Reddit reported that comment expansion "
                            "failed."
                        )
                    )
                ],
            )

    evidence = await _call(ErrorSession(), entry, tmp_path, "offline")

    assert evidence.status == "failed"
    assert evidence.detail == "MCP isError or empty content"
    assert "comment expansion failed" in (
        tmp_path / "offline-morechildren.txt"
    ).read_text()


async def test_parity_runner_follows_validated_pagination_cursor(tmp_path):
    entry = CorpusEntry(
        id="browse",
        live="stable",
        tool="browse_reddit",
        arguments={"subreddit": "Python", "limit": 1},
        pagination=True,
        required_any=("r/Python",),
    )
    session = _Session(
        "r/Python · new · 1 posts\nhttps://www.reddit.com/r/Python/comments/abc123/\n\n[Next page: after=t3_abc123]",
        "r/Python · new · 1 posts\nhttps://www.reddit.com/r/Python/comments/def456/\n\n[Next page: after=t3_def456]",
    )

    evidence = await _call(session, entry, tmp_path, "warm")

    assert evidence.status == "passed"
    assert session.calls[1] == (
        "browse_reddit",
        {"subreddit": "Python", "limit": 1, "after": "t3_abc123"},
    )
    assert (tmp_path / "warm-browse.txt").read_text().startswith("r/Python")
    assert (tmp_path / "warm-browse-after.txt").read_text().startswith("r/Python")


async def test_parity_runner_rejects_repeated_pagination_page_and_cursor(tmp_path):
    entry = CorpusEntry(
        id="browse",
        live="stable",
        tool="browse_reddit",
        arguments={"subreddit": "Python", "limit": 1},
        pagination=True,
        required_any=("r/Python",),
    )
    page = (
        "r/Python · new · 1 posts\n"
        "https://www.reddit.com/r/Python/comments/abc123/\n\n"
        "[Next page: after=t3_abc123]"
    )

    evidence = await _call(_Session(page, page), entry, tmp_path, "warm")

    assert evidence.status == "failed"
    assert "repeated the first page" in evidence.detail


async def test_tool_pagination_rejects_ambiguous_multiple_next_cursors(tmp_path):
    entry = CorpusEntry(
        id="browse",
        live="stable",
        tool="browse_reddit",
        arguments={"subreddit": "Python", "limit": 1},
        pagination=True,
        required_any=("r/Python",),
    )
    ambiguous = (
        "r/Python · new · 1 posts\n"
        "https://www.reddit.com/r/Python/comments/abc123/\n\n"
        "[Next page: after=t3_abc123]\n"
        "[Next page: after=t3_def456]"
    )

    evidence = await _call(
        _Session(ambiguous, ambiguous),
        entry,
        tmp_path,
        "warm",
    )

    assert evidence.status == "failed"
    assert evidence.detail == "missing validated pagination cursor"


async def test_tool_pagination_rejects_ambiguous_page_two_cursors(tmp_path):
    entry = CorpusEntry(
        id="browse",
        live="stable",
        tool="browse_reddit",
        arguments={"subreddit": "Python", "limit": 1},
        pagination=True,
        required_any=("r/Python",),
    )
    first = (
        "r/Python · new · 1 posts\n"
        "https://www.reddit.com/r/Python/comments/abc123/\n\n"
        "[Next page: after=t3_abc123]"
    )
    ambiguous_second = (
        "r/Python · new · 1 posts\n"
        "https://www.reddit.com/r/Python/comments/def456/\n\n"
        "[Next page: after=t3_def456]\n"
        "[Next page: after=t3_ghi789]"
    )

    evidence = await _call(
        _Session(first, ambiguous_second),
        entry,
        tmp_path,
        "warm",
    )

    assert evidence.status == "failed"
    assert "did not advance to a new cursor" in evidence.detail


async def test_parity_runner_follows_mapped_fetch_full_next_page_url(tmp_path):
    entry = CorpusEntry(
        id="mapped_fetch",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/new/?limit=1",
        pagination=True,
        required_any=("# r/Python · new",),
    )
    first = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. First post\n"
        "   r/Python · 1 score · 2 comments · u/alice\n"
        "   https://www.reddit.com/r/Python/comments/abc123/first/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_abc123&count=1]"
    )
    second = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Second post\n"
        "   r/Python · 2 score · 3 comments · u/bob\n"
        "   https://www.reddit.com/r/Python/comments/def456/second/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_def456&count=2]"
    )
    session = _Session(first, second)

    evidence = await _call(session, entry, tmp_path, "warm")

    assert evidence.status == "passed"
    assert session.calls[1] == (
        "fetch",
        {
            "url": (
                "https://www.reddit.com/r/Python/new/"
                "?limit=1&after=t3_abc123&count=1"
            ),
            "raw": False,
            "timeout": 90,
        },
    )
    assert evidence.followup_artifact == "warm-mapped_fetch-after.txt"


async def test_parity_runner_proves_mapped_fetch_four_leg_round_trip(
    tmp_path,
):
    entry = CorpusEntry(
        id="mapped_fetch",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/new/?limit=1",
        pagination=True,
        pagination_round_trip=True,
        required_any=("# r/Python · new",),
    )
    first = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. First post\n"
        "   https://www.reddit.com/r/Python/comments/abc123/first/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_abc123&count=1]"
    )
    second = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Second post\n"
        "   https://www.reddit.com/r/Python/comments/def456/second/\n\n"
        "[Previous page: https://www.reddit.com/r/Python/new/"
        "?limit=1&before=t3_def456&count=0]\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_def456&count=2]"
    )
    session = _Session(first, second, first, second)

    evidence = await _call(session, entry, tmp_path, "warm")

    assert evidence.status == "passed"
    assert [call[1]["url"] for call in session.calls] == [
        "https://www.reddit.com/r/Python/new/?limit=1",
        (
            "https://www.reddit.com/r/Python/new/"
            "?limit=1&after=t3_abc123&count=1"
        ),
        (
            "https://www.reddit.com/r/Python/new/"
            "?limit=1&before=t3_def456&count=0"
        ),
        (
            "https://www.reddit.com/r/Python/new/"
            "?limit=1&after=t3_abc123&count=1"
        ),
    ]
    assert (
        tmp_path / "warm-mapped_fetch-previous.txt"
    ).read_text() == first
    assert (
        tmp_path / "warm-mapped_fetch-round-trip-after.txt"
    ).read_text() == second


async def test_mapped_fetch_page_two_rejects_item_overlap(tmp_path):
    entry = CorpusEntry(
        id="mapped_fetch",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/new/?limit=1",
        pagination=True,
        required_any=("# r/Python · new",),
    )
    first = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. First rendering\n"
        "   https://www.reddit.com/r/Python/comments/abc123/first/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_abc123&count=1]"
    )
    second = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Changed rendering of same item\n"
        "   https://www.reddit.com/r/Python/comments/abc123/first/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_def456&count=2]"
    )

    evidence = await _call(
        _Session(first, second),
        entry,
        tmp_path,
        "warm",
    )

    assert evidence.status == "failed"
    assert "repeated one or more first-page items" in evidence.detail


def test_fetch_next_page_requires_one_cursor_count_and_exact_route_scope():
    expected = (
        "https://www.reddit.com/r/Python/search/"
        "?q=asyncio&sort=new&t=all&type=link&limit=5"
    )
    valid = (
        "[Next page: https://www.reddit.com/r/Python/search/"
        "?q=asyncio&sort=new&t=all&type=link&limit=5"
        "&after=t3_abc123&count=5]"
    )

    assert _validated_fetch_next_page(valid, expected) == (
        "https://www.reddit.com/r/Python/search/"
        "?q=asyncio&sort=new&t=all&type=link&limit=5"
        "&after=t3_abc123&count=5",
        "t3_abc123",
        5,
    )
    for invalid in (
        valid.replace("&after=t3_abc123", "&after=t3_abc123&after=t3_def456"),
        valid.replace("&count=5", ""),
        valid.replace("&count=5", "&count=0"),
        valid.replace("/r/Python/search/", "/r/Python/new/"),
        valid.replace("q=asyncio", "q=different"),
        valid.replace("]", "#fragment]"),
        valid.replace("/search/", "/search/;session=x"),
    ):
        assert _validated_fetch_next_page(invalid, expected) is None


def test_fetch_previous_page_requires_one_cursor_count_and_exact_route_scope():
    expected = (
        "https://www.reddit.com/r/Python/search/"
        "?q=asyncio&sort=new&t=all&type=link&limit=5"
    )
    valid = (
        "[Previous page: https://www.reddit.com/r/Python/search/"
        "?q=asyncio&sort=new&t=all&type=link&limit=5"
        "&before=t3_abc123&count=0]"
    )

    assert _validated_fetch_previous_page(valid, expected) == (
        "https://www.reddit.com/r/Python/search/"
        "?q=asyncio&sort=new&t=all&type=link&limit=5"
        "&before=t3_abc123&count=0",
        "t3_abc123",
        0,
    )
    for invalid in (
        valid.replace(
            "&before=t3_abc123",
            "&before=t3_abc123&before=t3_def456",
        ),
        valid.replace("&count=0", ""),
        valid.replace("&count=0", "&count=1001"),
        valid.replace("/r/Python/search/", "/r/Python/new/"),
        valid.replace("q=asyncio", "q=different"),
        valid.replace("]", "#fragment]"),
        valid.replace("/search/", "/search/;session=x"),
    ):
        assert _validated_fetch_previous_page(invalid, expected) is None


async def test_mapped_fetch_page_two_must_pass_full_semantic_contract(tmp_path):
    entry = CorpusEntry(
        id="listing_subreddit_new",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/new/?limit=1",
        pagination=True,
        required_any=("# r/Python · new",),
    )
    first = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Complete first post\n"
        "   r/Python · 2 score · 3 comments · u/alice\n"
        "   https://www.reddit.com/r/Python/comments/abc123/first/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_abc123&count=1]"
    )
    second = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Thin second post\n"
        "   https://www.reddit.com/r/Python/comments/def456/second/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_def456&count=2]"
    )

    evidence = await _call(
        _Session(first, second),
        entry,
        tmp_path,
        "warm",
    )

    assert evidence.status == "failed"
    assert "pagination follow-up failed semantics" in evidence.detail
    assert "complete post metadata" in evidence.detail


async def test_pagination_page_two_rejects_truncation_and_partial_failure(
    tmp_path,
):
    entry = CorpusEntry(
        id="listing_subreddit_new",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/new/?limit=1",
        pagination=True,
        required_any=("# r/Python · new",),
    )
    first = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Complete first post\n"
        "   r/Python · 2 score · 3 comments · u/alice\n"
        "   https://www.reddit.com/r/Python/comments/abc123/first/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_abc123&count=1]"
    )
    second = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Complete second post\n"
        "   r/Python · 4 score · 5 comments · u/bob\n"
        "   https://www.reddit.com/r/Python/comments/def456/second/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_def456&count=2]"
    )

    truncated = await _call(
        _Session(first, second + "\n[Truncated at ~100 tokens]"),
        entry,
        tmp_path,
        "truncated",
    )
    partial = await _call(
        _Session(first, second + "\n[Unavailable: transport failed]"),
        entry,
        tmp_path,
        "partial",
    )

    assert truncated.status == "failed"
    assert "pagination follow-up response was truncated" in truncated.detail
    assert partial.status == "failed"
    assert "pagination follow-up reported a partial failure" in partial.detail


async def test_pagination_round_trip_rejects_partial_previous_and_restored_next(
    tmp_path,
):
    entry = CorpusEntry(
        id="mapped_fetch",
        live="stable",
        kind="listing",
        url="https://www.reddit.com/r/Python/new/?limit=1",
        pagination=True,
        pagination_round_trip=True,
        required_any=("# r/Python · new",),
    )
    first = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. First post\n"
        "   https://www.reddit.com/r/Python/comments/abc123/first/\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_abc123&count=1]"
    )
    second = (
        "# r/Python · new\n\n1 items returned\n\n"
        "1. Second post\n"
        "   https://www.reddit.com/r/Python/comments/def456/second/\n\n"
        "[Previous page: https://www.reddit.com/r/Python/new/"
        "?limit=1&before=t3_def456&count=0]\n\n"
        "[Next page: https://www.reddit.com/r/Python/new/"
        "?limit=1&after=t3_def456&count=2]"
    )

    previous_partial = await _call(
        _Session(
            first,
            second,
            first + "\n[Unavailable: reverse transport failed]",
        ),
        entry,
        tmp_path,
        "previous-partial",
    )
    restored_partial = await _call(
        _Session(
            first,
            second,
            first,
            second + "\n[Unavailable: forward transport failed]",
        ),
        entry,
        tmp_path,
        "restored-partial",
    )

    assert previous_partial.status == "failed"
    assert "Previous reported a partial failure" in previous_partial.detail
    assert restored_partial.status == "failed"
    assert "restored Next reported a partial failure" in restored_partial.detail


async def test_discovery_rejects_named_partial_failure(tmp_path):
    text, evidence = await _discovery_call(
        _Session("Current source\n[Unavailable: transport failed]"),
        tmp_path,
        "partial_source",
        "fetch",
        {"url": "https://www.reddit.com/r/Python/"},
    )

    assert text is None
    assert evidence.status == "failed"
    assert "partial failure" in evidence.detail


def test_checker_never_pins_a_corpus_account_by_name():
    """Semantic checks must derive usernames from the entry, never hard-code them.

    A pinned account couples the contract to one corpus target: repointing
    ``user_listing`` away from ``u/AutoModerator`` -- which Reddit serves no
    comments for, while still exposing them via ``/overview`` -- failed on the
    *checker*, not the render, in all three phases. The ``spez`` pins had the
    same defect. ``_entry_username()`` exists precisely so the check follows the
    route.

    Subreddit names stay allowed: those are the route, not an account.
    """

    import re as _re
    from pathlib import Path

    source = Path("scripts/reddit_parity.py").read_text()
    offenders = []
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "_entry_username" in line:
            continue
        # Only flag u/<name> inside a regex or format string, not prose.
        if _re.search(r'r?f?["\'][^"\']*u/[A-Za-z0-9_-]{3,}', line):
            offenders.append(f"{number}: {stripped[:88]}")

    assert not offenders, (
        "hard-coded account name(s) in semantic checks; derive with "
        "_entry_username(entry) instead:\n  " + "\n  ".join(offenders)
    )


def test_velocity_ranked_exemption_is_narrow_and_justified():
    """Only the no-overlap clause is dropped, and only for re-ranking feeds.

    Reddit re-ranks ``rising``/``randomrising``/``best`` between the two paged
    requests, so an item crossing the page boundary in those seconds is the
    feed working as designed. Asserting zero overlap there tests Reddit's
    ranking stability, not our paging, and it failed across consecutive live
    runs. Every other pagination guarantee still applies to these feeds.
    """

    from scripts.reddit_parity import _VELOCITY_RANKED_FEEDS

    by_id = {entry.id: entry for entry in load_corpus(DEFAULT_CORPUS)}

    for entry_id in _VELOCITY_RANKED_FEEDS:
        assert entry_id in by_id, f"{entry_id} left the corpus"
        # The exemption must not become an excuse to stop paging them at all.
        assert by_id[entry_id].pagination, entry_id
        assert any(
            token in entry_id for token in ("rising", "best")
        ), f"{entry_id} is not a velocity-ranked feed"

    # Stable sorts must never be exempted.
    for entry_id in by_id:
        if any(k in entry_id for k in ("_new", "_top_", "controversial")):
            assert entry_id not in _VELOCITY_RANKED_FEEDS, entry_id

    # The exemption stays a small minority of paged routes.
    paged = sum(1 for e in by_id.values() if e.pagination)
    assert len(_VELOCITY_RANKED_FEEDS) < paged // 4, "exemption grew too broad"
