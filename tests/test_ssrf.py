"""Tests for SSRF protection."""

import asyncio
import socket
import time

import pytest

from fetchaller.security import ssrf
from fetchaller.security.ssrf import (
    BLOCK_PRIVATE,
    BLOCK_SINKHOLE,
    BLOCK_UNRESOLVED,
    check_host,
    clear_dns_cache,
    resolve_and_check,
)
from fetchaller.security.ssrf import is_private_host_sync as is_private_host


class TestSSRFProtection:
    """Test SSRF protection blocks private/internal hosts."""

    def setup_method(self):
        """Clear DNS cache before each test."""
        clear_dns_cache()

    # Localhost variants
    @pytest.mark.parametrize("host", [
        "localhost",
        "127.0.0.1",
        "127.0.0.2",
        "127.255.255.255",
    ])
    def test_blocks_localhost(self, host):
        assert is_private_host(host) is True

    # IPv6 loopback
    @pytest.mark.parametrize("host", [
        "::1",
        "[::1]",
    ])
    def test_blocks_ipv6_loopback(self, host):
        assert is_private_host(host) is True

    # Private IPv4 ranges
    @pytest.mark.parametrize("host", [
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.0.1",
        "192.168.255.255",
    ])
    def test_blocks_private_ipv4(self, host):
        assert is_private_host(host) is True

    # Link-local addresses
    @pytest.mark.parametrize("host", [
        "169.254.0.1",
        "169.254.169.254",  # AWS metadata
    ])
    def test_blocks_link_local(self, host):
        assert is_private_host(host) is True

    # DNS rebinding services
    @pytest.mark.parametrize("host", [
        "127.0.0.1.nip.io",
        "192.168.1.1.nip.io",
        "test.xip.io",
        "localtest.me",
        "foo.localtest.me",
        "test.sslip.io",
        "test.lvh.me",
    ])
    def test_blocks_dns_rebinding_services(self, host):
        assert is_private_host(host) is True

    # Internal hostname patterns
    @pytest.mark.parametrize("host", [
        "server.local",
        "db.internal",
    ])
    def test_blocks_internal_hostnames(self, host):
        assert is_private_host(host) is True

    # Public hosts should be allowed
    @pytest.mark.parametrize("host", [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",  # example.com
    ])
    def test_allows_public_ips(self, host):
        assert is_private_host(host) is False

    # Reserved/special addresses
    @pytest.mark.parametrize("host", [
        "0.0.0.0",
        "255.255.255.255",
    ])
    def test_blocks_reserved_addresses(self, host):
        assert is_private_host(host) is True


class TestObfuscatedAndSharedRanges:
    """Regression tests for the octal-IP bypass and CGNAT/multicast gaps."""

    def setup_method(self):
        clear_dns_cache()

    # Octal / leading-zero / out-of-range dotted quads: the OS resolver may
    # parse these as octal and dial a private host. Must fail closed.
    @pytest.mark.parametrize("host", [
        "010.0.0.1",       # octal -> 8.0.0.1 (glibc) or private forms
        "0177.0.0.1",      # octal -> 127.0.0.1 on glibc
        "0100.0.0.1",      # octal -> 64.0.0.1
        "00.0.0.0",
        "256.1.2.3",       # out-of-range octet
        "999.999.999.999",
    ])
    def test_blocks_obfuscated_ipv4(self, host):
        assert is_private_host(host) is True

    async def test_blocks_obfuscated_ipv4_async(self):
        for host in ("010.0.0.1", "0177.0.0.1", "256.1.2.3"):
            is_private, ips = await resolve_and_check(host)
            assert is_private is True, host
            assert ips == []

    # RFC 6598 CGNAT / shared address space (incl. Alibaba Cloud metadata).
    @pytest.mark.parametrize("host", [
        "100.64.0.1",
        "100.100.100.200",  # Alibaba Cloud ECS metadata endpoint
        "100.127.255.255",
    ])
    def test_blocks_cgnat(self, host):
        assert is_private_host(host) is True

    # Multicast (v4 224.0.0.0/4, v6 ff00::/8).
    @pytest.mark.parametrize("host", [
        "224.0.0.1",
        "239.255.255.250",  # SSDP
        "[ff02::1]",
    ])
    def test_blocks_multicast(self, host):
        assert is_private_host(host) is True

    # 100.63/100.128 are just outside CGNAT and are ordinary public space.
    @pytest.mark.parametrize("host", [
        "100.63.255.255",
        "100.128.0.1",
    ])
    def test_allows_public_ip_near_cgnat(self, host):
        assert is_private_host(host) is False


