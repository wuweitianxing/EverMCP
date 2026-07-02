# Changelog

All notable changes to EverMCP are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-07-02

EverMCP evolves from a local single-process tool framework into an
**MCP Gateway + Capability Governance UI**. Three milestone stages (S0–S2)
land in this release; S3 (polish) is deferred.

### Added — S0: capability generalization + dual transport + persistence

- **Capability model**: generalized `Tool` into `Capability` (`Tool` /
  `Resource` / `Prompt`) via `evermcp/core/capability.py`. The `@tool`
  decorator is unchanged; `@resource` / `@prompt` are new.
- **Provider abstraction** (`evermcp/core/provider.py`): `CapabilityProvider`
  protocol with `LocalFilesystemProvider` (migrates the v0.2.0 `ToolRegistry`
  scan + hot-reload; filesystem scanning stays Tool-only).
- **`CapabilityRegistry`** (`evermcp/core/registry.py`): multi-provider
  aggregation by `kind`; `ToolRegistry` retained as an alias.
- **Dual MCP transports**: stdio (unchanged) + Streamable HTTP via the
  official `mcp` SDK (`evermcp/protocol/http_server.py`).
- **MCP `resources/*` and `prompts/*` handlers** in `mcp_server.py`.
- **SQLite persistence base** (`evermcp/storage.py`) with `InlineCapability`
  table; `[gateway]` config section (host/port/`require_key`/`db_url`).
- **CLI flags**: `evermcp serve [--stdio] [--http --host H --port P]`.

### Added — S1: capability node-tree UI + form declarations

- **Web UI** (`evermcp/web/`): FastAPI + Vue3 ESM (CDN) + Element Plus.
  Capability node tree (left) + form declaration editor (center) + test-call
  panel + admin (right). No build step.
- **`InlineDeclarationProvider`**: form-declared Tool/Resource/Prompt stored
  in SQLite (pure metadata, no code execution).
- **REST API** (`evermcp/web/rest.py`): `GET /api/tree`, capability CRUD,
  `POST /api/test`.
- **Local token auth** for the UI (`TokenAuthMiddleware`); `--ui` flag.
- **Enable/disable governance**: `InlineCapability.enabled` controls MCP
  `tools/list` visibility; local/remote toggle is in-memory.

### Added — S2: reverse server registration + auth + logs

- **WebSocket reverse bridge** (`evermcp/protocol/ws_channel.py`): remote
  clients expose an existing stdio MCP server to the gateway via an outbound
  WS (NAT traversal, approach ported from MCPPort).
- **`RemoteClientProvider`**: bridges an `mcp.ClientSession` over WS; remote
  tools appear under the `remote.<client_id>.<tool>` namespace.
- **`evermcp-connect`** adapter (`evermcp/connect/stdio_ws_bridge.py`):
  `evermcp-connect --gateway ws://gw/ws --token K -- <mcp-server-cmd>`.
- **API key auth** (`evermcp/security/auth.py`): hash-stored, revocable,
  scope-bound (`ws:connect`, `admin`). WS handshake + admin REST endpoints.
- **Admin REST API** (`evermcp/protocol/rest_api.py`): `GET /api/clients`,
  API key CRUD, `GET /api/logs`.
- **Call logging**: `CallLog` table persists every call (name/source/success/
  duration/error_code); queryable from the UI. Indexed + prunable.
- **Heartbeat** (30s) + `remote_call_timeout_s` (default 60s); remote
  disconnect/timeout reuse error codes `-32002` / `-32003`.
- **Remote `isError` propagation**: remote tool failures raise
  `RemoteToolError` → `TOOL_EXCEPTION` (`-32003`) instead of masking as success.

### Changed

- **Tool-name namespace separator**: `:` → `.` to comply with MCP SEP-986
  (`[A-Za-z0-9._-]` only). Remote tools are now `remote.<client_id>.<tool>`;
  UI tree names use `local.<name>` / `inline.<name>`. Local tool names
  (`<category>.<name>`) are unchanged.
- **`pyproject.toml`**: version `0.2.0` → `0.3.0`; description updated to
  "MCP Gateway + Capability Governance UI for AI Agents"; Development Status
  `3 - Alpha` → `4 - Beta`.
- **`SECURITY.md`**: trust boundaries updated (added remote-client WS and
  browser-UI edges); new §8 documents API key + UI token auth; "not done"
  table refreshed.
- **`DESIGN.md`**: marked as a historical/archived document (describes
  v0.2.0); current behavior lives in `README.md` + `docs/gateway-plan.md`.

### Dependencies added

- `sqlmodel>=0.0.16` (S0) · `fastapi>=0.110.0` (S1) · `uvicorn[standard]>=0.27`
  (S1) · `websockets>=12.0` (S2). All permissive licenses.

### Backward compatibility

- The `@tool` decorator and `tools/<category>/<name>.py` contract are
  unchanged; `ToolRegistry` is retained as a `CapabilityRegistry` alias.
- Bare local tool names (`io.read_file`, no `local.` prefix) still work.
- `evermcp serve --tools-dir` behavior is unchanged; `--http` / `--ui` are
  opt-in flags (default off).
- New `[gateway]` config section is additive; old configs work as-is.
- Error codes `-32001..-32005` are reused; no new ranges.

### Tests

267 passed, 9 warnings (all `websockets` deprecation warnings, unrelated to
this release). Stage reviews archived under `docs/reviews/`.

---

## [0.2.0] — 2026-06-25

- Stripped bundled tools; the project ships only the framework + 2 reference
  tools in `examples/tools/`. Users point `--tools-dir` at their own tools.
- `@tool` decorator, `ToolRegistry` with watchdog hot-reload, `SafePath` /
  `SafeURL` security helpers, typed error envelopes (`-32001..-32005`),
  stdio MCP transport, layered config (TOML → env → CLI).
