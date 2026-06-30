# EverMCP Tool Examples

This directory contains **minimal reference implementations** of tools that
work with EverMCP. Use them as templates when writing your own.

## What's here

| Tool | Source | Demonstrates |
|---|---|---|
| `demo.hello` | [`tools/demo/hello.py`](tools/demo/hello.py) | The minimum viable tool: `@tool` decorator + return dict |
| `io.read_file` | [`tools/io/read_file.py`](tools/io/read_file.py) | File-system tool using `ctx.safe_path` for path allowlist enforcement |

Both tools are fully functional. Run them with:

```bash
evermcp serve --tools-dir examples/tools
```

You should see Claude (or another MCP client) pick up both tools.

## Writing your own tools

1. **Copy** `tools/demo/hello.py` into a new directory under `tools/`.
2. **Rename** the file and the function. The full tool name becomes
   `<directory_name>.<function_name>` — e.g. `tools/media/transcode.py`
   with `def transcode()` is exposed as `media.transcode`.
3. **Add parameters** with type hints. They'll appear in the JSON Schema
   the AI sees, including `min_length`/`max_length`/`ge`/`le` constraints
   from Pydantic `Field(...)`.
4. **Use `ctx.safe_path` / `ctx.safe_url`** for any file or network access —
   never read user input directly.
5. **Raise exceptions** instead of returning error dicts; EverMCP's worker
   wraps them in a JSON-RPC error envelope automatically.

For the full specification (subprocess tools, async tools, error envelopes,
security model), see [`../docs/adding-tools.md`](../docs/adding-tools.md).

## Resources & Prompts (S0 manual registration)

`@tool` is auto-registered by the local-filesystem scan, but `@resource`
and `@prompt` (defined in `evermcp.core.capability`) are **not** — S0
ships no `InlineDeclarationProvider` yet. To expose a Resource or Prompt
in S0, call the helper in your entry script after `Coordinator.initialize()`:

````python
from evermcp.core.registry import ToolRegistry
from evermcp.protocol.coordinator import Coordinator
from examples.tools.demo.resource_prompt_demo import register_demo_capabilities

coord = Coordinator(registry=ToolRegistry(tools_dir="examples/tools"))
coord.initialize()
register_demo_capabilities(coord.registry)  # wires `about` + `demo.greet_prompt`
````

The helper attaches the demo `ResourceFunc`/`PromptFunc` directly to the
`LocalFilesystemProvider._caps` dict — the registry's `list_resources`,
`list_prompts`, `read_resource`, and `get_prompt` all read from that dict.
For S1, replace this with an `InlineDeclarationProvider`; call sites
using `coordinator.read_resource(...)` / `coordinator.get_prompt(...)`
remain unchanged. See
[`tools/demo/resource_prompt_demo.py`](tools/demo/resource_prompt_demo.py)
for the full demo.

## Layout

```
examples/tools/
├── demo/
│   ├── __init__.py
│   └── hello.py         # → demo.hello
└── io/
    ├── __init__.py
    └── read_file.py     # → io.read_file
```

Tools at deeper paths work too — only the **immediate parent directory
name** becomes the category.