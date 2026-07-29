"""SSRF protection: block private/internal IP ranges."""

import asyncio
import functools
import ipaddress
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from urllib.parse import quote

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
_dns_cache_lock = threading.Lock()

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
# BLOCK_UNRESOLVED and BLOCK_SINKHOLE are resolver failures that we fail closed
# on. Conflating them sends people hunting for an SSRF misconfiguration that
# does not exist.
BLOCK_PRIVATE = "private"
BLOCK_UNRESOLVED = "unresolved"
BLOCK_SINKHOLE = "sinkhole"

_PRIVATE_MESSAGE = "Access to private/internal hosts is not allowed."

# Answers that are never a real destination.
#
# A resolver handing back the unspecified address is saying the lookup produced
# no usable record — a filtering resolver sinkholing the name, a stale or
# poisoned cache, or an upstream that answers instead of returning NXDOMAIN.
# 0.0.0.0 is also non-routable, so the private-range guard fired on it and the
# caller was told "Access to private/internal hosts is not allowed" for what was
# really a failed lookup. That reports a security policy decision for a DNS
# problem and sends people looking for an SSRF misconfiguration that isn't there.
#
# Only the unspecified addresses qualify. Some blockers sinkhole to 127.0.0.1
# instead, but that is equally a legitimate split-horizon answer, so re-resolving
# past it would bypass a deliberate internal mapping. 0.0.0.0 and :: have no such
# legitimate use as a destination.
#
# This changes only how a *resolver answer* is classified. An IP literal in the
# URL still goes through _is_private_ip and stays blocked: on Linux, connecting
# to 0.0.0.0 reaches localhost.


def _is_sinkhole_answer(ip_str: str) -> bool:
    """True if a resolver answer is the unspecified address (a failed lookup)."""
    try:
        return ipaddress.ip_address(ip_str.split("%", 1)[0]).is_unspecified
    except ValueError:
        return False


def _split_sinkhole_answers(ips: list[str]) -> tuple[list[str], bool]:
    """Partition resolver answers into real addresses and sinkhole placeholders.

    Returns ``(real_addresses, saw_sinkhole)``. A mixed answer keeps the real
    addresses — the placeholder alongside them is noise, not a verdict.
    """
    real = [ip for ip in ips if not _is_sinkhole_answer(ip)]
    return real, len(real) != len(ips)


# How long a confirmed sinkhole is remembered.
#
# Longer than _DNS_NEGATIVE_TTL on purpose. That two seconds exists so a
# transient resolver hiccup does not strand a healthy host. "The resolver
# sinkholes this name AND public resolvers cannot resolve it either" is not a
# hiccup — it is a stable condition, and re-deciding it every two seconds means
# paying the whole DoH fallback again and hammering the public resolvers on
# every retry of a host that is going to keep failing.
_DNS_SINKHOLE_TTL = 30

# Hosts whose most recent lookup came back sinkholed, so check_host can say so
# instead of reporting a generic resolver failure. Diagnostic state only — the
# access decision is still "blocked" either way. Expires on the same clock as
# the matching negative DNS entry, so the two never disagree about a host.
_sinkholed: dict[str, float] = {}


