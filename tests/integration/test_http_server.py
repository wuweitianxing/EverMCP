"""Integration tests for the EverMCP Streamable HTTP transport.

Spawns HTTPServer in a background task on a fixed test port (18787), connects
with httpx.AsyncClient using MCP's stateless + json_response mode for the
simplest smoke, and verifies tools/list and tools/call round-trips.
"""
from __future__ import annotations

import asyncio
import socket
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from evermcp.core.registry import ToolRegistry
from evermcp.protocol.coordinator import Coordinator
from evermcp.protocol.http_server import HTTPServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Ask the OS for a free TCP port on loopback.

    Used when the hard-coded port collides with another test process.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_tools_dir(tmp_path: Path) -> Path:
    """Create a temp tools dir with one demo tool."""
    tools_dir = tmp_path / "tools"
    cat_dir = tools_dir / "demo"
    cat_dir.mkdir(parents=True)
    (cat_dir / "hello.py").write_text(
        textwrap.dedent("""\
            from evermcp.core.tool import tool

            @tool(description="Say hello")
            def hello(name: str) -> dict:
                return {"message": f"hello, {name}"}
        """),
        encoding="utf-8",
    )
    return tools_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    """Poll the TCP port until uvicorn accepts connections or timeout.

    Uses a non-blocking ``socket.connect_ex`` so we don't hold the loop's
    attention during the wait. If the server task dies before the port
    opens, re-raise the task exception so the fixture fails fast instead
    of timing out silently.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with socket.socket() as s:
            s.setblocking(False)
            try:
                await asyncio.get_running_loop().sock_connect(s, (host, port))
                return  # connected
            except (BlockingIOError, OSError):
                pass
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"HTTP server did not start listening on {host}:{port} within {timeout}s"
            )
        await asyncio.sleep(0.05)


@pytest.fixture
async def http_server(tmp_path: Path) -> AsyncIterator[str]:
    """Spin up an HTTPServer on a free loopback port; yield the /mcp URL.

    Readiness is detected by polling the TCP port (no fixed ``sleep``).
    If the server task dies during startup, the underlying exception is
    re-raised — we never silently swallow startup failures.
    """
    port = _free_port()
    tools_dir = _make_tools_dir(tmp_path)

    coord = Coordinator(registry=ToolRegistry(tools_dir=tools_dir))
    coord.initialize()

    http = HTTPServer(
        coord,
        host="127.0.0.1",
        port=port,
        stateless=True,
        json_response=True,
    )
    task = asyncio.create_task(http.run(), name="http-server-fixture")

    # Wait until either the port is open or the server task crashed.
    poll_task = asyncio.create_task(_wait_for_port("127.0.0.1", port))
    done, _ = await asyncio.wait(
        {poll_task, task}, return_when=asyncio.FIRST_COMPLETED
    )
    if task in done and not poll_task.done():
        # Server task died before the port opened — surface the cause.
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        # Re-raise whatever killed the server. asyncio.gather would wrap
        # it in a Task exception; calling .result() unwraps it cleanly.
        try:
            task.result()
        except BaseException as exc:  # noqa: BLE001
            coord.shutdown()
            raise RuntimeError(f"HTTP server failed to start: {exc!r}") from exc
    elif task in done and task.exception() is not None:
        coord.shutdown()
        raise RuntimeError(f"HTTP server failed to start: {task.exception()!r}")
    else:
        # Port is open; cancel the poll task.
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

    url = f"http://127.0.0.1:{port}/mcp"
    try:
        yield url
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover — best-effort cleanup
                import logging

                logging.getLogger(__name__).warning(
                    "HTTP server task raised during shutdown: %r", exc
                )
        coord.shutdown()


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------

class TestToolsList:
    @pytest.mark.asyncio
    async def test_tools_list_round_trip(self, http_server: str) -> None:
        """POST {method: 'tools/list'} returns the registered tools."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        headers = {"Accept": "application/json"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(http_server, json=payload, headers=headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "result" in body, body
        assert "tools" in body["result"]
        tool_names = [t["name"] for t in body["result"]["tools"]]
        assert "demo.hello" in tool_names


class TestToolsCall:
    @pytest.mark.asyncio
    async def test_tools_call_round_trip(self, http_server: str) -> None:
        """POST {method: 'tools/call', name: 'demo.hello', args: {name}} works."""
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "demo.hello",
                "arguments": {"name": "world"},
            },
        }
        headers = {"Accept": "application/json"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(http_server, json=payload, headers=headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "result" in body, body
        result = body["result"]
        assert "content" in result
        assert len(result["content"]) >= 1
        text = result["content"][0]["text"]
        assert "hello, world" in text