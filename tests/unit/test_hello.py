"""Unit tests for examples/tools/demo/hello.py — the minimal example tool."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


from evermcp.core.tool import ToolFunc
from examples.tools.demo.hello import hello as _mod_hello

hello: ToolFunc = _mod_hello  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_is_tool_func(self) -> None:
        assert isinstance(hello, ToolFunc)

    def test_function_name(self) -> None:
        assert hello.fn.__name__ == "hello"

    def test_description_is_human_readable(self) -> None:
        """Description must be informative enough for an AI to know when to call it."""
        desc = hello.description
        assert "hello" in desc.lower()
        assert len(desc) > 10


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------


class TestInputSchema:
    def test_schema_has_name_property(self) -> None:
        schema = hello.input_schema()
        assert schema["type"] == "object"
        assert "name" in schema["properties"]
        assert "name" in schema["required"]

    def test_name_is_string_type(self) -> None:
        schema = hello.input_schema()
        assert schema["properties"]["name"]["type"] == "string"


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


class TestBehavior:
    def test_returns_greeting(self) -> None:
        result = hello.fn(name="World")
        assert result == {"message": "hello, World"}

    def test_with_name_unicode(self) -> None:
        result = hello.fn(name="世界")
        assert result["message"] == "hello, 世界"

    def test_with_empty_name(self) -> None:
        """No length constraint in the example; empty name should still produce output."""
        result = hello.fn(name="")
        assert result["message"] == "hello, "
