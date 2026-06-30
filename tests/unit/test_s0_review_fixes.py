"""Regression tests for the S0 code-review fixes.

These tests pin the behavior added/changed by fixes #1, #2, #7, and #8 from
``docs/s0-code-review-tmp.md``. If any of them fail, the corresponding
fix has regressed and must be re-applied.

Not exhaustive — they cover the surface area that was actually changed
by the fixes, not the wider feature.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from evermcp.core.capability import CapabilityKind, PromptFunc, ResourceFunc
from evermcp.core.registry import ToolRegistry
from evermcp.core.tool import (
    SECURITY_VIOLATION,
    TOOL_NOT_FOUND,
    TOOL_TIMEOUT,
    ToolContext,
    tool,
)
from evermcp.protocol.coordinator import Coordinator


# ---------------------------------------------------------------------------
# Fix #1: async error envelope must classify SecurityViolation / timeout
# ---------------------------------------------------------------------------


class TestAsyncErrorClassification:
    """call_tool_async must mirror LocalWorker's classification order."""

    @pytest.mark.asyncio
    async def test_security_violation_yields_minus_32005(self, tmp_path: Path) -> None:
        """SecurityViolation → SECURITY_VIOLATION, NOT TOOL_EXCEPTION."""
        _write_tool(
            tmp_path,
            "guard",
            "guarded",
            """
            from evermcp.core.tool import tool
            from evermcp.security.safepath import SecurityViolation

            @tool(description="Raise a SecurityViolation")
            def guarded() -> dict:
                raise SecurityViolation("blocked by policy")
            """,
        )
        coord = _build_coordinator(tmp_path)
        try:
            env = await coord.call_tool_async("guard.guarded", {})
            assert env["success"] is False
            assert env["error"]["code"] == SECURITY_VIOLATION
            assert "Security violation" in env["error"]["message"]
        finally:
            coord.shutdown()

    @pytest.mark.asyncio
    async def test_timeout_runtimeerror_yields_minus_32002(self, tmp_path: Path) -> None:
        """RuntimeError('timeout') → TOOL_TIMEOUT, NOT TOOL_EXCEPTION."""
        _write_tool(
            tmp_path,
            "slow",
            "slow_op",
            """
            from evermcp.core.tool import tool

            @tool(description="Time out")
            def slow_op() -> dict:
                raise RuntimeError("operation timeout after 60s")
            """,
        )
        coord = _build_coordinator(tmp_path)
        try:
            env = await coord.call_tool_async("slow.slow_op", {})
            assert env["success"] is False
            assert env["error"]["code"] == TOOL_TIMEOUT
            assert "timed out" in env["error"]["message"].lower()
        finally:
            coord.shutdown()

    @pytest.mark.asyncio
    async def test_missing_tool_yields_minus_32001(self, tmp_path: Path) -> None:
        """KeyError from registry.call → TOOL_NOT_FOUND."""
        coord = _build_coordinator(tmp_path)
        try:
            env = await coord.call_tool_async("never.registered", {})
            assert env["success"] is False
            assert env["error"]["code"] == TOOL_NOT_FOUND
        finally:
            coord.shutdown()


# ---------------------------------------------------------------------------
# Fix #7: ResourceFunc and PromptFunc both accept ctx OR _ctx
# ---------------------------------------------------------------------------


class TestCtxInjectionParity:
    """Both ``ctx`` and ``_ctx`` spellings should receive the live context."""

    @pytest.mark.asyncio
    async def test_resource_with_ctx_kwarg_receives_context(self) -> None:
        seen: dict[str, ToolContext | None] = {}

        from evermcp.core.capability import resource as resource_dec

        @resource_dec(uri="x://y", description="t")
        def res(ctx: ToolContext | None = None) -> str:
            seen["ctx"] = ctx
            return "ok"

        ctx = ToolContext(cwd=".", logger=None)  # type: ignore[arg-type]
        result = await res.call(ctx=ctx)
        assert result == "ok"
        assert seen["ctx"] is ctx

    @pytest.mark.asyncio
    async def test_resource_with_underscore_ctx_kwarg_receives_context(self) -> None:
        """Regression for fix #7: ResourceFunc must accept _ctx like PromptFunc."""
        seen: dict[str, ToolContext | None] = {}

        from evermcp.core.capability import resource as resource_dec

        @resource_dec(uri="x://y2", description="t")
        def res(_ctx: ToolContext | None = None) -> str:
            seen["_ctx"] = _ctx
            return "ok"

        ctx = ToolContext(cwd=".", logger=None)  # type: ignore[arg-type]
        result = await res.call(ctx=ctx)
        assert result == "ok"
        assert seen["_ctx"] is ctx

    @pytest.mark.asyncio
    async def test_prompt_with_underscore_ctx_kwarg_receives_context(self) -> None:
        """PromptFunc (already had _ctx handling) keeps its behavior."""
        seen: dict[str, ToolContext | None] = {}

        from evermcp.core.capability import prompt as prompt_dec

        @prompt_dec(description="t")
        def pr(_ctx: ToolContext | None = None) -> str:
            seen["_ctx"] = _ctx
            return "prompt text"

        ctx = ToolContext(cwd=".", logger=None)  # type: ignore[arg-type]
        result = await pr.call({}, ctx=ctx)
        assert result == "prompt text"
        assert seen["_ctx"] is ctx


