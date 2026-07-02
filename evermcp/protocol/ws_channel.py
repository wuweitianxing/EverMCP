"""WebSocket reverse-registration channel for remote MCP clients (S2).

A remote client runs its existing stdio MCP server locally and bridges it to
the gateway over an outbound WebSocket. On the gateway side, this module:

1. Accepts authenticated WS connections at ``/ws``.
2. Spawns an ``mcp.client.session.ClientSession`` over a custom transport
   backed by the WS.
3. Creates a ``RemoteClientProvider`` and injects it into the Coordinator's
   registry so the remote tools appear in ``tools/list``.
4. Refreshes the client's liveness timestamp while connected; protocol-
   level ping/pong keepalive is provided by the underlying transport.
5. Removes the provider and marks the client offline on disconnect.

The wire protocol is plain MCP JSON-RPC 2.0 messages sent as WebSocket text
frames — the WS is just a byte pipe, no new protocol is invented.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
import mcp.types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import APIRouter
from mcp.client.session import ClientSession
from mcp.shared.message import SessionMessage
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from evermcp.core.provider import RemoteClientProvider
from evermcp.security.auth import AuthError, validate_api_key
from evermcp.storage import (
    create_client,
    get_client,
    update_client_last_seen,
)

if TYPE_CHECKING:
    from evermcp.protocol.coordinator import Coordinator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSocket-backed MCP transport
# ---------------------------------------------------------------------------


@asynccontextmanager
async def websocket_transport(
    websocket: WebSocket,
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Bridge a Starlette WebSocket to MCP memory-object streams.

    Yields ``(read_stream, write_stream)`` compatible with
    ``mcp.client.session.ClientSession``.
    """
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async def _reader() -> None:
        try:
            async with read_stream_writer:
                while True:
                    try:
                        text = await websocket.receive_text()
                    except WebSocketDisconnect:
                        break
                    if not text:
                        continue
                    try:
                        message = types.JSONRPCMessage.model_validate_json(text)
                    except Exception as exc:  # pragma: no cover
                        logger.exception("Failed to parse JSON-RPC from WS")
                        await read_stream_writer.send(exc)
                        continue
                    await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:  # noqa: BLE001
            pass
        except Exception as exc:  # pragma: no cover
            logger.exception("WS reader error")
            with contextlib.suppress(anyio.ClosedResourceError):
                await read_stream_writer.send(exc)
        finally:
            await read_stream_writer.aclose()

    async def _writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json_text = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    )
                    try:
                        await websocket.send_text(json_text)
                    except WebSocketDisconnect:  # noqa: BLE001
                        break
        except anyio.ClosedResourceError:  # noqa: BLE001
            pass
        except Exception:  # pragma: no cover
            logger.exception("WS writer error")
        finally:
            await write_stream.aclose()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_reader)
        tg.start_soon(_writer)
        try:
            yield read_stream, write_stream
        finally:
            tg.cancel_scope.cancel()


# ---------------------------------------------------------------------------
# Heartbeat helper
# ---------------------------------------------------------------------------

# Starlette's WebSocket does not expose a protocol-level ping API, so we rely
# on the underlying uvicorn/websockets transport for ping/pong keepalive. As
# an application-level liveness marker we refresh the client's last_seen_at
# while the connection stays up. The poll cadence is short so a dropped
# client is noticed promptly (well within the remote-call timeout) and so
# disconnect clean-up stays responsive for short test/real timeouts.
HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_POLL_S = 1.0


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


async def handle_websocket(
    websocket: WebSocket,
    coordinator: Coordinator,
    api_key: str | None,
) -> None:
    """Handle one reverse-registered MCP client over WebSocket.

    Validates the API key, initializes a ClientSession over the WS bridge,
    injects a RemoteClientProvider into the Coordinator, and waits until the
    client disconnects.
    """
    try:
        key_row = validate_api_key(api_key, required_scope="ws:connect")
    except AuthError as exc:
        logger.warning("WS auth failed: %s", exc.message)
        await websocket.close(code=1008, reason=exc.message)
        return

    await websocket.accept()

    client_id = key_row.client_id or key_row.key_hash[:16]
    source = f"remote.{client_id}"

    # Ensure the client identity exists in the database.
    if get_client(client_id) is None:
        create_client(name=f"client-{client_id[:8]}", client_id=client_id)

    provider: RemoteClientProvider | None = None

    try:
        async with (
            websocket_transport(websocket) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            update_client_last_seen(client_id)

            provider = RemoteClientProvider(
                client_id=client_id,
                session=session,
                healthy=True,
            )
            await provider.refresh()
            coordinator.registry.add_provider(provider)
            logger.info(
                "Remote client registered: %s (%d tools)",
                source,
                len(provider.list_capabilities()),
            )

            # Keep the handler alive until the WS closes. The transport
            # reader/writer tasks do the actual I/O. While connected we
            # refresh the client's last_seen_at every HEARTBEAT_INTERVAL_S so
            # the timestamp reflects liveness instead of freezing at connect
            # time; protocol-level keepalive (ping/pong) is provided by the
            # underlying uvicorn/websockets transport. The short poll cadence
            # also lets us notice a dropped client promptly.
            last_seen_refresh = asyncio.get_running_loop().time()
            try:
                while websocket.client_state == WebSocketState.CONNECTED:
                    await asyncio.sleep(HEARTBEAT_POLL_S)
                    now = asyncio.get_running_loop().time()
                    if now - last_seen_refresh >= HEARTBEAT_INTERVAL_S:
                        update_client_last_seen(client_id)
                        last_seen_refresh = now
            except WebSocketDisconnect:  # noqa: BLE001
                pass
    except WebSocketDisconnect:  # noqa: BLE001
        logger.info("Remote client disconnected: %s", source)
    except Exception:  # pragma: no cover
        logger.exception("Remote client handler error: %s", source)
    finally:
        if provider is not None:
            provider.mark_unhealthy()
            try:
                coordinator.registry.remove_provider(source)
            except Exception:  # pragma: no cover
                logger.exception("Failed to remove remote provider: %s", source)
        logger.info("Remote client unregistered: %s", source)


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------


def build_ws_router(coordinator: Coordinator) -> APIRouter:
    """Return a FastAPI router with the ``/ws`` endpoint wired to a Coordinator."""
    router = APIRouter()

    @router.websocket("/ws")
    async def _ws_endpoint(websocket: WebSocket, token: str | None = None) -> None:
        # FastAPI Query dependency does not run for WebSocket routes in all
        # versions, so read the query parameter manually here. Fall back to
        # the ``X-EverMCP-Key`` header so keys are not leaked into URLs/logs.
        if token is None:
            token = websocket.query_params.get("token")
        if token is None:
            token = websocket.headers.get("x-evermcp-key")
        await handle_websocket(
            websocket,
            coordinator,
            api_key=token,
        )

    return router
