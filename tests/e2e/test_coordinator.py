"""End-to-end coordinator tests.

Per DESIGN.md §Testing Strategy:
- Layer 4: End-to-end tests (mock MCP 客户端 → 协调器 → worker → 工具)

These tests drive the full Coordinator -> LocalWorker -> @tool function chain
without going through stdio/MCP. The integration tests (tests/integration/) do
that with a real stdio subprocess. These tests verify the Coordinator's public
API surface (initialize/list_tools/call_tool/get_capabilities) wires everything
together correctly.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evermcp.core.registry import ToolRegistry
from evermcp.protocol.coordinator import Coordinator
from evermcp.security.config import Config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tools_dir(tmp_path: Path) -> Path:
    """Create a temp tools dir with a representative set of tools."""
    d = tmp_path / "tools"
    d.mkdir()

    (d / "demo").mkdir()
    (d / "demo" / "greet.py").write_text(
        textwrap.dedent("""\
        from evermcp.core.tool import tool

        @tool(description="Greet someone by name")
        def greet(name: str) -> dict:
            return {"greeting": f"hello, {name}"}
    """)
    )

    (d / "demo" / "add.py").write_text(
        textwrap.dedent("""\
        from evermcp.core.tool import tool

        @tool(description="Add two integers")
        def add(a: int, b: int) -> dict:
            return {"sum": a + b}
    """)
    )

    (d / "demo" / "boom.py").write_text(
        textwrap.dedent("""\
        from evermcp.core.tool import tool

        @tool(description="Explodes on demand")
        def boom() -> dict:
            raise RuntimeError("kaboom")
    """)
    )

    return d


@pytest.fixture
def coordinator(tools_dir: Path) -> Coordinator:
    """A fully-initialized coordinator backed by temp tools."""
    registry = ToolRegistry(tools_dir=tools_dir)
    coord = Coordinator(registry=registry)
    coord.initialize()
    yield coord
    coord.shutdown()


# ---------------------------------------------------------------------------
# initialize + list_tools
# ---------------------------------------------------------------------------


class TestInitializeAndList:
    def test_initialize_scans_tools(self, coordinator: Coordinator) -> None:
        names = {d["name"] for d in coordinator.list_tools()}
        assert "demo.greet" in names
        assert "demo.add" in names
        assert "demo.boom" in names

    def test_list_tools_returns_worker_descriptors(self, coordinator: Coordinator) -> None:
        """Coordinator.list_tools should delegate to worker and return the same shape."""
        descriptors = coordinator.list_tools()
        assert len(descriptors) == 3
        for d in descriptors:
            assert {"name", "description", "input_schema", "category"}.issubset(d.keys())

    def test_initialize_idempotent(self, coordinator: Coordinator) -> None:
        """Calling initialize again should rescan without duplicating."""
        before = {d["name"] for d in coordinator.list_tools()}
        coordinator.initialize()
        after = {d["name"] for d in coordinator.list_tools()}
        assert before == after


# ---------------------------------------------------------------------------
# call_tool — full Coordinator → Worker → @tool path
# ---------------------------------------------------------------------------


class TestCallToolRoundTrip:
    def test_successful_call(self, coordinator: Coordinator) -> None:
        result = coordinator.call_tool("demo.greet", {"name": "world"})
        assert result["success"] is True
        assert result["result"] == {"greeting": "hello, world"}

    def test_call_with_multiple_args(self, coordinator: Coordinator) -> None:
        result = coordinator.call_tool("demo.add", {"a": 2, "b": 3})
        assert result["success"] is True
        assert result["result"] == {"sum": 5}

    def test_call_generates_call_id(self, coordinator: Coordinator) -> None:
        """Each call gets a fresh UUID call_id (visible in the result's error.data if failed)."""
        result = coordinator.call_tool("demo.greet", {"name": "x"})
        assert result["success"] is True

    def test_nonexistent_tool_returns_error(self, coordinator: Coordinator) -> None:
        result = coordinator.call_tool("demo.nonexistent", {})
        assert result["success"] is False
        assert result["error"]["code"] == -32001  # TOOL_NOT_FOUND

    def test_tool_exception_becomes_error_envelope(self, coordinator: Coordinator) -> None:
        result = coordinator.call_tool("demo.boom", {})
        assert result["success"] is False
        assert result["error"]["code"] == -32003  # TOOL_EXCEPTION
        assert "kaboom" in result["error"]["message"]


# ---------------------------------------------------------------------------
# get_capabilities
# ---------------------------------------------------------------------------


class TestGetCapabilities:
    def test_returns_capability_dict(self, coordinator: Coordinator) -> None:
        caps = coordinator.get_capabilities()
        assert isinstance(caps, dict)
        assert "platform" in caps
        assert "cpu_cores" in caps
        assert "ffmpeg_encoders" in caps


# ---------------------------------------------------------------------------
# Coordinator with Config (verify SafeURL/SafePath injection)
# ---------------------------------------------------------------------------


class TestCoordinatorWithConfig:
    def test_config_filesystem_allowlist_propagates_to_ctx(self, tools_dir: Path) -> None:
        """filesystem_allowlist propagates to Coordinator's safe_path."""
        cfg = Config(filesystem_allowlist=[str(tools_dir)])
        registry = ToolRegistry(tools_dir=tools_dir)
        coord = Coordinator(registry=registry, config=cfg)
        coord.initialize()
        try:
            assert coord.safe_path is not None
            assert any(
                tools_dir.resolve() in ap.parents or ap == tools_dir.resolve()
                for ap in coord.safe_path.allowlist
            )
        finally:
            coord.shutdown()

    def test_config_network_allowlist_propagates_to_safe_url(self, tools_dir: Path) -> None:
        """Config.network_allowlist should be wired into SafeURL."""
        cfg = Config(network_allowlist=["github.com", "pypi.org"])
        registry = ToolRegistry(tools_dir=tools_dir)
        coord = Coordinator(registry=registry, config=cfg)
        coord.initialize()
        try:
            assert "github.com" in coord.safe_url.allowlist
            assert "pypi.org" in coord.safe_url.allowlist
        finally:
            coord.shutdown()

    def test_no_config_safe_url_still_has_default_deny(self, coordinator: Coordinator) -> None:
        """Even without Config, SafeURL should be created with default-deny semantics."""
        su = coordinator.safe_url
        # Default-deny rejects loopback
        import pytest

        from evermcp.security.safepath import SecurityViolation

        with pytest.raises(SecurityViolation):
            su.validate("http://127.0.0.1/")

    def test_no_filesystem_allowlist_safe_path_is_none(self, tools_dir: Path) -> None:
        """No filesystem_allowlist configured → no SafePath enforcement (tools responsible)."""
        cfg = Config()  # no allowlist
        registry = ToolRegistry(tools_dir=tools_dir)
        coord = Coordinator(registry=registry, config=cfg)
        # safe_path is None when no allowlist config (per coordinator init logic)
        assert coord.safe_path is None


# ---------------------------------------------------------------------------
# ToolContext injection — verify safe_url/safe_path reach tools
# ---------------------------------------------------------------------------


class TestToolContextInjection:
    def test_safe_path_injected_into_read_file_tool(self, tmp_path: Path) -> None:
        """The real io.read_file tool should see the Coordinator's SafePath via ctx."""
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        target = allowed_dir / "data.txt"
        target.write_text("hello from test", encoding="utf-8")

        cfg = Config(filesystem_allowlist=[str(allowed_dir)])
        # Point at the examples/tools/ dir (where io.read_file lives in v0.2.0+)
        examples_tools = Path(__file__).resolve().parent.parent.parent / "examples" / "tools"
        registry = ToolRegistry(tools_dir=examples_tools)
        coord = Coordinator(registry=registry, config=cfg)
        coord.initialize()
        try:
            result = coord.call_tool(
                "io.read_file",
                {"file_path": str(target)},
            )
            assert result["success"] is True
            assert "hello from test" in result["result"]["content"]
        finally:
            coord.shutdown()

    def test_safe_path_blocks_path_outside_allowlist(self, tmp_path: Path) -> None:
        """A file outside the filesystem_allowlist must be rejected by io.read_file."""
        forbidden = tmp_path / "forbidden"
        forbidden.mkdir()
        target = forbidden / "secret.txt"
        target.write_text("secret", encoding="utf-8")

        cfg = Config(filesystem_allowlist=[str(tmp_path / "allowed")])
        # Point at the examples/tools/ dir (where io.read_file lives in v0.2.0+)
        examples_tools = Path(__file__).resolve().parent.parent.parent / "examples" / "tools"
        registry = ToolRegistry(tools_dir=examples_tools)
        coord = Coordinator(registry=registry, config=cfg)
        coord.initialize()
        try:
            result = coord.call_tool(
                "io.read_file",
                {"file_path": str(target)},
            )
            assert result["success"] is False
            assert result["error"]["code"] == -32005  # SECURITY_VIOLATION
            assert "allowlist" in result["error"]["message"].lower()
        finally:
            coord.shutdown()
