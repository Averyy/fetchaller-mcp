"""MTop API client for AliExpress.

Handles token bootstrap, request signing, and automatic token refresh.
Uses wafer for HTTP transport with automatic TLS fingerprinting.

wafer exclusively owns browser solving and cookie storage. This module reads
the MTop application token through wafer's scoped ``get_cookie`` API because
the token is required to compute the documented request signature; it never
extracts browser cookies or injects cookies into a session.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import wafer

from ..config import get_wafer_cache_dir
from ..security.xss import safe_log_text

_ALIEXPRESS_TMD_API_RE = re.compile(r"^mtop\.aliexpress(?:\.[A-Za-z0-9_-]+)+$")
_ALIEXPRESS_TMD_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_TMD_PUNISH_SUFFIX = "/_____tmd_____/punish"


def _log(msg: str) -> None:
    print(
        f"[{datetime.now(UTC).isoformat()}] mtop: {safe_log_text(msg)}",
        file=sys.stderr,
    )


def compute_sign(token: str, timestamp: str, app_key: str, data_str: str) -> str:
    """Compute MTop request signature.

    sign = MD5(token & timestamp & appKey & data_json)
    """
    sign_input = f"{token}&{timestamp}&{app_key}&{data_str}"
    return hashlib.md5(sign_input.encode("utf-8")).hexdigest()


def _parse_token_cookie(value: str | None) -> str | None:
    """Return the usable MTop token from ``<token>_<timestamp>``."""

    if not isinstance(value, str):
        return None
    matched = re.fullmatch(r"([A-Za-z0-9]{1,128})_([0-9]{10,16})", value)
    if matched is None:
        return None
    return matched.group(1)


def _ret_text(result: dict) -> str:
    """Return only trusted string ret values for error classification."""

    ret = result.get("ret")
    if isinstance(ret, str):
        return ret
    if isinstance(ret, list):
        return " ".join(value for value in ret if isinstance(value, str))
    return ""


def _issued_tmd_challenge_url(result: dict) -> str | None:
    """Return a narrowly validated MTop-issued TMD punishment URL.

    ``FAIL_SYS_USER_VALIDATE`` includes the one-time browser URL for the
    *specific* failed request.  Solving a generic storefront page cannot
    complete that dialog.  Treat this server-provided value as untrusted until
    its scheme, exact host, path, and required x5sec token are all validated.
    """

    data = result.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("url")
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    normalized_path = "/" + parsed.path.lstrip("/")
    candidate_path = normalized_path.rstrip("/")
    path_valid = candidate_path == _TMD_PUNISH_SUFFIX
    if not path_valid and candidate_path.endswith(_TMD_PUNISH_SUFFIX):
        prefix = candidate_path[: -len(_TMD_PUNISH_SUFFIX)]
        parts = prefix.split("/")
        path_valid = (
            len(parts) == 4
            and not parts[0]
            and parts[1] == "h5"
            and _ALIEXPRESS_TMD_API_RE.fullmatch(parts[2]) is not None
            and _ALIEXPRESS_TMD_VERSION_RE.fullmatch(parts[3]) is not None
        )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "acs.aliexpress.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not path_valid
    ):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    x5secdata = query.get("x5secdata")
    if not x5secdata or len(x5secdata) != 1 or not x5secdata[0] or len(x5secdata[0]) > 4096:
        return None
    return value


class MTopClient:
    """AliExpress MTop API client with automatic token management."""

    BASE_URL = "https://acs.aliexpress.com"
    APP_KEY = "12574478"
    TOKEN_TTL = 3600  # ~60 min

    def __init__(self, browser_solver=None) -> None:
        self._session: wafer.AsyncSession | None = None
        self._token: str = ""
        # Keep the full cookie value as well as its signing-token portion.
        # The timestamp suffix distinguishes a refreshed cookie with the same
        # token value from the previous token lifecycle.
        self._token_cookie: str | None = None
        self._token_time: float = 0.0
        self._token_generation = 0
        self._bootstrap_lock = asyncio.Lock()
        self._browser_prime_lock = asyncio.Lock()
        self._browser_prime_generation = 0
        # Issued TMD URLs are single-use browser interactions.  Serialize
        # them separately from generic origin priming: concurrent MTop calls
        # must share the clearance earned by the first solve, not queue more
        # long-running tasks behind the solver's single browser worker.
        self._tmd_recovery_lock = asyncio.Lock()
        self._tmd_recovery_generation = 0
        self._browser_solver = browser_solver

    @property
    def browser_solver(self):
        """The browser solver instance, or None."""
        return self._browser_solver

    async def _get_session(self) -> wafer.AsyncSession:
        if self._session is None:
            self._session = wafer.AsyncSession(
                max_rotations=0,
                cache_dir=get_wafer_cache_dir(),
                browser_solver=self._browser_solver,
                solve_origin="https://www.aliexpress.com/",
                max_response_size=5 * 1024 * 1024,
            )
        return self._session

    def _token_expired(self) -> bool:
        if not self._token:
            return True
        return (time.time() - self._token_time) > self.TOKEN_TTL

    @staticmethod
    def _remaining(deadline: float | None, maximum: float) -> float:
        """Return a downstream timeout constrained by the operation deadline."""

        if deadline is None:
            return maximum
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("AliExpress product deadline exhausted")
        return min(maximum, remaining)

    @asynccontextmanager
    async def _deadline_lock(self, lock: asyncio.Lock, deadline: float | None):
        """Acquire a shared recovery lock without extending this request."""

        if deadline is None:
            await lock.acquire()
        else:
            timeout = self._remaining(deadline, float("inf"))
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
            except TimeoutError as exc:
                raise TimeoutError("AliExpress product deadline exhausted waiting for recovery") from exc
        try:
            yield
        finally:
            lock.release()

    def _set_token_cookie(self, token_cookie: str) -> bool:
        """Record a validated token cookie and advance its lifecycle version."""

        token = _parse_token_cookie(token_cookie)
        if token is None:
            return False
        if token == self._token and token_cookie == self._token_cookie:
            return False
        self._token = token
        self._token_cookie = token_cookie
        self._token_time = time.time()
        self._token_generation += 1
        return True

    def _clear_token_if_current(self, expected_generation: int) -> bool:
        """Invalidate only the token that was used by a failed request."""

        if self._token_generation != expected_generation:
            return False
        self._token = ""
        self._token_cookie = None
        self._token_time = 0.0
        self._token_generation += 1
        return True

    async def _warm_origin_for_token(
        self, observed_generation: int | None = None, deadline: float | None = None
    ) -> bool:
        """Deliberately prime the HTML origin and import its scoped token."""
        if not self._browser_solver:
            return False

        async with self._deadline_lock(self._browser_prime_lock, deadline):
            # Another blocked request may already have completed the same
            # recovery while this coroutine waited for the lock.
            if observed_generation is not None and self._token_generation != observed_generation:
                return True

            prime_token_generation = self._token_generation
            try:
                session = await self._get_session()
                _log("priming AliExpress origin through wafer browser")
                primed = await session.browser_prime(
                    "https://www.aliexpress.com/",
                    timeout=self._remaining(deadline, 30),
                    max_response_size=2 * 1024 * 1024,
                )
                if not primed:
                    if self._token_generation != prime_token_generation:
                        return True
                    _log("origin browser prime did not earn usable state")
                    return False

                token_cookie = session.get_cookie(
                    "_m_h5_tk",
                    "https://acs.aliexpress.com/",
                )
                token = _parse_token_cookie(token_cookie)
                if token is None:
                    if self._token_generation != prime_token_generation:
                        return True
                    _log("origin browser prime yielded no valid _m_h5_tk cookie")
                    return False
            except Exception as exc:
                if self._token_generation != prime_token_generation:
                    return True
                _log(f"origin browser prime failed ({type(exc).__name__})")
                return False
            # A request that completed while the browser was open may have
            # already installed a newer token. Never overwrite it with this
            # delayed prime result.
            if self._token_generation != prime_token_generation:
                return True
            self._set_token_cookie(token_cookie)
            self._browser_prime_generation += 1
            return True

    async def _solve_issued_tmd_challenge(
        self,
        challenge_url: str,
        observed_generation: int,
        deadline: float | None = None,
    ) -> bool:
        """Solve one issued TMD dialog; concurrent callers share its result."""

        async with self._deadline_lock(self._tmd_recovery_lock, deadline):
            if self._tmd_recovery_generation != observed_generation:
                return True
            action = parse_qs(
                urlsplit(challenge_url).query,
                keep_blank_values=True,
            ).get("action")
            challenge_kind = "recaptcha" if action == ["captcharecaptcha"] else "baxia" if not action else "other"
            _log(f"solving issued TMD challenge kind={challenge_kind}")
            token_generation_before_solve = self._token_generation
            try:
                session = await self._get_session()
                solved = await session.browser_solve_challenge(
                    challenge_url,
                    "tmd",
                    # A live Enterprise challenge can present several image
                    # rounds after the delayed wrapper and checkbox. Recorded
                    # human paths pushed a real four-round solve beyond the
                    # old 60s ceiling. Allow one bounded 165s attempt, always
                    # clamped to the product operation's single end-to-end
                    # deadline.
                    timeout=self._remaining(deadline, 165),
                    max_response_size=2 * 1024 * 1024,
                )
            except Exception as exc:
                _log(f"issued TMD challenge solve failed ({type(exc).__name__})")
                return False
            if solved:
                # A completed TMD wrapper may have minted the signing token
                # along with x5sec.  Import it only if no concurrent request
                # installed a newer lifecycle while the browser was solving.
                try:
                    token_cookie = session.get_cookie("_m_h5_tk", "https://acs.aliexpress.com/")
                    if (
                        self._token_generation == token_generation_before_solve
                        and _parse_token_cookie(token_cookie) is not None
                    ):
                        self._set_token_cookie(token_cookie)
                except Exception as exc:
                    _log(f"issued TMD token import failed ({type(exc).__name__})")
                self._tmd_recovery_generation += 1
            return bool(solved)

    async def _bootstrap_token(self, deadline: float | None = None) -> None:
        """Bootstrap a token by making a request with token="undefined".

        The server returns FAIL_SYS_TOKEN_EMPTY and sets _m_h5_tk cookie.
        We use a real API endpoint (not a dedicated bootstrap URL) because
        AliExpress only sets the token cookie on actual API requests.

        If the API blocks with x5sec, the subsequent signed request supplies
        the exact issued TMD URL for browser recovery.  A generic homepage
        visit cannot solve that one-time dialog and must not consume a second
        full browser timeout.
        """
        async with self._deadline_lock(self._bootstrap_lock, deadline):
            # Double-check after acquiring lock (another coroutine may have bootstrapped)
            if not self._token_expired():
                return

            session = await self._get_session()
            bootstrap_generation = self._token_generation
            timestamp = str(int(time.time() * 1000))
            data_str = "{}"
            sign = compute_sign("undefined", timestamp, self.APP_KEY, data_str)

            # Use a real API endpoint — AliExpress only sets _m_h5_tk on actual API calls
            url = f"{self.BASE_URL}/h5/mtop.aliexpress.pdp.pc.query/1.0/"
            params = {
                "jsv": "2.5.1",
                "appKey": self.APP_KEY,
                "t": timestamp,
                "sign": sign,
                "api": "mtop.aliexpress.pdp.pc.query",
                "v": "1.0",
                "timeout": "5000",
                "type": "originaljson",
                "dataType": "json",
                "data": data_str,
            }

            headers = {"Referer": "https://www.aliexpress.com/"}
            await session.get(
                url,
                params=params,
                headers=headers,
                timeout=self._remaining(deadline, 10),
            )

            # Extract _m_h5_tk from the response's cookies. Keyed by exact name,
            # so no _m_h5_tk_enc collision; value is "<token>_<timestamp>".
            tk = session.get_cookie("_m_h5_tk", url)
            if _parse_token_cookie(tk) is not None:
                # Do not replace state that was refreshed while this bootstrap
                # request was in flight.
                if self._token_generation == bootstrap_generation:
                    self._set_token_cookie(tk)
            else:
                if self._token_generation != bootstrap_generation:
                    return
                _log("token bootstrap failed: no _m_h5_tk cookie received; awaiting issued MTop challenge")

    async def request(
        self,
        api_name: str,
        version: str,
        data_dict: dict,
        deadline: float | None = None,
    ) -> dict:
        """Make an MTop API request.

        Handles token bootstrap and auto-refresh on expiry.

        Returns:
            Parsed JSON response dict. On success, contains ``data.result``.
            On error, contains ``ret`` with error codes.
        """
        if self._token_expired():
            await self._bootstrap_token(deadline)

        # Capture the token lifecycle before an in-flight request. A response
        # can arrive after another request refreshed the shared session state.
        # Its error must not invalidate that newer token or re-prime the browser.
        token_generation = self._token_generation
        result = await self._request_once(api_name, version, data_dict, deadline)

        # Check for token expiry (note: AliExpress has a typo "EXOIRED")
        ret_str = _ret_text(result)

        if "FAIL_SYS_TOKEN_EXOIRED" in ret_str or "FAIL_SYS_TOKEN_EMPTY" in ret_str:
            _log("token expired, re-bootstrapping")
            self._clear_token_if_current(token_generation)
            await self._bootstrap_token(deadline)
            token_generation = self._token_generation
            result = await self._request_once(api_name, version, data_dict, deadline)

        # x5sec block: MTop returns the exact, one-time TMD punishment URL
        # inside its otherwise-200 JSON response.  A generic origin visit
        # cannot complete that issued dialog, so solve the validated URL and
        # let wafer import only its verified clearance state before retrying.
        ret_str = _ret_text(result)
        if "FAIL_SYS_USER_VALIDATE" in ret_str or "RGV587_ERROR" in ret_str:
            challenge_url = _issued_tmd_challenge_url(result)
            if challenge_url:
                _log("x5sec blocked MTop request, solving issued TMD challenge")
                solved = await self._solve_issued_tmd_challenge(
                    challenge_url,
                    self._tmd_recovery_generation,
                    deadline,
                )
            else:
                _log("x5sec block omitted usable challenge URL, priming origin")
                solved = await self._warm_origin_for_token(token_generation, deadline)
            if solved:
                # A browser solve proves only that its own state completed.
                # MTop's native retry must be signed with a real token.  Read
                # a browser-minted _m_h5_tk first; if TMD minted only x5sec,
                # bootstrap once under that newly earned clearance.  Do not
                # replay an unsigned request when bootstrap still fails.
                if self._token_expired():
                    await self._bootstrap_token(deadline)
                if self._token_expired():
                    _log("TMD clearance yielded no usable MTop signing token")
                    return result
                result = await self._request_once(api_name, version, data_dict, deadline)

        return result

    async def _request_once(
        self,
        api_name: str,
        version: str,
        data_dict: dict,
        deadline: float | None,
    ) -> dict:
        """Preserve the three-argument seam when no deadline is requested."""

        if deadline is None:
            return await self._do_request(api_name, version, data_dict)
        return await self._do_request(api_name, version, data_dict, deadline=deadline)

    async def _do_request(
        self,
        api_name: str,
        version: str,
        data_dict: dict,
        deadline: float | None = None,
    ) -> dict:
        """Execute a single MTop request (no retry logic)."""
        session = await self._get_session()
        token = self._token
        token_generation = self._token_generation
        timestamp = str(int(time.time() * 1000))
        data_str = json.dumps(data_dict, separators=(",", ":"))
        sign = compute_sign(token, timestamp, self.APP_KEY, data_str)

        # API name goes in the URL path with dots preserved (NOT converted to slashes).
        # https://acs.aliexpress.com/h5/mtop.aliexpress.pdp.pc.query/1.0/
        url = f"{self.BASE_URL}/h5/{api_name}/{version}/"
        params = {
            "jsv": "2.5.1",
            "appKey": self.APP_KEY,
            "t": timestamp,
            "sign": sign,
            "api": api_name,
            "v": version,
            "timeout": "5000",
            "type": "originaljson",
            "dataType": "json",
            "data": data_str,
        }

        headers = {"Referer": "https://www.aliexpress.com/"}
        if os.environ.get("WAFER_TMD_DIAGNOSTICS") == "1":
            try:
                _log(f"MTop request cookie scopes: {session.cookie_scope_summary(url)}")
            except Exception:
                _log("MTop request cookie scope summary unavailable")
        resp = await session.get(
            url,
            params=params,
            headers=headers,
            timeout=self._remaining(deadline, 15),
        )

        # Update token if this response refreshed it
        tk = session.get_cookie("_m_h5_tk", url)
        if _parse_token_cookie(tk) is not None and self._token_generation == token_generation:
            self._set_token_cookie(tk)

        try:
            body = resp.text
            if not isinstance(body, str):
                raise ValueError("response body is not text")
            # Strip JSONP wrapper if present (e.g. "mtopjsonp1({...})")
            jsonp_match = re.match(r"^\s*\w+\(([\s\S]+)\)\s*;?\s*$", body)
            if jsonp_match:
                body = jsonp_match.group(1)
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
            raise ValueError("response JSON root is not an object")
        except Exception:
            return {"ret": ["PARSE_ERROR"], "data": {}}

    async def close(self) -> None:
        """Release the underlying HTTP session."""
        self._session = None
