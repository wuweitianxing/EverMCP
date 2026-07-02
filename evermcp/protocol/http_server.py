"""Streamable HTTP transport for EverMCP (S0).

Wraps the same ``mcp.server.Server`` instance that ``MCPServer`` uses for
stdio and exposes it over HTTP via the official MCP SDK's
:class:`mcp.server.streamable_http_manager.StreamableHTTPSessionManager`
+ uvicorn.

Design notes
------------
- We **share** the ``Server`` instance between stdio and HTTP so handler
  registration (tools / resources / prompts) lives in exactly one place:
  ``MCPServer``. ``HTTPServer`` simply reuses ``MCPServer.server`` (via
  ``build_mcp_server``).
- Default-bind to ``127.0.0.1`` — never expose an unauthenticated MCP
  server on a public interface.
- We do **not** pull in starlette/fastapi. ``StreamableHTTPSessionManager``
  exposes a per-request ASGI callable (``handle_request``). We wrap it in
  a tiny ASGI 3.0 application that also drives the manager's lifecycle
  via the ASGI ``lifespan`` protocol — this is required because the
  manager's anyio task group is only initialised inside
  ``async with manager.run():`` and must be active for the entire
  request-serving window.
- The MCP endpoint path is conventionally ``/mcp``. The manager itself
  doesn't filter by path, so we route all HTTP traffic at any path to
  ``handle_request`` (the standard MCP HTTP pattern).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from evermcp.protocol.coordinator import Coordinator
from evermcp.protocol.mcp_server import MCPServer

logger = logging.getLogger(__name__)


class _LifespanState:
    """Holds the per-server manager lifecycle state."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager
        self._task: asyncio.Task[None] | None = None
        self._entered = asyncio.Event()

    async def startup(self) -> None:
        """Enter ``manager.run()`` in a background task and wait for it."""
        if self._task is not None:
            raise RuntimeError("lifespan already started")

        async def _drive() -> None:
            async with self._manager.run():
                self._entered.set()
                # Park until cancelled by shutdown().
                await asyncio.Event().wait()

        self._task = asyncio.create_task(_drive(), name="mcp-http-lifespan")
        try:
            await asyncio.wait_for(self._entered.wait(), timeout=10.0)
        except TimeoutError as exc:
            raise RuntimeError("StreamableHTTPSessionManager.run() did not enter in time") from exc

    async def shutdown(self) -> None:
        """Cancel the background task so ``manager.run()`` exits."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._entered.clear()


def _build_asgi_app(manager: StreamableHTTPSessionManager) -> Any:
    """Build an ASGI 3.0 application that fronts ``manager.handle_request``.

    Handles three ASGI scope types:

    - ``http`` → delegate to ``manager.handle_request`` (per-request).
    - ``lifespan`` → drive the manager's lifecycle so its task group is
      active during the entire request-serving window.
    - anything else (websocket) → no-op.
    """
    state = _LifespanState(manager)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            await manager.handle_request(scope, receive, send)
            return

        if scope["type"] != "lifespan":
            return  # websocket etc. — not supported

        message = await receive()
        kind = message["type"]
        if kind == "lifespan.startup":
            try:
                await state.startup()
            except Exception as exc:  # noqa: BLE001
                logger.exception("HTTP lifespan startup failed")
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
                return
            await send({"type": "lifespan.startup.complete"})
            return

        if kind == "lifespan.shutdown":
            await state.shutdown()
            await send({"type": "lifespan.shutdown.complete"})
            return

        await send({"type": "lifespan.startup.failed", "message": f"unknown: {kind}"})

    return app


class HTTPServer:
    """Streamable HTTP transport for EverMCP (S0).

    The HTTP transport reuses the same underlying ``mcp.server.Server``
    instance as the stdio transport. To get this behavior the caller
    should pass an already-built :class:`MCPServer` via ``mcp_server=``:

        mcp_srv = MCPServer(coordinator)
        stdio = MCPServer(coordinator)  # the stdio path keeps its own
        http = HTTPServer(coordinator, mcp_server=mcp_srv)

    If ``mcp_server`` is not provided, the constructor falls back to
    building its own ``MCPServer(coordinator)`` — convenient for tests
    and single-transport use cases, but produces two independent
    ``Server`` instances when combined with a separate stdio MCPServer.
    """

    def __init__(
        self,
        coordinator: Coordinator,
        host: str = "127.0.0.1",
        port: int = 8787,
        json_response: bool = False,
        stateless: bool = False,
        mcp_server: MCPServer | Server | None = None,
    ) -> None:
        # Bind to loopback by default; explicit override required for LAN.
        self._coordinator = coordinator
        self._host = host
        self._port = port
        self._json_response = json_response
        self._stateless = stateless

        # Reuse the caller's Server instance when provided — the stdio
        # transport and the HTTP transport then share handler registration
        # (single source of truth). Falls back to building a private one.
        if mcp_server is None:
            mcp_server = MCPServer(coordinator)
        if isinstance(mcp_server, MCPServer):
            self._server: Server = mcp_server.server
        else:
            # Already a raw Server — accept as-is.
            self._server = mcp_server

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def build_mcp_server(self) -> Server:
        """Return the underlying ``mcp.server.Server`` instance.

        Kept for API symmetry with :class:`MCPServer` and to make the
        transport swappable in tests.
        """
        return self._server

    # ------------------------------------------------------------------
    # Transports
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run uvicorn + StreamableHTTPSessionManager until cancelled."""
        manager = StreamableHTTPSessionManager(
            self._server,
            json_response=self._json_response,
            stateless=self._stateless,
        )
        app = _build_asgi_app(manager)

        config = uvicorn.Config(
            app=app,
            host=self._host,
            port=self._port,
            log_level="info",
            lifespan="on",  # we implement lifespan inside ``app``
        )
        server = uvicorn.Server(config)
        logger.info(
            "EverMCP HTTP transport listening on http://%s:%d/mcp (stateless=%s, json_response=%s)",
            self._host,
            self._port,
            self._stateless,
            self._json_response,
        )
        try:
            await server.serve()
        finally:
            logger.info("EverMCP HTTP transport shut down")

    async def run_with_stdio(self, stdio_coro: Any) -> None:
        """Run HTTP + stdio transports concurrently.

        Lets the CLI's ``--stdio --http`` flag work without duplicating the
        event loop. Either failing cancels the other via gather propagation.
        """
        await asyncio.gather(stdio_coro, self.run())


__all__ = ["HTTPServer"]
