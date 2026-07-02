# EverMCP

**AI 代理的 MCP 网关 + 能力治理可视化平台。** 你编写工具；我们提供注册、安全边界、多源聚合，以及 stdio 和 Streamable HTTP 双传输。

本项目**不附带任何工具**。它提供框架、配置模型，以及 `examples/tools/` 中的几个参考工具供你复制和改编。

## 你将获得

- **工具注册表**，自动发现你指向的任何 `tools/<category>/*.py`，并通过热重载监控变化。
- **多源能力聚合**：本地文件 + 远程客户端（WebSocket 反向连接）+ UI 内联声明，全部集中管理。
- **安全边界**：集成在 `ToolContext` 中的 `SafePath`（文件系统允许列表）和 `SafeURL`（SSRF 防御）辅助工具。
- **双 MCP 传输**：stdio（用于 Claude Desktop / Claude Code / Cursor）和 Streamable HTTP（用于支持 HTTP 的代理）。
- **Web UI**：能力节点树可视化、内联声明、客户端/密钥管理、调用日志。
- **WebSocket 反向桥接**：`evermcp-connect` 让你可以从 NAT 后方将任何现有 MCP 服务器暴露给网关。
- **LocalWorker 协议**，带类型化错误信封（代码 `-32001`..`-32005`）。

你只需准备自己的工具目录。

## 安装

```bash
git clone <repo-url>
cd EverMCP
pip install -e ".[dev]"
```

需要 Python 3.11+。

## 快速开始

```bash
# 仅使用 stdio MCP 传输（经典模式）：
evermcp serve --tools-dir examples/tools

# 同时使用 stdio + HTTP + Web UI：
evermcp serve --tools-dir examples/tools --http --ui

# 不带工具启动（仅框架）
evermcp serve --tools-dir examples/tools

# 不带工具启动（仅框架）
evermcp list-tools --tools-dir examples/tools

# 将现有 MCP 服务器连接到网关：
evermcp connect --token <api-key> -- ws://127.0.0.1:8788/ws mcp-server
```

### Claude Desktop 配置

添加到你的 `claude_desktop_config.json`：

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

## 配置

将 `config.example.toml` 复制到 `~/.evermcp/config.toml`：

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

加载顺序：默认值 → `~/.evermcp/config.toml` → 环境变量（`EVERMCP_*`）→ CLI 标志。

## 编写你的第一个工具

查看 [`examples/tools/demo/hello.py`](examples/tools/demo/hello.py) — 最小的工具，12 行代码。

完整规范（子进程工具、异步工具、错误信封、安全模型）请阅读 [`docs/adding-tools.md`](docs/adding-tools.md)。

## 项目结构

```
EverMCP/
├── evermcp/               # 框架
│   ├── core/             # @tool 装饰器、ToolRegistry、ToolContext
│   ├── workers/          # LocalWorker、错误信封
│   ├── protocol/         # Coordinator + MCP stdio 服务器 + HTTP 服务器 + WS 通道 + REST API
│   ├── security/         # SafePath、SafeURL、Config、认证
│   ├── web/              # FastAPI Web UI
│   ├── connect/          # stdio-ws 桥接（evermcp-connect）
│   └── cli.py            # `evermcp serve` / `evermcp list-tools` / `evermcp connect`
├── examples/
│   └── tools/            # 2 个参考工具 — 复制这些开始
│       ├── demo/hello.py
│       └── io/read_file.py
├── docs/
│   ├── adding-tools.md   # 完整工具编写规范
│   ├── DESIGN.md         # 历史设计（已归档）
│   └── reviews/          # S0/S1/S2 审查（已归档）
├── tests/                # unit / worker / registry / e2e / integration / security
├── tools/                # 默认空 — 在此处指向 --tools-dir
├── config.example.toml
├── SECURITY.md           # v0.3.0 安全模型
└── pyproject.toml
```

## CLI

```
evermcp serve [--tools-dir PATH] [--stdio/--no-stdio] [--http/--no-http]
              [--host HOST] [--port PORT] [--ui/--no-ui] [--init-db/--no-init-db]
              # 启动 MCP 服务器（stdio 和/或 HTTP 传输）
evermcp list-tools [--tools-dir PATH]  # 打印已注册工具，退出
evermcp connect --token TOKEN -- GATEWAY_WS_URL SERVER_COMMAND
              # 将本地 MCP 服务器连接到网关
evermcp --help
evermcp --version
evermcp -v serve ...                    # 启用 DEBUG 日志
evermcp -c /path/to/config.toml serve   # 自定义配置文件
```

## 版本

- Python：3.11+（使用 `datetime.UTC`、`tomllib`、PEP 695 泛型）
- EverMCP：见 `pyproject.toml`（`version = "0.3.0"`）

## 许可证

MIT — 见 [`LICENSE`](LICENSE)。
