"""Core tool abstractions: @tool decorator, ToolContext, error helpers."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_type_hints

import pydantic_core
from pydantic.fields import FieldInfo
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from evermcp.security.config import Config
    from evermcp.security.safepath import SafePath
    from evermcp.security.safeurl import SafeURL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolContext — injected into tool functions that declare `ctx: ToolContext`
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """Runtime context injected into tool functions.

    Optional fields (safe_path, safe_url, config) are populated by the Coordinator
    before invoking the tool. Tools should check `if ctx and ctx.X is not None`
    rather than relying on dynamic attributes.
    """

    cwd: str = ""
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("evermcp.tool"))
    safe_path: SafePath | None = None
    safe_url: SafeURL | None = None
    config: Config | None = None


# ---------------------------------------------------------------------------
# ToolDescriptor — metadata exposed via MCP / worker protocol
# ---------------------------------------------------------------------------

class ToolDescriptor(TypedDict):
    name: str
    description: str
    input_schema: dict
    category: str


# ---------------------------------------------------------------------------
# Error codes (JSON-RPC compatible, reserved range -32001..-32099)
# ---------------------------------------------------------------------------

TOOL_NOT_FOUND = -32001
TOOL_TIMEOUT = -32002
TOOL_EXCEPTION = -32003
TOOL_INVALID_OUTPUT = -32004
SECURITY_VIOLATION = -32005


class ToolError(TypedDict):
    code: int
    message: str
    data: dict


def make_error(code: int, message: str, **extra: Any) -> ToolError:
    return ToolError(code=code, message=message, data=extra)


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------

class ToolFunc:
    """Wrapper around a tool function, carrying metadata for registration."""

    @property
    def category(self) -> str:
        return self._category

    @category.setter
    def category(self, value: str) -> None:
        self._category = value
        self.name = f"{value}.{self.fn.__name__}"

    def __init__(
        self,
        fn: Callable[..., Any],
        description: str,
        category: str,
    ) -> None:
        self._category = ""
        self.fn = fn
        self.description = description
        self.category = category  # go through setter to update name

    def input_schema(self) -> dict:
        """Derive JSON Schema from the function signature + Pydantic Field annotations."""
        hints = get_type_hints(self.fn, include_extras=True)
        sig = inspect.signature(self.fn)

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name == "ctx":
                continue  # skip injected context

            hint = hints.get(param_name, str)
            default = param.default

            # Pydantic Field-aware required detection
            is_required = default is inspect.Parameter.empty or (
                isinstance(default, FieldInfo) and default.is_required()
            )

            if is_required:
                required.append(param_name)
                # Pass the FieldInfo (not None) so description/ge/le/etc. are preserved.
                # _type_to_schema detects Field(...) has no default value and skips the
                # "default" key, so this doesn't add a misleading default to required fields.
                properties[param_name] = _type_to_schema(
                    param_name, hint, default if isinstance(default, FieldInfo) else None
                )
            else:
                properties[param_name] = _type_to_schema(param_name, hint, default)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema(),
            category=self.category,
        )

    def call(self, args: dict[str, Any], ctx: ToolContext | None = None) -> Any:
        """Invoke the tool function with validated arguments.

        Only injects `ctx` if the function signature actually declares a `ctx`
        parameter. Tools that don't need context work without it.
        """
        kwargs = dict(args)
        sig = inspect.signature(self.fn)
        if "ctx" in sig.parameters and ctx is not None:
            kwargs["ctx"] = ctx
        return self.fn(**kwargs)


def tool(description: str = "") -> Callable[[Callable[..., Any]], ToolFunc]:
    """Decorator that marks a function as an EverMCP tool.

    The category is derived from the file path (tools/<category>/<name>.py).
    """

    def decorator(fn: Callable[..., Any]) -> ToolFunc:
        # Category will be set by the registry during scanning
        tf = ToolFunc(fn=fn, description=description or fn.__doc__ or "", category="")
        return tf

    return decorator


# ---------------------------------------------------------------------------
# Type → JSON Schema helpers (simplified for v1)
# ---------------------------------------------------------------------------

def _type_to_schema(name: str, hint: Any, default: Any) -> dict[str, Any]:
    """Convert a Python type hint to a JSON Schema property.

    Supports:
    - Basic types: str, int, float, bool
    - Containers: list[T], dict[str, T]
    - Optional[T] (Union[..., None])
    - Literal["a", "b"]
    - Pydantic Field: ge, le, gt, lt, min_length, max_length, pattern, description, default
    - Annotated[T, Field(...)] syntax (Pydantic v2 preferred form)
    """
    import typing

    # Unwrap Annotated[T, Field(...)] -> (T, FieldInfo) so origin lookup works
    field_info: FieldInfo | None = None
    if hasattr(hint, "__metadata__") and getattr(hint, "__origin__", None) is not None:
        # Annotated[T, ...]: extract FieldInfo from metadata
        for m in hint.__metadata__:
            if isinstance(m, FieldInfo):
                field_info = m
                break
        hint = hint.__origin__  # unwrap to T for type_map lookup

    origin = getattr(hint, "__origin__", None)

    plain_default: Any = None

    # Detect Pydantic Field from default (when not using Annotated)
    if field_info is None and isinstance(default, FieldInfo):
        field_info = default
        if default.default is not pydantic_core.PydanticUndefined:
            plain_default = default.default
    elif default is not inspect.Parameter.empty:
        plain_default = default

    # Build base schema based on type
    if origin is typing.Union:
        # Optional[T] case (Union[T, None])
        args = [a for a in hint.__args__ if a is not type(None)]
        if len(args) == 1:
            schema = _type_to_schema(name, args[0], None)
        else:
            schema = {"type": "string"}  # multiple non-None types — too complex for v1
    elif origin is typing.Literal:
        schema = {"type": "string", "enum": list(hint.__args__)}
    elif origin is list:
        item_type = hint.__args__[0] if hint.__args__ else str
        schema = {
            "type": "array",
            "items": _type_to_schema(name, item_type, None),
        }
    elif origin is dict:
        schema = {"type": "object"}
    else:
        # Basic types — look up by hint (which is now unwrapped from Annotated)
        type_map = {
            str: {"type": "string"},
            int: {"type": "integer"},
            float: {"type": "number"},
            bool: {"type": "boolean"},
        }
        schema = dict(type_map.get(hint, {"type": "string"}))

    # Apply Pydantic Field constraints
    # In Pydantic v2, constraints (ge/le/gt/lt/min_length/max_length/pattern) live in
    # `field_info.metadata` as annotated_types / pydantic_core instances.
    if field_info is not None:
        if field_info.description:
            schema["description"] = field_info.description

        for m in field_info.metadata:
            cls_name = type(m).__name__
            # annotated_types: Ge, Le, Gt, Lt, MinLen, MaxLen
            if cls_name == "Ge" and hasattr(m, "ge"):
                schema["minimum"] = m.ge
            elif cls_name == "Le" and hasattr(m, "le"):
                schema["maximum"] = m.le
            elif cls_name == "Gt" and hasattr(m, "gt"):
                schema["exclusiveMinimum"] = m.gt
            elif cls_name == "Lt" and hasattr(m, "lt"):
                schema["exclusiveMaximum"] = m.lt
            elif cls_name == "MinLen" and hasattr(m, "min_length"):
                schema["minLength"] = m.min_length
            elif cls_name == "MaxLen" and hasattr(m, "max_length"):
                schema["maxLength"] = m.max_length
            elif cls_name == "_PydanticGeneralMetadata" and hasattr(m, "pattern") and m.pattern is not None:
                schema["pattern"] = m.pattern

    # Apply default value
    if plain_default is not None and "default" not in schema:
        schema["default"] = plain_default

    return schema
