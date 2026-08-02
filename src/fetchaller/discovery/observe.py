"""Observing what requests a page actually makes.

This is the only part of discovery that needs a browser, and it needs one only
as a *discovery* tool — nothing here runs on a search. The output is a list of
exchanges plus the settled DOM, which everything downstream reads without ever
launching a browser again.

**Why a plain browser and not wafer's.** Deciding which JSON payload on a page
is the job listing is content analysis, not bot bypass, and none of the seven
boards this was validated against issues a challenge. wafer owns blocking; this
owns looking. The one case that inverts that — discovering an endpoint on a
board which *is* challenge-protected — is detected here and reported as
:class:`ChallengeEncounteredError` rather than guessed at, because a plain browser
will simply be blocked and every downstream measurement would be taken against
the interstitial.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from wafer.browser import hardened_launch_config, scrub_headless_ua

from ..config import get_wafer_cache_dir
from ..ratelimit import discovery_limiter
from .payload import collection_size, decode_payload, looks_rate_limited

logger = logging.getLogger(__name__)

# Assets and telemetry. Everything not in here is a candidate.
#
# Skipping `script` is a deliberate trade: it keeps the capture small and
# scannable, at the cost of not being able to trace a token that exists only
# inside a JS chunk. Meta's `doc_id` is exactly that case — it is captured once
# as a literal and cannot be re-derived from traffic, which is why
# meta_careers' own bundle scan stays the better mechanism *there*.
SKIP_RESOURCE_TYPES = frozenset(
    {
        "image",
        "stylesheet",
        "font",
        "media",
        "manifest",
        "script",
        "texttrack",
        "websocket",
        "eventsource",
        "ping",
        "csp_violation",
        "preflight",
    }
)

# Resource types that can plausibly *be* the data. `document` is included
# because an SSR board can answer the navigation with the payload itself.
DATA_RESOURCE_TYPES = frozenset({"xhr", "fetch", "document", "other"})

# `response.body()` returns decoded bytes, so these describe different bytes
# than the ones being carried and must not be replayed.
_STRIP_RESPONSE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})

MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_EXCHANGES = 400

# Accessibility and HTML conventions, not site knowledge. Tried in order; the
# first present, visible *and enabled* control wins.
NUDGE_SELECTORS = (
    "[rel~=next]",
    "[aria-label*='next page' i]",
    "[aria-label*='next result' i]",
    "[data-testid*='next' i]",
    "[aria-label*='next' i]",
    "button[class*='next' i]",
    "a[class*='next' i]",
    "[aria-label*='load more' i]",
    "[aria-label*='show more' i]",
    "button[class*='load-more' i]",
)

_SETTLE_POLL_SECONDS = 0.25
_SETTLE_STABLE_READINGS = 3
_SETTLE_CAP_SECONDS = 10.0
_NUDGE_SETTLE_SECONDS = 8.0

# Second-level suffixes that are registries rather than registrable names. Not
# a full public suffix list — enough to keep the ranking's outermost sort key
# honest for the domains job boards actually use.
_MULTIPART_TLDS = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "or.jp", "ne.jp",
        "com.au", "net.au", "org.au", "co.nz", "com.br", "com.cn", "com.mx",
        "co.in", "co.za", "com.sg", "com.hk", "co.kr",
    }
)

_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform",
    "geo.captcha-delivery.com",
    "datadome",
    "px-captcha",
    "_pxhc",
    "incapsula incident",
    "/_incapsula_resource",
    "are you a human",
)


class DiscoveryUnavailableError(RuntimeError):
    """The browser needed to observe traffic could not be started."""


class ChallengeEncounteredError(RuntimeError):
    """The board answered with a bot interstitial rather than its own page.

    Discovery deliberately runs an ordinary browser. A board that challenges
    needs wafer's hardened one, so this is reported rather than worked around —
    every measurement taken against an interstitial would be measuring the
    interstitial.
    """


def registrable_domain(host: str) -> str:
    """Best-effort registrable domain, for the same-origin sort key."""
    host = (host or "").rstrip(".").casefold()
    if not host or host.replace(".", "").isdigit():
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTIPART_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


@dataclass(frozen=True)
class Exchange:
    """One request/response pair the page made while rendering."""

    order: int
    phase: str  # "load" | "nudge"
    method: str
    url: str
    resource_type: str
    status: int
    request_headers: dict[str, str]
    request_body: str | None
    response_headers: dict[str, str]
    body: str

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").casefold()

    @property
    def content_type(self) -> str:
        return self.response_headers.get("content-type", "").casefold()

    @property
    def is_data_type(self) -> bool:
        return self.resource_type in DATA_RESOURCE_TYPES


@dataclass
class Capture:
    """Everything one observation pass learned about a page."""

    url: str
    html: str
    exchanges: list[Exchange] = field(default_factory=list)
    cookies: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # What the caller asked for, which a redirect can strip. Uber sends
    # `/careers/list/?query=engineer` to `/en/`, discarding the only hint
    # ranking had — after which site navigation (16 entries, fully rendered,
    # so high coverage) outscored everything and was returned as the answer.
    requested_url: str = ""

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").casefold()

    @property
    def page_status(self) -> int:
        """Status of the navigation document itself, or 0 if it was not seen.

        Worth surfacing separately: a rate-limited or refused page still
        renders *something*, and every exchange on it is correctly rejected as
        non-data. Without this, that outcome reads as "the board has no API"
        rather than "you were throttled" — the same empty-versus-wrong
        confusion this package exists to remove.
        """
        fallback = 0
        for exchange in self.exchanges:
            if exchange.resource_type != "document" or exchange.host != self.host:
                continue
            # A 3xx is a hop, not an outcome. Meta redirects /jobs?q=… to
            # /jobsearch/?q=…, so reporting the first document's status called a
            # perfectly good capture "HTTP 301; nothing was observed".
            if 300 <= exchange.status < 400:
                fallback = fallback or exchange.status
                continue
            return exchange.status
        return fallback


class _Recorder:
    """Collects exchanges off the page's response event.

    Order is assigned synchronously at handler entry, before the first await,
    so it reflects the order responses actually arrived. Provenance depends on
    that ordering: a token can only have been minted by an *earlier* exchange.
    """

    def __init__(self) -> None:
        self.exchanges: list[Exchange] = []
        self.phase = "load"
        self.notes: list[str] = []
        self._order = 0
        self._pending: set[asyncio.Task] = set()
        self._capped = False
        self._closed = False
        self._truncated = 0

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    def stop(self) -> None:
        """Stop accepting new responses.

        Called before the final drain. Without it the listener stays live while
        drain() waits, so a response arriving just as the pending set empties
        schedules a body read that nobody awaits — and the browser then closes
        underneath it, losing the exchange or erroring mid-collect.
        """
        self._closed = True

    def _on_response(self, response) -> None:
        if self._closed:
            return
        try:
            if response.request.resource_type in SKIP_RESOURCE_TYPES:
                return
        except Exception:  # noqa: BLE001 - a torn-down request tells us nothing
            return
        if len(self.exchanges) + len(self._pending) >= MAX_EXCHANGES:
            if not self._capped:
                self._capped = True
                self.notes.append(f"capture capped at {MAX_EXCHANGES} exchanges")
                logger.info("discovery: exchange cap reached (%d)", MAX_EXCHANGES)
            return
        order = self._order
        self._order += 1
        phase = self.phase
        task = asyncio.ensure_future(self._collect(response, order, phase))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _collect(self, response, order: int, phase: str) -> None:
        try:
            request = response.request
            method = request.method
            url = request.url
            resource_type = request.resource_type
            status = response.status
        except Exception:  # noqa: BLE001 - the exchange is gone; nothing to record
            return

        request_headers = await self._request_headers(request)
        request_body = await self._request_body(request)
        response_headers = await self._response_headers(response)
        body = await self._response_body(response)

        self.exchanges.append(
            Exchange(
                order=order,
                phase=phase,
                method=method,
                url=url,
                resource_type=resource_type,
                status=status,
                request_headers=request_headers,
                request_body=request_body,
                response_headers=response_headers,
                body=body,
            )
        )

    async def _request_headers(self, request) -> dict[str, str]:
        try:
            raw = await request.all_headers()
        except Exception:  # noqa: BLE001 - fall back to the unresolved view
            try:
                raw = dict(request.headers)
            except Exception:  # noqa: BLE001
                return {}
        return {str(k).casefold(): str(v) for k, v in (raw or {}).items()}

    async def _request_body(self, request) -> str | None:
        # Both accessors can raise; each is guarded separately because the
        # buffer can be unavailable while the string form is not. Capped like
        # response bodies: an upload-heavy page would otherwise hold up to
        # MAX_EXCHANGES full request payloads in memory.
        try:
            buffer = request.post_data_buffer
            if buffer:
                return bytes(buffer)[:MAX_BODY_BYTES].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        try:
            body = request.post_data
        except Exception:  # noqa: BLE001
            return None
        return body[:MAX_BODY_BYTES] if body else body

    async def _response_headers(self, response) -> dict[str, str]:
        try:
            raw = await response.all_headers()
        except Exception:  # noqa: BLE001
            try:
                raw = dict(response.headers)
            except Exception:  # noqa: BLE001
                return {}
        return {
            str(k).casefold(): str(v)
            for k, v in (raw or {}).items()
            if str(k).casefold() not in _STRIP_RESPONSE_HEADERS
        }

    async def _response_body(self, response) -> str:
        # Raises on redirects and on transfers Chrome never buffered. A partial
        # record is still useful for token provenance, so an empty body is
        # preferred to dropping the exchange.
        try:
            raw = await response.body()
        except Exception:  # noqa: BLE001
            return ""
        if not raw:
            return ""
        if len(raw) > MAX_BODY_BYTES:
            self._truncated += 1
            raw = raw[:MAX_BODY_BYTES]
        return raw.decode("utf-8", "replace")

    async def drain(self) -> None:
        while self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        self.exchanges.sort(key=lambda e: e.order)
        if self._truncated:
            self.notes.append(f"{self._truncated} response bodies truncated at {MAX_BODY_BYTES} bytes")


async def _settle(page, budget: float) -> None:
    """Three bounded steps, each falling through rather than failing."""
    deadline = time.monotonic() + budget

    with contextlib.suppress(Exception):
        # Analytics beacons and long polls mean some pages never go idle, so
        # this gets half the remaining budget and no more. Floored at 1ms
        # because Playwright reads timeout=0 as "wait forever", which would
        # hang exactly when the budget has already run out.
        remaining = max(0.0, deadline - time.monotonic())
        await page.wait_for_load_state("networkidle", timeout=max(1.0, remaining * 500))

    # A page with a running animation never looks stable, so the cap matters
    # as much as the stability test.
    stable = 0
    previous = -1
    hard_stop = min(deadline, time.monotonic() + _SETTLE_CAP_SECONDS)
    while time.monotonic() < hard_stop and stable < _SETTLE_STABLE_READINGS:
        try:
            size = await page.evaluate("document.documentElement.outerHTML.length")
        except Exception:  # noqa: BLE001 - mid-navigation; try again
            size = -1
        if size == previous and size > 0:
            stable += 1
        else:
            stable = 0
            previous = size
        await asyncio.sleep(_SETTLE_POLL_SECONDS)


async def _scroll_nudge(page, recorder: _Recorder) -> str | None:
    """Scroll to the bottom, the other universal "load more" convention.

    Pagination controls are only half of it. Meta's ``/jobsearch/`` renders its
    first page server-side and loads the rest on infinite scroll, so there is no
    next control to click at all — the nudge reported "no pagination control"
    and discovery then settled for a routing payload. Scrolling is as generic as
    clicking ``[rel~=next]``: no board is named, and a page with nothing more to
    load simply does not react.
    """
    try:
        before = await page.evaluate("document.body.scrollHeight")
    except Exception:  # noqa: BLE001
        return None
    recorder.phase = "nudge"
    for _ in range(2):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:  # noqa: BLE001
            recorder.phase = "load"
            return None
        await _settle(page, _NUDGE_SETTLE_SECONDS / 2)
        try:
            after = await page.evaluate("document.body.scrollHeight")
        except Exception:  # noqa: BLE001
            break
        if after > before:
            return "scroll"
        before = after
    recorder.phase = "load"
    return None


async def _nudge(page, recorder: _Recorder) -> str | None:
    """Perform at most one interaction that asks the page for more data."""
    for selector in NUDGE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            # A disabled next control is the single-page case: clicking it
            # spends the budget and learns nothing.
            if not await locator.is_visible():
                continue
            if not await locator.is_enabled():
                continue
        except Exception:  # noqa: BLE001 - selector unusable; try the next one
            continue
        try:
            recorder.phase = "nudge"
            await locator.click(timeout=5000)
        except Exception:  # noqa: BLE001 - not clickable after all
            recorder.phase = "load"
            continue
        await _settle(page, _NUDGE_SETTLE_SECONDS)
        return selector
    return await _scroll_nudge(page, recorder)


def default_profile_dir() -> str:
    """Where the observing browser keeps its profile between passes."""
    base = get_wafer_cache_dir()
    root = Path(base).parent if base else Path(tempfile.gettempdir()) / "fetchaller"
    return str(root / "discovery" / "browser-profile")


_scrubbed_ua: str | None = None


async def _probe_user_agent(pw, launch: dict) -> str | None:
    """Read the launched browser's own UA and strip the ``HeadlessChrome`` token.

    ``--headless=new`` does not remove it, and that token alone earns degraded
    service from sites that challenge nothing otherwise. Read and scrub rather
    than compose one, so the Chrome version stays truthful.

    Cached per process: the persistent-profile path needs the value *before* its
    context exists, so it cannot probe its own.
    """
    global _scrubbed_ua
    if _scrubbed_ua is not None:
        return _scrubbed_ua
    try:
        browser = await pw.chromium.launch(**launch)
    except Exception:  # noqa: BLE001
        return None
    try:
        page = await browser.new_page()
        raw = await page.evaluate("navigator.userAgent")
        await page.close()
        _scrubbed_ua = scrub_headless_ua(raw) if raw else None
    except Exception:  # noqa: BLE001
        _scrubbed_ua = None
    finally:
        with contextlib.suppress(Exception):
            await browser.close()
    return _scrubbed_ua


async def _register_init_scripts(page, scripts) -> None:
    """Install wafer's headless corrections via CDP.

    Registered through ``Page.addScriptToEvaluateOnNewDocument`` after
    ``Page.enable``. The CDP session is deliberately **not** detached
    afterwards — detaching unregisters them.
    """
    if not scripts:
        return
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.enable")
        for script in scripts:
            await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})
    except Exception as exc:  # noqa: BLE001 - corrections are best-effort
        logger.info("discovery: could not register init scripts (%s)", exc)


async def _open(pw, *, headless: bool, profile_dir: str | None):
    """Start a hardened browser, preferring a persistent profile.

    **The launch configuration comes from wafer, not from a flag list copied
    here**, so a Chrome bump on wafer's side reaches discovery. It strips
    ``--enable-automation`` — the strongest single automation signal — and
    Playwright's ``--force-color-profile=srgb``, and switches to
    ``--headless=new``.

    This is not defensive tuning. A bare ``headless=True`` launch announces
    ``HeadlessChrome/…`` in its user agent, and that alone earned Meta's rate
    limiter and Cloudflare's challenge on Uber's prefetches. Both produced
    *degraded answers that were then recorded as facts about those boards* —
    three wrong verdicts between them. A flagged browser does not fail loudly;
    it quietly measures something else.

    A persistent profile is preferred because a blank one every pass makes the
    origin see a brand-new anonymous visitor each time. It falls back to an
    ephemeral browser when the directory is locked by a concurrent pass.
    """
    config = hardened_launch_config(headless=headless)
    launch = {
        "headless": headless,
        "args": list(config.args),
        "ignore_default_args": list(config.ignore_default_args),
    }
    viewport = {"width": 1440, "height": 900}
    user_agent = await _probe_user_agent(pw, launch)

    if profile_dir:
        try:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            context = await pw.chromium.launch_persistent_context(
                profile_dir, viewport=viewport, user_agent=user_agent, **launch
            )
            return None, context, config
        except Exception as exc:  # noqa: BLE001 - locked or unwritable
            logger.info("discovery: persistent profile unavailable (%s); using a fresh one", exc)

    try:
        browser = await pw.chromium.launch(**launch)
    except Exception as exc:  # noqa: BLE001
        raise DiscoveryUnavailableError(f"could not start a browser: {exc}") from exc
    context = await browser.new_context(viewport=viewport, user_agent=user_agent)
    return browser, context, config


async def _warm_origin(context, page, url: str, deadline: float) -> None:
    """Visit the target's own origin root before the target itself."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return
    root = f"{parts.scheme}://{parts.netloc}/"
    if root.rstrip("/") == url.rstrip("/"):
        return  # already the root
    try:
        existing = await context.cookies(root)
        if existing:
            return  # this profile has been here before
    except Exception:  # noqa: BLE001 - no cookie view is not a reason to skip
        pass
    if time.monotonic() >= deadline:
        return
    with contextlib.suppress(Exception):
        await page.goto(root, wait_until="domcontentloaded", timeout=15000)
        await _settle(page, 3.0)


