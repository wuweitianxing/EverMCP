"""CLI entry point for EverMCP."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path

import click

from evermcp.security.config import Config


class _ISOFormatter(logging.Formatter):
    """JSON-like formatter for stderr: ISO 8601 timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
        return f'{{"ts": "{ts}Z", "level": "{record.levelname}", "msg": "{record.getMessage()}"}}'


class _HumanFormatter(logging.Formatter):
    """Human-readable formatter for log file."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
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
@click.version_option(version=_pkg_version("evermcp"), prog_name="evermcp")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.option("--config", "-c", "config_file", default=None, help="Path to config TOML file")
@click.pass_context
def main(ctx: click.Context, verbose: bool, config_file: str | None) -> None:
    """EverMCP — MCP Gateway + Capability Governance UI for AI Agents."""
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
@click.option(
    "--stdio/--no-stdio",
    default=True,
    help="Run the stdio MCP transport (default: on). Disable with --no-stdio.",
)
@click.option(
    "--http/--no-http",
    default=False,
    help="Run the Streamable HTTP transport (default: off).",
)
@click.option(
    "--host",
    default=None,
    help="HTTP bind host (default from [gateway].host in config; 127.0.0.1).",
)
@click.option(
    "--port",
    default=None,
    type=int,
    help="HTTP bind port (default from [gateway].port in config; 8787).",
)
@click.option(
    "--init-db/--no-init-db",
    default=True,
    help="Initialize the SQLite gateway database on startup (default: on).",
)
@click.option(
    "--ui/--no-ui",
    default=False,
    help="Enable the Web UI (S1). Default: off. Requires --http.",
)
@click.pass_context
def serve(  # noqa: C901 — small command, complexity is acceptable
    ctx: click.Context,
    tools_dir: str | None,
    stdio: bool,
    http: bool,
    host: str | None,
    port: int | None,
    init_db: bool,
    ui: bool,
) -> None:
    """Start the MCP server (stdio and/or Streamable HTTP transport)."""
    config = ctx.obj["config"]

    # At least one transport must be enabled — otherwise nothing would run.
    if not stdio and not http:
        raise click.UsageError("At least one of --stdio or --http must be enabled.")

    # UI requires HTTP
    if ui and not http:
        raise click.UsageError("--ui requires --http")

    # Resolve host/port: CLI > config > default.
    bind_host = host or config.gateway.host
    bind_port = port if port is not None else config.gateway.port

    resolved_tools_dir = _resolve_tools_dir(tools_dir)
    if resolved_tools_dir:
        click.echo(f"[evermcp] loading tools from: {resolved_tools_dir}", err=True)
    else:
        click.echo(
            "[evermcp] no tools directory configured; server will expose 0 tools. "
            "Pass --tools-dir <path> or set EVERMCP_TOOLS_DIR.",
            err=True,
        )

    # Lazy imports so --help / list-tools don't pay the cost.
    from evermcp.core.registry import ToolRegistry
    from evermcp.protocol.coordinator import Coordinator
    from evermcp.protocol.http_server import HTTPServer
    from evermcp.protocol.mcp_server import MCPServer

    registry = ToolRegistry(tools_dir=resolved_tools_dir)
    coordinator = Coordinator(registry=registry, config=config)
    coordinator.initialize()

    # Optional DB init (S0: off by default; S1+ will turn on).
    if init_db:
        from evermcp.storage import init_db as _init_db

        _init_db()
        click.echo("[evermcp] gateway database initialized", err=True)

    # Build ONE MCPServer and share it across transports. This is the
    # single source of truth for tool/resource/prompt handler registration —
    # see https_server.py for the same guarantee on the HTTP side.
    mcp_server = MCPServer(coordinator)

    # Build the stdio coroutine (only if stdio enabled).
    async def _stdio() -> None:
        await mcp_server.run()

    async def _http() -> None:
        # Pass the shared MCPServer so HTTP handlers are the same instances
        # registered by the stdio path.
        http_server = HTTPServer(
            coordinator,
            host=bind_host,
            port=bind_port,
            mcp_server=mcp_server,
        )
        await http_server.run()

    async def _ui() -> None:
        """Start the FastAPI web UI server (loopback only, token-protected)."""
        import uvicorn

        from evermcp.web.app import create_app

        web_app = create_app(coordinator, require_token=True)
        config = uvicorn.Config(
            web_app,
            host="127.0.0.1",  # forced loopback — never follow --host
            port=bind_port + 1,
            log_level="info",
        )
        server = uvicorn.Server(config)
        click.echo(
            f"[evermcp] starting Web UI on http://127.0.0.1:{bind_port + 1}/ "
            f"(loopback only, token-protected)",
            err=True,
        )
        await server.serve()

    async def _run() -> None:
        tasks = []

        if stdio:
            tasks.append(asyncio.create_task(_stdio(), name="stdio"))

        if http:
            tasks.append(asyncio.create_task(_http(), name="http"))

        if ui:
            tasks.append(asyncio.create_task(_ui(), name="ui"))

        if not tasks:
            raise RuntimeError("No tasks to run")

        if len(tasks) == 1:
            await tasks[0]
        else:
            click.echo(
                f"[evermcp] running {' + '.join(t.get_name() for t in tasks)}",
                err=True,
            )
            await asyncio.gather(*tasks)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("[evermcp] interrupted, shutting down", err=True)
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
        click.echo(f"No tools found in {resolved_tools_dir or 'default location'}.")
        return

    for d in descriptors:
        click.echo(f"  {d['name']:30s}  {d['description']}")


@main.command("connect")
@click.option(
    "--gateway",
    default="ws://127.0.0.1:8788/ws",
    help="Gateway WebSocket URL (default: ws://127.0.0.1:8788/ws).",
)
@click.option(
    "--token",
    required=True,
    help="API key for gateway authentication.",
)
@click.option(
    "--cwd",
    default=None,
    help="Working directory for the local MCP server subprocess.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable debug logging.",
)
@click.argument("server_command", nargs=-1, required=True)
@click.pass_context
def connect(
    ctx: click.Context,
    gateway: str,
    token: str,
    cwd: str | None,
    verbose: bool,
    server_command: tuple[str, ...],
) -> None:
    """Bridge a local stdio MCP server to the gateway."""
    from evermcp.connect.stdio_ws_bridge import main as bridge_main

    argv = ["--gateway", gateway, "--token", token]
    if cwd:
        argv.extend(["--cwd", cwd])
    if verbose:
        argv.append("--verbose")
    # Allow users to type either `evermcp connect -- python server.py` or
    # `evermcp connect python server.py`.
    command = list(server_command)
    if command and command[0] == "--":
        command = command[1:]
    argv.extend(command)
    raise SystemExit(bridge_main(argv))


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
