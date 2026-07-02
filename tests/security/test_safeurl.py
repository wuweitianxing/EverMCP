"""Tests for SafeURL — URL validation + SSRF defense."""

from __future__ import annotations

import pytest

from evermcp.security.safepath import SecurityViolation
from evermcp.security.safeurl import SafeURL

# ---------------------------------------------------------------------------
# Scheme validation
# ---------------------------------------------------------------------------


class TestSchemeValidation:
    def test_http_allowed(self) -> None:
        su = SafeURL()
        scheme, host = su.validate("http://example.com/")
        assert scheme == "http"
        assert host == "example.com"

    def test_https_allowed(self) -> None:
        su = SafeURL()
        scheme, host = su.validate("https://example.com/")
        assert scheme == "https"
        assert host == "example.com"

    @pytest.mark.parametrize("scheme", ["file", "ftp", "gopher", "javascript", "data", ""])
    def test_disallowed_schemes_rejected(self, scheme: str) -> None:
        su = SafeURL()
        with pytest.raises(SecurityViolation, match="scheme"):
            su.validate(f"{scheme}://example.com/")


# ---------------------------------------------------------------------------
# Hostname presence
# ---------------------------------------------------------------------------


class TestHostnamePresence:
    def test_missing_hostname_rejected(self) -> None:
        su = SafeURL()
        with pytest.raises(SecurityViolation, match="hostname"):
            su.validate("http://")


# ---------------------------------------------------------------------------
# Loopback hostnames (always rejected)
# ---------------------------------------------------------------------------


class TestLoopbackHostnames:
    @pytest.mark.parametrize(
        "hostname",
        [
            "localhost",
            "LOCALHOST",  # case-insensitive
            "localhost.localdomain",
            "ip6-localhost",
            "ip6-loopback",
        ],
    )
    def test_blocked_hostname_rejected(self, hostname: str) -> None:
        su = SafeURL()
        with pytest.raises(SecurityViolation, match="Loopback"):
            su.validate(f"http://{hostname}/")

    def test_blocked_hostname_rejected_even_with_allowlist(self) -> None:
        """Allowlist cannot override hardcoded loopback block."""
        su = SafeURL(allowlist=["localhost"])  # user shoots self in foot
        with pytest.raises(SecurityViolation, match="Loopback"):
            su.validate("http://localhost/")


# ---------------------------------------------------------------------------
# Private/loopback/link-local IP rejection
# ---------------------------------------------------------------------------


class TestPrivateIPRejection:
    @pytest.mark.parametrize(
        "url,label",
        [
            ("http://127.0.0.1/", "loopback IPv4"),
            ("http://127.255.255.254/", "loopback range edge"),
            ("http://10.0.0.1/", "private 10/8"),
            ("http://172.16.0.1/", "private 172.16/12"),
            ("http://192.168.1.1/", "private 192.168/16"),
            ("http://169.254.169.254/latest/meta-data", "link-local (AWS metadata)"),
            ("http://0.0.0.0/", "unspecified"),
            ("http://224.0.0.1/", "multicast"),
            ("http://[::1]/", "loopback IPv6"),
            ("http://[fc00::1]/", "unique local IPv6"),
            ("http://[fe80::1]/", "link-local IPv6"),
        ],
    )
    def test_private_ip_rejected(self, url: str, label: str) -> None:  # noqa: ARG002
        su = SafeURL()
        with pytest.raises(SecurityViolation, match="IP not allowed"):
            su.validate(url)

    def test_public_ip_allowed_without_allowlist(self) -> None:
        """Public IPs pass when no allowlist is set (default behavior)."""
        su = SafeURL()
        # 1.1.1.1 is a public IP (Cloudflare DNS)
        scheme, host = su.validate("http://1.1.1.1/")
        assert scheme == "http"
        assert host == "1.1.1.1"


# ---------------------------------------------------------------------------
# Allowlist behavior
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_no_allowlist_accepts_any_hostname(self) -> None:
        su = SafeURL()
        scheme, host = su.validate("https://any-random-site.example/")
        assert host == "any-random-site.example"

    def test_exact_match_allowed(self) -> None:
        su = SafeURL(allowlist=["github.com"])
        scheme, host = su.validate("https://github.com/foo")
        assert host == "github.com"

    def test_subdomain_match_allowed(self) -> None:
        """api.github.com should match when github.com is in allowlist."""
        su = SafeURL(allowlist=["github.com"])
        scheme, host = su.validate("https://api.github.com/repos")
        assert host == "api.github.com"

    def test_allowlist_case_insensitive(self) -> None:
        su = SafeURL(allowlist=["GitHub.com"])
        scheme, host = su.validate("https://github.com/")
        assert host == "github.com"

    def test_non_matching_hostname_rejected(self) -> None:
        su = SafeURL(allowlist=["github.com"])
        with pytest.raises(SecurityViolation, match="not in allowlist"):
            su.validate("https://evil.com/")

    def test_suffix_match_only_for_subdomains(self) -> None:
        """evil-github.com should NOT match github.com (suffix, not subdomain)."""
        su = SafeURL(allowlist=["github.com"])
        with pytest.raises(SecurityViolation, match="not in allowlist"):
            su.validate("https://evil-github.com/")

    def test_empty_allowlist_treated_as_no_allowlist(self) -> None:
        """Edge case: empty list should not reject all hostnames."""
        su = SafeURL(allowlist=[])
        # Should not raise — empty allowlist means "no allowlist"
        scheme, host = su.validate("https://example.com/")
        assert host == "example.com"


# ---------------------------------------------------------------------------
# Returns (scheme, hostname) tuple
# ---------------------------------------------------------------------------


class TestReturnValue:
    def test_strips_port_from_hostname(self) -> None:
        """urlparse.hostname already strips port, so result should be host only."""
        su = SafeURL()
        scheme, host = su.validate("https://example.com:8080/path")
        assert scheme == "https"
        assert host == "example.com"

    def test_ipv6_hostname_bracket_stripped(self) -> None:
        """http://[::1]/ → hostname should be '::1' (without brackets) for ip_address() to parse."""
        su = SafeURL()
        # ::1 is loopback, should be rejected
        with pytest.raises(SecurityViolation, match="IP not allowed"):
            su.validate("http://[::1]/")


# ---------------------------------------------------------------------------
# allowlist property
# ---------------------------------------------------------------------------


class TestAllowlistProperty:
    def test_allowlist_lowercased(self) -> None:
        su = SafeURL(allowlist=["GitHub.COM", "PYPI.org"])
        assert su.allowlist == ["github.com", "pypi.org"]

    def test_allowlist_empty_default(self) -> None:
        su = SafeURL()
        assert su.allowlist == []