def _looks_challenged(html: str) -> bool:
    lowered = (html or "")[:20000].casefold()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def throttled_exchanges(exchanges: list[Exchange], host: str) -> list[Exchange]:
    """Same-host 2xx exchanges whose payload is a throttle notice.

    The measurable sibling of :func:`challenged_exchanges`. A challenge has to
    be attributed by replay; a rate-limit error states itself, so this one is
    decisive on its own.
    """
    out = []
    for e in exchanges:
        if e.host != host or not (200 <= e.status < 300) or not e.body:
            continue
        decoded = decode_payload(e.body)
        if decoded is not None and looks_rate_limited(decoded):
            out.append(e)
    return out


def challenged_exchanges(exchanges: list[Exchange], host: str) -> list[Exchange]:
    """Same-host exchanges the *capture browser* was served an interstitial for.

    The page loading fine says nothing about its data requests: Uber's board
    renders normally, then prefetches each posting — ``/en/jobs/300543/?_rsc=…``,
    real requisition ids — and the browser is answered ``403 Just a moment...``
    for all sixteen.

    **This detects a refusal; it does not attribute one.** Uber is the worked
    example, and it is evidence *against* blaming the board. The same
    ``/en/jobs/300543/?_rsc=1`` answered plain ``wafer`` with ``200``, 383,021
    bytes of ``text/x-component``, 66 Flight rows carrying job id, location,
    salary and department — no interstitial marker, no challenge, zero
    rotations. The 403s were a property of *this browser*: it was launching
    with ``--enable-automation`` and a ``HeadlessChrome`` user agent. Once
    :func:`_open` adopted wafer's hardened launch configuration, every one of
    those prefetches returned ``200``.

    Three investigations died on that distinction — "server-rendered, correctly
    refused", then "the RSC decoder is missing", then "challenge-protected, send
    it to wafer" — because none replayed a refused exchange over plain HTTP.
    That check is one request and it is decisive, so callers must run it before
    concluding anything: see ``pipeline._diagnose_refusals``. Never route a
    browser-side 403 to wafer without it.
    """
    return [
        e
        for e in exchanges
        if e.host == host and (e.status in (401, 403, 429, 503)) and _looks_challenged(e.body)
    ]


