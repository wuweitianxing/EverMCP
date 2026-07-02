"""Coordinator — receives tool/resource/prompt calls from MCP layer, dispatches.

S0 architecture (per docs/gateway-plan.md):
- Holds a CapabilityRegistry (multi-provider aggregation).
- S0: `ToolRegistry` (subclass) is still the default — keeps v0.2.0 behavior.
- S0 keeps the **synchronous** `call_tool` API used by stdio + LocalWorker;
  an additional `call_tool_async` is the canonical path for HTTP / future WS.

Routing: name-based, no namespace prefix in S0 (single LocalFilesystemProvider).
S1 will introduce `local.`, `inline.`, `remote.<id>.` prefixes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from evermcp import storage
from evermcp.core.registry import CapabilityRegistry, ToolRegistry
from evermcp.core.tool import (
    SECURITY_VIOLATION,
    TOOL_EXCEPTION,
    TOOL_INVALID_OUTPUT,
    TOOL_NOT_FOUND,
    TOOL_TIMEOUT,
    ToolContext,
    make_error,
)
from evermcp.workers.local import LocalWorker

if TYPE_CHECKING:
    from evermcp.security.config import Config
    from evermcp.security.safepath import SafePath
    from evermcp.security.safeurl import SafeURL

logger = logging.getLogger(__name__)


class Coordinator:
    """Central dispatcher that routes calls to providers via the registry.

    S0: Wraps a ToolRegistry (back-compat). New S0 surface adds
    `list_resources` / `list_prompts` / `read_resource` / `get_prompt` /
    `call_tool_async` for the HTTP / future-WS paths.
    """

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        config: Config | None = None,
    ) -> None:
        # Back-compat: instantiating `Coordinator()` with no registry still
        # produces a working local provider pointed at the default tools dir.
        self._registry: CapabilityRegistry = registry or ToolRegistry()
        self._worker = LocalWorker(self._registry)
        self._config = config
        # Timeout for remote client tool calls (S2). Kept as a simple scalar
        # so HTTP/WS handlers can apply it uniformly.
        self._remote_call_timeout_s = 60.0
        if config is not None:
            self._remote_call_timeout_s = float(getattr(config, "remote_call_timeout_s", 60.0))
        # Build SafePath once at init so we don't reconstruct it per call.
        self._safe_path: SafePath | None = None
        if config and (config.filesystem_allowlist or config.denied_paths):
            from evermcp.security.safepath import SafePath as _SafePath

            self._safe_path = _SafePath(
                allowlist=config.filesystem_allowlist,
                denied=config.denied_paths,
            )
        # Build SafeURL once at init. Always create one so tools can use it for
        # default-deny (private/loopback IPs) even when no allowlist is configured.
        from evermcp.security.safeurl import SafeURL as _SafeURL

        self._safe_url: SafeURL = _SafeURL(
            allowlist=config.network_allowlist if config else None,
        )

    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    @property
    def worker(self) -> LocalWorker:
        return self._worker

    @property
    def config(self) -> Config | None:
        return self._config

    @property
    def safe_path(self) -> SafePath | None:
        return self._safe_path

    @property
    def safe_url(self) -> SafeURL:
        return self._safe_url

    def initialize(self) -> None:
        """Scan tools and start hot-reload watcher (delegates to registry)."""
        self._registry.scan()
        self._registry.start_watching()
        logger.info(
            "Coordinator initialized with %d tools",
            len(self._registry.list_tools()),
        )

    def shutdown(self) -> None:
        """Stop the hot-reload watcher (delegates to registry)."""
        self._registry.stop_watching()
        logger.info("Coordinator shut down")

    # ------------------------------------------------------------------
    # Capability list helpers — back-compat + new S0 surface
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        """List all available tools (delegates to registry).

        S0: returns raw ToolDescriptor dicts with the same shape v0.2.0 produced.
        """
        return self._registry.list_tools()

    def list_resources(self) -> list[dict[str, Any]]:
        """List all available resources (S0: providers may return [])."""
        return self._registry.list_resources()

    def list_prompts(self) -> list[dict[str, Any]]:
        """List all available prompts (S0: providers may return [])."""
        return self._registry.list_prompts()

    def list_resource_templates(self) -> list[dict[str, Any]]:
        """List resource templates (S0: always []; S1+ may populate from inline decls)."""
        return []

    # ------------------------------------------------------------------
    # Capability read/get (used by MCP resources/read and prompts/get)
    # ------------------------------------------------------------------

    async def read_resource(self, uri: str) -> tuple[Any, str]:
        """Read a resource by URI. Returns (content, mime_type).

        Raises KeyError if no resource matches.
        """
        return await self._registry.read_resource(uri)

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Render a prompt by name with the given arguments.

        Raises KeyError if no prompt matches.
        """
        return await self._registry.get_prompt(name, arguments)

    # ------------------------------------------------------------------
    # Tool call — sync (v0.2.0 contract) + async (S0 HTTP / future WS)
    # ------------------------------------------------------------------

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Synchronous tool call — back-compat shim over the LocalWorker.

        .. deprecated:: S2
            Prefer :meth:`call_tool_async`. This synchronous shim is retained
            for v0.2.0 callers (stdio MCP server, LocalWorker, existing
            tests); it delegates to ``LocalWorker`` via the synchronous
            ``ToolFunc.call(args, ctx)`` path and intentionally does **not**
            apply the S2 observability / safety nets that
            :meth:`call_tool_async` does:

              * No ``CallLog`` audit entry is persisted.
              * No remote-call timeout is enforced (remote providers are not
                reachable from this synchronous path anyway).

            New integrators must use ``call_tool_async`` to get call logging
            and remote-call timeout handling.
        """
        logger.warning(
            "Coordinator.call_tool(%s) is deprecated: it bypasses CallLog "
            "persistence and the remote-call timeout; use call_tool_async.",
            name,
        )
        return self._worker.call_tool(name, arguments or {}, ctx=self._build_ctx())

    async def call_tool_async(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Async tool call — preferred path for HTTP / future WS handlers.

        Routes through the CapabilityRegistry's multi-provider async surface,
        which LocalFilesystemProvider implements via `asyncio.to_thread`.
        Returns the same envelope as `call_tool`:
            {success: bool, result?: Any, error?: {code, message, data}}

        S2: persists every call to ``CallLog`` and applies a timeout to remote
        client calls.
        """
        call_id = str(uuid.uuid4())
        args = arguments or {}
        ctx = self._build_ctx()
        started_at = datetime.now(UTC)
        logger.info("Dispatching async tool call: %s (call_id=%s)", name, call_id)

        # NOTE: use ``get_capability`` (not ``get``) so the source is resolved
        # across *all* providers (including remote clients). ``ToolRegistry``
        # overrides ``get`` to return a bare ``ToolFunc`` for the LocalWorker
        # sync path, which would yield ``None`` for remote tools and silently
        # mis-classify them as ``source == "local"`` — defeating the
        # remote-call timeout branch below.
        cap = self._registry.get_capability(name)
        source = getattr(cap, "source", "local") if cap else "local"

        try:
            if source.startswith("remote."):
                result = await asyncio.wait_for(
                    self._registry.call(name, args, ctx),
                    timeout=self._remote_call_timeout_s,
                )
            else:
                result = await self._registry.call(name, args, ctx)
        except KeyError:
            duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            await self._log_call(
                call_id, name, source, False, started_at, duration_ms, TOOL_NOT_FOUND
            )
            return {
                "success": False,
                "error": make_error(
                    TOOL_NOT_FOUND, f"Tool not found: {name}", tool=name, args=args
                ),
            }
        except TimeoutError as exc:
            duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            await self._log_call(
                call_id, name, source, False, started_at, duration_ms, TOOL_TIMEOUT
            )
            logger.error("Tool %s timed out (async, remote): %s", name, exc)
            return {
                "success": False,
                "error": make_error(
                    TOOL_TIMEOUT,
                    f"Tool {name} timed out after {self._remote_call_timeout_s}s",
                    tool=name,
                    args=args,
                ),
            }
        except Exception as exc:
            # Mirror LocalWorker's classification order (see
            # evermcp/workers/local.py:call_tool) so HTTP/WS callers see the
            # same error envelope as the stdio path — most importantly
            # SECURITY_VIOLATION (-32005) and TOOL_TIMEOUT (-32002) instead
            # of being collapsed into a generic TOOL_EXCEPTION.
            duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            envelope = self._classify_exception(exc, name, args)
            if envelope is not None:
                error_code = envelope.get("error", {}).get("code")
                await self._log_call(
                    call_id, name, source, False, started_at, duration_ms, error_code
                )
                return envelope
            logger.exception("Tool %s raised (async)", name)
            await self._log_call(
                call_id, name, source, False, started_at, duration_ms, TOOL_EXCEPTION
            )
            return {
                "success": False,
                "error": make_error(
                    TOOL_EXCEPTION,
                    f"Tool {name} raised an exception: {exc}",
                    tool=name,
                    args=args,
                    stderr=str(exc),
                ),
            }

        # Validate JSON serializability to match sync path's TOOL_INVALID_OUTPUT.
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            logger.error("Tool %s returned non-JSON-serializable result: %s", name, exc)
            await self._log_call(
                call_id, name, source, False, started_at, duration_ms, TOOL_INVALID_OUTPUT
            )
            return {
                "success": False,
                "error": make_error(
                    TOOL_INVALID_OUTPUT,
                    f"Tool {name} returned non-JSON-serializable output: {exc}",
                    tool=name,
                    args=args,
                    result_type=type(result).__name__,
                ),
            }

        duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
        await self._log_call(call_id, name, source, True, started_at, duration_ms)
        logger.info("Tool call result: %s (call_id=%s, success=True)", name, call_id)
        return {"success": True, "result": result}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ctx(self) -> ToolContext:
        """Construct a ToolContext with safe_path / safe_url / config wired."""
        return ToolContext(
            cwd=".",
            logger=logger,
            safe_path=self._safe_path,
            safe_url=self._safe_url,
            config=self._config,
        )

    async def _log_call(
        self,
        call_id: str,
        name: str,
        source: str,
        success: bool,
        started_at: datetime,
        duration_ms: int,
        error_code: int | None = None,
    ) -> None:
        """Persist a call-log entry (S2).

        The synchronous SQLite write is dispatched off the event loop via
        ``asyncio.to_thread`` so concurrent ``call_tool_async`` invocations
        don't block on disk I/O.

        Caveat: a SQLite ``:memory:`` engine uses a per-thread connection
        pool (``SingletonThreadPool``), so each thread sees its own private
        in-memory database and a worker-thread write would be silently lost.
        For such engines we persist on the event-loop thread instead;
        file-backed (queue-pooled) engines — the production default — go
        through ``to_thread`` as intended.

        Errors are swallowed so logging failures never break tool calls.
        """
        try:
            engine = storage.get_engine()
            kwargs: dict[str, Any] = {
                "call_id": call_id,
                "name": name,
                "source": source,
                "success": success,
                "started_at": started_at,
                "duration_ms": duration_ms,
                "error_code": error_code,
            }
            url = engine.url
            if url.drivername.startswith("sqlite") and url.database in (
                ":memory:",
                None,
                "",
            ):
                storage.create_call_log(engine=engine, **kwargs)
            else:
                await asyncio.to_thread(storage.create_call_log, engine=engine, **kwargs)
        except Exception:  # pragma: no cover
            logger.exception("Failed to persist call log for %s", name)

    def _classify_exception(
        self,
        exc: BaseException,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Map known tool exceptions to the v0.2.0 error envelope.

        Mirrors the classification order used by ``LocalWorker.call_tool`` so
        async (HTTP / future-WS) callers see the same error codes as the
        sync stdio path. Returns ``None`` when the exception does not match
        any known category — the caller is then responsible for producing
        a generic TOOL_EXCEPTION envelope.

        Categories (in order of precedence):
            SecurityViolation       → SECURITY_VIOLATION (-32005)
            RuntimeError("timeout") → TOOL_TIMEOUT     (-32002)
        """
        # Import here to avoid a circular import with the security module.
        from evermcp.security.safepath import SecurityViolation

        if isinstance(exc, SecurityViolation):
            logger.warning("Tool %s security violation: %s", name, exc)
            return {
                "success": False,
                "error": make_error(
                    SECURITY_VIOLATION,
                    f"Security violation in tool {name}: {exc}",
                    tool=name,
                    args=args,
                ),
            }

        if isinstance(exc, RuntimeError) and "timeout" in str(exc).lower():
            logger.error("Tool %s timed out: %s", name, exc)
            return {
                "success": False,
                "error": make_error(
                    TOOL_TIMEOUT,
                    f"Tool {name} timed out: {exc}",
                    tool=name,
                    args=args,
                ),
            }

        return None

    def get_capabilities(self) -> dict[str, Any]:
        """Get device capabilities from the worker."""
        return self._worker.get_capabilities()
