"""Unit tests for the Capability protocol, @resource, and @prompt decorators."""

from __future__ import annotations

from typing import Any

import pytest

from evermcp.core.capability import (
    CapabilityKind,
    PromptFunc,
    ResourceFunc,
    prompt,
    resource,
)
from evermcp.core.tool import ToolFunc, tool

# ---------------------------------------------------------------------------
# CapabilityKind enum
# ---------------------------------------------------------------------------


class TestCapabilityKind:
    def test_members_present(self) -> None:
        assert hasattr(CapabilityKind, "TOOL")
        assert hasattr(CapabilityKind, "RESOURCE")
        assert hasattr(CapabilityKind, "PROMPT")

    def test_string_values(self) -> None:
        assert CapabilityKind.TOOL.value == "tool"
        assert CapabilityKind.RESOURCE.value == "resource"
        assert CapabilityKind.PROMPT.value == "prompt"

    def test_is_str_enum(self) -> None:
        # str Enum: each member is usable as a string
        assert CapabilityKind.TOOL == "tool"
        assert CapabilityKind.RESOURCE == "resource"
        assert CapabilityKind.PROMPT == "prompt"

    def test_member_count(self) -> None:
        # MCP three-primitive taxonomy — guard against accidental additions.
        assert len(CapabilityKind) == 3


# ---------------------------------------------------------------------------
# @resource decorator
# ---------------------------------------------------------------------------


class TestResourceDecorator:
    def test_returns_resource_func(self) -> None:
        @resource(uri="evermcp://test", description="Test resource")
        def about() -> str:
            return "content"

        assert isinstance(about, ResourceFunc)

    def test_kind_is_resource(self) -> None:
        @resource(uri="evermcp://test", description="Test")
        def about() -> str:
            return "x"

        assert about.kind == CapabilityKind.RESOURCE

    def test_descriptor_shape(self) -> None:
        @resource(uri="evermcp://about", description="About EverMCP")
        def about() -> str:
            return "EverMCP gateway v3"

        desc = about.descriptor()
        assert desc == {
            "uri": "evermcp://about",
            "name": "about",
            "description": "About EverMCP",
            "mimeType": "text/plain",
        }

    def test_descriptor_custom_mime(self) -> None:
        @resource(
            uri="evermcp://json",
            description="JSON data",
            mime_type="application/json",
        )
        def j() -> str:
            return "{}"

        desc = j.descriptor()
        assert desc["mimeType"] == "application/json"

    def test_descriptor_custom_name(self) -> None:
        @resource(
            uri="evermcp://x",
            description="x",
            name="explicit_name",
        )
        def some_func() -> str:
            return ""

        assert some_func.descriptor()["name"] == "explicit_name"

    @pytest.mark.asyncio
    async def test_call_invokes_function(self) -> None:
        @resource(uri="evermcp://g", description="Greeting")
        def greet(_ctx: Any = None) -> str:
            return "hello"

        result = await greet.call({})
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_call_passes_args(self) -> None:
        @resource(uri="evermcp://x", description="x")
        def with_args(prefix: str = ">", _ctx: Any = None) -> str:
            return f"{prefix}body"

        result = await with_args.call({"prefix": ">>"})
        assert result == ">>body"


# ---------------------------------------------------------------------------
# @prompt decorator
# ---------------------------------------------------------------------------


class TestPromptDecorator:
    def test_returns_prompt_func(self) -> None:
        @prompt(description="Greet the user")
        def greet() -> str:
            return "Hello"

        assert isinstance(greet, PromptFunc)

    def test_kind_is_prompt(self) -> None:
        @prompt(description="Greet the user")
        def greet() -> str:
            return "Hello"

        assert greet.kind == CapabilityKind.PROMPT

    def test_descriptor_includes_arguments(self) -> None:
        @prompt(description="Greet in a language")
        def greet(language: str = "en") -> str:
            return f"Greet in {language}"

        desc = greet.descriptor()
        assert desc["name"] == "greet"
        assert desc["description"] == "Greet in a language"
        assert isinstance(desc["arguments"], list)
        assert len(desc["arguments"]) == 1
        arg = desc["arguments"][0]
        assert arg["name"] == "language"
        # default exists → not required
        assert arg["required"] is False

    def test_descriptor_skips_ctx_argument(self) -> None:
        @prompt(description="Has ctx")
        def with_ctx(language: str = "en", _ctx: Any = None) -> str:
            return f"hi in {language}"

        desc = with_ctx.descriptor()
        arg_names = [a["name"] for a in desc["arguments"]]
        assert "language" in arg_names
        assert "ctx" not in arg_names
        assert "_ctx" not in arg_names

    def test_descriptor_skips_ctx_keyword_only(self) -> None:
        """When a prompt only takes ctx, descriptor() should produce empty args."""

        @prompt(description="Ctx only")
        def only_ctx(_ctx: Any = None) -> str:
            return "ctx only"

        desc = only_ctx.descriptor()
        assert desc["arguments"] == []

    def test_descriptor_marks_required_args(self) -> None:
        @prompt(description="Required")
        def req(language: str) -> str:
            return f"hi {language}"

        desc = req.descriptor()
        assert desc["arguments"][0]["required"] is True

    @pytest.mark.asyncio
    async def test_call_invokes_function(self) -> None:
        @prompt(description="Greet")
        def greet(language: str = "en") -> str:
            return f"Greet the user in {language}"

        result = await greet.call({"language": "en"})
        assert result == "Greet the user in en"

    @pytest.mark.asyncio
    async def test_call_with_default(self) -> None:
        @prompt(description="Greet default")
        def greet(language: str = "en") -> str:
            return f"Greet in {language}"

        result = await greet.call({})
        assert result == "Greet in en"


# ---------------------------------------------------------------------------
# ToolFunc class attrs (unified Capability model)
# ---------------------------------------------------------------------------


class TestToolFuncCapabilityAttrs:
    def test_kind_is_tool(self) -> None:
        assert ToolFunc.kind == "tool"

    def test_enabled_default_true(self) -> None:
        assert ToolFunc.enabled is True

    def test_source_default_local(self) -> None:
        assert ToolFunc.source == "local"

    def test_decorated_tool_inherits_attrs(self) -> None:
        @tool(description="Test tool")
        def my_tool(x: int) -> dict:
            return {"x": x}

        # Class attrs are shared; instances expose them through normal lookup
        assert my_tool.kind == "tool"
        assert my_tool.enabled is True
        assert my_tool.source == "local"


# ---------------------------------------------------------------------------
# Sanity: ResourceFunc.call return type (str)
# ---------------------------------------------------------------------------


class TestResourceFuncReturnType:
    @pytest.mark.asyncio
    async def test_returns_str(self) -> None:
        @resource(uri="evermcp://str", description="string resource")
        def text() -> str:
            return "plain text content"

        result = await text.call({})
        assert isinstance(result, str)
        assert result == "plain text content"