def _has_own_host_data(exchanges: list[Exchange], host: str) -> bool:
    """Whether the load phase already fetched a *record set* on the page's own host.

    Three real boards each defeated a weaker version of this gate, and the
    progression is the whole argument for the final rule:

    **Exact host, not registrable domain.** Apple's search page issues no XHR of
    its own but pulls a global-header payload from ``www.apple.com`` while the
    page sits on ``jobs.apple.com``. That payload scores 12.35 under the
    ranking — high enough to suppress a lenient gate and leave
    ``POST /api/v1/search`` undiscovered forever.

    **Not merely a request.** Google's results page fires two ``204`` analytics
    beacons at ``www.google.com/g/collect`` on load. Same-host XHRs and nothing
    else; counting them left ``batchexecute`` undiscovered.

    **A record set, not merely a payload.** Routing and telemetry payloads
    satisfy any gate that only asks for a non-empty same-host body — Meta's
    ``bulk-route-definitions`` is 42 KB of them with zero records — and
    discovery then minimizes the routing endpoint instead of the search.

    Requiring a *record set* separates them: real listings carry one (Netflix
    10, Workday 70, Amazon's jobs array), routing and beacons carry none.

    **Correction.** This rule was originally justified by a claim that Meta
    server-renders its results and issues no search query. That was false on
    both clauses. Of the first 25 titles the GraphQL search returns, **zero**
    appear in the 461,620-byte document, and the browser *does* issue the
    query — with the right ``doc_id`` and byte-identical variables. It came
    back ``HTTP 200`` carrying ``{"errors":[{"message":"Rate limit
    exceeded"}]}`` in 114 bytes, because the capture browser was announcing
    ``HeadlessChrome`` and being throttled for it. The gate's Google and Apple
    justifications were measured independently and still stand; the Meta one is
    withdrawn. See :func:`throttled_exchanges`.
    """
    for exchange in exchanges:
        if exchange.phase != "load" or exchange.host != host:
            continue
        if exchange.resource_type not in ("xhr", "fetch"):
            continue
        if not (200 <= exchange.status < 300) or not exchange.body:
            continue
        if collection_size(decode_payload(exchange.body)) > 0:
            return True
    return False


