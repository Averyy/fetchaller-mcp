"""SSRF protection: block private/internal IP ranges."""

import ipaddress
import re
import socket
from functools import lru_cache


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address string is private/internal."""
    try:
        # Try IPv4
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
        # Try IPv6
        ip = ipaddress.IPv6Address(ip_str)
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


def _check_ipv4_mapped_ipv6(ipv6_str: str) -> bool:
    """Check IPv4-mapped IPv6 addresses like ::ffff:127.0.0.1."""
    v4_mapped_match = re.match(r"^::ffff:(\d+\.\d+\.\d+\.\d+)$", ipv6_str, re.IGNORECASE)
    if v4_mapped_match:
        return _is_private_ip(v4_mapped_match.group(1))
    return False


@lru_cache(maxsize=1024)
def _resolve_hostname(hostname: str) -> list[str]:
    """
    Resolve hostname to IP addresses.

    Cached to prevent repeated DNS lookups, but cache is bounded.
    Returns list of resolved IP addresses.
    """
    try:
        # Use getaddrinfo for both IPv4 and IPv6
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return list(set(addr[4][0] for addr in results))
    except (socket.gaierror, socket.herror, OSError):
        return []


def is_private_host(hostname: str) -> bool:
    """
    Check if hostname resolves to private/internal addresses.

    Blocks:
    - localhost variants
    - Private IPv4 ranges (10.x, 172.16-31.x, 192.168.x)
    - Link-local addresses (169.254.x, fe80::)
    - Loopback (127.x, ::1)
    - Unique local IPv6 (fc00::/7)
    - DNS rebinding services (nip.io, xip.io, localtest.me)
    - Resolves hostnames to check final IP addresses (DNS rebinding protection)
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
    ipv4_match = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$", hostname)
    if ipv4_match:
        return _is_private_ip(hostname)

    # Check for bracketed IPv6 addresses [::1]
    if hostname.startswith("[") and hostname.endswith("]"):
        ipv6_str = hostname[1:-1]
        if _is_private_ip(ipv6_str):
            return True
        if _check_ipv4_mapped_ipv6(ipv6_str):
            return True
        return False

    # Check for non-bracketed IPv6 addresses (::1, fe80::1, etc.)
    # IPv6 addresses contain colons but are not URLs with ports
    if ":" in hostname and not hostname.startswith("["):
        # This looks like a bare IPv6 address
        if _is_private_ip(hostname):
            return True
        if _check_ipv4_mapped_ipv6(hostname):
            return True
        # Don't return False yet - might be a hostname with a port

    # DNS rebinding protection: resolve hostname and check all IPs
    # This prevents attacks where a hostname initially resolves to a public IP
    # but later resolves to a private IP
    resolved_ips = _resolve_hostname(hostname)
    for ip in resolved_ips:
        if _is_private_ip(ip):
            return True
        if _check_ipv4_mapped_ipv6(ip):
            return True

    return False


def clear_dns_cache() -> None:
    """Clear the DNS resolution cache. Useful for testing."""
    _resolve_hostname.cache_clear()
