"""MTop API client for AliExpress.

Handles token bootstrap, request signing, and automatic token refresh.
Uses wafer for HTTP transport with automatic TLS fingerprinting.

Architecture note — why browser_solver is called explicitly here:
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The MTop API (acs.aliexpress.com) returns JSON, not HTML. When AliExpress
blocks a request, the API returns HTTP 200 with a JSON body containing
``FAIL_SYS_USER_VALIDATE`` / ``RGV587_ERROR`` — this is an **auth flow**
(invalid/missing tokens), not a WAF challenge.

wafer's automatic challenge detection is designed for HTML challenge pages
(Cloudflare 403, Akamai, etc). Passing browser_solver to a JSON API session
would cause wafer to detect the ``x5secdata`` cookie, try to browser-solve
the API URL, get a JSON page in the browser (not a challenge), and time out.

The correct approach is for fetchaller to handle this as application-level
auth: when the API says "your tokens are bad", we visit aliexpress.com in a
browser (where page JS makes internal MTop calls that set ``_m_h5_tk``),
extract those cookies, and inject them into the API session. This is domain
logic that belongs in the caller, not in wafer's transport layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime

import wafer

from ..config import get_wafer_cache_dir


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] mtop: {msg}", file=sys.stderr)


def compute_sign(token: str, timestamp: str, app_key: str, data_str: str) -> str:
    """Compute MTop request signature.

    sign = MD5(token & timestamp & appKey & data_json)
    """
    sign_input = f"{token}&{timestamp}&{app_key}&{data_str}"
    return hashlib.md5(sign_input.encode("utf-8")).hexdigest()


class MTopClient:
    """AliExpress MTop API client with automatic token management."""

    BASE_URL = "https://acs.aliexpress.com"
    APP_KEY = "12574478"
    TOKEN_TTL = 3600  # ~60 min

    def __init__(self, browser_solver=None) -> None:
        self._session: wafer.AsyncSession | None = None
        self._token: str = ""
        self._token_time: float = 0.0
        self._bootstrap_lock = asyncio.Lock()
        self._browser_solver = browser_solver

    @property
    def browser_solver(self):
        """The browser solver instance, or None."""
        return self._browser_solver

    async def _get_session(self) -> wafer.AsyncSession:
        if self._session is None:
            # No browser_solver — this session talks to a JSON API, not HTML
            # pages. See module docstring for full explanation.
            self._session = wafer.AsyncSession(
                max_rotations=0,
                cache_dir=get_wafer_cache_dir(),
            )
        return self._session

    def _token_expired(self) -> bool:
        if not self._token:
            return True
        return (time.time() - self._token_time) > self.TOKEN_TTL

    async def _browser_solve_for_token(self) -> bool:
        """Visit aliexpress.com in a browser to obtain _m_h5_tk.

        This is AliExpress-specific auth logic, not a WAF bypass. The MTop
        API requires a signed ``_m_h5_tk`` token that is only set by
        JavaScript on the AliExpress frontend. When the HTTP-only bootstrap
        fails (x5sec blocks it), we launch a real browser so the page JS
        executes, makes its own internal MTop calls, and the resulting
        ``_m_h5_tk`` cookie gets set. We then extract it and inject it into
        our API session.

        Returns True if token was obtained.
        """
        if not self._browser_solver:
            return False

        _log("triggering browser solve for _m_h5_tk token")
        try:
            result = await asyncio.to_thread(
                self._browser_solver.solve,
                "https://www.aliexpress.com/",
            )
        except Exception as e:
            _log(f"browser solve failed: {e}")
            return False

        if not result:
            _log("browser solve returned no result")
            return False

        # Extract _m_h5_tk from browser cookies and inject into session
        cookies = result.cookies or []
        session = await self._get_session()
        ae_url = "https://www.aliexpress.com/"
        for cookie in cookies:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            domain = cookie.get("domain", "")
            if name and value:
                # add_cookie takes a raw Set-Cookie header string
                raw = f"{name}={value}; Domain={domain}; Path=/"
                session.add_cookie(raw, ae_url)
                if name == "_m_h5_tk" and value:
                    self._token = value.split("_")[0]
                    self._token_time = time.time()
                    _log(f"token from browser: {self._token[:8]}...")

        if self._token:
            _log(f"browser solve injected {len(cookies)} cookies")
            return True

        _log("browser solve succeeded but no _m_h5_tk in cookies")
        return False

    async def _bootstrap_token(self) -> None:
        """Bootstrap a token by making a request with token="undefined".

        The server returns FAIL_SYS_TOKEN_EMPTY and sets _m_h5_tk cookie.
        We use a real API endpoint (not a dedicated bootstrap URL) because
        AliExpress only sets the token cookie on actual API requests.

        If the API blocks with x5sec, falls back to browser_solver.
        """
        async with self._bootstrap_lock:
            # Double-check after acquiring lock (another coroutine may have bootstrapped)
            if not self._token_expired():
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

            # Extract _m_h5_tk from response Set-Cookie headers
            token_found = False
            for cookie_val in resp.get_all("set-cookie"):
                if "_m_h5_tk=" in cookie_val and "_m_h5_tk_enc" not in cookie_val:
                    val = cookie_val.split("_m_h5_tk=")[1].split(";")[0]
                    self._token = val.split("_")[0]
                    self._token_time = time.time()
                    _log(f"token bootstrapped: {self._token[:8]}...")
                    token_found = True
                    break

            if not token_found:
                _log("token bootstrap failed: no _m_h5_tk cookie received")
                # Fall back to browser solve — page JS sets _m_h5_tk
                await self._browser_solve_for_token()

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

        # x5sec block — API-level auth rejection, not a WAF challenge.
        # The API returns 200 JSON with these error codes when our session
        # lacks valid tokens. Browser solve gets them (see module docstring).
        ret = result.get("ret", [])
        ret_str = " ".join(ret) if isinstance(ret, list) else str(ret)
        if "FAIL_SYS_USER_VALIDATE" in ret_str or "RGV587_ERROR" in ret_str:
            _log("x5sec blocked MTop request, attempting browser solve")
            if await self._browser_solve_for_token():
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
        resp = await session.get(url, params=params, headers=headers, timeout=15)

        # Update token from response cookies if refreshed
        for cookie_val in resp.get_all("set-cookie"):
            if "_m_h5_tk=" in cookie_val and "_m_h5_tk_enc" not in cookie_val:
                val = cookie_val.split("_m_h5_tk=")[1].split(";")[0]
                new_token = val.split("_")[0]
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
        """Release the underlying HTTP session."""
        self._session = None
