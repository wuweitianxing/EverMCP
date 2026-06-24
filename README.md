# EverMCP

**MCP tool orchestration framework for AI Agents.** You write the tools; we
provide the registration, security boundary, and stdio transport.

This project **does not ship any tools**. It ships the framework, the
configuration model, and a couple of reference tools in `examples/tools/`
that you copy and adapt.

## What you get

- A **Tool Registry** that auto-discovers any `tools/<category>/*.py` you
  point it at, watching for changes with hot-reload.
- A **Security boundary** — `SafePath` (filesystem allowlists) and `SafeURL`
  (SSRF defense) helpers wired into `ToolContext`.
- A **stdio MCP server** that exposes your tools to Claude Desktop, Claude
  Code, Cursor, or any other MCP client.
- A **Worker protocol** with typed error envelopes (codes `-32001`..`-32005`).

You bring your own tool directory.

## Install

```bash
git clone <repo-url>
cd EverMCP
pip install -e ".[dev]"
```

Python 3.11+ required.

## Quick start

```bash
# Run the bundled examples (2 reference tools):
evermcp serve --tools-dir examples/tools

# Run with your own tool directory:
evermcp serve --tools-dir ~/my-mcp-tools

# Or set the env var once:
export EVERMCP_TOOLS_DIR=~/my-mcp-tools
evermcp serve

# Verify what's loaded:
evermcp list-tools --tools-dir examples/tools
```

### Claude Desktop config

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "evermcp": {
      "command": "evermcp",
      "args": ["serve", "--tools-dir", "C:/Users/you/my-mcp-tools"]
    }
  }
}
```

## Configuration

Copy `config.example.toml` to `~/.evermcp/config.toml`:

```toml
[general]
log_level = "INFO"
log_file = "~/.evermcp/evermcp.log"

[security]
filesystem_allowlist = ["~/data", "~/Downloads"]
network_allowlist   = ["github.com", "pypi.org"]
denied_paths        = ["~/.ssh", "~/.aws", "~/.config/gh"]
```

Loading order: defaults → `~/.evermcp/config.toml` → env vars (`EVERMCP_*`) → CLI flags.

## Write your first tool

See [`examples/tools/demo/hello.py`](examples/tools/demo/hello.py) — the
smallest possible tool, 12 lines.

For the full specification (subprocess tools, async tools, error envelopes,
security model), read [`docs/adding-tools.md`](docs/adding-tools.md).

## Project structure

```
EverMCP/
├── evermcp/            # framework (no tool-specific code)
│   ├── core/           # @tool decorator, ToolRegistry, ToolContext
│   ├── workers/        # LocalWorker, error envelope
│   ├── protocol/       # Coordinator + MCP stdio server
│   ├── security/       # SafePath, SafeURL, Config
│   └── cli.py          # `evermcp serve` / `evermcp list-tools`
├── examples/
│   └── tools/          # 2 reference tools — copy these to start
│       ├── demo/hello.py
│       └── io/read_file.py
├── docs/
│   └── adding-tools.md # full tool-authoring spec
├── tests/              # unit / worker / registry / e2e / integration / security
├── tools/              # empty by default — point --tools-dir here for your tools
├── config.example.toml
├── DESIGN.md           # architecture & design rationale
├── SECURITY.md         # v1.0 security model
└── pyproject.toml
```

## CLI

```
evermcp serve [--tools-dir PATH]        # start MCP server (stdio)
evermcp list-tools [--tools-dir PATH]   # print registered tools, exit
evermcp --help
evermcp --version
evermcp -v serve ...                    # enable DEBUG logging
evermcp -c /path/to/config.toml serve   # custom config file
```

## Versioning

- Python: 3.11+ (uses `datetime.UTC`, `tomllib`, PEP 695 generics)
- EverMCP: see `pyproject.toml` (`version = "0.2.0"`)

## License

MIT — see [`LICENSE`](LICENSE).