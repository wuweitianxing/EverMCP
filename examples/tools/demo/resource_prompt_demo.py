"""S0 demo: Resource and Prompt capabilities.

In S0 there is no InlineDeclarationProvider — Resources and Prompts must be
registered manually. This file shows two patterns:

  1. Declare the capability with the @resource / @prompt decorators. The
     decorators return ResourceFunc / PromptFunc objects that already
     satisfy the Capability Protocol (kind/name/source/descriptor/call).
  2. Call `register_demo_capabilities(registry)` from your entry script to
     inject the ResourceFunc and PromptFunc directly into the
     LocalFilesystemProvider's `_caps` dict. The registry's `list_*`,
     `read_resource`, and `get_prompt` methods all read from `_caps`
     (via `list_capabilities`), so the injected entries appear in MCP
     `resources/list`, `resources/read`, and `prompts/get` immediately.

The file is placed under `examples/tools/demo/` so users pointing
`ToolRegistry(tools_dir="examples/tools")` at it will have the decorators
evaluated during the local-provider scan. The scan only auto-registers
`ToolFunc` instances, so `@resource` / `@prompt` declarations here are
*not* picked up by the scan — that is the S0 contract. Use the helper
below to wire them in.

Usage (from your entry script):

    from evermcp.core.registry import ToolRegistry
    from evermcp.protocol.coordinator import Coordinator
    from examples.tools.demo.resource_prompt_demo import (
        about_resource,
        greet_prompt,
        register_demo_capabilities,
    )

    registry = ToolRegistry(tools_dir="examples/tools")
    coordinator = Coordinator(registry=registry)
    coordinator.initialize()
    register_demo_capabilities(coordinator.registry)

After the helper runs:

    - `coordinator.list_tools()`      → ["demo.hello", "io.read_file"]
    - `coordinator.list_resources()`  → [{"name": "about", ...}]
    - `coordinator.list_prompts()`    → [{"name": "demo.greet_prompt", ...}]
    - `coordinator.read_resource("evermcp://about")`
        → ("EverMCP v3 gateway (S0). ...", "text/plain")
    - `coordinator.get_prompt("demo.greet_prompt", {"language": "fr"})`
        → "Please greet the user in fr."

For S1, replace this helper with an InlineDeclarationProvider backed by
SQLite — the registration call sites stay the same.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from evermcp.core.capability import prompt, resource

if TYPE_CHECKING:
    from evermcp.core.capability import PromptFunc, ResourceFunc
    from evermcp.core.registry import CapabilityRegistry


# ---------------------------------------------------------------------------
# Resource: about
# ---------------------------------------------------------------------------

@resource(
    uri="evermcp://about",
    description="Short description of the EverMCP gateway.",
    mime_type="text/plain",
    name="about",
)
def about_resource() -> str:
    """Return a short, human-readable description of the gateway."""
    return (
        "EverMCP v3 gateway (S0). Tools from local files; Resources and "
        "Prompts are registered manually in S0. See docs/gateway-plan.md."
    )


# ---------------------------------------------------------------------------
# Prompt: demo.greet_prompt
# ---------------------------------------------------------------------------

@prompt(
    description="Ask the agent to greet someone in a chosen language.",
    name="demo.greet_prompt",
)
def greet_prompt(language: str = "en") -> str:
    """Render the prompt body; the argument is forwarded by the decorator.

    The signature is auto-inspected to build the MCP `prompts/list` argument
    schema — `language` shows up as an optional parameter with default "en".
    """
    return f"Please greet the user in {language}."


# ---------------------------------------------------------------------------
# Manual registration helper (S0)
# ---------------------------------------------------------------------------

def register_demo_capabilities(registry: "CapabilityRegistry") -> None:
    """Inject the demo Resource and Prompt into the given registry.

    S0 mechanism: `LocalFilesystemProvider._caps` is the canonical store
    the registry reads from. `ResourceFunc` and `PromptFunc` already
    expose the Capability Protocol (kind / name / descriptor / async
    call), so they can be assigned directly — no adapter wrapper needed.

    For S1, replace this with an InlineDeclarationProvider; call sites
    that need Resources or Prompts keep using the same
    `coordinator.read_resource(...)` / `coordinator.get_prompt(...)` API.
    """
    # The CapabilityRegistry exposes `.providers` (live list, read-only).
    # In S0 there is exactly one LocalFilesystemProvider with source="local".
    injected = False
    for provider in registry.providers:
        if getattr(provider, "source", None) != "local":
            continue
        caps: dict[str, object] = provider._caps  # type: ignore[attr-defined]
        caps[about_resource.name] = about_resource
        caps[greet_prompt.name] = greet_prompt
        injected = True
        break

    if not injected:
        raise RuntimeError(
            "register_demo_capabilities: no LocalFilesystemProvider found "
            "in the registry. Did you forget to construct a ToolRegistry "
            "or add a LocalFilesystemProvider before calling this helper?"
        )


__all__ = [
    "about_resource",
    "greet_prompt",
    "register_demo_capabilities",
]