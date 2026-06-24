"""Core abstractions: @tool decorator, ToolContext, ToolRegistry."""

from evermcp.core.registry import ToolRegistry
from evermcp.core.tool import ToolContext, tool

__all__ = ["tool", "ToolContext", "ToolRegistry"]
