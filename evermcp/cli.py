"""CLI entry point for EverMCP."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import click


class _ISOFormatter(logging.Formatter):
    """JSON-like formatter for stderr: ISO 8601 timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        return f'{{"ts": "{ts}Z", "level": "{record.levelname}", "msg": "{record.getMessage()}"}}'


class _HumanFormatter(logging.Formatter):
    """Human-readable formatter for log file."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return f"{ts} {record.levelname:<8s} {record.getMessage()}"


def _setup_logging(config_log_level: str, config_log_file: Path, verbose: bool) -> None:
    """Configure dual-handler logging: stderr (JSON) + file (human-readable).

    Per DESIGN.md §Logging:
    - stderr: structured JSON (does not pollute stdio MCP protocol)
    - log file: human-readable format
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # let handlers filter

    level = logging.DEBUG if verbose else getattr(logging, config_log_level.upper(), logging.INFO)

    # Handler 1: stderr — structured JSON with ISO 8601 timestamps
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(_ISOFormatter())
    root.addHandler(stderr_handler)

    # Handler 2: file — human-readable
    try:
        config_log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(config_log_file),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)  # always DEBUG to file
        file_handler.setFormatter(_HumanFormatter())
        root.addHandler(file_handler)
    except OSError as exc:
        # Log to stderr if file handler fails (best-effort)
        logging.getLogger("evermcp").warning("Cannot open log file %s: %s", config_log_file, exc)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.option("--config", "-c", "config_file", default=None, help="Path to config TOML file")
@click.pass_context
def main(ctx: click.Context, verbose: bool, config_file: str | None) -> None:
    """EverMCP — Cross-device tool orchestration for AI Agents."""
    # Load config early so log_level is available
    from evermcp.security.config import Config

    config = Config.load(config_file=config_file)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config

    _setup_logging(config.log_level, config.log_file, verbose)


@main.command()
@click.option(
    "--tools-dir",
    default=None,
    help=(
        "Path to the directory containing tool modules. "
        "Overrides $EVERMCP_TOOLS_DIR. Default: <repo>/tools (empty in this repo)."
    ),
)
@click.pass_context
def serve(ctx: click.Context, tools_dir: str | None) -> None:
    """Start the MCP server (stdio transport)."""
    config = ctx.obj["config"]

    resolved_tools_dir = _resolve_tools_dir(tools_dir)
    if resolved_tools_dir:
        click.echo(f"[evermcp] loading tools from: {resolved_tools_dir}", err=True)
    else:
        click.echo(
            "[evermcp] no tools directory configured; server will expose 0 tools. "
            "Pass --tools-dir <path> or set EVERMCP_TOOLS_DIR.",
            err=True,
        )

    from evermcp.core.registry import ToolRegistry
    from evermcp.protocol.coordinator import Coordinator
    from evermcp.protocol.mcp_server import MCPServer

    registry = ToolRegistry(tools_dir=resolved_tools_dir)
    coordinator = Coordinator(registry=registry, config=config)
    coordinator.initialize()

    server = MCPServer(coordinator)

    try:
        asyncio.run(server.run())
    finally:
        coordinator.shutdown()


@main.command()
@click.option(
    "--tools-dir",
    default=None,
    help="Path to the directory containing tool modules. Overrides $EVERMCP_TOOLS_DIR.",
)
@click.pass_context
def list_tools(ctx: click.Context, tools_dir: str | None) -> None:  # noqa: ARG001
    """List all registered tools (no server, just discovery)."""
    from evermcp.core.registry import ToolRegistry

    resolved_tools_dir = _resolve_tools_dir(tools_dir)
    registry = ToolRegistry(tools_dir=resolved_tools_dir)
    descriptors = registry.scan()

    if not descriptors:
        click.echo(
            f"No tools found in {resolved_tools_dir or 'default location'}."
        )
        return

    for d in descriptors:
        click.echo(f"  {d['name']:30s}  {d['description']}")


def _resolve_tools_dir(cli_value: str | None) -> Path | None:
    """Resolve tools directory using priority: CLI > env > None.

    Returns None if no explicit value was given. None means "let ToolRegistry
    fall back to its built-in default" (which points at <repo>/tools/, empty
    in this repo).
    """
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    env_value = os.environ.get("EVERMCP_TOOLS_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return None


if __name__ == "__main__":
    main()