class TestFailClosed:
    """A host that cannot be resolved must be blocked, not allowed (fail closed)."""

    def setup_method(self):
        clear_dns_cache()

    def test_sync_unresolvable_host_blocked(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _boom)
        # Previously fell through to "allowed" (return False); must now block.
        assert is_private_host("does-not-resolve.example") is True

    async def test_async_unresolvable_host_blocked(self, monkeypatch):
        async def _empty(_host):
            return []

        monkeypatch.setattr(ssrf, "_resolve_hostname", _empty)
        is_private, ips = await resolve_and_check("does-not-resolve.example")
        assert is_private is True

    async def test_unencodable_hostname_blocks_instead_of_raising(self):
        """A label over the DNS limit makes getaddrinfo raise UnicodeError.

        That escaped both resolvers as an unhandled exception -- surfacing to the
        MCP caller as a crash on the async path, and onto a socket-server thread
        on the sync path the browser egress proxy uses. It is simply an
        unresolvable name, so it must fail closed like any other lookup miss.
        """

        hostname = "a" * 5000 + ".example"
        is_private, ips = await resolve_and_check(hostname)
        assert is_private is True
        assert ips == []

    def test_unencodable_hostname_blocks_on_the_sync_proxy_path(self):
        verdict = ssrf.check_host_sync("a" * 5000 + ".example")
        assert verdict.blocked is True
        assert verdict.reason == BLOCK_UNRESOLVED
        assert verdict.ips == []

    async def test_async_public_host_returns_ips_to_pin(self, monkeypatch):
        async def _public(_host):
            return ["93.184.216.34"]

        monkeypatch.setattr(ssrf, "_resolve_hostname", _public)
        is_private, ips = await resolve_and_check("example.com")
        assert is_private is False
        assert ips == ["93.184.216.34"]

    async def test_async_private_resolution_blocked_no_ips(self, monkeypatch):
        async def _private(_host):
            return ["10.0.0.5"]

        monkeypatch.setattr(ssrf, "_resolve_hostname", _private)
        is_private, ips = await resolve_and_check("sneaky.example")
        assert is_private is True
        assert ips == []  # never hand back a private IP to pin

    async def test_async_ip_literal_public_no_pin(self):
        # A public IP literal is allowed but has no host to pin (the URL already
        # targets a fixed address), so no IPs are returned.
        is_private, ips = await resolve_and_check("93.184.216.34")
        assert is_private is False
        assert ips == []


