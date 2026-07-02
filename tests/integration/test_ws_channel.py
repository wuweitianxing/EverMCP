"""Integration tests for S2 — WebSocket reverse registration.

These tests simulate a remote MCP server by speaking JSON-RPC directly over
a WebSocket, avoiding subprocess flakiness. They verify:

- WS auth rejects invalid keys and accepts valid keys
# - Remote tools appear in the registry under ``remote.<client_id>.`` prefix
- Remote tool calls round-trip through the gateway
- Disconnect marks the provider unhealthy and removes it
- Call logs are persisted
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import Any

import pytest
import websockets
from sqlalchemy import create_engine

from evermcp.core.registry import CapabilityRegistry
from evermcp.protocol.coordinator import Coordinator
from evermcp.security.config import Config
from evermcp.storage import (
    create_api_key,
    create_client,
    init_db,
    list_call_logs,
)
from evermcp.web.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_engine():
    """Create an in-memory SQLite engine with all S2 tables."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


@pytest.fixture
def api_key(in_memory_engine):
    """Create a valid API key bound to a client identity."""
    client = create_client(name="test-bot", client_id="test-bot", engine=in_memory_engine)
    return create_api_key(
        key="emcp_test_secret_key",
        client_id=client.id,
        scopes="ws:connect",
        engine=in_memory_engine,
    )


@pytest.fixture
def admin_api_key(in_memory_engine):
    """Create an API key with the ``admin`` scope for gating admin REST endpoints."""
    return create_api_key(
        key="emcp_admin_secret_key",
        scopes="admin",
        engine=in_memory_engine,
    )


@pytest.fixture
def coordinator(in_memory_engine):
    """Create a Coordinator with an empty registry and in-memory DB."""
    registry = CapabilityRegistry()
    config = Config()
    # Point the default storage engine at the in-memory DB by monkey-patching
    # the module-level default. Tests run sequentially so this is safe.
    from evermcp import storage as storage_module

    original_default = storage_module.DEFAULT_DB_URL
    storage_module.DEFAULT_DB_URL = "sqlite:///:memory:"
    init_db(in_memory_engine)

    coord = Coordinator(registry=registry, config=config)
    yield coord
    storage_module.DEFAULT_DB_URL = original_default


@pytest.fixture
async def app(coordinator, in_memory_engine, api_key):
    """Create a FastAPI app with WS + REST endpoints wired to in-memory DB."""
    # Patch the global engine used by auth/storage functions so they hit the
    # in-memory DB instead of the default file URL.
    from evermcp import storage as storage_module

    original_get_engine = storage_module.get_engine

    def _test_engine():
        return in_memory_engine

    storage_module.get_engine = _test_engine

    app = create_app(coordinator, require_token=False)
    try:
        yield app
    finally:
        storage_module.get_engine = original_get_engine


@pytest.fixture
async def ws_client(app):
    """Yield a connected WebSocket test client."""

    # websockets doesn't speak ASGI directly; use uvicorn on a free port.
    port = _free_port()
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    try:
        await _wait_for_port("127.0.0.1", port)
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        server.should_exit = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            with socket.socket() as s:
                s.setblocking(False)
                await asyncio.get_running_loop().sock_connect(s, (host, port))
                return
        except (BlockingIOError, OSError):
            pass
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Server did not start on {host}:{port}")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Remote MCP server simulation
# ---------------------------------------------------------------------------


