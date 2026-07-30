"""Paced, reproducible live New Reddit parity corpus runner.

The checked-in corpus distinguishes stable anonymous URLs, dynamically
discovered public URLs with opaque IDs and access
states that have no real anonymous target. A skipped route is evidence of an
unrun target, never a pass.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from fetchaller.content.reddit import route_reddit_url

if __package__:
    from scripts.smoke_test import (
        _BLOCKED_RESPONSE,
        EXPECTED_TOOLS,
        _stdio_server_parameters,
    )
else:
    from smoke_test import _BLOCKED_RESPONSE, EXPECTED_TOOLS, _stdio_server_parameters

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "baselines" / "reddit-parity-corpus.json"
LEGACY_CONTRACT = ROOT / "baselines" / "reddit-legacy-contract-v1.json"


def _load_legacy_surface_variants() -> tuple[dict[str, Any], ...]:
    """Load the independent, versioned legacy route-variant inventory."""

    payload = json.loads(LEGACY_CONTRACT.read_text())
    if (
        payload.get("schema_version") != 1
        or payload.get("contract_id") != "old-reddit-public-read-surface-v1"
        or not isinstance(payload.get("surface_variants"), list)
    ):
        raise ValueError("unsupported independent Reddit legacy contract")
    variants: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in payload["surface_variants"]:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("id"), str)
            or not raw["id"]
            or raw["id"] in ids
            or raw.get("representation")
            not in {"normal", "explicit_json", "raw", "fallback"}
        ):
            raise ValueError("invalid independent Reddit surface variant")
        is_fetch = (
            isinstance(raw.get("kind"), str)
            and isinstance(raw.get("example_url"), str)
        )
        is_tool = (
            isinstance(raw.get("tool"), str)
            and isinstance(raw.get("arguments"), dict)
        )
        if is_fetch == is_tool:
            raise ValueError(
                f"legacy variant must be one fetch route or tool: {raw['id']}"
            )
        if is_fetch:
            route = route_reddit_url(raw["example_url"])
            if route is None or route.kind != raw["kind"]:
                raise ValueError(
                    f"legacy variant no longer maps to {raw['kind']}: "
                    f"{raw['id']}"
                )
        ids.add(raw["id"])
        variants.append(raw)
    return tuple(variants)


LEGACY_SURFACE_VARIANTS = _load_legacy_surface_variants()
_LEGACY_VARIANT_BY_ID = {
    variant["id"]: variant for variant in LEGACY_SURFACE_VARIANTS
}
_CURSOR = re.compile(r"\[Next page: after=(t[1-6]_[A-Za-z0-9]{2,16})\]")
_FETCH_NEXT_PAGE = re.compile(
    r"(?m)^\[Next page: "
    r"(?P<url>https://www\.reddit\.com/[^\]\s]+)\]$"
)
_FETCH_PREVIOUS_PAGE = re.compile(
    r"(?m)^\[Previous page: "
    r"(?P<url>https://www\.reddit\.com/[^\]\s]+)\]$"
)
_EXPECTED_LIVE_ACCESS_ERRORS = {
    "user_upvoted_private": (
        "Error: Reddit account-private activity is not publicly readable."
    ),
    "user_downvoted_private": (
        "Error: Reddit account-private activity is not publicly readable."
    ),
    "user_gilded_given_private": (
        "Error: Reddit account-private gildings given are not publicly "
        "readable."
    ),
}
_LOGIN_PAGE = re.compile(
    r"(?im)^\s*(?:#\s*)?(?:log in|sign in)\s*$|"
    r"^\s*#\s*welcome to reddit\s*$|"
    r"^\s*\[redirected to:\s*https://www\.reddit\.com/"
    r"(?:account/)?login(?:[/?)#][^\]]*)?\]\s*$|"
    r"<title[^>]*>\s*(?:log in|sign in)",
)
_CLASSIC_REDDIT_BLOCK = re.compile(
    r"whoa there, pardner|too many requests|rate limit(?:ed| exceeded)?",
    re.IGNORECASE,
)
_REDDIT_POST_URL = re.compile(
    r"https://www\.reddit\.com/r/[A-Za-z0-9_+-]+/comments/[a-z0-9]+/",
)
_COMMENT_ITEM_URL = re.compile(
    r"(?m)^Permalink: "
    r"(https://www\.reddit\.com/(?:r/[A-Za-z0-9_+-]+/)?comments/"
    r"[A-Za-z0-9]{2,16}/[^/?#\s]+/[A-Za-z0-9]{2,16}/)$"
)
_POST_ITEM_URL = re.compile(
    r"(?m)^\s{3}"
    r"(https://www\.reddit\.com/r/[A-Za-z0-9_+-]+/comments/"
    r"[A-Za-z0-9]{2,16}/(?:[^/?#\s]+/)?)$"
)
_BARE_POST_ITEM_URL = re.compile(
    r"(?m)^(https://www\.reddit\.com/r/[A-Za-z0-9_+-]+/comments/"
    r"[A-Za-z0-9]{2,16}/(?:[^/?#\s]+/)?)$"
)
_DIRECTORY_ITEM_URL = re.compile(
    r"(?m)^\s{3}(https://www\.reddit\.com/"
    r"(?:r/[A-Za-z0-9_]+|user/[A-Za-z0-9_-]+)/)$"
)
_LIVE_ITEM_ID = re.compile(r"(?m)^Update ID: ([A-Za-z0-9-]{2,128})$")
_JSON_POST_PERMALINK = re.compile(
    r"/r/(?P<subreddit>[A-Za-z0-9_]{1,21})/comments/"
    r"(?P<post_id>[A-Za-z0-9]{2,16})/"
)
_POST_LISTING_KINDS = frozenset(
    {
        "comment_listing",
        "domain_listing",
        "listing",
        "search",
        "user_listing",
        "wiki_discussions",
    }
)
_TRUNCATION_MARKER = re.compile(r"\[Truncated at ~\d+ tokens\]\s*\Z")

# Feeds Reddit re-ranks between the two paged requests. Every other part of the
# pagination contract still applies to them -- a cursor must exist, page two
# must return items with real identities, and its body must differ from page
# one. Only the no-overlap clause is dropped, because it asserts that Reddit's
# ranking holds still for the seconds between requests, which is a claim about
# Reddit, not about our paging. ``randomrising`` is randomized by definition,
# and ``rising``/``best`` re-rank on velocity; an item legitimately moving
# across the page boundary is the feed working, not a paging defect.
_VELOCITY_RANKED_FEEDS = frozenset({
    "listing_global_best",
    "listing_global_rising",
    "listing_global_randomrising",
    "listing_subreddit_rising",
    "listing_subreddit_randomrising",
    "multi_rising",
    "multi_randomrising",
})
_NAMED_PARTIAL_FAILURE = re.compile(
    r"(?im)^\s*\[[^\]\n]*(?:unavailable|failed|timed out|invalid response|"
    r"not returned)"
    r"[^\]\n]*\]\s*$"
)
_BROWSER_EGRESS_SUMMARY = re.compile(
    r"(?m)BROWSER_EGRESS_SUMMARY allowed=(?P<allowed>\d+) "
    r"denied=(?P<denied>\d+)\s*$"
)
_BROWSER_DISPATCH_SUMMARY = re.compile(
    r"(?m)BROWSER_DISPATCH_SUMMARY "
    r"total=(?P<total>\d+) reddit=(?P<reddit>\d+)\s*$"
)
_REDDIT_SESSION_AUDIT = re.compile(
    r"(?m)REDDIT_SESSION_AUDIT "
    r"hydrated_anonymous=(?P<hydrated>[01]) "
    r"hydrated_cookie_count=(?P<count>\d+) "
    r"bootstrap_instrumented=(?P<instrumented>[01]) "
    r"bootstrap_network_attempts=(?P<attempts>\d+)\s*$"
)
_PROCESS_IDENTITY = re.compile(
    r"PROCESS_IDENTITY "
    r"pid=(?P<pid>[1-9]\d*) "
    r"fetchaller_source=(?P<fetchaller_source>\S+) "
    r"fetchaller_sha256=(?P<fetchaller_sha256>[a-f0-9]{64}) "
    r"wafer_source=(?P<wafer_source>\S+) "
    r"wafer_sha256=(?P<wafer_sha256>[a-f0-9]{64})"
)
_PROCESS_IDENTITY_END = re.compile(
    r"PROCESS_IDENTITY_END "
    r"pid=(?P<pid>[1-9]\d*) "
    r"fetchaller_source=(?P<fetchaller_source>\S+) "
    r"fetchaller_sha256=(?P<fetchaller_sha256>[a-f0-9]{64}) "
    r"wafer_source=(?P<wafer_source>\S+) "
    r"wafer_sha256=(?P<wafer_sha256>[a-f0-9]{64})"
)
_DYNAMIC_POST_URL = re.compile(
    r"https://www\.reddit\.com/r/(?P<subreddit>[A-Za-z0-9_+-]+)/"
    r"comments/(?P<post_id>[a-z0-9]{2,16})/"
)
_DYNAMIC_MULTI_URL = re.compile(
    r"https://www\.reddit\.com/user/(?P<username>[A-Za-z0-9_-]{1,64})/"
    r"m/(?P<multi>[A-Za-z0-9_-]{1,64})/",
    re.IGNORECASE,
)
_DYNAMIC_LIVE_UPDATE_URL = re.compile(
    r"https://www\.reddit\.com/live/(?P<thread>[A-Za-z0-9]{2,16})/"
    r"updates/(?P<update>[A-Za-z0-9-]{2,128})/?",
    re.IGNORECASE,
)
_DYNAMIC_COLLECTION_URL = re.compile(
    r"https://www\.reddit\.com/r/(?P<subreddit>[A-Za-z0-9_]{1,21})/"
    r"collection/(?P<collection>[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?",
    re.IGNORECASE,
)
_COLLECTION_ITEMS = re.compile(r"(?m)^(\d[\d,]*) items returned$")
_REVISION_ID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CorpusEntry:
    id: str
    live: str
    kind: str | None = None
    url: str | None = None
    raw: bool = False
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    pagination: bool = False
    pagination_round_trip: bool = False
    required_any: tuple[str, ...] = ()
    access_state: str | None = None
    discovery: str | None = None
    offline_reason: str | None = None
    expect_error: bool = False
    allow_empty: bool = False


@dataclass
class Evidence:
    id: str
    stage: str
    status: str
    detail: str
    chars: int = 0
    sha256: str = ""
    artifact: str = ""
    followup_chars: int = 0
    followup_sha256: str = ""
    followup_artifact: str = ""


def _python_source_tree_identity(
    package_file: Path,
    *,
    child_source: Path,
) -> dict[str, str]:
    """Return the server-compatible hash and expected child source path."""

    package_file = package_file.resolve()
    package_root = package_file.parent
    digest = hashlib.sha256()
    sources = sorted(
        path
        for path in package_root.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    if not sources:
        raise ValueError(f"empty Python source tree: {package_root}")
    for source in sources:
        relative = source.relative_to(package_root).as_posix().encode()
        content = source.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    expected_source = child_source.resolve()
    if (
        not expected_source.is_file()
        or not expected_source.is_relative_to(package_root)
    ):
        raise ValueError(
            f"expected child source is outside package: {expected_source}"
        )
    return {
        "package_file": str(package_file),
        "source": str(expected_source),
        "sha256": digest.hexdigest(),
    }


def _capture_harness_identity(corpus: Path) -> dict[str, object]:
    """Hash the executing gate, selected corpus, and both source packages."""

    runner = Path(__file__).resolve()
    smoke = (ROOT / "scripts" / "smoke_test.py").resolve()
    selected_corpus = corpus.resolve()
    files: dict[str, dict[str, str]] = {}
    for name, path in (
        ("runner", runner),
        ("smoke_test", smoke),
        ("corpus", selected_corpus),
        ("legacy_contract", LEGACY_CONTRACT.resolve()),
    ):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"harness identity file is unavailable: {path}")
        files[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    fetchaller_spec = importlib.util.find_spec("fetchaller")
    wafer_spec = importlib.util.find_spec("wafer")
    if (
        fetchaller_spec is None
        or fetchaller_spec.origin is None
        or wafer_spec is None
        or wafer_spec.origin is None
    ):
        raise ValueError("harness package source identity is unavailable")
    fetchaller_file = Path(fetchaller_spec.origin)
    wafer_file = Path(wafer_spec.origin)
    packages = {
        "fetchaller": _python_source_tree_identity(
            fetchaller_file,
            child_source=fetchaller_file.resolve().parent / "server.py",
        ),
        "wafer": _python_source_tree_identity(
            wafer_file,
            child_source=wafer_file,
        ),
    }
    return {"files": files, "packages": packages}


def _audit_harness_identity(
    start: dict[str, object],
    end: dict[str, object],
) -> Evidence:
    """Require exact runner/corpus/package identity for the entire gate."""

    if start == end:
        packages = start.get("packages")
        fetchaller_hash = (
            packages.get("fetchaller", {}).get("sha256")
            if isinstance(packages, dict)
            and isinstance(packages.get("fetchaller"), dict)
            else None
        )
        wafer_hash = (
            packages.get("wafer", {}).get("sha256")
            if isinstance(packages, dict)
            and isinstance(packages.get("wafer"), dict)
            else None
        )
        return Evidence(
            "harness_identity",
            "release",
            "passed",
            (
                "runner, smoke launcher, corpus, legacy contract, and package "
                "trees "
                f"unchanged (fetchaller={fetchaller_hash}, wafer={wafer_hash})"
            ),
        )

    changed: list[str] = []
    for section in ("files", "packages"):
        start_section = start.get(section)
        end_section = end.get(section)
        if not isinstance(start_section, dict) or not isinstance(
            end_section,
            dict,
        ):
            changed.append(section)
            continue
        for name in sorted(set(start_section) | set(end_section)):
            if start_section.get(name) != end_section.get(name):
                changed.append(f"{section}.{name}")
    return Evidence(
        "harness_identity",
        "release",
        "failed",
        "release harness identity changed during run: "
        + ", ".join(changed or ["unknown"]),
    )


def _audit_reddit_cookie_cache(cache_dir: Path, stage: str) -> Evidence:
    """Prove the fresh runtime wrote a readable, unexpired Reddit cache."""

    cache_path = cache_dir / "reddit.com.json"
    try:
        if cache_path.is_symlink() or not cache_path.is_file():
            raise ValueError("reddit.com.json was not durably created")
        payload = json.loads(cache_path.read_text())
        if not isinstance(payload, list) or not payload:
            raise ValueError("reddit.com.json is not a non-empty cookie list")
        now = time.time()
        active = [
            entry
            for entry in payload
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and bool(entry["name"])
            and isinstance(entry.get("raw"), str)
            and bool(entry["raw"])
            and isinstance(entry.get("expires"), (int, float))
            and not isinstance(entry.get("expires"), bool)
            and float(entry["expires"]) > now
        ]
        if not active:
            raise ValueError("reddit.com.json has no unexpired cookies")
        if len(active) != len(payload):
            raise ValueError("reddit.com.json contains malformed or expired entries")
        mode = cache_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError("reddit.com.json is not owner-only")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return Evidence(
            "durable_reddit_cookie_cache",
            stage,
            "failed",
            str(exc),
        )
    return Evidence(
        "durable_reddit_cookie_cache",
        stage,
        "passed",
        (
            f"fresh runtime cache is readable with {len(active)} "
            "unexpired owner-only Reddit cookies"
        ),
    )


def _audit_process_identity(
    log_path: Path,
    stage: str,
    *,
    expected_packages: dict[str, object] | None = None,
    compare_paths: bool = True,
) -> Evidence:
    """Require one unchanged source identity across each MCP process life."""

    try:
        text = log_path.read_text()
    except OSError as exc:
        return Evidence("process_identity", stage, "failed", str(exc))
    start_matches = list(_PROCESS_IDENTITY.finditer(text))
    end_matches = list(_PROCESS_IDENTITY_END.finditer(text))
    if len(start_matches) != 1 or len(end_matches) != 1:
        return Evidence(
            "process_identity",
            stage,
            "failed",
            (
                "expected one start and one end process identity, found "
                f"start={len(start_matches)} end={len(end_matches)}"
            ),
        )
    fields = start_matches[0].groupdict()
    end_fields = end_matches[0].groupdict()
    if fields != end_fields:
        changed = sorted(
            name
            for name, value in fields.items()
            if end_fields.get(name) != value
        )
        return Evidence(
            "process_identity",
            stage,
            "failed",
            "process source identity changed during run: "
            + ", ".join(changed),
        )
    if expected_packages is not None:
        mismatched: list[str] = []
        for package_name in ("fetchaller", "wafer"):
            expected = expected_packages.get(package_name)
            if not isinstance(expected, dict):
                mismatched.append(f"{package_name}_identity")
                continue
            source_name = f"{package_name}_source"
            hash_name = f"{package_name}_sha256"
            if fields[hash_name] != expected.get("sha256"):
                mismatched.append(hash_name)
            if compare_paths and fields[source_name] != expected.get("source"):
                mismatched.append(source_name)
        if mismatched:
            return Evidence(
                "process_identity",
                stage,
                "failed",
                "child source identity did not match release harness: "
                + ", ".join(mismatched),
            )
    for name in ("fetchaller_source", "wafer_source"):
        source = Path(fields[name])
        if not source.is_absolute() or source.suffix != ".py":
            return Evidence(
                "process_identity",
                stage,
                "failed",
                f"{name} was not an absolute Python source path",
            )
    return Evidence(
        "process_identity",
        stage,
        "passed",
        (
            f"pid={fields['pid']} "
            f"fetchaller={fields['fetchaller_source']}@"
            f"{fields['fetchaller_sha256']} "
            f"wafer={fields['wafer_source']}@{fields['wafer_sha256']}"
        ),
    )


def _audit_browser_egress(
    path: Path,
    stage: str,
    *,
    require_zero: bool,
) -> Evidence:
    """Read the server's exact guarded-browser connection summary."""

    try:
        text = path.read_text()
    except OSError as exc:
        return Evidence(
            "browser_egress",
            stage,
            "failed",
            f"server stderr audit is unreadable: {exc}",
            artifact=str(path),
        )
    matches = list(_BROWSER_EGRESS_SUMMARY.finditer(text))
    if len(matches) != 1:
        return Evidence(
            "browser_egress",
            stage,
            "failed",
            "expected exactly one guarded-browser shutdown summary",
            artifact=str(path),
        )
    allowed = int(matches[0].group("allowed"))
    denied = int(matches[0].group("denied"))
    if require_zero and (allowed or denied):
        return Evidence(
            "browser_egress",
            stage,
            "failed",
            (
                "recreated server opened guarded-browser connections "
                f"(allowed={allowed}, denied={denied})"
            ),
            artifact=str(path),
        )
    return Evidence(
        "browser_egress",
        stage,
        "passed",
        f"guarded-browser connections: allowed={allowed}, denied={denied}",
        artifact=str(path),
    )


