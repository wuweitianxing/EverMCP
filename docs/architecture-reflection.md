# EverMCP 工程架构反思:独创 vs 复用 vs 砍除

> 日期: 2026-06-30
> 依据: `competitive-analysis.md`(竞品) + 协议/可嵌入性核实(见附录)
> 目的: 从使用者角度,基于成熟开源项目反思 `gateway-plan.md`,明确哪些独创值得保留、哪些设计不必要、哪些应直接复用轮子。

---

## 0. 核心结论先行

EverMCP 的工程独创性**集中在「以 MCP 能力为节点的可视化治理」**,不在「网关传输」或「脚本执行」。因此:

- **传输/聚合层 → 最大化复用**:官方 `mcp` SDK 的 `ClientSessionGroup` + MCPPort 的 WS 反向思路。
- **治理/可视化层 → 自建**:能力节点树 + 多源治理 UI,这是差异所在。
- **脚本执行层 → 降级可选**:本地 IDE + 文件热加载已是极佳开发体验,浏览器内联脚本是非必要增强。

这样自建代码量大幅收敛,聚焦真正差异化的部分。

---

## 1. 协议核实结论(决策前提)

| 项目 | 协议 | 可商用嵌入? | 对 EverMCP 的意义 |
|---|---|---|---|
| 官方 `modelcontextprotocol/python-sdk` | MIT | ✅ | **核心依赖**,已内置 `ClientSessionGroup` 多 server 聚合 |
| `fangyinc/mcpport` | MIT | ✅ | Python,WS 反向注册通道可移植改造 |
| `metatool-ai/metamcp` | MIT | ✅ | TS,聚合/namespace 设计可参考,不可直接嵌(Python) |
| `smart-mcp-proxy/mcpproxy-go` | MIT | ✅ | Go,BM25 检索思路可借鉴,代码不可嵌 |
| `langflow-ai/langflow` | MIT | ✅ | 画布基于 React Flow(MIT),直接用底层库即可 |
| `flowiseai/Flowise` | Apache-2.0(主) | ✅ | 同上 |
| `activepieces` (core) | MIT | ✅ | core 可参考 |
| `modelcontextprotocol/inspector` | MIT | ✅ | TS+React 独立应用,UI 可参考/iframe |
| `mcpjungle/MCPJungle` | MPL-2.0 | ⚠️ 文件级 copyleft | Go,仅思路参考 |
| `n8n-io/n8n` | **Sustainable Use** | ❌ | **不得嵌入商用**,仅设计参考 |
| `langgenius/dify` | **Modified Apache** | ⚠️ 受限 | **商用嵌入风险高**,仅设计参考 |
| React Flow / Vue Flow / Monaco | MIT | ✅ | UI 组件直接采用 |
| RestrictedPython | ZPL-2.1(类 BSD) | ✅ | 沙箱候选(若做脚本) |

**重大发现**:官方 `python-sdk` 的 `src/mcp/client/session_group.py` 已提供 `ClientSessionGroup`——「管理到多个 MCP server 的连接,聚合 tools/resources/prompts」。这意味着**多 transport 聚合层不必自建**。

---

## 2. 从三类使用者角度看独创性

| 使用者 | 关心什么 | EverMCP 独创 | 已有方案能否满足 |
|---|---|---|---|
| **Agent 调用方** | 能力多、好发现、稳定 | 能力节点树可视化(看清网关有什么) | MCP Inspector 能列,但无治理树 |
| **能力提供者(客户端/设备)** | 发布简单、不大改代码 | (计划)声明式单能力发布 | **MCPPort 整 server 反向注册已能满足** |
| **低代码编排者(非开发者)** | 不配环境、所见即所得 | 以能力为节点的注册树 | Langflow/Flowise 是工作流图,非能力树 |

**关键反思**:对「能力提供者」,MCPPort 的「整 server 反向注册」**比 EverMCP 计划的「单能力声明式发布」更友好**——客户端不用改代码,把现有 MCP server 暴露即可。单能力发布是更精细的控制,但增加客户端 SDK 复杂度,**对多数使用者是过度设计**。

---

## 3. 逐项审视 `gateway-plan.md` 的设计

### ✅ 真独创,值得保留(自建)

