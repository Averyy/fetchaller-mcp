"""Bot challenge detection, cookie caching, and solving.

Handles Cloudflare, Akamai, DataDome, PerimeterX, Imperva, Kasada,
Alibaba Cloud WAF (acw_sc__v2), Amazon rate-limit captcha, and
unknown JS challenges.

ACW challenges are solved inline with pure Python (~1ms).
Amazon rate-limit captchas are solved inline with curl_cffi (~10ms).
All other challenges use PyDoll (persistent headless Chrome).
"""

import asyncio
import json
import logging
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .config import BROWSER_FINGERPRINTS, Config

logger = logging.getLogger(__name__)

# Suppress expected PyDoll Turnstile timeout errors (JS-only CF challenges
# never show the visual checkbox, so the Turnstile bypass always times out).
logging.getLogger("pydoll.elements.mixins.find_elements_mixin").setLevel(logging.CRITICAL)
logging.getLogger("pydoll.browser.tab").setLevel(logging.CRITICAL)

# Max cookie cache entries (LRU eviction when exceeded) [#27]
_MAX_CACHE_ENTRIES = 200
# Default TTL for non-CF entries (24 hours) [#27]
_DEFAULT_TTL = 86400


def _log(msg: str) -> None:
    # Match format used by all other modules (no module prefix) [#16]
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)


# ── ACW SC V2 Solver (Alibaba Cloud WAF) ─────────────────────────────────────
# Pure Python, ~1ms, zero external deps. The challenge page contains obfuscated
# JS with RC4-encoded constants, but after deobfuscation the shuffle table and
# XOR key are always the same across all sites. We just extract arg1, shuffle,
# and XOR.

# Fixed constants (identical after deobfuscation on every site using this WAF)
_ACW_SHUFFLE = [
    15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23,
    25, 13, 6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4,
    17, 5, 3, 28, 34, 37, 12, 36,
]
_ACW_KEY = "3000176000856006061501533003690027800375"


def is_acw_waf_challenge(body: str) -> bool:
    """Detect Alibaba Cloud WAF challenge page."""
    return "acw_sc__v2" in body and "arg1" in body


def solve_acw_sc_v2(html: str) -> str | None:
    """Solve Alibaba Cloud WAF acw_sc__v2 challenge.

    Extracts arg1 from the challenge HTML, applies the fixed shuffle
    permutation, and XORs with the fixed key to produce the cookie value.

    Returns:
        Cookie value (40-char hex string), or None if arg1 not found.
    """
    arg1_m = re.search(r"var\s+arg1\s*=\s*'([0-9A-Fa-f]+)'", html)
    if not arg1_m:
        return None
    arg1 = arg1_m.group(1)

    if len(arg1) < max(_ACW_SHUFFLE):
        return None

    # Shuffle: output[i] = arg1[table[i] - 1]
    shuffled = "".join(arg1[v - 1] for v in _ACW_SHUFFLE)

    # XOR hex pairs
    result = []
    for i in range(0, min(len(shuffled), len(_ACW_KEY)), 2):
        xored = int(shuffled[i : i + 2], 16) ^ int(_ACW_KEY[i : i + 2], 16)
        result.append(f"{xored:02x}")
    return "".join(result)


# ── Amazon Inline Captcha Solver ──────────────────────────────────────────────
# Amazon's rate-limit interstitial is a simple HTML page with a "Continue
# shopping" link or form — no JS challenge, no image CAPTCHA. We can solve
# it inline with curl_cffi (~10ms) instead of spinning up Chrome (~5-10s).


_AMAZON_DOMAIN_RE = re.compile(
    r"(?:^|\.)(?:amazon|amzn)\."
    r"(?:com|ca|co\.uk|de|fr|it|es|co\.jp|com\.au|in|com\.br|com\.mx|"
    r"nl|sg|sa|ae|eg|pl|se|tr|to|com\.be|cn|com\.tr|com\.sg)$",
    re.IGNORECASE,
)


def _is_amazon_domain(url: str) -> bool:
    """Check if URL points to a known Amazon domain (security: prevents external SSRF)."""
    hostname = urlparse(url).hostname or ""
    return bool(_AMAZON_DOMAIN_RE.search(hostname))