# ---------------------------------------------------------------------------
# Fix #8: MCP resource/prompt handlers translate KeyError → [-32001] envelope
# ---------------------------------------------------------------------------


class TestMcpHandlerKeyErrorTranslation:
    """Direct exercise of the MCP handler surface."""

    @pytest.mark.asyncio
    async def test_read_resource_missing_uri_raises_mcp_error(self) -> None:
        """A missing resource URI surfaces as a McpError with the same
        `[-32001] Resource not found: <uri>` envelope used by call_tool."""
        from mcp.shared.exceptions import McpError

        from evermcp.protocol.mcp_server import MCPServer

        coord = Coordinator(registry=ToolRegistry(tools_dir=None))
        try:
            # Force coord.list_resources() to return []; nothing registered.
            srv = MCPServer(coord)
            handler = srv.server.request_handlers
            from mcp.types import ReadResourceRequest

            handler_fn = handler.get(ReadResourceRequest)
            if handler_fn is None:
                pytest.skip("SDK layout differs; skipping KeyError-path test")
            # Synthesize a request object — the handler reads .params.uri.
            class _Req:
                def __init__(self, uri):
                    self.params = type("P", (), {"uri": uri})()

            try:
                await handler_fn(_Req("evermcp://does/not/exist"))
            except McpError as exc:
                assert "Resource not found" in exc.error.message
                assert "[-32001]" in exc.error.message
                return
            pytest.fail("Expected McpError for missing resource URI")
        finally:
            coord.shutdown()

    @pytest.mark.asyncio
    async def test_get_prompt_missing_name_raises_mcp_error(self) -> None:
        """A missing prompt name surfaces as McpError with the same envelope."""
        from mcp.shared.exceptions import McpError

        from evermcp.protocol.mcp_server import MCPServer

        coord = Coordinator(registry=ToolRegistry(tools_dir=None))
        try:
            srv = MCPServer(coord)
            handler = srv.server.request_handlers
            from mcp.types import GetPromptRequest

            handler_fn = handler.get(GetPromptRequest)
            if handler_fn is None:
                pytest.skip("SDK layout differs; skipping KeyError-path test")

            class _Req:
                def __init__(self, name, arguments):
                    self.params = type("P", (), {"name": name, "arguments": arguments})()

            try:
                await handler_fn(_Req("never.registered", {}))
            except McpError as exc:
                assert "Prompt not found" in exc.error.message
                assert "[-32001]" in exc.error.message
                return
            pytest.fail("Expected McpError for missing prompt name")
        finally:
            coord.shutdown()


# ---------------------------------------------------------------------------
# Fix #2: failed exec_module must not leave a half-initialized module behind
# ---------------------------------------------------------------------------


class TestFailedModuleLoadCleanup:
    """Hot-reload must clean up sys.modules when a tool file fails to load."""

    def test_failed_load_does_not_leak_module(self, tmp_path: Path) -> None:
        # Write a tool file with a deliberate syntax error.
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "boom.py").write_text(
            "def not valid python @@@\n",
            encoding="utf-8",
        )

        # Pre-condition: nothing under the broken name yet.
        assert "tools.broken.boom" not in sys.modules

        registry = ToolRegistry(tools_dir=tmp_path)
        registry.scan()

        # Post-condition: still nothing under the broken name.
        assert "tools.broken.boom" not in sys.modules, (
            "Failed exec_module must not leave a half-initialized module "
            "in sys.modules — fix #2 regression"
        )


# ---------------------------------------------------------------------------
# Fix #3: ToolRegistry must not rely on the dead __dict__ hack
# ---------------------------------------------------------------------------


def test_tool_registry_tools_dir_is_a_path(tmp_path: Path) -> None:
    """Even after dropping the __dict__ shadow, the legacy accessor must
    still return the resolved Path so the v0.2.0 hot-reload tests pass."""
    (tmp_path / "demo").mkdir()
    registry = ToolRegistry(tools_dir=tmp_path)
    from pathlib import Path

    assert isinstance(registry.tools_dir, Path)
    assert registry.tools_dir == tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_tool(tmp_path: Path, category: str, name: str, body: str) -> None:
    cat_dir = tmp_path / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")


def _build_coordinator(tmp_path: Path) -> Coordinator:
    registry = ToolRegistry(tools_dir=tmp_path)
    coord = Coordinator(registry=registry)
    coord.initialize()
    return coord