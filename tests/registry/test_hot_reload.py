"""Hot-reload integration tests for ToolRegistry + watchdog.

Scenarios:
1. Create a tool file → registry picks it up within 5s
2. Modify a tool file → descriptor updates within 5s
3. Delete a tool file → tool disappears within 5s
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from evermcp.core.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOL_SOURCE_INITIAL = """\
from evermcp.core.tool import tool

@tool(description="Initial description")
def foo(x: int) -> dict:
    return {"x": x}
"""

TOOL_SOURCE_MODIFIED = """\
from evermcp.core.tool import tool

@tool(description="Modified description")
def foo(x: int) -> dict:
    return {"x": x}
"""


@pytest.fixture
def tools_dir(tmp_path: Path) -> Path:
    """Create a temporary tools directory with no tools."""
    d = tmp_path / "tools"
    d.mkdir()
    return d


@pytest.fixture
def registry(tools_dir: Path) -> ToolRegistry:
    """Create a registry backed by the temp tools dir."""
    return ToolRegistry(tools_dir=tools_dir)


def _wait_for(
    condition,
    timeout: float = 5.0,
    interval: float = 0.2,
) -> bool:
    """Poll until condition() returns True or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Test 1: Create tool file → appears in registry
# ---------------------------------------------------------------------------


def test_create_tool_file_appears(registry: ToolRegistry, tools_dir: Path) -> None:
    """Writing a new .py file in tools/<category>/ should register the tool within 5s."""
    # Start with 0 tools
    registry.scan()
    assert registry.list_tools() == []

    # Start watching
    registry.start_watching()

    try:
        # Create category dir + tool file
        cat_dir = tools_dir / "test"
        cat_dir.mkdir(exist_ok=True)
        tool_file = cat_dir / "foo.py"
        tool_file.write_text(TOOL_SOURCE_INITIAL, encoding="utf-8")

        # Wait for registry to pick it up
        found = _wait_for(lambda: any(t["name"] == "test.foo" for t in registry.list_tools()))
        assert found, "Tool 'test.foo' did not appear within 5s after file creation"

        tools = registry.list_tools()
        foo_tool = next(t for t in tools if t["name"] == "test.foo")
        assert foo_tool["description"] == "Initial description"
    finally:
        registry.stop_watching()


# ---------------------------------------------------------------------------
# Test 2: Modify tool file → descriptor updates
# ---------------------------------------------------------------------------


def test_modify_tool_file_updates(registry: ToolRegistry, tools_dir: Path) -> None:
    """Changing the @tool description should update the descriptor within 5s."""
    cat_dir = tools_dir / "test"
    cat_dir.mkdir(exist_ok=True)
    tool_file = cat_dir / "foo.py"
    tool_file.write_text(TOOL_SOURCE_INITIAL, encoding="utf-8")

    registry.scan()
    registry.start_watching()

    try:
        # Verify initial state
        tools = registry.list_tools()
        assert any(t["name"] == "test.foo" for t in tools)

        # Modify the file (description change)
        # Use a small sleep + write to ensure filesystem event fires on Windows
        time.sleep(0.3)
        tool_file.write_text(TOOL_SOURCE_MODIFIED, encoding="utf-8")

        # Wait for updated descriptor
        found = _wait_for(
            lambda: any(
                t["name"] == "test.foo" and t["description"] == "Modified description"
                for t in registry.list_tools()
            )
        )
        assert found, "Tool 'test.foo' description did not update within 5s after modification"
    finally:
        registry.stop_watching()


# ---------------------------------------------------------------------------
# Test 3: Delete tool file → tool disappears
# ---------------------------------------------------------------------------


def test_delete_tool_file_disappears(registry: ToolRegistry, tools_dir: Path) -> None:
    """Deleting the .py file should unregister the tool within 5s."""
    cat_dir = tools_dir / "test"
    cat_dir.mkdir(exist_ok=True)
    tool_file = cat_dir / "foo.py"
    tool_file.write_text(TOOL_SOURCE_INITIAL, encoding="utf-8")

    registry.scan()
    registry.start_watching()

    try:
        # Verify initial state
        assert any(t["name"] == "test.foo" for t in registry.list_tools())

        # Delete the file
        tool_file.unlink()

        # Wait for tool to disappear
        found = _wait_for(lambda: not any(t["name"] == "test.foo" for t in registry.list_tools()))
        assert found, "Tool 'test.foo' did not disappear within 5s after deletion"
    finally:
        registry.stop_watching()
