"""LocalWorker — in-process worker implementation.

v1 transport: **in-process function call** (see DESIGN.md §Worker Protocol
§Schema). The Coordinator invokes our methods directly with Python objects;
no JSON serialization, no RPC dispatcher. This is a deliberate v1
simplification: cross-process JSON-RPC serialization will be added in v2
when workers split into separate processes behind gRPC.
"""

from __future__ import annotations

import json
import logging
import platform
import uuid
from typing import Any

from evermcp.core.registry import ToolRegistry
from evermcp.core.tool import (
    SECURITY_VIOLATION,
    TOOL_EXCEPTION,
    TOOL_INVALID_OUTPUT,
    TOOL_NOT_FOUND,
    TOOL_TIMEOUT,
    ToolContext,
    ToolError,
    make_error,
)
from evermcp.security.safepath import SecurityViolation

logger = logging.getLogger(__name__)


class CapabilityDescriptor(dict):
    """Device capability descriptor for the worker."""

    def __init__(
        self,
        cpu_cores: int = 0,
        memory_total_mb: int = 0,
        memory_free_mb: int = 0,
        disk_free_gb: float = 0.0,
        ffmpeg_encoders: list[str] | None = None,
        gpu_available: bool = False,
        npu_available: bool = False,
        platform_name: str = "",
    ) -> None:
        super().__init__(
            cpu_cores=cpu_cores,
            memory_total_mb=memory_total_mb,
            memory_free_mb=memory_free_mb,
            disk_free_gb=disk_free_gb,
            ffmpeg_encoders=ffmpeg_encoders or [],
            gpu_available=gpu_available,
            npu_available=npu_available,
            platform=platform_name,
        )


class ToolResult(dict):
    """Result envelope for tool calls."""

    def __init__(self, success: bool, result: Any = None, error: ToolError | None = None) -> None:
        super().__init__(success=success, result=result, error=error)


class LocalWorker:
    """In-process worker that executes tools via the ToolRegistry.

    v1: All calls are synchronous, in-process.
    v2: Replace with gRPC-based remote worker.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def list_tools(self) -> list[dict[str, Any]]:
        """List all available tools with their descriptors."""
        return self._registry.list_tools()

    def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        call_id: str | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """Execute a tool by name with the given arguments.

        If `ctx` is None, a default ToolContext is created. The Coordinator typically
        pre-builds a context with safe_path and config so tools can access them.

        Error codes:
        - TOOL_NOT_FOUND: tool not registered
        - SECURITY_VIOLATION: path/URL not in allowlist
        - TOOL_TIMEOUT: tool exceeded time limit
        - TOOL_EXCEPTION: tool function raised any other exception
        - TOOL_INVALID_OUTPUT: tool returned non-JSON-serializable value
        """
        call_id = call_id or str(uuid.uuid4())
        tool_func = self._registry.get(name)

        if tool_func is None:
            return ToolResult(
                success=False,
                error=make_error(TOOL_NOT_FOUND, f"Tool not found: {name}", tool=name, args=args),
            )

        # Execute the tool
        if ctx is None:
            ctx = ToolContext(cwd=".", logger=logger)
        try:
            result = tool_func.call(args, ctx=ctx)
        except SecurityViolation as exc:
            logger.warning("Tool %s security violation: %s", name, exc)
            return ToolResult(
                success=False,
                error=make_error(
                    SECURITY_VIOLATION,
                    f"Security violation in tool {name}: {exc}",
                    tool=name,
                    args=args,
                ),
            )
        except RuntimeError as exc:
            # Detect timeout errors (tools raise RuntimeError("...timeout..."))
            if "timeout" in str(exc).lower():
                logger.error("Tool %s timed out: %s", name, exc)
                return ToolResult(
                    success=False,
                    error=make_error(
                        TOOL_TIMEOUT,
                        f"Tool {name} timed out: {exc}",
                        tool=name,
                        args=args,
                    ),
                )
            logger.exception("Tool %s raised RuntimeError", name)
            return ToolResult(
                success=False,
                error=make_error(
                    TOOL_EXCEPTION,
                    f"Tool {name} raised an exception: {exc}",
                    tool=name,
                    args=args,
                    stderr=str(exc),
                ),
            )
        except Exception as exc:
            logger.exception("Tool %s raised an exception", name)
            return ToolResult(
                success=False,
                error=make_error(
                    TOOL_EXCEPTION,
                    f"Tool {name} raised an exception: {exc}",
                    tool=name,
                    args=args,
                    stderr=str(exc),
                ),
            )

        # Validate JSON serializability (catches TypeError/ValueError, returns -32004)
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            logger.error("Tool %s returned non-JSON-serializable result: %s", name, exc)
            return ToolResult(
                success=False,
                error=make_error(
                    TOOL_INVALID_OUTPUT,
                    f"Tool {name} returned non-JSON-serializable output: {exc}",
                    tool=name,
                    args=args,
                    result_type=type(result).__name__,
                ),
            )

        return ToolResult(success=True, result=result)

    def get_capabilities(self) -> CapabilityDescriptor:
        """Return the device capability descriptor."""
        try:
            import psutil

            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            return CapabilityDescriptor(
                cpu_cores=psutil.cpu_count(logical=False) or 0,
                memory_total_mb=mem.total // (1024 * 1024),
                memory_free_mb=mem.available // (1024 * 1024),
                disk_free_gb=round(disk.free / (1024 ** 3), 1),
                ffmpeg_encoders=self._detect_ffmpeg_encoders(),
                gpu_available=self._detect_gpu(),
                npu_available=False,
                platform_name=platform.system().lower(),
            )
        except ImportError:
            return CapabilityDescriptor(platform_name=platform.system().lower())

    def _detect_ffmpeg_encoders(self) -> list[str]:
        """Detect available FFmpeg encoders."""
        import shutil
        import subprocess

        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            return []

        try:
            result = subprocess.run(
                [ffmpeg_path, "-encoders", "-hide_banner"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            encoders = []
            for line in result.stdout.splitlines():
                if "V" in line[:5]:  # Video encoders
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        encoders.append(parts[1])
            return encoders
        except Exception:
            return []

    def _detect_gpu(self) -> bool:
        """Simple GPU detection (v1: basic check)."""
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi"], capture_output=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
