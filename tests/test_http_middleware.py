"""HTTP middleware ordering and relative-clock regressions."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from fetchaller.config import Config
from fetchaller.http.app import _transport_security_settings, create_app
from fetchaller.http.middleware import (
    RateLimiter,
    RateLimitMiddleware,
    RequestBodyLimitMiddleware,
)


def test_app_rate_limits_before_reading_request_body():
    app = create_app(
        Config(
            api_key=None,
            data_dir=None,
            browser_preflight=False,
        ),
        mcp_server=MagicMock(),
    )

    middleware = [item.cls for item in app.user_middleware]

    assert middleware.index(RateLimitMiddleware) < middleware.index(
        RequestBodyLimitMiddleware
    )


@pytest.mark.parametrize(
    ("server_url", "allowed_host", "allowed_origin"),
    [
        ("https://Fetchaller.Example", "fetchaller.example", "https://fetchaller.example"),
        ("https://fetchaller.example:443", "fetchaller.example", "https://fetchaller.example"),
        ("https://fetchaller.example:8443", "fetchaller.example:8443", "https://fetchaller.example:8443"),
        ("http://localhost:80", "localhost", "http://localhost"),
        ("http://[::1]:6000", "[::1]:6000", "http://[::1]:6000"),
    ],
)
def test_mcp_transport_security_is_bound_to_exact_public_origin(
    server_url,
    allowed_host,
    allowed_origin,
):
    settings = _transport_security_settings(Config(server_url=server_url))

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == [allowed_host]
    assert settings.allowed_origins == [allowed_origin]


def test_mcp_transport_rejects_host_and_origin_rebinding():
    server_url = "https://fetchaller.example"
    app = create_app(
        Config(
            api_key="test-api-key",
            jwt_secret="0123456789abcdef0123456789abcdef",
            server_url=server_url,
            data_dir=None,
            browser_preflight=False,
        )
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "security-test", "version": "1"},
        },
    }
    headers = {
        "authorization": "Bearer test-api-key",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    with TestClient(app, base_url=server_url) as client:
        absent_origin = client.post("/mcp", json=initialize, headers=headers)
        exact_origin = client.post(
            "/mcp",
            json=initialize,
            headers={**headers, "origin": server_url},
        )
        hostile_host = client.post(
            "/mcp",
            json=initialize,
            headers={**headers, "host": "attacker.example"},
        )
        hostile_origin = client.post(
            "/mcp",
            json=initialize,
            headers={**headers, "origin": "https://attacker.example"},
        )

    assert absent_origin.status_code == 200
    assert exact_origin.status_code == 200
    assert hostile_host.status_code == 421
    assert hostile_host.text == "Invalid Host header"
    assert hostile_origin.status_code == 403
    assert hostile_origin.text == "Invalid Origin header"


@pytest.mark.parametrize(
    ("configured_url", "client_url"),
    [
        ("https://fetchaller.example:443", "https://fetchaller.example"),
        ("http://localhost:80", "http://localhost"),
    ],
)
def test_mcp_transport_accepts_clients_that_omit_explicit_default_port(
    configured_url,
    client_url,
):
    """HTTP clients canonicalize :443/:80 out of Host and Origin."""
    config = Config(
        api_key="test-api-key",
        jwt_secret="0123456789abcdef0123456789abcdef",
        server_url=configured_url,
        data_dir=None,
        browser_preflight=False,
    )
    app = create_app(config)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "default-port-test", "version": "1"},
        },
    }
    headers = {
        "authorization": "Bearer test-api-key",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "origin": client_url,
    }

    with TestClient(app, base_url=client_url) as client:
        response = client.post("/mcp", json=initialize, headers=headers)

    assert config.effective_server_url == client_url
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_over_limit_request_body_receive_is_never_called():
    limiter = RateLimiter(requests_per_minute=0)
    middleware = RateLimitMiddleware(MagicMock(), rate_limiter=limiter)
    receive_called = False

    async def receive():
        nonlocal receive_called
        receive_called = True
        raise AssertionError("rate-limited request body was consumed")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.4", 1234),
            "server": ("fetchaller.example", 443),
            "http_version": "1.1",
            "app": MagicMock(),
        },
        receive=receive,
    )

    response = await middleware.dispatch(
        request,
        MagicMock(side_effect=AssertionError("downstream was called")),
    )

    assert response.status_code == 429
    assert receive_called is False


def test_rate_limiter_uses_monotonic_clock(monkeypatch):
    from fetchaller.http import middleware

    monkeypatch.setattr(
        middleware.time,
        "time",
        MagicMock(side_effect=AssertionError("wall clock used")),
    )
    monkeypatch.setattr(middleware.time, "monotonic", lambda: 123.0)

    limiter = RateLimiter(requests_per_minute=2)

    assert limiter.check("203.0.113.4") == (True, None)
    assert list(limiter._entries["203.0.113.4"].requests) == [123.0]
