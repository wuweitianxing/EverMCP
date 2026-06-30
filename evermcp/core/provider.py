"""Capability Providers — pluggable sources for Tools / Resources / Prompts.

S0 ships:
- CapabilityProvider Protocol (the contract every provider satisfies)
- LocalFilesystemProvider — wraps the existing ToolRegistry; only produces
  Tool capabilities (Resources/Prompts come from manual registration in S0,
  InlineDeclarationProvider in S1, RemoteClientProvider in S2).

Design notes:
- Providers are *passive* — they don't decide routing or naming policy.
  The CapabilityRegistry asks each provider for its capabilities and
  merges the views (S0: only one provider at a time; S1+: multi-provider).
- Health is a coarse boolean — used by the future UI for node-tree badges
  and the registry for healthy-only filtering. Always True for
  LocalFilesystemProvider; RemoteClientProvider will toggle on disconnect.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from evermcp.core.capability import Capability, CapabilityKind
from evermcp.core.tool import ToolContext, ToolFunc

logger = logging.getLogger(__name__)

# Default tools dir: <repo>/tools (same fallback as ToolRegistry in v0.2.0).
_DEFAULT_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"


# ---------------------------------------------------------------------------
# CapabilityProvider — Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class CapabilityProvider(Protocol):
    """Contract every provider satisfies.

    S0: only LocalFilesystemProvider is implemented here.
    S1: InlineDeclarationProvider joins.
    S2: RemoteClientProvider joins (one instance per remote client).
    """

    source: str

    def list_capabilities(self) -> list[Capability]:
        """Return all capabilities this provider currently exposes."""
        ...

    def get(self, name: str) -> Capability | None:
        """Look up by full capability name (provider-local; no namespace prefix)."""
        ...

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        """Invoke a capability. Raises KeyError if name not found here."""
        ...

    def health(self) -> bool:
        """Coarse health flag (used by future UI / registry filters)."""
        ...


# ---------------------------------------------------------------------------
# _ToolCapabilityAdapter — wraps a ToolFunc so it conforms to Capability
# ---------------------------------------------------------------------------

class _ToolCapabilityAdapter:
    """Adapts a ToolFunc to the Capability Protocol (kind=TOOL).

    ToolFunc is already structurally a Capability (kind/name/source/descriptor/call);
    we just add:
      - an async `call()` that runs the sync tool in a worker thread (so
        HTTP / WS handlers can `await` it without blocking the loop);
      - a stable `source` attribute.
    """

    kind = CapabilityKind.TOOL
    enabled = True

    def __init__(self, tool_func: ToolFunc, source: str = "local") -> None:
        self._tf = tool_func
        self.source = source
        # Forward the canonical name (set by ToolRegistry after category assignment).
        self.name = tool_func.name
        self.description = tool_func.description

    def descriptor(self) -> dict[str, Any]:
        return self._tf.descriptor()

    async def call(
        self,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        """Run the synchronous ToolFunc in a worker thread.

        The wrapping keeps existing tool code 100% sync (callers don't have
        to learn asyncio), while letting the coordinator / HTTP handler await
        it cleanly.
        """
        import asyncio

        return await asyncio.to_thread(self._tf.call, args, ctx)


# ---------------------------------------------------------------------------
# LocalFilesystemProvider
# ---------------------------------------------------------------------------

class LocalFilesystemProvider:
    """Scans tools/<category>/<name>.py for @tool functions.

    Replicates v0.2.0's ToolRegistry scan + hot-reload behavior, but exposes
    Capability objects instead of bare ToolFunc. Local files are Tool-only;
    Resources/Prompts do not come from filesystem scanning (per design).
    """

    source = "local"

    def __init__(self, tools_dir: Path | str | None = None) -> None:
        self._tools_dir = Path(tools_dir) if tools_dir else _DEFAULT_TOOLS_DIR
        # name -> _ToolCapabilityAdapter (the canonical store)
        self._caps: dict[str, _ToolCapabilityAdapter] = {}
        # Backwards-compat: also expose the underlying ToolFuncs by name, so
        # the legacy ToolRegistry subclass can keep its `get(name) -> ToolFunc`
        # contract working without touching LocalWorker.
        self._tool_funcs: dict[str, ToolFunc] = {}
        self._watcher = None

    # ----- metadata helpers -----

    @property
    def tools_dir(self) -> Path:
        return self._tools_dir

    def list_tools(self) -> list[dict[str, Any]]:
        """Backwards-compat: return raw ToolDescriptor dicts (same as v0.2.0)."""
        return [t.descriptor() for t in self._tool_funcs.values()]

    # ----- CapabilityProvider contract -----

    def list_capabilities(self) -> list[Capability]:
        return list(self._caps.values())

    def get(self, name: str) -> Capability | None:
        return self._caps.get(name)

    def get_tool_func(self, name: str) -> ToolFunc | None:
        """Backwards-compat accessor for the bare ToolFunc (used by LocalWorker)."""
        return self._tool_funcs.get(name)

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        cap = self._caps.get(name)
        if cap is None:
            raise KeyError(name)
        return await cap.call(args, ctx)

    def health(self) -> bool:
        return True

    # ----- scan + hot reload (lifted from ToolRegistry) -----

    def scan(self) -> list[dict[str, Any]]:
        """Scan tools/ and rebuild the capability table.

        Returns raw ToolDescriptor dicts (back-compat). Side effect: updates
        both `_caps` (Capability view) and `_tool_funcs` (legacy view).
        """
        self._caps.clear()
        self._tool_funcs.clear()

        if not self._tools_dir.is_dir():
            logger.warning("Tools directory not found: %s", self._tools_dir)
            return []

        for category_dir in sorted(self._tools_dir.iterdir()):
            if not category_dir.is_dir() or category_dir.name.startswith("_"):
                continue

            category = category_dir.name
            for py_file in sorted(category_dir.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._load_tool_file(py_file, category)

        descriptors = [t.descriptor() for t in self._tool_funcs.values()]
        logger.info(
            "Scanned %d tools from %s", len(descriptors), self._tools_dir
        )
        return descriptors

    def _load_tool_file(self, path: Path, category: str) -> None:
        """Load a single tool file and register its @tool functions."""
        module_name = f"tools.{category}.{path.stem}"

        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            logger.warning("Cannot load module spec: %s", path)
            return

        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("Failed to load tool file: %s", path)
            # Don't leave a half-initialized module in sys.modules: a future
            # import (or this scanner's next attempt) might pick it up
            # instead of reloading from disk, masking the syntax error.
            sys.modules.pop(module_name, None)
            return

        # Only register in sys.modules AFTER a successful exec — matches
        # importlib.import_module's contract.
        sys.modules[module_name] = module

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, ToolFunc):
                attr.category = category
                full_name = f"{category}.{attr.fn.__name__}"
                self._tool_funcs[full_name] = attr
                self._caps[full_name] = _ToolCapabilityAdapter(attr, source=self.source)
                logger.info("Registered tool: %s", full_name)

    def start_watching(self) -> None:
        """Start watchdog file system watcher for hot-reload."""
        try:
            from watchdog.observers import Observer

            from evermcp.core.watcher import ToolFileHandler

            self._watcher = Observer()
            handler = ToolFileHandler(provider=self)
            self._watcher.schedule(handler, str(self._tools_dir), recursive=True)
            self._watcher.start()
            logger.info("Started watching tools directory: %s", self._tools_dir)
        except ImportError:
            logger.warning("watchdog not installed, hot-reload disabled")

    def stop_watching(self) -> None:
        """Stop the file system watcher."""
        if self._watcher:
            self._watcher.stop()
            self._watcher.join(timeout=5)
            self._watcher = None
            logger.info("Stopped watching tools directory")


# ---------------------------------------------------------------------------
# InlineDeclarationProvider — form-declared capabilities (S1)
# ---------------------------------------------------------------------------

class InlineDeclarationProvider:
    """Provides capabilities declared via the gateway UI form.

    Reads from the ``InlineCapability`` SQLite table. Capabilities are
    pure metadata — no script execution. Each row becomes a Capability
    object whose ``call`` / ``read`` / ``get`` delegates to a small
    stub implementation.

    Design notes:
    - ``kind == "tool"`` → produces a stub ``Capability`` whose ``call()``
      raises ``NotImplementedError`` (form-declared tools are placeholders
      until wired to real code in S2+).
    - ``kind == "resource"`` → produces a stub whose ``read()`` returns
      static content from ``schema_json``.
    - ``kind == "prompt"`` → produces a stub whose ``get()`` returns
      static prompt text from ``schema_json``.
    - Enabled/disabled state is controlled by the ``enabled`` column;
      disabled capabilities are invisible to the registry.
    """

    source = "inline"

    def __init__(self, engine: Any = None) -> None:
        from evermcp.storage import get_engine, init_db

        # Resolve the engine once and ensure the schema exists. Previously each
        # CRUD method re-ran init_db on every call, which is wasteful and can
        # race with concurrent sessions on some SQLite drivers.
        self._engine = engine if engine is not None else get_engine()
        init_db(self._engine)
        self._caps: dict[str, Capability] = {}
        self._reload()

    # ----- metadata helpers -----

    def _reload(self) -> None:
        """Reload capabilities from the database."""
        from evermcp.storage import list_inline_capabilities

        self._caps.clear()
        rows = list_inline_capabilities(self._engine)
        for row in rows:
            cap = self._row_to_capability(row)
            if cap.enabled:
                self._caps[row.name] = cap
        logger.info("InlineDeclarationProvider loaded %d capabilities", len(self._caps))

    def _row_to_capability(self, row: Any) -> Capability:
        """Convert an InlineCapability row to a Capability object."""
        kind = CapabilityKind(row.kind)
        name = row.name
        description = row.description or ""

        if kind == CapabilityKind.TOOL:
            return _InlineToolCapability(name, description, row.schema_json)
        elif kind == CapabilityKind.RESOURCE:
            return _InlineResourceCapability(name, description, row.schema_json)
        elif kind == CapabilityKind.PROMPT:
            return _InlinePromptCapability(name, description, row.schema_json)
        else:
            raise ValueError(f"Unknown kind: {row.kind}")

    # ----- CapabilityProvider contract -----

    def list_capabilities(self) -> list[Capability]:
        return list(self._caps.values())

    def get(self, name: str) -> Capability | None:
        return self._caps.get(name)

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        cap = self._caps.get(name)
        if cap is None:
            raise KeyError(name)
        return await cap.call(args, ctx)

    def health(self) -> bool:
        return True

    def add_capability(
        self,
        kind: str,
        name: str,
        description: str,
        schema_json: str = "{}",
        enabled: bool = True,
    ) -> None:
        """Add or update a capability in memory and the database."""
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy import select

        from evermcp.storage import InlineCapability, Session

        with Session(self._engine) as session:
            existing = session.exec(
                select(InlineCapability).where(
                    InlineCapability.name == name,
                    InlineCapability.source == "inline",
                )
            ).first()

            if existing:
                existing.kind = kind
                existing.description = description
                existing.schema_json = schema_json
                existing.enabled = enabled
                existing.updated_at = datetime.now(UTC)
            else:
                row = InlineCapability(
                    id=uuid4().hex,
                    kind=kind,
                    name=name,
                    description=description,
                    schema_json=schema_json,
                    enabled=enabled,
                )
                session.add(row)

            session.commit()

        self._reload()

    def delete_capability(self, name: str) -> bool:
        """Delete a capability by name. Returns True if deleted."""
        from sqlalchemy import select

        from evermcp.storage import InlineCapability, Session

        # Select-then-delete instead of relying on `result.rowcount`.
        # `Session.exec` is oriented toward SELECT; on DELETE the underlying
        # SQLite driver may report rowcount == -1, causing a successful
        # delete to be reported as not-deleted.
        with Session(self._engine) as session:
            stmt = select(InlineCapability).where(
                InlineCapability.name == name,
                InlineCapability.source == "inline",
            )
            # Use .scalars() to get the model instance directly (session.exec
            # returns a Row tuple for select(Model), which session.delete
            # rejects as UnmappedInstanceError).
            existing = session.scalars(stmt).first()
            if existing is None:
                return False
            session.delete(existing)
            session.commit()

        self._reload()
        return True

    def update_capability_enabled(self, name: str, enabled: bool) -> bool:
        """Toggle enabled state. Returns True if found and updated."""
        from datetime import UTC, datetime

        from sqlalchemy import select

        from evermcp.storage import InlineCapability, Session

        with Session(self._engine) as session:
            stmt = select(InlineCapability).where(
                InlineCapability.name == name,
                InlineCapability.source == "inline",
            )
            # Use .scalars() to get model instances directly, not Row tuples
            instance = session.scalars(stmt).first()

            if instance is not None:
                instance.enabled = enabled
                instance.updated_at = datetime.now(UTC)
                session.commit()
                self._reload()
                return True
            return False


# ---------------------------------------------------------------------------
# Stub capability implementations for form-declared capabilities
# ---------------------------------------------------------------------------

def _parse_schema(schema_json: str) -> dict[str, Any]:
    """Parse a schema_json string into a dict, returning {} on failure."""
    import json

    try:
        return json.loads(schema_json)
    except (json.JSONDecodeError, TypeError):
        return {}


class _InlineToolCapability:
    """Stub for a form-declared tool capability.

    The tool is purely metadata — calling it raises NotImplementedError
    until wired to real code (S2+).
    """

    kind = CapabilityKind.TOOL
    source = "inline"

    def __init__(self, name: str, description: str, schema_json: str) -> None:
        self.name = name
        self.description = description
        self.enabled = True
        self._schema = schema_json

    def descriptor(self) -> dict[str, Any]:
        schema = _parse_schema(self._schema)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": schema.get("inputSchema", {}),
        }

    async def call(
        self,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        raise NotImplementedError(
            f"Inline tool '{self.name}' is declared but not wired to implementation. "
            "Use local tool files (tools/<category>/<name>.py) or S2 remote clients."
        )


class _InlineResourceCapability:
    """Stub for a form-declared resource capability."""

    kind = CapabilityKind.RESOURCE
    source = "inline"

    def __init__(self, name: str, description: str, schema_json: str) -> None:
        self.name = name
        self.description = description
        self.enabled = True
        self._schema = schema_json

    def descriptor(self) -> dict[str, Any]:
        schema = _parse_schema(self._schema)
        # S0's ResourceFunc.descriptor() returns the resource address under the
        # "uri" key; mcp_server._list_resources reads d["uri"] and
        # registry.read_resource matches desc.get("uri") == uri. Form-declared
        # resources store the address as "uriTemplate" in schema_json, so fall
        # back to that. Keep "uriTemplate" as an optional extra field for
        # callers (e.g. the web UI) that still inspect it.
        return {
            "name": self.name,
            "description": self.description,
            "uri": schema.get("uri") or schema.get("uriTemplate", ""),
            "uriTemplate": schema.get("uriTemplate", ""),
            "mimeType": schema.get("mimeType", "text/plain"),
        }

    async def call(
        self,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        raise NotImplementedError(
            f"Inline resource '{self.name}' is declared but not wired."
        )


class _InlinePromptCapability:
    """Stub for a form-declared prompt capability."""

    kind = CapabilityKind.PROMPT
    source = "inline"

    def __init__(self, name: str, description: str, schema_json: str) -> None:
        self.name = name
        self.description = description
        self.enabled = True
        self._schema = schema_json

    def descriptor(self) -> dict[str, Any]:
        schema = _parse_schema(self._schema)
        return {
            "name": self.name,
            "description": self.description,
            "arguments": schema.get("arguments", []),
        }

    async def call(
        self,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> Any:
        raise NotImplementedError(
            f"Inline prompt '{self.name}' is declared but not wired."
        )