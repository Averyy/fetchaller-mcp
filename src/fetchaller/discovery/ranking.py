"""Deciding which captured exchange is the data.

The hardest part of discovery, because the two obvious signals disagree and
**neither is correct alone**:

*How much of the payload is on screen* puts the chrome first. Measured on
Netflix's Eightfold board, ``/api/apply/v2/branding`` covers 1.00 (31 of 31
values, all of them navigation labels — ``CAREERS``, ``LOCATIONS``,
``CULTURE MEMO``) and scores 18.15, while the actual listing covers 0.29 and
scores 8.94. The branding blob wins by 2x.

*Largest record set* puts a lookup table first. Workday serves
``/wday/cxs/{tenant}/videoplayerlabels``: 334 entries shaped
``{"key": ..., "label": ...}``, structurally identical to job postings and five
times more numerous than the 70 real ones.

No weighting fixes both. Picking the listing on Netflix needs weight > 27 on the
record term; picking ``/jobs`` on Workday needs weight < 21.6. The cases are
directly opposed.

**What resolves it: chrome does not depend on the query.** The page URL says
what was searched, and a payload that mentions it is the one that answered the
search. Branding never mentions "engineer"; the listing does. So a query hint,
when there is one, outranks both signals — and score is only the fallback for
boards whose URL carries no query at all.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from .observe import Capture, Exchange, registrable_domain
from .payload import (
    HEAD_WINDOW,
    collection_size,
    decode_payload,
    distinctive_values,
    looks_rate_limited,
)

# A value in the page URL only counts as a hint if it reads as a word. Analytics
# blobs like Google's `_gl=1*16occzp*_up*MQ..` fail this and are ignored.
_HINT_RE = re.compile(r"[A-Za-z][A-Za-z0-9 ,._'+-]*")
_HINT_MIN_LEN = 3
_HINT_MAX_LEN = 60
_HINT_STOPWORDS = frozenset(
    {"true", "false", "asc", "desc", "relevant", "recent", "date", "all", "any", "none", "null"}
)

_DATA_CONTENT_TYPES = ("json", "javascript", "text/plain", "ndjson", "xml")

# Sampling for the reverse signal. Long words only, and only rare ones: common
# words match everything and evidence nothing.
_DOM_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]{7,}")
_DOM_TOKEN_SAMPLE = 30

_SCRIPTISH_TAGS_RE = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")

# Refusing to answer is a real outcome. Uber's board hits this correctly:
# jobs.uber.com is server-rendered Next.js whose only XHRs are RSC flight
# responses, and reporting "no data request" beats returning a plan that
# fetches a page and calls it an API.
_MIN_VALUES_TO_ANSWER = 5


def visible_text(html: str) -> str:
    """Rendered text, with script/style content removed."""
    if not html:
        return ""
    stripped = _SCRIPTISH_TAGS_RE.sub(" ", html)
    return " ".join(_TAG_RE.sub(" ", stripped).split())


def query_hints(url: str) -> list[str]:
    """Query-string values from the page URL that read as words."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return []
    hints: list[str] = []
    seen: set[str] = set()
    for _name, value in parse_qsl(query, keep_blank_values=False):
        text = (value or "").strip()
        if not (_HINT_MIN_LEN <= len(text) <= _HINT_MAX_LEN):
            continue
        if not _HINT_RE.fullmatch(text):
            continue
        lowered = text.casefold()
        if lowered in _HINT_STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        hints.append(text)
    return hints


def dom_sample(text: str, size: int = _DOM_TOKEN_SAMPLE) -> list[str]:
    """Rare long words from the rendered page, for the reverse signal."""
    counts = Counter(m.group(0).casefold() for m in _DOM_TOKEN_RE.finditer(text))
    rare = [word for word, n in counts.items() if n <= 2]
    rare.sort()
    return rare[:size]


def _coverage(values: list[str], page_text_lower: str) -> tuple[float, float]:
    """Fraction of the payload's head-window values that are on screen.

    Only the first :data:`HEAD_WINDOW` values are considered. A listing renders
    the top of its list and virtualizes the rest, so coverage over the whole
    payload understates the very payload being sought — measured on Meta, 3% of
    all job titles were on the page but 62% of the first forty values were.
    """
    head = values[:HEAD_WINDOW]
    if not head:
        return 0.0, 0.0
    hits = sum(1 for v in head if v.casefold() in page_text_lower)
    # A payload offering two strings must not score full marks on both landing.
    confidence = min(1.0, len(head) / 8.0)
    return hits / len(head), confidence


def _dom_overlap(sample: list[str], body_lower: str) -> float:
    if not sample:
        return 0.0
    return sum(1 for word in sample if word in body_lower) / len(sample)


@dataclass
class Candidate:
    """A scored, decoded exchange."""

    exchange: Exchange
    decoded: object
    parsed: bool
    records: int
    values: list[str]
    score: float
    coverage: float
    on_subject: bool
    same_site: bool

    @property
    def url(self) -> str:
        return self.exchange.url


def _is_data_content_type(content_type: str) -> bool:
    return any(marker in content_type for marker in _DATA_CONTENT_TYPES)