class FakeRemoteMCPServer:
    """A tiny MCP server speaking JSON-RPC over a WebSocket.

    Responds to ``initialize``, ``tools/list`` and ``tools/call``.
    """

    def __init__(self) -> None:
        self.tools = [
            {
                "name": "search",
                "description": "Search the knowledge base",
                "inputSchema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ]

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return self._result(
                req_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1.0.0"},
                },
            )
        if method == "tools/list":
            return self._result(req_id, {"tools": self.tools})
        if method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})
            if name == "search":
                return self._result(
                    req_id,
                    {
                        "content": [{"type": "text", "text": f"results for {arguments.get('q')}"}],
                        "isError": False,
                    },
                )
            return self._error(req_id, -32601, f"Unknown tool: {name}")
        if method == "notifications/initialized":
            return None
        return self._error(req_id, -32601, f"Unknown method: {method}")

    def _result(self, req_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _error(self, req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _run_fake_server(
    ws: websockets.WebSocketClientProtocol,
    server: FakeRemoteMCPServer,
) -> None:
    """Read JSON-RPC requests and reply until the websocket closes."""
    try:
        async for raw in ws:
            text = raw if isinstance(raw, str) else raw.decode("utf-8")
            msg = json.loads(text)
            response = server.handle(msg)
            if response is not None:
                await ws.send(json.dumps(response))
    except websockets.exceptions.ConnectionClosed:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_rejects_invalid_token(ws_client):
    """A WS connection with a bad token should be rejected."""
    uri = f"{ws_client}?token=bad-token"
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(uri):
            pass


@pytest.mark.asyncio
async def test_ws_registers_remote_tools(ws_client, coordinator):
    """A valid remote client should expose prefixed tools in the registry."""
    uri = f"{ws_client}?token=emcp_test_secret_key"
    server = FakeRemoteMCPServer()

    async with websockets.connect(uri) as ws:
        server_task = asyncio.create_task(_run_fake_server(ws, server))
        try:
            # Wait for the provider to appear in the registry.
            for _ in range(50):
                providers = [p.source for p in coordinator.registry.providers]
                if "remote.test-bot" in providers:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Remote provider did not register in time")

            tools = coordinator.list_tools()
            names = [t["name"] for t in tools]
            assert "remote.test-bot.search" in names
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task


@pytest.mark.asyncio
async def test_ws_call_remote_tool(ws_client, coordinator):
    """A remote tool call should round-trip through the gateway."""
    uri = f"{ws_client}?token=emcp_test_secret_key"
    server = FakeRemoteMCPServer()

    async with websockets.connect(uri) as ws:
        server_task = asyncio.create_task(_run_fake_server(ws, server))
        try:
            # Wait for registration.
            for _ in range(50):
                if "remote.test-bot" in [p.source for p in coordinator.registry.providers]:
                    break
                await asyncio.sleep(0.05)

            result = await coordinator.call_tool_async("remote.test-bot.search", {"q": "mcp"})
            assert result["success"] is True
            assert "results for mcp" in result["result"]
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task


@pytest.mark.asyncio
async def test_ws_disconnect_removes_tools(ws_client, coordinator):
    """After disconnect, remote tools should disappear and health be offline."""
    uri = f"{ws_client}?token=emcp_test_secret_key"
    server = FakeRemoteMCPServer()

    ws = await websockets.connect(uri)
    server_task = asyncio.create_task(_run_fake_server(ws, server))
    try:
        for _ in range(50):
            if "remote.test-bot" in [p.source for p in coordinator.registry.providers]:
                break
            await asyncio.sleep(0.05)
        assert "remote.test-bot.search" in [t["name"] for t in coordinator.list_tools()]
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        await ws.close()

    # Give the gateway handler time to clean up.
    for _ in range(50):
        providers = [p.source for p in coordinator.registry.providers]
        if "remote.test-bot" not in providers:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("Remote provider was not removed after disconnect")

    assert "remote.test-bot.search" not in [t["name"] for t in coordinator.list_tools()]


@pytest.mark.asyncio
async def test_call_log_persisted(ws_client, coordinator, in_memory_engine):
    """A remote tool call should create a CallLog row."""
    uri = f"{ws_client}?token=emcp_test_secret_key"
    server = FakeRemoteMCPServer()

    async with websockets.connect(uri) as ws:
        server_task = asyncio.create_task(_run_fake_server(ws, server))
        try:
            for _ in range(50):
                if "remote.test-bot" in [p.source for p in coordinator.registry.providers]:
                    break
                await asyncio.sleep(0.05)

            await coordinator.call_tool_async("remote.test-bot.search", {"q": "logs"})
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task

    rows, total = list_call_logs(engine=in_memory_engine)
    assert total >= 1
    assert any(r.name == "remote.test-bot.search" and r.source == "remote.test-bot" for r in rows)


# ---------------------------------------------------------------------------
# REST admin API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_clients_and_keys(ws_client, app, api_key, admin_api_key):
    """The admin API should expose client and key CRUD (admin-scope auth)."""
    from httpx import ASGITransport, AsyncClient

    # Admin endpoints require an admin-scope API key.
    headers = {"Authorization": "Bearer emcp_admin_secret_key"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # List clients
        resp = await ac.get("/api/clients", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert any(c["id"] == api_key.client_id for c in data["clients"])

        # List keys
        resp = await ac.get("/api/keys", headers=headers)
        assert resp.status_code == 200
        assert any(k["key_hash"] == api_key.key_hash for k in resp.json()["keys"])

        # Create a new key
        resp = await ac.post("/api/keys", params={"scopes": "ws:connect"}, headers=headers)
        assert resp.status_code == 200
        created = resp.json()
        assert created["status"] == "created"
        new_hash = created["key_hash"]

        # Revoke it
        resp = await ac.post(f"/api/keys/{new_hash}/revoke", headers=headers)
        assert resp.status_code == 200

        # Delete it
        resp = await ac.delete(f"/api/keys/{new_hash}", headers=headers)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_call_logs(ws_client, coordinator, app, in_memory_engine, admin_api_key):
    """The admin API should return call logs (admin-scope auth)."""
    from httpx import ASGITransport, AsyncClient

    # Admin endpoints require an admin-scope API key.
    headers = {"Authorization": "Bearer emcp_admin_secret_key"}

    # Generate a log entry via a failed local call.
    await coordinator.call_tool_async("nonexistent.tool", {})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/logs", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert any(log["name"] == "nonexistent.tool" for log in data["logs"])


@pytest.mark.asyncio
async def test_admin_api_accepts_api_key_under_ui_token_mode(
    coordinator, in_memory_engine, admin_api_key, monkeypatch
):
    """In ``--ui`` mode (require_token=True) admin endpoints accept an API key.

    Regression for S2: ``TokenAuthMiddleware`` gates ``/api/*`` on the local
    UI token, but the S2 admin endpoints authenticate via an admin-scope API
    key (the ``require_api_key_http`` dependency on the admin router). The
    middleware must let admin paths through, otherwise a valid API key is
    rejected with 401 before the dependency ever runs.
    """
    from httpx import ASGITransport, AsyncClient

    from evermcp import storage as storage_module
    from evermcp.web import app as app_module

    monkeypatch.setattr(storage_module, "get_engine", lambda: in_memory_engine)
    # Avoid touching the real on-disk token file during app construction.
    monkeypatch.setattr(app_module, "_get_or_create_token", lambda: "ui-token")

    app = create_app(coordinator, require_token=True)

    admin_headers = {"Authorization": "Bearer emcp_admin_secret_key"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Admin endpoint: an admin-scope API key must be accepted even though
        # the request carries no UI token (the middleware bypasses admin paths).
        resp = await ac.get("/api/keys", headers=admin_headers)
        assert resp.status_code == 200

        # Without any credential, the admin dependency rejects with 401.
        resp = await ac.get("/api/keys")
        assert resp.status_code == 401

        # A non-admin /api/ path is still gated by the UI token: a request
        # with no UI token is rejected by the middleware (401), confirming
        # S1 endpoint protection remains intact.
        resp = await ac.get("/api/tree")
        assert resp.status_code == 401

        # ... and a valid UI token lets the non-admin endpoint through.
        resp = await ac.get("/api/tree", headers={"Authorization": "Bearer ui-token"})
        assert resp.status_code == 200
