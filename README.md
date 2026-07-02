# EverMCP

**MCP Gateway + Capability Governance UI for AI Agents.** You write the tools;
we provide registration, security boundaries, multi-source aggregation,
and both stdio and Streamable HTTP transports.

This project **does not ship any tools**. It ships the framework, the
configuration model, and a couple of reference tools in `examples/tools/`
that you copy and adapt.

## What you get

- **Tool Registry** that auto-discovers any `tools/<category>/*.py` you
  point it at, watching for changes with hot-reload.
- **Multi-source capability aggregation**: local files + remote clients
  (WebSocket reverse-connection) + inline UI declarations, all in one place.
- **Security boundaries**: `SafePath` (filesystem allowlists) and `SafeURL`
  (SSRF defense) helpers wired into `ToolContext`.
- **Dual MCP transports**: stdio (for Claude Desktop / Claude Code / Cursor)
  and Streamable HTTP (for agents that speak HTTP).
- **Web UI**: capability tree visualization, inline declarations,
  client/key management, call logs.
- **WebSocket reverse bridge**: `evermcp-connect` lets you expose any
  existing MCP server to the gateway from behind NAT.
- **LocalWorker protocol** with typed error envelopes (codes `-32001`..`-32005`).

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
# Run with stdio MCP transport only (classic mode):
evermcp serve --tools-dir examples/tools

# Run with both stdio + HTTP + Web UI:
evermcp serve --tools-dir examples/tools --http --ui

# List tools without starting server:
evermcp list-tools --tools-dir examples/tools

# Connect an existing MCP server to the gateway:
evermcp connect --token <api-key> -- ws://127.0.0.1:8788/ws mcp-server
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

[gateway]
host = "127.0.0.1"
port = 8787
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
├── evermcp/               # framework
│   ├── core/             # @tool decorator, ToolRegistry, ToolContext
│   ├── workers/          # LocalWorker, error envelope
│   ├── protocol/         # Coordinator + MCP stdio server + HTTP server + WS channel + REST API
│   ├── security/         # SafePath, SafeURL, Config, auth
│   ├── web/              # FastAPI Web UI
│   ├── connect/          # stdio-ws bridge (evermcp-connect)
│   └── cli.py            # `evermcp serve` / `evermcp list-tools` / `evermcp connect`
├── examples/
│   └── tools/            # 2 reference tools — copy these to start
│       ├── demo/hello.py
│       └── io/read_file.py
├── docs/
│   ├── adding-tools.md   # full tool-authoring spec
│   ├── DESIGN.md         # historical design (archived)
│   └── reviews/          # S0/S1/S2 reviews (archived)
├── tests/                # unit / worker / registry / e2e / integration / security
├── tools/                # empty by default — point --tools-dir here for your tools
├── config.example.toml
├── CHANGELOG.md          # release history
├── SECURITY.md           # v0.3.0 security model
└── pyproject.toml
```

## CLI

```
evermcp serve [--tools-dir PATH] [--stdio/--no-stdio] [--http/--no-http]
              [--host HOST] [--port PORT] [--ui/--no-ui] [--init-db/--no-init-db]
              # start MCP server (stdio and/or HTTP transport)
evermcp list-tools [--tools-dir PATH]  # print registered tools, exit
evermcp connect --token TOKEN -- GATEWAY_WS_URL SERVER_COMMAND
              # connect a local MCP server to the gateway
evermcp --help
evermcp --version
evermcp -v serve ...                    # enable DEBUG logging
evermcp -c /path/to/config.toml serve   # custom config file
```

## Versioning

- Python: 3.11+ (uses `datetime.UTC`, `tomllib`, PEP 695 generics)
- EverMCP: see `pyproject.toml` (`version = "0.3.0"`)

## License

MIT — see [`LICENSE`](LICENSE).