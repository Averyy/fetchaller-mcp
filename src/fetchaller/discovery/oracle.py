"""Deciding whether a replayed request returned the same answer.

The browser's own response is ground truth, so a replay can be *checked* rather
than believed. This is the whole reason discovery is trustworthy: these APIs
report a malformed request as ``HTTP 200`` with an empty-looking result, which
is indistinguishable from "there is no data" unless something knows what the
right answer looked like.

Equality is useless — timestamps, request ids and ordering all vary between two
correct responses. So a signature is compared instead, and every threshold here
was tuned against a live board rather than chosen.

Two of the five rules exist only because the minimizer silently broke a plan
without them:

- **Rule 3's upper bound.** Without it, minimization drops a filter, gets the
  *unfiltered* listing back, sees the same shape and *more* records, and calls
  it a match. The cached plan then silently searches everything.
- **Rule 4.** The upper bound is not enough on its own: where page size caps the
  result, dropping the query does not move the record count at all. Apple
  answers a search for "engineer" and a search for nothing with the same twenty
  rows. Only comparing the payload's *content* catches it — without this rule,
  minimization dropped ``query: "engineer"`` from Apple's plan and reported
  success.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .payload import collection_size, decode_payload, distinctive_values, json_shape

# Tuned thresholds. Changing either without a live re-measurement is how the
# silent-empty failures come back.
MIN_SHAPE_OVERLAP = 0.85
MIN_CONTENT_OVERLAP = 0.5
RECORD_TOLERANCE = 2.0

# How many of the payload's own distinctive strings rule 4 tracks. Compared as
# a set, so a reordered but equivalent answer still matches.
SIGNATURE_SAMPLE = 80


@dataclass(frozen=True)
class Signature:
    """What is compared between the browser's answer and a replay's."""

    parsed: bool
    length: int
    records: int
    shape: frozenset = field(default_factory=frozenset)
    values: frozenset = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        return self.length == 0


def _sample_key(value: str) -> bytes:
    return hashlib.blake2b(value.encode("utf-8", "replace"), digest_size=8).digest()


def signature(text: str) -> Signature:
    """Measure a response body."""
    text = text or ""
    decoded = decode_payload(text)
    if decoded is None:
        return Signature(parsed=False, length=len(text), records=0)
    # Sampled by a stable hash of each value, not by document order and not
    # lexically. Rule 4 compares these as a set, so the sample must be:
    #
    # - **order-independent**, or a legitimately reordered answer samples a
    #   different slice and reads as a subject change; and
    # - **insertion-unbiased**, which rules out sorting. Lexical bottom-k looks
    #   order-independent but a board that adds records sharing a prefix — a
    #   rolling page of "Android …" titles — evicts the entire observed sample
    #   at once and rejects a correct request.
    #
    # hashlib rather than hash(): str hashing is salted per process, so a plan
    # verified in one process would not compare equal in the next.
    values = sorted(distinctive_values(decoded), key=_sample_key)[:SIGNATURE_SAMPLE]
    return Signature(
        parsed=True,
        length=len(text),
        records=collection_size(decoded),
        shape=json_shape(decoded),
        values=frozenset(values),
    )


def signatures_match(
    observed: Signature,
    candidate: Signature,
    *,
    min_shape_overlap: float = MIN_SHAPE_OVERLAP,
    min_content_overlap: float = MIN_CONTENT_OVERLAP,
) -> bool:
    """Whether ``candidate`` is the same answer as ``observed``."""
    if observed.parsed != candidate.parsed:
        return False

    if observed.parsed:
        union = observed.shape | candidate.shape
        overlap = len(observed.shape & candidate.shape) / len(union) if union else 1.0
        # A subset is accepted outright: a correct answer may legitimately omit
        # optional keys the browser's response happened to carry.
        if not (
            overlap >= min_shape_overlap
            or (observed.shape and observed.shape <= candidate.shape)
        ):
            return False

        if observed.records > 0:
            floor = max(1, observed.records * 0.5)
            if candidate.records < floor:
                return False  # the silent-empty trap
            if candidate.records > max(floor, observed.records * RECORD_TOLERANCE):
                return False  # a filter stopped applying
        elif candidate.records != 0:
            return False

        # Same shape, same size, different subject. Catches a dropped query
        # where the page size hides the loss from the record count.
        if observed.values:
            retained = len(observed.values & candidate.values) / len(observed.values)
            if retained < min_content_overlap:
                return False
        return True

    if candidate.length <= 0:
        return False
    return observed.length * 0.5 <= candidate.length <= observed.length * RECORD_TOLERANCE
