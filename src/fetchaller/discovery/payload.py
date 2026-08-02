"""Decoding and measuring a captured response body.

Everything here exists because a real board's data does not arrive as plain
JSON. Four shapes beyond it were measured live, and each one reads as *zero
records* if it is not handled:

- **Anti-hijacking guards.** Google's ``batchexecute`` prefixes ``)]}'``.
- **NDJSON.** Meta's GraphQL responses are newline-delimited.
- **Length-prefixed chunk streams.** ``batchexecute`` interleaves a bare number
  with each JSON array, so a naive line scan decodes the chunk length ``1234``
  and concludes the payload is a scalar.
- **JSON nested inside a string.** Positional RPCs put the real payload in a
  string field. Without following it, Google's 110 KB of job data measures as 3
  records rather than 21.

The measurements here feed two consumers with opposite needs — ranking wants to
know *how much of this payload is on screen*, the oracle wants to know *is this
the same answer as before* — so both read the same primitives.
"""

from __future__ import annotations

import json
import re

# Recursion cap. Deep enough for every board measured; bounded so a
# self-referential or pathologically nested payload cannot hang the pass.
MAX_DEPTH = 12

# A response body's distinctive strings are the ones worth looking for in the
# rendered page. URLs and data URIs are excluded: they appear in both regardless
# of subject, so they inflate coverage without evidencing anything.
_MIN_VALUE_LEN = 6
_MAX_VALUE_LEN = 120
_VALUE_PREFIX_SKIP = ("http://", "https://", "/", "data:")

# A listing renders the top of its list and virtualizes the rest, so coverage
# measured over the whole payload understates the very payload being sought.
# Measured on Meta: of 466 job titles in the payload 14 were on the page (3%),
# but 62% of the first forty values were.
HEAD_WINDOW = 40

_GUARDS = (")]}'", ")]}", "while(1);", "for (;;);", "for(;;);")


def strip_json_guard(text: str) -> str:
    """Remove a leading anti-hijacking guard, if present."""
    if not isinstance(text, str):
        return ""
    stripped = text.lstrip()
    for guard in _GUARDS:
        if stripped.startswith(guard):
            return stripped[len(guard) :].lstrip(",\n\r \t")
    return text


def decode_payload(text: str):
    """Plain JSON, guarded JSON, NDJSON, a chunk stream, or a Flight stream."""
    if not text:
        return None
    # NOTE: Flight decoding is implemented and tested (:func:`decode_flight`)
    # but deliberately NOT wired in here. See that function for the measurement.
    body = strip_json_guard(text)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        # Skip the bare chunk lengths batchexecute interleaves between arrays.
        if isinstance(decoded, (dict, list)):
            return decoded
    return None


# React Server Components "Flight" framing: `<hex-row-id>:<payload>`, where the
# payload is a module import (`I[...]`), a hint (`H[...]`), a length-prefixed
# text blob (`T<hexlen>,<chars>`) or plain JSON. Next.js serves it as
# `text/x-component`.
# No ``^``: this is used with ``.match(text, pos)``, which already anchors at
# pos, whereas ``^`` would assert string-start and so match only the first row.
_FLIGHT_ROW_RE = re.compile(r"([0-9a-f]{1,6}):")
# Transport rows: the protocol's own module table, never application data.
_FLIGHT_TRANSPORT_PREFIXES = ("I", "H", "E")
_FLIGHT_MIN_ROWS = 3


def looks_like_flight(text: str) -> bool:
    """Whether a body is a React Flight stream rather than JSON."""
    if not text or ":" not in text[:64]:
        return False
    rows = imports = 0
    for line in text.split("\n"):
        match = _FLIGHT_ROW_RE.match(line)
        if not match:
            continue
        rows += 1
        if line[match.end() : match.end() + 1] == "I":
            imports += 1
    # A module table is what distinguishes Flight from anything else that
    # happens to start lines with hex and a colon.
    return rows >= _FLIGHT_MIN_ROWS and imports >= 1