| 设计 | 为什么独创 | 处置 |
|---|---|---|
| **能力节点树可视化**(按来源/类别分组 + 搜索 + 启停 + 健康徽标) | 所调研项目无同类(MCP Inspector 是平铺列表,编排器是工作流图)。这是 EverMCP 最强差异。 | **提升为卖点首位**,Phase 3 核心 |
| **多源能力聚合 + 治理**(本地文件 / 远程客户端 / 内联脚本统一管理 + 启停/版本/可见性) | MetaMCP 是单源拉取,EverMCP 是多源含反向 + 治理。 | 自建 `CapabilityRegistry`,但**聚合传输用官方 session_group** |
| **文件即工具 + 热加载 + 零样板**(v0.2.0 已有) | 比 Langflow 的自定义 component 更轻(文件路径即命名空间,Pydantic Field 自动推 schema)。 | 保留,作为 LocalFilesystemProvider |

### 🔄 应复用轮子,不要自建

| 设计 | 原计划 | 反思后 | 理由 |
|---|---|---|---|
| **多 transport 聚合层** | 自建 CapabilityRegistry 聚合 | **基于官方 `ClientSessionGroup`** | 官方 SDK 已内置聚合 tools/resources/prompts,MIT。自建是重复造轮子 |
| **MCP stdio/HTTP server** | 自建(已用官方 SDK) | 继续用官方 SDK | 已对,维持 |
| **WS 反向通道** | 自建协议 | **移植/参考 MCPPort**(MIT,Python) | MCPPort 已实现出站 WS + Bearer + NAT 穿透,解耦隧道与路由后可复用 |
| **节点编辑器前端** | Vue3 + Element Plus Tree | 维持(树用 Tree 组件,不必引入 React Flow) | 「树」比「图」简单,Element Plus Tree 足够。若未来升级到编排图再用 Vue Flow |
| **Monaco 编辑器** | 自建集成 | 直接用(MIT) | 已对 |

### ❌ 不必要 / 应降级 / 应砍除

| 设计 | 原计划位置 | 反思 | 处置 |
|---|---|---|---|
| **单能力声明式发布**(客户端 SDK 注册单条 Tool/Resource/Prompt) | Phase 2 核心 | 对能力提供者,整 server 注册(MCPPort 式)更友好、更简单。单能力发布增加 SDK 复杂度,多数使用者用不到。 | **Phase 2 改为整 server 反向注册**(直接复用 MCPPort 思路);单能力发布延后至 Phase 3 由 UI 提供(UI 内联能力本就是单能力发布) |
| **Monaco 内联脚本编辑器 + 沙箱** | Phase 3 核心 | 本地 IDE(VS Code)+ 文件热加载体验远好于浏览器 Monaco。内联脚本只对「非开发者/快速试错」有价值,是增强非核心。沙箱还是高风险高成本项。 | **降级为 Phase 3.5 可选**;Phase 3 核心 UI = 节点树 + 表单式声明编辑器 + 管理面板。脚本编辑器作为增强,若做用 RestrictedPython |
| **持久化远程能力**(远程能力落 DB) | Phase 2 | 若 Phase 2 用整 server 反向注册,远程能力「在线即注册、离线即消失」,只需持久化客户端注册信息 + API key,不必持久化能力本身。 | **Phase 2 只持久化 clients + api_keys**,远程能力在内存;降低 DB 复杂度 |
| **BM25 工具检索** | Phase 4 | 初期能力数 <50 无需检索。mcpproxy-go 是 Go 不可嵌,只能 Python 重写。 | **Phase 4 可选**,初期不做;若做用 `rank_bm25` 库 + 借鉴 `retrieve_tools` 元工具思路 |
| **gRPC worker 序列化层**(DESIGN v2 项) | DESIGN v2 | 反向通道用 WS 已满足远程能力调用,不必引入 gRPC。 | **砍除**,WS 反向通道已覆盖 v2 远程调度需求 |
| **Resource/Prompt 的文件扫描** | Phase 1 泛化 | v0.2.0 只有 Tool 文件扫描。Resource/Prompt 多数来自 UI 声明或远程,本地文件扫描对它们价值低。 | Phase 1 只泛化数据模型 + MCP handler,**本地文件扫描仍只针对 Tool**;Resource/Prompt 走 UI/远程 |

---

## 4. 修订后的工程边界

```
自建(独创,值得造)            复用(直接用轮子)            砍除/降级
─────────────────────         ──────────────────         ─────────────────
能力节点树 UI                  官方 mcp SDK(transport      gRPC worker 层
多源 CapabilityRegistry          + session_group 聚合)     单能力发布 SDK(初期)
  + 治理(启停/版本/可见性)    MCPPort WS 反向思路         Monaco 脚本编辑器(降级)
LocalFilesystemProvider       Vue3 + Element Plus         持久化远程能力(降级)
  (v0.2.0 已有)               Monaco(MIT)                BM25 检索(Phase 4 可选)
表单式声明编辑器              RestrictedPython(若做沙箱)
管理面板(客户端/key/日志)
```

