"""Coordinator — receives tool calls from MCP layer, dispatches to workers.

v1: Single LocalWorker, **in-process direct function call** (no serialization,
no dispatcher; see DESIGN.md §Worker Protocol §Schema). Coordinator holds a
LocalWorker instance and invokes its methods directly with Python dicts.

v2: Multiple workers, capability-based routing. The dispatch layer here will
gain a JSON-RPC-over-gRPC serialization boundary (Coordinator -> remote
LocalWorker), but the v1 in-process path stays as a fast local fastpath.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from evermcp.core.registry import ToolRegistry
from evermcp.core.tool import ToolContext
from evermcp.workers.local import LocalWorker

if TYPE_CHECKING:
    from evermcp.security.config import Config
    from evermcp.security.safepath import SafePath
    from evermcp.security.safeurl import SafeURL

logger = logging.getLogger(__name__)


class Coordinator:
    """Central dispatcher that routes tool calls to workers.

    v1: Wraps a single LocalWorker. The routing logic is trivial (one worker).
    v2: Will maintain a worker pool and route based on CapabilityDescriptor.
    """

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        config: Config | None = None,
    ) -> None:
        self._registry = registry or ToolRegistry()
        self._worker = LocalWorker(self._registry)
        self._config = config
        # Build SafePath once at init so we don't reconstruct it per call.
        # If neither allowlist nor denied paths are configured, _safe_path stays None
        # (i.e. no filesystem policy enforcement — tools are responsible for their own checks).
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
    def registry(self) -> ToolRegistry:
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
        """Scan tools and start hot-reload watcher."""
        self._registry.scan()
        self._registry.start_watching()
        logger.info("Coordinator initialized with %d tools", len(self._registry.list_tools()))

    def shutdown(self) -> None:
        """Stop the hot-reload watcher."""
        self._registry.stop_watching()
        logger.info("Coordinator shut down")

    def list_tools(self) -> list[dict[str, Any]]:
        """List all available tools. Delegates to worker."""
        return self._worker.list_tools()

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch a tool call to the worker.

        v1: Always sends to the single LocalWorker.
        v2: Will select worker based on capabilities.

        Builds a ToolContext with safe_path and config wired from __init__ so
        tools can access them via the ctx parameter.
        """
        call_id = str(uuid.uuid4())
        args = arguments or {}
        ctx = ToolContext(
            cwd=".",
            logger=logger,
            safe_path=self._safe_path,
            safe_url=self._safe_url,
            config=self._config,
        )
        logger.info("Dispatching tool call: %s (call_id=%s)", name, call_id)
        result = self._worker.call_tool(name, args, call_id=call_id, ctx=ctx)
        logger.info("Tool call result: %s (call_id=%s, success=%s)", name, call_id, result.get("success"))
        return result

    def get_capabilities(self) -> dict[str, Any]:
        """Get device capabilities from the worker."""
        return self._worker.get_capabilities()
