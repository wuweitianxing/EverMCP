"""MCP stdio server — bridges MCP protocol to Coordinator.

Exposes the underlying ``mcp.server.Server`` instance so it can be shared
with alternative transports (e.g. Streamable HTTP — see
``evermcp.protocol.http_server.HTTPServer``).
"""

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


# ---------------------------------------------------------------------------
# Adapter for the SDK's read_resource handler protocol
# ---------------------------------------------------------------------------

class _ReadResourceContent:
    """Adapter satisfying the SDK's ``read_resource`` return contract.

    The official ``mcp.server.Server.read_resource`` decorator accepts a
    list of objects exposing ``.content`` (str|bytes) and ``.mime_type``
    (str|None); it wraps each into ``TextResourceContents`` /
    ``BlobResourceContents`` internally. The MCP Python SDK doesn't
    publish a public ``ReadResourceContents`` type for this protocol, so
    we define the smallest possible adapter here.

    Note: this is a stable private interface contract with the SDK, but
    it is not part of any public API surface. If the SDK changes its
    contract in a future release, this class is the single place to
    adjust.
    """

    __slots__ = ("content", "mime_type")

    def __init__(self, content: Any, mime_type: str) -> None:
        self.content = content
        self.mime_type = mime_type


class MCPServer:
    """stdio MCP server that delegates to a Coordinator.

    The internal ``Server`` instance is exposed via ``self.server`` (and via
    ``build_mcp_server``) so the same handler registration can be reused by
    the Streamable HTTP transport in :mod:`evermcp.protocol.http_server`.
    """

    def __init__(self, coordinator: Coordinator) -> None:
        self._coordinator = coordinator
        self.server: Server = Server("evermcp")
        self._register_handlers()

    # ------------------------------------------------------------------
    # Public helpers — used by HTTPServer to share the same Server
    # ------------------------------------------------------------------

    def build_mcp_server(self) -> Server:
        """Return the underlying ``mcp.server.Server`` instance.

        Provided for symmetry with the standalone ``build_mcp_server``
        helper that some callers (tests, alternative transports) prefer.
        """
        return self.server

    @staticmethod
    def _build_for(coordinator: Coordinator) -> Server:
        """Construct a fully-wired ``Server`` for an arbitrary coordinator.

        Useful when callers want to skip instantiating ``MCPServer`` (which
        has no other state worth carrying in the HTTP path). Returns a
        brand-new Server instance with all stdio-compatible handlers
        registered — safe to pass straight to ``StreamableHTTPSessionManager``.
        """
        return MCPServer(coordinator).server

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        self._register_tool_handlers()
        self._register_resource_handlers()
        self._register_prompt_handlers()

    @staticmethod
    def _raise_not_found(label: str, key: str, code: int) -> None:
        """Raise a McpError formatted like the tool-call error envelope.

        Mirrors the convention used by ``_call_tool`` so AI clients see the
        same `[-code] message` prefix regardless of which primitive
        (tool / resource / prompt) produced the miss.

        Args:
            label: "Resource" / "Prompt" / "Tool" — used in the message.
            key:   the missing key (URI for resources, name otherwise).
            code:  our error code (e.g. -32001 for TOOL_NOT_FOUND).
        """
        raise McpError(
            types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"[{code}] {label} not found: {key}",
            )
        )

    def _register_tool_handlers(self) -> None:
        @self.server.list_tools()
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

        @self.server.call_tool()
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

    def _register_resource_handlers(self) -> None:
        @self.server.list_resources()
        async def _list_resources() -> list[types.Resource]:
            descriptors = self._coordinator.list_resources()
            out: list[types.Resource] = []
            for d in descriptors:
                try:
                    out.append(
                        types.Resource(
                            uri=d["uri"],  # type: ignore[arg-type]
                            name=d.get("name", d["uri"]),
                            description=d.get("description"),
                            mimeType=d.get("mimeType"),
                        )
                    )
                except Exception:  # pragma: no cover — defensive: skip malformed
                    logger.exception("Skipping malformed resource descriptor: %r", d)
            return out

        @self.server.read_resource()
        async def _read_resource(uri: Any):
            # MCP hands us an AnyUrl; coerce to str for the coordinator.
            uri_str = str(uri)
            try:
                content, mime = await self._coordinator.read_resource(uri_str)
            except KeyError:
                # Coordinator raises KeyError when no provider exposes the
                # requested URI. Translate to the same [-code] envelope used
                # by call_tool so the AI gets a uniform error shape across
                # all three MCP primitives.
                from evermcp.core.tool import TOOL_NOT_FOUND

                self._raise_not_found("Resource", uri_str, TOOL_NOT_FOUND)

            # SDK contract: list of objects with .content and .mime_type.
            # See _ReadResourceContent docstring for why this isn't a
            # public MCP type.
            return [_ReadResourceContent(content, mime)]

    def _register_prompt_handlers(self) -> None:
        @self.server.list_prompts()
        async def _list_prompts() -> list[types.Prompt]:
            descriptors = self._coordinator.list_prompts()
            out: list[types.Prompt] = []
            for d in descriptors:
                try:
                    args = d.get("arguments") or []
                    out.append(
                        types.Prompt(
                            name=d["name"],
                            description=d.get("description"),
                            arguments=[
                                types.PromptArgument(
                                    name=a["name"],
                                    description=a.get("description"),
                                    required=a.get("required", False),
                                )
                                for a in args
                            ],
                        )
                    )
                except Exception:  # pragma: no cover — defensive
                    logger.exception("Skipping malformed prompt descriptor: %r", d)
            return out

        @self.server.get_prompt()
        async def _get_prompt(
            name: str,
            arguments: dict[str, Any] | None = None,
        ) -> types.GetPromptResult:
            try:
                text = await self._coordinator.get_prompt(name, arguments or {})
            except KeyError:
                # See _read_resource: missing prompt → uniform [-32001] envelope.
                from evermcp.core.tool import TOOL_NOT_FOUND

                self._raise_not_found("Prompt", name, TOOL_NOT_FOUND)
            description = next(
                (p.get("description") or "" for p in self._coordinator.list_prompts() if p["name"] == name),
                "",
            )
            return types.GetPromptResult(
                description=description,
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text=text),
                    )
                ],
            )

    # ------------------------------------------------------------------
    # Backwards-compat alias — see spec for resource/prompt refactor.
    # ------------------------------------------------------------------

    def register_resources_handlers(self) -> None:
        """Idempotently register resource + prompt handlers.

        Safe to call multiple times; the underlying ``@server.<handler>()``
        decorators replace previous registrations on the same Server
        instance. Exists so the HTTP transport can opt in explicitly if it
        builds its own Server outside of ``__init__``.
        """
        self._register_resource_handlers()
        self._register_prompt_handlers()

    # ------------------------------------------------------------------
    # stdio transport
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the MCP server on stdio transport."""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )