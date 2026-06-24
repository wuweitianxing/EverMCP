"""Safe file reading tool.

Contract per DESIGN.md:
- Path validation through SafePath (when ctx.safe_path is wired via Coordinator)
- File size limit with truncation
- Multiple encoding support
- Returns content + metadata

Errors:
- FileNotFoundError   -> TOOL_EXCEPTION (-32003)
- PermissionError      -> TOOL_EXCEPTION (-32003)
- SecurityViolation    -> SECURITY_VIOLATION (-32005)
- UnicodeDecodeError   -> swallowed (errors='replace'); content has U+FFFD
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field

from evermcp.core.tool import ToolContext, tool

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BYTES = 1024 * 1024  # 1 MB
_SUPPORTED_ENCODINGS = frozenset({
    "utf-8", "utf-16", "utf-32", "ascii", "latin-1", "cp1252", "gbk", "gb18030", "big5",
})


@tool(description="Read a text file. Path must be in filesystem_allowlist (configured via Config).")
def read_file(
    file_path: Annotated[
        str,
        Field(description="Absolute path to the file to read. Must be in filesystem_allowlist."),
    ],
    max_bytes: Annotated[
        int,
        Field(ge=1, le=100 * 1024 * 1024, description="Maximum bytes to read (default 1MB, max 100MB). Larger files are truncated."),
    ] = _DEFAULT_MAX_BYTES,
    encoding: Annotated[
        str,
        Field(description="File encoding. Common: utf-8, ascii, latin-1, gbk, gb18030, big5."),
    ] = "utf-8",
    ctx: ToolContext | None = None,
) -> dict:
    """Read a text file safely.

    Returns:
        {
            "content": str,       # file contents (possibly truncated)
            "size_bytes": int,    # actual file size on disk
            "read_bytes": int,    # bytes actually read (== min(size, max_bytes))
            "truncated": bool,    # True if file was larger than max_bytes
            "encoding": str,      # encoding used to decode
        }
    """
    log = ctx.logger if ctx else logger

    # Path validation through SafePath (if wired). When no SafePath is provided
    # (e.g. coordinator without config), we just resolve the path — no policy enforcement.
    if ctx is not None and ctx.safe_path is not None:
        validated_path = ctx.safe_path.validate(file_path)
    else:
        validated_path = Path(file_path).expanduser().resolve()

    log.info("read_file: %s (max_bytes=%d, encoding=%s)", validated_path, max_bytes, encoding)

    if not validated_path.is_file():
        raise FileNotFoundError(f"File not found: {validated_path}")

    size = validated_path.stat().st_size
    truncated = size > max_bytes

    with validated_path.open("rb") as f:
        data = f.read(max_bytes)

    content = data.decode(encoding, errors="replace")

    log.info(
        "read_file complete: %d/%d bytes read (truncated=%s)",
        len(data), size, truncated,
    )

    return {
        "content": content,
        "size_bytes": size,
        "read_bytes": len(data),
        "truncated": truncated,
        "encoding": encoding,
    }