def _audit_browser_dispatch(path: Path, stage: str) -> Evidence:
    """Prove Reddit was never dispatched to generic BrowserSolver."""

    try:
        text = path.read_text()
    except OSError as exc:
        return Evidence(
            "browser_dispatch",
            stage,
            "failed",
            f"server stderr audit is unreadable: {exc}",
            artifact=str(path),
        )
    matches = list(_BROWSER_DISPATCH_SUMMARY.finditer(text))
    if len(matches) != 1:
        return Evidence(
            "browser_dispatch",
            stage,
            "failed",
            "expected exactly one BrowserSolver dispatch summary",
            artifact=str(path),
        )
    total = int(matches[0].group("total"))
    reddit = int(matches[0].group("reddit"))
    if reddit != 0:
        return Evidence(
            "browser_dispatch",
            stage,
            "failed",
            f"generic BrowserSolver received {reddit} Reddit dispatches",
            artifact=str(path),
        )
    return Evidence(
        "browser_dispatch",
        stage,
        "passed",
        f"zero Reddit BrowserSolver dispatches ({total} total dispatches)",
        artifact=str(path),
    )


def _audit_reddit_session(
    path: Path,
    stage: str,
    *,
    expect_hydrated: bool,
    require_no_bootstrap: bool,
) -> Evidence:
    """Prove cache hydration and count Reddit's pure-HTTP verification legs."""

    try:
        text = path.read_text()
    except OSError as exc:
        return Evidence(
            "reddit_session_persistence",
            stage,
            "failed",
            f"server stderr audit is unreadable: {exc}",
            artifact=str(path),
        )
    matches = list(_REDDIT_SESSION_AUDIT.finditer(text))
    if len(matches) != 1:
        return Evidence(
            "reddit_session_persistence",
            stage,
            "failed",
            "expected exactly one Reddit session shutdown audit",
            artifact=str(path),
        )
    hydrated = matches[0].group("hydrated") == "1"
    cookie_count = int(matches[0].group("count"))
    instrumented = matches[0].group("instrumented") == "1"
    attempts = int(matches[0].group("attempts"))
    if not instrumented:
        return Evidence(
            "reddit_session_persistence",
            stage,
            "failed",
            "Reddit pure-HTTP verification counter was not instrumented",
            artifact=str(path),
        )
    if hydrated != expect_hydrated:
        return Evidence(
            "reddit_session_persistence",
            stage,
            "failed",
            (
                "Reddit anonymous cache hydration state was "
                f"{int(hydrated)}, expected {int(expect_hydrated)}"
            ),
            artifact=str(path),
        )
    if require_no_bootstrap and attempts:
        return Evidence(
            "reddit_session_persistence",
            stage,
            "failed",
            (
                "recreated Reddit session reran pure-HTTP verification "
                f"{attempts} time(s) after cache hydration"
            ),
            artifact=str(path),
        )
    return Evidence(
        "reddit_session_persistence",
        stage,
        "passed",
        (
            f"hydrated_anonymous={int(hydrated)}, "
            f"hydrated_cookie_count={cookie_count}, "
            "bootstrap_instrumented=1, "
            f"bootstrap_network_attempts={attempts}"
        ),
        artifact=str(path),
    )


def _legacy_variant_inventory_error(
    entries: list[CorpusEntry],
) -> str | None:
    """Require an exact structural match to the independent legacy matrix."""

    by_id = {entry.id: entry for entry in entries}
    expected_ids = set(_LEGACY_VARIANT_BY_ID)
    actual_ids = set(by_id)
    if actual_ids != expected_ids or len(entries) != len(expected_ids):
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        return (
            "corpus did not exactly match independent legacy variants: "
            f"missing={missing!r} unexpected={unexpected!r}"
        )
    for entry_id, variant in _LEGACY_VARIANT_BY_ID.items():
        entry = by_id[entry_id]
        if (
            entry.kind != variant.get("kind")
            or entry.tool != variant.get("tool")
        ):
            return f"legacy variant route/tool mismatch: {entry_id}"
        representation = (
            "raw"
            if entry.raw
            else "explicit_json"
            if entry.kind == "explicit_json"
            else "fallback"
            if entry.kind == "html_fallback"
            else "normal"
        )
        if representation != variant["representation"]:
            return f"legacy variant representation mismatch: {entry_id}"
        if entry.tool and entry.arguments != variant["arguments"]:
            return f"legacy tool arguments mismatch: {entry_id}"
    return None


def load_corpus(path: Path) -> list[CorpusEntry]:
    """Load and strictly validate the versioned, checked-in corpus."""

    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 1 or not isinstance(payload.get("routes"), list):
        raise ValueError("unsupported Reddit parity corpus")
    entries: list[CorpusEntry] = []
    ids: set[str] = set()
    for raw in payload["routes"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise ValueError("corpus entry has no string id")
        if raw["id"] in ids or raw.get("live") not in {
            "stable", "unstable", "fixture_only"
        }:
            raise ValueError(f"invalid corpus entry: {raw.get('id')!r}")
        ids.add(raw["id"])
        is_fetch = isinstance(raw.get("kind"), str) and isinstance(raw.get("url"), str)
        is_tool = isinstance(raw.get("tool"), str) and isinstance(raw.get("arguments"), dict)
        if is_fetch == is_tool:
            raise ValueError(f"corpus entry must be one fetch route or one tool: {raw['id']}")
        required_any = raw.get("required_any")
        if (
            not isinstance(required_any, list)
            or not required_any
            or any(not isinstance(marker, str) or not marker for marker in required_any)
        ):
            raise ValueError(f"corpus entry needs non-empty semantic markers: {raw['id']}")
        access_state = raw.get("access_state")
        if access_state is not None and access_state not in {
            "banned", "forbidden", "gated", "not_found", "private", "quarantined"
        }:
            raise ValueError(f"unknown access state: {raw['id']}")
        discovery = raw.get("discovery")
        if discovery is not None and (
            not isinstance(discovery, str) or not discovery
        ):
            raise ValueError(f"invalid dynamic discovery key: {raw['id']}")
        offline_reason = raw.get("offline_reason")
        if (raw["live"] == "fixture_only") != (
            isinstance(offline_reason, str) and bool(offline_reason.strip())
        ):
            raise ValueError(
                f"fixture-only entry needs an exclusive offline reason: {raw['id']}"
            )
        expect_error = raw.get("expect_error", False)
        if (
            not isinstance(expect_error, bool)
            or expect_error
            and (
                raw["live"] == "fixture_only"
                or access_state is None
            )
        ):
            raise ValueError(f"invalid expected-error entry: {raw['id']}")
        allow_empty = raw.get("allow_empty", False)
        if not isinstance(allow_empty, bool) or allow_empty and expect_error:
            raise ValueError(f"invalid allow-empty entry: {raw['id']}")
        pagination_round_trip = raw.get("pagination_round_trip", False)
        if (
            not isinstance(pagination_round_trip, bool)
            or pagination_round_trip
            and (
                not raw.get("pagination", False)
                or not is_fetch
            )
        ):
            raise ValueError(
                f"invalid pagination round-trip declaration: {raw['id']}"
            )
        entry = CorpusEntry(
            id=raw["id"], live=raw["live"], kind=raw.get("kind"), url=raw.get("url"),
            raw=raw.get("raw", False), tool=raw.get("tool"), arguments=raw.get("arguments"),
            pagination=raw.get("pagination", False),
            pagination_round_trip=pagination_round_trip,
            required_any=tuple(required_any), access_state=access_state,
            discovery=discovery,
            offline_reason=offline_reason,
            expect_error=expect_error,
            allow_empty=allow_empty,
        )
        if is_fetch:
            mapped = route_reddit_url(entry.url or "")
            if mapped is None or mapped.kind != entry.kind:
                raise ValueError(f"corpus route no longer maps to {entry.kind}: {entry.id}")
        entries.append(entry)
    if inventory_error := _legacy_variant_inventory_error(entries):
        raise ValueError(inventory_error)
    return entries


def eligible(entry: CorpusEntry, include_unstable: bool) -> tuple[bool, str]:
    """Select live evidence without relabelling offline evidence.

    ``fixture_only`` is an offline evidence class, not a lower-confidence live
    target.  It therefore stays unrun even when unstable live targets are
    explicitly selected.
    """

    if entry.live == "fixture_only":
        return (
            False,
            "not run live: offline fixture evidence only — "
            f"{entry.offline_reason}",
        )
    if entry.live == "unstable" and not include_unstable:
        return False, "not run: target is intentionally unstable"
    return True, ""


def _text(result: Any) -> str:
    return "\n".join(str(getattr(item, "text", "")) for item in result.content)


def _write_artifact(directory: Path, stage: str, entry_id: str, text: str) -> str:
    name = f"{stage}-{entry_id}.txt"
    (directory / name).write_text(text)
    return name


def _prepare_empty_evidence_directory(directory: Path) -> Path:
    """Reject stale evidence instead of mixing it into a release run."""

    if directory.is_symlink():
        raise ValueError(
            f"evidence output directory must not be a symlink: {directory}"
        )
    directory = directory.resolve()
    if directory.exists():
        if not directory.is_dir() or any(directory.iterdir()):
            raise ValueError(
                f"evidence output directory must start empty: {directory}"
            )
        return directory
    directory.mkdir(parents=True)
    return directory


def _body_evidence(entry: CorpusEntry, stage: str, directory: Path, text: str) -> Evidence:
    """Persist every response before evaluating it, including failed bodies."""

    artifact = _write_artifact(directory, stage, entry.id, text)
    return Evidence(
        entry.id,
        stage,
        "failed",
        "not evaluated",
        len(text),
        hashlib.sha256(text.encode()).hexdigest(),
        artifact,
    )


def _valid_explicit_reddit_listing(text: str) -> bool:
    """Require a nonempty coherent Reddit post listing, not a JSON shell."""

    try:
        payload = json.loads(text)
    except ValueError:
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "Listing"
        or not isinstance(payload.get("data"), dict)
        or not isinstance(payload["data"].get("children"), list)
        or not payload["data"]["children"]
    ):
        return False
    for child in payload["data"]["children"]:
        data = child.get("data") if isinstance(child, dict) else None
        if (
            not isinstance(child, dict)
            or child.get("kind") != "t3"
            or not isinstance(data, dict)
        ):
            return False
        post_id = data.get("id")
        title = data.get("title")
        subreddit = data.get("subreddit")
        permalink = data.get("permalink")
        match = (
            _JSON_POST_PERMALINK.match(permalink)
            if isinstance(permalink, str)
            else None
        )
        if (
            not isinstance(post_id, str)
            or re.fullmatch(r"[A-Za-z0-9]{2,16}", post_id) is None
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(subreddit, str)
            or match is None
            or match.group("post_id") != post_id
            or match.group("subreddit").casefold() != subreddit.casefold()
            or (
                data.get("name") is not None
                and data.get("name") != f"t3_{post_id}"
            )
        ):
            return False
    return True


def _valid_raw_new_reddit_html(text: str) -> bool:
    """Require a substantive r/Python post inside the New Reddit app tree."""

    lowered = text.lower()
    if _CLASSIC_REDDIT_BLOCK.search(text) or _LOGIN_PAGE.search(text):
        return False
    if not (
        ("<shreddit-app" in lowered or "<reddit-app" in lowered)
        and "<shreddit-feed" in lowered
    ):
        return False

    class SubstantiveFeedPostFoundError(Exception):
        pass

    class NewRedditFeedParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.found = False

        def handle_starttag(
            self,
            tag: str,
            attrs: list[tuple[str, str | None]],
        ) -> None:
            tag = tag.casefold()
            if (
                tag == "shreddit-post"
                and any(
                    ancestor in {"shreddit-app", "reddit-app"}
                    for ancestor in self.stack
                )
                and "shreddit-feed" in self.stack
            ):
                attributes = {
                    name.casefold(): value
                    for name, value in attrs
                    if value is not None
                }
                fullname = attributes.get("id") or attributes.get("thingid")
                title = attributes.get("post-title")
                permalink = attributes.get("permalink")
                if (
                    isinstance(fullname, str)
                    and re.fullmatch(
                        r"t3_[A-Za-z0-9]{2,16}",
                        fullname,
                    )
                    and isinstance(title, str)
                    and title.strip()
                    and isinstance(permalink, str)
                ):
                    post_id = fullname.removeprefix("t3_")
                    match = re.fullmatch(
                        r"/r/(?P<subreddit>[A-Za-z0-9_]+)/comments/"
                        r"(?P<post_id>[A-Za-z0-9]{2,16})/[^/?#]+/",
                        permalink,
                        re.IGNORECASE,
                    )
                    if (
                        match is not None
                        and match.group("subreddit").casefold() == "python"
                        and match.group("post_id") == post_id
                    ):
                        self.found = True
                        raise SubstantiveFeedPostFoundError
            self.stack.append(tag)

        def handle_startendtag(
            self,
            tag: str,
            attrs: list[tuple[str, str | None]],
        ) -> None:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

        def handle_endtag(self, tag: str) -> None:
            tag = tag.casefold()
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index] == tag:
                    del self.stack[index:]
                    break

    parser = NewRedditFeedParser()
    try:
        parser.feed(text)
        parser.close()
    except SubstantiveFeedPostFoundError:
        return True
    except (AssertionError, ValueError):
        return False
    return parser.found