def decode_flight(text: str) -> dict | None:
    """Decode a Flight stream into ``{row_id: value}``.

    **Correct, tested, and deliberately not wired into :func:`decode_payload`.**

    It was built to close the obvious gap — Next.js boards serve
    ``text/x-component``, which otherwise measures as zero records. Enabling it
    made discovery *confidently wrong* about the one board available to test it
    against, which is worse than the honest refusal it replaced:

    - Uber's Flight decodes cleanly: 32 rows, 401 distinctive values.
    - The largest record set in it is
      ``siteSettings.properties.navigation.locales.en.explore`` — **16
      navigation menu entries**, byte-identical between the job-detail page and
      the list page. Chrome, not postings.
    - There is no job record set anywhere in it. Uber renders postings into
      React element trees (excluded by :func:`_is_record_list`) and serves the
      real search from Oracle Fusion, server-side.
    - With it on, discovery returned ``GET jobs.uber.com/en/`` with "16
      records", marked ``verified``. The query hint could not save it: Uber's
      nav contains "Engineering", so ``engineer`` matches the chrome.

    Wiring it in therefore buys nothing real and costs a false positive. It is
    kept because the *format* handling is right and the next Next.js board may
    genuinely expose records — at which point this needs a board where it
    demonstrably helps before being switched on, plus a discriminator stronger
    than a substring hint.

    Keyed by row id rather than returned as a list on purpose: the row table is
    the protocol's own structure, and a list of similarly-shaped rows would be
    counted as a record set. Only what the rows *carry* should count.

    Module (``I``), hint (``H``) and error (``E``) rows are transport and are
    dropped. Text rows are length-prefixed in hex and may contain newlines, so
    they are consumed by length rather than by line.
    """
    if not text:
        return None
    rows: dict[str, object] = {}
    position = 0
    length = len(text)
    while position < length:
        newline = text.find("\n", position)
        line_end = length if newline == -1 else newline
        match = _FLIGHT_ROW_RE.match(text, position, line_end)
        if not match:
            position = line_end + 1
            continue
        row_id = match.group(1)
        body_start = match.end()
        kind = text[body_start : body_start + 1]

        if kind == "T":
            # T<hexlen>,<chars> — the length is authoritative, and the payload
            # can span newlines, so a line-oriented read would truncate it.
            comma = text.find(",", body_start)
            if comma == -1:
                position = line_end + 1
                continue
            try:
                size = int(text[body_start + 1 : comma], 16)
            except ValueError:
                position = line_end + 1
                continue
            start = comma + 1
            rows[row_id] = text[start : start + size]
            position = start + size + 1
            continue

        position = line_end + 1
        if kind in _FLIGHT_TRANSPORT_PREFIXES:
            continue
        try:
            rows[row_id] = json.loads(text[body_start:line_end])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return rows or None


def nested(value):
    """A string that is itself a JSON document, else None."""
    if not isinstance(value, str):
        return None
    candidate = value.lstrip()
    if not candidate.startswith(("[", "{")) or len(candidate) < 8:
        return None
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, (dict, list)) else None


def json_shape(value, prefix: str = "", depth: int = 0) -> frozenset:
    """Dotted key paths with array indices collapsed to ``[]``.

    A 20-record and a 200-record response of the same kind share a shape, which
    is what lets the oracle compare answers whose sizes legitimately differ.
    """
    if depth > MAX_DEPTH:
        return frozenset()

    # A container always contributes its *own* path, whether or not it has
    # contents. Two things depend on that, and they pull in opposite directions:
    #
    # Emitting nothing for an empty container makes
    # ``{"query":"x","results":[]}`` and ``{"query":"x","errors":[]}`` share a
    # shape — and since both also report zero records and the same values, they
    # falsely verify as the same answer.
    #
    # But emitting *only* a leaf marker for the empty case is just as wrong the
    # other way: a ``facets`` list that is empty in the browser's answer and
    # populated in a replay would then share no path at all for that key, the
    # Jaccard overlap would collapse, and a correct replay would be rejected.
    # Emitting the container path in both cases makes the empty shape a proper
    # subset of the populated one, which rule 2 already accepts.
    if isinstance(value, dict):
        paths: set[str] = {prefix} if prefix else set()
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths |= json_shape(child, child_prefix, depth + 1)
        return frozenset(paths)
    if isinstance(value, list):
        marker = prefix + "[]"
        if not value:
            return frozenset({marker})
        return frozenset({marker}) | json_shape(value[0], marker, depth + 1)
    return frozenset({prefix})


