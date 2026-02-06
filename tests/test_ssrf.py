"""Tests for SSRF protection."""

import pytest

from fetchaller.security.ssrf import clear_dns_cache, is_private_host_sync as is_private_host


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