def _mark_sinkholed(hostname: str, now: float) -> None:
    with _dns_cache_lock:
        if len(_sinkholed) >= _DNS_CACHE_MAX:
            for key in [k for k, exp in _sinkholed.items() if now >= exp]:
                del _sinkholed[key]
            if len(_sinkholed) >= _DNS_CACHE_MAX:
                for key in list(_sinkholed)[: _DNS_CACHE_MAX // 2]:
                    del _sinkholed[key]
        _sinkholed[hostname] = now + _DNS_SINKHOLE_TTL


def _was_sinkholed(hostname: str) -> bool:
    with _dns_cache_lock:
        expires_at = _sinkholed.get(hostname)
    return expires_at is not None and time.monotonic() < expires_at


# DNS-over-HTTPS fallback, used ONLY after a sinkholed answer.
#
# The endpoints are IP literals on purpose: a hostname here would need resolving
# by the very resolver we are working around, and would recurse back into this
# module. Both operators publish certificates with these IPs in the SAN list, so
# TLS verification still applies.
#
# Every address this returns is handed back through the same private-range gate
# as a system-resolver answer — the fallback changes where the record comes
# from, never whether the destination is allowed.
_DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",  # Cloudflare
    "https://8.8.8.8/resolve",  # Google
)
_DOH_TIMEOUT = 5  # seconds, per query
_DOH_TOTAL_TIMEOUT = 6  # seconds for the whole fallback, however many queries
_DOH_MAX_ANSWERS = 32
_DOH_MAX_RESPONSE = 64 * 1024
# DNS record types that carry an address. Everything else in an answer chain
# (CNAME, DNAME, ...) is a link to follow, not a destination.
_DOH_ADDRESS_TYPES = (1, 28)  # A, AAAA


def _doh_fallback_enabled() -> bool:
    """Whether to re-resolve sinkholed hosts against a public resolver.

    OFF by default, and deliberately so. A 0.0.0.0 answer is usually a resolver
    doing its job — a Pi-hole, a UniFi gateway, an enterprise blocklist — and
    quietly resolving past it overrides a policy someone set, while disclosing
    the requested hostname to Cloudflare or Google.

    The defect this module actually had was calling that answer a private-host
    block. Naming it correctly is the fix: BLOCK_SINKHOLE says the resolver is
    at fault, so the resolver can be repaired rather than worked around.

    Set DNS_DOH_FALLBACK=1 to opt in where a broken resolver cannot be fixed.

    Read per call rather than cached at import so a deployment can toggle it
    without a rebuild, and so tests can exercise both paths.
    """
    return os.environ.get("DNS_DOH_FALLBACK", "0") == "1"


