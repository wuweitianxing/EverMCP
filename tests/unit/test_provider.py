"""Unit tests for LocalFilesystemProvider."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from evermcp.core.capability import CapabilityKind
from evermcp.core.provider import LocalFilesystemProvider

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tools_dir(tmp_path: Path) -> Path:
    """Create a temp tools dir with one @tool under tools/cat/foo.py."""
    d = tmp_path / "tools"
    (d / "cat").mkdir(parents=True)
    (d / "cat" / "foo.py").write_text(
        textwrap.dedent("""\
            from evermcp.core.tool import tool

            @tool(description="A small tool for tests")
            def foo(x: int) -> dict:
                return {"echo": x}
        """),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def empty_tools_dir(tmp_path: Path) -> Path:
    """Create a temp tools dir with no tool files."""
    d = tmp_path / "tools"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# list_capabilities / get
# ---------------------------------------------------------------------------


class TestListCapabilities:
    def test_empty_dir_returns_empty_list(self, empty_tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=empty_tools_dir)
        provider.scan()
        assert provider.list_capabilities() == []

    def test_one_tool_returns_one_capability(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        caps = provider.list_capabilities()
        assert len(caps) == 1
        cap = caps[0]
        assert cap.name == "cat.foo"
        assert cap.kind == CapabilityKind.TOOL

    def test_capability_descriptor_shape(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        cap = provider.list_capabilities()[0]
        desc = cap.descriptor()
        assert desc["name"] == "cat.foo"
        assert desc["description"] == "A small tool for tests"
        assert isinstance(desc["input_schema"], dict)
        assert desc["category"] == "cat"
        # input_schema reflects `x: int`
        assert "x" in desc["input_schema"]["properties"]


class TestGet:
    def test_get_existing(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        cap = provider.get("cat.foo")
        assert cap is not None
        assert cap.name == "cat.foo"

    def test_get_nonexistent_returns_none(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        assert provider.get("nonexistent") is None

    def test_get_before_scan_returns_none(self, tools_dir: Path) -> None:
        # Without scan(), internal store is empty.
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        assert provider.get("cat.foo") is None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_true(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        assert provider.health() is True

    def test_health_true_even_when_empty(self, empty_tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=empty_tools_dir)
        provider.scan()
        assert provider.health() is True


# ---------------------------------------------------------------------------
# call (async)
# ---------------------------------------------------------------------------


class TestCall:
    @pytest.mark.asyncio
    async def test_call_returns_tool_result(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        result = await provider.call("cat.foo", {"x": 1})
        assert result == {"echo": 1}

    @pytest.mark.asyncio
    async def test_call_with_other_args(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        result = await provider.call("cat.foo", {"x": 42})
        assert result == {"echo": 42}

    @pytest.mark.asyncio
    async def test_call_nonexistent_raises_keyerror(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        with pytest.raises(KeyError):
            await provider.call("nonexistent", {})


# ---------------------------------------------------------------------------
# start_watching — must not raise even without watchdog
# ---------------------------------------------------------------------------


class TestStartWatching:
    def test_start_watching_does_not_raise(self, tools_dir: Path) -> None:
        """Even if watchdog is missing, start_watching() must swallow ImportError."""
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        provider.scan()
        # Must not raise, regardless of whether watchdog is installed.
        provider.start_watching()
        # Clean up if watchdog was available and watcher was started.
        provider.stop_watching()


# ---------------------------------------------------------------------------
# tools_dir property + source tag
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_tools_dir_reflects_constructor(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        assert provider.tools_dir == tools_dir

    def test_source_is_local(self, tools_dir: Path) -> None:
        provider = LocalFilesystemProvider(tools_dir=tools_dir)
        assert provider.source == "local"
        provider.scan()
        cap = provider.list_capabilities()[0]
        assert cap.source == "local"
