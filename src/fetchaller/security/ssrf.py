"""SSRF protection: block private/internal IP ranges."""

import asyncio
import functools
import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# Pre-compiled regexes for hot path
_IPV4_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")

# RFC 6598 Carrier-Grade NAT / shared address space. Python's ipaddress does
# NOT classify this as private, yet it routes to carrier/cloud internal infra
# and to Alibaba Cloud's metadata endpoint (100.100.100.200) — so block it.
_CGNAT_V4 = ipaddress.IPv4Network("100.64.0.0/10")

# RFC 6052 well-known NAT64 prefix. On an IPv6-only / NAT64 network EVERY public
# IPv4 host resolves into this range, and Python flags it is_reserved — so a
# blanket block bricks the whole tool there while telling the user "private
# host". What actually matters for SSRF is the IPv4 address embedded in the low
# 32 bits, which is where the translated packet lands. Unwrap and judge that.
#
# Only the /96 encoding is unwrapped. RFC 6052 also permits /32/40/48/56/64
# prefixes whose embedded-v4 bit positions differ, and the RFC 8215 local-use
# prefix (64:ff9b:1::/48) does not carry its own length — we cannot know where
# the v4 sits, so those stay blocked rather than guessed at.
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")


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


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address an IPv6 address translates to, if any.

    Covers the three transition mechanisms that carry a routable v4 destination
    inside a v6 address: v4-mapped (::ffff:0:0/96), 6to4 (2002::/16), and
    well-known NAT64 (64:ff9b::/96). Teredo is handled by the caller because it
    embeds two addresses (server and client), both of which must be checked.
    """
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip in _NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


# DNS cache TTL (seconds)
_DNS_CACHE_TTL = 60
# Failures are cached too, but only briefly. Not caching them at all sounds
# safer and is worse: during a resolver outage every request re-runs a lookup
# that occupies a pool thread for the full timeout, so a handful of dead hosts
# can starve resolution for healthy ones. A short TTL absorbs that stampede
# while keeping a recovered host blocked for ~2s rather than the full minute
# that caused the false "private host" rejections.
_DNS_NEGATIVE_TTL = 2
_DNS_CACHE_MAX = 1024
# Generous by design. This bounds a *queued* resolver call, not a healthy one
# (a warm lookup is single-digit milliseconds). Too tight and transient load
# turns into a bogus "private host" rejection.
_DNS_RESOLVE_TIMEOUT = 8  # seconds
_dns_cache: dict[str, tuple[list[str], float]] = {}

# Dedicated resolver pool.
#
# loop.getaddrinfo() dispatches to asyncio's DEFAULT executor, which this
# process shares with every other blocking call — HTML parsing, PDF extraction,
# and the fan-out of concurrent tool calls. Under load that pool saturates,
# getaddrinfo queues behind unrelated work, blows _DNS_RESOLVE_TIMEOUT, and a
# perfectly public host fails closed and is reported as private. Giving DNS its
# own threads means tool concurrency can never starve name resolution.
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fetchaller-dns")


# Reasons a host was blocked. BLOCK_PRIVATE is a security decision;
# BLOCK_UNRESOLVED is a resolver failure that we fail closed on. Conflating the
# two sends people hunting for an SSRF misconfiguration that does not exist.
BLOCK_PRIVATE = "private"
BLOCK_UNRESOLVED = "unresolved"

_PRIVATE_MESSAGE = "Access to private/internal hosts is not allowed."


@dataclass(frozen=True)
class HostVerdict:
    """Outcome of an SSRF host check.

    ``ips`` holds the validated public addresses to pin the connection to, and is
    empty for both blocked hosts and IP literals (which already target a fixed
    address, so there is nothing to pin).
    """

    hostname: str
    blocked: bool
    ips: list[str]
    reason: str | None = None  # BLOCK_* when blocked, else None

    @property
    def message(self) -> str:
        """User-facing explanation. Empty string when the host is allowed."""
        if not self.blocked:
            return ""
        if self.reason == BLOCK_UNRESOLVED:
            return (
                f"Could not resolve host '{self.hostname}' — DNS lookup failed or timed out. "
                f"Blocked as a precaution. This is a name-resolution failure, not a "
                f"private-host policy block; the host may be fine. Retry shortly."
            )
        return _PRIVATE_MESSAGE


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address string is private/internal."""
    try:
        return _v4_blocked(ipaddress.IPv4Address(ip_str))
    except ipaddress.AddressValueError:
        pass

    try:
        ip = ipaddress.IPv6Address(ip_str)

        # Transition addresses carry a real IPv4 destination. Judge that, not the
        # v6 wrapper — otherwise NAT64/6to4 networks see every public host
        # rejected as "private" (is_reserved / is_private both fire on them).
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return _v4_blocked(embedded)

        # Teredo embeds both a server and a client v4; either being internal is
        # disqualifying.
        if ip.teredo:
            return any(_v4_blocked(v4) for v4 in ip.teredo)

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