def parse_amazon_captcha(html: str, page_url: str) -> dict | None:
    """Parse Amazon rate-limit captcha page to extract the submission target.

    Returns:
        Dict with 'method' ('GET'/'POST'), 'url' (absolute), 'params' (dict),
        or None if the page structure is unrecognized or target is non-Amazon.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    # Strategy 1: Find <a> links with "continue shopping" text
    for a in soup.find_all("a", href=True):
        if "continue shopping" in (a.get_text() or "").lower():
            href = urljoin(page_url, a["href"])
            if _is_amazon_domain(href):
                return {"method": "GET", "url": href, "params": {}}

    # Strategy 2: Find <form> and extract action + hidden fields
    form = soup.find("form")
    if form:
        action = form.get("action", "")
        abs_action = urljoin(page_url, action) if action else page_url
        if _is_amazon_domain(abs_action):
            method = (form.get("method") or "GET").upper()
            params = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    params[name] = inp.get("value", "")
            return {"method": method, "url": abs_action, "params": params}

    return None


async def solve_amazon_inline(
    html: str, page_url: str, impersonate: str = "",
    original_cookies: list[dict] | None = None,
) -> dict | None:
    """Solve Amazon rate-limit captcha inline via curl_cffi form submission.

    Args:
        original_cookies: Cookies from the original request session. Amazon's
            captcha form needs the session context (session-id, etc.) to associate
            the "continue shopping" action with the rate-limited session.

    Returns:
        Dict with 'cookies' (list of cookie dicts), 'user_agent' (str),
        and 'impersonate' (str — the TLS fingerprint used during solve),
        or None if solving failed.
    """
    from .content.fetcher import _find_ca_bundle

    if not impersonate:
        impersonate = DEFAULT_IMPERSONATE

    parsed_target = parse_amazon_captcha(html, page_url)
    if not parsed_target:
        _log("Amazon captcha: unrecognized page structure, falling through to Chrome")
        return None

    target_url = parsed_target["url"]
    parsed = urlparse(target_url)

    # Scheme validation — only allow http/https (libcurl supports file://, ftp://, etc.)
    if parsed.scheme not in ("http", "https"):
        _log(f"Amazon captcha: non-http scheme in target URL: {_sanitize_url(target_url)}")
        return None

    # SSRF check on target URL (async version — resolves DNS, catches rebinding)
    target_host = parsed.hostname or ""
    try:
        from .security.ssrf import is_private_host
        if await is_private_host(target_host):
            _log(f"Amazon captcha: target URL points to private host {target_host}")
            return None
    except Exception:
        return None

    try:
        from curl_cffi.requests import AsyncSession

        ca_bundle = _find_ca_bundle()
        session_kwargs = {"impersonate": impersonate}
        if ca_bundle:
            session_kwargs["verify"] = ca_bundle

        async with AsyncSession(**session_kwargs) as session:
            # Inject original session cookies so Amazon associates the captcha
            # solve with the rate-limited session (session-id, etc.)
            if original_cookies:
                for c in original_cookies:
                    session.cookies.set(
                        c.get("name", ""),
                        c.get("value", ""),
                        domain=c.get("domain"),
                        path=c.get("path", "/"),
                    )

            if parsed_target["method"] == "POST":
                resp = await session.post(
                    target_url,
                    data=parsed_target["params"],
                    headers={"Referer": page_url},
                    allow_redirects=True,
                    timeout=10,
                )
            else:
                resp = await session.get(
                    target_url,
                    params=parsed_target["params"] or None,
                    headers={"Referer": page_url},
                    allow_redirects=True,
                    timeout=10,
                )

            # SSRF check on final URL after redirects — always validate, even
            # if hostname didn't change (guards against DNS rebinding)
            final_url = str(resp.url)
            final_host = urlparse(final_url).hostname or ""
            if final_host:
                from .security.ssrf import is_private_host as _async_ssrf
                if await _async_ssrf(final_host):
                    _log(f"Amazon captcha: redirect landed on private host {final_host}")
                    return None

            # Extract cookies from session jar
            cookies = []
            for cookie in session.cookies.jar:
                cookies.append({
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
                })

            if not cookies:
                _log("Amazon captcha: no cookies obtained from inline solve")
                return None

            _log(f"Amazon captcha solved inline: {len(cookies)} cookies from {_sanitize_url(target_url)}")
            return {"cookies": cookies, "user_agent": "", "impersonate": impersonate}

    except Exception:
        logger.exception("Amazon inline captcha solve failed")
        return None


# ── Challenge Detection ───────────────────────────────────────────────────────


def is_amazon_captcha(body: str) -> bool:
    """Detect Amazon rate-limit / captcha page.

    Amazon serves a 200 OK with a small page (~5-15K) containing just a
    "Continue shopping" button when it rate-limits or suspects bot activity.
    Normal product pages are 1-3M chars, so body size is a reliable signal.

    Checks for Amazon-specific markers to avoid false positives on non-Amazon
    small pages that happen to contain "continue shopping".
    """
    if len(body) >= 50_000:
        return False
    body_lower = body.lower()
    if "continue shopping" not in body_lower:
        return False
    # Require Amazon-specific markers (any one is sufficient)
    return "amazon" in body_lower or "amzn" in body_lower or "/errors/validatecaptcha" in body_lower


def detect_challenge(status_code: int, headers: dict[str, str], body: str) -> str | None:
    """Detect bot challenge type from HTTP response.

    Returns:
        Challenge type string ('acw', 'cloudflare', 'akamai', 'datadome',
        'perimeterx', 'imperva', 'kasada', 'amazon', 'unknown') or None.
    """
    set_cookie = headers.get("set-cookie", "")

    # --- Pure Python solves (no browser) ---

    # Alibaba Cloud WAF [#25: call is_acw_waf_challenge instead of duplicating]
    if is_acw_waf_challenge(body):
        return "acw"

    # --- Browser solves ---

    # Cloudflare — explicit header
    if headers.get("cf-mitigated") == "challenge":
        return "cloudflare"
    # Cloudflare — fallback for older deployments
    if status_code == 403 and "window._cf_chl_opt" in body:
        return "cloudflare"

    # Akamai [#19: body-marker check now requires non-200 status]
    if "_abck" in set_cookie or "ak_bmsc" in set_cookie:
        if status_code == 403:
            return "akamai"
        if status_code != 200 and ("bmSz" in body or "sensor_data" in body or "_BomA" in body):
            return "akamai"
        # Akamai behavioral challenge — status 200 with small challenge page.
        # Normal Akamai-protected pages return 200 with large bodies (real content).
        # Behavioral challenges return 200 with tiny bodies (< 10KB) containing
        # Akamai-branded UI for human verification (tile selection, etc.).
        if status_code == 200 and len(body) < 10_000:
            if "sec-if-cpt" in body or "behavioral-content" in body:
                return "akamai"

    # [#12] Defer body.lower() — only compute once, only when status is 403/429
    body_lower: str | None = None
    if status_code in (403, 429):
        body_lower = body.lower()

    if status_code == 403 and body_lower is not None:
        if "akam" in body_lower or "akamai" in body_lower:
            return "akamai"

    # DataDome
    if "datadome" in set_cookie and status_code == 403:
        return "datadome"
    if status_code == 403 and body_lower is not None:
        if "datadome" in body_lower or "dd.js" in body_lower:
            return "datadome"

    # PerimeterX / HUMAN [#26: also detect on 429 — DigiKey returns 429 with PX challenge]
    if ("_px3" in set_cookie or "_pxhd" in set_cookie) and status_code in (403, 429):
        return "perimeterx"
    if status_code in (403, 429) and body_lower is not None:
        if "perimeterx" in body_lower or "human.security" in body_lower or "press & hold" in body_lower or "px-captcha" in body_lower:
            return "perimeterx"

    # Imperva / Incapsula
    if ("reese84" in set_cookie or "___utmvc" in set_cookie) and status_code == 403:
        return "imperva"
    if status_code == 403 and body_lower is not None:
        if "incapsula" in body_lower or "imperva" in body_lower:
            return "imperva"

    # Kasada
    if status_code == 429 and any(h.lower().startswith("x-kpsdk") for h in headers):
        return "kasada"

    # Alibaba Cloud WAF TMD punish — missing-session block page (status 200).
    # TMD blocks requests without valid session cookies. Cannot be solved by
    # extracting cookies like Akamai/CF — requires session warming (visit
    # homepage first to establish cookies, then navigate to target page).
    if status_code == 200 and "/_____tmd_____/punish" in body:
        return "tmd"

    # Amazon rate-limit / captcha (status 200, small page with "Continue shopping")
    if status_code == 200 and is_amazon_captcha(body):
        return "amazon"

    # Generic fallback — unidentified 403/429 with JS challenge page
    if status_code in (403, 429) and body_lower is not None and "<script" in body_lower and len(body) < 50_000:
        return "unknown"

    return None


# ── Cookie Cache ──────────────────────────────────────────────────────────────


class CachedCookies:
    """Cached cookie set for a domain."""

    __slots__ = ("challenge_type", "cookies", "user_agent", "impersonate", "expires", "final_url", "created_at")

    def __init__(
        self,
        challenge_type: str,
        cookies: list[dict],
        user_agent: str,
        impersonate: str,
        expires: float | None = None,
        final_url: str | None = None,
        created_at: float | None = None,
    ) -> None:
        self.challenge_type = challenge_type
        self.cookies = cookies
        self.user_agent = user_agent
        self.impersonate = impersonate
        self.expires = expires
        self.final_url = final_url
        self.created_at = created_at or time.time()

    def to_dict(self) -> dict:
        """Serialize without deep copy (cookies are already dicts). [#23]"""
        return {
            "challenge_type": self.challenge_type,
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "impersonate": self.impersonate,
            "expires": self.expires,
            "final_url": self.final_url,
            "created_at": self.created_at,
        }


class CookieCache:
    """Per-domain cookie cache with CF expiry tracking, LRU eviction, and optional disk persistence.

    Concurrency model: all public methods are synchronous (no await points),
    so in-memory dict operations are atomic within the asyncio event loop.
    A threading.Lock serializes disk I/O in _save()/_load() as defense-in-depth.
    """

    def __init__(self, persist_path: str | None = None) -> None:
        self._cache: dict[str, CachedCookies] = {}
        self._persist_path = Path(persist_path) if persist_path else None
        self._disk_lock = threading.Lock()
        if self._persist_path:
            self._load()

    def get(self, domain: str) -> CachedCookies | None:
        """Get cached cookies for domain. Proactively evicts expired entries."""
        entry = self._cache.get(domain)
        if entry is None:
            return None
        now = time.time()
        # CF: proactive eviction on expiry
        if entry.expires is not None and now >= entry.expires:
            _log(f"CF cookies expired for {domain}, evicting")
            del self._cache[domain]
            self._save()
            return None
        # Non-CF: TTL-based eviction [#27]
        if entry.expires is None and (now - entry.created_at) >= _DEFAULT_TTL:
            _log(f"Non-CF cookies expired (TTL) for {domain}, evicting")
            del self._cache[domain]
            self._save()
            return None
        return entry

    def set(
        self,
        domain: str,
        challenge_type: str,
        cookies: list[dict],
        user_agent: str,
        impersonate: str,
        final_url: str | None = None,
        *,
        _save: bool = True,
    ) -> None:
        """Cache cookies for a domain."""
        # [#10] Validate final_url isn't pointing to known-private host (sync fast path).
        # Returns True for IPs/localhost, None for hostnames needing DNS — those are
        # validated with async is_private_host() in fetch_url before actual use.
        if final_url:
            try:
                final_host = urlparse(final_url).hostname or ""
                from .security.ssrf import _is_private_host_sync
                if _is_private_host_sync(final_host) is True:
                    _log(f"Rejecting private final_url {final_url} for {domain}")
                    final_url = None
            except Exception:
                final_url = None

        expires = None
        if challenge_type == "cloudflare":
            # Extract cf_clearance expiry
            for c in cookies:
                if c.get("name") == "cf_clearance":
                    exp = c.get("expires", -1)
                    if isinstance(exp, (int, float)) and exp > 0:
                        expires = float(exp)
                    break

        # LRU eviction: remove oldest entry if at capacity [#27]
        if len(self._cache) >= _MAX_CACHE_ENTRIES and domain not in self._cache:
            oldest_domain = min(self._cache, key=lambda d: self._cache[d].created_at)
            del self._cache[oldest_domain]

        self._cache[domain] = CachedCookies(
            challenge_type=challenge_type,
            cookies=cookies,
            user_agent=user_agent,
            impersonate=impersonate,
            expires=expires,
            final_url=final_url,
        )
        _log(f"Cached {len(cookies)} cookies for {domain} (type={challenge_type}, expires={expires})")
        if _save:
            self._save()

    def evict(self, domain: str) -> None:
        """Evict cached cookies for domain (called on re-challenge)."""
        if domain in self._cache:
            _log(f"Evicting stale cookies for {domain}")
            del self._cache[domain]
            self._save()

    def _save(self) -> None:
        """Persist cache to disk as JSON (serialized by _disk_lock)."""
        if not self._persist_path:
            return
        try:
            with self._disk_lock:
                self._persist_path.parent.mkdir(parents=True, exist_ok=True)
                # [#23] Use to_dict() instead of asdict() to avoid deep copy
                data = {domain: entry.to_dict() for domain, entry in self._cache.items()}
                tmp = self._persist_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2))
                tmp.replace(self._persist_path)
        except Exception:
            logger.exception("Failed to save cookie cache")

    def _load(self) -> None:
        """Load cache from disk JSON (serialized by _disk_lock)."""
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            with self._disk_lock:
                data = json.loads(self._persist_path.read_text())
            now = time.time()
            _known_fields = {"challenge_type", "cookies", "user_agent", "impersonate", "expires", "final_url", "created_at"}
            for domain, entry_dict in data.items():
                try:
                    # Filter to known fields — forward-compatible if future versions add keys
                    entry = CachedCookies(**{k: v for k, v in entry_dict.items() if k in _known_fields})
                except (TypeError, KeyError):
                    _log(f"Skipping malformed cookie entry for {domain}")
                    continue
                # Skip expired CF entries
                if entry.expires is not None and now >= entry.expires:
                    _log(f"Skipping expired CF cookies for {domain} on load")
                    continue
                # Skip expired non-CF entries [#27]
                if entry.expires is None and (now - entry.created_at) >= _DEFAULT_TTL:
                    _log(f"Skipping TTL-expired cookies for {domain} on load")
                    continue
                # [#10] Validate final_url on load (sync fast path only — DNS
                # hostnames validated async in fetch_url before use)
                if entry.final_url:
                    try:
                        final_host = urlparse(entry.final_url).hostname or ""
                        from .security.ssrf import _is_private_host_sync
                        if _is_private_host_sync(final_host) is True:
                            _log(f"Rejecting private final_url in cached entry for {domain}")
                            entry.final_url = None
                    except Exception:
                        entry.final_url = None
                self._cache[domain] = entry
            _log(f"Loaded {len(self._cache)} cookie entries from disk")
        except Exception:
            logger.exception("Failed to load cookie cache, starting fresh")


