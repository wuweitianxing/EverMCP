"""Integration tests for MCP stdio server.

Uses MCP SDK Client to connect to a real stdio server subprocess
and verifies list_tools / call_tool round-trips.

NOTE: These tests use asyncio.run() directly instead of pytest-asyncio
to avoid cancel-scope issues with stdio_client on some platforms.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ---------------------------------------------------------------------------
# Helper: spawn server subprocess with a temp tools dir
# ---------------------------------------------------------------------------

_SERVER_SCRIPT = textwrap.dedent("""\
    import asyncio
    import sys
    from pathlib import Path

    # Ensure evermcp is importable
    sys.path.insert(0, {project_root!r})

    from evermcp.core.registry import ToolRegistry
    from evermcp.protocol.coordinator import Coordinator
    from evermcp.protocol.mcp_server import MCPServer

    # Use a custom tools directory from CLI arg
    tools_dir = Path(sys.argv[1])
    registry = ToolRegistry(tools_dir=tools_dir)
    coordinator = Coordinator(registry=registry)
    coordinator.initialize()
    server = MCPServer(coordinator)

    asyncio.run(server.run())
""")


def _make_server_params(tools_dir: Path) -> StdioServerParameters:
    """Create StdioServerParameters pointing to our test server."""
    project_root = str(Path(__file__).resolve().parent.parent.parent)
    script = _SERVER_SCRIPT.format(project_root=project_root)
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", script, str(tools_dir)],
    )


async def _run_test(async_test_fn, tools_dir: Path) -> None:
    """Boilerplate: spin up stdio_client + session, run test, tear down."""
    params = _make_server_params(tools_dir)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await async_test_fn(session)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tools_dir(tmp_path: Path) -> Path:
    """Create a temporary tools directory with test tools."""
    d = tmp_path / "tools"
    d.mkdir()

    cat_dir = d / "test"
    cat_dir.mkdir()
    (cat_dir / "echo.py").write_text(
        textwrap.dedent("""\
            from evermcp.core.tool import tool

            @tool(description="Echo the input back")
            def echo(msg: str) -> dict:
                return {"echoed": msg}
        """),
        encoding="utf-8",
    )

    (cat_dir / "fail.py").write_text(
        textwrap.dedent("""\
            from evermcp.core.tool import tool

            @tool(description="Always fails")
            def fail(msg: str) -> dict:
                raise RuntimeError("intentional failure")
        """),
        encoding="utf-8",
    )

    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_list_tools(tools_dir: Path) -> None:
    """Server should list tools discovered from the tools directory."""

    async def _test(session: ClientSession) -> None:
        result = await session.list_tools()
        tool_names = [t.name for t in result.tools]
        assert "test.echo" in tool_names
        assert "test.fail" in tool_names
        assert len(result.tools) == 2

    asyncio.run(_run_test(_test, tools_dir))


def test_call_tool_success(tools_dir: Path) -> None:
    """Calling test.echo should return the echoed message."""

    async def _test(session: ClientSession) -> None:
        result = await session.call_tool("test.echo", {"msg": "hello world"})
        assert len(result.content) == 1
        text = result.content[0].text
        data = json.loads(text)
        assert data == {"echoed": "hello world"}

    asyncio.run(_run_test(_test, tools_dir))


def test_call_tool_error(tools_dir: Path) -> None:
    """Calling test.fail should return an error with code prefix."""

    async def _test(session: ClientSession) -> None:
        result = await session.call_tool("test.fail", {"msg": "trigger"})
        # MCP wraps errors in CallToolResult with isError=True
        assert result.isError is True
        # The error message should contain the error code prefix
        text = result.content[0].text
        assert "[-32003]" in text
        assert "intentional failure" in text

    asyncio.run(_run_test(_test, tools_dir))


def test_call_tool_not_found(tools_dir: Path) -> None:
    """Calling a nonexistent tool should return TOOL_NOT_FOUND error."""

    async def _test(session: ClientSession) -> None:
        result = await session.call_tool("nonexistent.tool", {})
        assert result.isError is True
        text = result.content[0].text
        assert "[-32001]" in text
        assert "not found" in text.lower()

    asyncio.run(_run_test(_test, tools_dir))


def test_tool_input_schema(tools_dir: Path) -> None:
    """Tool descriptors should include proper JSON Schema."""

    async def _test(session: ClientSession) -> None:
        result = await session.list_tools()
        echo_tool = next(t for t in result.tools if t.name == "test.echo")
        schema = echo_tool.inputSchema
        assert schema["type"] == "object"
        assert "msg" in schema["properties"]
        assert schema["properties"]["msg"]["type"] == "string"
        assert "msg" in schema["required"]

    asyncio.run(_run_test(_test, tools_dir))
