# EverMCP Security Model

**v0.2.0 范围**: EverMCP **不 ship 任何工具**——本文档描述**框架**的安全机制（SafePath、SafeURL、错误信封、ToolContext 注入），以及**写新工具**时必须遵守的检查清单。v2 跨设备调度会带来新风险（mTLS、设备鉴权），届时另文描述。

## 信任边界

| 边界 | 信任等级 | 说明 |
|---|---|---|
| **AI 客户端 → 协调器 (MCP)** | **不可信** | AI 的工具调用按用户输入处理，不假设是安全的 |
| **协调器 → Worker** | **可信** | v1 同进程（in-process function call，无网络），v2 需 mTLS |
| **Worker → Tool** | **可信** | 工具代码由开发者审计 |
| **Tool → 系统** | **受限** | 受 SafePath / SafeURL 约束 |

**核心原则**: AI 客户端不可信，工具作者写的是受信代码但被不可信输入调用，所以所有接触外部资源的工具函数必须过安全 helper。

## v1 安全机制

### 1. Subprocess argv 注入防御

**所有 spawn subprocess 的工具必须用结构化参数构建 argv**：

```python
# ✅ 正确
subprocess.run(["ffmpeg", "-i", input_path, output_path], check=True)

# ❌ 错误
subprocess.run(f"ffmpeg -i {input_path} {output_path}", shell=True)  # shell injection!
```

约束：
- **绝不**用 `shell=True`
- **绝不**用字符串拼接/f-string 构建命令行
- 路径作为单个 argv 元素传入，由 OS 解释
- 工具启动 FFmpeg 之前必须 `ctx.safe_path.validate(input_path)`（如果配置了 filesystem_allowlist）

实装：`tools/media/transcode.py:200-235` — FFmpeg 调用纯 argv 列表。

### 2. 文件系统: SafePath

**任何触文件的工具必须用 `evermcp.security.safepath.SafePath`**：

```python
from evermcp.security.safepath import SafePath, SecurityViolation

# Coordinator 自动构建并注入 ctx.safe_path
def read_file(path: str, ctx: ToolContext) -> dict:
    safe = ctx.safe_path.validate(path)  # raise SecurityViolation if not allowed
    return {"content": safe.read_text(...)}
```

行为：
- **denied list 先检查**（更高优先级）— 防 `~/.ssh`、`~/.aws` 等
- 路径 `expanduser() + resolve()` 后检查（消除 `..` 遍历、symlink 逃逸）
- 不在 allowlist → `SecurityViolation` → 工具异常 → `TOOL_EXCEPTION` 错误信封

配置：
```toml
[security]
filesystem_allowlist = ["~/data", "~/Downloads"]
denied_paths = ["~/.ssh", "~/.aws", "~/.config/gh"]
```

实装：`evermcp/security/safepath.py` + `tools/io/read_file.py`。

### 3. 网络: SafeURL (SSRF 防御)

**任何触网络的工具必须用 `evermcp.security.safeurl.SafeURL`**：

```python
from evermcp.security.safeurl import SafeURL

# Coordinator 自动构建并注入 ctx.safe_url
def get(url: str, ctx: ToolContext) -> dict:
    scheme, host = ctx.safe_url.validate(url)  # raise SecurityViolation if not allowed
    return httpx.get(url, ...)
```

**默认拒绝**（无 allowlist 时也生效）：
- scheme 不是 `http`/`https` → 拒绝
- hostname 是 localhost 类名 (`localhost`, `localhost.localdomain`, `ip6-localhost`, `ip6-loopback`) → 拒绝
- hostname 是字面 IP 且满足 `is_private`/`is_loopback`/`is_link_local`/`is_reserved`/`is_multicast`/`is_unspecified` → 拒绝

**Allowlist 模式**（config 配了 `network_allowlist` 时启用）：
- hostname 必须精确匹配 OR 是子域名
- 例: `github.com` 在 allowlist → `github.com` 和 `api.github.com` 都过
- 例: `evil-github.com` **不**匹配 `github.com`（后缀不算子域）

配置：
```toml
[security]
network_allowlist = ["github.com", "pypi.org"]  # 空 = 仅默认拒绝
```

实装：`evermcp/security/safeurl.py` + 示例见 `examples/tools/io/read_file.py`（FS 工具的 SafePath 用法）。

### 4. v1 SSRF 已知限制（DNS rebinding）

**当前实现只检查 URL 字面 hostname，不解析后检查 IP**。

攻击场景：
```
http://attacker.com/  →  首次 DNS 解析 → 1.2.3.4 (安全)
                   →  工具内部 httpx 重新解析 → 127.0.0.1 (恶意)
```

**v1 接受这个风险**（DESIGN.md §Reviewer Concerns 提到）。v2 强化方向：在 `SafeURL.validate()` 里先解析 IP，把解析结果也跑一次 default-deny。

### 5. 二进制发现（外部依赖）

工具如果需要外部二进制（FFmpeg, ImageMagick, git, …），遵守：

- 默认 `shutil.which("<binary>")`（PATH 里找）
- 配置文件可覆盖：`[general] <binary>_path = "/path/to/binary"`（v1.0 shipped config 只暴露 `ffmpeg_binary`）
- 找不到 → 工具启动时**大声报错**（不静默 fallback 到"看似工作的"二进制）
- **绝不**从 AI 输入拿二进制路径（命令注入风险）

### 6. 错误信封（错误码）

工具失败时，LocalWorker 把异常包装成 JSON-RPC 错误码返回给 AI。`call_tool` 用 3 层 except 分别处理：

| 错误码 | 含义 | 何时触发 |
|---|---|---|
| `-32001` | TOOL_NOT_FOUND | AI 调了不存在的工具 |
| `-32002` | TOOL_TIMEOUT | 工具 raise `RuntimeError` 且 message 含 "timeout"（如 subprocess 被 SIGTERM kill） |
| `-32003` | TOOL_EXCEPTION | 工具 raise 其他 `Exception`（非 SecurityViolation、非 timeout RuntimeError） |
| `-32004` | TOOL_INVALID_OUTPUT | 工具返回了非 JSON 可序列化的值 |
| `-32005` | SECURITY_VIOLATION | 工具 raise `SecurityViolation`（SafePath / SafeURL 拒绝） |

### 7. ToolContext 注入

Coordinator 给每个工具调用构造 `ToolContext`，里面装好：
- `safe_path`: `SafePath` 实例（如果 config 配了 filesystem_allowlist）
- `safe_url`: `SafeURL` 实例（总是有 — 默认拒绝也用它）
- `config`: `Config` 实例（如果 Coordinator 有）
- `logger`: 日志 logger

工具作者应该 `ctx.safe_path.validate(...)` 而不是自己新建 SafePath — 让配置真正生效。

## v1 **不**做的事

| 不做 | 原因 |
|---|---|
| AI 客户端 ↔ 协调器鉴权 | stdio MCP 假设本地用户，进程隔离是边界 |
| 工具沙箱隔离 | 依赖 OS 用户权限；v1 工具代码受信 |
| Worker 间 mTLS | v1 是单进程，无网络 |
| DNS rebinding 防护 | 见 §4，v1 接受风险 |
| 工具返回值的 schema 验证 | v1 信任工具作者写对返回 dict；v2 加 pydantic model |
| Rate limiting | stdio MCP 下 AI 客户端重试受其自身控制 |
| 审计日志 | 当前只有 stderr JSON log；v2 加结构化 audit trail |

## 加新工具时的安全检查清单

写一个新 `@tool` 函数前，过一遍：

- [ ] 工具签名里有 `ctx: ToolContext = None`？
- [ ] 触文件系统 → 用 `ctx.safe_path.validate(...)`（不直接用路径）？
- [ ] 触网络 → 用 `ctx.safe_url.validate(...)`（不直接 urlparse）？
- [ ] spawn subprocess → argv 列表，**无** `shell=True`，**无**字符串拼接？
- [ ] 二进制路径 → 从 `ctx.config.ffmpeg_binary`（或类似）拿，不从用户输入拿？
- [ ] 异常 → 让它 raise（不静默吞掉）；LocalWorker 包装成错误信封
- [ ] 返回值 → dict/list/str/int/float/bool/None 之一（JSON 可序列化）
- [ ] 写一个对应的单元测试覆盖 happy path + 至少一个 security rejection 路径

## 报告安全问题

发现 v1 安全问题请开 GitHub issue（或发邮件给 maintainer），标 `security` label。v1 阶段我们没有正式的 coordinated disclosure 流程。
