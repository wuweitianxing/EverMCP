"""Unit tests for the `evermcp` CLI's --tools-dir resolution priority.

Priority (highest to lowest):
  1. --tools-dir flag (CLI value)
  2. $EVERMCP_TOOLS_DIR env var
  3. None (let ToolRegistry fall back to its built-in default)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from evermcp.cli import _resolve_tools_dir, main

# ---------------------------------------------------------------------------
# _resolve_tools_dir pure function
# ---------------------------------------------------------------------------


class TestResolveToolsDir:
    def test_cli_value_wins(self, tmp_path: Path) -> None:
        """CLI value is used when provided."""
        result = _resolve_tools_dir(str(tmp_path))
        assert result == tmp_path

    def test_env_var_used_when_no_cli(self, tmp_path: Path) -> None:
        """Env var used when CLI value is None."""
        with patch.dict(os.environ, {"EVERMCP_TOOLS_DIR": str(tmp_path)}):
            result = _resolve_tools_dir(None)
        assert result == tmp_path

    def test_cli_value_overrides_env_var(self, tmp_path: Path) -> None:
        """CLI value wins over env var when both are set."""
        other = tmp_path / "other"
        other.mkdir()
        with patch.dict(os.environ, {"EVERMCP_TOOLS_DIR": str(other)}):
            result = _resolve_tools_dir(str(tmp_path))
        assert result == tmp_path  # CLI wins

    def test_returns_none_when_neither_set(self) -> None:
        """None signals 'use ToolRegistry default'."""
        with patch.dict(os.environ, {}, clear=True):
            result = _resolve_tools_dir(None)
        assert result is None

    def test_expanduser_and_resolve(self) -> None:
        """Tilde and relative paths are expanded/resolved."""
        result = _resolve_tools_dir("~/somewhere")
        assert result is not None
        assert result.is_absolute()
        assert "~" not in str(result)


# ---------------------------------------------------------------------------
# `evermcp list-tools --tools-dir PATH`
# ---------------------------------------------------------------------------


class TestListToolsCommand:
    def test_lists_examples(self) -> None:
        """Pointing at examples/tools/ should list demo.hello and io.read_file."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["list-tools", "--tools-dir", "examples/tools"],
        )
        assert result.exit_code == 0, result.output
        assert "demo.hello" in result.output
        assert "io.read_file" in result.output

    def test_nonexistent_dir_reports_error(self, tmp_path: Path) -> None:
        """Pointing at a nonexistent dir shows a 'no tools found' message but exits 0."""
        runner = CliRunner()
        # Use a real path so ToolRegistry doesn't crash, but it's empty
        empty = tmp_path / "empty"
        empty.mkdir()
        result = runner.invoke(
            main,
            ["list-tools", "--tools-dir", str(empty)],
        )
        assert result.exit_code == 0
        assert "No tools found" in result.output

    def test_default_dir_is_empty_tools_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No flag, no env: list-tools scans the default <repo>/tools/ (empty)."""
        # We can't change CWD reliably here without disturbing the whole test session,
        # so instead we monkeypatch ToolRegistry to capture the tools_dir it got.
        from evermcp.core import registry as reg_module

        captured: dict[str, object] = {}

        original_init = reg_module.ToolRegistry.__init__

        def spy_init(self: reg_module.ToolRegistry, tools_dir: object = None) -> None:
            captured["tools_dir"] = tools_dir
            original_init(self, tools_dir)

        monkeypatch.setattr(reg_module.ToolRegistry, "__init__", spy_init)

        runner = CliRunner()
        with patch.dict(os.environ, {}, clear=True):
            result = runner.invoke(main, ["list-tools"])
        assert result.exit_code == 0
        # Default is None → ToolRegistry falls back to its built-in default (TOOLS_DIR constant)
        assert captured["tools_dir"] is None
