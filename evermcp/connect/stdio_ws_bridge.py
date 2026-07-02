"""Client-side adapter: bridge a local stdio MCP server to a gateway WebSocket.

Usage:
    evermcp-connect --gateway ws://gateway/ws --token <API_KEY> -- <cmd> [args...]

This adapter:
1. Spawns the local MCP server as a subprocess with stdin/stdout pipes.
2. Connects an outbound WebSocket to the EverMCP gateway (auth via token).
3. Relays JSON-RPC messages in both directions until either side closes.

The gateway sees the remote client as a ``RemoteClientProvider`` and exposes
its tools under the ``remote.<client_id>.`` namespace.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import Any

import websockets

logger = logging.getLogger(__name__)

DEFAULT_GATEWAY = "ws://127.0.0.1:8788/ws"


async def _relay_stdin_to_ws(
    process: asyncio.subprocess.Process,
    websocket: Any,
) -> None:
    """Read newline-delimited JSON-RPC from the subprocess stdout and send to WS."""
    assert process.stdout is not None
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                logger.info("Subprocess stdout closed")
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            await websocket.send(text)
            logger.debug("-> gateway: %s", text[:200])
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover
        logger.exception("Error relaying stdout to websocket")


async def _relay_ws_to_stdin(
    websocket: Any,
    process: asyncio.subprocess.Process,
) -> None:
    """Read JSON-RPC messages from the WS and write them to the subprocess stdin."""
    assert process.stdin is not None
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            text = message.strip()
            if not text:
                continue
            process.stdin.write((text + "\n").encode("utf-8"))
            await process.stdin.drain()
            logger.debug("<- gateway: %s", text[:200])
    except asyncio.CancelledError:
        pass
    except websockets.exceptions.ConnectionClosed:  # noqa: BLE001
        logger.info("WebSocket connection closed")
    except Exception:  # pragma: no cover
        logger.exception("Error relaying websocket to stdin")


async def run_bridge(
    gateway_url: str,
    token: str,
    command: list[str],
    cwd: Path | str | None = None,
) -> int:
    """Run the stdio-to-WebSocket bridge and return the subprocess exit code.

    Args:
        gateway_url: WebSocket URL of the gateway endpoint.
        token: API key for the gateway WS handshake.
        command: The local MCP server command + arguments.
        cwd: Optional working directory for the subprocess.

    Returns:
        The subprocess exit code (or 1 if the bridge failed).
    """
    if not command:
        logger.error("No MCP server command provided")
        return 1

    logger.info("Connecting to gateway: %s", gateway_url)

    try:
        websocket = await websockets.connect(
            gateway_url, additional_headers={"X-EverMCP-Key": token}
        )
    except websockets.exceptions.InvalidStatus as exc:
        logger.error("Gateway rejected connection: %s", exc)
        return 1
    except Exception:  # pragma: no cover
        logger.exception("Failed to connect to gateway")
        return 1

    logger.info("Connected to gateway; starting local MCP server: %s", command[0])

    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )

    # Log stderr in the background so users can see server diagnostics.
    async def _log_stderr() -> None:
        assert process.stderr is not None
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                logger.warning("[server stderr] %s", text)
        except asyncio.CancelledError:
            pass

    tasks = [
        asyncio.create_task(_relay_stdin_to_ws(process, websocket)),
        asyncio.create_task(_relay_ws_to_stdin(websocket, process)),
        asyncio.create_task(_log_stderr()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
    except asyncio.CancelledError:
        # Bridge task cancelled externally: tear down all relays before cleanup.
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
        try:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        except Exception:  # pragma: no cover
            logger.exception("Error terminating subprocess")

    return process.returncode or 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="evermcp-connect",
        description="Bridge a local stdio MCP server to an EverMCP gateway.",
    )
    parser.add_argument(
        "--gateway",
        default=DEFAULT_GATEWAY,
        help=f"Gateway WebSocket URL (default: {DEFAULT_GATEWAY})",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="API key for gateway authentication.",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for the local MCP server subprocess.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "server_command",
        nargs=argparse.REMAINDER,
        help="MCP server command and arguments (prefix with '--').",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the stdio-to-WebSocket bridge."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.server_command:
        logger.error("No MCP server command provided. Use '-- <cmd> [args...]'.")
        return 1

    # Strip leading '--' if the user included it.
    command = args.server_command
    if command[0] == "--":
        command = command[1:]

    try:
        return asyncio.run(
            run_bridge(
                gateway_url=args.gateway,
                token=args.token,
                command=command,
                cwd=Path(args.cwd) if args.cwd else None,
            )
        )
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("Interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
