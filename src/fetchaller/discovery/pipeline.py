"""Turning a page URL into a cacheable plain-HTTP request.

The whole pass, in order: observe the page in a browser, decide which exchange
is the data, replay it verbatim and check the answer against the browser's,
minimize it to the parts that matter, harden the survivors into mint steps, and
then verify the *serialized* plan through exactly the code path a later replay
will use.

That last step is not belt-and-braces. Everything upstream verifies a request as
*dicts*; what ships is the serialized plan, and percent-encoding means the two
can differ. A plan is only marked ``verified`` after the thing that will
actually be sent has been sent.

Cost is 5 to 40 live requests plus a browser launch — tens of seconds. Run it on
failure, cache the result, never on a search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import wafer

from ..config import get_wafer_cache_dir
from ..ratelimit import discovery_limiter
from . import minimize as mini
from .observe import (
    Capture,
    ChallengeEncounteredError,
    DiscoveryUnavailableError,
    _looks_challenged,
    capture,
    challenged_exchanges,
    throttled_exchanges,
)
from .oracle import signature, signatures_match
from .plan import (
    MintStep,
    PlanUnresolvedError,
    RequestPlan,
    classify_body,
    encode_fields,
    execute,
    header_delta,
    mint_values,
    pairs_to_fields,
    seed_cookies,
)
from .provenance import build_mint_steps, marker
from .ranking import Candidate, best, query_hints, rank, visible_text

logger = logging.getLogger(__name__)

# Bounded: the fallback only runs when the batch swap failed, and every probe
# is a live request.
_HARDEN_PROBE_BUDGET = 8

__all__ = [
    "ChallengeEncounteredError",
    "Discovery",
    "DiscoveryUnavailableError",
    "discover",
]


@dataclass
class Discovery:
    """The outcome of one discovery pass."""

    url: str
    plan: RequestPlan | None
    reason: str = ""
    candidate: Candidate | None = None
    capture: Capture | None = None
    probes: int = 0

    @property
    def ok(self) -> bool:
        """Whether the plan is safe to use.

        Deliberately requires ``verified``, not merely that a plan was built.
        An unverified plan is still exposed on ``.plan`` for inspection, but it
        did not reproduce the browser's answer — treating that as success is the
        same believe-don't-verify mistake the oracle exists to prevent.
        """
        return self.plan is not None and self.plan.verified


def _split_url(url: str) -> tuple[str, dict]:
    """Base URL without its query, plus the query as a field mapping."""
    parts = urlsplit(url)
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    query = pairs_to_fields(parse_qsl(parts.query, keep_blank_values=True))
    return base, query


def _build_fields(candidate: Candidate) -> tuple[dict, str, str | None, str | None]:
    """The combined header/query/field mapping for one exchange.

    Headers, query parameters and body fields are minimized *together*. An
    endpoint may demand an ``Origin`` it never validates, and a POST's build ids
    live in its query string rather than its body — on Google, minimization
    drops ``bl``, ``f.sid`` and ``_reqid`` from the query and leaves a single
    body field. Minimizing the body alone would keep all three.
    """
    exchange = candidate.exchange
    base, query = _split_url(exchange.url)
    body_kind, body_fields = classify_body(
        exchange.request_body, exchange.request_headers.get("content-type", "")
    )

    fields: dict = {}
    for name, value in header_delta(exchange.request_headers).items():
        fields[mini.HEADER + name] = value
    for name, value in query.items():
        fields[mini.QUERY + name] = value
    for name, value in body_fields.items():
        fields[mini.FIELD + name] = value
    return fields, base, body_kind, exchange.request_body


def _assemble(
    *,
    method: str,
    base: str,
    fields: dict,
    body_kind: str | None,
    raw_body: str | None,
) -> RequestPlan:
    """Reassemble a plan from a (possibly reduced) field mapping."""
    headers = {}
    query: dict = {}
    body_fields: dict = {}
    for key, value in fields.items():
        if key.startswith(mini.HEADER):
            headers[key[len(mini.HEADER) :]] = value
        elif key.startswith(mini.QUERY):
            query[key[len(mini.QUERY) :]] = value
        elif key.startswith(mini.FIELD):
            body_fields[key[len(mini.FIELD) :]] = value

    url = base if not query else f"{base}?{encode_fields(query)}"

    if body_kind == "json":
        body = json.dumps(body_fields)
    elif body_kind == "form":
        body = encode_fields(body_fields)
    elif body_kind == "raw":
        # A raw body yields no fields and is left whole — the right answer for a
        # positional protocol, where dropping a slot shifts every argument after
        # it rather than removing one.
        body = raw_body
    else:
        body = None

    return RequestPlan(
        method=method,
        url=url,
        headers=headers,
        body=body,
        body_kind=body_kind,
        required_fields=tuple(fields),
    )


_session: wafer.AsyncSession | None = None
_session_lock = asyncio.Lock()


async def _get_session(browser_solver=None) -> wafer.AsyncSession:
    global _session
    if _session is None:
        async with _session_lock:
            if _session is None:
                _session = wafer.AsyncSession(
                    browser_solver=browser_solver,
                    cache_dir=get_wafer_cache_dir(),
                    timeout=timedelta(seconds=60),
                )
    return _session


async def close_session() -> None:
    global _session
    _session = None


# Statuses that mean "ask again later", not "this is the answer".
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})
_CAPTURE_ATTEMPTS = 2
_THROTTLE_BACKOFF_SECONDS = 20.0


async def _capture_with_retry(
    url: str, *, timeout: float, expect: str | None
) -> tuple[Capture, Candidate | None]:
    """Observe the page until it yields data, or the attempts run out.

    Retries on two conditions, because a board refuses in two different ways and
    both were misread as facts about the board:

    **A throttled navigation.** Measured on ``metacareers.com``: a 200 load
    yields ~693 KB of HTML and a 128,175-byte ``/graphql`` results query *every
    time*, while a 429 yields a ~179 KB holding page and no query at all.

    **A 200 that carries no data.** The nastier one — the same silent-empty
    failure this package exists to detect, applied to the page load rather than
    the API. Meta answers some requests with a reduced page: status 200, no
    results query, only routing and telemetry. Keying the retry on status alone
    left 2 runs in 5 concluding the board had no API.

    So the retry keys on *finding an answerable candidate* — but stops early
    when the capture shows a challenge, because retrying a refused board only
    adds load. Attempts are deliberately few: an earlier 3-attempt version
    tripled request volume against a host already throttling us, and made the
    outcome worse rather than better.
    """
    observation = await capture(url, timeout=timeout)
    candidate = best(observation, expect=expect)

    # A throttle bound to the browser *identity* outlives any backoff, because
    # the persistent profile keeps presenting the same one. Measured on Meta:
    # the aged profile got `{"errors":[{"message":"Rate limit exceeded"}]}` for
    # 35 minutes straight, while a fresh profile got the full 128,175-byte
    # results query immediately. The profile exists to make us a *returning*
    # visitor; a throttled returning visitor is worse than a new one.
    if candidate is None and throttled_exchanges(observation.exchanges, observation.host):
        logger.info("discovery: %s throttled this browser identity; retrying fresh", observation.host)
        fresh = tempfile.mkdtemp(prefix="fetchaller-discovery-")
        shutil.rmtree(fresh, ignore_errors=True)
        try:
            retried = await capture(url, timeout=timeout, profile_dir=fresh)
            retried.notes.append("previous browser identity was rate-limited; retried with a fresh one")
            observation, candidate = retried, best(retried, expect=expect)
        except Exception as exc:  # noqa: BLE001 - keep the original observation
            logger.info("discovery: fresh-identity retry failed (%s)", type(exc).__name__)
        finally:
            shutil.rmtree(fresh, ignore_errors=True)

    for attempt in range(1, _CAPTURE_ATTEMPTS):
        if candidate is not None:
            return observation, candidate
        if challenged_exchanges(observation.exchanges, observation.host):
            # Refused, not empty. Retrying adds load and changes nothing.
            return observation, None
        status = observation.page_status
        throttled = status in _TRANSIENT_STATUSES
        backoff = _THROTTLE_BACKOFF_SECONDS * attempt
        logger.info(
            "discovery: %s yielded no data (status %s); backing off %.0fs before retry %d/%d",
            urlsplit(url).hostname,
            status or "unknown",
            backoff,
            attempt + 1,
            _CAPTURE_ATTEMPTS,
        )
        # Hold every other discovery caller off this host too, not just us.
        discovery_limiter.defer(backoff)
        retried = await capture(url, timeout=timeout)
        retried.notes.append(
            f"first attempt yielded no data ({'status ' + str(status) if throttled else 'nothing answerable'})"
        )
        observation = retried
        candidate = best(observation, expect=expect)

    return observation, candidate


async def discover(
    url: str,
    *,
    expect: str | None = None,
    session: wafer.AsyncSession | None = None,
    timeout: float = 120.0,
    max_probes: int = mini.DEFAULT_MAX_PROBES,
    harden: bool = True,
    browser_solver=None,
) -> Discovery:
    """Discover the plain-HTTP request that returns ``url``'s data.

    ``expect`` is a string the page displays. Supplying it is decisive and
    cheap: ranking without a hint is a heuristic, and a page URL carrying no
    query gives it nothing to work with.
    """
    observation, candidate = await _capture_with_retry(url, timeout=timeout, expect=expect)
    if candidate is None:
        # Check the *data* requests before concluding the board has none. A
        # page can render perfectly while every payload request behind it is
        # refused, and that reads identically to "no API here".
        blocked = challenged_exchanges(observation.exchanges, observation.host)
        if blocked:
            reason = await _diagnose_refusals(
                session or await _get_session(browser_solver), blocked, observation
            )
            return Discovery(url=url, plan=None, capture=observation, reason=reason)
        throttled = throttled_exchanges(observation.exchanges, observation.host)
        if throttled:
            sample = throttled[0]
            return Discovery(
                url=url,
                plan=None,
                capture=observation,
                reason=(
                    f"{observation.host} rate-limited the data request: HTTP {sample.status} on "
                    f"{urlsplit(sample.url).path} carrying a rate-limit error, not results. "
                    "The endpoint exists and is declining to answer — retry later."
                ),
            )
        server_rendered = _server_rendered_listing(observation, expect=expect)
        if server_rendered:
            return Discovery(
                url=url,
                plan=None,
                capture=observation,
                reason=(
                    f"{observation.host} server-renders its results: the document at "
                    f"{server_rendered} *is* the data, and there is no client-side endpoint to "
                    "discover. Fetch and parse the page, paginating by its own links."
                ),
            )
        status = observation.page_status
        if status and not 200 <= status < 400:
            # Say what actually happened. A throttled page still renders, and
            # every exchange on it is correctly rejected as non-data, so
            # without this the outcome reads as "this board has no API".
            reason = f"the page itself returned HTTP {status}; nothing was observed"
        elif not rank(observation, expect=expect):
            reason = "no exchange looked like data"
        else:
            reason = "every candidate was too thin to build a plan from"
        return Discovery(url=url, plan=None, reason=reason, capture=observation)

    observed = signature(candidate.exchange.body)
    fields, base, body_kind, raw_body = _build_fields(candidate)
    method = candidate.exchange.method

    http = session or await _get_session(browser_solver)

    # Continue the browser's session rather than probing as a stranger. The
    # observing browser already earned cookies for this origin; dozens of
    # cookieless probes against the endpoint it just called is the pattern
    # that gets throttled.
    seeded = seed_cookies(http, observation.cookies)

    # Steps established so far. ddmin probes through this, so once a token is
    # re-mintable every later probe carries the mint rather than the stale
    # literal.
    active_steps: tuple[MintStep, ...] = ()

    async def probe(subset: dict, *, steps: tuple[MintStep, ...] | None = None) -> bool:
        trial = _assemble(
            method=method, base=base, fields=subset, body_kind=body_kind, raw_body=raw_body
        )
        trial.mint = active_steps if steps is None else steps
        try:
            response = await execute(
                http, trial, timeout=45.0, throttle=discovery_limiter.wait
            )
        except (wafer.WaferError, PlanUnresolvedError, OSError):
            return False
        except Exception:  # noqa: BLE001 - a probe failure is just a failed probe
            return False
        # Honour an explicit slow-down rather than spending the rest of the
        # budget collecting 429s, which all read as "this request is wrong".
        if response.status_code == 429:
            discovery_limiter.defer(float(response.retry_after or 30.0))
            return False
        return signatures_match(observed, signature(response.text or ""))

    # 1. Verbatim. A captured request whose tokens are still accepted needs
    #    no minting machinery, and the simplest plan that works is the one
    #    most likely to keep working.
    working = await probe(fields)
    probes = 1
    minted_early = False
    notes: list[str] = []

    if not working and harden:
        # The captured request may be well-formed but carrying an expired
        # token — which is exactly what mint steps are for. Trying provenance
        # here is what separates "this shape is wrong" from "this credential
        # went stale"; without it a stale CSRF ends the pass with no plan.
        fields, steps, spent = await _harden(fields, probe, candidate, observation)
        probes += spent
        if steps:
            active_steps = steps
            working = True
            minted_early = True
            notes.append("captured token was stale; re-minted before minimizing")

    # 2. Minimize. If the full set already fails, ddmin returns it whole and
    #    drops nothing, and the plan stays unverified.
    kept, dropped, spent = await mini.ddmin(fields, probe, max_probes=max_probes)
    probes += spent

    plan = _assemble(
        method=method, base=base, fields=kept, body_kind=body_kind, raw_body=raw_body
    )
    plan.dropped_fields = dropped
    plan.mint = active_steps

    # 3. Harden. A value the plan can re-mint is more durable than a literal
    #    that happens to still work today — and a CSRF token is exactly the
    #    kind of value that stops working later for no visible reason.
    if harden and working and not minted_early:
        hardened_fields, steps, spent = await _harden(kept, probe, candidate, observation)
        probes += spent
        if steps:
            plan = _assemble(
                method=method,
                base=base,
                fields=hardened_fields,
                body_kind=body_kind,
                raw_body=raw_body,
            )
            plan.dropped_fields = dropped
            plan.mint = steps
        elif spent:
            notes.append("mint steps rejected; kept literal values")
    plan.notes = (*plan.notes, *notes)

    # Minimization can drop the only field that used a marker. The step is
    # then dead weight that costs one wasted fetch on every future replay, so
    # prune anything the serialized plan no longer references.
    plan.mint = _used_steps(plan)

    # 4. Verify what actually ships. Upstream verified dicts; this verifies the
    #    serialized plan through the same path replay uses, re-minting included.
    probes += 1
    final = await _verify_serialized(http, plan, observed)
    plan.verified = final is not None
    plan.record_count = final.records if final else 0
    if not plan.verified:
        plan.notes = (*plan.notes, "serialized plan did not reproduce the browser's answer")

    if seeded:
        plan.notes = (*plan.notes, f"replayed with {seeded} cookies carried from the browser")
    plan.notes = (*plan.notes, *observation.notes)
    return Discovery(
        url=url,
        plan=plan,
        candidate=candidate,
        capture=observation,
        probes=probes,
        reason="" if plan.verified else "plan could not be verified",
    )


# A listing repeats what was searched for; a page that merely mentions it once
# in a heading or a nav label does not.
_SSR_HINT_REPEATS = 4


def _server_rendered_listing(observation: Capture, *, expect: str | None) -> str | None:
    """The page's own document, when the results are baked into it.

    Trap 10 in reverse: an SSR board answers the navigation *with the payload*,
    so "no data request" is the right observation but the wrong conclusion —
    there is nothing to discover because the page already is the endpoint.

    Measured on Uber: the board renders ten postings per page and paginates with
    plain ``<a href="/en/jobs?query=…&page=2&pagesize=10">`` links, no XHR at
    all. Reporting that beats "every candidate was too thin", which reads as a
    failure when it is a finding.

    Deliberately requires the search terms to appear *repeatedly* in the
    rendered text. Meta's document mentions "engineer" too, but carries none of
    the results — zero of the first 25 GraphQL titles appear in it — so a
    single-mention test would mislabel a throttled board as server-rendered.
    """
    hints = [expect] if expect else query_hints(observation.url)
    if not expect:
        hints += query_hints(observation.requested_url or "")
    hints = [h for h in hints if h]
    if not hints:
        return None

    text = visible_text(observation.html).casefold()
    if not text:
        return None
    if not any(text.count(hint.casefold()) >= _SSR_HINT_REPEATS for hint in hints):
        return None

    for exchange in observation.exchanges:
        if (
            exchange.resource_type == "document"
            and exchange.host == observation.host
            and 200 <= exchange.status < 300
        ):
            return exchange.url
    return observation.url


async def _diagnose_refusals(http, blocked, observation: Capture) -> str:
    """Decide whether a browser-side refusal is the board's doing or ours.

    **Never attribute a refusal to the board without replaying it.** A
    browser-side 403 has two completely different causes and they demand
    opposite responses:

    - The *board* challenges. Then a plain replay is challenged too, and solving
      it is bot bypass — wafer's, not this package's.
    - The *capture browser* is flagged. Then the plain replay just succeeds, the
      board is fine, and filing a wafer request would be filing one for a
      non-problem.

    Measured on Uber: the browser got ``403 Just a moment...`` on all sixteen
    posting prefetches, while the identical URL over plain wafer returned ``200``
    with 383,021 bytes of Flight data. Three separate investigations concluded
    the board was at fault because none of them ran this one request.
    """
    sample = blocked[0]
    path = urlsplit(sample.url).path
    try:
        await discovery_limiter.wait()
        response = await http.get(sample.url, timeout=30.0)
        status, text = response.status_code, response.text or ""
        challenged = _looks_challenged(text) or bool(getattr(response, "challenge_type", None))
    except wafer.ChallengeDetected:
        status, challenged = 0, True
    except Exception as exc:  # noqa: BLE001 - inconclusive, and said so below
        return (
            f"{len(blocked)} data request(s) on {observation.host} were refused in the browser "
            f"(e.g. {sample.status} on {path}), and the plain-HTTP check was inconclusive "
            f"({type(exc).__name__}). Cause not established."
        )

    if challenged:
        return (
            f"{len(blocked)} data request(s) on {observation.host} were refused, and a plain-HTTP "
            f"replay of {path} is challenged too. The board is protected; solving that is wafer's, "
            "not discovery's."
        )
    if 200 <= status < 300:
        return (
            f"{len(blocked)} data request(s) on {observation.host} were refused in the browser "
            f"(e.g. {sample.status} on {path}), but the same URL answers plain HTTP with {status}. "
            "The board is fine — this capture browser is being flagged. Discovery's browser is "
            "deliberately an ordinary one; harden it or route capture through wafer's."
        )
    return (
        f"{len(blocked)} data request(s) on {observation.host} were refused in the browser "
        f"(e.g. {sample.status} on {path}); a plain-HTTP replay returned {status} without a "
        "challenge. Cause not established — do not assume the board is protected."
    )


def _used_steps(plan: RequestPlan) -> tuple[MintStep, ...]:
    """Mint steps the serialized plan actually references."""
    if not plan.mint:
        return ()
    from urllib.parse import unquote

    # Read the decoded forms too: a marker inside a query or form body is
    # stored percent-encoded, so a raw substring check would prune a live step.
    haystack = " ".join(
        part
        for raw in (plan.url, plan.body or "", json.dumps(plan.headers))
        for part in (raw, unquote(raw))
    )
    return tuple(step for step in plan.mint if marker(step.name) in haystack)


async def _harden(kept: dict, probe, candidate: Candidate, observation: Capture):
    """Swap literal values for mint steps, keeping only swaps that hold.

    Tried as a batch first, because that is one probe and it is what a board
    with real tokens needs — on Meta it converts both ``lsd`` and ``x-fb-lsd``
    into a single re-mint from the page.

    The per-value fallback exists because one bad step poisons the batch.
    Amazon keeps twelve ``facets[]`` values like ``normalized_country_code``:
    long enough to look traceable, but stable parameter names rather than
    tokens. Minting them breaks the request, and without the fallback a board
    carrying both a real token and a long literal would lose the real one too.
    """
    hardened, steps = build_mint_steps(
        kept,
        exchanges=observation.exchanges,
        page_url=observation.url,
        page_html=observation.html,
        before_order=candidate.exchange.order,
    )
    if not steps:
        return kept, (), 0

    spent = 1
    if await probe(hardened, steps=steps):
        return hardened, steps, spent

    # Fall back to one value at a time, bounded — every probe is a live request.
    # Ordered most token-like first so that a board carrying one real token
    # among many long literals spends its budget on the token. This only
    # reorders; nothing is excluded that the budget would otherwise reach.
    accepted_fields = dict(kept)
    accepted_steps: list[MintStep] = []
    for step in sorted(steps, key=lambda s: -_token_likeness(hardened, kept, s)):
        if spent >= _HARDEN_PROBE_BUDGET:
            break
        trial_fields = dict(accepted_fields)
        for key, value in hardened.items():
            if marker(step.name) in _as_text(value):
                trial_fields[key] = value
        trial_steps = (*accepted_steps, step)
        spent += 1
        if await probe(trial_fields, steps=trial_steps):
            accepted_fields = trial_fields
            accepted_steps = list(trial_steps)
    return accepted_fields, tuple(accepted_steps), spent


def _as_text(value) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _token_likeness(hardened: dict, kept: dict, step: MintStep) -> int:
    """How much a minted value resembles a token rather than a parameter name.

    Used only to order the per-value hardening fallback. ``lsd``'s value scores
    4; Amazon's ``normalized_country_code`` scores 0.
    """
    needle = marker(step.name)
    for key, value in hardened.items():
        if needle not in _as_text(value):
            continue
        for original in ([kept[key]] if not isinstance(kept.get(key), list) else kept[key]):
            text = str(original)
            if len(text) < 12:
                continue
            score = 0
            if any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
                score += 2
            if any(c.isupper() for c in text) and any(c.islower() for c in text):
                score += 1
            if "_" not in text:
                score += 1
            return score
    return 0


async def _verify_serialized(http, plan: RequestPlan, observed):
    """Round-trip the plan through JSON and replay exactly what would ship."""
    shipped = RequestPlan.from_json(plan.to_json())
    try:
        minted = (
            await mint_values(http, shipped.mint, timeout=30.0, throttle=discovery_limiter.wait)
            if shipped.mint
            else {}
        )
        response = await execute(
            http, shipped, timeout=45.0, minted=minted, throttle=discovery_limiter.wait
        )
    except Exception:  # noqa: BLE001 - an unverifiable plan is reported, not raised
        return None
    candidate_signature = signature(response.text or "")
    if not signatures_match(observed, candidate_signature):
        return None
    return candidate_signature
