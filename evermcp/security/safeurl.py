"""SafeURL — URL validation against allowlist + SSRF defense.

Security model (DESIGN.md §Security Model):
- http/https only (no file://, no javascript:, no gopher://)
- Always reject: localhost, known loopback hostnames, private/loopback/link-local/
  reserved/multicast/unspecified IPs
- If `allowlist` is configured: hostname must match (exact OR a subdomain)
- v1 limitation: only checks the literal hostname. A hostname like `evil.com`
  that resolves to 127.0.0.1 will pass validation (DNS rebinding risk).
  Future hardening: resolve at validate-time and re-check the resolved IP.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from evermcp.security.safepath import SecurityViolation

# Hostnames that always resolve to loopback. Always rejected.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
})


class SafeURL:
    """Validates URLs against an allowlist and a default-deny IP policy.

    Usage:
        su = SafeURL()                            # no allowlist: only default-deny
        su.validate("https://example.com/")        # OK
        su.validate("http://127.0.0.1/")          # raises SecurityViolation
        su.validate("http://localhost/")          # raises SecurityViolation

        su = SafeURL(allowlist=["github.com"])
        su.validate("https://github.com/")        # OK (exact)
        su.validate("https://api.github.com/")    # OK (subdomain)
        su.validate("https://evil.com/")          # raises SecurityViolation
    """

    def __init__(self, allowlist: list[str] | None = None) -> None:
        """allowlist: list of allowed hostnames (e.g. ["github.com", "pypi.org"]).
        None or empty = no allowlist (only default-deny for private/loopback IPs).
        """
        self._allowlist = [a.lower() for a in (allowlist or [])]

    @property
    def allowlist(self) -> list[str]:
        return list(self._allowlist)

    def validate(self, url: str) -> tuple[str, str]:
        """Validate a URL against the security policy.

        Returns (scheme, hostname) on success.
        Raises SecurityViolation on any rejection.
        """
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            raise SecurityViolation(
                f"Invalid URL scheme: {parsed.scheme!r}. Only http and https are allowed."
            )

        hostname = parsed.hostname
        if not hostname:
            raise SecurityViolation("URL missing hostname")

        hostname_lower = hostname.lower()

        if hostname_lower in _BLOCKED_HOSTNAMES:
            raise SecurityViolation(f"Loopback hostname not allowed: {hostname}")

        # Reject literal private/loopback/link-local/reserved/multicast/unspecified IPs
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            # Not a literal IP — hostname; will be checked against allowlist below
            pass
        else:
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise SecurityViolation(
                    f"Private/loopback/link-local/reserved/multicast IP not allowed: {ip}"
                )

        # Allowlist check (only if an allowlist is configured)
        if self._allowlist and not self._matches_allowlist(hostname_lower):
            raise SecurityViolation(f"Host not in allowlist: {hostname}")

        return parsed.scheme, hostname

    def _matches_allowlist(self, hostname: str) -> bool:
        """Match hostname against allowlist (exact OR subdomain).

        `github.com` in allowlist matches both `github.com` and `api.github.com`.
        """
        for allowed in self._allowlist:
            if hostname == allowed or hostname.endswith("." + allowed):
                return True
        return False
