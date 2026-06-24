# Adding Tools to EverMCP

This document is the **complete specification** for writing tools that work
with EverMCP. It covers naming, the `@tool` decorator, type annotations,
`ToolContext`, error handling, the security model, hot-reload, and the
common patterns you'll need (network, subprocess, async).

For the fastest path, start with the 5-minute walkthrough and come back here
for the details.

---

## 1. 5-minute walkthrough

1. **Pick a category name** — `demo`, `io`, `media`, `git`, `k8s` …
   whatever fits your domain. This becomes the first part of the tool name.
2. **Create the directory**: `mkdir -p tools/<category>`.
3. **Write a tool file**: `tools/<category>/hello.py`

   ```python
   from evermcp.core.tool import tool

   @tool(description="Say hello to someone by name.")
   def hello(name: str) -> dict:
       return {"message": f"hello, {name}"}
   ```

4. **Point EverMCP at it**:

   ```bash
   evermcp serve --tools-dir tools
   ```

5. **Verify in your MCP client** — Claude Desktop, Claude Code, etc.
   should now see `demo.hello` (or whatever category you chose).

That's it. The remaining sections explain how to make the tool *good*.

---

## 2. Naming convention

**Tool full name** = `<category>.<function_name>`, where:

- `category` = the **immediate parent directory name** of the tool file
- `function_name` = the Python function name

| File path | Function | Tool name |
|---|---|---|
| `tools/demo/hello.py` | `def hello()` | `demo.hello` |
| `tools/io/read_file.py` | `def read_file()` | `io.read_file` |
| `tools/git/status.py` | `def status()` | `git.status` |

Rules:

- One file can contain **multiple** `@tool` functions (each gets its own name).
- Category names should be lowercase, no dots, no spaces.
- `__init__.py` files in category dirs are ignored.
- Files starting with `_` (e.g. `_helpers.py`) are ignored.

---

## 3. The `@tool` decorator

Every tool function must be decorated:

```python
from evermcp.core.tool import tool

@tool(description="<human-readable one-liner>")
def my_tool(...):
    ...
```

The `description` is **what the AI sees** to decide whether to call your
tool. Write it like a tool-use description for an LLM:

```python
# ✅ Good
@tool(description="Fetch a URL via HTTP GET. Returns response body + status. "
                  "Rejects private/loopback IPs.")

# ❌ Bad — too vague
@tool(description="HTTP tool")
```

---

## 4. Type annotations → JSON Schema

Every parameter **must have a type hint**. EverMCP translates it into a JSON
Schema that the AI uses for argument validation.

| Hint | JSON Schema |
|---|---|
| `str` | `{"type": "string"}` |
| `int` | `{"type": "integer"}` |
| `float` | `{"type": "number"}` |
| `bool` | `{"type": "boolean"}` |
| `list[T]` | `{"type": "array", "items": {...}}` |
| `dict[str, T]` | `{"type": "object", "additionalProperties": {...}}` |
| `Optional[T]` | nullable |
| `Literal["a", "b"]` | enum |
| `T \| None` | nullable (same as `Optional[T]`) |

For constraints, use Pydantic's `Annotated[T, Field(...)]`:

```python
from typing import Annotated
from pydantic import Field

@tool(description="Read a file safely.")
def read_file(
    file_path: Annotated[str, Field(description="Absolute path to file.")],
    max_bytes: Annotated[int, Field(ge=1, le=100_000_000,
                                    description="Max bytes to read.")] = 1_048_576,
    encoding: Annotated[str, Field(description="File encoding.")] = "utf-8",
) -> dict:
    ...
```

Constraints supported by `Field(...)`:

- `ge`, `gt`, `le`, `lt` — numeric bounds
- `min_length`, `max_length` — string length
- `pattern` — regex
- `description` — shown to the AI

Optional parameters can have defaults. Required parameters have no default
and go into the schema's `required` array.

---

## 5. `ToolContext` — accessing runtime state

If your tool needs the logger, the configured safe-path/safe-URL, or the
raw config object, declare a parameter typed `ToolContext`:

```python
from evermcp.core.tool import tool, ToolContext

@tool(description="Read a file.")
def read_file(
    file_path: Annotated[str, Field(...)],
    ctx: ToolContext | None = None,
) -> dict:
    log = ctx.logger if ctx else logger
    safe_path = ctx.safe_path if ctx else None
    config = ctx.config if ctx else None
    ...
```

`ctx` is **always optional** — the framework injects it when available,
but tools must work without it (e.g. when called directly from tests).

### Fields on `ToolContext`

| Field | Type | What it is |
|---|---|---|
| `cwd` | `str` | working directory (for tool subprocesses) |
| `logger` | `logging.Logger` | per-tool logger |
| `safe_path` | `SafePath \| None` | filesystem allowlist helper (None = no policy) |
| `safe_url` | `SafeURL \| None` | SSRF defense helper (None = no allowlist configured) |
| `config` | `Config \| None` | raw config object (custom fields per tool) |

