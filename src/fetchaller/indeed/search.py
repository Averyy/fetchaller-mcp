"""Public entry points: ``search_indeed`` and ``get_indeed_job``."""

from __future__ import annotations

import asyncio

from ..jobfilter import filter_by_title, location_matches, tokens
from . import api
from .render import render_job, render_search_results

# Fixed, never a multiple of `limit`. Indeed pages ten at a time and each page
# is ~1.4MB, so this is eight requests at worst.
_EXAMINE_CEILING = 80

# The second request only diagnoses whether Indeed ignored `location`; it must
# never consume the successful primary response's whole deadline.
_INERTNESS_PROBE_MAX_SECONDS = 30.0


async def search_indeed(
    *,
    title: str = "",
    location: str = "",
    radius_km: int = 0,
    strict_title: bool = True,
    strict_location: bool = False,
    limit: int = 25,
    timeout: float = 240.0,
    browser_solver=None,
) -> dict:
    """Search ca.indeed.com.

    ``strict_location`` defaults to False because Indeed searches a radius, the
    same as Job Bank; the postings' own locations are still reported so a
    neighbouring town is visible rather than implied.
    """
    limit = max(1, min(int(limit or 25), 100))

    deadline = asyncio.get_running_loop().time() + timeout
    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)

            jobs, total, complete = await api.search_all(
                session=session,
                query=title,
                location=location,
                radius=max(0, int(radius_km or 0)),
                max_records=_EXAMINE_CEILING,
            )

            notes: list[str] = []

            # Three boards indexed here now accept a location, ignore it, and
            # report success: Job Bank's `locationstring`, LinkedIn's radius,
            # and Phenom. Indeed is not assumed innocent — if a located search
            # returns the same count as an unlocated one, the filter did not
            # apply and the caller is told rather than shown the whole country.
            location_ignored = False
            if location and total:
                remaining = deadline - asyncio.get_running_loop().time()
                plain_total = 0
                probe_completed = False
                if remaining > 1.0:
                    probe_timeout = min(
                        _INERTNESS_PROBE_MAX_SECONDS,
                        remaining - 1.0,
                    )
                    try:
                        async with asyncio.timeout(probe_timeout):
                            plain_body = await api.search_page(
                                session=session,
                                query=title,
                            )
                            _, plain_total = api.parse_search(plain_body)
                            probe_completed = True
                    except Exception:  # noqa: BLE001 - advisory probe only
                        # The primary located response is already valid. A
                        # challenge, timeout, or markup failure on this extra
                        # diagnostic request must not throw those jobs away.
                        pass
                if not probe_completed:
                    notes.append(
                        "Indeed's location filter could not be independently "
                        "verified because the comparison request did not return "
                        "a usable search page; the primary located response is "
                        "shown without making an inert-filter claim."
                    )
                elif plain_total and plain_total == total:
                    total = 0
                    location_ignored = True
                    notes.append(
                        f"Indeed returned the same {plain_total} results with and "
                        f"without “{location}”, so its location filter did not "
                        "apply and its own count is not reported. The postings "
                        "below were matched here instead."
                    )

            examined = len(jobs)

            location_dropped = 0
            # `strict_location` is the caller's preference, but a board filter
            # KNOWN to be inert removes the choice: nothing else scoped this
            # result set, so the local filter is the only guarantee left. The
            # note above claims the postings "were matched here instead" — that
            # claim was false by default until this ran unconditionally.
            if location and (strict_location or location_ignored):
                wanted = tokens(location)
                kept = [j for j in jobs if location_matches(j.get("location") or "", wanted)]
                location_dropped = len(jobs) - len(kept)
                jobs = kept

            title_dropped = 0
            if strict_title and title:
                jobs, title_dropped = filter_by_title(jobs, lambda j: j.get("title"), title)

            matched = len(jobs)
            jobs = jobs[:limit]

            if not complete:
                # Not a window that can be widened. Pagination is
                # robots-disallowed (and login-walled behind that), so refining
                # changes WHICH postings appear, never how many.
                notes.append(
                    f"Indeed answers one page per query ({examined} postings) and "
                    "every deeper offset returns its sign-in wall, so narrowing the "
                    "query changes which postings appear rather than how many. Use "
                    "get_indeed_job on any result for its full description, salary "
                    "band and expiry date."
                )

            return {
                "content": render_search_results(
                    jobs,
                    title=title,
                    location=location,
                    # Not scoped once the board's filter is known inert.
                    scoped=not location_ignored,
                    board_total=total,
                    title_filtered=title_dropped,
                    location_filtered=location_dropped,
                    truncated_by_limit=matched - len(jobs),
                    examined=examined,
                    notes=notes,
                ),
                "content_type": "markdown",
            }
    except TimeoutError:
        return {"error": f"Indeed search timed out after {timeout:.0f}s."}
    except api.IndeedBlockedError:
        return {
            "error": (
                "Indeed declined the request or served a challenge. It is "
                "Cloudflare-fronted; retry shortly."
            )
        }
    except api.IndeedUnrecognisedPageError:
        return {
            "error": (
                "Indeed answered 200 with a page that is not a search result — a "
                "sign-in shell, a soft 404, or changed markup. Reporting this "
                "rather than 'no jobs matched', which would be a different and "
                "wrong answer."
            )
        }
    except api.IndeedUnavailableError:
        return {"error": "Indeed is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"Indeed search failed ({type(exc).__name__})."}


async def get_indeed_job(
    job_key: str,
    *,
    timeout: float = 120.0,
    browser_solver=None,
) -> dict:
    """One posting in full, including the description and its expiry date.

    This is the mirror: several ATS platforms serve a list fine and gate the
    posting body behind a session or an "unsupported browser" check. Indeed
    carries the same body in plain HTML, with a structured salary band and a
    validThrough date no other indexed board publishes.
    """
    key = " ".join(str(job_key or "").split())
    if not key.isalnum():
        return {"error": "job_key must be the alphanumeric Indeed key from a ?jk= URL."}

    try:
        async with asyncio.timeout(timeout):
            session = await api._get_session(browser_solver)
            job = await api.fetch_job(key, session=session)
            if job is None:
                return {
                    "error": (
                        f"Indeed posting {key} carried no JobPosting data. The key "
                        "may be wrong, or the posting may have been removed."
                    )
                }
            return {"content": render_job(job), "content_type": "markdown"}
    except TimeoutError:
        return {"error": f"Indeed fetch timed out after {timeout:.0f}s."}
    except api.IndeedBlockedError:
        return {"error": "Indeed declined the request or served a challenge. Retry shortly."}
    except api.IndeedUnavailableError:
        return {"error": "Indeed is not responding correctly. Try again shortly."}
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
        return {"error": f"Indeed fetch failed ({type(exc).__name__})."}