# ── Challenge Solver ──────────────────────────────────────────────────────────

# Default impersonate value — newest Chrome auto-discovered from curl_cffi [#3]
DEFAULT_IMPERSONATE = BROWSER_FINGERPRINTS[-1]


class ChallengeSolver:
    """Browser-based challenge solver with persistent Chrome and global lock.

    Uses PyDoll for headless Chrome automation. One Chrome instance shared
    across all solves, with idle timeout for resource cleanup.

    NOTE: Chrome runs with --no-sandbox because the Chrome sandbox requires
    elevated container capabilities (SYS_ADMIN). Container isolation via
    non-root appuser and Docker resource limits provides the security boundary. [#28]
    """

    def __init__(self, config: Config | None = None) -> None:
        self._browser = None
        self._tab = None
        self._xvfb = None  # Xvfb subprocess (Docker/CI)
        self._original_display: str | None = None  # [#5] Track original DISPLAY for cleanup
        self._lock = asyncio.Lock()
        self._last_used: float = 0
        self._idle_timeout = (config.chrome_idle_timeout if config else 60) * 60  # Convert minutes to seconds

        # Kill Chrome on exit — even if the MCP server gets SIGTERM'd without
        # calling close(). Prevents zombie Chrome processes when Claude Code
        # closes the connection or the server crashes.
        import atexit
        import signal

        def _kill_on_exit():
            self._sync_kill_browser()
        atexit.register(_kill_on_exit)

        # SIGTERM (sent by Claude Code on session close) and SIGHUP (terminal
        # closed) don't run atexit handlers by default. Install signal handlers
        # that clean up Chrome before exiting.
        def _signal_handler(signum, frame):
            self._sync_kill_browser()
            # Re-raise with default handler so the process actually exits
            signal.signal(signum, signal.SIG_DFL)
            import os
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(sig, _signal_handler)
            except (OSError, ValueError):
                pass  # Can't set handlers in non-main thread

    def _sync_kill_browser(self) -> None:
        """Synchronously kill Chrome process. For use in atexit/signal handlers
        where async is not available.

        Note: We intentionally skip proc.wait() — signal handlers must be fast
        and non-blocking. The OS reaps the zombie when the parent exits
        immediately after (via os.kill re-raise in the signal handler).
        """
        if self._browser is not None:
            try:
                proc = self._browser._browser_process_manager._process
                if proc and proc.poll() is None:
                    _log(f"atexit: killing Chrome pid={proc.pid}")
                    proc.kill()
            except Exception:
                pass
        if self._xvfb is not None:
            try:
                self._xvfb.kill()
            except Exception:
                pass

    async def solve(self, url: str, challenge_type: str) -> dict | None:
        """Solve a browser challenge and return cookies + UA.

        Args:
            url: The URL that triggered the challenge.
            challenge_type: One of 'cloudflare', 'akamai', or other browser types.

        Returns:
            Dict with 'cookies' (list of cookie dicts), 'user_agent' (str),
            'impersonate' (str — curl_cffi fingerprint for replay),
            or None if solve failed. Returns a special dict with 'error' key
            if the lock is busy.
        """
        if self._lock.locked():
            return {
                "error": "Bot challenge is being solved for another domain. Please try again in about 30 seconds."
            }

        async with self._lock:
            try:
                tab = await self._ensure_browser()
                if tab is None:
                    return None

                # [#9] SSRF validation before passing URL to browser
                try:
                    parsed = urlparse(url)
                    from .security.ssrf import is_private_host
                    if await is_private_host(parsed.hostname or ""):
                        return {"error": "Access to private/internal hosts is not allowed."}
                except Exception:
                    logger.exception(f"SSRF check failed for {url}, blocking as precaution")
                    return {"error": "Could not validate URL safety."}

                if challenge_type == "tmd":
                    result = await self._solve_tmd(tab, url)
                elif challenge_type == "cloudflare":
                    result = await self._solve_cloudflare(tab, url)
                elif challenge_type == "akamai":
                    result = await self._solve_akamai(tab, url)
                elif challenge_type == "amazon":
                    result = await self._solve_amazon(tab, url)
                else:
                    result = await self._solve_generic(tab, url)

                self._last_used = time.monotonic()

                # Navigate away to clean state
                try:
                    await tab.go_to("about:blank", timeout=5)
                except Exception:
                    pass

                # [#3] Attach recommended impersonate value for curl_cffi replay
                if result and "error" not in result:
                    result["impersonate"] = DEFAULT_IMPERSONATE

                return result
            except Exception:
                logger.exception(f"Challenge solve failed for {url} (type={challenge_type})")
                # Try to recover browser state; if that fails, Chrome is dead
                try:
                    await tab.go_to("about:blank", timeout=5)
                except Exception:
                    _log("Chrome unresponsive after solve failure, killing")
                    await self._stop_browser()
                return None

    async def with_page(self, url: str, callback, wait: float = 5, timeout: float = 20):
        """Navigate Chrome to a URL, wait for JS, then call callback(tab).

        The callback receives the PyDoll tab and can do multiple round-trips
        (execute scripts, get cookies, make fetch() calls) while Chrome is
        still on the page.

        Args:
            url: URL to load in Chrome.
            callback: Async function taking (tab) and returning any value.
            wait: Seconds to wait after load for SPA rendering.
            timeout: Page load timeout in seconds.

        Returns:
            Whatever callback returns, or None if Chrome busy/failed.
        """
        if self._lock.locked():
            _log("with_page: Chrome busy, skipping")
            return None

        async with self._lock:
            try:
                tab = await self._ensure_browser()
                if tab is None:
                    return None

                # SSRF check
                try:
                    parsed = urlparse(url)
                    from .security.ssrf import is_private_host
                    if await is_private_host(parsed.hostname or ""):
                        return None
                except Exception:
                    return None

                _log(f"Loading page in Chrome: {_sanitize_url(url)}")
                try:
                    await tab.go_to(url, timeout=int(timeout))
                except Exception:
                    logger.exception("Chrome page load failed")
                    try:
                        await tab.go_to("about:blank", timeout=5)
                    except Exception:
                        _log("Chrome unresponsive after page load failure, killing")
                        await self._stop_browser()
                    return None

                # Wait for SPA rendering
                await asyncio.sleep(wait)

                # Call user callback with the live tab
                result = await callback(tab)

                self._last_used = time.monotonic()

                # Navigate away to clean state
                try:
                    await tab.go_to("about:blank", timeout=5)
                except Exception:
                    pass

                return result
            except Exception:
                logger.exception(f"with_page failed for {url}")
                try:
                    if tab is not None:
                        await tab.go_to("about:blank", timeout=5)
                except Exception:
                    _log("Chrome unresponsive after with_page failure, killing")
                    await self._stop_browser()
                return None

    async def _ensure_browser(self):
        """Start or reuse persistent Chrome instance.

        Runs Chrome in headful mode (not headless) because Cloudflare detects
        headless browsers. Uses Xvfb virtual display in Docker/CI where no
        real display exists, or moves the window offscreen on desktop.
        """
        # Check idle timeout
        if self._browser is not None and self._last_used > 0:
            idle = time.monotonic() - self._last_used
            if idle > self._idle_timeout:
                _log(f"Chrome idle for {idle:.0f}s, shutting down")
                await self._stop_browser()

        if self._browser is not None:
            _log("Reusing existing Chrome instance")

        if self._browser is None:
            _log("Starting new Chrome (previous instance: none or killed)")
            try:
                import os
                import shutil
                import subprocess

                try:
                    from pydoll.browser.chromium import Chrome
                    from pydoll.browser.options import ChromiumOptions
                except ImportError:
                    # [#14/#15] PyDoll not installed — clear error message
                    _log("Chrome automation unavailable: pydoll-python not installed. "
                         "Install with: pip install 'fetchaller-mcp[botfighter]'")
                    return None

                options = ChromiumOptions()
                options.add_argument("--disable-blink-features=AutomationControlled")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--disable-gpu")

                # PyDoll looks for google-chrome but Debian installs chromium
                if shutil.which("chromium") and not shutil.which("google-chrome"):
                    options.binary_location = shutil.which("chromium")

                # CF detects headless Chrome — must run headful with Xvfb
                # (virtual display) so the browser thinks it has a screen.
                # Xvfb: Docker has it installed. macOS: brew install xquartz.
                # Fallback: offscreen window position (works on macOS without Xvfb).
                has_display = os.environ.get("DISPLAY")
                has_xvfb = shutil.which("Xvfb")

                if has_xvfb and not has_display:
                    # [#5] Track original DISPLAY for cleanup
                    self._original_display = os.environ.get("DISPLAY")
                    # Use PID-based display number to avoid collision with other Xvfb instances
                    display_num = 99 + (os.getpid() % 100)
                    display_str = f":{display_num}"
                    # Start Xvfb virtual display
                    self._xvfb = subprocess.Popen(
                        ["Xvfb", display_str, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    # [#22] Verify Xvfb started
                    await asyncio.sleep(0.2)
                    if self._xvfb.poll() is not None:
                        _log(f"Xvfb failed to start (exit code {self._xvfb.returncode})")
                        self._xvfb = None
                        # Try headless as fallback
                        _log("WARNING: Xvfb failed — using headless (CF may block)")
                        options.add_argument("--headless=new")
                    else:
                        # Set DISPLAY so Chrome (spawned by PyDoll) uses our Xvfb.
                        # Restored in _stop_browser when Chrome shuts down.
                        os.environ["DISPLAY"] = display_str
                        _log(f"Started Xvfb on {display_str}")
                elif not has_display and sys.platform == "darwin":
                    # macOS without Xvfb — offscreen window (still headful)
                    options.add_argument("--window-position=-9999,-9999")
                elif not has_display:
                    # No display, no Xvfb, not macOS — headless as last resort
                    _log("WARNING: No display and no Xvfb — using headless (CF may block)")
                    options.add_argument("--headless=new")

                options.add_argument("--window-size=1920,1080")

                self._browser = Chrome(options=options)
                self._tab = await self._browser.start()
                _log("Chrome started")
            except Exception:
                logger.exception("Failed to start Chrome")
                # Kill the partially-started browser process if it exists
                if self._browser is not None:
                    try:
                        await self._browser.stop()
                    except Exception:
                        pass
                    # Force-kill if stop() didn't work
                    try:
                        proc = self._browser._browser_process_manager._process
                        if proc and proc.poll() is None:
                            proc.kill()
                            proc.wait(timeout=5)
                    except Exception:
                        pass
                self._browser = None
                self._tab = None
                return None

        return self._tab

    async def _stop_browser(self) -> None:
        """Stop Chrome and clean up (including Xvfb if started).

        PyDoll's stop() raises BrowserNotRunning if the CDP connection is dead,
        but the Chrome *process* may still be alive. We always force-kill the
        process as a fallback to prevent orphan Chrome zombies.
        """
        import os

        if self._browser is not None:
            # Try graceful shutdown first
            try:
                await self._browser.stop()
            except Exception:
                pass
            # Force-kill the process even if stop() failed or raised
            # BrowserNotRunning (CDP dead but process alive = zombie)
            try:
                proc = self._browser._browser_process_manager._process
                if proc and proc.poll() is None:
                    _log(f"Force-killing Chrome process pid={proc.pid}")
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                pass
            self._browser = None
            self._tab = None
            _log("Chrome stopped")
        if self._xvfb is not None:
            self._xvfb.terminate()
            # [#17] Wait for Xvfb to avoid zombie process
            try:
                self._xvfb.wait(timeout=5)
            except Exception:
                try:
                    self._xvfb.kill()
                    self._xvfb.wait()
                except Exception:
                    pass
            self._xvfb = None
            # [#5] Restore original DISPLAY value
            if self._original_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = self._original_display
            self._original_display = None
            _log("Xvfb stopped")

    async def _extract_result(self, tab) -> dict | None:
        """Extract all cookies, UA, and final URL from current tab."""
        try:
            cookies = await tab.get_cookies()
            ua_response = await tab.execute_script(
                "return navigator.userAgent",
                return_by_value=True,
            )
            user_agent = ua_response.get("result", {}).get("result", {}).get("value", "")
            final_url = await tab.current_url
            return {"cookies": cookies, "user_agent": user_agent, "final_url": final_url}
        except Exception:
            logger.exception("Failed to extract cookies/UA")
            return None

    async def _solve_tmd(self, tab, url: str, timeout: float = 20) -> dict | None:
        """Solve Alibaba Cloud WAF TMD challenge via session warming.

        TMD blocks requests without valid session cookies. Unlike CF/Akamai
        where you visit the blocked URL and extract cookies, TMD requires
        visiting the *homepage* first to establish a session, then cookies
        work for all subpages and API endpoints.
        """
        # Derive homepage from the blocked URL
        parsed = urlparse(url)
        homepage = f"{parsed.scheme}://{parsed.hostname}/"
        _log(f"Solving TMD challenge via session warming: {homepage}")

        try:
            await tab.go_to(homepage, timeout=int(timeout))
        except Exception:
            logger.exception("TMD session warming: homepage load failed")
            return None

        # Wait for SPA initialization and cookie setting
        await asyncio.sleep(5)

        return await self._extract_result(tab)

    async def _solve_cloudflare(self, tab, url: str, timeout: float = 30) -> dict | None:
        """Solve Cloudflare challenge using PyDoll's built-in Turnstile bypass.

        Runs the Turnstile bypass as a cancellable task while polling for
        cf_clearance concurrently. JS-only challenges (majority) resolve in
        ~5s without needing the Turnstile bypass; cancelling early avoids
        a 30s timeout waiting for a checkbox that doesn't exist.
        """
        _log(f"Solving Cloudflare challenge for {_sanitize_url(url)}")

        async def _run_bypass():
            async with tab.expect_and_bypass_cloudflare_captcha(time_to_wait_captcha=timeout):
                await tab.go_to(url, timeout=int(timeout))

        # Run Turnstile bypass in background (cancellable)
        bypass_task = asyncio.create_task(_run_bypass())

        # Poll for cf_clearance concurrently — cancel bypass when found
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bypass_task.done():
                break
            try:
                cookies = await tab.get_cookies()
                if any(c.get("name") == "cf_clearance" for c in cookies):
                    _log("cf_clearance cookie obtained")
                    bypass_task.cancel()
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        # Clean up bypass task
        if not bypass_task.done():
            bypass_task.cancel()
        try:
            await bypass_task
        except (asyncio.CancelledError, Exception):
            pass

        return await self._extract_result(tab)

    async def _solve_akamai(self, tab, url: str, timeout: float = 15) -> dict | None:
        """Solve Akamai challenge by loading page and polling for valid _abck cookie.

        Because Akamai binds _abck cookies to TLS fingerprint, cookie replay
        via curl_cffi often fails. As a fallback, we extract the fully rendered
        page HTML from Chrome's DOM so the caller can use it directly without
        needing cookie replay.
        """
        _log(f"Solving Akamai challenge for {_sanitize_url(url)}")
        try:
            await tab.go_to(url, timeout=int(timeout))
        except Exception:
            logger.exception("Akamai page load failed")
            return await self._extract_result(tab)

        # Poll for valid _abck cookie (non-~ prefix)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cookies = await tab.get_cookies()
            abck = next((c for c in cookies if c.get("name") == "_abck"), None)
            if abck and not str(abck.get("value", "")).startswith("~"):
                _log("Valid _abck cookie detected")
                break
            await asyncio.sleep(1)

        # Wait for page content to render (SPA pages need JS execution time)
        await asyncio.sleep(2)

        result = await self._extract_result(tab)
        if result is None:
            return None

        # Extract rendered HTML from Chrome DOM — Akamai cookie replay often
        # fails due to TLS fingerprint binding, so we grab the page content
        # while Chrome still has the solved session active.
        try:
            html_response = await tab.execute_script(
                "return document.documentElement.outerHTML",
                return_by_value=True,
            )
            html = html_response.get("result", {}).get("result", {}).get("value", "")
            if html and len(html) > 1000:
                result["html"] = html
                _log(f"Extracted {len(html):,} chars of rendered HTML from Chrome")
        except Exception:
            logger.exception("Failed to extract HTML from Chrome DOM")

        return result

    async def _solve_amazon(self, tab, url: str, timeout: float = 15) -> dict | None:
        """Solve Amazon rate-limit by clicking 'Continue shopping' and waiting for the real page."""
        _log(f"Solving Amazon captcha for {_sanitize_url(url)}")
        try:
            await tab.go_to(url, timeout=int(timeout))
        except Exception:
            logger.exception("Amazon page load failed")
            return await self._extract_result(tab)

        # Wait for the page to settle
        await asyncio.sleep(2)

        # Try to click "Continue shopping" link/button
        try:
            # Amazon's continue page has an <a> or <input> with "Continue shopping"
            clicked = await tab.execute_script("""
                // Try <a> links first
                for (const a of document.querySelectorAll('a')) {
                    if (a.textContent.trim().toLowerCase().includes('continue shopping')) {
                        a.click();
                        return 'clicked_link';
                    }
                }
                // Try <input type="submit"> buttons
                for (const btn of document.querySelectorAll('input[type="submit"], button')) {
                    if ((btn.value || btn.textContent || '').toLowerCase().includes('continue')) {
                        btn.click();
                        return 'clicked_button';
                    }
                }
                // Try form submission directly
                const form = document.querySelector('form');
                if (form) {
                    form.submit();
                    return 'submitted_form';
                }
                return 'nothing_found';
            """, return_by_value=True)
            action = clicked.get("result", {}).get("result", {}).get("value", "unknown")
            _log(f"Amazon captcha action: {action}")
        except Exception:
            logger.exception("Failed to click Amazon continue button")

        # Wait for navigation to complete and real page to load
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            try:
                # Check if we've navigated to the real page (large HTML)
                page_len = await tab.execute_script(
                    "return document.documentElement.outerHTML.length",
                    return_by_value=True,
                )
                html_size = page_len.get("result", {}).get("result", {}).get("value", 0)
                if html_size > 100_000:
                    _log(f"Amazon page loaded ({html_size:,} chars)")
                    break
                # Also check if "continue shopping" is no longer on the page
                has_continue = await tab.execute_script(
                    "return document.body.textContent.toLowerCase().includes('continue shopping')",
                    return_by_value=True,
                )
                if not has_continue.get("result", {}).get("result", {}).get("value", True):
                    _log("Amazon captcha page cleared")
                    break
            except Exception:
                pass

        return await self._extract_result(tab)

    async def _solve_generic(self, tab, url: str, timeout: float = 10) -> dict | None:
        """Solve generic challenge by loading page and waiting for network idle."""
        _log(f"Solving generic challenge for {_sanitize_url(url)}")
        try:
            await tab.go_to(url, timeout=int(timeout))
        except Exception:
            logger.exception("Generic page load failed")

        # Wait for network idle (challenge JS to finish)
        await asyncio.sleep(min(3, timeout))
        return await self._extract_result(tab)

    async def close(self) -> None:
        """Shut down Chrome."""
        await self._stop_browser()


def _sanitize_url(url: str) -> str:
    """Strip query params from URL for safe logging."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