# This registry is deliberately derived from the independent legacy matrix,
# not from the current parity corpus. The corpus is required to match it
# exactly, so deleting a same-kind route variant cannot silently shrink both
# the test inventory and the semantic validator surface.
SEMANTIC_VALIDATOR_IDS = frozenset(
    variant["id"] for variant in LEGACY_SURFACE_VARIANTS
)
# Reddit scores go negative on heavily downvoted content, and the renderer
# prints them verbatim ("-2,224 score"). Every score pattern below therefore
# accepts a leading minus: matching only [\d,]+ made such cards invisible to
# the counted-output gate, which then reported a live render as incomplete.
_POST_CARD = re.compile(
    r"(?m)^\d+\.\s+\S[^\n]*\n"
    r"\s+.*(?:score (?:hidden|-?[\d,]+)|-?[\d,]+ score)"
    r".*[\d,]+ comments.*u/(?:[A-Za-z0-9_-]+|\[deleted\])"
)
_SOURCE_POST_METADATA = re.compile(
    r"(?m)^r/[A-Za-z0-9_+-]+ · "
    r"u/(?:[A-Za-z0-9_-]+|\[deleted\]) · "
    r"(?:score (?:hidden|-?[\d,]+)|-?[\d,]+ score)"
    r"(?: · \d+% upvoted)? · [\d,]+ comments · "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$"
)
_COMMENT_HEADING = re.compile(
    r"(?m)^#{3,6} (?:↳ )*u/(?:[A-Za-z0-9_-]+|\[deleted\]) · "
    r"(?:score hidden|-?[\d,]+ score) · "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC"
)
_COMMENT_BODY = re.compile(
    r"(?ms)^#{3,6} (?:↳ )*u/.+? UTC[^\n]*\n\n"
    r"[^\S\r\n]{0,256}"
    r"(?!Edited:|Awards:|Archived gilding evidence:|Media:|"
    r"Permalink:|Parent context:)\S.*?"
    r"(?:\n\n(?:Edited:|Awards:|Media:|Permalink:)|\Z)"
)
_ACTIVITY_COMMENT_HEADING = re.compile(
    r"(?m)^\d+\. \*\*\S[^\n]*\*\*\n"
    r"\s+r/[A-Za-z0-9_+-]+ · u/(?:[A-Za-z0-9_-]+|\[deleted\]) "
    r"(?:· author flair: [^\n]{1,256}? )?· "
    r"(?:score hidden|-?[\d,]+ score) · "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC"
)
_ACTIVITY_COMMENT_BODY = re.compile(
    r"(?ms)^\d+\. \*\*\S[^\n]*\*\*\n"
    r"\s+r/[A-Za-z0-9_+-]+ · u/(?:[A-Za-z0-9_-]+|\[deleted\]) "
    r"(?:· author flair: [^\n]{1,256}? )?· "
    r"(?:score hidden|-?[\d,]+ score) · "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC[^\n]*\n\n"
    r"[^\S\r\n]{0,256}"
    r"(?!Edited:|Awards:|Archived gilding evidence:|Media:|"
    r"Permalink:|Parent context:)\S.*?"
    r"(?:\n\n(?:Edited:|Awards:|Media:|Permalink:)|\Z)"
)
_COMMENT_PERMALINK = re.compile(
    r"(?m)^Permalink: (?P<url>https://www\.reddit\.com/"
    r"(?:r/[A-Za-z0-9_+-]+/)?comments/[A-Za-z0-9]{2,16}/"
    r"[^/?#\s]+/[A-Za-z0-9]{2,16}/)$"
)
_PARENT_CONTEXT = re.compile(
    r"(?m)^Parent context: (?P<url>https://www\.reddit\.com/"
    r"(?:r/[A-Za-z0-9_+-]+/)?comments/[A-Za-z0-9]{2,16}/"
    r"(?:[^/?#\s]+/(?:[A-Za-z0-9]{2,16}/\?context=3)?)?)$"
)
_LIVE_UPDATE_BODY = re.compile(
    r"(?ms)^\d+\. \*\*u/(?:[A-Za-z0-9_-]+|\[deleted\]) · "
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\*\*(?: · \[stricken\])?\n\n"
    r"(?!Media:|Update ID:)\S.+?"
    r"(?:\n\n(?:Media:|Update ID:)|\n\d+\. \*\*u/|\Z)"
)
_NEXT_PAGE_URL = re.compile(r"(?m)^\[Next page: https://www\.reddit\.com/")
_POSITIVE_RETURN_COUNT = re.compile(
    r"(?m)^(?P<count>[1-9]\d{0,2}(?:,\d{3})*) "
    r"(?P<label>items|posts|comments|results|users|communities|"
    r"contributors|updates|moderators|pages|revisions|trophies) returned$"
)
_RETURN_COUNT = re.compile(
    r"(?m)^(?P<count>(?:0|[1-9]\d{0,2}(?:,\d{3})*)) "
    r"(?P<label>items|posts|comments|results|users|communities|"
    r"contributors|updates|moderators|pages|revisions|trophies) returned$"
)
_COMMUNITY_CARD_HEADING = re.compile(
    r"(?m)^\d+\. \*\*r/(?P<name>[A-Za-z0-9_]+)\*\*(?: — .+)?$"
)
_USER_CARD_HEADING = re.compile(
    r"(?m)^\d+\. \*\*u/(?P<name>[A-Za-z0-9_-]+)\*\* · "
    r"[\d,]+ post karma · [\d,]+ comment karma$"
)
_MODERATOR_CARD = re.compile(
    r"(?m)^\d+\. \*\*u/(?P<name>[A-Za-z0-9_-]+)\*\* · .+\n"
    r"\s+https://www\.reddit\.com/user/(?P=name)/$"
)
_WIKI_PAGE_CARD = re.compile(
    r"(?m)^\d+\. \[.+\]\(https://www\.reddit\.com/"
    r"r/[A-Za-z0-9_]+/wiki/(?P<page>.+)\)$"
)
_WIKI_REVISION_CARD = re.compile(
    r"(?m)^\d+\. \*\*.+\*\* · .+ UTC · u/[A-Za-z0-9_-]+$\n"
    r"\s+revision: (?P<revision>[0-9a-f-]{36})$"
)
_TROPHY_CARD_HEADING = re.compile(r"(?m)^\d+\. \*\*(?P<name>.+)\*\*$")
_MULTI_COMMUNITY_CARD = re.compile(
    r"(?m)^\d+\. \*\*r/(?P<name>[A-Za-z0-9_]+)\*\*\n"
    r"\s+https://www\.reddit\.com/r/(?P=name)/$"
)
_LIVE_CONTRIBUTOR_CARD = re.compile(
    r"(?m)^\d+\. \*\*u/(?P<name>[A-Za-z0-9_-]+)\*\*\n"
    r"\s+https://www\.reddit\.com/user/(?P=name)/$"
)


def _has_positive_count(text: str, *labels: str) -> bool:
    accepted = set(labels)
    return any(
        match.group("label") in accepted
        and int(match.group("count").replace(",", "")) > 0
        for match in _POSITIVE_RETURN_COUNT.finditer(text)
    )


_CORPUS_USERNAME = re.compile(r"^/user/(?P<name>[A-Za-z0-9_-]{1,64})(?:/|$)")


def _entry_username(entry: CorpusEntry) -> str:
    """Return the account a user-scoped corpus entry targets.

    These checks used to hard-code ``spez``, which quietly coupled the contract
    to one account: swapping the corpus to a user who actually exposes public
    multireddits made every user route fail on the *checker*, not the render.
    Deriving the name from the entry keeps the contract about the route.
    """

    match = _CORPUS_USERNAME.match(urlparse(entry.url or "").path)
    if match is None:
        raise ValueError(f"corpus entry {entry.id} is not user-scoped: {entry.url!r}")
    return match.group("name")


def _comment_semantic_error(
    text: str,
    *,
    activity: bool,
) -> str | None:
    """Require every rendered comment card to retain body and navigation."""

    heading_pattern = (
        _ACTIVITY_COMMENT_HEADING if activity else _COMMENT_HEADING
    )
    body_pattern = _ACTIVITY_COMMENT_BODY if activity else _COMMENT_BODY
    headings = len(heading_pattern.findall(text))
    bodies = len(body_pattern.findall(text))
    permalinks = [
        match.group("url") for match in _COMMENT_PERMALINK.finditer(text)
    ]
    parents = [
        match.group("url") for match in _PARENT_CONTEXT.finditer(text)
    ]
    if headings <= 0:
        return "comment output lacked author/score/time metadata"
    if bodies != headings:
        return "one or more comment cards lacked a substantive body"
    if len(permalinks) != headings:
        return "one or more comment cards lacked an exact comment permalink"
    if len(parents) != headings:
        return "one or more comment cards lacked exact parent context"
    if any(permalink == parent for permalink, parent in zip(permalinks, parents)):
        return "comment permalink and parent context were identical"
    return None


def _page_item_identities(text: str) -> set[str]:
    """Extract primary rendered item identities, excluding repeated metadata."""

    identities = set(_COMMENT_ITEM_URL.findall(text))
    identities.update(_POST_ITEM_URL.findall(text))
    identities.update(_BARE_POST_ITEM_URL.findall(text))
    if identities:
        return identities
    identities.update(_DIRECTORY_ITEM_URL.findall(text))
    identities.update(f"live:{value}" for value in _LIVE_ITEM_ID.findall(text))
    return identities


def _count_claim(
    text: str,
    label: str,
) -> tuple[int | None, str | None]:
    matches = [
        match
        for match in _RETURN_COUNT.finditer(text)
        if match.group("label") == label
    ]
    if len(matches) != 1:
        return None, (
            f"expected exactly one {label!r} return count, found "
            f"{len(matches)}"
        )
    return int(matches[0].group("count").replace(",", "")), None


def _post_result_cardinality(
    text: str,
    *,
    label: str = "items",
) -> str | None:
    claimed, error = _count_claim(text, label)
    if error is not None:
        return error
    assert claimed is not None
    cards = len(_POST_CARD.findall(text))
    identities = set(_POST_ITEM_URL.findall(text))
    identities.update(_BARE_POST_ITEM_URL.findall(text))
    if cards != claimed or len(identities) != claimed:
        return (
            f"claimed {claimed} {label} but rendered {cards} complete post "
            "metadata cards and "
            f"{len(identities)} unique post identities"
        )
    return None


def _comment_result_cardinality(
    text: str,
    *,
    activity: bool,
    label: str,
) -> str | None:
    claimed, error = _count_claim(text, label)
    if error is not None:
        return error
    assert claimed is not None
    heading_pattern = (
        _ACTIVITY_COMMENT_HEADING if activity else _COMMENT_HEADING
    )
    headings = len(heading_pattern.findall(text))
    permalinks = [
        match.group("url") for match in _COMMENT_PERMALINK.finditer(text)
    ]
    if (
        headings != claimed
        or len(permalinks) != claimed
        or len(set(permalinks)) != claimed
    ):
        return (
            f"claimed {claimed} {label} but rendered {headings} complete "
            "comment headings and "
            f"{len(set(permalinks))} unique comment permalinks"
        )
    return None


# Reddit search and directory listings legitimately return communities that are
# not public -- r/PythonBeginners is Restricted and shows up on page 2 of a
# community search. The renderer prints the real subreddit_type, so requiring
# the literal "Public" counted only some cards and reported the rest as thin.
_COMMUNITY_STATUS_LINE = (
    r"(?m)^\s+(?:Banned|Public|Private|Restricted|Employees Only|"
    r"Gold Restricted|Gold Only|Archived|User)(?: ·.*)?$"
)


def _directory_result_cardinality(
    text: str,
    *,
    users: bool,
    label: str,
) -> str | None:
    claimed, error = _count_claim(text, label)
    if error is not None:
        return error
    assert claimed is not None
    if users:
        names = _USER_CARD_HEADING.findall(text)
        profiles = re.findall(
            r"(?m)^\s+https://www\.reddit\.com/user/"
            r"([A-Za-z0-9_-]+)/$",
            text,
        )
        activity = re.findall(
            r"(?m)^\s+Public activity: https://www\.reddit\.com/user/"
            r"([A-Za-z0-9_-]+)/overview/$",
            text,
        )
        created = re.findall(r"(?m)^\s+Created: .+ UTC$", text)
        counts = (len(names), len(profiles), len(activity), len(created))
        unique = (
            len({name.casefold() for name in names}),
            len({profile.casefold() for profile in profiles}),
            len({item.casefold() for item in activity}),
        )
    else:
        names = _COMMUNITY_CARD_HEADING.findall(text)
        navigation = re.findall(
            r"(?m)^\s+https://www\.reddit\.com/r/([A-Za-z0-9_]+)/$",
            text,
        )
        subscribers = re.findall(r"(?m)^\s+[\d,]+ subscribers$", text)
        created = re.findall(r"(?m)^\s+Created: .+ UTC$", text)
        public = re.findall(_COMMUNITY_STATUS_LINE, text)
        counts = (
            len(names),
            len(navigation),
            len(subscribers),
            len(created),
            len(public),
        )
        unique = (
            len({name.casefold() for name in names}),
            len({item.casefold() for item in navigation}),
        )
    if any(count != claimed for count in (*counts, *unique)):
        family = "user" if users else "community"
        return (
            f"claimed {claimed} {label} but one or more {family} cards were "
            "thin, duplicated, or missing"
        )
    return None


def _pattern_result_cardinality(
    text: str,
    *,
    label: str,
    pattern: re.Pattern[str],
    family: str,
    unique_group: str | None = None,
) -> str | None:
    claimed, error = _count_claim(text, label)
    if error is not None:
        return error
    assert claimed is not None
    matches = list(pattern.finditer(text))
    identities = (
        [match.group(unique_group) for match in matches]
        if unique_group is not None
        else []
    )
    unique = (
        len(
            {
                identity.casefold()
                if unique_group == "name"
                else identity
                for identity in identities
            }
        )
        if unique_group is not None
        else len(matches)
    )
    if len(matches) != claimed or unique != claimed:
        return (
            f"claimed {claimed} {label} but rendered {len(matches)} complete "
            f"{family} cards with {unique} unique identities"
        )
    return None