def _evict_dns_cache(now: float) -> None:
    """Drop expired entries, and half the cache if it is still at capacity."""
    if len(_dns_cache) < _DNS_CACHE_MAX:
        return
    for k in [k for k, (_, exp) in _dns_cache.items() if now >= exp]:
        del _dns_cache[k]
    if len(_dns_cache) >= _DNS_CACHE_MAX:
        for k in list(_dns_cache.keys())[: _DNS_CACHE_MAX // 2]:
            del _dns_cache[k]


async def _resolve_hostname(hostname: str) -> list[str] | None:
    """
    Resolve hostname to IP addresses with TTL-based caching.

    Runs on a dedicated executor so unrelated blocking work cannot starve it.
    Cache entries expire after _DNS_CACHE_TTL seconds to prevent stale DNS from
    enabling SSRF bypass via DNS rebinding.

    Returns the resolved addresses, or ``None`` if resolution itself failed
    (timeout, gaierror, NXDOMAIN). Failures are cached for _DNS_NEGATIVE_TTL
    only — never the full success TTL, which is what turned one transient
    hiccup into a 60-second lockout reported as "private host".

    An empty list in the cache always means "lookup failed": a successful
    lookup that yields no addresses is returned without being cached.
    """
    now = time.monotonic()

    cached = _dns_cache.get(hostname)
    if cached:
        ips, expires_at = cached
        if now < expires_at:
            return ips if ips else None

    # Non-blocking DNS resolution on the dedicated resolver pool.
    try:
        loop = asyncio.get_running_loop()
        results = await asyncio.wait_for(
            loop.run_in_executor(
                _DNS_EXECUTOR,
                functools.partial(
                    socket.getaddrinfo,
                    hostname,
                    None,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                ),
            ),
            timeout=_DNS_RESOLVE_TIMEOUT,
        )
        ips = list(set(addr[4][0] for addr in results))
    except (socket.gaierror, socket.herror, OSError, TimeoutError):
        # Cache the failure only briefly (see _DNS_NEGATIVE_TTL): enough to stop
        # a stampede, far too short to strand a host that comes back.
        _evict_dns_cache(now)
        _dns_cache[hostname] = ([], now + _DNS_NEGATIVE_TTL)
        return None

    if not ips:
        return []

    _evict_dns_cache(now)
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


async def check_host(hostname: str) -> HostVerdict:
    """Check a host and return a verdict carrying the IPs to pin and a reason.

    - Static-rule hosts (localhost, IP literals, rebinding domains) get the
      static verdict with an empty IP list: an IP-literal URL already targets a
      fixed address (nothing to pin), and a blocked host has no IPs to hand back.
    - For DNS hosts, resolves once and inspects every resolved IP. If ANY IP is
      private the verdict is BLOCK_PRIVATE. If resolution fails or yields no
      addresses the verdict is BLOCK_UNRESOLVED — still blocked (FAIL CLOSED),
      but distinguishable, because "the resolver timed out" and "you tried to
      reach an internal host" need very different responses from the caller.
    - Otherwise returns the resolved IPs so the caller can pin the socket to
      exactly these pre-validated addresses, closing the TOCTOU DNS-rebinding
      window between this check and the connect.
    """
    # Fast path: check static rules first (no async needed)
    result = _is_private_host_sync(hostname)
    if result is not None:
        return HostVerdict(hostname, result, [], BLOCK_PRIVATE if result else None)

    # DNS rebinding protection: resolve hostname and check all IPs
    resolved_ips = await _resolve_hostname(hostname.lower())
    if not resolved_ips:
        # Fail closed on both the failure (None) and the empty-answer ([]) case.
        return HostVerdict(hostname, True, [], BLOCK_UNRESOLVED)
    for ip in resolved_ips:
        if _is_private_ip(ip):
            return HostVerdict(hostname, True, [], BLOCK_PRIVATE)

    # Copy: `resolved_ips` is the live list held in _dns_cache, and the caller
    # stores this straight into its connection-pin map. Handing out the cache's
    # own object lets anything downstream rewrite the validated-IP set for that
    # host for the rest of the TTL. Cheap copy, removes the whole class.
    return HostVerdict(hostname, False, list(resolved_ips))


async def resolve_and_check(hostname: str) -> tuple[bool, list[str]]:
    """Backward-compatible wrapper over :func:`check_host`.

    Returns ``(is_private, public_ips)``. Prefer ``check_host`` in new code —
    it distinguishes an unresolvable host from a genuinely private one.
    """
    verdict = await check_host(hostname)
    return verdict.blocked, verdict.ips


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
    - Transition addresses (NAT64/6to4/Teredo/v4-mapped) whose embedded IPv4 is internal
    - Hosts that cannot be resolved (fails closed)
    """
    verdict = await check_host(hostname)
    return verdict.blocked


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
