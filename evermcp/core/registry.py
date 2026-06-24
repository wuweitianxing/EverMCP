"""Tool Registry — scans tools/ directory, hot-reload via watchdog."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from evermcp.core.tool import ToolDescriptor, ToolFunc

logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"


class ToolRegistry:
    """Discovers and registers tools from the tools/ directory.

    Tools are organized as: tools/<category>/<name>.py
    Each @tool-decorated function becomes <category>.<function_name>.
    """

    def __init__(self, tools_dir: Path | str | None = None) -> None:
        self._tools_dir = Path(tools_dir) if tools_dir else TOOLS_DIR
        self._tools: dict[str, ToolFunc] = {}
        self._watcher = None

    @property
    def tools_dir(self) -> Path:
        return self._tools_dir

    def scan(self) -> list[ToolDescriptor]:
        """Scan tools/ directory and register all @tool functions."""
        self._tools.clear()

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

        descriptors = [t.descriptor() for t in self._tools.values()]
        logger.info("Scanned %d tools from %s", len(descriptors), self._tools_dir)
        return descriptors

    def _load_tool_file(self, path: Path, category: str) -> None:
        """Load a single tool file and register its @tool functions."""
        module_name = f"tools.{category}.{path.stem}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                logger.warning("Cannot load module spec: %s", path)
                return

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("Failed to load tool file: %s", path)
            return

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, ToolFunc):
                attr.category = category
                full_name = f"{category}.{attr.fn.__name__}"
                self._tools[full_name] = attr
                logger.info("Registered tool: %s", full_name)

    def get(self, name: str) -> ToolFunc | None:
        """Look up a tool by its full name (e.g. 'media.transcode')."""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDescriptor]:
        """Return descriptors for all registered tools."""
        return [t.descriptor() for t in self._tools.values()]

    def start_watching(self) -> None:
        """Start watchdog file system watcher for hot-reload."""
        try:
            from watchdog.observers import Observer

            from evermcp.core.watcher import ToolFileHandler

            self._watcher = Observer()
            handler = ToolFileHandler(registry=self)
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
