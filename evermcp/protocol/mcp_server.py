"""MCP stdio server — bridges MCP protocol to Coordinator."""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.shared.exceptions import McpError

from evermcp.protocol.coordinator import Coordinator

logger = logging.getLogger(__name__)


class MCPServer:
    """stdio MCP server that delegates to a Coordinator."""

    def __init__(self, coordinator: Coordinator) -> None:
        self._coordinator = coordinator
        self._server: Server = Server("evermcp")
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self._server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            descriptors = self._coordinator.list_tools()
            return [
                types.Tool(
                    name=d["name"],
                    description=d["description"],
                    inputSchema=d["input_schema"],
                )
                for d in descriptors
            ]

        @self._server.call_tool()
        async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.ContentBlock]:
            result = self._coordinator.call_tool(name, arguments)
            if result["success"]:
                return [
                    types.TextContent(
                        type="text",
                        text=json.dumps(result["result"], ensure_ascii=False, indent=2),
                    )
                ]
            err = result["error"]
            # MCP doesn't support native error code extension;
            # embed the code as a prefix so the AI can read it directly.
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=f"[{err['code']}] {err['message']}: {err.get('data', {})}",
                )
            )

    async def run(self) -> None:
        """Run the MCP server on stdio transport."""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )
