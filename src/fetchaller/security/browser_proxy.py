"""Loopback-only SOCKS5 egress guard for Chromium challenge solving.

Chromium does not use wafer/wreq's ``resolve=`` DNS pins.  A challenged page
can therefore make browser-native redirects, subresource requests, XHR/fetch,
worker, or WebSocket connections to an internal address unless the browser's
actual sockets are constrained.

This proxy is that socket boundary:

* it accepts SOCKS5 CONNECT only on an ephemeral IPv4 loopback port;
* it resolves every requested hostname through fetchaller's SSRF policy;
* it rejects the destination when any DNS answer is non-public;
* it connects to one of the approved *numeric IPs*, never the hostname; and
* it leaves port selection to Chromium's own unsafe-port policy.

TLS remains end-to-end between Chromium and the origin, so SNI, certificate
validation, HTTP/2, WebSockets, and the browser's network fingerprint are not
modified.  Patchright configures Chromium to send hostname-bearing SOCKS
requests and to proxy loopback targets rather than applying Chrome's implicit
localhost bypass.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterable

from .ssrf import HostVerdict, _is_private_ip, check_host_sync

logger = logging.getLogger("fetchaller.browser_proxy")

_SOCKS_VERSION = 5
_AUTH_NONE = 0
_AUTH_UNACCEPTABLE = 0xFF
_CMD_CONNECT = 1
_ATYP_IPV4 = 1
_ATYP_DOMAIN = 3
_ATYP_IPV6 = 4

_REP_OK = 0
_REP_GENERAL_FAILURE = 1
_REP_DENIED = 2
_REP_NETWORK_UNREACHABLE = 3
_REP_HOST_UNREACHABLE = 4
_REP_CONNECTION_REFUSED = 5
_REP_COMMAND_UNSUPPORTED = 7
_REP_ADDRESS_UNSUPPORTED = 8


class BrowserProxyError(RuntimeError):
    """The guarded browser proxy could not start or pass readiness checks."""


class _ProtocolError(Exception):
    def __init__(self, reply: int = _REP_GENERAL_FAILURE):
        self.reply = reply
        super().__init__(reply)


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def process_request(self, request, client_address) -> None:
        """Acquire capacity before ThreadingMixIn allocates a handler thread."""

        owner: BrowserEgressProxy = self.owner  # type: ignore[attr-defined]
        if not owner._slots.acquire(blocking=False):
            # An untrusted page can fan out across many hostnames. Closing at
            # the accept boundary keeps max_connections a real thread/resource
            # bound rather than spawning an unbounded number of denial threads.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            owner._slots.release()
            self.shutdown_request(request)
            raise


class _SocksHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        owner: BrowserEgressProxy = self.server.owner  # type: ignore[attr-defined]
        owner._handle_client(self.request)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise _ProtocolError()
        data.extend(chunk)
    return bytes(data)


def _reply_address(sock: socket.socket | None) -> bytes:
    if sock is None:
        return bytes((_ATYP_IPV4, 0, 0, 0, 0, 0, 0))
    try:
        host, port = sock.getsockname()[:2]
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv4Address):
            return bytes((_ATYP_IPV4,)) + address.packed + int(port).to_bytes(2, "big")
        return bytes((_ATYP_IPV6,)) + address.packed + int(port).to_bytes(2, "big")
    except (OSError, ValueError):
        return bytes((_ATYP_IPV4, 0, 0, 0, 0, 0, 0))


def _send_reply(
    client: socket.socket,
    reply: int,
    upstream: socket.socket | None = None,
) -> None:
    client.sendall(bytes((_SOCKS_VERSION, reply, 0)) + _reply_address(upstream))


def _canonical_domain(raw: bytes) -> str:
    try:
        host = raw.decode("ascii").rstrip(".").lower()
    except UnicodeDecodeError as exc:
        raise _ProtocolError(_REP_ADDRESS_UNSUPPORTED) from exc
    if (
        not host
        or len(host) > 253
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in host)
        or "/" in host
        or "\\" in host
        or "@" in host
        or ":" in host
    ):
        raise _ProtocolError(_REP_ADDRESS_UNSUPPORTED)
    try:
        # Chrome normally supplies A-labels already. Round-tripping through the
        # stdlib codec canonicalizes case/Unicode variants before policy lookup.
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _ProtocolError(_REP_ADDRESS_UNSUPPORTED) from exc
    if any(not label or len(label) > 63 for label in host.split(".")):
        raise _ProtocolError(_REP_ADDRESS_UNSUPPORTED)
    return host


class BrowserEgressProxy:
    """A bounded, loopback-only SOCKS5 CONNECT proxy.

    ``host_checker`` is injectable for deterministic protocol tests. Production
    callers must use the default fetchaller SSRF policy.
    """

    def __init__(
        self,
        *,
        host_checker: Callable[[str], HostVerdict] = check_host_sync,
        allowed_ports: Iterable[int] | None = None,
        connect_timeout: float = 10.0,
        handshake_timeout: float = 5.0,
        idle_timeout: float = 90.0,
        max_connections: int = 128,
        max_resolved_ips: int = 16,
    ) -> None:
        ports = None if allowed_ports is None else frozenset(allowed_ports)
        if ports is not None and (
            not ports
            or any(
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
                for port in ports
            )
        ):
            raise ValueError("allowed_ports must be None or contain valid TCP ports")
        timeouts = (connect_timeout, handshake_timeout, idle_timeout)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in timeouts
        ):
            raise ValueError("proxy timeouts must be finite positive numbers")
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or max_connections <= 0
        ):
            raise ValueError("max_connections must be a positive integer")
        if (
            isinstance(max_resolved_ips, bool)
            or not isinstance(max_resolved_ips, int)
            or max_resolved_ips <= 0
        ):
            raise ValueError("max_resolved_ips must be a positive integer")

        self._host_checker = host_checker
        self._allowed_ports = ports
        self._connect_timeout = float(connect_timeout)
        self._handshake_timeout = float(handshake_timeout)
        self._idle_timeout = float(idle_timeout)
        self._max_resolved_ips = max_resolved_ips
        self._slots = threading.BoundedSemaphore(max_connections)
        self._close_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._socket_lock = threading.Lock()
        self._active_sockets: set[socket.socket] = set()
        self._stats_lock = threading.Lock()
        self._allowed_connections = 0
        self._denied_connections = 0
        self._server: _ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        server = self._server
        if server is None:
            raise BrowserProxyError("browser proxy has not been started")
        return f"socks5://127.0.0.1:{server.server_address[1]}"

    @property
    def ready(self) -> bool:
        thread = self._thread
        return (
            self._server is not None
            and thread is not None
            and thread.is_alive()
            and not self._close_event.is_set()
        )

    @property
    def allowed_connections(self) -> int:
        with self._stats_lock:
            return self._allowed_connections

    @property
    def denied_connections(self) -> int:
        with self._stats_lock:
            return self._denied_connections

    def start(self) -> None:
        """Bind an ephemeral loopback port and start accepting connections."""

        with self._lifecycle_lock:
            if self.ready:
                return
            if self._server is not None:
                raise BrowserProxyError("browser proxy cannot be restarted after close")
            self._close_event.clear()
            try:
                server = _ThreadingTCPServer(("127.0.0.1", 0), _SocksHandler)
            except OSError as exc:
                raise BrowserProxyError(
                    f"could not bind browser proxy: {type(exc).__name__}"
                ) from exc
            server.owner = self  # type: ignore[attr-defined]
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="fetchaller-browser-proxy",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()

        try:
            self.preflight()
        except Exception:
            self.close()
            raise
        logger.info("Guarded browser proxy listening at %s", self.url)

    def preflight(self) -> None:
        """Exercise the listener and SOCKS no-auth negotiation."""

        if not self.ready:
            raise BrowserProxyError("browser proxy listener is not running")
        port = self._server.server_address[1]  # type: ignore[union-attr]
        try:
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=self._handshake_timeout,
            ) as probe:
                probe.sendall(bytes((_SOCKS_VERSION, 1, _AUTH_NONE)))
                if _recv_exact(probe, 2) != bytes((_SOCKS_VERSION, _AUTH_NONE)):
                    raise BrowserProxyError("browser proxy negotiation failed")
        except (OSError, _ProtocolError) as exc:
            raise BrowserProxyError(
                f"browser proxy preflight failed: {type(exc).__name__}"
            ) from exc

    def close(self) -> None:
        """Stop accepting and force-close every active browser tunnel."""

        with self._lifecycle_lock:
            server = self._server
            thread = self._thread
            if server is None:
                return
            self._close_event.set()

        # Closing active sockets first prevents server_close from leaving
        # daemon handlers stuck in an idle browser tunnel.
        with self._socket_lock:
            active = list(self._active_sockets)
        for sock in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        server.shutdown()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lifecycle_lock:
            self._thread = None
        logger.info("Guarded browser proxy stopped")

    def _track(self, sock: socket.socket) -> None:
        with self._socket_lock:
            self._active_sockets.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._socket_lock:
            self._active_sockets.discard(sock)

    def _approved_ips(self, host: str) -> list[str]:
        try:
            verdict = self._host_checker(host)
        except Exception as exc:
            logger.warning(
                "Browser proxy resolver failed for %s (%s)",
                host,
                type(exc).__name__,
            )
            raise _ProtocolError(_REP_HOST_UNREACHABLE) from exc
        if verdict.blocked or not verdict.ips:
            raise _ProtocolError(_REP_DENIED)

        approved: list[str] = []
        for raw in verdict.ips:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise _ProtocolError(_REP_DENIED) from exc
            normalized = str(address)
            # Re-check inside the socket boundary. A buggy/injected resolver
            # cannot make the proxy dial an internal address by claiming it was
            # approved.
            if _is_private_ip(normalized):
                raise _ProtocolError(_REP_DENIED)
            if normalized not in approved:
                approved.append(normalized)
                if len(approved) > self._max_resolved_ips:
                    raise _ProtocolError(_REP_HOST_UNREACHABLE)
        if not approved:
            raise _ProtocolError(_REP_DENIED)
        return approved

    def _connect_numeric(self, ips: list[str], port: int) -> socket.socket:
        deadline = time.monotonic() + self._connect_timeout
        last_error: OSError | None = None
        for raw in ips:
            if self._close_event.is_set():
                raise _ProtocolError()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            address = ipaddress.ip_address(raw)
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            upstream = socket.socket(family, socket.SOCK_STREAM)
            self._track(upstream)
            try:
                if self._close_event.is_set():
                    raise _ProtocolError()
                upstream.settimeout(remaining)
                if family == socket.AF_INET6:
                    upstream.connect((raw, port, 0, 0))
                else:
                    upstream.connect((raw, port))
                upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return upstream
            except _ProtocolError:
                self._untrack(upstream)
                upstream.close()
                raise
            except OSError as exc:
                last_error = exc
                self._untrack(upstream)
                upstream.close()
        if isinstance(last_error, ConnectionRefusedError):
            raise _ProtocolError(_REP_CONNECTION_REFUSED) from last_error
        raise _ProtocolError(_REP_HOST_UNREACHABLE) from last_error

    def _read_request(self, client: socket.socket) -> tuple[str, int]:
        version, method_count = _recv_exact(client, 2)
        if version != _SOCKS_VERSION or method_count == 0:
            raise _ProtocolError()
        methods = _recv_exact(client, method_count)
        if _AUTH_NONE not in methods:
            client.sendall(bytes((_SOCKS_VERSION, _AUTH_UNACCEPTABLE)))
            raise _ProtocolError()
        client.sendall(bytes((_SOCKS_VERSION, _AUTH_NONE)))

        version, command, reserved, atyp = _recv_exact(client, 4)
        if version != _SOCKS_VERSION or reserved != 0:
            raise _ProtocolError()
        if command != _CMD_CONNECT:
            raise _ProtocolError(_REP_COMMAND_UNSUPPORTED)

        if atyp == _ATYP_IPV4:
            host = str(ipaddress.IPv4Address(_recv_exact(client, 4)))
        elif atyp == _ATYP_IPV6:
            host = str(ipaddress.IPv6Address(_recv_exact(client, 16)))
        elif atyp == _ATYP_DOMAIN:
            length = _recv_exact(client, 1)[0]
            if length == 0:
                raise _ProtocolError(_REP_ADDRESS_UNSUPPORTED)
            host = _canonical_domain(_recv_exact(client, length))
        else:
            raise _ProtocolError(_REP_ADDRESS_UNSUPPORTED)

        port = int.from_bytes(_recv_exact(client, 2), "big")
        if port == 0 or (
            self._allowed_ports is not None and port not in self._allowed_ports
        ):
            raise _ProtocolError(_REP_DENIED)
        return host, port

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        stop = threading.Event()
        activity_lock = threading.Lock()
        last_activity = [time.monotonic()]
        client.settimeout(1.0)
        upstream.settimeout(1.0)

        def pump(source: socket.socket, destination: socket.socket) -> None:
            while not stop.is_set() and not self._close_event.is_set():
                try:
                    data = source.recv(64 * 1024)
                except TimeoutError:
                    with activity_lock:
                        idle = time.monotonic() - last_activity[0]
                    if idle >= self._idle_timeout:
                        stop.set()
                    continue
                except OSError:
                    stop.set()
                    break
                if not data:
                    stop.set()
                    break
                try:
                    destination.sendall(data)
                except OSError:
                    stop.set()
                    break
                with activity_lock:
                    last_activity[0] = time.monotonic()

        reverse = threading.Thread(
            target=pump,
            args=(upstream, client),
            name="fetchaller-browser-proxy-relay",
            daemon=True,
        )
        reverse.start()
        pump(client, upstream)
        stop.set()
        reverse.join(timeout=2)

    def _handle_client(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        self._track(client)
        try:
            if self._close_event.is_set():
                return
            client.settimeout(self._handshake_timeout)
            try:
                host, port = self._read_request(client)
                ips = self._approved_ips(host)
                if self._close_event.is_set():
                    raise _ProtocolError()
                upstream = self._connect_numeric(ips, port)
                with self._stats_lock:
                    self._allowed_connections += 1
                _send_reply(client, _REP_OK, upstream)
                self._relay(client, upstream)
            except _ProtocolError as exc:
                if exc.reply == _REP_DENIED:
                    with self._stats_lock:
                        self._denied_connections += 1
                try:
                    _send_reply(client, exc.reply)
                except OSError:
                    pass
            except (OSError, ValueError):
                try:
                    _send_reply(client, _REP_GENERAL_FAILURE)
                except OSError:
                    pass
        finally:
            if upstream is not None:
                self._untrack(upstream)
                try:
                    upstream.close()
                except OSError:
                    pass
            self._untrack(client)
            try:
                client.close()
            except OSError:
                pass
            self._slots.release()

    def __enter__(self) -> BrowserEgressProxy:
        self.start()
        return self

    def __exit__(self, *_args) -> None:
        self.close()