def is_record_list(items) -> bool:
    """Whether a list's entries share enough structure to be a record set.

    The two-key floor excludes Amazon's facet arrays, whose entries are dicts
    with a *single* key each and a *different* key each
    (``[{"job_function_corporate_80rdb4": 6286}, ...]``). The positional branch
    includes Google's 21-slot job arrays.
    """
    if not isinstance(items, list) or len(items) < 2:
        return False
    sample = [i for i in items[:8] if isinstance(i, (dict, list))]
    if len(sample) < 2:
        return False
    if all(isinstance(i, dict) for i in sample):
        shared = set(sample[0])
        for item in sample[1:]:
            shared &= set(item)
        return len(shared) >= 2
    if all(isinstance(i, list) for i in sample):
        # React element nodes are 4-wide positional arrays — ["$", type, key,
        # props] — so a rendered component tree looks exactly like a positional
        # record set. Measured on Uber: a job *detail* page decodes to 32 of
        # them, which would have read as 32 job records and made every Next.js
        # page answerable. Markup is not data.
        if all(_is_react_element(i) for i in sample):
            return False
        widths = {len(i) for i in sample}
        return len(widths) == 1 and next(iter(widths)) >= 3
    return False


def _is_react_element(value) -> bool:
    """Whether a positional array is a React element node rather than a record.

    Flight tags elements with a ``$`` sentinel in slot 0 — bare ``"$"`` for an
    element, ``"$L…"``/``"$S…"`` for a lazy or symbol reference.
    """
    return (
        isinstance(value, list)
        and len(value) >= 1
        and isinstance(value[0], str)
        and value[0].startswith("$")
    )


def collection_size(value, depth: int = 0) -> int:
    """Largest list of structurally similar entries anywhere in the payload.

    Follows JSON nested inside string values, because a positional protocol
    hides its records there.
    """
    if depth > MAX_DEPTH:
        return 0
    best = 0
    if isinstance(value, dict):
        for child in value.values():
            best = max(best, collection_size(child, depth + 1))
        return best
    if isinstance(value, list):
        for child in value:
            best = max(best, collection_size(child, depth + 1))
        if is_record_list(value):
            best = max(best, len(value))
        return best
    inner = nested(value)
    return collection_size(inner, depth + 1) if inner is not None else 0


# Phrases an API uses to say "slow down" inside an otherwise successful
# response. Deliberately short and generic — matched against decoded string
# values only, never raw body text, so a posting that mentions "quota" in its
# description cannot trip it.
_THROTTLE_PHRASES = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "quota exceeded",
    "throttled",
    "slow down",
)


def looks_rate_limited(decoded) -> bool:
    """Whether a decoded 2xx payload is actually a throttle notice.

    The most expensive silent failure in this package, and the one it was built
    to catch, arriving one layer up: a well-formed ``HTTP 200`` that means
    "we are refusing you" while looking like "there is no data".

    Measured on Meta — the browser issues its real search query and gets::

        HTTP 200, 114 bytes
        {"errors":[{"message":"Rate limit exceeded","code":1675004}], ...}

    Ranking then discards it as too thin, leaving a 42,685-byte routing payload
    as the largest same-host candidate, and discovery minimizes *that*. The
    board looked like it had no search API. It has one; it was declining to
    answer.
    """
    if not isinstance(decoded, (dict, list)):
        return False
    # An errors-bearing payload with no usable data is the shape that matters;
    # a partial success carrying both should still be treated as data.
    if isinstance(decoded, dict):
        data = decoded.get("data")
        if isinstance(data, (dict, list)) and data:
            return False
    for text in _walk_values(decoded):
        lowered = text.casefold()
        if any(phrase in lowered for phrase in _THROTTLE_PHRASES):
            return True
    return False


def _walk_values(value, depth: int = 0):
    """Distinctive string leaves in document order, following nested JSON."""
    if depth > MAX_DEPTH:
        return
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child, depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            yield from _walk_values(child, depth + 1)
        return
    if not isinstance(value, str):
        return
    inner = nested(value)
    if inner is not None:
        yield from _walk_values(inner, depth + 1)
        return
    text = value.strip()
    if _MIN_VALUE_LEN <= len(text) <= _MAX_VALUE_LEN and not text.startswith(_VALUE_PREFIX_SKIP):
        yield text


def distinctive_values(value, limit: int = 0) -> list[str]:
    """Deduplicated distinctive strings, in document order.

    Order matters: ranking reads only the first ``HEAD_WINDOW`` of these,
    because that is the part of a listing the page actually renders.
    """
    out: list[str] = []
    seen: set[str] = set()
    for text in _walk_values(value):
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if limit and len(out) >= limit:
            break
    return out