class TestNegativeDNSCachingIsShortLived:
    """A transient resolver failure must not lock a host out for a full TTL.

    The regression: _resolve_hostname cached its empty-on-error result under the
    60s success TTL, so one timeout blocked the host for a full minute even
    after DNS recovered — and reported it as "private", sending users to debug
    an SSRF policy that was never the problem.

    Failures are still cached, but only for _DNS_NEGATIVE_TTL (see the tests
    below): long enough to stop a stampede during an outage, short enough that a
    recovered host is not stranded.
    """

    def setup_method(self):
        clear_dns_cache()

    async def test_recovers_without_waiting_out_the_success_ttl(self, monkeypatch):
        calls = []

        async def _flaky(host):
            calls.append(host)
            # Fail once, then succeed — mimicking a transient timeout.
            return None if len(calls) == 1 else ["93.184.216.34"]

        monkeypatch.setattr(ssrf, "_resolve_hostname", _flaky)

        first = await check_host("flaky.example")
        assert first.blocked is True
        assert first.reason == BLOCK_UNRESOLVED

        # The moment DNS recovers the host must be reachable again.
        second = await check_host("flaky.example")
        assert second.blocked is False
        assert second.ips == ["93.184.216.34"]
        assert len(calls) == 2, "second check must re-resolve, not read a cached failure"

    async def test_resolver_error_cached_only_briefly(self, monkeypatch):
        """Failures ARE cached — but for seconds, not the full success TTL.

        Not caching failures at all lets a resolver outage re-run a lookup per
        request, each holding a pool thread for the whole timeout, which starves
        resolution for healthy hosts. The bug was the 60s duration, not the
        caching itself.
        """
        def _boom(*args, **kwargs):
            raise socket.gaierror("temporary failure in name resolution")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _boom)
        assert await ssrf._resolve_hostname("nope.example") is None

        entry = ssrf._dns_cache.get("nope.example")
        assert entry is not None, "failure should be cached to absorb a stampede"
        ips, expires_at = entry
        assert ips == [], "an empty cache entry is the 'lookup failed' marker"
        assert expires_at - time.monotonic() <= ssrf._DNS_NEGATIVE_TTL
        assert ssrf._DNS_NEGATIVE_TTL <= 5
        assert ssrf._DNS_NEGATIVE_TTL < ssrf._DNS_CACHE_TTL / 10

    async def test_cached_failure_reads_back_as_failure_not_success(self, monkeypatch):
        """A cached empty list must fail closed, never look like 'resolved to nothing'."""
        ssrf._dns_cache["cached-fail.example"] = ([], time.monotonic() + 60)
        assert await ssrf._resolve_hostname("cached-fail.example") is None

        verdict = await check_host("cached-fail.example")
        assert verdict.blocked is True
        assert verdict.reason == BLOCK_UNRESOLVED

    async def test_negative_entry_expires_and_rereolves(self, monkeypatch):
        calls = []

        def _ok(*args, **kwargs):
            calls.append(1)
            return [(None, None, None, None, ("93.184.216.34", 0))]

        # Pre-seed an ALREADY-EXPIRED failure entry.
        ssrf._dns_cache["recovered.example"] = ([], time.monotonic() - 1)
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _ok)

        assert await ssrf._resolve_hostname("recovered.example") == ["93.184.216.34"]
        assert len(calls) == 1

    async def test_successful_resolution_is_cached(self, monkeypatch):
        calls = []

        def _ok(*args, **kwargs):
            calls.append(1)
            return [(None, None, None, None, ("93.184.216.34", 0))]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _ok)
        assert await ssrf._resolve_hostname("good.example") == ["93.184.216.34"]
        assert await ssrf._resolve_hostname("good.example") == ["93.184.216.34"]
        assert len(calls) == 1, "a successful lookup should still be cached"


class TestBlockReasons:
    """'DNS timed out' and 'you asked for an internal host' are different events."""

    def setup_method(self):
        clear_dns_cache()

    async def test_unresolved_message_is_not_a_privacy_block(self, monkeypatch):
        async def _fail(_host):
            return None

        monkeypatch.setattr(ssrf, "_resolve_hostname", _fail)
        verdict = await check_host("github.com")

        assert verdict.blocked is True
        assert verdict.reason == BLOCK_UNRESOLVED
        msg = verdict.message
        assert "github.com" in msg
        assert "resolve" in msg.lower()
        # Must NOT claim the host is private — that was the misleading bit.
        assert "private/internal hosts is not allowed" not in msg

    async def test_private_message_unchanged(self, monkeypatch):
        async def _private(_host):
            return ["10.0.0.5"]

        monkeypatch.setattr(ssrf, "_resolve_hostname", _private)
        verdict = await check_host("sneaky.example")

        assert verdict.blocked is True
        assert verdict.reason == BLOCK_PRIVATE
        assert verdict.message == "Access to private/internal hosts is not allowed."

    async def test_allowed_host_has_no_message(self, monkeypatch):
        async def _public(_host):
            return ["93.184.216.34"]

        monkeypatch.setattr(ssrf, "_resolve_hostname", _public)
        verdict = await check_host("example.com")
        assert verdict.blocked is False
        assert verdict.reason is None
        assert verdict.message == ""


