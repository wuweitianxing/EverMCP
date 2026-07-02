"""Integration tests — ``ToolRegistry`` (CLI production config) exposes remote tools.

Regression coverage for an S2 blind spot: the CLI ``serve`` command builds its
``Coordinator`` on a :class:`evermcp.core.registry.ToolRegistry` (a
``CapabilityRegistry`` subclass), while the S2 e2e tests used the plain
``CapabilityRegistry``. ``ToolRegistry`` historically overrode
``list_tools()`` and ``get()`` to consult *only* the local filesystem
provider, which silently:

1. dropped remote tools from ``tools/list`` (Agents never saw them), and
2. made ``Coordinator.call_tool_async`` resolve ``cap`` to ``None`` for remote
   tools via ``registry.get(name)``, mis-classifying their ``source`` as
   ``"local"`` and so never entering the remote-call timeout branch.

These tests build a ``Coordinator`` on a ``ToolRegistry`` (matching the CLI),
register a fake remote provider, and assert remote tools are both listed and
correctly routed — closing the production-config blind spot.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine

from evermcp.core.capability import Capability, CapabilityKind
from evermcp.core.registry import ToolRegistry
from evermcp.core.tool import ToolContext
from evermcp.protocol.coordinator import Coordinator
from evermcp.security.config import Config
from evermcp.storage import init_db

# ---------------------------------------------------------------------------
# Minimal fake remote provider + capability (duck-typed CapabilityProvider)
# ---------------------------------------------------------------------------


class _FakeRemoteCapability:
    """A minimal ``Capability`` standing in for a remote MCP tool."""

    kind = CapabilityKind.TOOL
    enabled = True

    def __init__(self, client_id: str, tool_name: str) -> None:
        self.name = f"remote.{client_id}.{tool_name}"
        self.source = f"remote.{client_id}"
        self.description = f"fake remote tool {tool_name}"

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {"type": "object"},
        }

    async def call(
        self,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        return {"echo": args, "client": self.source}


class _FakeRemoteProvider:
    """A minimal provider exposing one fake remote tool.

    Mirrors the relevant surface of
    :class:`evermcp.core.provider.RemoteClientProvider` (source prefix,
    ``remote.<client_id>.`` names) without needing a live WebSocket session.
    """

    source = "remote.test-bot"

    def __init__(self) -> None:
        self._cap = _FakeRemoteCapability("test-bot", "search")

    def list_capabilities(self) -> list[Capability]:
        return [self._cap]  # type: ignore[list-item]

    def get(self, name: str) -> Capability | None:
        return self._cap if name == self._cap.name else None  # type: ignore[return-value]

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        if name != self._cap.name:
            raise KeyError(name)
        return await self._cap.call(args, ctx)

    def health(self) -> bool:
        return True


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
def coordinator(in_memory_engine, monkeypatch):
    """A Coordinator backed by a ``ToolRegistry`` (CLI production config).

    ``tools_dir`` points at an empty temp dir so the local filesystem
    provider contributes no tools — the only capability comes from the fake
    remote provider, which makes the assertions unambiguous. The storage
    engine is patched to in-memory SQLite so ``call_tool_async``'s CallLog
    write doesn't touch the real on-disk DB.
    """
    from evermcp import storage as storage_module

    monkeypatch.setattr(storage_module, "get_engine", lambda: in_memory_engine)

    registry = ToolRegistry(tools_dir="_nonexistent_tools_dir_for_test")
    registry.add_provider(_FakeRemoteProvider())
    coord = Coordinator(registry=registry, config=Config())
    return coord


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tool_registry_exposes_remote_tools(coordinator):
    """``ToolRegistry``-backed ``coordinator.list_tools()`` includes remote tools.

    This is the core regression: with the old ``ToolRegistry.list_tools()``
    override (local-only), the remote ``search`` tool was absent from
    ``tools/list`` in CLI ``serve`` mode. After deleting the override, the
    inherited ``CapabilityRegistry.list_tools()`` traverses all providers.
    """
    names = [t["name"] for t in coordinator.list_tools()]
    assert "remote.test-bot.search" in names


def test_tool_registry_get_capability_resolves_remote_source(coordinator):
    """``get_capability`` returns the remote Capability with its ``source``.

    ``ToolRegistry.get()`` is intentionally kept returning a ``ToolFunc``
    (``None`` for remote tools) for the synchronous ``LocalWorker`` path.
    ``get_capability`` is the provider-traversing accessor the Coordinator
    uses to read ``source`` — it must resolve remote tools so the
    remote-call timeout branch is reachable.
    """
    cap = coordinator.registry.get_capability("remote.test-bot.search")
    assert cap is not None
    assert cap.source == "remote.test-bot"

    # Contrast: the legacy ``get()`` (ToolFunc path) does not see remote tools.
    assert coordinator.registry.get("remote.test-bot.search") is None


@pytest.mark.asyncio
async def test_tool_registry_call_async_routes_remote(coordinator):
    """``call_tool_async`` round-trips a remote call through ``ToolRegistry``.

    Verifies end-to-end that the source is correctly resolved as
    ``remote...`` (not mis-classified as ``local``), the remote provider's
    ``call()`` is invoked, and the result envelope is returned.
    """
    result = await coordinator.call_tool_async("remote.test-bot.search", {"q": "mcp"})
    assert result["success"] is True
    assert result["result"]["echo"] == {"q": "mcp"}
    assert result["result"]["client"] == "remote.test-bot"
