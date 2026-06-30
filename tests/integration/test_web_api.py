"""Integration tests for S1 — InlineDeclarationProvider + Web API.

Tests cover:
- InlineDeclarationProvider CRUD operations
- REST API endpoints (/api/tree, /api/capabilities, /api/test)
- Capability enable/disable governance
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from evermcp.core.capability import CapabilityKind
from evermcp.core.provider import (
    InlineDeclarationProvider,
    _InlinePromptCapability,
    _InlineResourceCapability,
    _InlineToolCapability,
)
from evermcp.storage import DEFAULT_DB_URL, InlineCapability, init_db
from sqlalchemy import create_engine
from sqlmodel import Session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_engine():
    """Create an in-memory SQLite engine for testing."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


@pytest.fixture
def inline_provider(in_memory_engine):
    """Create an InlineDeclarationProvider with in-memory DB."""
    return InlineDeclarationProvider(engine=in_memory_engine)


@pytest.fixture
async def test_app(coordinator, in_memory_engine):
    """Create a FastAPI test app with inline provider."""
    from evermcp.web.app import create_app

    # Inject inline provider into coordinator registry
    provider = InlineDeclarationProvider(engine=in_memory_engine)
    coordinator.registry._providers.append(provider)

    app = create_app(coordinator)
    return app


# ---------------------------------------------------------------------------
# InlineDeclarationProvider tests
# ---------------------------------------------------------------------------


class TestInlineDeclarationProvider:
    """Tests for InlineDeclarationProvider CRUD operations."""

    def test_add_tool_capability(self, inline_provider, in_memory_engine):
        """Test adding a tool capability."""
        inline_provider.add_capability(
            kind="tool",
            name="test.translate",
            description="Translate text to a target language",
            schema_json=json.dumps({
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "lang": {"type": "string"},
                    },
                    "required": ["text", "lang"],
                }
            }),
        )

        caps = inline_provider.list_capabilities()
        assert len(caps) == 1
        assert caps[0].name == "test.translate"
        assert caps[0].kind == CapabilityKind.TOOL
        assert caps[0].source == "inline"

    def test_add_resource_capability(self, inline_provider):
        """Test adding a resource capability."""
        inline_provider.add_capability(
            kind="resource",
            name="test.config",
            description="Application configuration",
            schema_json=json.dumps({
                "uriTemplate": "config://app",
                "mimeType": "application/json",
            }),
        )

        caps = inline_provider.list_capabilities()
        assert len(caps) == 1
        assert caps[0].kind == CapabilityKind.RESOURCE

    def test_add_prompt_capability(self, inline_provider):
        """Test adding a prompt capability."""
        inline_provider.add_capability(
            kind="prompt",
            name="test.greet",
            description="Greeting prompt template",
            schema_json=json.dumps({
                "arguments": [
                    {"name": "name", "description": "User name", "required": True}
                ]
            }),
        )

        caps = inline_provider.list_capabilities()
        assert len(caps) == 1
        assert caps[0].kind == CapabilityKind.PROMPT

    def test_delete_capability(self, inline_provider):
        """Test deleting a capability."""
        inline_provider.add_capability(
            kind="tool",
            name="test.delete_me",
            description="Will be deleted",
        )
        assert len(inline_provider.list_capabilities()) == 1

        result = inline_provider.delete_capability("test.delete_me")
        assert result is True
        assert len(inline_provider.list_capabilities()) == 0

    def test_delete_nonexistent_capability(self, inline_provider):
        """Test deleting a non-existent capability returns False."""
        result = inline_provider.delete_capability("test.nonexistent")
        assert result is False

    def test_toggle_enabled(self, inline_provider):
        """Test enabling/disabling a capability."""
        inline_provider.add_capability(
            kind="tool",
            name="test.toggle",
            description="Toggle test",
            enabled=True,
        )

        caps = inline_provider.list_capabilities()
        assert len(caps) == 1
        assert caps[0].enabled is True

        result = inline_provider.update_capability_enabled("test.toggle", False)
        assert result is True

        caps = inline_provider.list_capabilities()
        assert len(caps) == 0  # Disabled caps are filtered out

    def test_health_always_true(self, inline_provider):
        """Test that health always returns True."""
        assert inline_provider.health() is True

    def test_get_capability(self, inline_provider):
        """Test getting a specific capability by name."""
        inline_provider.add_capability(
            kind="tool",
            name="test.get_me",
            description="Get test",
        )

        cap = inline_provider.get("test.get_me")
        assert cap is not None
        assert cap.name == "test.get_me"

        cap_missing = inline_provider.get("test.not_exist")
        assert cap_missing is None