class TestTransitionalIPv6:
    """NAT64 / 6to4 / Teredo carry a real IPv4 destination — judge that, not the wrapper.

    On an IPv6-only or NAT64 network every public IPv4 host resolves into
    64:ff9b::/96, which Python flags is_reserved. Blanket-blocking it made every
    public host look "private" and bricked the tool on such networks.
    """

    def setup_method(self):
        clear_dns_cache()

    # Embedded IPv4 is public -> allow.
    @pytest.mark.parametrize("host", [
        "64:ff9b::8c52:7203",    # NAT64 -> 140.82.114.3 (github)
        "2002:8c52:7203::1",     # 6to4  -> 140.82.114.3
        "::ffff:140.82.114.3",   # v4-mapped -> 140.82.114.3
    ])
    def test_allows_public_embedded_ipv4(self, host):
        assert is_private_host(host) is False

    # Embedded IPv4 is internal -> block. This is the case a blanket allow
    # would have opened up, so it matters more than the one above.
    @pytest.mark.parametrize("host", [
        "64:ff9b::7f00:1",       # NAT64 -> 127.0.0.1
        "64:ff9b::a00:5",        # NAT64 -> 10.0.0.5
        "64:ff9b::a9fe:a9fe",    # NAT64 -> 169.254.169.254 (cloud metadata)
        "64:ff9b::6440:1",       # NAT64 -> 100.64.0.1 (CGNAT)
        "2002:7f00:1::1",        # 6to4  -> 127.0.0.1
        "2002:a9fe:a9fe::1",     # 6to4  -> 169.254.169.254
        "::ffff:127.0.0.1",      # v4-mapped loopback
        "::ffff:169.254.169.254",
    ])
    def test_blocks_internal_embedded_ipv4(self, host):
        assert is_private_host(host) is True

    def test_teredo_blocked_when_either_endpoint_is_internal(self):
        # Teredo embeds a server and a client v4; 192.0.2.45 (TEST-NET-1) is
        # reserved, so this must block on the client address.
        assert is_private_host("2001:0:4136:e378:8000:63bf:3fff:fdd2") is True

    def test_ambiguous_nat64_prefix_stays_blocked(self):
        # RFC 8215 local-use 64:ff9b:1::/48 does not carry its own prefix
        # length, so the embedded-v4 position is unknowable. Fail closed.
        assert is_private_host("64:ff9b:1::8c52:7203") is True

    # Ordinary global IPv6 is unaffected by the unwrapping logic.
    @pytest.mark.parametrize("host", [
        "2606:50c0:8000::153",   # github pages
        "2001:4860:4860::8888",  # google dns
    ])
    def test_allows_ordinary_global_ipv6(self, host):
        assert is_private_host(host) is False

    @pytest.mark.parametrize("host", [
        "fe80::1",     # link-local
        "fc00::1",     # unique local
        "::1",         # loopback
        "ff02::1",     # multicast
    ])
    def test_still_blocks_internal_ipv6(self, host):
        assert is_private_host(host) is True


class TestResolverIsolation:
    """DNS must not be starved by unrelated blocking work.

    Root cause of the original incident: loop.getaddrinfo() runs on asyncio's
    DEFAULT executor, shared with HTML/PDF parsing and concurrent tool fan-out.
    Saturating it made github.com time out and get reported as a private host.
    """

    def setup_method(self):
        clear_dns_cache()

    async def test_resolves_while_default_executor_is_saturated(self):
        import asyncio
        import time as _time

        loop = asyncio.get_running_loop()
        # Occupy far more slots than the default executor has.
        hogs = [loop.run_in_executor(None, _time.sleep, 5) for _ in range(64)]
        await asyncio.sleep(0.2)

        try:
            verdict = await check_host("localhost.")  # resolves without network
            # The point is that it RESOLVED rather than timing out; localhost
            # is of course still blocked as private.
            assert verdict.reason != BLOCK_UNRESOLVED, (
                "DNS was starved by the default executor — resolver isolation failed"
            )
        finally:
            for h in hogs:
                h.cancel()

    def test_dns_uses_a_dedicated_executor(self):
        assert ssrf._DNS_EXECUTOR is not None
        # Must not be asyncio's shared default pool.
        assert ssrf._DNS_EXECUTOR._thread_name_prefix == "fetchaller-dns"

    def test_dns_timeout_has_headroom(self):
        # 3s was tight enough that ordinary load produced false "private" verdicts.
        assert ssrf._DNS_RESOLVE_TIMEOUT >= 8


