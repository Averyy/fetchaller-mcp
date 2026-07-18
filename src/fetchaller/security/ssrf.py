"""SSRF protection: block private/internal IP ranges."""

import asyncio
import ipaddress
import re
import socket
import time

# Pre-compiled regexes for hot path
_IPV4_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")

# RFC 6598 Carrier-Grade NAT / shared address space. Python's ipaddress does
# NOT classify this as private, yet it routes to carrier/cloud internal infra
# and to Alibaba Cloud's metadata endpoint (100.100.100.200) — so block it.
_CGNAT_V4 = ipaddress.IPv4Network("100.64.0.0/10")


def _v4_blocked(ip: ipaddress.IPv4Address) -> bool:
    """True if an IPv4 address is private/internal/non-routable and must be blocked."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast  # 224.0.0.0/4 — internal service discovery / amplification
        or ip in _CGNAT_V4
    )

# DNS cache TTL (seconds)
_DNS_CACHE_TTL = 60
_DNS_CACHE_MAX = 1024
_DNS_RESOLVE_TIMEOUT = 3  # seconds — DNS should be fast; slow = suspicious
_dns_cache: dict[str, tuple[list[str], float]] = {}


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address string is private/internal."""
    try:
        return _v4_blocked(ipaddress.IPv4Address(ip_str))
    except ipaddress.AddressValueError:
        pass

    try:
        ip = ipaddress.IPv6Address(ip_str)
        mapped = ip.ipv4_mapped
        if mapped:
            return _v4_blocked(mapped)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or ip.is_multicast  # ff00::/8
        )
    except ipaddress.AddressValueError:
        pass

    return False