def _parse_doh_answer(body: str) -> list[str]:
    """Extract address records from a DoH JSON response.

    Returns an empty list for anything malformed. Private addresses are NOT
    filtered here — that stays with the single caller-side gate in check_host.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return []

    found: list[str] = []
    for answer in answers[:_DOH_MAX_ANSWERS]:
        if not isinstance(answer, dict) or answer.get("type") not in _DOH_ADDRESS_TYPES:
            continue
        data = answer.get("data")
        if not isinstance(data, str):
            continue
        try:
            address = ipaddress.ip_address(data.strip())
        except ValueError:
            continue
        if address.is_unspecified:
            # A public resolver echoing the sinkhole is not an escape hatch.
            continue
        text = str(address)
        if text not in found:
            found.append(text)
    return found


def _doh_query_urls(hostname: str) -> list[tuple[str, str]]:
    """(endpoint, url) pairs to try, A records first then AAAA."""
    name = quote(hostname, safe="")
    return [
        (endpoint, f"{endpoint}?name={name}&type={qtype}")
        for endpoint in _DOH_ENDPOINTS
        for qtype in ("A", "AAAA")
    ]


def _resolvable_via_doh(hostname: str) -> bool:
    """Guard shared by both DoH paths: only plain public-looking names."""
    if not _doh_fallback_enabled():
        return False
    # A name we could not hand to getaddrinfo has no business going to a public
    # resolver either, and the length cap keeps a hostile URL from building an
    # oversized query string.
    return bool(hostname) and len(hostname) <= 253 and hostname.isascii()


async def _resolve_via_doh(hostname: str) -> list[str]:
    """Re-resolve a sinkholed host against public resolvers (async)."""
    if not _resolvable_via_doh(hostname):
        return []

    import wafer

    # One deadline for the whole fallback, not per query. There are four queries
    # (two resolvers x A/AAAA); at _DOH_TIMEOUT each, an unreachable resolver
    # would otherwise burn four full timeouts and eat the caller's entire fetch
    # budget on what is only a best-effort retry of a lookup that already failed.
    deadline = time.monotonic() + _DOH_TOTAL_TIMEOUT
    for _endpoint, url in _doh_query_urls(hostname):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            async with wafer.AsyncSession(max_retries=1, max_rotations=0) as session:
                resp = await session.get(
                    url,
                    headers={"accept": "application/dns-json"},
                    timeout=min(_DOH_TIMEOUT, remaining),
                    max_response_size=_DOH_MAX_RESPONSE,
                )
        except Exception:
            # Any transport failure just means this resolver is unreachable;
            # try the next query, then give up and fail closed as before.
            continue
        if getattr(resp, "status_code", None) != 200:
            continue
        addresses = _parse_doh_answer(resp.text)
        if addresses:
            return addresses
    return []


def _resolve_via_doh_sync(hostname: str) -> list[str]:
    """Blocking twin of :func:`_resolve_via_doh` for the browser egress proxy."""
    if not _resolvable_via_doh(hostname):
        return []

    import wafer

    # Same single deadline as the async twin — more important here, because this
    # runs on a browser-proxy socket thread that a connection is waiting on.
    deadline = time.monotonic() + _DOH_TOTAL_TIMEOUT
    for _endpoint, url in _doh_query_urls(hostname):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with wafer.SyncSession(max_retries=1, max_rotations=0) as session:
                resp = session.get(
                    url,
                    headers={"accept": "application/dns-json"},
                    timeout=min(_DOH_TIMEOUT, remaining),
                    max_response_size=_DOH_MAX_RESPONSE,
                )
        except Exception:
            continue
        if getattr(resp, "status_code", None) != 200:
            continue
        addresses = _parse_doh_answer(resp.text)
        if addresses:
            return addresses
    return []


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
        if self.reason == BLOCK_SINKHOLE:
            return (
                f"DNS resolution failed for '{self.hostname}': the resolver answered with "
                f"0.0.0.0/:: (a sinkhole answer, not a real address), and the public-resolver "
                f"fallback could not resolve it either. This is a DNS problem — the resolver "
                f"in use is filtering or has a stale record for this name — not a "
                f"private-host policy block. The host itself may be perfectly reachable."
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

    with _dns_cache_lock:
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
    except (socket.gaierror, socket.herror, OSError, TimeoutError, UnicodeError):
        # UnicodeError covers hostnames getaddrinfo cannot even IDNA-encode
        # (a label over 63 bytes, for one). That is an unresolvable name, not a
        # caller-visible crash, so it fails closed like any other lookup miss.
        #
        # Cache the failure only briefly (see _DNS_NEGATIVE_TTL): enough to stop
        # a stampede, far too short to strand a host that comes back.
        with _dns_cache_lock:
            _evict_dns_cache(now)
            _dns_cache[hostname] = ([], now + _DNS_NEGATIVE_TTL)
        return None

    ips, saw_sinkhole = _split_sinkhole_answers(ips)
    if not ips and saw_sinkhole:
        # The resolver answered, but with nothing addressable. Re-ask a public
        # resolver before failing: this is the case where the name is fine and
        # only the local resolver is broken.
        ips = await _resolve_via_doh(hostname)
        if not ips:
            _mark_sinkholed(hostname, now)
            with _dns_cache_lock:
                _evict_dns_cache(now)
                _dns_cache[hostname] = ([], now + _DNS_SINKHOLE_TTL)
            return None

    if not ips:
        return []

    with _dns_cache_lock:
        _evict_dns_cache(now)
        _dns_cache[hostname] = (ips, now + _DNS_CACHE_TTL)

    return ips


def _resolve_hostname_sync(hostname: str) -> list[str] | None:
    """Blocking resolver twin used by the browser egress proxy.

    Resolution still runs on the dedicated bounded DNS pool and has the same
    deadline/cache semantics as :func:`_resolve_hostname`.  The browser proxy
    runs on socket-server threads, so this function must not depend on an
    asyncio event loop.
    """

    now = time.monotonic()
    with _dns_cache_lock:
        cached = _dns_cache.get(hostname)
    if cached:
        ips, expires_at = cached
        if now < expires_at:
            return list(ips) if ips else None

    future = _DNS_EXECUTOR.submit(
        socket.getaddrinfo,
        hostname,
        None,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
    )
    try:
        results = future.result(timeout=_DNS_RESOLVE_TIMEOUT)
        ips = list({addr[4][0] for addr in results})
    except (
        socket.gaierror,
        socket.herror,
        OSError,
        FutureTimeoutError,
        UnicodeError,
    ):
        # See _resolve_hostname: an un-encodable hostname is an unresolvable
        # one. On this path it would otherwise escape onto a proxy thread.
        future.cancel()
        with _dns_cache_lock:
            _evict_dns_cache(now)
            _dns_cache[hostname] = ([], now + _DNS_NEGATIVE_TTL)
        return None

    ips, saw_sinkhole = _split_sinkhole_answers(ips)
    if not ips and saw_sinkhole:
        ips = _resolve_via_doh_sync(hostname)
        if not ips:
            _mark_sinkholed(hostname, now)
            with _dns_cache_lock:
                _evict_dns_cache(now)
                _dns_cache[hostname] = ([], now + _DNS_SINKHOLE_TTL)
            return None

    if not ips:
        return []

    with _dns_cache_lock:
        _evict_dns_cache(now)
        _dns_cache[hostname] = (ips, now + _DNS_CACHE_TTL)
    return list(ips)


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

    # Check for bracketed IPv6 addresses [::1]. Invalid literals (including
    # scope-id tricks such as ``[fe80::1%25eth0]``) fail closed instead of
    # falling through as an apparently public fixed address.
    if hostname.startswith("[") and hostname.endswith("]"):
        try:
            ipaddress.IPv6Address(hostname[1:-1])
        except ipaddress.AddressValueError:
            return True
        return _is_private_ip(hostname[1:-1])

    # Check for non-bracketed IPv6 addresses (::1, fe80::1, etc.)
    if ":" in hostname and not hostname.startswith("["):
        try:
            ipaddress.IPv6Address(hostname)
        except ipaddress.AddressValueError:
            return True
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
    normalized = hostname.lower()
    resolved_ips = await _resolve_hostname(normalized)
    if not resolved_ips:
        # Fail closed on both the failure (None) and the empty-answer ([]) case.
        reason = BLOCK_SINKHOLE if _was_sinkholed(normalized) else BLOCK_UNRESOLVED
        return HostVerdict(hostname, True, [], reason)
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
    return check_host_sync(hostname).blocked


def check_host_sync(hostname: str) -> HostVerdict:
    """Synchronous :func:`check_host` with approved numeric IPs.

    This is the security boundary used by the browser SOCKS proxy: it returns
    the exact public addresses the proxy may dial, so no hostname resolution
    occurs after validation.  Do not call it from an asyncio event-loop thread.
    """

    normalized = hostname.strip().rstrip(".").lower()
    if not normalized:
        return HostVerdict(hostname, True, [], BLOCK_PRIVATE)

    result = _is_private_host_sync(normalized)
    if result is not None:
        if result:
            return HostVerdict(hostname, True, [], BLOCK_PRIVATE)
        try:
            literal = str(ipaddress.ip_address(normalized.strip("[]")))
        except ValueError:
            # A static public verdict is only expected for canonical IP
            # literals. Fail closed if that invariant ever changes.
            return HostVerdict(hostname, True, [], BLOCK_PRIVATE)
        return HostVerdict(hostname, False, [literal])

    resolved_ips = _resolve_hostname_sync(normalized)
    if not resolved_ips:
        reason = BLOCK_SINKHOLE if _was_sinkholed(normalized) else BLOCK_UNRESOLVED
        return HostVerdict(hostname, True, [], reason)
    if any(_is_private_ip(ip) for ip in resolved_ips):
        return HostVerdict(hostname, True, [], BLOCK_PRIVATE)
    return HostVerdict(hostname, False, list(resolved_ips))


def clear_dns_cache() -> None:
    """Clear the DNS resolution cache. Useful for testing."""
    with _dns_cache_lock:
        _dns_cache.clear()
        _sinkholed.clear()
