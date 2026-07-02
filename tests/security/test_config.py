"""Tests for evermcp/security/config.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from evermcp.security.config import Config

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_log_level_is_info(self) -> None:
        cfg = Config()
        assert cfg.log_level == "INFO"

    def test_default_log_file(self) -> None:
        cfg = Config()
        assert cfg.log_file == Path("~/.evermcp/evermcp.log").expanduser().resolve()

    def test_default_ffmpeg_binary(self) -> None:
        cfg = Config()
        assert cfg.ffmpeg_binary == "ffmpeg"

    def test_default_ffmpeg_timeout(self) -> None:
        cfg = Config()
        assert cfg.ffmpeg_timeout_s == 600

    def test_default_filesystem_allowlist(self) -> None:
        cfg = Config()
        assert cfg.filesystem_allowlist == []

    def test_default_network_allowlist(self) -> None:
        cfg = Config()
        assert cfg.network_allowlist == []

    def test_default_denied_paths(self) -> None:
        cfg = Config()
        assert cfg.denied_paths == []


# ---------------------------------------------------------------------------
# Constructor overrides
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_custom_log_level(self) -> None:
        cfg = Config(log_level="debug")
        assert cfg.log_level == "DEBUG"

    def test_custom_log_level_normalized(self) -> None:
        cfg = Config(log_level="warning")
        assert cfg.log_level == "WARNING"

    def test_custom_ffmpeg_timeout(self) -> None:
        cfg = Config(ffmpeg_timeout_s=300)
        assert cfg.ffmpeg_timeout_s == 300

    def test_tilde_expansion_in_allowlist(self) -> None:
        cfg = Config(filesystem_allowlist=["~/data", "~/Downloads"])
        resolved = cfg.filesystem_allowlist
        assert all(not str(p).startswith("~") for p in resolved)
        assert all(p.is_absolute() for p in resolved)

    def test_tilde_expansion_in_denied_paths(self) -> None:
        cfg = Config(denied_paths=["~/.ssh"])
        resolved = cfg.denied_paths
        assert len(resolved) == 1
        assert not str(resolved[0]).startswith("~")
        assert resolved[0].is_absolute()


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------


class TestTomlLoading:
    def test_load_from_toml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config.load() reads a TOML file and overrides defaults."""
        config_dir = tmp_path / ".evermcp"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text(
            "[general]\n"
            'log_level = "DEBUG"\n'
            'log_file = "~/test.log"\n'
            "\n"
            "[ffmpeg]\n"
            'binary = "/usr/local/bin/ffmpeg"\n'
            "default_timeout_s = 120\n"
            "\n"
            "[security]\n"
            'filesystem_allowlist = ["~/data", "~/Downloads"]\n'
            'network_allowlist = ["github.com", "pypi.org"]\n'
            'denied_paths = ["~/.ssh", "~/.aws"]\n',
            encoding="utf-8",
        )

        # Point Config.load to our temp config
        cfg = Config.load(config_file=str(config_file))

        assert cfg.log_level == "DEBUG"
        assert cfg.log_file == Path("~/test.log").expanduser().resolve()
        assert cfg.ffmpeg_binary == "/usr/local/bin/ffmpeg"
        assert cfg.ffmpeg_timeout_s == 120
        assert len(cfg.filesystem_allowlist) == 2
        assert cfg.network_allowlist == ["github.com", "pypi.org"]
        assert len(cfg.denied_paths) == 2

    def test_missing_toml_file_returns_defaults(self, tmp_path: Path) -> None:
        """Config.load() with a nonexistent file returns defaults."""
        cfg = Config.load(config_file=str(tmp_path / "nonexistent.toml"))
        assert cfg.log_level == "INFO"
        assert cfg.ffmpeg_binary == "ffmpeg"

    def test_partial_toml_overrides_only_specified(self, tmp_path: Path) -> None:
        """A TOML file with only some fields overrides only those fields."""
        config_file = tmp_path / "partial.toml"
        config_file.write_text(
            '[ffmpeg]\nbinary = "ffmpeg-7"\n',
            encoding="utf-8",
        )

        cfg = Config.load(config_file=str(config_file))

        assert cfg.log_level == "INFO"  # default
        assert cfg.ffmpeg_binary == "ffmpeg-7"  # overridden

    def test_invalid_toml_returns_defaults(self, tmp_path: Path) -> None:
        """A malformed TOML file is silently ignored."""
        config_file = tmp_path / "bad.toml"
        config_file.write_text("this is not valid toml {{{", encoding="utf-8")

        cfg = Config.load(config_file=str(config_file))
        assert cfg.log_level == "INFO"


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    def test_env_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_LOG_LEVEL", "debug")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.log_level == "DEBUG"

    def test_env_ffmpeg_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_FFMPEG_BINARY", "/opt/ffmpeg")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.ffmpeg_binary == "/opt/ffmpeg"

    def test_env_ffmpeg_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_FFMPEG_TIMEOUT_S", "900")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.ffmpeg_timeout_s == 900

    def test_env_fs_allowlist_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_FS_ALLOWLIST", "~/data,~/Downloads")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert len(cfg.filesystem_allowlist) == 2
        assert all(not str(p).startswith("~") for p in cfg.filesystem_allowlist)

    def test_env_net_allowlist_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_NET_ALLOWLIST", "github.com, pypi.org")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.network_allowlist == ["github.com", "pypi.org"]

    def test_env_net_allowlist_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty string should not add empty entries."""
        monkeypatch.setenv("EVERMCP_NET_ALLOWLIST", "")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.network_allowlist == []

    def test_env_overrides_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env vars take precedence over TOML values."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[ffmpeg]\nbinary = "ffmpeg-from-toml"\ndefault_timeout_s = 100\n',
            encoding="utf-8",
        )

        monkeypatch.setenv("EVERMCP_FFMPEG_BINARY", "ffmpeg-from-env")
        monkeypatch.setenv("EVERMCP_FFMPEG_TIMEOUT_S", "200")

        cfg = Config.load(config_file=str(config_file))

        assert cfg.ffmpeg_binary == "ffmpeg-from-env"  # env wins
        assert cfg.ffmpeg_timeout_s == 200  # env wins


# ---------------------------------------------------------------------------
# Full load chain: defaults → TOML → env
# ---------------------------------------------------------------------------


class TestFullLoadChain:
    def test_defaults_then_toml_then_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the full override chain: defaults → TOML → env vars."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[general]\n"
            'log_level = "WARNING"\n'
            "\n"
            "[ffmpeg]\n"
            'binary = "ffmpeg-toml"\n'
            "default_timeout_s = 300\n"
            "\n"
            "[security]\n"
            'filesystem_allowlist = ["~/toml-data"]\n'
            'network_allowlist = ["toml.com"]\n',
            encoding="utf-8",
        )

        # Env overrides only log_level and ffmpeg_binary
        monkeypatch.setenv("EVERMCP_LOG_LEVEL", "ERROR")
        monkeypatch.setenv("EVERMCP_FFMPEG_BINARY", "ffmpeg-env")
        # No EVERMCP_FFMPEG_TIMEOUT_S set → TOML value should stick

        cfg = Config.load(config_file=str(config_file))

        # log_level: default INFO → TOML WARNING → env ERROR
        assert cfg.log_level == "ERROR"
        # ffmpeg_binary: default ffmpeg → TOML ffmpeg-toml → env ffmpeg-env
        assert cfg.ffmpeg_binary == "ffmpeg-env"
        # ffmpeg_timeout_s: default 600 → TOML 300 (no env override)
        assert cfg.ffmpeg_timeout_s == 300
        # filesystem_allowlist: default [] → TOML [~/toml-data] (no env override)
        assert len(cfg.filesystem_allowlist) == 1
        # network_allowlist: default [] → TOML [toml.com] (no env override)
        assert cfg.network_allowlist == ["toml.com"]