def _counted_output_error(entry: CorpusEntry, text: str) -> str | None:
    """Require every claimed result to have one complete unique output card."""

    entry_id = entry.id
    if entry_id in {
        "collection",
    }:
        # This fixed-count branch performs a stricter specialized check.
        return None
    if entry_id in {
        "gilded_global",
        "gilded_comments_global",
        "gilded_subreddit",
        "gilded_comments_subreddit",
        "user_gilded",
        "user_overview",
    }:
        claimed, error = _count_claim(text, "items")
        if error is not None:
            return error
        assert claimed is not None
        cards = (
            len(_ACTIVITY_COMMENT_HEADING.findall(text))
            + len(_POST_CARD.findall(text))
        )
        identities = _page_item_identities(text)
        if cards != claimed or len(identities) != claimed:
            return (
                f"claimed {claimed} items but rendered {cards} complete "
                f"activity cards and {len(identities)} unique identities"
            )
        return None
    if entry_id in {"listing_by_id_comment", "listing_by_id_mixed"}:
        claimed, error = _count_claim(text, "items")
        if error is not None:
            return error
        expected = 2 if entry_id == "listing_by_id_mixed" else 1
        identities = _page_item_identities(text)
        post_cards = len(_POST_CARD.findall(text))
        comment_cards = len(_ACTIVITY_COMMENT_HEADING.findall(text))
        if (
            claimed != expected
            or len(identities) != expected
            or post_cards + comment_cards != expected
        ):
            return (
                f"{entry_id} required exactly {expected} complete unique "
                "requested items"
            )
        return None
    if (
        entry_id == "listing"
        or entry_id.startswith("listing_")
        or entry_id == "domain_listing"
        or entry_id.startswith("domain_")
        or entry_id in {"search", "user_submitted", "wiki_discussions"}
    ):
        return _post_result_cardinality(text)
    if entry_id in {
        "comment_listing",
        "comments_global",
        "user_listing",
        "multi_comments",
    }:
        return _comment_result_cardinality(
            text,
            activity=True,
            label="items",
        )
    if entry_id == "morechildren":
        return _comment_result_cardinality(
            text,
            activity=False,
            label="comments",
        )
    if entry_id in {
        "subreddit_directory",
        "subreddit_directory_new",
        "subreddit_directory_default",
        "subreddit_directory_search",
        "subreddit_directory_gold",
    }:
        return _directory_result_cardinality(
            text,
            users=False,
            label="items",
        )
    if entry_id == "search_communities":
        return _directory_result_cardinality(
            text,
            users=False,
            label="items",
        )
    if entry_id in {
        "user_directory",
        "user_directory_new",
        "user_directory_search",
    }:
        return _directory_result_cardinality(
            text,
            users=True,
            label="users",
        )
    if entry_id == "search_users":
        return _directory_result_cardinality(
            text,
            users=True,
            label="items",
        )
    if entry_id == "moderators":
        # No roster is obtainable anonymously, so there is no claimed count to
        # reconcile; the semantic contract below requires the gated error text.
        return None
    if entry_id == "wiki_pages":
        return _pattern_result_cardinality(
            text,
            label="pages",
            pattern=_WIKI_PAGE_CARD,
            family="wiki-page",
            unique_group="page",
        )
    if entry_id == "wiki_revisions":
        return _pattern_result_cardinality(
            text,
            label="revisions",
            pattern=_WIKI_REVISION_CARD,
            family="wiki-revision",
            unique_group="revision",
        )
    if entry_id == "trophies":
        # Reddit awards the same trophy repeatedly -- redtaboo holds two
        # "Beta Team" and two "RedditGifts 2009-2022", spez eight "Inciteful
        # Link" -- each with its own date and permalink. Keying identity on the
        # name would demand that a faithful trophy case be lossy, so only the
        # count and per-card completeness are required here.
        return _pattern_result_cardinality(
            text,
            label="trophies",
            pattern=_TROPHY_CARD_HEADING,
            family="trophy",
            unique_group=None,
        )
    if entry_id == "multi_about":
        return _pattern_result_cardinality(
            text,
            label="communities",
            pattern=_MULTI_COMMUNITY_CARD,
            family="multireddit-community",
            unique_group="name",
        )
    if entry_id == "multi_profile" or (
        entry_id.startswith("multi_")
        and entry_id not in {"multi_about", "multi_comments"}
    ):
        feed = text.split("## Feed", 1)[-1].split("## Communities", 1)[0]
        return _post_result_cardinality(feed)
    if entry_id in {
        "duplicates",
        "duplicates_subreddit",
        "related",
        "related_subreddit",
    }:
        section = (
            "Other discussions"
            if entry_id.startswith("duplicates")
            else "Related posts"
        )
        result_text = text.split(f"## {section}", 1)[-1]
        return _post_result_cardinality(result_text)
    if entry_id == "live_contributors":
        return _pattern_result_cardinality(
            text,
            label="contributors",
            pattern=_LIVE_CONTRIBUTOR_CARD,
            family="live-contributor",
            unique_group="name",
        )
    if entry_id == "live_update":
        claimed, error = _count_claim(text, "updates")
        if error is not None:
            return error
        assert claimed is not None
        cards = len(_LIVE_UPDATE_BODY.findall(text))
        identities = set(_LIVE_ITEM_ID.findall(text))
        if cards != claimed or len(identities) != claimed:
            return (
                f"claimed {claimed} updates but rendered {cards} complete "
                f"update cards and {len(identities)} unique identities"
            )
        return None
    if entry_id in {"browse_page_1", "search_page_1"}:
        heading_pattern = (
            r"(?m)^r/Python · new · (?P<count>\d+) posts$"
            if entry_id == "browse_page_1"
            else (
                r'(?m)^Search: "asyncio" in r/Python · '
                r"new · all · (?P<count>\d+) results$"
            )
        )
        headings = list(re.finditer(heading_pattern, text))
        cards = len(_POST_CARD.findall(text))
        identities = _page_item_identities(text)
        if (
            len(headings) != 1
            or int(headings[0].group("count")) != 1
            or cards != 1
            or len(identities) != 1
        ):
            return f"{entry_id} did not render exactly one complete unique post"
    return None


def _validated_tool_next_cursor(text: str) -> re.Match[str] | None:
    """Require exactly one unambiguous browse/search continuation cursor."""

    matches = list(_CURSOR.finditer(text))
    return matches[0] if len(matches) == 1 else None