**自建代码量预估**:从原计划「聚合层 + 反向协议 + 沙箱 + 全套 UI」收敛到「多源治理 Registry + 能力节点树 UI + 表单声明 + 管理面板」。复用官方 SDK 做传输聚合、复用 MCPPort 思路做反向通道。

---

## 5. 修订后的分阶段计划(对 `gateway-plan.md` 的调整)

### Phase 1 — 能力模型泛化 + HTTP transport(网关骨架)[维持,微调]
- 泛化 `Tool → Capability(Tool/Resource/Prompt)` 数据模型 + MCP handler。
- **本地文件扫描仍只针对 Tool**(Resource/Prompt 不做文件扫描)。
- HTTP transport 用官方 SDK。
- **不自建聚合层**,Coordinator 直接用官方 `ClientSessionGroup` 聚合多个 Provider。
- SQLite 基座:仅 `capabilities`(本地 Tool 元数据缓存)+ 预留表。

### Phase 2 — 客户端整 server 反向注册(改为 MCPPort 式)[简化]
- **不做单能力声明式发布 SDK**;改为:远程客户端把现有 MCP server(stdio)经出站 WS 反向注册到网关。
- 移植/参考 MCPPort 的 WS 隧道 + Bearer 鉴权 + NAT 穿透,解耦隧道与 MCP 路由。
- RemoteClientProvider =「整 server 反向注册的客户端」在网关侧的 Provider。
- 持久化:仅 `clients` + `api_keys`,远程能力在内存(在线即注册)。
- 验收:客户端启动现有 MCP server + 反向连接,Agent 经网关 MCP 调用到该 server 的工具。

### Phase 3 — 能力节点树 UI + 表单声明[核心差异][重新定位]
- **核心**:能力节点树(按来源/类别分组 + 搜索 + 启停 + 健康徽标)+ 表单式声明编辑器(声明 Tool/Resource/Prompt 的 name/desc/schema)+ 管理面板(客户端/key/调用日志)。
- 表单声明的能力存 DB,作为 InlineDeclarationProvider(无脚本,纯声明,无沙箱风险)。
- 本地 token 鉴权,默认 `127.0.0.1`。
- **这是 EverMCP 的核心卖点**:以能力为节点的注册中心式可视化,无竞品。

### Phase 3.5 — Monaco 内联脚本编辑器(可选增强)[降级]
- 仅当 Phase 3 验证有需求时做。
- Monaco + RestrictedPython 沙箱,单能力即时发布。
- 风险:沙箱安全;收益:非开发者快速试错。

### Phase 4 — 规模化与打磨[精简]
- 多 Provider 健康度路由(轮询 + 健康过滤)。
- 能力生命周期:版本/启停/可见性(借鉴 MetaMCP namespace + mcpproxy-go profile)。
- **可选**:BM25 工具检索(借 mcpproxy-go 思路,`rank_bm25` 库)。
- **可选**:远程/内联能力审核流(借 mcpproxy-go quarantine)。
- 可观测性:调用日志持久化 + UI 查询。
- 安全强化:`SafeURL` DNS 解析后复检(闭合 DESIGN 已知 SSRF 限制)。

---

## 6. 对原 DESIGN.md 的两点修正建议

1. **DESIGN v2「gRPC worker 序列化层」**:网关化后远程能力走 WS 反向通道已满足,建议**从路线图移除 gRPC**,改为「WS 反向通道 + 官方 SDK transport」。
2. **DESIGN P5「Agent 自进化」**:原愿景是 Agent 运行时写工具。反思后,EverMCP 提供**人工低代码通道**(UI 声明/脚本),Agent 自进化仍为 v3+ 愿景不实现,但 UI 已部分满足「快速新增能力」的需求。

---

## 附录:协议核实数据来源

(完整查询清单见 `competitive-analysis.md` §7,此处仅列结论性证据)
- 官方 `python-sdk`:`pyproject.toml` classifier `MIT`,且 `src/mcp/client/session_group.py` 有 `ClientSessionGroup`(聚合多 server 的 tools/resources/prompts)。
- MCPPort:`LICENSE` + `pyproject.toml: license="MIT"`,WS 实现在 `src/mcpport/{client,gateway,types}.py`。
- n8n:多个 `package.json` 显式 `LicenseRef-n8n-sustainable-use` + `LICENSE_EE.md`,**不可商用嵌入**。
- Dify:README「Dify Open Source License...Apache 2.0 with additional conditions」,**商用受限**。
- Langflow/Flowise:画布基于 `@xyflow/react`(React Flow,MIT),非可嵌入组件,改用底层库。
