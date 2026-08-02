"""Discovering the plain-HTTP request an SPA makes.

A growing number of sites serve no content in their HTML and no discoverable
API: the data is fetched by JavaScript at runtime, and the exact shape of that
fetch — its URL, headers, body and query grammar — is knowable only by running
the page's own code. Six of this repo's ten job-board clients needed minified JS
bundles read by hand to find it, and each of those six will need it redone when
the site next ships.

This package replaces that archaeology with an observation pass: load the page
in a browser, watch what it asks for, replay the winner over plain HTTP, and
check the replay against what the browser actually got.

**This is not bot bypass, which is why it lives here and not in wafer.** None of
the seven boards it was validated against issues a challenge; the difficulty is
the request *shape*, not access. wafer owns blocking. The one case that inverts
that — discovering an endpoint on a board which *is* protected — surfaces as
:class:`ChallengeEncounteredError` rather than being guessed at.

The most damaging failure in this domain is a well-formed ``HTTP 200`` that
means "your request was malformed" while looking like "there is no data". Five
separate boards do this. Everything here is built around telling those apart:
the browser's own answer is ground truth, so a replay is *verified* rather than
believed.

Usage::

    result = await discover("https://jobs.apple.com/en-ca/search?search=engineer")
    if result.ok:
        cached = result.plan.to_json()          # replay later, no browser
        response = await execute(session, RequestPlan.from_json(cached))
"""

from .observe import Capture, ChallengeEncounteredError, DiscoveryUnavailableError, Exchange, capture
from .oracle import Signature, signature, signatures_match
from .pipeline import Discovery, close_session, discover
from .plan import MintFailedError, MintStep, PlanUnresolvedError, RequestPlan, execute, mint_values
from .ranking import Candidate, best, rank
from .store import (
    Replay,
    forget_plan,
    load_plan,
    looks_decayed,
    replay,
    resolve_plan_for,
    save_plan,
)

__all__ = [
    "Candidate",
    "Capture",
    "ChallengeEncounteredError",
    "Discovery",
    "DiscoveryUnavailableError",
    "Exchange",
    "MintFailedError",
    "MintStep",
    "PlanUnresolvedError",
    "RequestPlan",
    "Signature",
    "best",
    "capture",
    "close_session",
    "discover",
    "execute",
    "forget_plan",
    "load_plan",
    "looks_decayed",
    "mint_values",
    "rank",
    "Replay",
    "replay",
    "resolve_plan_for",
    "save_plan",
    "signature",
    "signatures_match",
]