---

## 6. Return values

Return values **must be JSON-serializable**:

| ✅ OK | ❌ Not OK |
|---|---|
| `dict` / `list` / `str` / `int` / `float` / `bool` / `None` | `pathlib.Path` (use `str(p)`) |
| Nested combinations of the above | `datetime` (use ISO string) |
| | `set` / `tuple` (use `list`) |
| | `bytes` (use base64 string) |
| | Custom objects (return `.to_dict()`) |

If you return a non-JSON-serializable value, the worker wraps it in error
code `-32004` (`TOOL_INVALID_OUTPUT`) before it reaches the AI.

---

## 7. Error handling — let exceptions propagate

**Raise exceptions**. Don't catch and return error dicts.

```python
# ✅ Good — exception propagates to worker
if not path.is_file():
    raise FileNotFoundError(f"Not found: {path}")

# ❌ Bad — AI sees a misleading "success"
return {"ok": False, "error": "not found"}
```

The worker catches your exception and wraps it in a JSON-RPC error envelope:

| Code | Meaning |
|---|---|
| `-32001` | Tool not registered |
| `-32002` | RuntimeError with "timeout" in message |
| `-32003` | Any other `Exception` |
| `-32004` | Return value not JSON-serializable |
| `-32005` | `SecurityViolation` (SafePath/SafeURL rejection) |

The AI sees the error code + message + (optionally) `data.stderr` and
`data.traceback` and can react accordingly.

---

## 8. The security model

**AI input is treated as untrusted.** You never accept paths or URLs from
the user and pass them to `open()` or `httpx.get()` directly — always go
through the safety helpers.

### Filesystem: `ctx.safe_path`

```python
from evermcp.security.safepath import SecurityViolation

def read_file(file_path: str, ctx: ToolContext | None) -> dict:
    if ctx and ctx.safe_path:
        validated = ctx.safe_path.validate(file_path)  # raises SecurityViolation
    else:
        validated = Path(file_path).expanduser().resolve()
    ...
```

`SafePath` enforces `filesystem_allowlist` and `denied_paths` from config.
Outside the allowlist → `SecurityViolation` → code `-32005`.

### Network: `ctx.safe_url`

```python
from evermcp.security.safeurl import SafeURL

def fetch(url: str, ctx: ToolContext | None) -> dict:
    safe_url = ctx.safe_url if ctx and ctx.safe_url else SafeURL()
    scheme, host = safe_url.validate(url)  # raises SecurityViolation
    ...
```

`SafeURL` rejects:

- Non-http/https schemes
- `localhost` and known loopback hostnames
- Literal IPs in `is_private` / `is_loopback` / `is_link_local` / `is_reserved` / `is_multicast`
- Hostnames not in `network_allowlist` (when one is configured)

**v1 known limitation**: literal hostname only — DNS rebinding attacks
(where `evil.com` resolves to `127.0.0.1` at request time) are NOT
mitigated. v2 will add DNS pinning. See [`../SECURITY.md`](../SECURITY.md).

### Subprocess: argv only, never shell

```python
import subprocess

# ✅ Good — argv list, no shell
result = subprocess.run(
    ["ffmpeg", "-i", str(input_path), str(output_path)],
    capture_output=True, text=True, timeout=600,
)

# ❌ NEVER — shell injection
subprocess.run(f"ffmpeg -i {input_path} {output_path}", shell=True)
```

Use `subprocess.Popen` if you need streaming output. Always set a
`timeout` and clean up the process on timeout.

---

## 9. Common patterns

### Async tools (long-running inference)

If your tool is I/O bound (network calls, ML inference), make it `async`:

```python
import asyncio
from evermcp.core.tool import tool

@tool(description="Synthesize speech.")
async def synthesize(text: str) -> dict:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _blocking_inference, text)
    return result
```

The MCP server's event loop handles the `await`. The tool runs in a thread
pool, so it doesn't block other concurrent tool calls.

### Streaming progress

The stdio MCP transport can't stream progress to the AI mid-call — the
AI sees the response only after the tool returns. Two options:

1. **Log to the log file** (`~/.evermcp/evermcp.log`) so the developer
   can watch progress.
2. **Use `ctx.logger`** for the same effect — it lands in the log file.

For progress visible to the AI, wait for v2 (SSE transport).

### Custom config

Tools can extend `Config` with their own fields via `ctx.config`:

```python
@tool(description="Transcode video.")
def transcode(input_path: str, ctx: ToolContext | None = None) -> dict:
    binary = "ffmpeg"  # default
    if ctx and ctx.config and hasattr(ctx.config, "ffmpeg_binary"):
        binary = ctx.config.ffmpeg_binary
    ...
```

