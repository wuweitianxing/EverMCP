"""EverMCP configuration loading.

Loading order: defaults → TOML file (~/.evermcp/config.toml) → env vars (EVERMCP_*) → CLI flags.

Env var mapping:
    EVERMCP_LOG_LEVEL        → log_level
    EVERMCP_FFMPEG_BINARY    → ffmpeg_binary
    EVERMCP_FFMPEG_TIMEOUT_S → ffmpeg_timeout_s
    EVERMCP_FS_ALLOWLIST     → filesystem_allowlist (comma-separated)
    EVERMCP_NET_ALLOWLIST    → network_allowlist (comma-separated)
    EVERMCP_TOOLS_DIR        → tools directory (CLI --tools-dir flag overrides)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# tomllib is stdlib in 3.11+; fall back to tomli for 3.10
try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

_DEFAULT_CONFIG_DIR = Path("~/.evermcp").expanduser()
_DEFAULT_CONFIG_FILE = _DEFAULT_CONFIG_DIR / "config.toml"


class Config:
    """EverMCP configuration with layered loading (defaults → TOML → env vars).

    v1.0 scope: this config object only carries **policy** (allowlists, binary
    locations) and **logging**. Tool-specific config (e.g. TTS model paths,
    per-tool timeouts) lives in tool code — EverMCP doesn't ship a registry of
    known tools, so it can't pre-allocate fields for them.
    """

    def __init__(
        self,
        log_level: str = "INFO",
        log_file: Path | str = "",
        ffmpeg_binary: str = "ffmpeg",
        ffmpeg_timeout_s: int = 600,
        filesystem_allowlist: list[Path | str] | None = None,
        network_allowlist: list[str] | None = None,
        denied_paths: list[Path | str] | None = None,
    ) -> None:
        self.log_level = log_level.upper()
        self.log_file = _resolve_path(log_file) if log_file else _DEFAULT_CONFIG_DIR / "evermcp.log"
        self.ffmpeg_binary = ffmpeg_binary
        self.ffmpeg_timeout_s = ffmpeg_timeout_s
        self.filesystem_allowlist = [_resolve_path(p) for p in (filesystem_allowlist or [])]
        self.network_allowlist = network_allowlist or []
        self.denied_paths = [_resolve_path(p) for p in (denied_paths or [])]

    # ------------------------------------------------------------------
    # Load: defaults → TOML → env vars
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_file: Path | str | None = None) -> Config:
        """Load configuration with layered override.

        1. Start with defaults
        2. Override from TOML file (~/.evermcp/config.toml, or custom path)
        3. Override from EVERMCP_* environment variables
        """
        # 1. Defaults
        cfg = cls()

        # 2. TOML file
        toml_path = _resolve_path(config_file) if config_file else _DEFAULT_CONFIG_FILE
        if toml_path.is_file():
            cfg = cls._apply_toml(cfg, toml_path)

        # 3. Environment variables
        cfg = cls._apply_env(cfg)

        return cfg

    @classmethod
    def _apply_toml(cls, cfg: Config, path: Path) -> Config:
        """Override config values from a TOML file."""
        try:
            with open(path, "rb") as f:
                data: dict[str, Any] = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return cfg

        general = data.get("general", {})
        ffmpeg = data.get("ffmpeg", {})
        security = data.get("security", {})

        if "log_level" in general:
            cfg.log_level = general["log_level"].upper()
        if "log_file" in general:
            cfg.log_file = _resolve_path(general["log_file"])
        if "binary" in ffmpeg:
            cfg.ffmpeg_binary = ffmpeg["binary"]
        if "default_timeout_s" in ffmpeg:
            cfg.ffmpeg_timeout_s = int(ffmpeg["default_timeout_s"])
        if "filesystem_allowlist" in security:
            cfg.filesystem_allowlist = [_resolve_path(p) for p in security["filesystem_allowlist"]]
        if "network_allowlist" in security:
            cfg.network_allowlist = list(security["network_allowlist"])
        if "denied_paths" in security:
            cfg.denied_paths = [_resolve_path(p) for p in security["denied_paths"]]

        return cfg

    @classmethod
    def _apply_env(cls, cfg: Config) -> Config:
        """Override config values from EVERMCP_* environment variables."""
        val = os.environ.get("EVERMCP_LOG_LEVEL")
        if val:
            cfg.log_level = val.upper()

        val = os.environ.get("EVERMCP_FFMPEG_BINARY")
        if val:
            cfg.ffmpeg_binary = val

        val = os.environ.get("EVERMCP_FFMPEG_TIMEOUT_S")
        if val:
            cfg.ffmpeg_timeout_s = int(val)

        val = os.environ.get("EVERMCP_FS_ALLOWLIST")
        if val:
            cfg.filesystem_allowlist = [_resolve_path(p) for p in val.split(",") if p.strip()]

        val = os.environ.get("EVERMCP_NET_ALLOWLIST")
        if val:
            cfg.network_allowlist = [p.strip() for p in val.split(",") if p.strip()]

        return cfg

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"Config(log_level={self.log_level!r}, "
            f"log_file={self.log_file!r}, "
            f"ffmpeg_binary={self.ffmpeg_binary!r}, "
            f"ffmpeg_timeout_s={self.ffmpeg_timeout_s}, "
            f"filesystem_allowlist={self.filesystem_allowlist!r}, "
            f"network_allowlist={self.network_allowlist!r}, "
            f"denied_paths={self.denied_paths!r})"
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _resolve_path(p: Path | str) -> Path:
    """Expand ~ and resolve to absolute path."""
    return Path(p).expanduser().resolve()
