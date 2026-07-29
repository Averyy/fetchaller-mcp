"""Entry point: ``get_wellfound(url, browser_solver)`` for the fetch tool."""

from __future__ import annotations

from urllib.parse import urlparse

import wafer

from . import api
from .render import render_company, render_job, render_search


def _titleize(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _search_title(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    # /role/r/{role}
    if len(parts) >= 3 and parts[0] == "role" and parts[1] == "r":
        return f"{_titleize(parts[2])} jobs (remote)"
    # /role/l/{role}/{location}
    if len(parts) >= 4 and parts[0] == "role" and parts[1] == "l":
        return f"{_titleize(parts[2])} jobs in {_titleize(parts[3])}"
    # /location/{location}
    if len(parts) >= 2 and parts[0] == "location":
        return f"Jobs in {_titleize(parts[1])}"
    return "Startup jobs"


async def get_wellfound(url: str, browser_solver=None, timeout: float | None = None) -> dict:
    """Dispatch a wellfound.com URL to the right renderer."""
    try:
        html = await api.fetch_html(url, browser_solver=browser_solver, timeout=timeout)

        # wellfound serves a 200 "Page not found - 404" shell for unresolvable URLs
        # (a bare /jobs/{id} with no slug, a wrong /company/{slug}, an expired job).
        # Catch it once for every URL type so the error is actionable, not a
        # misleading "possible challenge".
        if api.is_not_found_page(html):
            if api.is_wellfound_job(url):
                return {"error": (
                    "wellfound.com returned a 'not found' page for this job — it may be expired, "
                    "or the URL needs the full '/jobs/{id}-{slug}' form (a bare numeric "
                    "'/jobs/{id}' resolves to a 404)."
                )}
            return {"error": f"wellfound.com returned a 'not found' page for {url} — check the URL/slug."}

        # Job detail: JSON-LD JobPosting (no Apollo).
        if api.is_wellfound_job(url):
            job = api.jsonld_jobposting(html)
            if not job:
                return {"error": "wellfound.com job page had no JobPosting data."}
            return {"content": render_job(job, url)}

        next_data = api.parse_next_data(html)
        if not next_data:
            return {"error": "wellfound.com page had no embedded data (possible challenge)."}
        cache = api.apollo_cache(next_data)
        if not cache:
            return {"error": "wellfound.com page had no Apollo data."}

        if api.is_wellfound_company(url):
            startups = api.entities(cache, "Startup")
            # The profiled company is the one with a productDescription / slug match.
            slug = urlparse(url).path.rsplit("/", 1)[-1]
            startup = next((s for s in startups if s.get("slug") == slug), None) or \
                next((s for s in startups if s.get("productDescription")), None) or \
                (startups[0] if startups else None)
            if not startup:
                return {"error": "wellfound.com company page had no company data."}
            return {"content": render_company(cache, startup, url)}

        if api.is_wellfound_search(url):
            return {"content": render_search(cache, url, title=_search_title(url))}

        return {"error": f"Unrecognized wellfound.com URL: {url}"}
    except wafer.ChallengeDetected as e:
        return {"error": f"wellfound.com blocked the request ({e.challenge_type} challenge). Try again shortly."}
    except wafer.WaferTimeout:
        return {"error": "wellfound.com request timed out."}
    except wafer.WaferError as e:
        return {"error": f"wellfound.com error: {e}"}
    except Exception as e:  # noqa: BLE001 — surface a clean message to the LLM
        return {"error": f"wellfound.com fetch failed: {type(e).__name__}: {e}"}