async def capture(
    url: str,
    *,
    timeout: float = 90.0,
    headless: bool = True,
    nudge: bool = True,
    profile_dir: str | None = None,
    warm: bool = True,
) -> Capture:
    """Load ``url`` in a browser and record every non-asset exchange."""
    if profile_dir is None:
        profile_dir = default_profile_dir()
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise DiscoveryUnavailableError(
            "SPA discovery needs a browser; install wafer-py[browser]."
        ) from exc

    deadline = time.monotonic() + timeout
    recorder = _Recorder()

    # The browser navigates outside wafer, so nothing else spaces these. One
    # page load is not a burst, but back-to-back discovery passes on the same
    # host are — and a board that throttles the *navigation* returns a page
    # whose every exchange is correctly rejected as non-data, which reads as
    # "this board has no API" rather than "you were throttled".
    await discovery_limiter.wait()

    async with async_playwright() as pw:
        browser, context, config = await _open(pw, headless=headless, profile_dir=profile_dir)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await _register_init_scripts(page, config.init_scripts)

            # Arrive at the site root before the deep link, unless this profile
            # already holds cookies for the origin. Landing straight on a deep
            # search URL with no prior page view is not how a browser session
            # ever begins, and origins throttle it: measured on metacareers.com,
            # `/` answered 200 while `/jobs?q=…` was rate-limited outright.
            #
            # This is a session convention, not site knowledge — no board is
            # named, and the root is derived from the target URL.
            if warm:
                await _warm_origin(context, page, url, deadline)

            # Attached only now, so the warm-up's own payloads never compete in
            # ranking.
            recorder.attach(page)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=max(5000, int(timeout * 500)))
            except Exception as exc:  # noqa: BLE001 - a partial load still teaches us something
                recorder.notes.append(f"navigation did not complete cleanly: {type(exc).__name__}")

            await _settle(page, max(1.0, (deadline - time.monotonic()) * 0.5))

            try:
                html = await page.content()
            except Exception:  # noqa: BLE001
                html = ""
            if _looks_challenged(html):
                raise ChallengeEncounteredError(
                    f"{urlsplit(url).hostname} answered with a bot interstitial. "
                    "Discovery runs an ordinary browser; this board needs wafer's."
                )

            await recorder.drain()
            own_host = (urlsplit(page.url).hostname or "").casefold()
            # Gate on same-host data, not on "did anything rank at all".
            # Erring toward nudging costs seconds; erring away returns nothing.
            if nudge and not _has_own_host_data(recorder.exchanges, own_host):
                if time.monotonic() >= deadline:
                    recorder.notes.append("no budget left to nudge")
                else:
                    selector = await _nudge(page, recorder)
                    if selector:
                        recorder.notes.append(f"nudged with {selector}")
                    else:
                        recorder.notes.append("no pagination control to nudge")

            # Detach before the final drain so a late response cannot schedule
            # a body read that nobody awaits.
            recorder.stop()
            await recorder.drain()

            try:
                html = await page.content()
            except Exception:  # noqa: BLE001
                pass
            try:
                cookies = await context.cookies()
            except Exception:  # noqa: BLE001
                cookies = []
            final_url = page.url or url
        finally:
            with contextlib.suppress(Exception):
                # A persistent context owns its own lifetime; there is no
                # separate browser object to close.
                await (browser.close() if browser is not None else context.close())

    return Capture(
        url=final_url,
        requested_url=url,
        html=html,
        exchanges=recorder.exchanges,
        cookies=list(cookies),
        notes=recorder.notes,
    )