EverMCP ships a few core fields on `Config` (`log_level`, `ffmpeg_binary`,
etc.). Add your tool-specific fields by **extending `Config` in your own
code** — pass a subclass or augmented instance to `Coordinator(registry=..., config=cfg)`.

---

## 10. Hot-reload

EverMCP watches your tools directory and rescans when files change. New
files appear within ~1 second. **However**, the stdio MCP protocol caches
the tool list at handshake — so the AI won't see new tools until you
restart the AI client (not the server).

This is a stdio MCP protocol limitation, not an EverMCP bug. It will be
fixed when we add SSE transport in v2.

---

## 11. Logging

Two streams:

- **stderr** (visible to the developer): structured JSON lines like
  `{"ts": "2026-06-25T10:00:00Z", "level": "INFO", "msg": "..."}`.
  Routed through the CLI's logging setup.
- **`~/.evermcp/evermcp.log`** (rotated at 10 MB × 3): human-readable,
  always DEBUG level.

Use `ctx.logger` (or your module-level `logger`) to write messages. Avoid
`print()` — it'll pollute the stdio MCP stream.

Set level via `~/.evermcp/config.toml` → `[general] log_level` or `EVERMCP_LOG_LEVEL`.

---

## 12. Testing your tool

Write unit tests in `tests/unit/test_<name>.py`. Mock any external
dependencies (subprocess, network, filesystem). The test pattern:

```python
from tools.your_category.your_tool import your_tool as _mod
from evermcp.core.tool import ToolFunc

your_tool: ToolFunc = _mod  # type: ignore[assignment]

def test_happy_path(monkeypatch):
    # mock dependencies
    result = your_tool.fn(arg1="hello")
    assert result["..."] == "..."
```

`ToolFunc.fn(**kwargs)` calls your function directly with the args the AI
would pass. `monkeypatch.setattr(module, "name", fake)` is the cleanest way
to stub dependencies (cleaner than `unittest.mock.patch`).

---

## 13. Debugging checklist

If your tool isn't appearing:

- [ ] `tools_dir` points to the right directory — try `evermcp list-tools --tools-dir <path>`
- [ ] File is `tools/<category>/<something>.py`, not `tools/<category>/<sub>/something.py`
      (only the immediate parent counts as the category)
- [ ] Function is decorated with `@tool(description=...)`
- [ ] File doesn't start with `_`
- [ ] `__init__.py` in the category dir is fine but doesn't matter

If your tool errors out:

- [ ] Check `~/.evermcp/evermcp.log` for the stack trace
- [ ] Look at the error envelope's `code` (-32001 to -32005)
- [ ] For `-32003`, your exception is in `data.stderr` or `data.traceback`

If the AI misuses your tool:

- [ ] Improve the `description` — be specific about what the tool does
- [ ] Add `Field(description=...)` to parameters explaining what they mean
- [ ] Use `Literal[...]` for enums instead of open strings
- [ ] Add `ge`/`le`/`min_length`/`max_length` constraints to prevent garbage input

---

## 14. Complete example: `io.read_file`

```python
"""Safe file reading tool."""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Annotated

from pydantic import Field
from evermcp.core.tool import ToolContext, tool

logger = logging.getLogger(__name__)
_DEFAULT_MAX_BYTES = 1024 * 1024  # 1 MB


@tool(description="Read a text file. Path must be in filesystem_allowlist.")
def read_file(
    file_path: Annotated[str, Field(description="Absolute path to the file.")],
    max_bytes: Annotated[
        int,
        Field(ge=1, le=100_000_000,
              description="Max bytes to read (default 1MB, max 100MB)."),
    ] = _DEFAULT_MAX_BYTES,
    ctx: ToolContext | None = None,
) -> dict:
    log = ctx.logger if ctx else logger

    # Validate path against SafePath (if configured)
    if ctx is not None and ctx.safe_path is not None:
        validated_path = ctx.safe_path.validate(file_path)
    else:
        validated_path = Path(file_path).expanduser().resolve()

    log.info("read_file: %s (max_bytes=%d)", validated_path, max_bytes)

    if not validated_path.is_file():
        raise FileNotFoundError(f"File not found: {validated_path}")

    size = validated_path.stat().st_size
    with validated_path.open("rb") as f:
        data = f.read(max_bytes)

    return {
        "content": data.decode("utf-8", errors="replace"),
        "size_bytes": size,
        "read_bytes": len(data),
        "truncated": size > max_bytes,
    }
```

This is the actual `examples/tools/io/read_file.py`. It's 50 lines of
real, tested code — copy it as a starting point for filesystem tools.

---

## 15. Where to get help

- [`DESIGN.md`](../DESIGN.md) — architecture, design rationale, v2 plans
- [`SECURITY.md`](../SECURITY.md) — security model in detail
- `tests/unit/` — real tool examples with tests
- Source code — `evermcp/core/tool.py` is the `@tool` decorator
  implementation (well-commented)