class TestCacheIsNotAliased:
    """The verdict must never hand out the DNS cache's own list object.

    fetch.py stores verdict.ips directly into its connection-pin map and passes
    it to wafer. If that is the cached list, anything mutating it silently
    rewrites the validated-IP set for that host for the rest of the TTL.
    """

    def setup_method(self):
        clear_dns_cache()

    async def test_verdict_ips_is_a_copy(self, monkeypatch):
        def _ok(*args, **kwargs):
            return [(None, None, None, None, ("93.184.216.34", 0))]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _ok)
        verdict = await check_host("copy.example")
        cached = ssrf._dns_cache["copy.example"][0]

        assert verdict.ips == cached
        assert verdict.ips is not cached

    async def test_mutating_verdict_does_not_poison_the_cache(self, monkeypatch):
        def _ok(*args, **kwargs):
            return [(None, None, None, None, ("93.184.216.34", 0))]

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _ok)
        first = await check_host("poison.example")
        first.ips.append("10.0.0.5")  # a downstream consumer mutating its pin list

        second = await check_host("poison.example")
        assert second.blocked is False
        assert second.ips == ["93.184.216.34"]
        assert "10.0.0.5" not in second.ips


class TestSinkholeAnswers:
    """A resolver answering 0.0.0.0 / :: reported a failed lookup, not a private host.

    A filtering resolver (or a stale/poisoned cache) hands back the unspecified
    address for a name it will not resolve. That address is non-routable, so the
    private-range guard fired on it and the caller was told "Access to
    private/internal hosts is not allowed" — a security verdict for what is
    actually a DNS failure, pointing at entirely the wrong cause.
    """

    def setup_method(self):
        clear_dns_cache()

    @staticmethod
    def _answers(*addresses):
        def _resolve(*args, **kwargs):
            return [(None, None, None, None, (address, 0)) for address in addresses]

        return _resolve

    async def test_ipv4_sinkhole_is_not_a_private_verdict(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", self._answers("0.0.0.0"))
        monkeypatch.setattr(ssrf, "_resolve_via_doh", self._no_doh)

        verdict = await check_host("sinkholed.example")

        assert verdict.blocked is True  # still fails closed
        assert verdict.reason == BLOCK_SINKHOLE
        assert verdict.reason != BLOCK_PRIVATE
        assert "0.0.0.0" in verdict.message
        assert "not a" in verdict.message and "private-host policy block" in verdict.message

    async def test_ipv6_sinkhole_is_not_a_private_verdict(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", self._answers("::"))
        monkeypatch.setattr(ssrf, "_resolve_via_doh", self._no_doh)

        verdict = await check_host("sinkholed6.example")

        assert verdict.blocked is True
        assert verdict.reason == BLOCK_SINKHOLE

    async def test_sinkhole_alongside_a_real_address_keeps_the_real_one(self, monkeypatch):
        monkeypatch.setattr(
            ssrf.socket,
            "getaddrinfo",
            self._answers("0.0.0.0", "93.184.216.34"),
        )

        verdict = await check_host("mixed.example")

        assert verdict.blocked is False
        assert verdict.ips == ["93.184.216.34"]

    async def test_public_resolver_fallback_recovers_the_host(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", self._answers("0.0.0.0"))

        async def _doh(hostname):
            assert hostname == "recovered.example"
            return ["51.161.117.187"]

        monkeypatch.setattr(ssrf, "_resolve_via_doh", _doh)

        verdict = await check_host("recovered.example")

        assert verdict.blocked is False
        assert verdict.ips == ["51.161.117.187"]

    async def test_fallback_answers_still_face_the_private_range_gate(self, monkeypatch):
        """The fallback changes where the record comes from, never what is allowed."""
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", self._answers("0.0.0.0"))

        async def _doh(hostname):
            return ["169.254.169.254"]  # cloud metadata endpoint

        monkeypatch.setattr(ssrf, "_resolve_via_doh", _doh)

        verdict = await check_host("rebind.example")

        assert verdict.blocked is True
        assert verdict.reason == BLOCK_PRIVATE

    @pytest.mark.parametrize("literal", ["0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0"])
    async def test_unspecified_ip_literals_are_still_private_blocks(self, literal):
        """Only a resolver ANSWER is reclassified. On Linux, dialing 0.0.0.0
        reaches localhost, so a literal in the URL must stay a hard block."""
        verdict = await check_host(literal)

        assert verdict.blocked is True
        assert verdict.reason == BLOCK_PRIVATE

    async def test_ordinary_resolution_failure_is_still_unresolved(self, monkeypatch):
        def _fail(*args, **kwargs):
            raise socket.gaierror("nope")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fail)

        verdict = await check_host("gone.example")

        assert verdict.reason == BLOCK_UNRESOLVED

    async def test_sync_twin_agrees(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", self._answers("0.0.0.0"))
        monkeypatch.setattr(ssrf, "_resolve_via_doh_sync", lambda hostname: [])

        verdict = ssrf.check_host_sync("sync-sinkhole.example")

        assert verdict.blocked is True
        assert verdict.reason == BLOCK_SINKHOLE

    async def test_fallback_is_off_unless_opted_in(self, monkeypatch):
        """A 0.0.0.0 answer is usually a blocklist doing its job; resolving past
        it by default would override policy nobody asked us to override."""
        monkeypatch.delenv("DNS_DOH_FALLBACK", raising=False)
        assert ssrf._resolvable_via_doh("example.com") is False
        assert await ssrf._resolve_via_doh("example.com") == []

    async def test_fallback_can_be_opted_into(self, monkeypatch):
        monkeypatch.setenv("DNS_DOH_FALLBACK", "1")
        assert ssrf._resolvable_via_doh("example.com") is True

    async def test_sinkhole_is_still_named_correctly_without_the_fallback(self, monkeypatch):
        """The actual defect was the misclassification; that fix must not
        depend on the opt-in."""
        monkeypatch.delenv("DNS_DOH_FALLBACK", raising=False)
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", self._answers("0.0.0.0"))

        verdict = await check_host("policy-blocked.example")

        assert verdict.reason == BLOCK_SINKHOLE
        assert verdict.reason != BLOCK_PRIVATE

    @staticmethod
    async def _no_doh(hostname):
        return []


class TestDohAnswerParsing:
    """The DoH response is untrusted input; only address records may escape it."""

    def test_extracts_a_and_aaaa_records(self):
        body = (
            '{"Status":0,"Answer":['
            '{"name":"x.example","type":1,"TTL":300,"data":"51.161.117.187"},'
            '{"name":"x.example","type":28,"TTL":300,"data":"2606:4700::1"}]}'
        )
        assert ssrf._parse_doh_answer(body) == ["51.161.117.187", "2606:4700::1"]

    def test_ignores_cname_and_other_chain_links(self):
        body = (
            '{"Status":0,"Answer":['
            '{"name":"x.example","type":5,"data":"real.example."},'
            '{"name":"real.example","type":1,"data":"93.184.216.34"}]}'
        )
        assert ssrf._parse_doh_answer(body) == ["93.184.216.34"]

    def test_a_public_resolver_echoing_the_sinkhole_is_not_an_escape_hatch(self):
        body = '{"Status":0,"Answer":[{"name":"x.example","type":1,"data":"0.0.0.0"}]}'
        assert ssrf._parse_doh_answer(body) == []

    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            "[]",
            '{"Status":3}',
            '{"Answer":"nope"}',
            '{"Answer":[{"type":1,"data":"not-an-ip"}]}',
            '{"Answer":[{"type":1}]}',
            '{"Answer":[null]}',
            '{"Answer":[{"type":1,"data":123}]}',
        ],
    )
    def test_malformed_payloads_yield_nothing(self, body):
        assert ssrf._parse_doh_answer(body) == []

    def test_answers_are_bounded(self):
        answers = ",".join(
            f'{{"type":1,"data":"93.184.216.{n}"}}' for n in range(1, 200)
        )
        parsed = ssrf._parse_doh_answer('{"Answer":[' + answers + "]}")
        assert len(parsed) <= ssrf._DOH_MAX_ANSWERS

    def test_endpoints_are_ip_literals(self):
        """A hostname here would need the very resolver we are working around,
        and would recurse back into this module."""
        import ipaddress
        from urllib.parse import urlparse

        for endpoint in ssrf._DOH_ENDPOINTS:
            host = urlparse(endpoint).hostname
            ipaddress.ip_address(host)  # raises if it is not a literal

    def test_query_url_escapes_the_hostname(self):
        urls = [url for _endpoint, url in ssrf._doh_query_urls("evil.example&type=ANY")]
        # The injected parameter must survive only as escaped text inside name=.
        assert all("evil.example%26type%3DANY" in url for url in urls)
        assert all(url.count("&type=") == 1 for url in urls)

    @pytest.mark.parametrize("hostname", ["", "x" * 254, "héllo.example"])
    def test_unresolvable_names_never_reach_a_public_resolver(self, hostname):
        assert ssrf._resolvable_via_doh(hostname) is False


class TestFallbackIsBounded:
    """The fallback is a best-effort retry of a lookup that already failed. It
    must not be able to spend the caller's whole fetch budget doing it."""

    def setup_method(self):
        clear_dns_cache()

    def test_whole_fallback_has_one_deadline(self):
        """Four queries at the per-query timeout would be four full timeouts."""
        assert ssrf._DOH_TOTAL_TIMEOUT <= ssrf._DOH_TIMEOUT + 2
        assert len(ssrf._doh_query_urls("x.example")) > 1

    async def test_unreachable_resolvers_return_within_the_deadline(self, monkeypatch):
        import time as _time

        class _Hang:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                await asyncio.sleep(float(kwargs.get("timeout", 30)))
                raise TimeoutError

        monkeypatch.setattr("wafer.AsyncSession", _Hang)
        monkeypatch.setattr(ssrf, "_DOH_TIMEOUT", 0.4)
        monkeypatch.setattr(ssrf, "_DOH_TOTAL_TIMEOUT", 0.6)

        started = _time.monotonic()
        result = await ssrf._resolve_via_doh("slow.example")
        elapsed = _time.monotonic() - started

        assert result == []
        assert elapsed < 1.2, f"fallback ran past its total deadline: {elapsed:.2f}s"

    def test_confirmed_sinkhole_is_remembered_longer_than_a_hiccup(self):
        """Re-deciding a stable condition every 2s re-runs the whole fallback
        and hammers the public resolvers on every retry."""
        assert ssrf._DNS_SINKHOLE_TTL > ssrf._DNS_NEGATIVE_TTL
        assert ssrf._DNS_SINKHOLE_TTL <= ssrf._DNS_CACHE_TTL

    async def test_the_sinkhole_marker_outlives_its_cache_entry(self, monkeypatch):
        """If the marker expired first, the same blocked host would start
        reporting a generic resolver failure instead of the real diagnosis."""

        def _sinkhole(*args, **kwargs):
            return [(None, None, None, None, ("0.0.0.0", 0))]

        async def _no_doh(hostname):
            return []

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _sinkhole)
        monkeypatch.setattr(ssrf, "_resolve_via_doh", _no_doh)

        first = await check_host("stable-sinkhole.example")
        second = await check_host("stable-sinkhole.example")  # served from cache

        assert first.reason == BLOCK_SINKHOLE
        assert second.reason == BLOCK_SINKHOLE