# ---------------------------------------------------------------------------
# Stub capability tests
# ---------------------------------------------------------------------------


class TestStubCapabilities:
    """Tests for the stub capability implementations."""

    def test_inline_tool_descriptor(self):
        """Test tool descriptor format."""
        cap = _InlineToolCapability(
            name="test.tool",
            description="A test tool",
            schema_json=json.dumps({"inputSchema": {"type": "object"}}),
        )
        desc = cap.descriptor()
        assert desc["name"] == "test.tool"
        assert desc["description"] == "A test tool"
        assert "inputSchema" in desc

    def test_inline_tool_call_raises(self):
        """Test that calling an inline tool raises NotImplementedError."""
        cap = _InlineToolCapability(
            name="test.tool",
            description="A test tool",
            schema_json="{}",
        )
        import asyncio

        with pytest.raises(NotImplementedError, match="not wired"):
            asyncio.run(cap.call({}))

    def test_inline_resource_descriptor(self):
        """Test resource descriptor format."""
        cap = _InlineResourceCapability(
            name="test.resource",
            description="A test resource",
            schema_json=json.dumps({"uriTemplate": "test://uri"}),
        )
        desc = cap.descriptor()
        assert desc["name"] == "test.resource"
        # "uri" is the canonical address key consumed by mcp_server /
        # registry; it must fall back to "uriTemplate" when the form only
        # stored that (which is the common case for S1 inline resources).
        assert desc["uri"] == "test://uri"
        assert desc["uriTemplate"] == "test://uri"

    def test_inline_prompt_descriptor(self):
        """Test prompt descriptor format."""
        cap = _InlinePromptCapability(
            name="test.prompt",
            description="A test prompt",
            schema_json=json.dumps({"arguments": [{"name": "q"}]}),
        )
        desc = cap.descriptor()
        assert desc["name"] == "test.prompt"
        assert len(desc["arguments"]) == 1


# ---------------------------------------------------------------------------
# REST API integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator():
    """Create a mock coordinator with a registry."""
    from evermcp.core.registry import CapabilityRegistry

    coord = MagicMock()
    coord.registry = CapabilityRegistry()
    return coord


@pytest.mark.asyncio
async def test_api_tree_endpoint(coordinator):
    """Test GET /api/tree returns capability tree."""
    from evermcp.web.app import create_app

    app = create_app(coordinator)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data


@pytest.mark.asyncio
async def test_api_capabilities_endpoint(coordinator):
    """Test GET /api/capabilities returns flat list."""
    from evermcp.web.app import create_app

    app = create_app(coordinator)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert "capabilities" in data


@pytest.mark.asyncio
async def test_api_create_capability(coordinator, in_memory_engine):
    """Test POST /api/capabilities creates a new capability."""
    from evermcp.core.provider import InlineDeclarationProvider
    from evermcp.web.app import create_app

    # Add inline provider to registry
    provider = InlineDeclarationProvider(engine=in_memory_engine)
    coordinator.registry._providers.append(provider)

    app = create_app(coordinator)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/capabilities", json={
            "kind": "tool",
            "name": "api.test.create",
            "description": "Created via API",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "created"
        assert data["name"] == "api.test.create"

        # Verify it was actually created
        caps = provider.list_capabilities()
        assert any(c.name == "api.test.create" for c in caps)


@pytest.mark.asyncio
async def test_api_create_capability_validation(coordinator):
    """Test POST /api/capabilities validates required fields."""
    from evermcp.web.app import create_app

    app = create_app(coordinator)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Missing name
        resp = await ac.post("/api/capabilities", json={
            "kind": "tool",
            "description": "No name",
        })
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_api_test_call_endpoint(coordinator):
    """Test POST /api/test calls a capability."""
    from evermcp.web.app import create_app

    app = create_app(coordinator)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/test", json={
            "name": "nonexistent.tool",
            "kind": "tool",
            "args": {},
        })
        assert resp.status_code == 200
        data = resp.json()
        # Should fail because the tool doesn't exist
        assert data["success"] is False
