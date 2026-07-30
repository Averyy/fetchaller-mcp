"""MCP server setup for fetchaller."""

import asyncio
import hashlib
import importlib.util
import math
import os
import re
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from . import __version__
from .cache.response_cache import ResponseCache
from .config import Config, load_config, set_wafer_cache_dir
from .linkedin.search import get_linkedin_job, search_linkedin_jobs
from .marketplace.search import search_marketplace
from .queue.reddit_queue import QueueConfig, RedditRequestQueue
from .realtor.search import search_realtor
from .security.browser_proxy import BrowserEgressProxy, BrowserProxyError
from .security.xss import redact_secrets_for_log, sanitize_for_log
from .tools.browse_reddit import browse_reddit
from .tools.fetch import (
    ALLOWED_METHODS,
    fetch_url,
    validate_request_headers,
    validate_request_method,
)
from .tools.get_alibaba_product import get_alibaba_product
from .tools.get_aliexpress_product import get_aliexpress_product
from .tools.search import search_web
from .tools.search_alibaba import search_alibaba_tool
from .tools.search_aliexpress import search_aliexpress_tool
from .tools.search_reddit import search_reddit


def _log(msg: str) -> None:
    """Log with timestamp."""
    print(
        f"[{datetime.now(UTC).isoformat()}] {redact_secrets_for_log(msg)}",
        file=sys.stderr,
    )


