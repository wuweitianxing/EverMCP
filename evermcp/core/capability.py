"""Capability model — unified abstraction over MCP Tool/Resource/Prompt.

S0 scope: define the Protocol + decorators (`@resource`, `@prompt`).
Local filesystem scanning remains Tool-only (LocalFilesystemProvider).
Resource/Prompt capabilities are registered manually (process-injected) in S0;
S1 will add InlineDeclarationProvider backed by SQLite.

Backward compatibility:
- `ToolFunc` (in core/tool.py) implements `Capability` via duck-typing:
  it already exposes `kind` (added in S0, defaults to TOOL), `name`, `description`,
  `descriptor()`, and a synchronous `call()`. LocalFilesystemProvider wraps it
  in a small adapter that adds an async `call()`.
- Existing @tool code is **unaffected**; the kind attribute is added with a
  default so old code keeps working.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from evermcp.core.tool import ToolContext

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# CapabilityKind — MCP three-primitive taxonomy
# ---------------------------------------------------------------------------


class CapabilityKind(StrEnum):
    """MCP three-primitive taxonomy.

    Matches MCP spec: tools are invokable functions; resources are addressable
    data sources (URI-keyed); prompts are parameterized message templates.
    """

    TOOL = "tool"
    RESOURCE = "resource"
    PROMPT = "prompt"


# ---------------------------------------------------------------------------
# Context-injection helper (shared by ResourceFunc and PromptFunc)
# ---------------------------------------------------------------------------


def _inject_ctx_into_kwargs(
    fn: Callable[..., Any],
    kwargs: dict[str, Any],
    ctx: ToolContext | None,
) -> dict[str, Any]:
    """Inject the coordinator's ToolContext into the callable's kwargs.

    Inspects ``fn``'s signature and, if it declares a ``ctx`` or ``_ctx``
    parameter, populates the matching kwarg with the live context. Both
    spellings are accepted because resource/prompt authors occasionally
    prefix the parameter with an underscore to avoid clashes with their own
    ``ctx`` variable.

    The input ``kwargs`` dict is **not mutated**; a shallow copy is returned
    so callers can rely on side-effect-free behavior.
    """
    import inspect

    if ctx is None:
        return kwargs
    sig = inspect.signature(fn)
    if "ctx" in sig.parameters:
        kwargs["ctx"] = ctx
    elif "_ctx" in sig.parameters:
        kwargs["_ctx"] = ctx
    return kwargs


# ---------------------------------------------------------------------------
# Capability — Protocol that ToolFunc, ResourceFunc, PromptFunc all conform to
# ---------------------------------------------------------------------------


@runtime_checkable
class Capability(Protocol):
    """Unified interface for Tool / Resource / Prompt.

    Provider implementations return objects implementing this Protocol. The
    registry routes calls by name to the matching Capability.

    Properties:
        kind:        which MCP primitive this capability represents
        name:        unique within the provider; e.g. "io.read_file"
        source:      provenance tag; "local" | "remote.<id>" | "inline"
        description: human-readable text shown to the Agent
        enabled:     if False, hidden from MCP list_* but kept in registry
    """

    kind: CapabilityKind
    name: str
    source: str
    description: str
    enabled: bool

    def descriptor(self) -> dict[str, Any]:
        """Return an MCP-shaped descriptor dict.

        For Tool:    {name, description, input_schema}
        For Resource:{uri, name, description, mimeType?}
        For Prompt:  {name, description, arguments?}
        """
        ...

    async def call(
        self,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        """Invoke the capability.

        For Tool:    args = call arguments; returns the tool result.
        For Resource: args = {} (resources are addressable by URI, see read()).
        For Prompt:  args = prompt arguments dict; returns rendered messages.
        """
        ...


# ---------------------------------------------------------------------------
# ResourceFunc — @resource decorator
# ---------------------------------------------------------------------------


class ResourceFunc:
    """Wrapper around a function that produces a Resource's content.

    Usage:
        @resource(uri="evermcp://about", description="About EverMCP")
        def about(_ctx: ToolContext | None = None) -> str:
            return "EverMCP gateway v3"
    """

    kind: CapabilityKind = CapabilityKind.RESOURCE
    enabled: bool = True

    def __init__(
        self,
        fn: Callable[..., Any],
        uri: str,
        description: str,
        mime_type: str = "text/plain",
        name: str | None = None,
    ) -> None:
        self.fn = fn
        self.uri = uri
        self._description = description or fn.__doc__ or ""
        self.mime_type = mime_type
        # Default name = function name; can be overridden
        self.name = name or fn.__name__
        self.source = "inline"  # S0: only manual registration; S1: may come from DB

    @property
    def description(self) -> str:
        return self._description

    def descriptor(self) -> dict[str, Any]:
        """Return MCP-shaped Resource descriptor."""
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self._description,
            "mimeType": self.mime_type,
        }

    async def call(
        self,
        args: dict[str, Any] | None = None,
        ctx: ToolContext | None = None,
    ) -> str:
        """Read the resource by invoking the underlying function.

        Note: MCP `resources/read` uses URI routing; Coordinator dispatches to
        the matching ResourceFunc based on the requested URI.
        """
        kwargs = _inject_ctx_into_kwargs(self.fn, dict(args or {}), ctx)
        return self.fn(**kwargs)


def resource(
    uri: str,
    description: str = "",
    mime_type: str = "text/plain",
    name: str | None = None,
) -> Callable[[Callable[..., Any]], ResourceFunc]:
    """Decorator: mark a function as a Resource.

    The function should return the resource content (str or bytes).
    URI must be unique within the gateway.
    """

    def decorator(fn: Callable[..., Any]) -> ResourceFunc:
        return ResourceFunc(
            fn=fn,
            uri=uri,
            description=description,
            mime_type=mime_type,
            name=name,
        )

    return decorator


# ---------------------------------------------------------------------------
# PromptFunc — @prompt decorator
# ---------------------------------------------------------------------------


class PromptFunc:
    """Wrapper around a function that renders a Prompt's messages.

    Usage:
        @prompt(description="Greet the user in a chosen language")
        def greet(language: str = "en", _ctx: ToolContext | None = None) -> str:
            return f"Please greet the user in {language}."
    """

    kind: CapabilityKind = CapabilityKind.PROMPT
    enabled: bool = True

    def __init__(
        self,
        fn: Callable[..., Any],
        description: str,
        name: str | None = None,
        arguments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.fn = fn
        self._description = description or fn.__doc__ or ""
        self.name = name or fn.__name__
        self.source = "inline"
        # arguments: optional list of {name, description, required} dicts;
        # if None, derived from function signature (excluding ctx)
        self._explicit_arguments = arguments

    @property
    def description(self) -> str:
        return self._description

    def _derive_arguments(self) -> list[dict[str, Any]]:
        """Build arguments list from the function signature.

        Skips `ctx` (injected) and `_ctx` (also injected if author prefixed).
        All other parameters are exposed as optional prompt arguments.
        """
        import inspect

        from evermcp.core.tool import _type_to_schema

        sig = inspect.signature(self.fn)
        arguments: list[dict[str, Any]] = []
        for param_name, param in sig.parameters.items():
            if param_name in {"ctx", "_ctx"}:
                continue
            from typing import get_type_hints

            try:
                hints = get_type_hints(self.fn, include_extras=True)
            except Exception:
                hints = {}
            hint = hints.get(param_name, str)
            default = param.default
            is_required = default is inspect.Parameter.empty
            # Build a minimal schema, then strip the `default` for required params
            schema = _type_to_schema(param_name, hint, default)
            if is_required and "default" in schema:
                schema.pop("default", None)
            arg: dict[str, Any] = {"name": param_name, "required": is_required}
            if schema.get("description"):
                arg["description"] = schema["description"]
            arguments.append(arg)
        return arguments

    @property
    def arguments(self) -> list[dict[str, Any]]:
        if self._explicit_arguments is not None:
            return self._explicit_arguments
        return self._derive_arguments()

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self._description,
            "arguments": self.arguments,
        }

    async def call(
        self,
        args: dict[str, Any] | None = None,
        ctx: ToolContext | None = None,
    ) -> str:
        """Render the prompt text by invoking the underlying function."""
        kwargs = _inject_ctx_into_kwargs(self.fn, dict(args or {}), ctx)
        return self.fn(**kwargs)


def prompt(
    description: str = "",
    name: str | None = None,
    arguments: list[dict[str, Any]] | None = None,
) -> Callable[[Callable[..., Any]], PromptFunc]:
    """Decorator: mark a function as a Prompt.

    The function should return the prompt text (str). Optional `arguments`
    list overrides the auto-derived one from the function signature.
    """

    def decorator(fn: Callable[..., Any]) -> PromptFunc:
        return PromptFunc(
            fn=fn,
            description=description,
            name=name,
            arguments=arguments,
        )

    return decorator


# ---------------------------------------------------------------------------
# CapabilityRecord — internal registry-friendly view (no behavior, metadata only)
# ---------------------------------------------------------------------------


@dataclass
class CapabilityRecord:
    """Lightweight metadata snapshot used by the registry for list_* operations.

    The full Capability object lives in the provider; the registry keeps a
    shallow record so list_* doesn't have to wrap-unwrap. Records are cheap
    to serialize for the MCP layer.
    """

    kind: CapabilityKind
    name: str
    source: str
    description: str
    enabled: bool
    descriptor: dict[str, Any] = field(default_factory=dict)
