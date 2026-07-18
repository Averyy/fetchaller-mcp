"""Tests for SSRF protection."""

import socket

import pytest

from fetchaller.security import ssrf
from fetchaller.security.ssrf import clear_dns_cache, resolve_and_check
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
        assert ips == []

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
