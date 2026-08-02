"""Tracing a volatile value back to the request that minted it.

**Verbatim first.** A captured request whose tokens are still accepted needs no
minting machinery at all, and the simplest plan that works is the one most
likely to keep working. Measured: Meta's ``lsd`` and Apple's
``x-apple-csrf-token`` are both still accepted stale, from a fresh session with
no cookies.

Provenance matters anyway, because a value the plan can *re-mint* is more
durable than a literal that happens to still work today — and a CSRF token is
exactly the kind of value that stops working later for no visible reason. So
after minimization, surviving values are swapped for mint steps even when
verbatim worked, and the swap is kept only if the answer is unchanged.

On Meta this converts ``lsd`` into a re-mint of ``["LSD",[],{"token":"…"}]``
from the page — the same regex ``meta_careers/api.py`` already uses, derived
automatically rather than written by hand.
"""

from __future__ import annotations

import re

from .plan import MintStep

# Below this, a value is not distinctive enough to be worth tracing; short
# values match everywhere and produce nonsense anchors.
MIN_VOLATILE_LEN = 12

# How much text before the value anchors its pattern.
_PREFIX_WINDOW = 48

# An unanchored pattern degenerates into "any run of token characters" and
# re-mints the first random string in the document. Refuse rather than guess.
MIN_PREFIX_CONTEXT = 8

_TOKEN_CHARS_RE = re.compile(r"[A-Za-z0-9_-]+")
_MARKER_RE = re.compile(r"\{\{mint:([A-Za-z0-9_]+)\}\}")
_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9]+")


def marker(name: str) -> str:
    return "{{mint:" + name + "}}"


def marker_names(text: str) -> set[str]:
    return set(_MARKER_RE.findall(text or ""))


def anchored_pattern(text: str, value: str) -> str | None:
    """A regex whose group(1) recovers ``value`` from ``text``."""
    position = text.find(value)
    if position < 0:
        return None
    prefix = text[max(0, position - _PREFIX_WINDOW) : position]
    if len(prefix) < MIN_PREFIX_CONTEXT:
        return None
    low = max(8, len(value) // 2)
    high = max(low, len(value) * 3)
    span = "{" + f"{low},{high}" + "}"
    if _TOKEN_CHARS_RE.fullmatch(value):
        capture = r"([A-Za-z0-9_-]" + span + ")"
    else:
        capture = r"([^\"'<>\s]" + span + ")"
    pattern = re.escape(prefix) + capture

    # Verify the pattern against the text it was built from. Neither character
    # class can represent a value containing whitespace or a quote, so a
    # pattern can be produced that does not recover its own value — and a mint
    # step whose regex captures the wrong thing fails at the origin as an
    # ordinary-looking rejection, which is the confusion this exists to remove.
    match = re.search(pattern, text)
    if match is None or match.group(1) != value:
        return None
    return pattern


def _step_name(key: str, taken: set[str]) -> str:
    base = _NAME_SAFE_RE.sub("_", key.split(":", 1)[-1]).strip("_").upper() or "TOKEN"
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    taken.add(name)
    return name


def trace(value: str, *, exchanges, page_url: str, page_html: str, before_order: int):
    """Find where ``value`` came from, or None.

    Only exchanges *earlier* than the target are searched — a value cannot have
    been minted by a request that had not happened yet.

    Mint sources are restricted to GET exchanges and the page itself. A POST
    mint step would need its own body reproduced to be replayable, and a mint
    step that cannot be replayed is worse than a literal.
    """
    earlier = [
        e
        for e in exchanges
        if e.order < before_order and e.method.upper() == "GET" and 200 <= e.status < 300
    ]

    # 1. An earlier response header equal to it exactly. Unambiguous.
    for exchange in earlier:
        for header, header_value in exchange.response_headers.items():
            if header_value == value:
                return MintStep(
                    name="",
                    method="GET",
                    url=exchange.url,
                    source="header",
                    selector=header,
                )

    # 2. An earlier response body containing it.
    for exchange in earlier:
        if not exchange.body or value not in exchange.body:
            continue
        pattern = anchored_pattern(exchange.body, value)
        if pattern:
            return MintStep(name="", method="GET", url=exchange.url, source="regex", selector=pattern)

    # 3. The settled page HTML.
    if page_html and value in page_html:
        pattern = anchored_pattern(page_html, value)
        if pattern:
            return MintStep(name="", method="GET", url=page_url, source="regex", selector=pattern)

    return None


def _traceable(value) -> bool:
    return isinstance(value, str) and len(value) >= MIN_VOLATILE_LEN


def build_mint_steps(
    fields: dict,
    *,
    exchanges,
    page_url: str,
    page_html: str,
    before_order: int,
) -> tuple[dict, tuple[MintStep, ...]]:
    """Replace traceable values with ``{{mint:NAME}}`` markers.

    Values are deduplicated **by value**: a CSRF token often feeds both a header
    and the body, and the two must share one mint step. Otherwise every replay
    refetches the same page once per use, and the two copies can disagree.
    """
    by_value: dict[str, str] = {}
    steps: list[MintStep] = []
    taken: set[str] = set()
    out: dict = {}

    def convert(key: str, value):
        if isinstance(value, list):
            return [convert(key, item) for item in value]
        if not _traceable(value):
            return value
        if value in by_value:
            return marker(by_value[value])
        step = trace(
            value,
            exchanges=exchanges,
            page_url=page_url,
            page_html=page_html,
            before_order=before_order,
        )
        if step is None:
            return value
        name = _step_name(key, taken)
        by_value[value] = name
        steps.append(
            MintStep(name=name, method=step.method, url=step.url, source=step.source, selector=step.selector)
        )
        return marker(name)

    for key, value in fields.items():
        out[key] = convert(key, value)

    # Deduplicate steps that describe the same fetch and extraction.
    unique: list[MintStep] = []
    seen: set[tuple[str, str, str, str]] = set()
    for step in steps:
        key = (step.method, step.url, step.source, step.selector)
        if key in seen:
            continue
        seen.add(key)
        unique.append(step)
    return out, tuple(unique)
