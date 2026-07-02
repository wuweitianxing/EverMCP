"""SafePath — filesystem path validation against allowlist and denied list.

Security model:
    - AI client input is treated as untrusted
    - All file paths must pass through SafePath.validate() before use
    - Path is resolved (expanduser + resolve) BEFORE checking, so ../ traversal
      cannot escape the allowlist
    - Denied paths are checked FIRST (higher priority than allowlist)
"""

from __future__ import annotations

from pathlib import Path


class SecurityViolation(Exception):  # noqa: N818
    """Raised when a path violates the security policy."""

    pass


class SafePath:
    """Validates filesystem paths against an allowlist and a denied list.

    Usage:
        sp = SafePath(allowlist=[Path("~/data")], denied=[Path("~/.ssh")])
        safe = sp.validate("~/data/video.mp4")   # returns resolved Path
        sp.validate("~/.ssh/id_rsa")              # raises SecurityViolation
        sp.validate("/etc/passwd")                # raises SecurityViolation
    """

    def __init__(
        self,
        allowlist: list[Path | str],
        denied: list[Path | str] | None = None,
    ) -> None:
        self._allowlist = [_resolve(p) for p in allowlist]
        self._denied = [_resolve(p) for p in (denied or [])]

    def validate(self, path: str | Path) -> Path:
        """Validate a path against the security policy.

        Returns the resolved absolute Path if allowed.
        Raises SecurityViolation if the path is denied or not in allowlist.
        """
        p = _resolve(path)

        # Check denied list first (higher priority)
        for denied in self._denied:
            if _is_under(p, denied):
                raise SecurityViolation(f"Path in denied list: {p}")

        # Check allowlist
        for allowed in self._allowlist:
            if _is_under(p, allowed):
                return p

        raise SecurityViolation(f"Path not in allowlist: {p}")

    @property
    def allowlist(self) -> list[Path]:
        return list(self._allowlist)

    @property
    def denied(self) -> list[Path]:
        return list(self._denied)


def _resolve(p: Path | str) -> Path:
    """Expand ~ and resolve to absolute path (eliminates symlinks and ..)."""
    return Path(p).expanduser().resolve()


def _is_under(path: Path, root: Path) -> bool:
    """Check if path is equal to or under root.

    Uses Path.relative_to() which raises ValueError if path is not under root.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
