"""Tests for evermcp/security/safepath.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from evermcp.security.safepath import SafePath, SecurityViolation

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def safe_dir(tmp_path: Path) -> Path:
    """Create a directory structure for testing.

    tmp_path/
      allowed/
        data/
          video.mp4
          subdir/
            file.txt
      denied/
        secret/
          key.pem
      other/
        random.txt
    """
    (tmp_path / "allowed" / "data" / "subdir").mkdir(parents=True)
    (tmp_path / "allowed" / "data" / "video.mp4").write_text("video", encoding="utf-8")
    (tmp_path / "allowed" / "data" / "subdir" / "file.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "denied" / "secret").mkdir(parents=True)
    (tmp_path / "denied" / "secret" / "key.pem").write_text("key", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "random.txt").write_text("random", encoding="utf-8")
    return tmp_path


@pytest.fixture
def sp(safe_dir: Path) -> SafePath:
    """SafePath with allowed/ and denied/secret/."""
    return SafePath(
        allowlist=[safe_dir / "allowed"],
        denied=[safe_dir / "denied" / "secret"],
    )


# ---------------------------------------------------------------------------
# Allowed paths
# ---------------------------------------------------------------------------


class TestAllowedPaths:
    def test_direct_child(self, sp: SafePath, safe_dir: Path) -> None:
        result = sp.validate(safe_dir / "allowed" / "data" / "video.mp4")
        assert result == (safe_dir / "allowed" / "data" / "video.mp4").resolve()

    def test_nested_child(self, sp: SafePath, safe_dir: Path) -> None:
        result = sp.validate(safe_dir / "allowed" / "data" / "subdir" / "file.txt")
        assert result == (safe_dir / "allowed" / "data" / "subdir" / "file.txt").resolve()

    def test_allowlist_root_itself(self, sp: SafePath, safe_dir: Path) -> None:
        result = sp.validate(safe_dir / "allowed")
        assert result == (safe_dir / "allowed").resolve()

    def test_nonexistent_file_in_allowed(self, sp: SafePath, safe_dir: Path) -> None:
        """Nonexistent paths under allowlist should still pass validation."""
        result = sp.validate(safe_dir / "allowed" / "data" / "nonexistent.mp4")
        assert result == (safe_dir / "allowed" / "data" / "nonexistent.mp4").resolve()


# ---------------------------------------------------------------------------
# Denied paths
# ---------------------------------------------------------------------------


class TestDeniedPaths:
    def test_denied_child(self, sp: SafePath, safe_dir: Path) -> None:
        with pytest.raises(SecurityViolation, match="denied list"):
            sp.validate(safe_dir / "denied" / "secret" / "key.pem")

    def test_denied_root(self, sp: SafePath, safe_dir: Path) -> None:
        with pytest.raises(SecurityViolation, match="denied list"):
            sp.validate(safe_dir / "denied" / "secret")

    def test_denied_nonexistent(self, sp: SafePath, safe_dir: Path) -> None:
        """Nonexistent paths under denied should still be blocked."""
        with pytest.raises(SecurityViolation, match="denied list"):
            sp.validate(safe_dir / "denied" / "secret" / "new_file.txt")


# ---------------------------------------------------------------------------
# Not in allowlist
# ---------------------------------------------------------------------------


class TestNotInAllowlist:
    def test_outside_allowlist(self, sp: SafePath, safe_dir: Path) -> None:
        with pytest.raises(SecurityViolation, match="not in allowlist"):
            sp.validate(safe_dir / "other" / "random.txt")

    def test_absolute_system_path(self, sp: SafePath) -> None:
        with pytest.raises(SecurityViolation, match="not in allowlist"):
            sp.validate("C:\\Windows\\System32")

    def test_root_path(self, sp: SafePath) -> None:
        with pytest.raises(SecurityViolation, match="not in allowlist"):
            sp.validate("C:\\")


# ---------------------------------------------------------------------------
# Path traversal attacks
# ---------------------------------------------------------------------------


class TestTraversalAttacks:
    def test_dotdot_from_allowed_to_denied(self, sp: SafePath, safe_dir: Path) -> None:
        """../ traversal from an allowed dir to a denied dir must be blocked.

        allowed/data/../../denied/secret/key.pem
        After resolve: should point to denied/secret/key.pem
        """
        with pytest.raises(SecurityViolation, match="denied list"):
            sp.validate(
                safe_dir / "allowed" / "data" / ".." / ".." / "denied" / "secret" / "key.pem"
            )

    def test_dotdot_from_allowed_to_outside(self, sp: SafePath, safe_dir: Path) -> None:
        """../ traversal from an allowed dir to outside must be blocked.

        allowed/data/../../other/random.txt
        After resolve: should point to other/random.txt (not in allowlist)
        """
        with pytest.raises(SecurityViolation, match="not in allowlist"):
            sp.validate(safe_dir / "allowed" / "data" / ".." / ".." / "other" / "random.txt")

    def test_dotdot_beyond_root(self, sp: SafePath, safe_dir: Path) -> None:
        """Multiple ../ that go above the temp root must be blocked."""
        with pytest.raises(SecurityViolation):
            sp.validate(safe_dir / "allowed" / ".." / ".." / ".." / "etc" / "passwd")

    def test_dotdot_in_middle(self, sp: SafePath, safe_dir: Path) -> None:
        """../ in the middle of a path that still stays in allowed should pass."""
        result = sp.validate(safe_dir / "allowed" / "data" / "subdir" / ".." / "video.mp4")
        assert result == (safe_dir / "allowed" / "data" / "video.mp4").resolve()

    def test_current_dot_stays(self, sp: SafePath, safe_dir: Path) -> None:
        """./ should not affect validation."""
        result = sp.validate(safe_dir / "allowed" / "." / "data" / "video.mp4")
        assert result == (safe_dir / "allowed" / "data" / "video.mp4").resolve()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_allowlist_blocks_everything(self, safe_dir: Path) -> None:
        sp = SafePath(allowlist=[])
        with pytest.raises(SecurityViolation, match="not in allowlist"):
            sp.validate(safe_dir / "anything")

    def test_empty_string_path(self, safe_dir: Path) -> None:
        sp = SafePath(allowlist=[safe_dir])
        # Empty string resolves to cwd, which is not in allowlist
        with pytest.raises(SecurityViolation):
            sp.validate("")

    def test_path_with_tilde(self, tmp_path: Path) -> None:
        """~ expansion should work correctly."""
        sp = SafePath(allowlist=[tmp_path])
        # This won't match because ~ expands to user home, not tmp_path
        with pytest.raises(SecurityViolation):
            sp.validate("~/some_file")

    def test_same_path_in_allowlist_and_denied_denied_wins(self, safe_dir: Path) -> None:
        """If a path is in both allowlist and denied, denied takes priority."""
        sp = SafePath(
            allowlist=[safe_dir / "allowed"],
            denied=[safe_dir / "allowed" / "data"],
        )
        with pytest.raises(SecurityViolation, match="denied list"):
            sp.validate(safe_dir / "allowed" / "data" / "video.mp4")

    def test_string_path_input(self, sp: SafePath, safe_dir: Path) -> None:
        """String input should work the same as Path input."""
        result = sp.validate(str(safe_dir / "allowed" / "data" / "video.mp4"))
        assert result == (safe_dir / "allowed" / "data" / "video.mp4").resolve()

    def test_properties(self, sp: SafePath, safe_dir: Path) -> None:
        assert len(sp.allowlist) == 1
        assert len(sp.denied) == 1
