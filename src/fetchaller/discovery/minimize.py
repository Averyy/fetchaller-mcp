"""Reducing a captured request to the parts the endpoint actually needs.

Delta debugging over **one combined mapping** of headers, query parameters and
body fields, namespaced ``header:``, ``query:`` and ``field:``.

Minimizing all three together is what makes it work. An endpoint may demand an
``Origin`` it never validates. A POST's build ids live in its *query string*,
not its body: on Google, minimization drops
``bl=boq_corp-hiring-boq-cportal-frontend_20260728.05_p0``, ``f.sid`` and
``_reqid``, leaving a single field. Those are exactly the values that make a
pinned request rot at the next deploy, and none of them are in the body.

What survives becomes ``required_fields``; what is dropped is surfaced too,
because that list is how a caller learns which parameters the endpoint accepts.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Every probe is a live request, so the budget is small. On exhaustion the
# smallest passing subset found so far is returned rather than nothing.
DEFAULT_MAX_PROBES = 48

HEADER = "header:"
QUERY = "query:"
FIELD = "field:"


def partition(items: list, n: int) -> list[list]:
    """Split ``items`` into at most ``n`` roughly equal chunks."""
    if n <= 1 or len(items) <= 1:
        return [list(items)] if items else []
    n = min(n, len(items))
    size, extra = divmod(len(items), n)
    chunks: list[list] = []
    start = 0
    for index in range(n):
        stop = start + size + (1 if index < extra else 0)
        chunks.append(items[start:stop])
        start = stop
    return [c for c in chunks if c]


def _select(fields: dict, order: list[str], required: set[str], names) -> dict:
    included = required | set(names)
    return {name: fields[name] for name in order if name in included}


async def ddmin(
    fields: dict,
    probe,
    *,
    required=(),
    max_probes: int = DEFAULT_MAX_PROBES,
) -> tuple[dict, tuple[str, ...], int]:
    """Smallest subset of ``fields`` for which ``probe`` still passes.

    ``probe`` is an async callable taking a candidate mapping and returning
    whether the request still returns the same answer.

    Returns ``(kept, dropped, probes_spent)``.
    """
    order = list(fields)
    required_names = {name for name in required if name in fields}
    removable = [name for name in order if name not in required_names]
    cache: dict[frozenset, bool] = {}
    probes = 0

    def finish(current) -> tuple[dict, tuple[str, ...], int]:
        kept = _select(fields, order, required_names, current)
        dropped = tuple(name for name in order if name not in kept)
        return kept, dropped, probes

    async def test(names) -> bool | None:
        """True/False, or None when the probe budget is exhausted."""
        nonlocal probes
        current = _select(fields, order, required_names, names)
        key = frozenset(current)
        if key in cache:
            return cache[key]  # memo hits are free
        if probes >= max_probes:
            return None
        probes += 1
        cache[key] = result = bool(await probe(current))
        return result

    # You cannot minimize what does not work, and shipping a shrunken broken
    # request is worse than reporting failure. If the full set already fails,
    # hand it back untouched for the caller to mark unverified.
    if await test(removable) is not True:
        return dict(fields), (), probes

    current = removable
    n = min(2, len(removable))
    while current:
        chunks = partition(current, n)
        progressed = False

        for chunk in chunks:  # each chunk alone
            if len(chunk) == len(current):
                continue
            result = await test(chunk)
            if result is None:
                return finish(current)
            if result:
                current, n, progressed = chunk, min(max(2, n - 1), len(chunk)), True
                break
        if progressed:
            continue

        for chunk in chunks:  # then each complement
            names = set(chunk)
            complement = [name for name in current if name not in names]
            result = await test(complement)
            if result is None:
                return finish(current)
            if result:
                current, n, progressed = complement, min(max(2, n - 1), len(complement)), True
                break
        if progressed:
            continue

        if n >= len(current):
            break
        n = min(len(current), n * 2)

    return finish(current)
