"""Minimal example tool — demonstrates the @tool decorator contract.

This tool has no side effects: it just echoes back a greeting. Use it as
a starting template when writing your own tools:

1. Copy this file to `tools/<your_category>/hello.py`
2. Rename the function (this becomes `<your_category>.<function_name>`)
3. Add parameters — each one shows up in the JSON Schema the AI sees
4. Return a JSON-serializable dict

Run with:
    evermcp serve --tools-dir <your_tools_dir>
"""
from __future__ import annotations

from evermcp.core.tool import tool


@tool(description="Say hello to someone by name.")
def hello(name: str) -> dict:
    """Greet someone by name.

    Returns:
        {"message": "hello, <name>"}
    """
    return {"message": f"hello, {name}"}