def _python_source_tree_sha256(package_file: Path) -> str:
    """Hash every Python source path and byte in one imported package tree."""

    digest = hashlib.sha256()
    package_root = package_file.resolve().parent
    sources = sorted(
        path
        for path in package_root.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    if not sources:
        raise RuntimeError("Python source tree identity is empty")
    for source in sources:
        relative = source.relative_to(package_root).as_posix().encode()
        content = source.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


class _AuditedBrowserSolver:
    """Count browser dispatches without recording URLs or query data."""

    def __init__(self, solver, audit: dict[str, int]) -> None:
        self._solver = solver
        self._audit = audit
        self._lock = threading.Lock()

    def _record(self, url: str) -> None:
        hostname = (urlparse(url).hostname or "").rstrip(".").casefold()
        with self._lock:
            self._audit["total"] += 1
            if hostname == "reddit.com" or hostname.endswith(".reddit.com"):
                self._audit["reddit"] += 1

    def solve(self, url: str, *args, **kwargs):
        self._record(url)
        return self._solver.solve(url, *args, **kwargs)

    async def asolve(self, url: str, *args, **kwargs):
        self._record(url)
        return await self._solver.asolve(url, *args, **kwargs)

    # ``intercept_iframe`` drives the same browser to ``embedder_url``. Auditing
    # only solve/asolve let a real navigation reach the browser uncounted, so a
    # "reddit=0" summary asserted more than it had actually observed.
    def intercept_iframe(self, embedder_url: str, *args, **kwargs):
        self._record(embedder_url)
        return self._solver.intercept_iframe(embedder_url, *args, **kwargs)

    async def aintercept_iframe(self, embedder_url: str, *args, **kwargs):
        self._record(embedder_url)
        return await self._solver.aintercept_iframe(embedder_url, *args, **kwargs)

    # ``render`` (wafer 0.4.2) navigates the browser to the page and returns its
    # serialized DOM. That is a full navigation like any other — the only reason
    # it is not a "solve" is that no challenge prompted it — so it has to be
    # counted, or a "reddit=0" summary asserts more than it observed.
    def render(self, url: str, *args, **kwargs):
        self._record(url)
        return self._solver.render(url, *args, **kwargs)

    async def arender(self, url: str, *args, **kwargs):
        self._record(url)
        return await self._solver.arender(url, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._solver, name)


def _log_browser_dispatch_summary(server) -> None:
    audit = getattr(server, "_browser_dispatch_audit", None)
    if not (
        isinstance(audit, dict)
        and isinstance(audit.get("total"), int)
        and not isinstance(audit.get("total"), bool)
        and isinstance(audit.get("reddit"), int)
        and not isinstance(audit.get("reddit"), bool)
    ):
        _log("BROWSER_DISPATCH_SUMMARY unavailable")
        return
    _log(
        "BROWSER_DISPATCH_SUMMARY "
        f"total={audit['total']} reddit={audit['reddit']}"
    )


def _log_browser_egress_summary(proxy) -> None:
    """Emit one machine-readable shutdown audit without request details."""

    if proxy is None:
        _log("BROWSER_EGRESS_SUMMARY unavailable")
        return
    allowed = getattr(proxy, "allowed_connections", None)
    denied = getattr(proxy, "denied_connections", None)
    if (
        isinstance(allowed, bool)
        or not isinstance(allowed, int)
        or isinstance(denied, bool)
        or not isinstance(denied, int)
    ):
        _log("BROWSER_EGRESS_SUMMARY unavailable")
        return
    _log(
        "BROWSER_EGRESS_SUMMARY "
        f"allowed={allowed} "
        f"denied={denied}"
    )


def _log_reddit_session_audit() -> None:
    """Emit value-free Reddit persistence telemetry before session cleanup."""

    from .tools.browse_reddit import reddit_session_audit

    audit = reddit_session_audit()
    if audit is None:
        _log("REDDIT_SESSION_AUDIT unavailable")
        return
    # wafer >=0.4.4 names the branch that ended the last bootstrap. Log it: a
    # bare attempt count cannot distinguish a non-2xx root from an unparseable
    # verification page or a browser fallback that got no time budget.
    _log(
        "REDDIT_SESSION_AUDIT "
        f"hydrated_anonymous={audit['hydrated_anonymous']} "
        f"hydrated_cookie_count={audit['hydrated_cookie_count']} "
        f"bootstrap_network_attempts={audit['bootstrap_network_attempts']} "
        f"successes={audit.get('successes')} "
        f"last_outcome={audit.get('last_outcome')} "
        f"last_status={audit.get('last_status')} "
        f"browser_attempts={audit.get('browser_attempts')} "
        f"last_browser_outcome={audit.get('last_browser_outcome')} "
        f"last_browser_budget={audit.get('last_browser_budget')}"
    )


def _preflight_recaptcha_models() -> None:
    """Require locally bundled grid models for a browser-complete runtime."""

    from wafer.browser import preflight_recaptcha_models

    # Production runs with HF_HUB_OFFLINE=1 and both pinned assets baked into
    # the image.  Wait for their native ONNX sessions and warmups to finish
    # instead of timing out while wafer's shared loader is still alive.  A
    # timed-out daemon loader can otherwise outlive failed startup and race
    # interpreter teardown; readiness must be binary, not abandoned midway.
    preflight_recaptcha_models(timeout=None)


def close_browser_runtime(server) -> None:
    """Synchronously close browser resources, including partial construction."""

    try:
        solver = getattr(server, "_browser_solver", None)
        if solver is not None:
            solver.close()
    except Exception:
        _log("warning: browser solver cleanup failed")
    finally:
        if hasattr(server, "_browser_solver"):
            server._browser_solver = None

    try:
        proxy = getattr(server, "_browser_proxy", None)
        _log_browser_egress_summary(proxy)
        if proxy is not None:
            proxy.close()
    except Exception:
        _log("warning: browser proxy cleanup failed")
    finally:
        if hasattr(server, "_browser_proxy"):
            server._browser_proxy = None


async def close_browser_runtime_bounded(
    server, *, timeout: float = 5.0
) -> None:
    """Release browser resources without holding the HTTP shutdown hostage.

    ``BrowserSolver.close()`` must run on its owning worker and can be queued
    behind an in-flight challenge.  Calling it directly from the ASGI lifespan
    turns a normal container stop into an unbounded wait.  Run that close in a
    daemon helper and bound only the shutdown wait; the helper still closes the
    browser if its active solve returns later, while the server can terminate.
    """
    solver = getattr(server, "_browser_solver", None)
    if hasattr(server, "_browser_solver"):
        server._browser_solver = None

    if solver is not None:
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[BaseException | None] = loop.create_future()

        def finish(error: BaseException | None) -> None:
            if not completed.done():
                completed.set_result(error)

        def close_solver() -> None:
            error: BaseException | None = None
            try:
                solver.close()
            except BaseException as exc:
                error = exc
            try:
                loop.call_soon_threadsafe(finish, error)
            except RuntimeError:
                # The process is already exiting; this daemon cannot keep it
                # alive and will finish its close if the worker unblocks.
                pass

        threading.Thread(
            target=close_solver,
            name="fetchaller-browser-close",
            daemon=True,
        ).start()
        try:
            error = await asyncio.wait_for(asyncio.shield(completed), timeout)
            if error is not None:
                _log("warning: browser solver cleanup failed")
        except TimeoutError:
            _log(
                "warning: browser solver cleanup exceeded "
                f"{timeout:.1f}s; continuing shutdown"
            )

    proxy = getattr(server, "_browser_proxy", None)
    if hasattr(server, "_browser_proxy"):
        server._browser_proxy = None
    _log_browser_egress_summary(proxy)
    if proxy is not None:
        try:
            proxy.close()
        except Exception:
            _log("warning: browser proxy cleanup failed")


async def cleanup_server(server) -> None:
    """Clean up server resources (shared between stdio and HTTP shutdown).

    Each cleanup step is wrapped in try/except so one failure doesn't
    prevent the remaining resources from being released.
    """
    try:
        if hasattr(server, '_reddit_queue'):
            await server._reddit_queue.stop()
    except Exception:
        _log("warning: reddit queue cleanup failed")

    _log_reddit_session_audit()
    _log_browser_dispatch_summary(server)
    await close_browser_runtime_bounded(server)

    # Release module-level shared sessions (set to None, GC handles the rest).
    cleanup_fns = []
    try:
        from .search import close_session as close_search_session
        cleanup_fns.append(close_search_session)
    except ImportError:
        pass
    try:
        from .facebook_marketplace.graphql import close_session as close_fb_session
        cleanup_fns.append(close_fb_session)
    except ImportError:
        pass
    try:
        from .kijiji.api import close_session as close_kijiji_session
        cleanup_fns.append(close_kijiji_session)
    except ImportError:
        pass
    try:
        from .aliexpress.product import close_client as close_mtop_client
        cleanup_fns.append(close_mtop_client)
    except ImportError:
        pass
    try:
        from .aliexpress.reviews import close_session as close_reviews_session
        cleanup_fns.append(close_reviews_session)
    except ImportError:
        pass
    try:
        from .aliexpress.search import close_session as close_ae_search_session
        cleanup_fns.append(close_ae_search_session)
    except ImportError:
        pass
    try:
        from .tools.browse_reddit import close_session as close_reddit_session
        cleanup_fns.append(close_reddit_session)
    except ImportError:
        pass
    try:
        from .craigslist.sapi import close_session as close_cl_session
        cleanup_fns.append(close_cl_session)
    except ImportError:
        pass
    try:
        from .costco.api import close_session as close_costco_session
        cleanup_fns.append(close_costco_session)
    except ImportError:
        pass
    try:
        from .realtor.api import close_session as close_realtor_session
        cleanup_fns.append(close_realtor_session)
    except ImportError:
        pass
    try:
        from .wellfound.api import close_session as close_wellfound_session
        cleanup_fns.append(close_wellfound_session)
    except ImportError:
        pass
    try:
        from .linkedin.api import close_session as close_linkedin_session
        cleanup_fns.append(close_linkedin_session)
    except ImportError:
        pass

    for fn in cleanup_fns:
        try:
            await fn()
        except Exception:
            _log(f"warning: {fn.__module__} cleanup failed")


# Where a local X server publishes its socket. Module-level so tests can point
# it at a temp dir instead of the real /tmp.
_X11_SOCKET_DIR = Path("/tmp/.X11-unix")


def _system_chrome_path() -> str:
    """Where wafer's BrowserSolver expects to find *system* Chrome."""
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform == "win32":
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        candidates = [
            os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")
            for root in roots
            if root
        ]
        for candidate in candidates:
            if os.access(candidate, os.R_OK | os.X_OK):
                return candidate
        return candidates[0] if candidates else (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        )
    for candidate in (
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.access(candidate, os.R_OK | os.X_OK):
            return candidate
    return "/opt/google/chrome/chrome"


def _browser_solver_ready(
    executable_path: str | None = None,
) -> tuple[bool, str]:
    """Check that the browser wafer actually launches exists and is usable here.

    Checking Patchright's support-asset registry proves nothing about the
    executable BrowserSolver launches, so verify the caller-pinned path or
    wafer's branded-Chrome discovery path here. BrowserSolver's launch preflight
    validates branded Chrome and aligns browser-bound HTTP identity to it. A
    version mismatch is warned rather than treated as a readiness failure;
    container builds separately pin Chrome to wafer/wreq's emulation.

    Returns (ready, detail).
    """
    chrome = executable_path or _system_chrome_path()
    if not os.access(chrome, os.R_OK | os.X_OK):
        return False, f"pinned Chrome not executable at {chrome}"

    # The solver runs headful by default (wafer: "Must run headful — headless =
    # 16.7% bypass rate"). On Linux that needs an X display; without one Chrome
    # exits immediately and the challenge fails with a confusing timeout.
    if sys.platform not in ("darwin", "win32"):
        display = os.environ.get("DISPLAY")
        if not display:
            return False, f"{chrome} found but DISPLAY is unset (headful Chrome needs an X server; start Xvfb)"
        # Checking the env var alone is not enough: the Docker image sets
        # DISPLAY unconditionally, so if Xvfb failed to start this check would
        # go green over a solver that cannot launch — the same false-positive
        # the bundled-Chromium check used to give. Verify the socket is really
        # being served. A remote/TCP DISPLAY (host:0) has no local socket, so
        # it is accepted unverified rather than wrongly failed.
        if display.startswith(":"):
            screen = display[1:].split(".")[0]
            sock = _X11_SOCKET_DIR / f"X{screen}"
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1.0)
                    probe.connect(str(sock))
            except OSError:
                return False, (
                    f"{chrome} found and DISPLAY={display}, but no X server "
                    f"is accepting connections on {sock} (Xvfb not running)"
                )

    return True, chrome


def _summarize_args(tool_name: str, args: dict) -> str:
    """Summarize tool arguments for logging."""
    if tool_name == "fetch":
        url = redact_secrets_for_log(args.get("url", "?"))
        timeout = args.get("timeout", 10)
        return f"url={url} timeout={timeout}s"
    elif tool_name == "browse_reddit":
        return f"subreddit_len={len(str(args.get('subreddit', '')))} sort={args.get('sort', 'hot')}"
    elif tool_name == "search_reddit":
        return f"query_len={len(str(args.get('query', '')))} subreddit={'set' if args.get('subreddit') else 'all'}"
    elif tool_name == "search":
        return f"query_len={len(str(args.get('query', '')))} page={args.get('page', 1)}"
    elif tool_name == "get_aliexpress_product":
        return (
            f"product_id_len={len(str(args.get('product_id', '')))} "
            f"timeout={args.get('timeout', 180)}s"
        )
    elif tool_name == "search_aliexpress":
        return (
            f"query_len={len(str(args.get('query', '')))} "
            f"page={args.get('page', 1)} "
            f"sort={args.get('sort', 'default')} "
            f"timeout={args.get('timeout', 180)}s"
        )
    elif tool_name == "get_alibaba_product":
        return (
            f"product_id_len={len(str(args.get('product_id', '')))} "
            f"timeout={args.get('timeout', 180)}s"
        )
    elif tool_name == "search_alibaba":
        return (
            f"query_len={len(str(args.get('query', '')))} "
            f"page={args.get('page', 1)} "
            f"sort={args.get('sort', 'default')} "
            f"timeout={args.get('timeout', 180)}s"
        )
    elif tool_name == "search_marketplace":
        return (
            f"query_len={len(str(args.get('query', '')))} "
            f"location_len={len(str(args.get('location', '')))} "
            f"platform_count={len(args.get('platforms') or []) or 'all'}"
        )
    elif tool_name == "search_realtor":
        return (
            f"location_len={len(str(args.get('location', '')))} "
            f"{sanitize_for_log(str(args.get('transaction', 'sale')), 16)} "
            f"price={args.get('min_price', '')}-{args.get('max_price', '')}"
        )
    return f"argument_keys={','.join(sorted(str(key)[:32] for key in args)[:20])}"


_TOOL_ARGUMENTS: dict[str, set[str]] = {
    "fetch": {"url", "maxTokens", "timeout", "raw", "method", "headers", "body"},
    "browse_reddit": {"subreddit", "sort", "time", "limit", "after", "timeout"},
    "search_reddit": {
        "query",
        "subreddit",
        "sort",
        "time",
        "limit",
        "after",
        "timeout",
    },
    "search": {"query", "page"},
    "get_aliexpress_product": {"product_id", "timeout"},
    "search_aliexpress": {
        "query",
        "page",
        "sort",
        "min_price",
        "max_price",
        "timeout",
    },
    "get_alibaba_product": {"product_id", "timeout"},
    "search_alibaba": {
        "query",
        "page",
        "sort",
        "min_price",
        "max_price",
        "timeout",
    },
    "search_marketplace": {
        "query",
        "location",
        "platforms",
        "category",
        "sort",
        "condition",
        "min_price",
        "max_price",
    },
    "search_linkedin_jobs": {
        "keywords",
        "location",
        "geo_id",
        "date_posted",
        "workplace",
        "experience",
        "job_type",
        "min_salary",
        "easy_apply",
        "under_10_applicants",
        "sort",
        "start",
        "limit",
    },
    "get_linkedin_job": {"job_id"},
    "search_realtor": {
        "location",
        "transaction",
        "property_type",
        "building_type",
        "min_price",
        "max_price",
        "min_beds",
        "min_baths",
        "ownership",
        "sort",
        "page",
    },
}
_TOOL_REQUIRED = {
    "fetch": {"url"},
    "browse_reddit": {"subreddit"},
    "search_reddit": {"query"},
    "search": {"query"},
    "get_aliexpress_product": {"product_id"},
    "search_aliexpress": {"query"},
    "get_alibaba_product": {"product_id"},
    "search_alibaba": {"query"},
    "search_marketplace": {"query", "location"},
    "search_realtor": {"location"},
    "search_linkedin_jobs": {"keywords"},
    "get_linkedin_job": {"job_id"},
}
_STRING_LIMITS = {
    "url": 8192,
    "query": 512,
    "keywords": 512,
    "job_id": 32,
    "geo_id": 24,
    "location": 256,
    "subreddit": 100,
    "after": 64,
    "product_id": 2048,
    "sort": 32,
    "time": 16,
    "category": 32,
    "condition": 32,
    "transaction": 16,
    "property_type": 32,
    "building_type": 32,
    "ownership": 16,
}
_ENUMS = {
    "time": {"hour", "day", "week", "month", "year", "all"},
    "category": {
        "all",
        "cars",
        "electronics",
        "furniture",
        "clothing",
        "tools",
        "free",
        "bikes",
        "phones",
        "motorcycles",
        "boats",
        "rvs",
        "auto_parts",
        "sporting",
        "toys",
        "baby",
    },
    "condition": {"new", "like_new", "good", "fair"},
    "transaction": {"sale", "rent"},
    "property_type": {
        "any",
        "residential",
        "condo",
        "recreational",
        "vacant-land",
        "multi-family",
        "agriculture",
        "parking",
    },
    "building_type": {
        "house",
        "duplex",
        "triplex",
        "townhouse",
        "apartment",
        "other",
    },
    "ownership": {"freehold", "condo"},
}
_TOOL_ENUMS = {
    ("browse_reddit", "sort"): {"hot", "new", "top", "rising"},
    ("search_linkedin_jobs", "sort"): {"relevance", "recent"},
    ("search_linkedin_jobs", "date_posted"): {"any", "24h", "week", "month"},
    ("search_linkedin_jobs", "workplace"): {"on_site", "remote", "hybrid"},
    ("search_linkedin_jobs", "experience"): {
        "internship",
        "entry",
        "associate",
        "mid_senior",
        "director",
        "executive",
    },
    ("search_linkedin_jobs", "job_type"): {
        "full_time",
        "part_time",
        "contract",
        "temporary",
        "internship",
    },
    ("search_linkedin_jobs", "min_salary"): {40000, 60000, 80000, 100000, 120000},
    ("search_reddit", "sort"): {
        "relevance",
        "hot",
        "top",
        "new",
        "comments",
    },
    ("search_aliexpress", "sort"): {
        "default",
        "orders",
        "price_asc",
        "price_desc",
    },
    ("search_alibaba", "sort"): {
        "default",
        "price_asc",
        "price_desc",
    },
    ("search_marketplace", "sort"): {
        "date",
        "price_asc",
        "price_desc",
        "relevance",
    },
    ("search_realtor", "sort"): {
        "newest",
        "oldest",
        "price-asc",
        "price-desc",
    },
}
_INTEGER_RANGES = {
    "maxTokens": (1, 250_000),
    "timeout": (1, 300),
    "limit": (1, 25),
    "page": (1, 100),
    "min_beds": (0, 100),
    "min_baths": (0, 100),
    # LinkedIn's guest search answers rows 0..999; 1000+ returns HTTP 400.
    "start": (0, 999),
}


def _validate_tool_arguments(tool_name: str, arguments: object) -> str | None:
    """Fail closed at the MCP boundary instead of relying on client-side schema use."""

    allowed = _TOOL_ARGUMENTS.get(tool_name)
    if allowed is None:
        return None
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    extras = set(arguments) - allowed
    if extras:
        return f"unsupported argument: {sorted(str(value) for value in extras)[0][:64]}"
    missing = _TOOL_REQUIRED[tool_name] - set(arguments)
    if missing:
        return f"missing required argument: {sorted(missing)[0]}"

    for name, value in arguments.items():
        if name in _STRING_LIMITS:
            if not isinstance(value, str):
                return f"{name} must be a string"
            if (
                not value.strip()
                or len(value) > _STRING_LIMITS[name]
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                return f"{name} must be a bounded non-empty string without control characters"
        allowed_enum = _TOOL_ENUMS.get((tool_name, name), _ENUMS.get(name))
        if allowed_enum is not None and value not in allowed_enum:
            return f"unsupported {name}"
        if name in _INTEGER_RANGES:
            minimum, maximum = _INTEGER_RANGES[name]
            if tool_name == "get_aliexpress_product" and name == "timeout":
                maximum = 180
            if type(value) is not int or not minimum <= value <= maximum:
                return f"{name} must be an integer from {minimum} to {maximum}"
        if name in {"min_price", "max_price"}:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                return f"{name} must be a finite non-negative number"
            if (
                tool_name in {"search_marketplace", "search_realtor"}
                and type(value) is not int
            ):
                return f"{name} must be an integer"
        if name == "raw" and type(value) is not bool:
            return "raw must be a boolean"
        if name in {"easy_apply", "under_10_applicants"} and type(value) is not bool:
            return f"{name} must be a boolean"
        # method/headers/body are validated in full by tools.fetch (the single
        # place that knows the transport's rules). Here we only reject shapes
        # that should never have crossed the boundary, so a malformed call fails
        # at the edge like every other argument rather than deeper in.
        if name == "method":
            method_error = validate_request_method(value)[1]
            if method_error:
                return method_error
        if name == "headers":
            header_error = validate_request_headers(value)[1]
            if header_error:
                return header_error
        if name == "body" and not isinstance(value, str):
            return "body must be a string"
        if name == "job_id" and not (value.isdigit() and 6 <= len(value) <= 20):
            return "job_id must be a numeric LinkedIn job ID (6-20 digits)"
        if name == "geo_id" and not (value.isdigit() and len(value) <= 20):
            return "geo_id must be a numeric LinkedIn geo ID"
        if name == "platforms":
            if (
                not isinstance(value, list)
                or not value
                or len(value) > 3
                or any(
                    not isinstance(platform, str)
                    or platform not in {"kijiji", "craigslist", "facebook"}
                    for platform in value
                )
            ):
                return "platforms must be a non-empty unique platform list"
            if len(value) != len(set(value)):
                return "platforms must be a non-empty unique platform list"

    if arguments.get("min_price") is not None and arguments.get("max_price") is not None:
        if arguments["min_price"] > arguments["max_price"]:
            return "min_price must not exceed max_price"
    after = arguments.get("after")
    if after is not None and not re.fullmatch(r"t[1-6]_[A-Za-z0-9]{2,16}", after):
        return "after must be a Reddit pagination cursor"
    subreddit = arguments.get("subreddit")
    if subreddit is not None and not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_]{0,20}",
        subreddit,
    ):
        return "subreddit must be a valid Reddit community name"
    if tool_name == "search_realtor" and arguments.get("page", 1) > 30:
        return "page must be an integer from 1 to 30"
    return None