async def _resolve_hostname(hostname: str) -> list[str]:
    """
    Resolve hostname to IP addresses with TTL-based caching.

    Uses asyncio.getaddrinfo to avoid blocking the event loop.
    Cache entries expire after _DNS_CACHE_TTL seconds to prevent
    stale DNS from enabling SSRF bypass via DNS rebinding.
    """
    now = time.monotonic()

    cached = _dns_cache.get(hostname)
    if cached:
        ips, expires_at = cached
        if now < expires_at:
            return ips

    # Non-blocking DNS resolution with short timeout
    try:
        loop = asyncio.get_running_loop()
        results = await asyncio.wait_for(
            loop.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
            timeout=_DNS_RESOLVE_TIMEOUT,
        )
        ips = list(set(addr[4][0] for addr in results))
    except (socket.gaierror, socket.herror, OSError, TimeoutError):
        ips = []

    # Evict oldest entries if cache is full
    if len(_dns_cache) >= _DNS_CACHE_MAX:
        expired = [k for k, (_, exp) in _dns_cache.items() if now >= exp]
        for k in expired:
            del _dns_cache[k]
        if len(_dns_cache) >= _DNS_CACHE_MAX:
            to_remove = list(_dns_cache.keys())[: _DNS_CACHE_MAX // 2]
            for k in to_remove:
                del _dns_cache[k]
    _dns_cache[hostname] = (ips, now + _DNS_CACHE_TTL)

    return ips


def _is_private_host_sync(hostname: str) -> bool | None:
    """
    Check hostname against static rules (no DNS needed).

    Returns True/False for definitive answers, None if DNS resolution is needed.
    """
    hostname = hostname.lower()

    # Block localhost variants
    if hostname in ("localhost", "127.0.0.1", "::1", "[::1]"):
        return True

    # Block common internal hostnames
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        return True

    # Block DNS rebinding services
    rebinding_domains = (".nip.io", ".xip.io", ".localtest.me", ".sslip.io", ".lvh.me")
    if any(hostname.endswith(d) for d in rebinding_domains) or hostname in ("localtest.me", "lvh.me"):
        return True

    # Check for direct IPv4 addresses.
    #
    # A host that looks like a dotted quad but isn't a *canonical* IPv4 literal
    # ("010.0.0.1", "0177.0.0.1", "256.1.2.3") is an obfuscation attempt: modern
    # ipaddress rejects the leading zero / out-of-range octet, but the OS
    # resolver may still parse it as octal and dial a private address (e.g.
    # "0177.0.0.1" -> 127.0.0.1). Returning False here would mark it "public,
    # nothing to pin" and hand the raw string to wafer — a full SSRF bypass.
    # Fail closed: a non-canonical all-numeric dotted quad has no legitimate use.
    if _IPV4_PATTERN.match(hostname):
        try:
            ipaddress.IPv4Address(hostname)
        except ipaddress.AddressValueError:
            return True
        return _is_private_ip(hostname)

    # Check for bracketed IPv6 addresses [::1]
    if hostname.startswith("[") and hostname.endswith("]"):
        return _is_private_ip(hostname[1:-1])

    # Check for non-bracketed IPv6 addresses (::1, fe80::1, etc.)
    if ":" in hostname and not hostname.startswith("["):
        return _is_private_ip(hostname)

    # Need DNS resolution
    return None


async def resolve_and_check(hostname: str) -> tuple[bool, list[str]]:
    """Check a host and return the validated public IPs to pin the connection to.

    Returns ``(is_private, public_ips)``:

    - Static-rule hosts (localhost, IP literals, rebinding domains) return the
      static verdict with an empty IP list: an IP-literal URL already targets a
      fixed address (nothing to pin), and a blocked host has no IPs to hand back.
    - For DNS hosts, resolves once and inspects every resolved IP. If ANY IP is
      private -- or resolution yields NO IPs (timeout / error / NXDOMAIN) --
      returns ``(True, [])``: FAIL CLOSED, an unresolvable host is blocked, not
      allowed. Otherwise returns ``(False, resolved_ips)`` so the caller can pin
      the socket to exactly these pre-validated addresses, closing the TOCTOU
      DNS-rebinding window between this check and the connect.
    """
    # Fast path: check static rules first (no async needed)
    result = _is_private_host_sync(hostname)
    if result is not None:
        return result, []

    # DNS rebinding protection: resolve hostname and check all IPs
    resolved_ips = await _resolve_hostname(hostname.lower())
    if not resolved_ips:
        # Fail closed: a slow/failed resolver returning [] previously read as
        # "public" (allowed). An unresolvable host must be blocked.
        return True, []
    for ip in resolved_ips:
        if _is_private_ip(ip):
            return True, []

    return False, resolved_ips


async def is_private_host(hostname: str) -> bool:
    """
    Check if hostname resolves to private/internal addresses.

    This is async because it may need to perform DNS resolution.

    Blocks:
    - localhost variants
    - Private IPv4 ranges (10.x, 172.16-31.x, 192.168.x)
    - Link-local addresses (169.254.x, fe80::)
    - Loopback (127.x, ::1)
    - Unique local IPv6 (fc00::/7)
    - DNS rebinding services (nip.io, xip.io, localtest.me)
    - Resolves hostnames to check final IP addresses (DNS rebinding protection)
    - Hosts that cannot be resolved (fails closed)
    """
    is_private, _ = await resolve_and_check(hostname)
    return is_private


def is_private_host_sync(hostname: str) -> bool:
    """
    Synchronous version for tests and non-async contexts.

    Uses blocking DNS resolution. Do NOT call from async code.
    """
    result = _is_private_host_sync(hostname)
    if result is not None:
        return result

    # Blocking DNS fallback
    hostname = hostname.lower()
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ips = list(set(addr[4][0] for addr in results))
    except (socket.gaierror, socket.herror, OSError):
        ips = []

    if not ips:
        return True  # Fail closed: an unresolvable host is blocked, not allowed.

    for ip in ips:
        if _is_private_ip(ip):
            return True

    return False


def clear_dns_cache() -> None:
    """Clear the DNS resolution cache. Useful for testing."""
    _dns_cache.clear()
