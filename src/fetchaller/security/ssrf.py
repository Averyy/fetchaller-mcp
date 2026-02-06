"""SSRF protection: block private/internal IP ranges."""

import asyncio
import ipaddress
import re
import socket
import time

# Pre-compiled regexes for hot path
_IPV4_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")

# DNS cache TTL (seconds)
_DNS_CACHE_TTL = 60
_DNS_CACHE_MAX = 1024
_DNS_RESOLVE_TIMEOUT = 3  # seconds — DNS should be fast; slow = suspicious
_dns_cache: dict[str, tuple[list[str], float]] = {}


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address string is private/internal."""
    try:
        ip = ipaddress.IPv4Address(ip_str)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ipaddress.AddressValueError:
        pass

    try:
        ip = ipaddress.IPv6Address(ip_str)
        mapped = ip.ipv4_mapped
        if mapped:
            return (
                mapped.is_private
                or mapped.is_loopback
                or mapped.is_link_local
                or mapped.is_reserved
                or mapped.is_unspecified
            )
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
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

    # Check for direct IPv4 addresses
    if _IPV4_PATTERN.match(hostname):
        return _is_private_ip(hostname)

    # Check for bracketed IPv6 addresses [::1]
    if hostname.startswith("[") and hostname.endswith("]"):
        return _is_private_ip(hostname[1:-1])

    # Check for non-bracketed IPv6 addresses (::1, fe80::1, etc.)
    if ":" in hostname and not hostname.startswith("["):
        return _is_private_ip(hostname)

    # Need DNS resolution
    return None


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
    """
    # Fast path: check static rules first (no async needed)
    result = _is_private_host_sync(hostname)
    if result is not None:
        return result

    # DNS rebinding protection: resolve hostname and check all IPs
    resolved_ips = await _resolve_hostname(hostname.lower())
    for ip in resolved_ips:
        if _is_private_ip(ip):
            return True

    return False


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

    for ip in ips:
        if _is_private_ip(ip):
            return True

    return False


def clear_dns_cache() -> None:
    """Clear the DNS resolution cache. Useful for testing."""
    _dns_cache.clear()