def create_server(
    config: Config | None = None,
    cache: ResponseCache | None = None,
    reddit_queue: RedditRequestQueue | None = None,
    browser_solver=None,
) -> Server:
    """
    Create and configure the MCP server.

    Args:
        config: Optional configuration (loads from env if not provided)
        cache: Optional ResponseCache instance
        reddit_queue: Optional RedditRequestQueue instance
        browser_solver: Optional BrowserSolver for browser-based challenges

    Returns:
        Configured MCP Server instance
    """
    if config is None:
        config = load_config()

    # Set global wafer cache dir so all modules creating sessions use it.
    set_wafer_cache_dir(config.wafer_cache_dir)

    if cache is None:
        cache = ResponseCache.from_config(config)

    if reddit_queue is None:
        reddit_queue = RedditRequestQueue(QueueConfig.from_config(config))
        # Note: Queue auto-starts on first enqueue() call when event loop is running
        # Don't call start() here as there may not be a running event loop yet

    # Every real BrowserSolver is forced through a loopback-only SOCKS5 proxy
    # that validates and numerically pins every browser destination. Chromium
    # does not honor wafer/wreq's resolve= map, so enabling a solver without
    # this egress boundary would reopen DNS-rebinding and subresource SSRF.
    browser_proxy: BrowserEgressProxy | None = None
    if browser_solver is False:
        # Tests/embedders use False as the explicit "do not create" sentinel.
        browser_solver = None
    else:
        try:
            from wafer.browser import BrowserSolver

            browser_proxy = BrowserEgressProxy()
            browser_proxy.start()
            if browser_solver is None:
                solver_kwargs = {"egress_guard_proxy": browser_proxy.url}
                if config.browser_executable_path is not None:
                    solver_kwargs["executable_path"] = (
                        config.browser_executable_path
                    )
                browser_solver = BrowserSolver(**solver_kwargs)
            else:
                configure_guard = getattr(
                    browser_solver, "configure_egress_guard", None
                )
                if not callable(configure_guard):
                    raise BrowserProxyError(
                        "injected BrowserSolver cannot be configured with "
                        "the required egress proxy"
                    )
                configure_guard(browser_proxy.url)

            ready, detail = _browser_solver_ready(
                config.browser_executable_path
            )
            if ready:
                if config.browser_preflight:
                    try:
                        # A browser-complete deployment must have both pinned
                        # Enterprise grid models locally ready. Runtime runs
                        # HF offline, so this verifies the immutable image
                        # cache rather than allowing a request-time download.
                        _preflight_recaptcha_models()
                        # wafer owns launch details; exercise its actual launch
                        # path now so readiness cannot pass on a stale X socket
                        # or missing runtime library.
                        browser_solver.preflight()
                        _log(
                            "BrowserSolver and reCAPTCHA models launched "
                            f"successfully via {detail}"
                        )
                    except Exception as exc:
                        browser_solver.close()
                        browser_solver = None
                        browser_proxy.close()
                        browser_proxy = None
                        _log(
                            "WARNING: BrowserSolver launch preflight failed "
                            f"({type(exc).__name__})"
                        )
                else:
                    _log(f"BrowserSolver available via {detail} (launch preflight disabled)")
            else:
                _log(
                    f"WARNING: BrowserSolver imported but NOT usable - {detail}. "
                    "Bot challenge solving WILL fail. Configure "
                    "BROWSER_EXECUTABLE_PATH with executable branded Google "
                    "Chrome and ensure its headful display is available."
                )
                browser_solver.close()
                browser_solver = None
                browser_proxy.close()
                browser_proxy = None
        except ImportError:
            _log("BrowserSolver not available (install wafer-py[browser] for bot challenge solving)")
            if browser_proxy is not None:
                browser_proxy.close()
                browser_proxy = None
            browser_solver = None
        except (BrowserProxyError, RuntimeError, TypeError, ValueError) as exc:
            _log(
                "WARNING: guarded BrowserSolver setup failed "
                f"({type(exc).__name__}); browser solving disabled"
            )
            if browser_solver is not None:
                try:
                    browser_solver.close()
                except Exception:
                    pass
            if browser_proxy is not None:
                browser_proxy.close()
            browser_solver = None
            browser_proxy = None

    browser_dispatch_audit = {"total": 0, "reddit": 0}
    managed_browser_solver = browser_solver
    if browser_solver is not None:
        browser_solver = _AuditedBrowserSolver(
            browser_solver,
            browser_dispatch_audit,
        )

    server = Server("fetchaller", version=__version__)
    # Store for external cleanup access (e.g., HTTP app lifespan)
    server._reddit_queue = reddit_queue  # type: ignore[attr-defined]
    server._browser_solver = managed_browser_solver  # type: ignore[attr-defined]
    server._browser_proxy = browser_proxy  # type: ignore[attr-defined]
    server._browser_dispatch_audit = browser_dispatch_audit  # type: ignore[attr-defined]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List available tools."""
        return [
            Tool(
                name="fetch",
                description=(
                    "Fetch any URL and return the page content as clean markdown. "
                    "Handles HTML, JSON, XML, CSV, and PDF files. "
                    "Use this tool for reading/fetching web pages - it has no domain restrictions. "
                    "For discovering URLs via search, use the search tool. For reading URL content, use this tool."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "url": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 8192,
                            "description": "The URL to fetch",
                        },
                        "maxTokens": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 250000,
                            "description": "Maximum tokens to return (default: 25000)",
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "description": "Request timeout in seconds (default: 10)",
                        },
                        "raw": {
                            "type": "boolean",
                            "description": "Return raw HTML instead of markdown (default: false)",
                        },
                        "method": {
                            "type": "string",
                            "enum": sorted(ALLOWED_METHODS),
                            "description": (
                                "HTTP method (default: GET). Use POST for search/listing APIs "
                                "that only answer to POST (Getro, Algolia, GraphQL). POST "
                                "requests skip site-specific handling and the response cache."
                            ),
                        },
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string", "maxLength": 8192},
                            "maxProperties": 32,
                            "description": (
                                "Extra request headers, e.g. "
                                '{"Accept": "application/json"}. Connection and '
                                "body-framing headers cannot be set."
                            ),
                        },
                        "body": {
                            "type": "string",
                            "maxLength": 1048576,
                            "description": (
                                "Request body (POST only). Content-Type defaults to "
                                "application/json when the body is valid JSON."
                            ),
                        },
                    },
                    "required": ["url"],
                },
            ),
            Tool(
                name="browse_reddit",
                description=(
                    "Browse a subreddit's posts. Returns metadata and URLs. "
                    "Use mcp__fetchaller__fetch to read full post content."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "subreddit": {
                            "type": "string",
                            "description": "Subreddit name without r/ prefix",
                            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["hot", "new", "top", "rising"],
                            "default": "hot",
                            "description": "Sort order",
                        },
                        "time": {
                            "type": "string",
                            "enum": ["hour", "day", "week", "month", "year", "all"],
                            "default": "day",
                            "description": "Time filter (only applies to 'top' sort)",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 25,
                            "default": 10,
                            "description": "Number of posts (1-25)",
                        },
                        "after": {
                            "type": "string",
                            "pattern": "^t[1-6]_[A-Za-z0-9]{2,16}$",
                            "description": "Pagination cursor from previous response",
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "description": "Request timeout in seconds (default: 10)",
                        },
                    },
                    "required": ["subreddit"],
                },
            ),
            Tool(
                name="search_reddit",
                description=(
                    "Search Reddit posts. Returns metadata and URLs. "
                    "Use mcp__fetchaller__fetch to read full post content."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "Search query",
                        },
                        "subreddit": {
                            "type": "string",
                            "description": "Limit to subreddit (without r/)",
                            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["relevance", "hot", "top", "new", "comments"],
                            "default": "relevance",
                            "description": "Sort order",
                        },
                        "time": {
                            "type": "string",
                            "enum": ["hour", "day", "week", "month", "year", "all"],
                            "default": "all",
                            "description": "Time filter",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 25,
                            "default": 10,
                            "description": "Number of results (1-25)",
                        },
                        "after": {
                            "type": "string",
                            "pattern": "^t[1-6]_[A-Za-z0-9]{2,16}$",
                            "description": "Pagination cursor from previous response",
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "description": "Request timeout in seconds (default: 10)",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="search",
                description=(
                    "Search the web and return results with titles, URLs, and snippets. "
                    "Use this to discover URLs, then use fetch to read full page content."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "Search query",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 1,
                            "description": "Result page (1-indexed, default: 1)",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_aliexpress_product",
                description=(
                    "Get AliExpress product details including price, specifications, "
                    "ratings, and recent reviews. Accepts a numeric product ID or full URL."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "product_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2048,
                            "description": "Numeric product ID or full AliExpress URL",
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 180,
                            "default": 180,
                            "description": "End-to-end timeout in seconds (default: 180)",
                        },
                    },
                    "required": ["product_id"],
                },
            ),
            Tool(
                name="search_aliexpress",
                description=(
                    "Search AliExpress products. Returns product listings with prices, "
                    "ratings, and links. Best-effort — may fail if anti-bot protection triggers."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "Search query",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 1,
                            "description": "Page number (1-indexed, default: 1)",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["default", "orders", "price_asc", "price_desc"],
                            "default": "default",
                            "description": "Sort order",
                        },
                        "min_price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Minimum price filter",
                        },
                        "max_price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Maximum price filter",
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "default": 180,
                            "description": (
                                "End-to-end timeout in seconds (default: 180)"
                            ),
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="get_alibaba_product",
                description=(
                    "Get Alibaba.com B2B product details including tiered pricing, "
                    "MOQ, lead times, supplier info, and specifications. "
                    "Accepts a numeric product ID or full URL."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "product_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2048,
                            "description": "Numeric product ID or full Alibaba.com URL",
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "default": 180,
                            "description": (
                                "End-to-end timeout in seconds (default: 180)"
                            ),
                        },
                    },
                    "required": ["product_id"],
                },
            ),
            Tool(
                name="search_alibaba",
                description=(
                    "Search Alibaba.com B2B products. Returns supplier listings with "
                    "tiered pricing, MOQ, and supplier info."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "Search query",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 1,
                            "description": "Page number (1-indexed, default: 1)",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["default", "price_asc", "price_desc"],
                            "default": "default",
                            "description": "Sort order",
                        },
                        "min_price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Minimum price filter (USD)",
                        },
                        "max_price": {
                            "type": "number",
                            "minimum": 0,
                            "description": "Maximum price filter (USD)",
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "default": 180,
                            "description": (
                                "End-to-end timeout in seconds (default: 180)"
                            ),
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="search_marketplace",
                description=(
                    "Search Kijiji, Craigslist, and Facebook Marketplace simultaneously. "
                    "Takes human-readable parameters (city name, category, price range) and "
                    "returns grouped results from all platforms. Use fetch(url) to get full "
                    "listing details for any result URL."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "description": "Search keywords (e.g. \"golf r\", \"ikea couch\")",
                        },
                        "location": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": (
                                "City name, optionally with province/state "
                                "(e.g. \"toronto\", \"st catharines, ON\", \"seattle\")"
                            ),
                        },
                        "platforms": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "enum": ["kijiji", "craigslist", "facebook"],
                            },
                            "description": (
                                "Platforms to search (default: all). "
                                "Kijiji is Canada-only and auto-skipped for US locations."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "all", "cars", "electronics", "furniture", "clothing",
                                "tools", "free", "bikes", "phones", "motorcycles",
                                "boats", "rvs", "auto_parts", "sporting", "toys", "baby",
                            ],
                            "default": "all",
                            "description": "Category filter",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["date", "price_asc", "price_desc", "relevance"],
                            "default": "date",
                            "description": "Sort order",
                        },
                        "condition": {
                            "type": "string",
                            "enum": ["new", "like_new", "good", "fair"],
                            "description": "Item condition filter",
                        },
                        "min_price": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Minimum price in dollars",
                        },
                        "max_price": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Maximum price in dollars",
                        },
                    },
                    "required": ["query", "location"],
                },
            ),
            Tool(
                name="search_linkedin_jobs",
                description=(
                    "Search LinkedIn's public job board (no account needed). Filter by "
                    "keywords, location, date posted, remote/hybrid/on-site, experience "
                    "level, job type, and minimum salary. Returns title, company, "
                    "location, posting date, and a linkedin.com/jobs/view URL. Call "
                    "get_linkedin_job(job_id) for the full description."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "keywords": {
                            "type": "string",
                            "maxLength": 512,
                            "description": "Job title or skills, e.g. \"software engineer\"",
                        },
                        "location": {
                            "type": "string",
                            "maxLength": 256,
                            "description": (
                                "City/region, e.g. \"Toronto, Ontario, Canada\". "
                                "Resolved to a LinkedIn geo ID automatically."
                            ),
                        },
                        "geo_id": {
                            "type": "string",
                            "pattern": "^[0-9]{1,20}$",
                            "description": (
                                "LinkedIn geo ID, if you already have one. Takes "
                                "precedence over location."
                            ),
                        },
                        "date_posted": {
                            "type": "string",
                            "enum": ["any", "24h", "week", "month"],
                            "default": "any",
                            "description": "How recently the job was posted",
                        },
                        "workplace": {
                            "type": "string",
                            "enum": ["on_site", "remote", "hybrid"],
                            "description": "Workplace type",
                        },
                        "experience": {
                            "type": "string",
                            "enum": [
                                "internship", "entry", "associate",
                                "mid_senior", "director", "executive",
                            ],
                            "description": "Experience level",
                        },
                        "job_type": {
                            "type": "string",
                            "enum": ["full_time", "part_time", "contract", "temporary", "internship"],
                            "description": "Employment type",
                        },
                        "min_salary": {
                            "type": "integer",
                            "enum": [40000, 60000, 80000, 100000, 120000],
                            "description": "Minimum annual salary filter",
                        },
                        "easy_apply": {
                            "type": "boolean",
                            "description": (
                                "Only postings you can apply to inside LinkedIn "
                                "(no external application form)"
                            ),
                        },
                        "under_10_applicants": {
                            "type": "boolean",
                            "description": (
                                "LinkedIn's \"Under 10 applicants\" filter — strongly "
                                "favours postings with few applicants so far"
                            ),
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["relevance", "recent"],
                            "default": "relevance",
                            "description": (
                                "LinkedIn's own sort is not honoured on this endpoint; "
                                "\"recent\" sorts the fetched results by posting date here."
                            ),
                        },
                        "start": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 999,
                            "description": "Result offset for pagination (max 999)",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 25,
                            "description": "Jobs to return (default: 10)",
                        },
                    },
                    "required": ["keywords"],
                },
            ),
            Tool(
                name="get_linkedin_job",
                description=(
                    "Get the full public detail for one LinkedIn job posting: description, "
                    "seniority, employment type, job function, industries, and applicant "
                    "count. Applying still requires a LinkedIn account."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "pattern": "^[0-9]{6,20}$",
                            "description": (
                                "Numeric LinkedIn job ID (6-20 digits), the trailing "
                                "number in a linkedin.com/jobs/view/<id> URL"
                            ),
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="search_realtor",
                description=(
                    "Search Canadian homes for sale or rent on realtor.ca with full filters "
                    "(location, price, beds, baths, property/building type, ownership). Returns "
                    "listings with price, address, beds/baths, size, agent and a realtor.ca URL. "
                    "Call fetch(url) on a listing URL for the full description, every property "
                    "detail, and similar nearby homes."
                ),
                inputSchema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "location": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": (
                                "City, neighbourhood, or postal code "
                                "(e.g. \"Ottawa\", \"Orleans, Ottawa\", \"Toronto\", \"M5V\")"
                            ),
                        },
                        "transaction": {
                            "type": "string",
                            "enum": ["sale", "rent"],
                            "default": "sale",
                            "description": "Buy (sale) or rent",
                        },
                        "property_type": {
                            "type": "string",
                            "enum": [
                                "any", "residential", "condo", "recreational",
                                "vacant-land", "multi-family", "agriculture", "parking",
                            ],
                            "default": "any",
                            "description": "Property category",
                        },
                        "building_type": {
                            "type": "string",
                            "enum": ["house", "duplex", "triplex", "townhouse", "apartment", "other"],
                            "description": "Building type filter",
                        },
                        "min_price": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Minimum price (sale) or monthly rent",
                        },
                        "max_price": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Maximum price (sale) or monthly rent",
                        },
                        "min_beds": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "Minimum bedrooms",
                        },
                        "min_baths": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "Minimum bathrooms",
                        },
                        "ownership": {
                            "type": "string",
                            "enum": ["freehold", "condo"],
                            "description": "Ownership type",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["newest", "oldest", "price-asc", "price-desc"],
                            "default": "newest",
                            "description": "Sort order",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 30,
                            "default": 1,
                            "description": "Result page (~20 per page, up to 600 total)",
                        },
                    },
                    "required": ["location"],
                },
            ),
        ]

    def _format_result(name: str, result: dict, start_time: float) -> CallToolResult:
        """Format a tool result into CallToolResult, with logging.

        Tool-level failures use MCP's ``isError`` bit so protocol clients and
        release gates cannot mistake blocked/timeout/error text for success.
        """
        elapsed = (time.time() - start_time) * 1000
        if not isinstance(result, dict):
            _log(f"TOOL END: {name} INVALID_RESULT time={elapsed:.1f}ms")
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: tool returned an invalid result.",
                    )
                ],
                isError=True,
            )
        if "error" in result:
            _log(f"TOOL END: {name} ERROR_PRESENT time={elapsed:.1f}ms")
            text = f"Error: {result['error']}"
            if "body" in result:
                text += f"\n\nPartial content:\n{result['body']}"
            return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)

        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            _log(f"TOOL END: {name} EMPTY_CONTENT time={elapsed:.1f}ms")
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: tool returned no content.",
                    )
                ],
                isError=True,
            )
        _log(f"TOOL END: {name} OK content_len={len(content)} time={elapsed:.1f}ms")
        return CallToolResult(content=[TextContent(type="text", text=content)], isError=False)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        """Handle tool calls."""

        start_time = time.time()
        validation_error = _validate_tool_arguments(name, arguments)
        if validation_error:
            _log(
                f"TOOL END: {sanitize_for_log(name, 64)} "
                f"INVALID_ARGUMENTS time=0.0ms"
            )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Error: invalid arguments: {validation_error}",
                    )
                ],
                isError=True,
            )
        tool_args_summary = _summarize_args(name, arguments)
        _log(f"TOOL START: {name} {tool_args_summary}")

        try:
            if name == "fetch":
                result = await fetch_url(
                    url=arguments["url"],
                    max_tokens=max(1, min(250000, arguments.get("maxTokens", config.default_max_tokens))),
                    timeout=max(1, min(300, arguments.get("timeout", config.default_timeout_seconds))),
                    raw=arguments.get("raw", False),
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                    reddit_queue=reddit_queue,
                    method=arguments.get("method", "GET"),
                    headers=arguments.get("headers"),
                    body=arguments.get("body"),
                )
                return _format_result(name, result, start_time)

            elif name == "browse_reddit":
                result = await browse_reddit(
                    subreddit=arguments["subreddit"],
                    sort=arguments.get("sort", "hot"),
                    time=arguments.get("time", "day"),
                    limit=arguments.get("limit", 10),
                    after=arguments.get("after"),
                    timeout=max(1, min(300, arguments.get("timeout", 10))),
                    queue=reddit_queue,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_reddit":
                result = await search_reddit(
                    query=arguments["query"],
                    subreddit=arguments.get("subreddit"),
                    sort=arguments.get("sort", "relevance"),
                    time=arguments.get("time", "all"),
                    limit=arguments.get("limit", 10),
                    after=arguments.get("after"),
                    timeout=max(1, min(300, arguments.get("timeout", 10))),
                    queue=reddit_queue,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search":
                result = await search_web(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                )
                return _format_result(name, result, start_time)

            elif name == "get_aliexpress_product":
                result = await get_aliexpress_product(
                    product_id=arguments["product_id"],
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                    timeout=arguments.get("timeout", 180),
                )
                return _format_result(name, result, start_time)

            elif name == "search_aliexpress":
                result = await search_aliexpress_tool(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                    sort=arguments.get("sort", "default"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    timeout=arguments.get("timeout", 180),
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "get_alibaba_product":
                result = await get_alibaba_product(
                    product_id=arguments["product_id"],
                    timeout=arguments.get("timeout", 180),
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_alibaba":
                result = await search_alibaba_tool(
                    query=arguments["query"],
                    page=max(1, arguments.get("page", 1)),
                    sort=arguments.get("sort", "default"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    timeout=arguments.get("timeout", 180),
                    cache=cache,
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_marketplace":
                result = await search_marketplace(
                    query=arguments["query"],
                    location=arguments["location"],
                    platforms=arguments.get("platforms"),
                    category=arguments.get("category"),
                    sort=arguments.get("sort", "date"),
                    condition=arguments.get("condition"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    config=config,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_linkedin_jobs":
                result = await search_linkedin_jobs(
                    keywords=arguments.get("keywords", ""),
                    location=arguments.get("location", ""),
                    geo_id=arguments.get("geo_id"),
                    date_posted=arguments.get("date_posted", "any"),
                    workplace=arguments.get("workplace"),
                    experience=arguments.get("experience"),
                    job_type=arguments.get("job_type"),
                    min_salary=arguments.get("min_salary"),
                    easy_apply=arguments.get("easy_apply", False),
                    under_10_applicants=arguments.get("under_10_applicants", False),
                    sort=arguments.get("sort", "relevance"),
                    start=arguments.get("start", 0),
                    limit=arguments.get("limit", 10),
                    max_tokens=config.default_max_tokens,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "get_linkedin_job":
                result = await get_linkedin_job(
                    arguments["job_id"],
                    max_tokens=config.default_max_tokens,
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            elif name == "search_realtor":
                result = await search_realtor(
                    location=arguments["location"],
                    transaction=arguments.get("transaction", "sale"),
                    property_type=arguments.get("property_type", "any"),
                    building_type=arguments.get("building_type"),
                    min_price=arguments.get("min_price"),
                    max_price=arguments.get("max_price"),
                    min_beds=arguments.get("min_beds"),
                    min_baths=arguments.get("min_baths"),
                    ownership=arguments.get("ownership"),
                    sort=arguments.get("sort", "newest"),
                    page=max(1, arguments.get("page", 1)),
                    browser_solver=browser_solver,
                )
                return _format_result(name, result, start_time)

            else:
                elapsed = (time.time() - start_time) * 1000
                _log(f"TOOL END: {name} UNKNOWN time={elapsed:.1f}ms")
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Unknown tool: {name}")], isError=True,
                )

        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            _log(f"TOOL END: {name} EXCEPTION={type(e).__name__} time={elapsed:.1f}ms")
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error: {type(e).__name__}: unexpected error (check server logs)")], isError=True,
            )

    return server


async def run_stdio_server(config: Config | None = None) -> None:
    """Run the server in stdio mode."""

    from . import __version__

    # Capture source identity before config loading, model/browser preflight,
    # or any other startup work can execute against bytes that later change on
    # disk. The shutdown record must match this exact entry-time snapshot.
    fetchaller_source = Path(__file__).resolve()
    wafer_spec = importlib.util.find_spec("wafer")
    wafer_source = (
        Path(wafer_spec.origin).resolve()
        if wafer_spec is not None and wafer_spec.origin is not None
        else None
    )
    if wafer_source is None or not wafer_source.is_file():
        raise RuntimeError("wafer source identity is unavailable")
    _log(
        f"fetchaller MCP stdio server v{__version__} starting "
        f"PROCESS_IDENTITY pid={os.getpid()} "
        f"fetchaller_source={fetchaller_source} "
        f"fetchaller_sha256={_python_source_tree_sha256(fetchaller_source)} "
        f"wafer_source={wafer_source} "
        f"wafer_sha256={_python_source_tree_sha256(wafer_source)}"
    )

    if config is None:
        config = load_config()

    # BrowserSolver preflight is synchronous Patchright work and cannot execute
    # on the active asyncio loop used by stdio.
    import asyncio

    server = await asyncio.to_thread(create_server, config)

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        try:
            await cleanup_server(server)
        finally:
            _log(
                "PROCESS_IDENTITY_END "
                f"pid={os.getpid()} "
                f"fetchaller_source={fetchaller_source} "
                "fetchaller_sha256="
                f"{_python_source_tree_sha256(fetchaller_source)} "
                f"wafer_source={wafer_source} "
                f"wafer_sha256={_python_source_tree_sha256(wafer_source)}"
            )
