"""MTop API client for AliExpress.

Handles token bootstrap, request signing, and automatic token refresh.
Uses curl_cffi with Chrome impersonation for TLS fingerprint matching.

Integrates with botfighter's cookie cache: when cached cookies exist for
aliexpress.com (from a prior TMD solve), seeds the session with them
instead of bootstrapping from scratch (which would also get TMD-blocked).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import sys
import time
from datetime import UTC, datetime

from curl_cffi.requests import AsyncSession

from ..config import BROWSER_FINGERPRINTS


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] mtop: {msg}", file=sys.stderr)


def compute_sign(token: str, timestamp: str, app_key: str, data_str: str) -> str:
    """Compute MTop request signature.

    sign = MD5(token & timestamp & appKey & data_json)
    """
    sign_input = f"{token}&{timestamp}&{app_key}&{data_str}"
    return hashlib.md5(sign_input.encode("utf-8")).hexdigest()


class MTopClient:
    """AliExpress MTop API client with automatic token management.

    Optionally accepts a cookie_cache and challenge_solver for integration
    with botfighter. When TMD blocks the token bootstrap, triggers a browser
    session warm-up and retries with the resulting cookies.
    """

    BASE_URL = "https://acs.aliexpress.com"
    APP_KEY = "12574478"
    TOKEN_TTL = 3600  # ~60 min

    def __init__(
        self,
        cookie_cache=None,
        challenge_solver=None,
    ) -> None:
        self._session: AsyncSession | None = None
        self._token: str = ""
        self._token_time: float = 0.0
        self._bootstrap_lock = asyncio.Lock()
        self._cookie_cache = cookie_cache
        self._challenge_solver = challenge_solver
        self._seeded: bool = False
        self._cookie_header: str = ""  # Cookie header for cross-subdomain requests
        self._ua: str = ""  # User-Agent from browser session
        self._impersonate: str = random.choice(BROWSER_FINGERPRINTS)

    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate=self._impersonate)
        return self._session

    def _token_expired(self) -> bool:
        if not self._token:
            return True
        return (time.time() - self._token_time) > self.TOKEN_TTL

    async def _seed_from_cache(self) -> bool:
        """Seed session cookies from botfighter's cookie cache.

        Returns True if cookies were found and token extracted.
        """
        if self._seeded or not self._cookie_cache:
            return False

        # Check for cached cookies under aliexpress.com or www.aliexpress.com
        for domain in ("www.aliexpress.com", "aliexpress.com"):
            cached = self._cookie_cache.get(domain)
            if cached:
                cookies = cached.cookies
                ua = cached.user_agent
                impersonate = cached.impersonate

                self._inject_cookies(cookies, ua, impersonate)

                self._seeded = True
                _log(f"session seeded with {len(cookies)} cookies from {domain} cache")
                return bool(self._token)

        return False

    def _inject_cookies(self, cookies: list[dict], ua: str = "", impersonate: str = "") -> None:
        """Inject browser cookies into the MTop session.

        Sets the Cookie header directly because curl_cffi's cookie jar
        doesn't send .aliexpress.com cookies to acs.aliexpress.com subdomain.
        """
        # Build cookie header string
        cookie_parts = []
        for cookie in cookies:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            if name and value:
                cookie_parts.append(f"{name}={value}")

            # Extract _m_h5_tk token
            if name == "_m_h5_tk" and value:
                self._token = value.split("_")[0]
                self._token_time = time.time()
                _log(f"token from cookies: {self._token[:8]}...")

        self._cookie_header = "; ".join(cookie_parts)

        if ua:
            self._ua = ua
        # Pin TLS fingerprint to match the browser that generated the cookies
        if impersonate:
            self._impersonate = impersonate

    async def _trigger_tmd_solve(self) -> bool:
        """Trigger a TMD solve via botfighter and seed session from result.

        Returns True if solve succeeded and session was seeded.
        """
        if not self._challenge_solver:
            return False

        _log("triggering TMD solve via botfighter")
        solve_result = await self._challenge_solver.solve(
            "https://www.aliexpress.com/", "tmd"
        )

        if not solve_result or "error" in solve_result:
            error = solve_result.get("error", "unknown") if solve_result else "no result"
            _log(f"TMD solve failed: {error}")
            return False

        # Cache the cookies for future use
        cookies = solve_result.get("cookies", [])
        ua = solve_result.get("user_agent", "")
        impersonate = solve_result.get("impersonate", "chrome")

        if self._cookie_cache:
            self._cookie_cache.set(
                "www.aliexpress.com", "tmd", cookies, ua, impersonate,
            )

        self._inject_cookies(cookies, ua, impersonate)

        self._seeded = True
        _log(f"session seeded with {len(cookies)} cookies from TMD solve")
        return bool(self._token)

    async def _bootstrap_token(self) -> None:
        """Bootstrap a token by making a request with token="undefined".

        The server returns FAIL_SYS_TOKEN_EMPTY and sets _m_h5_tk cookie.
        We use a real API endpoint (not a dedicated bootstrap URL) because
        AliExpress only sets the token cookie on actual API requests.
        """
        async with self._bootstrap_lock:
            # Double-check after acquiring lock (another coroutine may have bootstrapped)
            if not self._token_expired():
                return

            # Try seeding from botfighter cache first
            if await self._seed_from_cache():
                return

            session = await self._get_session()
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
            resp = await session.get(url, params=params, headers=headers, timeout=10)

            # Extract _m_h5_tk from response Set-Cookie headers first (most reliable),
            # then fall back to session cookies
            token_found = False
            for header_name, header_val in resp.headers.multi_items():
                if header_name.lower() == "set-cookie" and "_m_h5_tk=" in header_val and "_m_h5_tk_enc" not in header_val:
                    val = header_val.split("_m_h5_tk=")[1].split(";")[0]
                    self._token = val.split("_")[0]
                    self._token_time = time.time()
                    _log(f"token bootstrapped: {self._token[:8]}...")
                    token_found = True
                    break

            if not token_found:
                for cookie in session.cookies.jar:
                    if cookie.name == "_m_h5_tk":
                        self._token = cookie.value.split("_")[0]
                        self._token_time = time.time()
                        _log(f"token bootstrapped (cookie jar): {self._token[:8]}...")
                        token_found = True
                        break

            if not token_found:
                _log("token bootstrap failed: no _m_h5_tk cookie received")
                # Last resort: trigger TMD solve to get cookies
                await self._trigger_tmd_solve()

    async def request(
        self,
        api_name: str,
        version: str,
        data_dict: dict,
    ) -> dict:
        """Make an MTop API request.

        Handles token bootstrap and auto-refresh on expiry.

        Returns:
            Parsed JSON response dict. On success, contains ``data.result``.
            On error, contains ``ret`` with error codes.
        """
        if self._token_expired():
            await self._bootstrap_token()

        result = await self._do_request(api_name, version, data_dict)

        # Check for token expiry (note: AliExpress has a typo "EXOIRED")
        ret = result.get("ret", [])
        if isinstance(ret, list):
            ret_str = " ".join(ret)
        else:
            ret_str = str(ret)

        if "FAIL_SYS_TOKEN_EXOIRED" in ret_str or "FAIL_SYS_TOKEN_EMPTY" in ret_str:
            _log("token expired, re-bootstrapping")
            self._token = ""
            await self._bootstrap_token()
            result = await self._do_request(api_name, version, data_dict)

        # TMD block — trigger solve and retry once
        ret = result.get("ret", [])
        ret_str = " ".join(ret) if isinstance(ret, list) else str(ret)
        if "FAIL_SYS_USER_VALIDATE" in ret_str or "RGV587_ERROR" in ret_str:
            _log("TMD blocked MTop request, attempting TMD solve")
            if await self._trigger_tmd_solve():
                # Re-sign and retry with new token
                result = await self._do_request(api_name, version, data_dict)

        return result

    async def _do_request(
        self,
        api_name: str,
        version: str,
        data_dict: dict,
    ) -> dict:
        """Execute a single MTop request (no retry logic)."""
        session = await self._get_session()
        timestamp = str(int(time.time() * 1000))
        data_str = json.dumps(data_dict, separators=(",", ":"))
        sign = compute_sign(self._token, timestamp, self.APP_KEY, data_str)

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
        # Inject cookies via header (curl_cffi cookie jar doesn't send
        # .aliexpress.com cookies to acs.aliexpress.com subdomain)
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        if self._ua:
            headers["User-Agent"] = self._ua
        resp = await session.get(url, params=params, headers=headers, timeout=15)

        # Update token from response cookies if refreshed
        for cookie in session.cookies.jar:
            if cookie.name == "_m_h5_tk":
                new_token = cookie.value.split("_")[0]
                if new_token != self._token:
                    self._token = new_token
                    self._token_time = time.time()

        try:
            body = resp.text
            # Strip JSONP wrapper if present (e.g. "mtopjsonp1({...})")
            jsonp_match = re.match(r"^\s*\w+\(([\s\S]+)\)\s*;?\s*$", body)
            if jsonp_match:
                body = jsonp_match.group(1)
            return json.loads(body)
        except Exception:
            return {"ret": ["PARSE_ERROR"], "data": {}}

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
