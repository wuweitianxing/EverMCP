"""Watchdog handler for tool file changes.

S0: accepts either a LocalFilesystemProvider (preferred) or a legacy
ToolRegistry (kept for hot-reload tests). Both expose `.tools_dir` and
`.scan()`, so the handler is duck-typed across both.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileSystemEventHandler,
)

logger = logging.getLogger(__name__)


class _ReloadTarget(Protocol):
    """Anything with a tools_dir path and a scan() method."""

    tools_dir: Path

    def scan(self) -> Any: ...


class ToolFileHandler(FileSystemEventHandler):
    """Handles file system events in the tools/ directory for hot-reload."""

    def __init__(self, registry: Any = None, provider: Any = None) -> None:
        # Accept either form: provider= (preferred S0), registry= (legacy).
        # Both expose .tools_dir and .scan(); pick whichever was given.
        self._target: _ReloadTarget = provider if provider is not None else registry
        assert self._target is not None, "ToolFileHandler needs a provider or registry"

    def on_created(self, event: FileCreatedEvent) -> None:
        if self._is_tool_file(event.src_path):
            logger.info("New tool file detected: %s", event.src_path)
            self._reload_category(event.src_path)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if self._is_tool_file(event.src_path):
            logger.info("Tool file modified: %s", event.src_path)
            self._reload_category(event.src_path)

    def on_deleted(self, event: FileDeletedEvent) -> None:
        if self._is_tool_file(event.src_path):
            logger.info("Tool file deleted: %s", event.src_path)
            self._reload_category(event.src_path)

    def _is_tool_file(self, path: str) -> bool:
        """Check if the path is a tool Python file (not __init__.py)."""
        p = Path(path)
        return p.suffix == ".py" and p.name != "__init__.py"

    def _reload_category(self, path: str) -> None:
        """Re-scan the affected category directory.

        v1: Full rescan — clears all and reloads from disk.
        Simple but O(total_tools). Optimize later if needed.
        """
        p = Path(path)
        category_dir = p.parent

        if category_dir == self._target.tools_dir:
            return  # root-level file, not a category

        self._target.scan()
        logger.info("Hot-reload complete for category: %s", category_dir.name)
