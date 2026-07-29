"""HTTP deployment smoke-test orchestration tests."""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from scripts import http_smoke_test
from scripts.smoke_test import Result


class _AsyncClient:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class _ClientSession:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


@asynccontextmanager
async def _streamable_client(*args, **kwargs):
    del args, kwargs
    yield object(), object(), None


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _QueuedHttpClient:
    def __init__(self, *, get=(), post=()):
        self.get_responses = list(get)
        self.post_responses = list(post)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self.get_responses.pop(0)

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self.post_responses.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("passed", [True, False])
async def test_all_tools_http_gate_propagates_semantic_failure(
    passed: bool,
) -> None:
    result = Result(
        "fetch",
        passed,
        "ok" if passed else "semantic contract failed",
        "content",
    )
    health = {
        "status": "healthy",
        "readiness": {"browser": True, "oauth": True},
    }
    suite = AsyncMock(return_value=[result])
    with (
        patch(
            "scripts.http_smoke_test._wait_for_health",
            new=AsyncMock(return_value=health),
        ),
        patch(
            "scripts.http_smoke_test.httpx.AsyncClient",
            _AsyncClient,
        ),
        patch(
            "scripts.http_smoke_test.streamable_http_client",
            _streamable_client,
        ),
        patch(
            "scripts.http_smoke_test.ClientSession",
            _ClientSession,
        ),
        patch(
            "scripts.http_smoke_test.run_live_tool_suite",
            new=suite,
        ),
    ):
        if passed:
            await http_smoke_test.run(
                "http://localhost:6000",
                "test-key",
                1,
                None,
                True,
            )
        else:
            with pytest.raises(RuntimeError, match="live tool gates failed"):
                await http_smoke_test.run(
                    "http://localhost:6000",
                    "test-key",
                    1,
                    None,
                    True,
                )

    suite.assert_awaited_once()


@pytest.mark.asyncio
async def test_pair_oauth_client_exchanges_and_rejects_code_replay():
    client = _QueuedHttpClient(
        get=[
            _Response(
                text='<input name="csrf_token" value="csrf-value">',
            )
        ],
        post=[
            _Response(
                text=(
                    '<a href="http://localhost:8765/callback?'
                    'code=one-time-code&amp;state=container-smoke">'
                    "Click here if not redirected</a>"
                ),
            ),
            _Response(
                payload={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                }
            ),
            _Response(
                status_code=400,
                payload={"error": "invalid_grant"},
            ),
        ],
    )

    tokens = await http_smoke_test._pair_oauth_client(
        client,
        "http://localhost:6000",
        "api-key",
        "client-id",
    )

    assert tokens["access_token"] == "access-token"
    assert [request[0] for request in client.requests] == [
        "GET",
        "POST",
        "POST",
        "POST",
    ]
    assert (
        client.requests[1][2]["data"]["csrf_token"]
        == "csrf-value"
    )
    assert (
        client.requests[2][2]["data"]["code_verifier"]
        == http_smoke_test._PKCE_VERIFIER
    )


@pytest.mark.asyncio
async def test_oauth_smoke_rotates_persisted_refresh_and_rejects_replay(
    tmp_path,
):
    state_path = tmp_path / "oauth-state.json"
    http_smoke_test._write_oauth_state(
        state_path,
        {
            "client_id": "persisted-client",
            "refresh_token": "old-refresh",
        },
    )
    client = _QueuedHttpClient(
        post=[
            _Response(
                payload={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                }
            ),
            _Response(
                status_code=400,
                payload={"error": "invalid_grant"},
            ),
        ],
    )
    verified = AsyncMock()
    with (
        patch(
            "scripts.http_smoke_test.httpx.AsyncClient",
            return_value=client,
        ),
        patch(
            "scripts.http_smoke_test._verify_mcp_bearer",
            new=verified,
        ),
    ):
        status = await http_smoke_test._verify_oauth_persistence(
            "http://localhost:6000",
            "api-key",
            state_path,
        )

    assert status == "rotated"
    verified.assert_awaited_once_with(
        "http://localhost:6000",
        "new-access",
    )
    state = http_smoke_test._read_oauth_state(state_path)
    assert state == {
        "client_id": "persisted-client",
        "refresh_token": "new-refresh",
    }
    assert os.stat(state_path).st_mode & 0o777 == 0o600
    assert [
        request[2]["data"]["refresh_token"]
        for request in client.requests
    ] == ["old-refresh", "old-refresh"]


@pytest.mark.asyncio
async def test_oauth_smoke_registers_pairs_calls_mcp_and_seals_state(
    tmp_path,
):
    state_path = tmp_path / "oauth-state.json"
    client = _QueuedHttpClient(
        post=[
            _Response(payload={"client_id": "new-client"}),
        ],
    )
    paired = AsyncMock(
        return_value={
            "access_token": "paired-access",
            "refresh_token": "paired-refresh",
        }
    )
    verified = AsyncMock()
    with (
        patch(
            "scripts.http_smoke_test.httpx.AsyncClient",
            return_value=client,
        ),
        patch(
            "scripts.http_smoke_test._pair_oauth_client",
            new=paired,
        ),
        patch(
            "scripts.http_smoke_test._verify_mcp_bearer",
            new=verified,
        ),
    ):
        status = await http_smoke_test._verify_oauth_persistence(
            "http://localhost:6000",
            "api-key",
            state_path,
        )

    assert status == "paired"
    paired.assert_awaited_once_with(
        client,
        "http://localhost:6000",
        "api-key",
        "new-client",
    )
    verified.assert_awaited_once_with(
        "http://localhost:6000",
        "paired-access",
    )
    assert http_smoke_test._read_oauth_state(state_path) == {
        "client_id": "new-client",
        "refresh_token": "paired-refresh",
    }
    assert os.stat(state_path).st_mode & 0o777 == 0o600
