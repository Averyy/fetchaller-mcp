"""Persisting a discovered plan, so discovery runs once and replay runs always.

Discovery costs a browser launch and tens of seconds. Replay costs one request.
The intended cycle is therefore: pin a known-good request per board and treat it
as the fast path; when a board's answer stops looking right, re-derive once,
compare, and either self-heal or report honestly that the board really is empty.

``record_count`` is what makes "stopped looking right" measurable rather than a
guess. Measured on Meta: the healthy plan returns 588 records; the same request
with ``doc_id`` incremented by one returns ``HTTP 200`` with 1 record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import get_wafer_cache_dir
from .plan import RequestPlan

logger = logging.getLogger(__name__)

_SAFE_KEY_RE = re.compile(r"[^a-z0-9_.-]+")

# A replay returning far less than the plan recorded means the plan rotted, not
# that the board emptied. Deliberately generous: real boards do shrink.
DECAY_FLOOR = 0.5


def _root() -> Path:
    base = get_wafer_cache_dir()
    if base:
        return Path(base).parent / "discovery"
    return Path(tempfile.gettempdir()) / "fetchaller-discovery"


def plan_path(key: str) -> Path:
    """Where the plan for ``key`` lives.

    The key is slugified for readability and suffixed with a hash, so two keys
    that slugify identically cannot collide.
    """
    slug = _SAFE_KEY_RE.sub("-", (key or "").casefold()).strip("-")[:60] or "plan"
    digest = hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12]
    return _root() / f"{slug}.{digest}.json"


def load_plan(key: str) -> RequestPlan | None:
    """The cached plan for ``key``, or None."""
    path = plan_path(key)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        return RequestPlan.from_json(raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("discovery: discarding unreadable plan at %s", path)
        return None


def save_plan(key: str, plan: RequestPlan) -> bool:
    """Store ``plan`` under ``key``. Written atomically."""
    path = plan_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a half-written plan would be indistinguishable from a rotted
        # one, and would send a malformed request rather than trigger rediscovery.
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(plan.to_json())
        os.replace(temporary, path)
        return True
    except OSError as exc:
        logger.warning("discovery: could not cache plan for %s: %s", key, exc)
        return False


def forget_plan(key: str) -> bool:
    try:
        plan_path(key).unlink()
        return True
    except OSError:
        return False


def looks_decayed(plan: RequestPlan, records: int) -> bool:
    """Whether a replay's record count suggests the plan has rotted."""
    if not plan.record_count:
        return False
    return records < max(1, plan.record_count * DECAY_FLOOR)


@dataclass
class Replay:
    """The result of replaying a cached plan."""

    response: object | None = None
    plan: RequestPlan | None = None
    records: int = 0
    rediscovered: bool = False
    decayed: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Whether the response can be trusted as the board's real answer.

        A decayed response is *not* ok even though it exists. A rotated
        identifier answers ``HTTP 200`` with a near-empty result, and returning
        that as success is precisely the silent-empty failure this package was
        built to eliminate — the caller would render "no jobs found" from a
        broken request. The response stays on ``.response`` for inspection.
        """
        return self.response is not None and not self.decayed


async def replay(
    session,
    key: str,
    url: str,
    *,
    expect: str | None = None,
    self_heal: bool = True,
    timeout: float = 45.0,
    **kwargs,
) -> Replay:
    """Execute the cached plan for ``key``, re-deriving it once if it rotted.

    This is the cycle the whole package exists to support. A rotated
    identifier does not error — it answers ``HTTP 200`` with a near-empty
    result, which is indistinguishable from an empty board unless the healthy
    record count is known. It is, so the difference is measurable and the
    response is either self-healed or reported honestly.
    """
    from .oracle import signature
    from .plan import execute

    plan = await resolve_plan_for(key, url, expect=expect, **kwargs)
    if plan is None:
        return Replay(reason="no verified plan could be built")

    failure = ""
    try:
        response = await execute(session, plan, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - reported, and possibly healed below
        failure = f"replay failed ({type(exc).__name__})"
        if not self_heal:
            return Replay(plan=plan, reason=failure)
        response = None

    records = signature(response.text or "").records if response is not None else 0
    if response is not None and not looks_decayed(plan, records):
        return Replay(response=response, plan=plan, records=records)

    if not self_heal:
        return Replay(
            response=response,
            plan=plan,
            records=records,
            decayed=True,
            reason=f"returned {records} records against a recorded {plan.record_count}",
        )

    logger.info(
        "discovery: plan for %s looks rotted (%d vs %d records); re-deriving",
        key,
        records,
        plan.record_count,
    )
    fresh = await resolve_plan_for(key, url, expect=expect, force=True, **kwargs)
    if fresh is None:
        # The original response is returned for inspection but never as
        # success: "rotted" and "genuinely empty" are indistinguishable here,
        # and guessing in the caller's favour is how a broken request comes to
        # be rendered as "no results".
        return Replay(
            response=response,
            plan=plan,
            records=records,
            decayed=True,
            reason=failure
            or "plan looks rotted and rediscovery found nothing; the board may really be empty",
        )
    try:
        healed = await execute(session, fresh, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return Replay(plan=fresh, rediscovered=True, reason=f"replay failed ({type(exc).__name__})")

    # The fresh plan verified during discovery, but that was a different
    # request moments earlier. Check what it actually returned here too —
    # otherwise a failed heal reports success with one record.
    healed_records = signature(healed.text or "").records
    still_decayed = looks_decayed(fresh, healed_records)
    return Replay(
        response=healed,
        plan=fresh,
        records=healed_records,
        rediscovered=True,
        decayed=still_decayed,
        reason=(
            f"re-derived plan still returned {healed_records} records "
            f"against a recorded {fresh.record_count}"
            if still_decayed
            else ""
        ),
    )


async def resolve_plan_for(
    key: str,
    url: str,
    *,
    expect: str | None = None,
    force: bool = False,
    **kwargs,
) -> RequestPlan | None:
    """Return a verified plan for ``key``, discovering it once if needed."""
    if not force:
        cached = load_plan(key)
        if cached is not None and cached.verified:
            return cached

    from .pipeline import discover

    result = await discover(url, expect=expect, **kwargs)
    if result.plan is not None and result.plan.verified:
        save_plan(key, result.plan)
        return result.plan
    if result.plan is None:
        logger.info("discovery: no plan for %s (%s)", key, result.reason)
    return None
