"""Name the timeout that actually fired.

``wafer.WaferTimeout`` subclasses ``TimeoutError``, so one request that runs
past its session's own limit lands in the same ``except TimeoutError`` as the
tool's overall budget, and was reported as that budget running out: "Oracle
Recruiting search timed out after 90s" arrived 62.6s in, from a single request
that got no answer within the session's 60s (2026-09-25). The fix for each is
different — retrying the one request versus asking for less — so say which.
"""

from __future__ import annotations

import wafer


def timeout_error(what: str, budget: float, exc: BaseException) -> str:
    if isinstance(exc, wafer.WaferTimeout):
        return (
            f"{what} failed: one request got no answer within {exc.timeout_secs:g}s "
            f"(the {budget:.0f}s budget had not run out). Try again shortly."
        )
    return f"{what} timed out after {budget:.0f}s."
