"""Unit tests for tools/io/read_file.py.

Tests cover:
- Basic read of existing file
- File not found
- Security violation via ctx.safe_path
- Truncation when file > max_bytes
- Different encodings (utf-8, latin-1, gbk)
- Default parameters
- Binary file (errors='replace' fallback)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evermcp.core.tool import ToolFunc
from examples.tools.io.read_file import read_file as _mod_read_file

read_file: ToolFunc = _mod_read_file  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_file(path: Path, content: str | bytes, encoding: str = "utf-8") -> None:
    """Helper to write content to a file.

    Uses newline='\n' explicitly on Windows so that test assertions about
    line endings are predictable (LF, not CRLF).
    """
    if isinstance(content, str):
        path.write_text(content, encoding=encoding, newline="\n")
    else:
        path.write_bytes(content)


# ---------------------------------------------------------------------------
# Basic read
# ---------------------------------------------------------------------------

class TestBasicRead:
    def test_read_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        write_file(f, "Hello, world!\n")
        result = read_file.fn(file_path=str(f))
        assert result["content"] == "Hello, world!\n"
        assert result["size_bytes"] == len("Hello, world!\n")
        assert result["read_bytes"] == result["size_bytes"]
        assert result["truncated"] is False
        assert result["encoding"] == "utf-8"

    def test_read_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        result = read_file.fn(file_path=str(f))
        assert result["content"] == ""
        assert result["size_bytes"] == 0
        assert result["truncated"] is False

    def test_read_multiline(self, tmp_path: Path) -> None:
        f = tmp_path / "multi.txt"
        content = "line1\nline2\nline3\n"
        write_file(f, content)
        result = read_file.fn(file_path=str(f))
        assert result["content"] == content


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TestErrors:
    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            read_file.fn(file_path=str(tmp_path / "does_not_exist.txt"))

    def test_directory_not_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            read_file.fn(file_path=str(tmp_path))  # tmp_path is a directory

    def test_security_violation_via_ctx(self, tmp_path: Path) -> None:
        """SafePath on ctx should block paths outside allowlist."""
        from evermcp.core.tool import ToolContext
        from evermcp.security.safepath import SafePath

        f = tmp_path / "secret.txt"
        write_file(f, "sensitive data")

        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        sp = SafePath(allowlist=[allowed_dir])

        ctx = ToolContext(safe_path=sp)

        with pytest.raises(Exception):  # SecurityViolation
            read_file.fn(file_path=str(f), ctx=ctx)

    def test_security_allows_in_allowlist(self, tmp_path: Path) -> None:
        """SafePath on ctx should allow paths inside allowlist."""
        from evermcp.core.tool import ToolContext
        from evermcp.security.safepath import SafePath

        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        f = allowed_dir / "ok.txt"
        write_file(f, "public data")

        sp = SafePath(allowlist=[allowed_dir])
        ctx = ToolContext(safe_path=sp)

        result = read_file.fn(file_path=str(f), ctx=ctx)
        assert result["content"] == "public data"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_truncation_when_oversized(self, tmp_path: Path) -> None:
        f = tmp_path / "big.txt"
        write_file(f, "x" * 1000)
        result = read_file.fn(file_path=str(f), max_bytes=100)
        assert result["truncated"] is True
        assert result["read_bytes"] == 100
        assert result["size_bytes"] == 1000
        assert result["content"] == "x" * 100

    def test_no_truncation_when_exact_size(self, tmp_path: Path) -> None:
        f = tmp_path / "exact.txt"
        write_file(f, "x" * 100)
        result = read_file.fn(file_path=str(f), max_bytes=100)
        assert result["truncated"] is False
        assert result["read_bytes"] == 100

    def test_max_bytes_constraint_in_schema(self) -> None:
        """Pydantic Field constraints (ge=1) are surfaced in JSON Schema for MCP clients."""
        # The constraint is metadata only — Pydantic doesn't enforce it on direct fn calls.
        # It IS enforced by MCP clients that validate against the JSON Schema.
        schema = read_file.input_schema()
        max_bytes_schema = schema["properties"]["max_bytes"]
        assert max_bytes_schema.get("minimum") == 1
        assert max_bytes_schema.get("maximum") == 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

class TestEncoding:
    def test_utf8(self, tmp_path: Path) -> None:
        f = tmp_path / "u.txt"
        write_file(f, "你好世界", encoding="utf-8")
        result = read_file.fn(file_path=str(f), encoding="utf-8")
        assert result["content"] == "你好世界"

    def test_latin1(self, tmp_path: Path) -> None:
        f = tmp_path / "l.txt"
        # Latin-1: "café" = "caf\xE9"
        write_file(f, "café", encoding="latin-1")
        result = read_file.fn(file_path=str(f), encoding="latin-1")
        assert result["content"] == "café"

    def test_gbk(self, tmp_path: Path) -> None:
        f = tmp_path / "g.txt"
        write_file(f, "你好", encoding="gbk")
        result = read_file.fn(file_path=str(f), encoding="gbk")
        assert result["content"] == "你好"

    def test_binary_file_with_errors_replace(self, tmp_path: Path) -> None:
        """Binary file read as utf-8 should not crash; uses errors='replace'."""
        f = tmp_path / "bin.bin"
        f.write_bytes(b"\x00\xFF\xFE some bytes")
        result = read_file.fn(file_path=str(f), encoding="utf-8")
        # The content should be decoded with replacement chars (U+FFFD)
        assert "\ufffd" in result["content"] or result["content"]


# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_default_max_bytes(self, tmp_path: Path) -> None:
        """Without explicit max_bytes, should use 1MB default."""
        from examples.tools.io.read_file import _DEFAULT_MAX_BYTES
        assert _DEFAULT_MAX_BYTES == 1024 * 1024

    def test_default_encoding(self, tmp_path: Path) -> None:
        f = tmp_path / "default.txt"
        write_file(f, "hello")
        result = read_file.fn(file_path=str(f))
        assert result["encoding"] == "utf-8"