def _score(
    exchange: Exchange,
    *,
    records: int,
    values: list[str],
    page_text_lower: str,
    dom_words: list[str],
    same_site: bool,
    is_page_document: bool,
    expect_matched: bool | None,
) -> tuple[float, float]:
    body_lower = exchange.body.casefold()
    coverage, confidence = _coverage(values, page_text_lower)
    overlap = _dom_overlap(dom_words, body_lower)

    score = 100.0 if expect_matched else 0.0
    score += 12.0 * coverage * confidence
    score += 4.0 * overlap
    if _is_data_content_type(exchange.content_type):
        score += 3.0
    if exchange.method.upper() != "GET":
        score += 2.0
    score += min(2.0, math.log10(len(exchange.body) + 1) / 3.0)
    score += min(3.0, math.log10(records + 1))
    if not same_site and overlap == 0.0:
        score -= 5.0
    # Penalize the navigation document, never exclude it: an SSR board can
    # answer the navigation with the payload itself.
    if is_page_document and not _is_data_content_type(exchange.content_type):
        score -= 4.0
    return score, coverage


def _normalized(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{parts.query}"


def rank(capture: Capture, *, expect: str | None = None) -> list[Candidate]:
    """Score and order the captured exchanges, best first."""
    page_text_lower = visible_text(capture.html).casefold()
    dom_words = dom_sample(page_text_lower)
    page_site = registrable_domain(capture.host)
    page_document = _normalized(capture.url)

    # Hints come from what was *asked for* as well as where the browser
    # landed: a redirect that drops the query drops the only thing separating
    # the listing from the chrome.
    if expect:
        hints = [expect]
    else:
        hints = query_hints(capture.url)
        for hint in query_hints(capture.requested_url or ""):
            if hint.casefold() not in {h.casefold() for h in hints}:
                hints.append(hint)
    hints_lower = [h.casefold() for h in hints if h]

    candidates: list[Candidate] = []
    for exchange in capture.exchanges:
        if not exchange.is_data_type:
            continue
        if not (200 <= exchange.status < 300):
            continue
        if not exchange.body:
            continue

        body_lower = exchange.body.casefold()
        decoded = decode_payload(exchange.body)
        parsed = decoded is not None
        # A throttle notice is a refusal, not data. Left in, Meta's 114-byte
        # "Rate limit exceeded" reads as merely thin and a routing payload wins.
        if parsed and looks_rate_limited(decoded):
            continue
        records = collection_size(decoded) if parsed else 0
        values = distinctive_values(decoded) if parsed else []

        # Match hints against the payload's own *values*, never its raw text.
        # A routing payload echoes the page URL back as a key — Meta's
        # /ajax/bulk-route-definitions/ contains the literal string
        # "/jobsearch/?q=engineer" — so a raw-text test marks it on-subject and
        # it can then win the record tiebreak against the real results query.
        # distinctive_values() walks values only and drops URL-shaped strings,
        # so the echo cannot masquerade as content.
        subject_text = "\n".join(values).casefold() if values else ""

        expect_matched: bool | None = None
        if expect:
            # An explicit expectation is decisive: a payload that does not
            # contain it is not the answer, whatever else it scores. Checked
            # against values first, falling back to raw text so a caller
            # naming something outside the payload's string leaves still works.
            expect_matched = expect.casefold() in subject_text or expect.casefold() in body_lower
            if not expect_matched:
                continue
        same_site = registrable_domain(exchange.host) == page_site
        is_page_document = (
            exchange.resource_type == "document" and _normalized(exchange.url) == page_document
        )

        score, coverage = _score(
            exchange,
            records=records,
            values=values,
            page_text_lower=page_text_lower,
            dom_words=dom_words,
            same_site=same_site,
            is_page_document=is_page_document,
            expect_matched=expect_matched,
        )
        if score <= 0:
            continue

        candidates.append(
            Candidate(
                exchange=exchange,
                decoded=decoded,
                parsed=parsed,
                records=records,
                values=values,
                score=score,
                coverage=coverage,
                on_subject=any(hint in subject_text for hint in hints_lower),
                same_site=same_site,
            )
        )

    if any(c.on_subject for c in candidates):
        # Same registrable domain stays the outermost key: a consent or
        # analytics vendor can ship a larger array than the board does —
        # Netflix's cookielaw payload has 200 records.
        candidates.sort(
            key=lambda c: (not c.same_site, not c.on_subject, -c.records, -c.score)
        )
    else:
        candidates.sort(key=lambda c: (not c.same_site, -c.score))
    return candidates


def is_answerable(candidate: Candidate, *, expect: str | None = None) -> bool:
    """Whether a candidate is solid enough to build a plan from.

    Ranking must be allowed to reject everything, and the bar is deliberately
    higher than "it parsed and had some strings in it". Meta's
    ``bulk-route-definitions`` payload carries 309 distinct values and **zero**
    records; on a capture where the real results were server-rendered it was the
    highest-scoring candidate left, so a values-only rule handed back a routing
    endpoint with complete confidence. That is worse than refusing.

    **Deliberate deviation from the original spec**, which also accepted any
    payload with >= 5 distinctive values. That rule was written before Meta's
    routing payload was observed, and it is what let it through: restricting the
    match to on-subject values did not help either, because the route definition
    stores the parsed query parameter as a literal value — the bare string
    ``"engineer"`` is in there. Two separate captures ended with discovery
    confidently returning a routing endpoint that carries no postings at all.

    So a record set is now required outright, unless the caller supplied
    ``expect`` and it matched. No board among the seven validated needs the
    looser rule: every real listing carries a record set, and the boards that
    carry none (Uber, and Meta whenever it server-renders its results) are
    exactly the ones that *should* be refused. ``expect`` remains the escape
    hatch for a genuinely non-list payload.
    """
    if expect:
        return True
    return candidate.parsed and candidate.records > 0


def best(capture: Capture, *, expect: str | None = None) -> Candidate | None:
    for candidate in rank(capture, expect=expect):
        if is_answerable(candidate, expect=expect):
            return candidate
    return None
