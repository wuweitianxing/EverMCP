"""End-to-end test for S2 — remote client via HTTP MCP endpoint.

Spins up the full gateway (FastAPI + uvicorn), registers a fake remote MCP
server over WebSocket, and drives ``tools/call`` through the Streamable HTTP
MCP endpoint. This mirrors what an Agent would do when talking to EverMCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import Any

import httpx
import pytest
import websockets
from sqlalchemy import create_engine

from evermcp.core.registry import CapabilityRegistry
from evermcp.protocol.coordinator import Coordinator
from evermcp.protocol.http_server import HTTPServer
from evermcp.protocol.mcp_server import MCPServer
from evermcp.security.config import Config
from evermcp.storage import create_api_key, create_client, init_db
from evermcp.web.app import create_app


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


class FakeRemoteMCPServer:
    """Tiny MCP server used to simulate the remote client."""

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
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1.0.0"},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self.tools}}
        if method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})
            if name == "search":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": f"e2e results for {arguments.get('q')}"}
                        ],
                        "isError": False,
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        if method == "notifications/initialized":
            return None
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }


async def _run_fake_server(
    ws: websockets.WebSocketClientProtocol,
    server: FakeRemoteMCPServer,
) -> None:
    try:
        async for raw in ws:
            text = raw if isinstance(raw, str) else raw.decode("utf-8")
            msg = json.loads(text)
            response = server.handle(msg)
            if response is not None:
                await ws.send(json.dumps(response))
    except websockets.exceptions.ConnectionClosed:
        pass


@pytest.fixture
async def gateway_url():
    """Start the gateway (HTTP MCP + FastAPI WS/UI) and yield URLs."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)

    from evermcp import storage as storage_module

    original_get_engine = storage_module.get_engine
    storage_module.get_engine = lambda: engine

    client = create_client(name="e2e-bot", client_id="e2e-bot", engine=engine)
    create_api_key(
        key="emcp_e2e_secret",
        client_id=client.id,
        scopes="ws:connect",
        engine=engine,
    )

    registry = CapabilityRegistry()
    coord = Coordinator(registry=registry, config=Config())

    # Share one MCPServer between HTTP MCP and the FastAPI WS/UI app.
    mcp_server = MCPServer(coord)

    mcp_port = _free_port()
    web_port = _free_port()

    http_server = HTTPServer(
        coord,
        host="127.0.0.1",
        port=mcp_port,
        stateless=True,
        json_response=True,
        mcp_server=mcp_server,
    )
    http_task = asyncio.create_task(http_server.run())

    web_app = create_app(coord, require_token=False)
    import uvicorn

    web_config = uvicorn.Config(web_app, host="127.0.0.1", port=web_port, log_level="warning")
    web_server = uvicorn.Server(web_config)
    web_task = asyncio.create_task(web_server.serve())

    try:
        await _wait_for_port("127.0.0.1", mcp_port)
        await _wait_for_port("127.0.0.1", web_port)
        yield (
            f"http://127.0.0.1:{mcp_port}/mcp",
            f"ws://127.0.0.1:{web_port}/ws",
        )
    finally:
        for task in (http_task, web_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        storage_module.get_engine = original_get_engine


@pytest.mark.asyncio
async def test_http_mcp_call_remote_tool(gateway_url):
    """An Agent calling via HTTP should be able to invoke a remote tool."""
    mcp_url, ws_url = gateway_url
    server = FakeRemoteMCPServer()

    async with websockets.connect(f"{ws_url}?token=emcp_e2e_secret") as ws:
        server_task = asyncio.create_task(_run_fake_server(ws, server))
        try:
            # Wait for the remote provider to register.
            async with httpx.AsyncClient(timeout=5.0) as client:
                for _ in range(50):
                    resp = await client.post(
                        mcp_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/list",
                            "params": {},
                        },
                        headers={"Accept": "application/json"},
                    )
                    tools = resp.json().get("result", {}).get("tools", [])
                    if any(t["name"] == "remote.e2e-bot.search" for t in tools):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("Remote tool did not appear in tools/list")

                resp = await client.post(
                    mcp_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "remote.e2e-bot.search",
                            "arguments": {"q": "gateway"},
                        },
                    },
                    headers={"Accept": "application/json"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert "result" in body, body
                text = body["result"]["content"][0]["text"]
                assert "e2e results for gateway" in text
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
