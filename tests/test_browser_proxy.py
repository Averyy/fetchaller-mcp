"""Security and protocol tests for Chromium's guarded SOCKS5 egress."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from fetchaller.security import browser_proxy as browser_proxy_module
from fetchaller.security.browser_proxy import BrowserEgressProxy, BrowserProxyError
from fetchaller.security.ssrf import (
    BLOCK_PRIVATE,
    BLOCK_UNRESOLVED,
    HostVerdict,
    check_host_sync,
)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise AssertionError("proxy closed before the complete reply")
        data.extend(chunk)
    return bytes(data)


def _read_reply(sock: socket.socket) -> int:
    version, reply, reserved, atyp = _recv_exact(sock, 4)
    assert version == 5
    assert reserved == 0
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 4:
        _recv_exact(sock, 16)
    elif atyp == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    else:
        raise AssertionError(f"unexpected reply ATYP {atyp}")
    _recv_exact(sock, 2)
    return reply


def _connect_proxy(proxy: BrowserEgressProxy) -> socket.socket:
    port = int(proxy.url.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.settimeout(2)
    sock.sendall(b"\x05\x01\x00")
    assert _recv_exact(sock, 2) == b"\x05\x00"
    return sock


def _request_domain(sock: socket.socket, host: str, port: int) -> int:
    raw = host.encode("ascii")
    assert len(raw) <= 255
    sock.sendall(
        b"\x05\x01\x00\x03"
        + bytes((len(raw),))
        + raw
        + port.to_bytes(2, "big")
    )
    return _read_reply(sock)


def _request_ip(sock: socket.socket, host: str, port: int) -> int:
    address = ipaddress.ip_address(host)
    atyp = 1 if address.version == 4 else 4
    sock.sendall(
        b"\x05\x01\x00"
        + bytes((atyp,))
        + address.packed
        + port.to_bytes(2, "big")
    )
    return _read_reply(sock)


@contextmanager
def _echo_server():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                listener.settimeout(0.2)
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with client:
                try:
                    data = client.recv(1024)
                    if data:
                        client.sendall(data)
                except OSError:
                    pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)


def test_proxy_lifecycle_and_preflight():
    proxy = BrowserEgressProxy()
    with pytest.raises(RuntimeError):
        _ = proxy.url
    proxy.start()
    assert proxy.ready
    assert proxy.url.startswith("socks5://127.0.0.1:")
    proxy.preflight()
    proxy.close()
    assert not proxy.ready
    proxy.close()  # idempotent


def test_connection_limit_rejects_excess_and_recovers_capacity():
    with BrowserEgressProxy(max_connections=1) as proxy:
        first = _connect_proxy(proxy)
        try:
            port = int(proxy.url.rsplit(":", 1)[1])
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=2,
            ) as excess:
                excess.settimeout(2)
                excess.sendall(b"\x05\x01\x00")
                try:
                    closed = excess.recv(2) == b""
                except (ConnectionResetError, OSError):
                    closed = True
                assert closed
        finally:
            first.close()

        deadline = time.monotonic() + 2
        while True:
            try:
                proxy.preflight()
                break
            except BrowserProxyError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("connect_timeout", 0),
        ("handshake_timeout", -1),
        ("idle_timeout", float("inf")),
        ("idle_timeout", float("nan")),
        ("connect_timeout", True),
        ("max_connections", 0),
        ("max_connections", True),
        ("max_connections", 1.5),
        ("max_resolved_ips", 0),
        ("max_resolved_ips", True),
        ("max_resolved_ips", 1.5),
    ],
)
def test_invalid_resource_bounds_are_rejected(keyword, value):
    with pytest.raises(ValueError):
        BrowserEgressProxy(**{keyword: value})


@pytest.mark.parametrize(
    "allowed_ports",
    [(), (0,), (65536,), (True,), ("443",)],
)
def test_invalid_explicit_port_allowlists_are_rejected(allowed_ports):
    with pytest.raises(ValueError):
        BrowserEgressProxy(allowed_ports=allowed_ports)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "metadata.internal",
        "printer.local",
        "127.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "localtest.me",
        "127.0.0.1.nip.io",
    ],
)
def test_private_and_rebinding_destinations_are_denied(host):
    with BrowserEgressProxy() as proxy, _connect_proxy(proxy) as sock:
        reply = (
            _request_ip(sock, host, 443)
            if ":" in host or host[0].isdigit() and host.count(".") == 3
            else _request_domain(sock, host, 443)
        )
    assert reply == 2


def test_explicit_port_allowlist_is_enforced_before_resolution():
    called = False

    def checker(host: str) -> HostVerdict:
        nonlocal called
        called = True
        return HostVerdict(host, False, ["93.184.216.34"])

    with BrowserEgressProxy(
        host_checker=checker,
        allowed_ports=(80, 443),
    ) as proxy:
        with _connect_proxy(proxy) as sock:
            assert _request_domain(sock, "example.com", 22) == 2
    assert called is False


@pytest.mark.parametrize("port", [8080, 8443])
def test_default_policy_allows_public_nonstandard_web_ports(port):
    seen: list[str] = []

    def checker(host: str) -> HostVerdict:
        seen.append(host)
        return HostVerdict(host, False, ["93.184.216.34"])

    with BrowserEgressProxy(host_checker=checker) as proxy:
        upstream, peer = socket.socketpair()
        with (
            upstream,
            peer,
            patch.object(proxy, "_connect_numeric", return_value=upstream),
            _connect_proxy(proxy) as sock,
        ):
            assert _request_domain(sock, "public.example", port) == 0

    assert seen == ["public.example"]


def test_mixed_public_private_answers_fail_closed():
    def checker(host: str) -> HostVerdict:
        return HostVerdict(
            host,
            False,
            ["93.184.216.34", "127.0.0.1"],
        )

    with BrowserEgressProxy(host_checker=checker) as proxy:
        with _connect_proxy(proxy) as sock:
            assert _request_domain(sock, "mixed.invalid", 443) == 2


def test_excessive_unique_public_dns_answers_fail_closed():
    answers = [f"93.184.216.{value}" for value in range(1, 5)]

    def checker(host: str) -> HostVerdict:
        return HostVerdict(host, False, answers)

    with BrowserEgressProxy(
        host_checker=checker,
        max_resolved_ips=3,
    ) as proxy:
        with _connect_proxy(proxy) as sock:
            assert _request_domain(sock, "fanout.invalid", 443) == 4


def test_numeric_fallbacks_share_one_total_connect_deadline(monkeypatch):
    timeouts: list[float] = []
    attempted: list[tuple] = []

    class FailingSocket:
        def settimeout(self, value):
            timeouts.append(value)

        def connect(self, address):
            attempted.append(address)
            raise TimeoutError("timed out")

        def close(self):
            pass

    sockets = [FailingSocket(), FailingSocket()]
    monkeypatch.setattr(
        browser_proxy_module.time,
        "monotonic",
        iter([100.0, 100.0, 106.0, 111.0]).__next__,
    )
    monkeypatch.setattr(
        browser_proxy_module.socket,
        "socket",
        lambda *_args: sockets.pop(0),
    )
    proxy = BrowserEgressProxy(connect_timeout=10)

    with pytest.raises(browser_proxy_module._ProtocolError):
        proxy._connect_numeric(
            ["93.184.216.1", "93.184.216.2", "93.184.216.3"],
            443,
        )

    assert attempted == [
        ("93.184.216.1", 443),
        ("93.184.216.2", 443),
    ]
    assert timeouts == [10.0, 4.0]
    assert proxy._active_sockets == set()


def test_close_interrupts_in_progress_connect_and_stops_fallbacks(monkeypatch):
    started = threading.Event()
    closed = threading.Event()
    attempted: list[tuple] = []

    class BlockingSocket:
        def settimeout(self, _value):
            pass

        def connect(self, address):
            attempted.append(address)
            started.set()
            assert closed.wait(2), "proxy close did not interrupt connect"
            raise OSError("closed")

        def shutdown(self, _how):
            closed.set()

        def close(self):
            closed.set()

    proxy = BrowserEgressProxy(connect_timeout=10)
    proxy.start()
    result: list[BaseException] = []

    def connect() -> None:
        try:
            proxy._connect_numeric(
                ["93.184.216.1", "93.184.216.2"],
                443,
            )
        except BaseException as exc:
            result.append(exc)

    with patch.object(
        browser_proxy_module.socket,
        "socket",
        return_value=BlockingSocket(),
    ):
        worker = threading.Thread(target=connect, daemon=True)
        worker.start()
        assert started.wait(1)
        proxy.close()
        worker.join(timeout=1)

    assert not worker.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], browser_proxy_module._ProtocolError)
    assert attempted == [("93.184.216.1", 443)]
    assert proxy._active_sockets == set()


@pytest.mark.parametrize("reason", [BLOCK_PRIVATE, BLOCK_UNRESOLVED])
def test_blocked_or_unresolved_verdict_fails_closed(reason):
    def checker(host: str) -> HostVerdict:
        return HostVerdict(host, True, [], reason)

    with BrowserEgressProxy(host_checker=checker) as proxy:
        with _connect_proxy(proxy) as sock:
            assert _request_domain(sock, "blocked.invalid", 443) == 2


def test_unsupported_authentication_is_rejected():
    with BrowserEgressProxy() as proxy:
        port = int(proxy.url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.sendall(b"\x05\x01\x02")
            assert _recv_exact(sock, 2) == b"\x05\xff"


def test_unsupported_socks_command_is_rejected():
    with BrowserEgressProxy() as proxy, _connect_proxy(proxy) as sock:
        sock.sendall(b"\x05\x03\x00\x01\x7f\x00\x00\x01\x00\x50")
        assert _read_reply(sock) == 7


def test_zero_length_domain_is_rejected():
    with BrowserEgressProxy() as proxy, _connect_proxy(proxy) as sock:
        sock.sendall(b"\x05\x01\x00\x03\x00\x01\xbb")
        assert _read_reply(sock) == 8


def test_unsupported_address_type_is_rejected():
    with BrowserEgressProxy() as proxy, _connect_proxy(proxy) as sock:
        sock.sendall(b"\x05\x01\x00\x09")
        assert _read_reply(sock) == 8


def test_domain_is_canonicalized_before_policy_check():
    seen: list[str] = []

    def checker(host: str) -> HostVerdict:
        seen.append(host)
        return HostVerdict(host, True, [], BLOCK_PRIVATE)

    with BrowserEgressProxy(host_checker=checker) as proxy:
        with _connect_proxy(proxy) as sock:
            assert _request_domain(sock, "ExAmPlE.CoM.", 443) == 2
    assert seen == ["example.com"]


@pytest.mark.parametrize("host", [" example.com", "example.com ", "\texample.com"])
def test_domain_whitespace_is_rejected_without_policy_lookup(host):
    called = False

    def checker(_host: str) -> HostVerdict:
        nonlocal called
        called = True
        return HostVerdict(_host, False, ["93.184.216.34"])

    with BrowserEgressProxy(host_checker=checker) as proxy:
        with _connect_proxy(proxy) as sock:
            assert _request_domain(sock, host, 443) == 8
    assert called is False


def test_proxy_dials_approved_numeric_ip_without_system_dns():
    """The tunnel reaches the approved IP even though the hostname cannot resolve."""

    with _echo_server() as echo_port:
        calls: list[str] = []

        def checker(host: str) -> HostVerdict:
            calls.append(host)
            return HostVerdict(host, False, ["127.0.0.1"])

        # This patch is deliberately confined to the positive relay harness:
        # production always re-checks and blocks loopback. It lets a local
        # server stand in for a public approved address without real network.
        with (
            patch(
                "fetchaller.security.browser_proxy._is_private_ip",
                return_value=False,
            ),
            BrowserEgressProxy(
                host_checker=checker,
                allowed_ports=(echo_port,),
            ) as proxy,
        ):
            with _connect_proxy(proxy) as sock:
                assert _request_domain(sock, "pinned.invalid", echo_port) == 0
                sock.sendall(b"pin-proof")
                assert _recv_exact(sock, 9) == b"pin-proof"

    assert calls == ["pinned.invalid"]


def test_check_host_sync_returns_exact_public_ips(monkeypatch):
    monkeypatch.setattr(
        "fetchaller.security.ssrf._resolve_hostname_sync",
        lambda _host: ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    )
    verdict = check_host_sync("Example.COM.")
    assert verdict.blocked is False
    assert verdict.ips == [
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    ]


@pytest.mark.parametrize(
    "host",
    ["fe80::1%eth0", "[fe80::1%25eth0]", "2001:db8::1%invalid"],
)
def test_scoped_or_invalid_ipv6_literals_fail_closed(host):
    verdict = check_host_sync(host)
    assert verdict.blocked is True
    assert verdict.ips == []


@pytest.mark.skipif(
    os.environ.get("FETCHALLER_RUN_BROWSER_CANARY") != "1",
    reason="set FETCHALLER_RUN_BROWSER_CANARY=1 to launch system Chrome",
)
def test_real_chromium_loopback_subresources_cannot_hit_canary():
    """Real Chrome must proxy (and deny) loopback rather than bypass SOCKS."""

    hits: list[str] = []
    attempted_hosts: set[str] = set()
    attempts_lock = threading.Lock()

    class CanaryHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    # Listen on all loopback aliases so each browser request vector can use a
    # distinct 127/8 destination.  That proves every vector reached the guard,
    # rather than merely observing a few retries from one request type.
    canary = HTTPServer(("0.0.0.0", 0), CanaryHandler)
    canary_thread = threading.Thread(
        target=canary.serve_forever,
        daemon=True,
    )
    canary_thread.start()
    canary_port = canary.server_address[1]

    from wafer.browser import BrowserSolver

    def recording_checker(host: str) -> HostVerdict:
        with attempts_lock:
            attempted_hosts.add(host)
        return check_host_sync(host)

    probe_hosts = {
        "css": "127.0.0.2",
        "image": "127.0.0.3",
        "iframe": "127.0.0.4",
        "fetch": "127.0.0.5",
        "xhr": "127.0.0.6",
        "events": "127.0.0.7",
        "websocket": "127.0.0.8",
        "worker": "127.0.0.9",
        "top_level": "127.0.0.10",
        "meta_refresh": "127.0.0.11",
    }
    proxy = BrowserEgressProxy(
        host_checker=recording_checker,
        allowed_ports=(canary_port,),
    )
    proxy.start()
    solver = BrowserSolver(
        headless=True,
        egress_guard_proxy=proxy.url,
        executable_path=os.environ.get("BROWSER_EXECUTABLE_PATH") or None,
    )
    try:
        def drive_page() -> None:
            solver._ensure_browser()
            context = solver._create_context()
            try:
                page = context.new_page()
                target = {
                    name: f"http://{host}:{canary_port}"
                    for name, host in probe_hosts.items()
                }
                page.set_content(
                    f"<link rel='stylesheet' href='{target['css']}/style.css'>"
                    f"<img src='{target['image']}/image'>"
                    f"<iframe src='{target['iframe']}/iframe'></iframe>"
                    "<script>"
                    f"fetch('{target['fetch']}/fetch').catch(() => {{}});"
                    "const xhr = new XMLHttpRequest();"
                    f"xhr.open('GET', '{target['xhr']}/xhr'); xhr.send();"
                    f"new EventSource('{target['events']}/events');"
                    f"new WebSocket('ws://{probe_hosts['websocket']}:{canary_port}/ws');"
                    "const workerCode = "
                    f"\"fetch('{target['worker']}/worker').catch(() => {{}})\";"
                    "new Worker(URL.createObjectURL("
                    "new Blob([workerCode], {type: 'text/javascript'})));"
                    "</script>"
                )
                page.wait_for_timeout(1000)

                direct = context.new_page()
                try:
                    direct.goto(
                        f"{target['top_level']}/top-level",
                        wait_until="domcontentloaded",
                        timeout=2000,
                    )
                except Exception:
                    pass

                refresh = context.new_page()
                refresh.set_content(
                    "<meta http-equiv='refresh' "
                    f"content='0;url={target['meta_refresh']}/meta-refresh'>"
                )
                refresh.wait_for_timeout(500)
            finally:
                context.close()

        solver._run_on_worker(drive_page)
    finally:
        solver.close()
        proxy.close()
        canary.shutdown()
        canary.server_close()
        canary_thread.join(timeout=2)

    assert attempted_hosts >= set(probe_hosts.values())
    assert proxy.denied_connections >= len(probe_hosts)
    assert hits == []


@pytest.mark.skipif(
    os.environ.get("FETCHALLER_RUN_BROWSER_CANARY") != "1",
    reason="set FETCHALLER_RUN_BROWSER_CANARY=1 to launch system Chrome",
)
def test_real_chromium_can_reach_public_https_through_guard():
    """The guard must preserve normal end-to-end browser TLS and navigation."""

    from wafer.browser import BrowserSolver

    proxy = BrowserEgressProxy()
    proxy.start()
    solver = BrowserSolver(
        headless=True,
        egress_guard_proxy=proxy.url,
        executable_path=os.environ.get("BROWSER_EXECUTABLE_PATH") or None,
    )
    try:
        def drive_page() -> tuple[int, str]:
            solver._ensure_browser()
            context = solver._create_context()
            try:
                page = context.new_page()
                response = page.goto(
                    "https://example.com/",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                return response.status if response else 0, page.title()
            finally:
                context.close()

        status, title = solver._run_on_worker(drive_page)
    finally:
        solver.close()
        proxy.close()

    assert status == 200
    assert title == "Example Domain"
    assert proxy.allowed_connections >= 1


@pytest.mark.skipif(
    os.environ.get("FETCHALLER_RUN_BROWSER_CANARY") != "1",
    reason="set FETCHALLER_RUN_BROWSER_CANARY=1 to launch system Chrome",
)
def test_real_chromium_can_reach_approved_nonstandard_port():
    """A local stand-in proves guarded Chrome retains nonstandard web ports."""

    hits: list[str] = []

    class CanaryHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"nonstandard-port-ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    canary = HTTPServer(("127.0.0.1", 0), CanaryHandler)
    canary_thread = threading.Thread(
        target=canary.serve_forever,
        daemon=True,
    )
    canary_thread.start()
    canary_port = canary.server_address[1]

    def approved_test_host(host: str) -> HostVerdict:
        assert host == "public-canary.invalid"
        return HostVerdict(host, False, ["127.0.0.1"])

    from wafer.browser import BrowserSolver

    # Loopback is a test-only stand-in for an approved public numeric address.
    # Production's independent _is_private_ip re-check remains exercised by
    # all denial tests above and is never configurable by server callers.
    with patch(
        "fetchaller.security.browser_proxy._is_private_ip",
        return_value=False,
    ):
        proxy = BrowserEgressProxy(host_checker=approved_test_host)
        proxy.start()
        solver = BrowserSolver(
            headless=True,
            egress_guard_proxy=proxy.url,
            executable_path=os.environ.get("BROWSER_EXECUTABLE_PATH") or None,
        )
        try:
            def drive_page() -> tuple[int, str]:
                solver._ensure_browser()
                context = solver._create_context()
                try:
                    page = context.new_page()
                    response = page.goto(
                        f"http://public-canary.invalid:{canary_port}/nonstandard",
                        wait_until="domcontentloaded",
                        timeout=10000,
                    )
                    return response.status if response else 0, page.text_content("body")
                finally:
                    context.close()

            status, body = solver._run_on_worker(drive_page)
        finally:
            solver.close()
            proxy.close()
            canary.shutdown()
            canary.server_close()
            canary_thread.join(timeout=2)

    assert canary_port not in (80, 443)
    assert status == 200
    assert body == "nonstandard-port-ok"
    assert hits == ["/nonstandard"]
    assert proxy.allowed_connections >= 1


@pytest.mark.skipif(
    os.environ.get("FETCHALLER_RUN_BROWSER_CANARY") != "1",
    reason="set FETCHALLER_RUN_BROWSER_CANARY=1 for guarded live fetch",
)
def test_actual_fetch_url_accepts_browser_only_guarded_solver():
    """Regression: native direct+pin and guarded browser egress may coexist."""

    from wafer.browser import BrowserSolver

    from fetchaller.tools.fetch import fetch_url

    proxy = BrowserEgressProxy()
    proxy.start()
    solver = BrowserSolver(
        headless=True,
        egress_guard_proxy=proxy.url,
        executable_path=os.environ.get("BROWSER_EXECUTABLE_PATH") or None,
    )
    try:
        result = asyncio.run(
            fetch_url(
                "https://example.com/",
                raw=True,
                timeout=15,
                browser_solver=solver,
            )
        )
    finally:
        solver.close()
        proxy.close()

    assert "error" not in result
    assert "Example Domain" in result["content"]
    assert solver.proxy_server is None
    assert solver.egress_guard_proxy == proxy.url
