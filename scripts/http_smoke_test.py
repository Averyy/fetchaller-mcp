"""Boot/readiness/MCP protocol smoke test for the HTTP deployment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from fetchaller import __version__

if __package__:
    from scripts.smoke_test import EXPECTED_TOOLS, run_live_tool_suite
else:
    from smoke_test import EXPECTED_TOOLS, run_live_tool_suite

_OAUTH_REDIRECT_URI = "http://localhost:8765/callback"
_OAUTH_SCOPE = "fetchaller:read"
_PKCE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_PKCE_CHALLENGE = (
    "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
)


async def _wait_for_health(base_url: str, wait_seconds: float) -> dict:
    deadline = time.monotonic() + wait_seconds
    last_error = "server did not respond"
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{base_url}/health")
                payload = response.json()
                if response.status_code == 200 and payload.get("status") == "healthy":
                    return payload
                last_error = f"HTTP {response.status_code}: {payload}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1)
    raise RuntimeError(f"health gate failed after {wait_seconds:g}s: {last_error}")


def _write_oauth_state(state_path: Path, state: dict[str, str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, state_path)
        os.chmod(state_path, 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _read_oauth_state(state_path: Path) -> dict[str, str]:
    payload = json.loads(state_path.read_text())
    if not isinstance(payload, dict):
        raise RuntimeError("OAuth smoke state is not an object")
    result: dict[str, str] = {}
    for name in ("client_id", "refresh_token"):
        value = payload.get(name)
        if value is not None:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 8192
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
            ):
                raise RuntimeError(f"OAuth smoke state has invalid {name}")
            result[name] = value
    return result


async def _verify_mcp_bearer(base_url: str, bearer: str) -> None:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=httpx.Timeout(10, read=30),
    ) as client:
        async with streamable_http_client(
            f"{base_url}/mcp",
            http_client=client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
    if initialized.serverInfo.name != "fetchaller":
        raise RuntimeError("OAuth access token failed MCP initialization")
    if [tool.name for tool in listed.tools] != EXPECTED_TOOLS:
        raise RuntimeError("OAuth access token returned the wrong MCP tool surface")


async def _pair_oauth_client(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    client_id: str,
) -> dict[str, str]:
    authorize_params = {
        "client_id": client_id,
        "redirect_uri": _OAUTH_REDIRECT_URI,
        "response_type": "code",
        "state": "container-smoke",
        "code_challenge": _PKCE_CHALLENGE,
        "code_challenge_method": "S256",
        "scope": _OAUTH_SCOPE,
        "resource": base_url,
    }
    authorize = await client.get(
        f"{base_url}/authorize",
        params=authorize_params,
    )
    csrf_match = re.search(
        r'name="csrf_token" value="([^"]+)"',
        authorize.text,
    )
    if authorize.status_code != 200 or csrf_match is None:
        raise RuntimeError(
            "persisted OAuth client was not accepted: "
            f"HTTP {authorize.status_code}"
        )
    approved = await client.post(
        f"{base_url}/authorize",
        data={
            **authorize_params,
            "api_key": api_key,
            "csrf_token": html.unescape(csrf_match.group(1)),
        },
    )
    callback_match = re.search(
        r'<a href="([^"]+)">Click here if not redirected</a>',
        approved.text,
    )
    if approved.status_code != 200 or callback_match is None:
        raise RuntimeError(
            f"OAuth authorization failed: HTTP {approved.status_code}"
        )
    callback = urlparse(html.unescape(callback_match.group(1)))
    callback_query = parse_qs(callback.query, keep_blank_values=True)
    codes = callback_query.get("code", [])
    states = callback_query.get("state", [])
    if (
        callback.scheme != "http"
        or callback.netloc != "localhost:8765"
        or callback.path != "/callback"
        or len(codes) != 1
        or states != ["container-smoke"]
    ):
        raise RuntimeError("OAuth authorization returned an invalid callback")
    token = await client.post(
        f"{base_url}/token",
        data={
            "grant_type": "authorization_code",
            "code": codes[0],
            "client_id": client_id,
            "redirect_uri": _OAUTH_REDIRECT_URI,
            "code_verifier": _PKCE_VERIFIER,
            "scope": _OAUTH_SCOPE,
            "resource": base_url,
        },
    )
    if token.status_code != 200:
        raise RuntimeError(f"OAuth code exchange failed: HTTP {token.status_code}")
    replay = await client.post(
        f"{base_url}/token",
        data={
            "grant_type": "authorization_code",
            "code": codes[0],
            "client_id": client_id,
            "redirect_uri": _OAUTH_REDIRECT_URI,
            "code_verifier": _PKCE_VERIFIER,
            "scope": _OAUTH_SCOPE,
            "resource": base_url,
        },
    )
    if replay.status_code != 400 or replay.json().get("error") != "invalid_grant":
        raise RuntimeError("OAuth authorization code replay was not rejected")
    return token.json()


async def _verify_oauth_persistence(
    base_url: str,
    api_key: str,
    state_path: Path,
) -> str:
    state = _read_oauth_state(state_path) if state_path.exists() else {}
    async with httpx.AsyncClient(timeout=10) as client:
        client_id = state.get("client_id")
        if client_id is None:
            registered = await client.post(
                f"{base_url}/register",
                json={
                    "redirect_uris": [_OAUTH_REDIRECT_URI],
                    "client_name": "fetchaller container persistence smoke",
                    "token_endpoint_auth_method": "none",
                },
            )
            registered.raise_for_status()
            client_id = registered.json()["client_id"]
        previous_refresh = state.get("refresh_token")
        if previous_refresh is None:
            tokens = await _pair_oauth_client(
                client,
                base_url,
                api_key,
                client_id,
            )
            status = "paired"
        else:
            rotated = await client.post(
                f"{base_url}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": previous_refresh,
                    "client_id": client_id,
                    "scope": _OAUTH_SCOPE,
                    "resource": base_url,
                },
            )
            if rotated.status_code != 200:
                raise RuntimeError(
                    "persisted OAuth refresh token was not accepted: "
                    f"HTTP {rotated.status_code}"
                )
            tokens = rotated.json()
            replay = await client.post(
                f"{base_url}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": previous_refresh,
                    "client_id": client_id,
                    "scope": _OAUTH_SCOPE,
                    "resource": base_url,
                },
            )
            if (
                replay.status_code != 400
                or replay.json().get("error") != "invalid_grant"
            ):
                raise RuntimeError("OAuth refresh-token replay was not rejected")
            status = "rotated"
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise RuntimeError("OAuth token response omitted required tokens")
        if previous_refresh is not None and refresh_token == previous_refresh:
            raise RuntimeError("OAuth refresh token was not rotated")

    await _verify_mcp_bearer(base_url, access_token)
    _write_oauth_state(
        state_path,
        {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "refresh_sha256": hashlib.sha256(
                refresh_token.encode()
            ).hexdigest(),
        },
    )
    return status


async def run(
    base_url: str,
    api_key: str,
    wait_seconds: float,
    oauth_client_state: Path | None,
    all_tools: bool,
) -> None:
    base_url = base_url.rstrip("/")
    health = await _wait_for_health(base_url.rstrip("/"), wait_seconds)
    if not health.get("readiness") or not all(health["readiness"].values()):
        raise RuntimeError(f"health readiness is incomplete: {health}")

    oauth_status = None
    if oauth_client_state is not None:
        oauth_status = await _verify_oauth_persistence(
            base_url,
            api_key,
            oauth_client_state,
        )

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        # The shared semantic suite grants each MCP call up to 180 seconds.
        # Keep the HTTP transport above that contract so its socket timeout
        # cannot abort a valid 90-second protected-commerce operation first.
        timeout=httpx.Timeout(30, read=200),
    ) as client:
        async with streamable_http_client(
            f"{base_url.rstrip('/')}/mcp",
            http_client=client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                if all_tools:
                    results = await run_live_tool_suite(session)
                    failed = [result for result in results if not result.passed]
                    for result in results:
                        print(
                            f"{'PASS' if result.passed else 'FAIL'} "
                            f"{result.name}: {result.detail}"
                        )
                        if not result.passed and result.text:
                            print(
                                "  "
                                + result.text[:300].replace("\n", " | ")
                            )
                    if failed:
                        raise RuntimeError(
                            f"{len(failed)}/{len(results)} live tool gates failed"
                        )
                    print(
                        f"PASS health={health['status']} "
                        f"tools={len(EXPECTED_TOOLS)} "
                        f"semantic_gates={len(results)} "
                        f"oauth={'skipped' if oauth_status is None else oauth_status}"
                    )
                    return

                initialized = await session.initialize()
                listed = await session.list_tools()
                fetched = await session.call_tool(
                    "fetch",
                    {
                        "url": "https://example.com/",
                        "timeout": 30,
                    },
                )

    names = [tool.name for tool in listed.tools]
    if initialized.serverInfo.name != "fetchaller":
        raise RuntimeError(f"unexpected server name: {initialized.serverInfo.name}")
    if initialized.serverInfo.version != __version__:
        raise RuntimeError(
            f"version mismatch: server={initialized.serverInfo.version}, "
            f"client={__version__}"
        )
    if names != EXPECTED_TOOLS:
        raise RuntimeError(f"unexpected tool surface: {names}")
    fetched_text = "\n".join(
        str(getattr(item, "text", ""))
        for item in fetched.content
    )
    if fetched.isError or "Example Domain" not in fetched_text:
        raise RuntimeError(
            "live fetch tool dispatch failed "
            f"(isError={fetched.isError}, chars={len(fetched_text)})"
        )
    print(
        f"PASS health={health['status']} "
        f"server={initialized.serverInfo.name} {initialized.serverInfo.version} "
        f"tools={len(names)} fetch_chars={len(fetched_text)} "
        f"oauth={'skipped' if oauth_status is None else oauth_status}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--api-key",
        default=os.environ.get("MCP_API_KEY"),
        help=(
            "MCP bearer key (defaults to MCP_API_KEY; prefer the environment "
            "to avoid exposing a secret in the process list)"
        ),
    )
    parser.add_argument("--wait-seconds", type=float, default=60)
    parser.add_argument("--oauth-client-state", type=Path)
    parser.add_argument(
        "--all-tools",
        action="store_true",
        help="call all ten live tools and enforce strict semantic contracts",
    )
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key or MCP_API_KEY is required")
    asyncio.run(
        run(
            args.url,
            args.api_key,
            args.wait_seconds,
            args.oauth_client_state,
            args.all_tools,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