def _pagination_scope(url: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    """Return the exact Reddit route/filter scope for a pagination URL."""

    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").rstrip(".").casefold() != "www.reddit.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        return None
    filters = tuple(
        sorted(
            (key, value)
            for key, value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key not in {"after", "before", "count"}
        )
    )
    return parsed.path, filters


def _validated_fetch_next_page(
    text: str,
    expected_url: str,
) -> tuple[str, str, int] | None:
    """Validate a single exact, forward-only Next link for one route scope."""

    matches = list(_FETCH_NEXT_PAGE.finditer(text))
    if len(matches) != 1:
        return None
    url = matches[0].group("url")
    if _pagination_scope(url) != _pagination_scope(expected_url):
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    after_values = query.get("after") or []
    before_values = query.get("before") or []
    count_values = query.get("count") or []
    if (
        any(len(values) != 1 for values in query.values())
        or len(after_values) != 1
        or before_values
        or len(count_values) != 1
        or re.fullmatch(r"\d{1,4}", count_values[0]) is None
        or int(count_values[0]) <= 0
        or int(count_values[0]) > 1000
        or re.fullmatch(
            r"t[1-6]_[A-Za-z0-9]{2,16}",
            after_values[0],
        )
        is None
    ):
        return None
    return url, after_values[0], int(count_values[0])


def _validated_fetch_previous_page(
    text: str,
    expected_url: str,
) -> tuple[str, str, int] | None:
    """Validate one exact, reverse-only Previous link for one route scope."""

    matches = list(_FETCH_PREVIOUS_PAGE.finditer(text))
    if len(matches) != 1:
        return None
    url = matches[0].group("url")
    if _pagination_scope(url) != _pagination_scope(expected_url):
        return None
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    before_values = query.get("before") or []
    after_values = query.get("after") or []
    count_values = query.get("count") or []
    if (
        any(len(values) != 1 for values in query.values())
        or len(before_values) != 1
        or after_values
        or len(count_values) != 1
        or re.fullmatch(r"\d{1,4}", count_values[0]) is None
        or int(count_values[0]) > 1000
        or re.fullmatch(
            r"t[1-6]_[A-Za-z0-9]{2,16}",
            before_values[0],
        )
        is None
    ):
        return None
    return url, before_values[0], int(count_values[0])


def _semantic_contract_error(entry: CorpusEntry, text: str) -> str | None:
    """Validate substantive legacy semantics, not merely a route heading."""

    # Ad-hoc callers and focused unit tests may construct their own IDs. The
    # checked-in release corpus is separately required to match this registry
    # exactly; unknown external IDs retain the generic safety checks.
    if entry.id not in SEMANTIC_VALIDATOR_IDS:
        return None
    if re.search(r"(?i)\b[\d,]+ exact upvotes?\b", text):
        return "score was incorrectly relabelled as an exact upvote count"

    def require(label: str, pattern: str | re.Pattern[str]) -> str | None:
        found = (
            pattern.search(text)
            if isinstance(pattern, re.Pattern)
            else re.search(pattern, text)
        )
        return None if found else f"missing legacy semantic field: {label}"

    def require_all(
        checks: tuple[tuple[str, str | re.Pattern[str]], ...],
    ) -> str | None:
        for label, pattern in checks:
            if error := require(label, pattern):
                return error
        return None

    if entry.id.startswith("access_"):
        return None
    if entry.id == "explicit_json":
        return (
            None
            if _valid_explicit_reddit_listing(text)
            else "explicit JSON lacked coherent post fields and navigation"
        )
    if entry.id == "raw_html":
        return (
            None
            if _valid_raw_new_reddit_html(text)
            else "raw response lacked a semantic New Reddit application tree"
        )
    if entry.id == "html_fallback":
        return (
            None
            if all(
                marker in text
                for marker in (
                    "# Bringing you the best of Reddit!",
                    "Ads-free browsing",
                    "Higher Rate Limits",
                )
            )
            else "HTML fallback lacked substantive canonical Reddit content"
        )

    if cardinality_error := _counted_output_error(entry, text):
        return cardinality_error

    if entry.id in {"listing_by_id_comment", "listing_by_id_mixed"}:
        if not _has_positive_count(text, "items"):
            return "by-ID route returned no exact requested items"
        if error := require(
            "exact by-ID listing identity",
            re.compile(r"(?m)^# Reddit · items by ID$"),
        ):
            return error
        comment_error = _comment_semantic_error(text, activity=True)
        if comment_error is not None:
            return comment_error
        if entry.id == "listing_by_id_mixed":
            return require_all(
                (
                    ("mixed by-ID post metadata", _POST_CARD),
                    ("mixed by-ID discussion URL", _REDDIT_POST_URL),
                )
            )
        if _POST_CARD.search(text):
            return "comment-only by-ID route contained a post"
        return None

    if entry.id in {
        "thread_global_permalink",
        "thread_gallery",
        "thread_subreddit_sticky",
    }:
        if error := require_all(
            (
                ("source post metadata", _SOURCE_POST_METADATA),
                (
                    "source discussion URL",
                    re.compile(
                        r"(?m)^\*\*Discussion:\*\* "
                        r"https://www\.reddit\.com/"
                    ),
                ),
                ("comment section", re.compile(r"(?m)^## Comments$")),
            )
        ):
            return error
        if entry.id == "thread_gallery":
            gallery_heading = re.search(
                r"(?m)^\*\*Gallery \((?P<count>[1-9]\d*) items\):\*\*$",
                text,
            )
            gallery_items = list(
                re.finditer(
                    r"(?ms)^- \*\*Item (?P<index>[1-9]\d*)\*\*"
                    r"(?P<meta>[^\n]*)\n"
                    r"(?P<links>(?:  - [^\n]+\n?)*)",
                    text,
                )
            )
            gallery_urls: list[str] = []
            complete_items = True
            for item in gallery_items:
                urls = re.findall(
                    r"(?m)^  - (?:Outbound: )?(https://\S+)$",
                    item.group("links"),
                )
                if not urls and "unavailable" not in item.group("meta"):
                    complete_items = False
                gallery_urls.extend(urls)
            claimed = (
                int(gallery_heading.group("count"))
                if gallery_heading is not None
                else 0
            )
            if (
                gallery_heading is None
                or claimed < 2
                or len(gallery_items) != claimed
                or [int(item.group("index")) for item in gallery_items]
                != list(range(1, claimed + 1))
                or not complete_items
                or len(gallery_urls) < 2
                or len(set(gallery_urls)) != len(gallery_urls)
            ):
                return (
                    "gallery thread lacked its exact complete unique media "
                    "inventory"
                )
        if _COMMENT_HEADING.search(text):
            return _comment_semantic_error(text, activity=False)
        return None

    if entry.id == "subreddit_sidebar":
        return require_all(
            (
                ("community identity", re.compile(r"(?m)^# r/Python$")),
                ("public status", re.compile(r"(?m)^- \*\*Status:\*\* Public$")),
                ("subscriber count", re.compile(r"(?m)^- \*\*Subscribers:\*\* [\d,]+$")),
                ("creation time", re.compile(r"(?m)^- \*\*Created:\*\* .+ UTC$")),
                ("full sidebar/about body", re.compile(r"(?ms)^## About\n\n\S.+")),
            )
        )

    if entry.id.startswith("multi_") and entry.id not in {
        "multi_about",
        "multi_profile",
    }:
        scope = entry.required_any[0]
        if error := require_all(
            (
                (
                    "exact multireddit feed scope",
                    re.compile(rf"(?m)^{re.escape(scope)}$"),
                ),
                (
                    "multireddit owner/visibility/subscribers/created",
                    re.compile(
                        rf"(?m)^owner u/{re.escape(_entry_username(entry))} · public · "
                        r"[\d,]+ subscribers · created .+ UTC$"
                    ),
                ),
                ("typed multireddit feed", re.compile(r"(?m)^## Feed$")),
                ("member communities", re.compile(r"(?m)^## Communities$")),
                (
                    "member community URL",
                    re.compile(r"https://www\.reddit\.com/r/[A-Za-z0-9_]+/"),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        if not _has_positive_count(text, "items"):
            return "multireddit variant returned no feed items"
        if entry.id == "multi_comments":
            return _comment_semantic_error(text, activity=True)
        return require_all(
            (
                ("multireddit post metadata", _POST_CARD),
                ("multireddit discussion URL", _REDDIT_POST_URL),
            )
        )

    if entry.id == "subreddit_directory_gold":
        if not re.search(r"(?m)^0 items returned$", text):
            return "retired gold-only directory was not exact empty"
        if _NEXT_PAGE_URL.search(text):
            return "retired gold-only directory fabricated pagination"
        return require_all(
            (
                (
                    "exact gold-directory scope",
                    re.compile(r"(?m)^# Reddit communities · gold$"),
                ),
                (
                    "archived exact-empty provenance",
                    re.compile(
                        r"Directory state source: exact archived Reddit "
                        r"snapshot \(Wayback, 2018-08-23\)\. "
                        r"No gold-only communities were listed\."
                    ),
                ),
            )
        )

    if entry.id.startswith("listing_"):
        heading = entry.required_any[0]
        if entry.allow_empty and re.search(r"(?m)^0 items returned$", text):
            if not re.search(rf"(?m)^{re.escape(heading)}$", text):
                return "empty listing lacked exact scope/sort/time"
            if _NEXT_PAGE_URL.search(text):
                return "empty listing fabricated a pagination cursor"
            return None
        if not _has_positive_count(text, "items"):
            return "listing variant was neither substantive nor exact empty"
        if error := require_all(
            (
                (
                    "exact listing scope/sort/time",
                    re.compile(rf"(?m)^{re.escape(heading)}$"),
                ),
                ("complete post metadata", _POST_CARD),
                ("canonical discussion URL", _REDDIT_POST_URL),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        return None

    if entry.id.startswith("domain_") and entry.id != "domain_listing":
        heading = entry.required_any[0]
        if entry.allow_empty and re.search(r"(?m)^0 items returned$", text):
            if not re.search(rf"(?m)^{re.escape(heading)}$", text):
                return "empty domain listing lacked exact scope/sort/time"
            if _NEXT_PAGE_URL.search(text):
                return "empty domain listing fabricated a pagination cursor"
            return None
        if not _has_positive_count(text, "items"):
            return "domain listing variant was neither substantive nor exact empty"
        if error := require_all(
            (
                (
                    "exact domain scope/sort/time",
                    re.compile(rf"(?m)^{re.escape(heading)}$"),
                ),
                ("complete domain post metadata", _POST_CARD),
                ("canonical discussion URL", _REDDIT_POST_URL),
                (
                    # Reddit's /domain/ listing includes subdomains, so a page
                    # can legitimately carry only discuss./docs./peps. links.
                    # Anchoring to (?:www\.)? passed only while page one
                    # happened to hold a bare python.org link.
                    "matching outbound domain",
                    re.compile(r"https?://(?:[a-z0-9-]+\.)*python\.org/"),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        return None

    if entry.id == "comments_global":
        if not _has_positive_count(text, "items"):
            return "global comments returned no public comments"
        if error := require_all(
            (
                ("exact global-comments scope", re.compile(r"(?m)^# Reddit · comments$")),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        return _comment_semantic_error(text, activity=True)

    if entry.id in {
        "gilded_global",
        "gilded_comments_global",
        "gilded_subreddit",
        "gilded_comments_subreddit",
        "user_gilded",
    }:
        if not _has_positive_count(text, "items"):
            return "archived gilded surface returned no current items"
        if entry.id == "user_gilded":
            # Only this gilded route is user-scoped. Deriving it inside the
            # mapping literal would call _entry_username() for the global and
            # subreddit feeds too, and it rejects their non-user paths.
            expected_heading = f"# u/{_entry_username(entry)} · gilded"
        else:
            expected_heading = {
                "gilded_global": "# Reddit · gilded",
                "gilded_comments_global": "# Reddit · comments gilded",
                "gilded_subreddit": "# r/Python · gilded",
                "gilded_comments_subreddit": "# r/Python · comments gilded",
            }[entry.id]
        if error := require_all(
            (
                (
                    "exact gilded route identity",
                    re.compile(rf"(?m)^{re.escape(expected_heading)}$"),
                ),
                (
                    "archived ordering/current hydration provenance",
                    re.compile(
                        r"Gilded ordering source: archived Reddit snapshot "
                        r"\(Wayback, \d{4}-\d{2}-\d{2}\)\. "
                        r"Item details: current Reddit API\."
                    ),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        returned = next(
            (
                int(match.group("count").replace(",", ""))
                for match in _POSITIVE_RETURN_COUNT.finditer(text)
                if match.group("label") == "items"
            ),
            0,
        )
        comment_cards = len(_ACTIVITY_COMMENT_HEADING.findall(text))
        post_cards = len(_POST_CARD.findall(text))
        if comment_cards + post_cards != returned:
            return "one or more gilded items lacked substantive current fields"
        archived_evidence = len(
            re.findall(
                r"Archived gilding evidence: (?:Gilded|[1-9][\d,]* "
                r"gildings?) in the exact archived Reddit snapshot",
                text,
            )
        )
        if archived_evidence != returned:
            return "one or more gilded items lacked exact archived award evidence"
        if entry.id in {
            "gilded_comments_global",
            "gilded_comments_subreddit",
        } and comment_cards != returned:
            return "comments-only gilded surface contained a non-comment item"
        if comment_cards:
            return _comment_semantic_error(text, activity=True)
        return None

    if entry.id == "user_overview":
        if not _has_positive_count(text, "items"):
            return "user overview returned no public activity"
        if error := require_all(
            (
                (
                    "exact user overview",
                    re.compile(rf"(?m)^# u/{re.escape(_entry_username(entry))} · overview$"),
                ),
                ("canonical activity URL", _REDDIT_POST_URL),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        returned = next(
            (
                int(match.group("count").replace(",", ""))
                for match in _POSITIVE_RETURN_COUNT.finditer(text)
                if match.group("label") == "items"
            ),
            0,
        )
        comment_cards = len(_ACTIVITY_COMMENT_HEADING.findall(text))
        post_cards = len(_POST_CARD.findall(text))
        if comment_cards + post_cards != returned:
            return "user overview contained a thin or unsupported activity card"
        if comment_cards:
            return _comment_semantic_error(text, activity=True)
        return None

    if entry.id == "user_submitted":
        if not _has_positive_count(text, "items"):
            return "user submitted listing returned no public posts"
        return require_all(
            (
                (
                    "exact submitted route",
                    re.compile(rf"(?m)^# u/{re.escape(_entry_username(entry))} · submitted$"),
                ),
                ("complete submitted post metadata", _POST_CARD),
                ("canonical submitted discussion URL", _REDDIT_POST_URL),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        )

    if entry.id.startswith("subreddit_directory_"):
        if not _has_positive_count(text, "items"):
            return "subreddit directory variant returned no communities"
        heading = entry.required_any[0]
        return require_all(
            (
                (
                    "exact subreddit-directory scope",
                    re.compile(rf"(?m)^{re.escape(heading)}$"),
                ),
                (
                    "community name/title",
                    re.compile(r"(?m)^\d+\. \*\*r/[A-Za-z0-9_]+\*\* — .+"),
                ),
                ("community subscribers", re.compile(r"(?m)^\s+[\d,]+ subscribers$")),
                ("community creation time", re.compile(r"(?m)^\s+Created: .+ UTC$")),
                ("community status", re.compile(_COMMUNITY_STATUS_LINE)),
                (
                    "community navigation",
                    re.compile(r"https://www\.reddit\.com/r/[A-Za-z0-9_]+/"),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        )

    if entry.id.startswith("user_directory_"):
        heading = entry.required_any[0]
        if entry.allow_empty and re.search(r"(?m)^0 users returned$", text):
            if not re.search(rf"(?m)^{re.escape(heading)}$", text):
                return "empty user directory lacked exact route scope"
            if _NEXT_PAGE_URL.search(text):
                return "empty user directory fabricated a pagination cursor"
            return None
        if not _has_positive_count(text, "users"):
            return "user directory variant was neither substantive nor exact empty"
        return require_all(
            (
                (
                    "exact user-directory scope",
                    re.compile(rf"(?m)^{re.escape(heading)}$"),
                ),
                (
                    "user identity and exact karma",
                    re.compile(
                        r"(?m)^\d+\. \*\*u/[A-Za-z0-9_-]+\*\* · "
                        r"[\d,]+ post karma · [\d,]+ comment karma$"
                    ),
                ),
                ("user creation time", re.compile(r"(?m)^\s+Created: .+ UTC$")),
                (
                    "user profile navigation",
                    re.compile(r"https://www\.reddit\.com/user/[A-Za-z0-9_-]+/"),
                ),
                (
                    "public activity navigation",
                    re.compile(
                        r"(?m)^\s+Public activity: "
                        r"https://www\.reddit\.com/user/[A-Za-z0-9_-]+/"
                        r"overview/$"
                    ),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        )

    if entry.id in {"listing", "domain_listing"}:
        if not _has_positive_count(text, "items"):
            return "listing did not return a positive item count"
        heading = (
            r"(?m)^# r/Python · hot$"
            if entry.id == "listing"
            else r"(?m)^# Reddit · domain python\.org · hot$"
        )
        checks = (
            ("route/sort identity", heading),
            ("complete post metadata", _POST_CARD),
            ("canonical discussion URL", _REDDIT_POST_URL),
            ("pagination continuation", _NEXT_PAGE_URL),
        )
        if error := require_all(checks):
            return error
        if entry.id == "domain_listing" and "python.org" not in text:
            return "domain listing lacked a matching outbound domain URL"
        return None

    if entry.id == "thread":
        if error := require_all(
            (
                ("source post metadata", _SOURCE_POST_METADATA),
                (
                    "source discussion URL",
                    re.compile(r"(?m)^\*\*Discussion:\*\* https://www\.reddit\.com/"),
                ),
                ("comment section", re.compile(r"(?m)^## Comments$")),
                ("comment author/score/time", _COMMENT_HEADING),
                ("comment body", _COMMENT_BODY),
                (
                    "nested comment",
                    re.compile(r"(?m)^#{4,6} (?:↳ )+u/"),
                ),
                ("comment permalink", _COMMENT_PERMALINK),
                ("comment parent context", _PARENT_CONTEXT),
            )
        ):
            return error
        return _comment_semantic_error(text, activity=False)

    if entry.id in {"comment_listing", "user_listing"}:
        if not _has_positive_count(text, "items"):
            return "comment activity listing returned no public activity"
        # Derived, not hard-coded: pinning the account here coupled the check to
        # one corpus target, so repointing user_listing (Reddit serves no
        # comments for u/AutoModerator) failed on the *checker* rather than the
        # render. Same defect the spez pins had.
        expected_heading = (
            r"(?m)^# r/Python · comments$"
            if entry.id == "comment_listing"
            else rf"(?m)^# u/{re.escape(_entry_username(entry))} · comments$"
        )
        if error := require_all(
            (
                ("activity owner and route", expected_heading),
                (
                    "comment activity metadata",
                    _ACTIVITY_COMMENT_HEADING,
                ),
                ("comment permalink", _COMMENT_PERMALINK),
                ("comment parent context", _PARENT_CONTEXT),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        return _comment_semantic_error(text, activity=True)

    if entry.id in {"search", "search_communities", "search_users"}:
        if not _has_positive_count(text, "items"):
            return "typed Reddit search returned no results"
        expected_type = {
            "search": "link",
            "search_communities": "sr",
            "search_users": "user",
        }[entry.id]
        if error := require_all(
            (
                (
                    "query/sort/time/type scope",
                    re.compile(
                        rf"(?m)^# .* · sort (?:new|relevance) · "
                        rf"time all · type {expected_type}$"
                    ),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        ):
            return error
        if entry.id == "search":
            return require_all(
                (
                    ("post result metadata", _POST_CARD),
                    ("canonical discussion URL", _REDDIT_POST_URL),
                )
            )
        if entry.id == "search_communities":
            return require_all(
                (
                    (
                        "community name/title",
                        re.compile(r"(?m)^\d+\. \*\*r/[A-Za-z0-9_]+\*\* — .+"),
                    ),
                    ("community subscribers", re.compile(r"(?m)^\s+[\d,]+ subscribers$")),
                    ("community creation time", re.compile(r"(?m)^\s+Created: .+ UTC$")),
                    ("community status", re.compile(_COMMUNITY_STATUS_LINE)),
                    (
                        "community URL",
                        re.compile(r"https://www\.reddit\.com/r/[A-Za-z0-9_]+/"),
                    ),
                )
            )
        return require_all(
            (
                (
                    "user name and karma",
                    re.compile(
                        r"(?m)^\d+\. \*\*u/[A-Za-z0-9_-]+\*\* · "
                        r"[\d,]+ post karma · [\d,]+ comment karma$"
                    ),
                ),
                ("user creation time", re.compile(r"(?m)^\s+Created: .+ UTC$")),
                (
                    "user profile URL",
                    re.compile(r"https://www\.reddit\.com/user/[A-Za-z0-9_-]+/"),
                ),
            )
        )

    if entry.id == "subreddit_about":
        return require_all(
            (
                ("community name/title", re.compile(r"(?s)^# r/Python\n\nPython\n")),
                ("public status", re.compile(r"(?m)^- \*\*Status:\*\* Public$")),
                ("subscriber count", re.compile(r"(?m)^- \*\*Subscribers:\*\* [\d,]+$")),
                ("creation time", re.compile(r"(?m)^- \*\*Created:\*\* .+ UTC$")),
                ("public description", re.compile(r"(?m)^## About$")),
            )
        )

    if entry.id == "rules":
        return require_all(
            (
                ("community rules heading", re.compile(r"(?m)^# Rules for r/Python$")),
                ("numbered rule and body", re.compile(r"(?ms)^## 1\. .+\n\n\S.+")),
                ("site-wide rules", re.compile(r"(?m)^## Reddit-wide rules$")),
            )
        )

    if entry.id == "moderators":
        # Reddit serves no roster to anonymous callers and fetchaller has no
        # credential path, so the only honest public outcome is the gated error.
        return require_all(
            (
                ("account-gated moderator error", "requires a logged-in account"),
                ("anonymous-only statement", "reads Reddit anonymously only"),
                ("no fabrication", "No moderator names were guessed"),
            )
        )

    if entry.id == "wiki_pages":
        if not _has_positive_count(text, "pages"):
            return "wiki page index was empty"
        # The page index is served by New Reddit's own logged-out page tree
        # (SSR, or the anonymous WikiPageRevisionsV2 route) and nothing else.
        return require_all(
            (
                (
                    "wiki page navigation",
                    re.compile(
                        r"(?m)^\d+\. \[.+\]\(https://www\.reddit\.com/"
                        r"r/[A-Za-z0-9_]+/wiki/.+\)$"
                    ),
                ),
            )
        )

    if entry.id == "wiki":
        return require_all(
            (
                ("wiki page identity", re.compile(r"(?m)^# r/Python wiki · index$")),
                (
                    "revision author/time",
                    re.compile(r"(?m)^revised by u/.+ · .+ UTC$"),
                ),
                (
                    "revision identity",
                    re.compile(r"(?m)^\*\*Revision:\*\* [0-9a-f-]{36}$"),
                ),
                ("wiki body", re.compile(r"(?m)^# Welcome to r/Python")),
            )
        )

    if entry.id == "wiki_revisions":
        if not _has_positive_count(text, "revisions"):
            return "wiki revision listing was empty"
        return require_all(
            (
                (
                    "revision page/author/time",
                    re.compile(
                        r"(?m)^\d+\. \*\*.+\*\* · .+ UTC · u/[A-Za-z0-9_-]+$"
                    ),
                ),
                (
                    "revision UUID",
                    re.compile(r"(?m)^\s+revision: [0-9a-f-]{36}$"),
                ),
            )
        )

    if entry.id == "wiki_discussions":
        if not _has_positive_count(text, "items"):
            return "wiki discussion target returned zero linked posts"
        return require_all(
            (
                (
                    "wiki discussion identity",
                    re.compile(r"(?m)^# r/climbharder · wiki discussions$"),
                ),
                ("discussion post metadata", _POST_CARD),
                ("discussion post URL", _REDDIT_POST_URL),
            )
        )

    if entry.id == "wiki_diff":
        return require_all(
            (
                (
                    "from revision identity/author/time",
                    re.compile(
                        r"(?m)^\*\*From:\*\* revision `[0-9a-f-]{36}` · "
                        r"u/[A-Za-z0-9_-]+ · .+ UTC"
                    ),
                ),
                (
                    "to revision identity/author/time",
                    re.compile(
                        r"(?m)^\*\*To:\*\* revision `[0-9a-f-]{36}` · "
                        r"u/[A-Za-z0-9_-]+ · .+ UTC"
                    ),
                ),
                ("bounded unified diff", re.compile(r"(?ms)^```diff\n--- revision .+\n\+\+\+ revision .+\n@@")),
            )
        )

    if entry.id in {"user_profile", "user_about"}:
        checks: tuple[tuple[str, str | re.Pattern[str]], ...] = (
            ("user identity", re.compile(rf"(?m)^# u/{re.escape(_entry_username(entry))}$")),
            ("post karma", re.compile(r"(?m)^- \*\*Post karma:\*\* [\d,]+$")),
            ("comment karma", re.compile(r"(?m)^- \*\*Comment karma:\*\* [\d,]+$")),
            ("creation time", re.compile(r"(?m)^- \*\*Created:\*\* .+ UTC$")),
            (
                "canonical user profile",
                re.compile(
                    r"(?m)^- \*\*Profile:\*\* https://www\.reddit\.com/user/"
                    rf"{re.escape(_entry_username(entry))}/$"
                ),
            ),
        )
        if error := require_all(checks):
            return error
        if entry.id == "user_profile":
            return require_all(
                (
                    (
                        "recent public activity",
                        re.compile(
                            r"(?ms)^## Recent activity\n\n"
                            r"(?:(?!^## ).)*?(?:"
                            r"^\d+\. \S.+\n\s+.*\bscore\b|"
                            r"^\d+\. \*\*\S.+\*\*\n\s+r/)"
                        ),
                    ),
                    ("activity permalink", _REDDIT_POST_URL),
                    (
                        "public trophies",
                        re.compile(
                            r"(?ms)^## Trophies\n\n"
                            r"(?:(?!^## ).)*?^\d+\. \*\*\S.+\*\*"
                        ),
                    ),
                    (
                        "public multireddits",
                        re.compile(
                            r"(?ms)^## Public multireddits\n\n"
                            r"(?:(?!^## ).)*?^\d+\. \*\*\S.+\*\*"
                        ),
                    ),
                    (
                        "moderated communities",
                        re.compile(
                            r"(?ms)^## Moderated communities\n\n"
                            r"(?:(?!^## ).)*?^\d+\. \*\*r/[A-Za-z0-9_]+\*\*"
                        ),
                    ),
                    ("pagination continuation", _NEXT_PAGE_URL),
                )
            )
        return None

    if entry.id == "trophies":
        if not _has_positive_count(text, "trophies"):
            return "public trophy roster was empty"
        return require_all(
            (
                (
                    "trophy owner",
                    re.compile(rf"(?m)^# Trophy case for u/{re.escape(_entry_username(entry))}$"),
                ),
                ("trophy name", re.compile(r"(?m)^\d+\. \*\*.+\*\*$")),
                (
                    "trophy description/date/icon",
                    re.compile(r"(?m)^\s+(?:Granted: .+ UTC|https://\S+)$"),
                ),
            )
        )

    if entry.id in {"multi_about", "multi_profile"}:
        if error := require_all(
            (
                (
                    "multireddit owner/visibility/subscribers/created",
                    re.compile(
                        r"(?m)^owner u/[A-Za-z0-9_-]+ · public · "
                        r"[\d,]+ subscribers · created .+ UTC$"
                    ),
                ),
                ("member communities", re.compile(r"(?m)^## Communities$")),
                (
                    "member community URL",
                    re.compile(r"https://www\.reddit\.com/r/[A-Za-z0-9_]+/"),
                ),
            )
        ):
            return error
        if entry.id == "multi_profile":
            return require_all(
                (
                    ("typed multireddit feed", re.compile(r"(?m)^## Feed$")),
                    ("feed post metadata", _POST_CARD),
                    ("feed discussion URL", _REDDIT_POST_URL),
                    ("pagination continuation", _NEXT_PAGE_URL),
                )
            )
        return None

    if entry.id in {
        "duplicates",
        "duplicates_subreddit",
        "related",
        "related_subreddit",
    }:
        if not _has_positive_count(text, "items"):
            return f"{entry.id} returned no public posts"
        section = (
            "Other discussions"
            if entry.id.startswith("duplicates")
            else "Related posts"
        )
        return require_all(
            (
                ("source post metadata", _SOURCE_POST_METADATA),
                (
                    "source discussion URL",
                    re.compile(r"(?m)^\*\*Discussion:\*\* https://www\.reddit\.com/"),
                ),
                (
                    f"{entry.id} section",
                    re.compile(rf"(?m)^## {re.escape(section)}$"),
                ),
                ("result post metadata", _POST_CARD),
                ("result discussion URL", _REDDIT_POST_URL),
            )
        )

    if entry.id == "morechildren":
        if not _has_positive_count(text, "comments"):
            return "morechildren returned no requested replies"
        if error := require_all(
            (
                ("expanded comment author/score/time/body", _COMMENT_HEADING),
                ("expanded comment body", _COMMENT_BODY),
                ("expanded comment permalink", _COMMENT_PERMALINK),
                ("expanded comment parent context", _PARENT_CONTEXT),
            )
        ):
            return error
        return _comment_semantic_error(text, activity=False)

    if entry.id == "subreddit_directory":
        if not _has_positive_count(text, "items"):
            return "subreddit directory returned no communities"
        return require_all(
            (
                (
                    "community name/title",
                    re.compile(r"(?m)^\d+\. \*\*r/[A-Za-z0-9_]+\*\* — .+"),
                ),
                ("community subscribers", re.compile(r"(?m)^\s+[\d,]+ subscribers$")),
                ("community creation time", re.compile(r"(?m)^\s+Created: .+ UTC$")),
                ("community status", re.compile(_COMMUNITY_STATUS_LINE)),
                (
                    "community navigation",
                    re.compile(r"https://www\.reddit\.com/r/[A-Za-z0-9_]+/"),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        )

    if entry.id == "user_directory":
        if not _has_positive_count(text, "users"):
            return "user directory returned no users"
        return require_all(
            (
                (
                    "user identity and karma",
                    re.compile(
                        r"(?m)^\d+\. \*\*u/[A-Za-z0-9_-]+\*\* · "
                        r"[\d,]+ post karma · [\d,]+ comment karma$"
                    ),
                ),
                ("user creation time", re.compile(r"(?m)^\s+Created: .+ UTC$")),
                (
                    "user navigation",
                    re.compile(r"https://www\.reddit\.com/user/[A-Za-z0-9_-]+/"),
                ),
                (
                    "public activity navigation",
                    re.compile(
                        r"(?m)^\s+Public activity: "
                        r"https://www\.reddit\.com/user/[A-Za-z0-9_-]+/"
                        r"overview/$"
                    ),
                ),
                ("pagination continuation", _NEXT_PAGE_URL),
            )
        )

    if entry.id in {"live", "live_about"}:
        checks = (
            ("live title", re.compile(r"(?m)^# \S.+$")),
            ("live state", re.compile(r"(?m)^\*\*State:\*\* \S+$")),
            ("live description", re.compile(r"(?ms)^\*\*State:\*\* .+\n\n\S.+")),
            (
                "canonical live root",
                re.compile(
                    r"(?m)^\*\*Live thread:\*\* "
                    r"https://www\.reddit\.com/live/[A-Za-z0-9]{2,16}/$"
                ),
            ),
        )
        if error := require_all(checks):
            return error
        if entry.id == "live":
            return require_all(
                (
                    ("live updates section", re.compile(r"(?m)^## Updates$")),
                    (
                        "live update author/time",
                        re.compile(
                            r"(?m)^\d+\. \*\*u/[A-Za-z0-9_-]+ · .+ UTC\*\*$"
                        ),
                    ),
                    ("live update body", _LIVE_UPDATE_BODY),
                    ("live update identity", re.compile(r"(?m)^Update ID: [0-9a-f-]{36}$")),
                    ("pagination continuation", _NEXT_PAGE_URL),
                )
            )
        return require(
            "exact about route",
            re.compile(
                r"(?m)^\*\*Requested route:\*\* "
                r"https://www\.reddit\.com/live/[A-Za-z0-9]{2,16}/about/$"
            ),
        )

    if entry.id == "live_contributors":
        if not _has_positive_count(text, "contributors"):
            return "live contributor roster was empty"
        return require_all(
            (
                (
                    "canonical live root",
                    re.compile(r"(?m)^\*\*Live thread:\*\* https://www\.reddit\.com/live/.+/$"),
                ),
                (
                    "exact contributors route",
                    re.compile(r"(?m)^\*\*Requested route:\*\* https://www\.reddit\.com/live/.+/contributors/$"),
                ),
                (
                    "contributor identity/navigation",
                    re.compile(
                        r"(?m)^\d+\. \*\*u/[A-Za-z0-9_-]+\*\*\n"
                        r"\s+https://www\.reddit\.com/user/[A-Za-z0-9_-]+/$"
                    ),
                ),
            )
        )

    if entry.id == "live_update":
        if not _has_positive_count(text, "updates"):
            return "focused live update was empty"
        return require_all(
            (
                (
                    "canonical live root",
                    re.compile(r"(?m)^\*\*Live thread:\*\* https://www\.reddit\.com/live/.+/$"),
                ),
                (
                    "exact update route",
                    re.compile(r"(?m)^\*\*Requested route:\*\* https://www\.reddit\.com/live/.+/updates/.+/$"),
                ),
                (
                    "update author/time",
                    re.compile(r"(?m)^\d+\. \*\*u/[A-Za-z0-9_-]+ · .+ UTC\*\*$"),
                ),
                ("update body", _LIVE_UPDATE_BODY),
                ("update identity", re.compile(r"(?m)^Update ID: [0-9a-f-]{36}$")),
            )
        )

    if entry.id == "collection":
        count_match = _COLLECTION_ITEMS.search(text)
        post_urls = _REDDIT_POST_URL.findall(text)
        if count_match is None or int(count_match.group(1).replace(",", "")) != 28:
            return "official archived collection did not return all 28 posts"
        if len(post_urls) != 28 or len(set(post_urls)) != 28:
            return "collection did not render 28 unique current post URLs"
        if len(_POST_CARD.findall(text)) != 28:
            return "collection did not render 28 substantive current post cards"
        return require_all(
            (
                (
                    "archived metadata provenance/date",
                    re.compile(
                        r"Metadata source: archived New Reddit snapshot "
                        r"\(Wayback, \d{4}-\d{2}-\d{2}\)\."
                    ),
                ),
                ("current post provenance", "Post details: current Reddit API."),
                ("ordered posts section", re.compile(r"(?m)^## Posts$")),
                ("post metadata", _POST_CARD),
            )
        )

    if entry.id in {"browse_page_1", "search_page_1"}:
        expected_heading = (
            r"(?m)^r/Python · new · 1 posts$"
            if entry.id == "browse_page_1"
            else (
                r'(?m)^Search: "asyncio" in r/Python · '
                r"new · all · 1 results$"
            )
        )
        return require_all(
            (
                ("tool query/sort/time/limit scope", re.compile(expected_heading)),
                ("tool post metadata", _POST_CARD),
                ("tool discussion URL", _REDDIT_POST_URL),
                (
                    "validated tool cursor",
                    re.compile(
                        r"(?m)^\[Next page: after="
                        r"t[1-6]_[A-Za-z0-9]{2,16}\]$"
                    ),
                ),
            )
        )

    return f"semantic validator for {entry.id} did not evaluate the entry"


async def _validate_fetch_pagination_round_trip(
    session: ClientSession,
    entry: CorpusEntry,
    directory: Path,
    stage: str,
    *,
    first_page_items: set[str],
    next_page_items: set[str],
    next_text: str,
    next_url: str,
    first_cursor: str,
    first_count: int,
) -> str | None:
    """Prove p1 → p2 → Previous → Next is lossless on a live mapped route."""

    previous_page = _validated_fetch_previous_page(next_text, next_url)
    if previous_page is None:
        return "pagination round trip lacked one valid Previous link"
    previous_url, _previous_cursor, previous_count = previous_page
    expected_previous_count = max(0, first_count - len(first_page_items))
    if previous_count != expected_previous_count:
        return (
            "pagination round-trip Previous count was "
            f"{previous_count}, expected {expected_previous_count}"
        )
    previous_result = await session.call_tool(
        "fetch",
        {"url": previous_url, "raw": entry.raw, "timeout": 90},
    )
    previous_text = _text(previous_result)
    _write_artifact(
        directory,
        stage,
        f"{entry.id}-previous",
        previous_text,
    )
    if previous_result.isError or not previous_text.strip():
        return "pagination round-trip Previous request failed"
    if match := _BLOCKED_RESPONSE.search(previous_text):
        return (
            "pagination round-trip Previous request was blocked/challenged "
            f"({match.group(0)!r})"
        )
    if match := _LOGIN_PAGE.search(previous_text):
        return (
            "pagination round-trip Previous request returned a login page "
            f"({match.group(0)!r})"
        )
    if _TRUNCATION_MARKER.search(previous_text):
        return "pagination round-trip Previous response was truncated"
    if match := _NAMED_PARTIAL_FAILURE.search(previous_text):
        return (
            "pagination round-trip Previous reported a partial failure "
            f"({match.group(0).strip()!r})"
        )
    previous_items = _page_item_identities(previous_text)
    if previous_items != first_page_items:
        return "pagination round-trip Previous did not recover page-one identities"
    if error := _semantic_contract_error(entry, previous_text):
        return f"pagination round-trip Previous failed semantics: {error}"
    restored_next = _validated_fetch_next_page(previous_text, previous_url)
    if restored_next is None:
        return "pagination round-trip Previous omitted its restored Next link"
    restored_url, restored_cursor, restored_count = restored_next
    if restored_cursor != first_cursor or restored_count != first_count:
        return (
            "pagination round-trip restored Next cursor/count did not match "
            "the original page-one continuation"
        )
    restored_result = await session.call_tool(
        "fetch",
        {"url": restored_url, "raw": entry.raw, "timeout": 90},
    )
    restored_text = _text(restored_result)
    _write_artifact(
        directory,
        stage,
        f"{entry.id}-round-trip-after",
        restored_text,
    )
    if restored_result.isError or not restored_text.strip():
        return "pagination round-trip restored Next request failed"
    if match := _BLOCKED_RESPONSE.search(restored_text):
        return (
            "pagination round-trip restored Next was blocked/challenged "
            f"({match.group(0)!r})"
        )
    if match := _LOGIN_PAGE.search(restored_text):
        return (
            "pagination round-trip restored Next returned a login page "
            f"({match.group(0)!r})"
        )
    if _TRUNCATION_MARKER.search(restored_text):
        return "pagination round-trip restored Next response was truncated"
    if match := _NAMED_PARTIAL_FAILURE.search(restored_text):
        return (
            "pagination round-trip restored Next reported a partial failure "
            f"({match.group(0).strip()!r})"
        )
    if _page_item_identities(restored_text) != next_page_items:
        return "pagination round-trip restored Next did not recover page-two identities"
    if error := _semantic_contract_error(entry, restored_text):
        return f"pagination round-trip restored Next failed semantics: {error}"
    return None


async def _call(session: ClientSession, entry: CorpusEntry, directory: Path, stage: str) -> Evidence:
    if entry.tool:
        result = await session.call_tool(entry.tool, entry.arguments or {})
    else:
        arguments: dict[str, object] = {
            "url": entry.url,
            "raw": entry.raw,
            "timeout": 90,
        }
        # Raw New Reddit places its semantic application tree after a large
        # document head.  The normal 25k-token fetch budget can truncate before
        # that tree and produce a false negative, so the live evidence runner
        # requests a deliberately high bounded representation.
        if entry.raw:
            arguments["maxTokens"] = 250_000
        result = await session.call_tool(
            "fetch",
            arguments,
        )
    text = _text(result)
    evidence = _body_evidence(entry, stage, directory, text)
    blocked_match = _BLOCKED_RESPONSE.search(text)
    if blocked_match:
        evidence.detail = (
            f"blocked/challenge response ({blocked_match.group(0)!r})"
        )
        return evidence
    login_match = _LOGIN_PAGE.search(text)
    if login_match:
        evidence.detail = (
            f"login page response ({login_match.group(0)!r})"
        )
        return evidence
    if _TRUNCATION_MARKER.search(text):
        evidence.detail = "response ended at the fetch truncation marker"
        return evidence
    partial_failure = _NAMED_PARTIAL_FAILURE.search(text)
    if partial_failure:
        evidence.detail = (
            "composite route reported a partial failure "
            f"({partial_failure.group(0).strip()!r})"
        )
        return evidence
    if entry.expect_error:
        if not result.isError or not text.strip():
            evidence.detail = "expected an MCP access-state error"
            return evidence
        expected_error = _EXPECTED_LIVE_ACCESS_ERRORS.get(entry.id)
        if (
            entry.access_state != "private"
            or expected_error is None
            or text.strip() != expected_error
        ):
            evidence.detail = "access-state error did not match its exact contract"
            return evidence
        evidence.status = "passed"
        evidence.detail = "exact expected access-state error"
        return evidence
    if result.isError or not text.strip():
        evidence.detail = "MCP isError or empty content"
        return evidence
    if not any(marker.casefold() in text.casefold() for marker in entry.required_any):
        evidence.detail = "missing expected semantic marker: " + " | ".join(entry.required_any)
        return evidence
    if entry.kind == "explicit_json":
        if not _valid_explicit_reddit_listing(text):
            evidence.detail = (
                "explicit JSON lacked a nonempty coherent Reddit post listing"
            )
            return evidence
    if (
        entry.kind in _POST_LISTING_KINDS
        and entry.id not in {"search_communities", "search_users"}
        and not entry.allow_empty
        and not entry.raw
        and _REDDIT_POST_URL.search(text) is None
    ):
        evidence.detail = "known-nonempty listing lacked a canonical Reddit post"
        return evidence
    if entry.kind == "html_fallback" and not (
        "# Bringing you the best of Reddit!" in text
        and "Ads-free browsing" in text
        and "Higher Rate Limits" in text
    ):
        evidence.detail = (
            "HTML fallback lacked substantive public Reddit Premium content"
        )
        return evidence
    if entry.kind == "collection":
        required = (
            "Metadata source: archived New Reddit snapshot",
            "Post details: current Reddit API.",
            "## Posts",
        )
        if not all(marker in text for marker in required):
            evidence.detail = (
                "collection lacked explicit archived-metadata/current-post "
                "provenance"
            )
            return evidence
        count_match = _COLLECTION_ITEMS.search(text)
        if (
            count_match is None
            or int(count_match.group(1).replace(",", "")) <= 0
            or _REDDIT_POST_URL.search(text) is None
        ):
            evidence.detail = (
                "collection lacked non-empty current Reddit post hydration"
            )
            return evidence
    if entry.raw and not _valid_raw_new_reddit_html(text):
        evidence.detail = "raw HTML was not a semantic New Reddit app/content tree"
        return evidence
    first_page_items = _page_item_identities(text)
    if entry.pagination and not first_page_items:
        evidence.detail = "pagination first page lacks primary item identities"
        return evidence
    semantic_error = _semantic_contract_error(entry, text)
    if semantic_error is not None:
        evidence.detail = semantic_error
        return evidence
    evidence.status, evidence.detail = "passed", "useful non-error MCP content"
    if entry.pagination:
        tool_cursor = _validated_tool_next_cursor(text) if entry.tool else None
        fetch_next = (
            _validated_fetch_next_page(text, entry.url or "")
            if entry.tool is None
            else None
        )
        if tool_cursor is None and fetch_next is None:
            evidence.status, evidence.detail = (
                "failed",
                "missing validated pagination cursor",
            )
        elif (
            fetch_next is not None
            and fetch_next[2] != len(first_page_items)
        ):
            evidence.status, evidence.detail = (
                "failed",
                "pagination count does not equal first-page item count",
            )
        else:
            if entry.tool:
                assert tool_cursor is not None
                first_cursor = tool_cursor.group(1)
                follow_tool = entry.tool
                follow = dict(entry.arguments or {}, after=first_cursor)
            else:
                assert fetch_next is not None
                next_url, first_cursor, first_count = fetch_next
                follow_tool = "fetch"
                follow = {
                    "url": next_url,
                    "raw": entry.raw,
                    "timeout": 90,
                }
                if entry.raw:
                    follow["maxTokens"] = 250_000
            next_result = await session.call_tool(follow_tool, follow)
            next_text = _text(next_result)
            followup_artifact = _write_artifact(
                directory, stage, f"{entry.id}-after", next_text
            )
            evidence.followup_chars = len(next_text)
            evidence.followup_sha256 = hashlib.sha256(next_text.encode()).hexdigest()
            evidence.followup_artifact = followup_artifact
            followup_blocked = _BLOCKED_RESPONSE.search(next_text)
            followup_login = _LOGIN_PAGE.search(next_text)
            if next_result.isError or not next_text.strip():
                evidence.status, evidence.detail = "failed", "pagination follow-up failed"
            elif followup_blocked:
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination blocked/challenge response "
                    f"({followup_blocked.group(0)!r})",
                )
            elif followup_login:
                evidence.status, evidence.detail = (
                    "failed",
                    f"pagination login page response ({followup_login.group(0)!r})",
                )
            elif _TRUNCATION_MARKER.search(next_text):
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination follow-up response was truncated",
                )
            elif (
                followup_partial_failure
                := _NAMED_PARTIAL_FAILURE.search(next_text)
            ):
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination follow-up reported a partial failure "
                    f"({followup_partial_failure.group(0).strip()!r})",
                )
            elif not any(
                marker.casefold() in next_text.casefold()
                for marker in entry.required_any
            ):
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination follow-up missing expected semantic marker",
                )
            elif not (next_page_items := _page_item_identities(next_text)):
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination follow-up lacks primary item identities",
                )
            elif evidence.followup_sha256 == evidence.sha256:
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination follow-up repeated the first page body",
                )
            elif (
                first_page_items.intersection(next_page_items)
                if entry.id not in _VELOCITY_RANKED_FEEDS
                # Re-ranking feeds may legitimately carry an item across the
                # page boundary, but page two must still bring something new.
                # Dropping the check outright let a cursor regression return
                # page one again and pass whenever scores/timestamps shifted.
                else not (next_page_items - first_page_items)
            ):
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination follow-up repeated one or more first-page items",
                )
            elif (
                followup_semantic_error := _semantic_contract_error(
                    entry,
                    next_text,
                )
            ) is not None:
                evidence.status, evidence.detail = (
                    "failed",
                    "pagination follow-up failed semantics: "
                    f"{followup_semantic_error}",
                )
            else:
                next_cursor = (
                    _validated_tool_next_cursor(next_text)
                    if entry.tool
                    else _validated_fetch_next_page(next_text, next_url)
                )
                advanced_cursor = (
                    next_cursor.group(1)
                    if isinstance(next_cursor, re.Match)
                    else next_cursor[1]
                    if next_cursor is not None
                    else None
                )
                if advanced_cursor is None or advanced_cursor == first_cursor:
                    evidence.status, evidence.detail = (
                        "failed",
                        "pagination follow-up did not advance to a new cursor",
                    )
                elif (
                    not entry.tool
                    and next_cursor is not None
                    and next_cursor[2]
                    != first_count + len(next_page_items)
                ):
                    evidence.status, evidence.detail = (
                        "failed",
                        "pagination follow-up item count did not advance exactly",
                    )
                elif entry.pagination_round_trip:
                    round_trip_error = (
                        await _validate_fetch_pagination_round_trip(
                            session,
                            entry,
                            directory,
                            stage,
                            first_page_items=first_page_items,
                            next_page_items=next_page_items,
                            next_text=next_text,
                            next_url=next_url,
                            first_cursor=first_cursor,
                            first_count=first_count,
                        )
                    )
                    if round_trip_error is not None:
                        evidence.status, evidence.detail = (
                            "failed",
                            round_trip_error,
                        )
    return evidence


async def _verify_tool_surface(session: ClientSession, stage: str) -> Evidence:
    """Assert the exact public MCP surface for every fresh server process."""

    tools = await session.list_tools()
    names = [tool.name for tool in tools.tools]
    if names != EXPECTED_TOOLS:
        return Evidence(
            "tool_surface",
            stage,
            "failed",
            f"expected {EXPECTED_TOOLS!r}, got {names!r}",
        )
    return Evidence("tool_surface", stage, "passed", "exact ten-tool MCP surface")


class _Pacer:
    """One request cadence shared by all stages and pagination follow-ups."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last_started: float | None = None

    async def wait(self) -> None:
        if self._last_started is not None:
            remaining = self.interval - (time.monotonic() - self._last_started)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_started = time.monotonic()


class _PacedSession:
    """Proxy that makes every network tool call obey the shared cadence."""

    def __init__(self, session: ClientSession, pacer: _Pacer) -> None:
        self._session = session
        self._pacer = pacer

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self._pacer.wait()
        return await self._session.call_tool(name, arguments)

    async def list_tools(self) -> Any:
        return await self._session.list_tools()


async def _discovery_call(
    session: ClientSession,
    directory: Path,
    discovery_id: str,
    tool: str,
    arguments: dict[str, Any],
) -> tuple[str | None, Evidence]:
    """Run and persist one real discovery call; never hide a failed source."""

    result = await session.call_tool(tool, arguments)
    text = _text(result)
    entry = CorpusEntry(
        id=f"discovery_{discovery_id}",
        live="unstable",
        tool=tool,
        arguments=arguments,
        required_any=("discovery",),
    )
    evidence = _body_evidence(entry, "discovery", directory, text)
    if result.isError or not text.strip():
        evidence.detail = "MCP isError or empty discovery response"
        return None, evidence
    blocked = _BLOCKED_RESPONSE.search(text)
    login = _LOGIN_PAGE.search(text)
    if blocked:
        evidence.detail = f"blocked discovery response ({blocked.group(0)!r})"
        return None, evidence
    if login:
        evidence.detail = f"login discovery response ({login.group(0)!r})"
        return None, evidence
    if _TRUNCATION_MARKER.search(text):
        evidence.detail = "discovery response ended at the fetch truncation marker"
        return None, evidence
    if partial_failure := _NAMED_PARTIAL_FAILURE.search(text):
        evidence.detail = (
            "discovery response reported a partial failure "
            f"({partial_failure.group(0).strip()!r})"
        )
        return None, evidence
    evidence.status = "passed"
    evidence.detail = "real discovery source returned useful MCP content"
    return text, evidence


def _walk_json(value: object):
    pending = [value]
    while pending:
        current = pending.pop()
        yield current
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


async def _discover_live_targets(
    session: ClientSession,
    directory: Path,
) -> tuple[dict[str, str], list[Evidence]]:
    """Discover every opaque public-read target from current real evidence."""

    targets: dict[str, str] = {}
    records: list[Evidence] = []

    seed_text, evidence = await _discovery_call(
        session,
        directory,
        "current_post_listing",
        "fetch",
        {
            "url": (
                "https://www.reddit.com/r/AskReddit/hot.json?"
                "limit=25&raw_json=1"
            ),
            "maxTokens": 250_000,
            "timeout": 90,
        },
    )
    records.append(evidence)
    selected_post: dict[str, Any] | None = None
    if seed_text is not None:
        try:
            payload = json.loads(seed_text)
        except ValueError:
            payload = None
        children = (
            payload.get("data", {}).get("children")
            if isinstance(payload, dict)
            and isinstance(payload.get("data"), dict)
            else None
        )
        candidates = [
            child["data"]
            for child in children or []
            if isinstance(child, dict)
            and child.get("kind") == "t3"
            and isinstance(child.get("data"), dict)
            and isinstance(child["data"].get("id"), str)
            and isinstance(child["data"].get("permalink"), str)
        ]
        if candidates:
            selected_post = max(
                candidates,
                key=lambda post: (
                    post.get("num_comments")
                    if isinstance(post.get("num_comments"), (int, float))
                    else -1
                ),
            )
            post_id = selected_post["id"]
            permalink = selected_post["permalink"].split("?")[0]
            post_url = f"https://www.reddit.com{permalink}"
            match = _DYNAMIC_POST_URL.match(post_url)
            if match is not None:
                thread_url = match.group(0)
                slug = permalink.rstrip("/").rsplit("/", 1)[-1]
                targets.update(
                    {
                        "thread": thread_url,
                        "thread_global_permalink": (
                            f"https://www.reddit.com/comments/{post_id}/"
                            f"{slug}/?limit=2"
                        ),
                    }
                )

    if selected_post is not None and "thread" in targets:
        thread_json, evidence = await _discovery_call(
            session,
            directory,
            "current_thread_json",
            "fetch",
            {
                "url": (
                    targets["thread"]
                    + ".json?limit=500&depth=10&raw_json=1"
                ),
                "maxTokens": 250_000,
                "timeout": 90,
            },
        )
        records.append(evidence)
        if thread_json is not None:
            try:
                thread_payload = json.loads(thread_json)
            except ValueError:
                thread_payload = None
            for current in _walk_json(thread_payload):
                if not isinstance(current, dict):
                    continue
                current_data = current.get("data")
                if not isinstance(current_data, dict):
                    continue
                if (
                    "listing_by_id_comment" not in targets
                    and current.get("kind") == "t1"
                    and isinstance(current_data.get("id"), str)
                    and re.fullmatch(
                        r"[A-Za-z0-9]{2,16}",
                        current_data["id"],
                    )
                ):
                    comment_fullname = f"t1_{current_data['id']}"
                    targets["listing_by_id_comment"] = (
                        "https://www.reddit.com/by_id/"
                        f"{comment_fullname}/"
                    )
                    targets["listing_by_id_mixed"] = (
                        "https://www.reddit.com/by_id/"
                        f"{comment_fullname},t3_{selected_post['id']}/"
                    )
                if (
                    current.get("kind") != "more"
                    or "morechildren" in targets
                ):
                    continue
                child_ids = current_data.get("children")
                if not (
                    isinstance(child_ids, list)
                    and child_ids
                    and all(
                        isinstance(child_id, str)
                        and re.fullmatch(r"[A-Za-z0-9]{2,16}", child_id)
                        for child_id in child_ids[:20]
                    )
                ):
                    continue
                targets["morechildren"] = (
                    "https://www.reddit.com/api/morechildren?"
                    + urlencode(
                        {
                            "link_id": f"t3_{selected_post['id']}",
                            "children": ",".join(child_ids[:20]),
                            "sort": "confidence",
                            "depth": "10",
                            "limit_children": "true",
                        }
                    )
                )

    related_seed, evidence = await _discovery_call(
        session,
        directory,
        "current_link_post_listing",
        "fetch",
        {
            "url": (
                "https://www.reddit.com/r/worldnews/hot.json?"
                "limit=25&raw_json=1"
            ),
            "maxTokens": 250_000,
            "timeout": 90,
        },
    )
    records.append(evidence)
    selected_link: dict[str, Any] | None = None
    if related_seed is not None:
        try:
            related_payload = json.loads(related_seed)
        except ValueError:
            related_payload = None
        related_children = (
            related_payload.get("data", {}).get("children")
            if isinstance(related_payload, dict)
            and isinstance(related_payload.get("data"), dict)
            else None
        )
        link_candidates = [
            child["data"]
            for child in related_children or []
            if isinstance(child, dict)
            and child.get("kind") == "t3"
            and isinstance(child.get("data"), dict)
            and child["data"].get("is_self") is False
            and isinstance(child["data"].get("id"), str)
            and re.fullmatch(
                r"[A-Za-z0-9]{2,16}",
                child["data"]["id"],
            )
            and isinstance(child["data"].get("subreddit"), str)
            and re.fullmatch(
                r"[A-Za-z0-9_]{1,21}",
                child["data"]["subreddit"],
            )
            and isinstance(child["data"].get("permalink"), str)
        ]
        if link_candidates:
            ordered_candidates = sorted(
                link_candidates,
                key=lambda post: (
                    post.get("num_comments")
                    if isinstance(post.get("num_comments"), (int, float))
                    else -1
                ),
                reverse=True,
            )
            # Pick a link that actually HAS crossposts. Selecting purely by
            # comment count regularly chose a post with none, and the
            # duplicates routes then rendered a correct "0 items returned"
            # that the gate reported as "returned no public posts" -- a
            # discovery defect scored as a product failure. Probe directly
            # rather than through _discovery_call so the fixed discovery
            # record shape is unchanged; fall back to the ranked pick so a
            # probe outage can never leave the routes unrun.
            selected_link = ordered_candidates[0]
            for candidate in ordered_candidates[:4]:
                probe = await session.call_tool(
                    "fetch",
                    {
                        "url": (
                            "https://www.reddit.com/duplicates/"
                            f"{candidate['id']}/?limit=5"
                        ),
                        "timeout": 60,
                    },
                )
                probe_text = _text(probe)
                if "## Other discussions" in probe_text and not re.search(
                    r"(?m)^0 items returned$", probe_text
                ):
                    selected_link = candidate
                    break
                await asyncio.sleep(6.0)
    if selected_link is None:
        evidence.status = "failed"
        evidence.detail = "current link listing had no exact public link post"
    else:
        link_id = selected_link["id"]
        link_subreddit = selected_link["subreddit"]
        link_slug = selected_link["permalink"].rstrip("/").rsplit("/", 1)[-1]
        targets.update(
            {
                "duplicates": (
                    f"https://www.reddit.com/duplicates/{link_id}/?limit=5"
                ),
                "duplicates_subreddit": (
                    f"https://www.reddit.com/r/{link_subreddit}/duplicates/"
                    f"{link_id}/{link_slug}/?limit=5"
                ),
                "related": (
                    f"https://www.reddit.com/related/{link_id}/?limit=5"
                ),
                "related_subreddit": (
                    f"https://www.reddit.com/r/{link_subreddit}/related/"
                    f"{link_id}/{link_slug}/?limit=5"
                ),
            }
        )

    gallery_seed, evidence = await _discovery_call(
        session,
        directory,
        "current_gallery_listing",
        "fetch",
        {
            "url": (
                "https://www.reddit.com/r/pics/new.json?"
                "limit=100&raw_json=1"
            ),
            "maxTokens": 250_000,
            "timeout": 90,
        },
    )
    records.append(evidence)
    gallery_post: dict[str, Any] | None = None
    if gallery_seed is not None:
        try:
            gallery_payload = json.loads(gallery_seed)
        except ValueError:
            gallery_payload = None
        gallery_children = (
            gallery_payload.get("data", {}).get("children")
            if isinstance(gallery_payload, dict)
            and isinstance(gallery_payload.get("data"), dict)
            else None
        )
        gallery_candidates = [
            child["data"]
            for child in gallery_children or []
            if isinstance(child, dict)
            and child.get("kind") == "t3"
            and isinstance(child.get("data"), dict)
            and child["data"].get("is_gallery") is True
            and isinstance(child["data"].get("id"), str)
            and re.fullmatch(
                r"[A-Za-z0-9]{2,16}",
                child["data"]["id"],
            )
            and isinstance(child["data"].get("gallery_data"), dict)
            and isinstance(
                child["data"]["gallery_data"].get("items"),
                list,
            )
            and len(child["data"]["gallery_data"]["items"]) >= 2
        ]
        if gallery_candidates:
            gallery_post = max(
                gallery_candidates,
                key=lambda post: len(
                    post["gallery_data"]["items"]
                ),
            )
    if gallery_post is None:
        evidence.status = "failed"
        evidence.detail = "current gallery listing had no multi-item gallery"
    else:
        targets["thread_gallery"] = (
            f"https://www.reddit.com/gallery/{gallery_post['id']}"
            "?limit=2&depth=1&sort=new"
        )

    revisions, evidence = await _discovery_call(
        session,
        directory,
        "current_wiki_revisions",
        "fetch",
        {
            "url": "https://www.reddit.com/r/Python/wiki/revisions/index/",
            "maxTokens": 25_000,
            "timeout": 90,
        },
    )
    records.append(evidence)
    revision_ids = list(dict.fromkeys(_REVISION_ID.findall(revisions or "")))
    if len(revision_ids) >= 2:
        targets["wiki_diff"] = (
            "https://www.reddit.com/r/Python/wiki/index/?"
            + urlencode({"v": revision_ids[0], "v2": revision_ids[1]})
        )

    multi_search, evidence = await _discovery_call(
        session,
        directory,
        "current_public_multireddit",
        "search",
        {
            "query": "site:reddit.com/user/*/m/ reddit multireddit",
            "page": 1,
        },
    )
    records.append(evidence)
    multi_match = _DYNAMIC_MULTI_URL.search(multi_search or "")
    if multi_match is not None:
        multi_base = multi_match.group(0)
        targets["multi_profile"] = multi_base
        targets["multi_about"] = f"{multi_base}about/"

    live_search, evidence = await _discovery_call(
        session,
        directory,
        "current_live_update",
        "search",
        {
            "query": "site:reddit.com/live/*/updates/ reddit",
            "page": 1,
        },
    )
    records.append(evidence)
    live_match = _DYNAMIC_LIVE_UPDATE_URL.search(live_search or "")
    if live_match is not None:
        live_base = (
            f"https://www.reddit.com/live/{live_match.group('thread')}/"
        )
        targets.update(
            {
                "live": live_base,
                "live_about": f"{live_base}about/",
                "live_contributors": f"{live_base}contributors/",
                "live_update": live_match.group(0).rstrip("/") + "/",
            }
        )

    collection_source, evidence = await _discovery_call(
        session,
        directory,
        "official_deprecated_collection",
        "fetch",
        {
            "url": (
                "https://www.reddit.com/r/modnews/comments/1am4b0e/"
                "deprecating_post_collections_mark_as_oc_and/"
            ),
            "maxTokens": 100_000,
            "timeout": 90,
        },
    )
    records.append(evidence)
    collection_match = _DYNAMIC_COLLECTION_URL.search(collection_source or "")
    if not (
        collection_source
        and "# Deprecating Post Collections" in collection_source
        and "several mod-oriented features will be removed" in collection_source
        and collection_match is not None
    ):
        evidence.status = "failed"
        evidence.detail = (
            "official Reddit deprecation evidence lacked an exact archived "
            "collection URL"
        )
    else:
        targets["collection"] = collection_match.group(0).rstrip("/") + "/"

    required = {
        "collection",
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
    }
    missing = sorted(required.difference(targets))
    records.append(
        Evidence(
            "dynamic_target_inventory",
            "discovery",
            "failed" if missing else "passed",
            (
                "missing real targets: " + ", ".join(missing)
                if missing
                else f"discovered all {len(required)} opaque public-read targets"
            ),
        )
    )
    return targets, records


def _materialize_entry(
    entry: CorpusEntry,
    targets: dict[str, str],
) -> CorpusEntry | None:
    if entry.discovery is None:
        return entry
    url = targets.get(entry.discovery)
    if url is None:
        return None
    materialized = replace(entry, url=url)
    route = route_reddit_url(url)
    if route is None or route.kind != entry.kind:
        return None
    return materialized


async def _run_entries(
    session: ClientSession,
    entries: list[CorpusEntry],
    stage: str,
    directory: Path,
    include_unstable: bool,
    targets: dict[str, str],
) -> list[Evidence]:
    records: list[Evidence] = []
    for entry in entries:
        should_run, reason = eligible(entry, include_unstable)
        if not should_run:
            records.append(Evidence(entry.id, stage, "not_run", reason))
            continue
        materialized = _materialize_entry(entry, targets)
        if materialized is None:
            records.append(
                Evidence(
                    entry.id,
                    stage,
                    "failed",
                    f"real target discovery failed for {entry.discovery}",
                )
            )
            continue
        records.append(await _call(session, materialized, directory, stage))
    return records


def _required_live_entry_ids(
    entries: list[CorpusEntry],
    *,
    strict: bool,
    include_unstable: bool,
) -> set[str]:
    required = (
        {entry.id for entry in entries if entry.live == "stable"}
        if strict
        else set()
    )
    if strict and include_unstable:
        required.update(entry.id for entry in entries if entry.live == "unstable")
    return required


_REQUIRED_ROUTE_STAGES = frozenset({"cold", "warm", "recreated"})
_REQUIRED_INFRASTRUCTURE_STAGES = {
    "tool_surface": frozenset({"cold", "recreated"}),
    "process_identity": frozenset(
        {"cold_warm_shutdown", "recreated_shutdown"}
    ),
    "browser_dispatch": frozenset(
        {"cold_warm_shutdown", "recreated_shutdown"}
    ),
    "browser_egress": frozenset(
        {"cold_warm_shutdown", "recreated_shutdown"}
    ),
    "reddit_session_persistence": frozenset(
        {"cold_warm_shutdown", "recreated_shutdown"}
    ),
    "durable_reddit_cookie_cache": frozenset(
        {"after_warm", "after_recreated"}
    ),
    "harness_identity": frozenset({"release"}),
}
_REQUIRED_DISCOVERY_STAGES = {
    "discovery_current_post_listing": frozenset({"discovery"}),
    "discovery_current_thread_json": frozenset({"discovery"}),
    "discovery_current_link_post_listing": frozenset({"discovery"}),
    "discovery_current_gallery_listing": frozenset({"discovery"}),
    "discovery_current_wiki_revisions": frozenset({"discovery"}),
    "discovery_current_public_multireddit": frozenset({"discovery"}),
    "discovery_current_live_update": frozenset({"discovery"}),
    "discovery_official_deprecated_collection": frozenset({"discovery"}),
    "dynamic_target_inventory": frozenset({"discovery"}),
}


def _has_exact_passed_stages(
    records: list[Evidence],
    expected_stages: frozenset[str],
) -> bool:
    """Require one passed record at every exact stage and nothing else."""

    stages = Counter(getattr(record, "stage", None) for record in records)
    return (
        set(stages) == expected_stages
        and all(stages[stage] == 1 for stage in expected_stages)
        and all(getattr(record, "status", None) == "passed" for record in records)
    )


def _has_exact_route_stage_shape(
    records: list[Evidence],
    *,
    require_passed: bool,
) -> bool:
    """Require one honest route result at every cold/warm/recreated stage."""

    stages = Counter(getattr(record, "stage", None) for record in records)
    statuses = [getattr(record, "status", None) for record in records]
    return (
        set(stages) == _REQUIRED_ROUTE_STAGES
        and all(stages[stage] == 1 for stage in _REQUIRED_ROUTE_STAGES)
        and all(status in {"passed", "not_run"} for status in statuses)
        and len(set(statuses)) == 1
        and (not require_passed or all(status == "passed" for status in statuses))
    )


def _exit_code(
    records: list[Evidence],
    strict: bool,
    *,
    entries: list[CorpusEntry] | None = None,
    include_unstable: bool = False,
) -> int:
    """Apply the composed release policy without calling an unrun item a pass.

    Failures always fail. Strict mode requires every stable public target;
    unstable public targets are additionally required when selected.
    The inherently non-public ``fixture_only`` class is never required live.
    """

    if any(record.status == "failed" for record in records):
        return 1
    if strict:
        if entries is None:
            # A release cannot prove exact route/stage cardinality without the
            # corpus that defines the required evidence set.
            return 1
        required_ids = _required_live_entry_ids(
            entries,
            strict=strict,
            include_unstable=include_unstable,
        )
        by_id: dict[str, list[Evidence]] = {}
        for record in records:
            by_id.setdefault(record.id, []).append(record)
        if any(
            not _has_exact_route_stage_shape(
                by_id.get(entry_id, []),
                require_passed=entry_id in required_ids,
            )
            for entry_id in {entry.id for entry in entries}
        ):
            return 1
        if any(
            not _has_exact_passed_stages(
                by_id.get(audit_id, []),
                expected_stages,
            )
            for audit_id, expected_stages
            in _REQUIRED_INFRASTRUCTURE_STAGES.items()
        ):
            return 1
        if strict and include_unstable and any(
            not _has_exact_passed_stages(
                by_id.get(discovery_id, []),
                expected_stages,
            )
            for discovery_id, expected_stages
            in _REQUIRED_DISCOVERY_STAGES.items()
        ):
            return 1
    return 0


def _docker_env_override(arguments: list[str], name: str) -> bool:
    """Return whether Docker arguments can set ``name`` in its container."""

    for index, argument in enumerate(arguments):
        value = ""
        if argument in {"-e", "--env"} and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif argument.startswith("--env="):
            value = argument.removeprefix("--env=")
        elif argument.startswith("-e") and len(argument) > 2:
            value = argument[2:]
        if value == name or value.startswith(f"{name}="):
            return True
    return False


def _configure_fresh_cache(
    parameters: Any,
    host_cache_dir: Path,
    requested_server_cache_dir: Path | None,
) -> dict[str, str]:
    """Attach one verified fresh cache to the launched process.

    Docker's client environment does not reach a container.  Inject a bind
    mount and container environment explicitly instead of advertising a cache
    proof that may silently use an ambient container volume.
    """

    command = [parameters.command, *parameters.args]
    server_env = dict(parameters.env or os.environ)
    parameters.env = server_env
    executable = Path(parameters.command).name
    is_docker_run = executable == "docker" and len(command) >= 2 and command[1] == "run"
    if executable == "docker" and not is_docker_run:
        raise ValueError("parity runner supports Docker only through exact `docker run`")
    if is_docker_run:
        docker_arguments = command[2:]
        protected_environment = (
            "WAFER_CACHE_DIR",
            "PUID",
            "PGID",
        )
        if any(_docker_env_override(docker_arguments, name) for name in protected_environment) or any(
            argument == "--env-file" or argument.startswith("--env-file=")
            for argument in docker_arguments
        ):
            raise ValueError(
                "Docker parity command must not set parity-managed environment "
                "variables or use --env-file; the runner injects them."
            )
        server_cache_dir = requested_server_cache_dir or Path(
            "/app/data/reddit-parity-cache"
        )
        if (
            not server_cache_dir.is_absolute()
            or ".." in server_cache_dir.parts
            or not server_cache_dir.is_relative_to(Path("/app/data"))
            or server_cache_dir == Path("/app/data")
        ):
            raise ValueError(
                "--server-cache-dir must be a child of /app/data for Docker"
            )
        injected = [
            "--mount",
            f"type=bind,src={host_cache_dir},dst={server_cache_dir}",
            "--env",
            f"WAFER_CACHE_DIR={server_cache_dir}",
        ]
        host_uid = os.getuid()
        host_gid = os.getgid()
        if host_uid != 0:
            injected.extend(
                (
                    "--env",
                    f"PUID={host_uid}",
                    "--env",
                    f"PGID={host_gid}",
                )
            )
        command[2:2] = injected
        parameters.command, parameters.args = command[0], command[1:]
        return {
            "mode": "docker_bind",
            "host_cache_dir": str(host_cache_dir),
            "server_cache_dir": str(server_cache_dir),
            "runtime_owner": (
                "image_default" if host_uid == 0 else f"{host_uid}:{host_gid}"
            ),
        }

    if requested_server_cache_dir is not None and requested_server_cache_dir != host_cache_dir:
        raise ValueError(
            "--server-cache-dir is only valid for a direct `docker run` command"
        )
    server_env["WAFER_CACHE_DIR"] = str(host_cache_dir)
    return {
        "mode": "direct_env",
        "host_cache_dir": str(host_cache_dir),
        "server_cache_dir": str(host_cache_dir),
    }


async def main_async(args: argparse.Namespace) -> int:
    harness_start = _capture_harness_identity(args.corpus)
    harness_packages = harness_start["packages"]
    if not isinstance(harness_packages, dict):
        raise ValueError("release harness package identity is invalid")
    entries = load_corpus(args.corpus)
    args.output = _prepare_empty_evidence_directory(args.output)
    all_records: list[Evidence] = []
    cache_dir = args.cache_dir or Path(tempfile.mkdtemp(prefix="fetchaller-reddit-parity-"))
    cache_dir = cache_dir.resolve()
    if cache_dir.exists() and any(cache_dir.iterdir()):
        raise ValueError(f"cache directory must start empty: {cache_dir}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Cold starts against a new, empty durable cache.  Warm shares its process;
    # recreated starts a new process with exactly that cache, never ambient
    # developer cookies. Docker receives a verified bind mount below, rather
    # than an environment variable that only configures the Docker client.
    parameters = _stdio_server_parameters()
    cache_mapping = _configure_fresh_cache(
        parameters, cache_dir, args.server_cache_dir
    )
    pacer = _Pacer(args.interval)
    targets: dict[str, str] = {}
    cold_warm_stderr = args.output / "cold-warm-server.stderr.log"
    with cold_warm_stderr.open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                paced = _PacedSession(session, pacer)
                all_records.append(await _verify_tool_surface(paced, "cold"))
                if args.include_unstable:
                    targets, discovery_records = await _discover_live_targets(
                        paced,
                        args.output,
                    )
                    all_records.extend(discovery_records)
                all_records.extend(
                    await _run_entries(
                        paced,
                        entries,
                        "cold",
                        args.output,
                        args.include_unstable,
                        targets,
                    )
                )
                all_records.extend(
                    await _run_entries(
                        paced,
                        entries,
                        "warm",
                        args.output,
                        args.include_unstable,
                        targets,
                    )
                )
    all_records.append(
        _audit_browser_egress(
            cold_warm_stderr,
            "cold_warm_shutdown",
            require_zero=False,
        )
    )
    all_records.append(
        _audit_process_identity(
            cold_warm_stderr,
            "cold_warm_shutdown",
            expected_packages=harness_packages,
            compare_paths=cache_mapping["mode"] == "direct_env",
        )
    )
    all_records.append(
        _audit_browser_dispatch(
            cold_warm_stderr,
            "cold_warm_shutdown",
        )
    )
    all_records.append(
        _audit_reddit_session(
            cold_warm_stderr,
            "cold_warm_shutdown",
            expect_hydrated=False,
            require_no_bootstrap=False,
        )
    )
    all_records.append(_audit_reddit_cookie_cache(cache_dir, "after_warm"))

    recreated_stderr = args.output / "recreated-server.stderr.log"
    with recreated_stderr.open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                paced = _PacedSession(session, pacer)
                all_records.append(
                    await _verify_tool_surface(paced, "recreated")
                )
                all_records.extend(
                    await _run_entries(
                        paced,
                        entries,
                        "recreated",
                        args.output,
                        args.include_unstable,
                        targets,
                    )
                )
    all_records.append(
        _audit_browser_egress(
            recreated_stderr,
            "recreated_shutdown",
            require_zero=True,
        )
    )
    all_records.append(
        _audit_process_identity(
            recreated_stderr,
            "recreated_shutdown",
            expected_packages=harness_packages,
            compare_paths=cache_mapping["mode"] == "direct_env",
        )
    )
    all_records.append(
        _audit_browser_dispatch(
            recreated_stderr,
            "recreated_shutdown",
        )
    )
    all_records.append(
        _audit_reddit_session(
            recreated_stderr,
            "recreated_shutdown",
            expect_hydrated=True,
            require_no_bootstrap=True,
        )
    )
    all_records.append(_audit_reddit_cookie_cache(cache_dir, "after_recreated"))
    try:
        harness_end = _capture_harness_identity(args.corpus)
    except (OSError, ValueError) as exc:
        harness_end = {
            "error": f"{type(exc).__name__}: {exc}",
        }
    all_records.append(
        _audit_harness_identity(
            harness_start,
            harness_end,
        )
    )
    class_counts = {
        evidence_class: sum(
            entry.live == evidence_class for entry in entries
        )
        for evidence_class in (
            "stable",
            "unstable",
            "fixture_only",
        )
    }
    report = {
        "schema_version": 1,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "command": [parameters.command, *parameters.args],
        "corpus": str(args.corpus),
        "interval_seconds": args.interval,
        "cache_mapping": cache_mapping,
        "include_unstable": args.include_unstable,
        "strict": args.strict,
        "harness_identity": {
            "start": harness_start,
            "end": harness_end,
        },
        "coverage": {
            "corpus_entries": len(entries),
            "by_evidence_class": class_counts,
            "required_live_entries": len(
                _required_live_entry_ids(
                    entries,
                    strict=args.strict,
                    include_unstable=args.include_unstable,
                )
            ),
            "offline_fixture_entries": [
                {
                    "id": entry.id,
                    "reason": entry.offline_reason,
                }
                for entry in entries
                if entry.live == "fixture_only"
            ],
            "discovered_targets": dict(sorted(targets.items())),
        },
        "policy": {
            "stable_live": "required" if args.strict else "run",
            "unstable_live": (
                "required" if args.strict and args.include_unstable
                else "run" if args.include_unstable
                else "not selected"
            ),
            "offline_fixtures": "never run or counted as live by this runner",
        },
        "results": [asdict(record) for record in all_records],
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    failed = [record for record in all_records if record.status == "failed"]
    passed = [record for record in all_records if record.status == "passed"]
    skipped = [record for record in all_records if record.status == "not_run"]
    print(
        f"Reddit parity: {len(passed)} passed, {len(skipped)} not run, "
        f"{len(failed)} failed; evidence={args.output}"
    )
    return _exit_code(
        all_records,
        args.strict,
        entries=entries,
        include_unstable=args.include_unstable,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=Path("/tmp/fetchaller-reddit-parity"))
    parser.add_argument("--interval", type=float, default=6.0)
    parser.add_argument(
        "--include-unstable",
        action="store_true",
        help="also run and, in strict mode, require unstable live targets",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="empty host cache directory; default creates a fresh temporary directory",
    )
    parser.add_argument(
        "--server-cache-dir",
        type=Path,
        help="cache path inside the launched server (for example a Docker volume mount)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "release mode: require every stable public target, "
            "plus every explicitly selected live evidence class"
        ),
    )
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("--interval must be at least five seconds")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
