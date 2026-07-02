"""Capability Registry — multi-provider aggregation of Tools / Resources / Prompts.

S0 architecture (per docs/gateway-plan.md):

    ┌──────────────────────────────────────────────────────────────────┐
    │                  CapabilityRegistry (this file)                  │
    │  - aggregates capabilities across providers                      │
    │  - routes calls by name to the first provider that owns it       │
    │  - exposes kind-filtered list helpers for MCP layer              │
    └──────────────────────────────────────────────────────────────────┘
                                │
                                │  providers: list[CapabilityProvider]
                                ▼
        ┌───────────────────────┴───────────────────────┐
        │                                               │
        ▼                                               ▼
  LocalFilesystemProvider (S0)               InlineDeclarationProvider (S1)
  RemoteClientProvider (S2)                  …

Backward compatibility:
- `ToolRegistry` is preserved as a thin subclass that auto-wires a single
  LocalFilesystemProvider. All v0.2.0-era tests, the CLI, the Coordinator,
  and the LocalWorker keep working without modification.
- `TOOLS_DIR` module constant is kept for any legacy importers.

Design notes:
- Providers are *passive* — they don't decide routing or naming policy. The
  registry asks each provider for its capabilities and merges the views.
  In S0 only one provider (LocalFilesystemProvider) is in use at a time;
  the multi-provider surface is here so S1+ can plug in InlineDeclaration /
  RemoteClient providers without a registry rewrite.
- Tools produced by LocalFilesystemProvider are wrapped in `_ToolCapabilityAdapter`
  (defined in evermcp.core.provider) which makes the bare `ToolFunc` satisfy
  the `Capability` Protocol and adds an async `call()` that runs the tool
  on a worker thread.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evermcp.core.capability import Capability, CapabilityKind
from evermcp.core.provider import (
    CapabilityProvider,
    LocalFilesystemProvider,
)
from evermcp.core.tool import ToolContext, ToolDescriptor, ToolFunc

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legacy default tools directory (preserved for old callers / type checkers)
# ---------------------------------------------------------------------------
TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"


# ---------------------------------------------------------------------------
# CapabilityRegistry — multi-provider registry (S0 core abstraction)
# ---------------------------------------------------------------------------


class CapabilityRegistry:
    """Aggregates capabilities from one or more `CapabilityProvider`s.

    S0 surface:
      - add_provider / remove_provider — manage the provider list
      - list_capabilities / list_tools / list_resources / list_prompts
      - get / call / read_resource / get_prompt
      - health — coarse per-source health flags
      - scan / start_watching / stop_watching — convenience for the common
        single-local-provider case (no-ops if no provider supports them)

    Routing: providers are tried in registration order; first provider with a
    matching capability name wins. This matches the "newest wins" expectation
    for the typical S1 case where an inline declaration shadows a local file.
    """

    def __init__(self, providers: list[CapabilityProvider] | None = None) -> None:
        self._providers: list[CapabilityProvider] = list(providers) if providers else []

    # ----- provider management -----

    def add_provider(self, provider: CapabilityProvider) -> None:
        """Register a provider. Appends to the end of the routing order."""
        self._providers.append(provider)
        logger.debug("Added provider: source=%s", getattr(provider, "source", "?"))

    def remove_provider(self, source: str) -> None:
        """Remove the first provider whose `.source` matches.

        Trivial O(n) lookup; called rarely (hot-reload of providers themselves,
        not individual capabilities).
        """
        for i, p in enumerate(self._providers):
            if getattr(p, "source", None) == source:
                self._providers.pop(i)
                logger.debug("Removed provider: source=%s", source)
                return
        logger.warning("remove_provider: no provider with source=%s", source)

    @property
    def providers(self) -> list[CapabilityProvider]:
        """Return the live list of providers (do not mutate)."""
        return list(self._providers)

    # ----- introspection -----

    def list_capabilities(self, kind: CapabilityKind | None = None) -> list[Capability]:
        """Return all capabilities across all providers, optionally filtered by kind.

        Order is deterministic: provider order × each provider's `list_capabilities`
        order. Duplicates (same name in multiple providers) are intentionally
        preserved — the routing layer in `get()` / `call()` resolves conflicts.
        """
        out: list[Capability] = []
        for provider in self._providers:
            try:
                caps = provider.list_capabilities()
            except Exception:
                logger.exception(
                    "Provider %s raised in list_capabilities", getattr(provider, "source", "?")
                )
                continue
            if kind is None:
                out.extend(caps)
            else:
                out.extend(c for c in caps if getattr(c, "kind", None) == kind)
        return out

    def list_tools(self) -> list[dict[str, Any]]:
        """Back-compat: return raw ToolDescriptor dicts (same shape as v0.2.0).

        Equivalent to `list_capabilities(kind=CapabilityKind.TOOL)` with each
        entry's `descriptor()` called. The dict is an MCP-shaped ToolDescriptor.
        """
        return [c.descriptor() for c in self.list_capabilities(CapabilityKind.TOOL)]

    def list_resources(self) -> list[dict[str, Any]]:
        """Return Resource descriptors across all providers."""
        return [c.descriptor() for c in self.list_capabilities(CapabilityKind.RESOURCE)]

    def list_prompts(self) -> list[dict[str, Any]]:
        """Return Prompt descriptors across all providers."""
        return [c.descriptor() for c in self.list_capabilities(CapabilityKind.PROMPT)]

    # ----- lookup & invocation -----

    def get_capability(self, name: str) -> Capability | None:
        """First-match ``Capability`` lookup across all providers.

        Unlike :meth:`get`, this is **not** overridden by :class:`ToolRegistry`
        and therefore always traverses every provider (including remote
        clients), returning the raw :class:`Capability` with its ``source``
        attribute intact. Use this when you need provider/source metadata —
        for example the Coordinator's remote-call timeout branch, which must
        read ``cap.source`` to decide whether to apply
        ``remote_call_timeout_s``.

        Use :meth:`get` for the legacy ``ToolFunc``-returning contract that
        :class:`ToolRegistry` keeps for the synchronous ``LocalWorker`` path.
        """
        for provider in self._providers:
            cap = provider.get(name)
            if cap is not None:
                return cap
        return None

    def get(self, name: str) -> Capability | None:
        """First-match lookup across providers. Returns the raw Capability.

        .. note::
            :class:`ToolRegistry` overrides this to return a bare ``ToolFunc``
            for the ``LocalWorker`` sync path; callers that need the raw
            ``Capability`` (with ``source``) across all providers should use
            :meth:`get_capability` instead.
        """
        return self.get_capability(name)

    def get_tool_func(self, name: str) -> ToolFunc | None:
        """Back-compat accessor: return the underlying ToolFunc, if any provider exposes one.

        LocalFilesystemProvider implements this so LocalWorker can keep its
        `registry.get(name) -> ToolFunc | None` call site untouched.
        """
        for provider in self._providers:
            getter = getattr(provider, "get_tool_func", None)
            if getter is None:
                continue
            tf = getter(name)
            if tf is not None:
                return tf
        return None

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        """Route a call to the first provider that owns the capability.

        Raises KeyError if no provider has it. Callers (Coordinator, HTTP
        handler) translate that into MCP's TOOL_NOT_FOUND error code.
        """
        for provider in self._providers:
            if provider.get(name) is not None:
                return await provider.call(name, args, ctx)
        raise KeyError(name)

    async def read_resource(self, uri: str) -> tuple[bytes | str, str]:
        """Read a Resource by URI. Returns (content, mime_type).

        Async because ResourceFunc.call() is async; running it via
        asyncio.run() would conflict with an outer event loop (e.g. the
        HTTP server's uvicorn loop). Callers must `await` this.

        Raises KeyError if no Resource has the given URI.
        """
        for cap in self.list_capabilities(CapabilityKind.RESOURCE):
            desc = cap.descriptor() or {}
            if desc.get("uri") == uri:
                content = await cap.call({}, None)
                mime_type = desc.get("mimeType", "text/plain")
                return content, mime_type
        raise KeyError(uri)

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Render a Prompt by name with the given arguments (async)."""
        for cap in self.list_capabilities(CapabilityKind.PROMPT):
            if cap.name == name:
                return await cap.call(arguments or {}, None)
        raise KeyError(name)

    # ----- health -----

    def health(self) -> dict[str, bool]:
        """Return a {source: healthy?} map across all providers.

        Used by the future UI for node-tree badges and by capability filters
        that hide unhealthy sources.
        """
        result: dict[str, bool] = {}
        for provider in self._providers:
            source = getattr(provider, "source", "?")
            try:
                result[source] = bool(provider.health())
            except Exception:
                logger.exception("Provider %s raised in health()", source)
                result[source] = False
        return result

    # ----- scan & hot-reload (single-provider conveniences) -----

    def scan(self) -> list[dict[str, Any]]:
        """Aggregate scan() across providers that support it.

        Returns the union of raw ToolDescriptor dicts (back-compat shape).
        Most providers implement scan(); RemoteClientProvider may not (it
        doesn't own files). Anything that doesn't have `.scan` is skipped.
        """
        out: list[dict[str, Any]] = []
        for provider in self._providers:
            scan_fn = getattr(provider, "scan", None)
            if scan_fn is None:
                continue
            try:
                results = scan_fn()
            except Exception:
                logger.exception("Provider %s raised in scan()", getattr(provider, "source", "?"))
                continue
            out.extend(results)
        return out

    def start_watching(self) -> None:
        """Start hot-reload on every provider that supports it.

        Safe to call when no provider implements start_watching; the loop is
        a no-op in that case.
        """
        for provider in self._providers:
            start = getattr(provider, "start_watching", None)
            if start is None:
                continue
            try:
                start()
            except Exception:
                logger.exception(
                    "Provider %s raised in start_watching()",
                    getattr(provider, "source", "?"),
                )

    def stop_watching(self) -> None:
        """Stop hot-reload on every provider that supports it."""
        for provider in self._providers:
            stop = getattr(provider, "stop_watching", None)
            if stop is None:
                continue
            try:
                stop()
            except Exception:
                logger.exception(
                    "Provider %s raised in stop_watching()",
                    getattr(provider, "source", "?"),
                )

    # ----- convenience accessors used by tests / CLI -----

    @property
    def tools_dir(self) -> Path | None:
        """Return the LocalFilesystemProvider's tools_dir if one is registered.

        Returns None when no LocalFilesystemProvider is present. The legacy
        `ToolRegistry.tools_dir` attribute is preserved by the subclass.
        """
        for provider in self._providers:
            td = getattr(provider, "tools_dir", None)
            if td is not None:
                return td
        return None


# ---------------------------------------------------------------------------
# ToolRegistry — back-compat subclass (v0.2.0 API preserved)
# ---------------------------------------------------------------------------


class ToolRegistry(CapabilityRegistry):
    """Back-compat `ToolRegistry` — auto-wires a single LocalFilesystemProvider.

    All v0.2.0-era callers (Coordinator, CLI, LocalWorker, tests) keep working
    without modification. The new `CapabilityRegistry` is the canonical class;
    `ToolRegistry` is the convenient one-provider shortcut.

    Constructor signature unchanged: `ToolRegistry(tools_dir=...)`. The new
    `tools_dir` is forwarded into the LocalFilesystemProvider; `None` falls
    back to the provider's built-in default (<repo>/tools).
    """

    def __init__(self, tools_dir: Path | str | None = None) -> None:
        # Initialize the multi-provider base with NO providers — we add ours below.
        super().__init__()
        self._local_provider = LocalFilesystemProvider(tools_dir=tools_dir)
        self.add_provider(self._local_provider)
        # Note: `tools_dir` is exposed as a @property below that returns the
        # local provider's path. Data descriptors (properties) take precedence
        # over instance __dict__ entries, so any `self.tools_dir = ...` attempt
        # would actually go to the read-only property and raise AttributeError
        # — that's intentional: callers should not reassign tools_dir post-init.

    @property
    def tools_dir(self) -> Path:  # type: ignore[override]
        """Return the local provider's tools_dir (concrete Path)."""
        return self._local_provider.tools_dir

    # ----- overrides to keep the LocalWorker call site working -----

    def get(self, name: str) -> ToolFunc | None:
        """Back-compat: return the raw ToolFunc instead of a Capability.

        LocalWorker calls `self._registry.get(name)` and expects a ToolFunc so
        it can invoke `tool_func.call(args, ctx)` synchronously. We unwrap the
        `_ToolCapabilityAdapter` that LocalFilesystemProvider stores in its
        `_caps` map by delegating to `get_tool_func` (which the provider
        implements and which directly returns the bare ToolFunc).

        Returns None if the name is unknown (same semantics as v0.2.0).
        """
        # Only LocalFilesystemProvider exposes `get_tool_func` in S0, so this
        # resolves to it. If the name is not found, return None (preserving
        # the legacy contract that LocalWorker checks via `if tool_func is None`).
        return self.get_tool_func(name)

    def scan(self) -> list[ToolDescriptor]:
        """Back-compat: scan via the local provider and return raw descriptors.

        Same return shape as v0.2.0; just delegates to the provider.
        """
        return self._local_provider.scan()

    def start_watching(self) -> None:
        """Back-compat: start hot-reload on the local provider."""
        self._local_provider.start_watching()

    def stop_watching(self) -> None:
        """Back-compat: stop hot-reload on the local provider."""
        self._local_provider.stop_watching()


__all__ = [
    "CapabilityRegistry",
    "ToolRegistry",
    "TOOLS_DIR",
]
