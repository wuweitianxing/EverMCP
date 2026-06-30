# EverMCP 竞品调研与差异化分析

> 调研日期: 2026-06-30
> 调研方法: GitHub 代码搜索 (`github_text_search`,均带 owner/repo scope,代码级验证)
> 局限声明: 本次调研环境 `fetch_webpage` 不可用,无法抓取 README HTML 与星数。**项目事实**均有代码搜索佐证;**星数/活跃度**为训练知识估算(已标 ⚠️),建议人工复核。配套计划见 `gateway-plan.md`。

---

## 1. 调研覆盖范围

两类开源项目:
- **MCP 网关 / 聚合 / Hub / 多 server 管理**:MetaMCP、MCPPort、mcpproxy-go、MCPJungle、Supergateway 等。
- **低代码工具编排 / 工作流自动化 / AI Agent 平台**:n8n、Langflow、Flowise、Dify、Activepieces,以及官方 MCP Inspector。

---

## 2. MCP 网关/聚合类项目矩阵

| 项目 | 仓库 | 语言 | 对外 transport | 能力来源 | 远程客户端反向发布? | 有 UI? | 持久化 | 鉴权 |
|---|---|---|---|---|---|---|---|---|
| **MetaMCP** | metatool-ai/metamcp | TS+Next.js | 统一端点 | 运营者在 UI **拉取配置**上游 stdio/SSE/StreamableHTTP | ❌ | ✅ 完整 Web UI | 后端服务(DB 未确认) | OAuth + API key |
| **MCPPort** | fangyinc/mcpport | Python | WebSocket/SSE/HTTP | **边缘 stdio server 经出站 WS 主动注册** | ✅(整 server 粒度) | ❌(CLI) | 未确认 | 有(机制未确认) |
| **mcpproxy-go** | smart-mcp-proxy/mcpproxy-go | Go+Vue | 单端点 | 运营者配置上游 | ❌ | ✅ Vue Web UI | BBolt | OAuth + agent token + profile |
| **MCPJungle** | mcpjungle/MCPJungle | Go+React | 统一 `/mcp` | CLI/JSON 配置注册上游 | ❌ | ✅ dashboard | 后端服务 | bearer/OAuth/custom headers |
| **Supergateway** | supercorp-ai/supergateway | TS | stdio↔SSE/WS | 1:1 transport 转换 | ❌ | ❌ | 无(无状态) | 无 |

> 星数/活跃度未本次验证,仅作量级参考。`punkpeye/mcp-gateway`、`lightning-mcp/lightning-mcp` 经核未发现同名仓库。

### 重点项目架构对比

- **MetaMCP(最接近「网关+UI」形态)**:中心化**拉取式**聚合——运营者在 Web UI 配置上游 MCP server,网关用官方 SDK 主动连接,聚合为统一 namespace 端点。支持 namespace 分组、单 server/单工具启停、private/public、注解覆盖(自定义 UI 提示与上游 metadata 合并)。**不做客户端反向发布,无低代码脚本编排。**

- **MCPPort(与 EverMCP 计划最同构,重点关注)**:中心 Gateway + 边缘 stdio-to-ws 适配器,边缘设备经**出站 WebSocket** 主动注册到中心(NAT 穿透)。**这正是 EverMCP 规划的「远程客户端 WS 反向通道」模式**。差异:MCPPort 是薄 transport 网关,注册粒度是「整个 stdio server 进程」,无低代码 UI、无脚本/资源/提示编排、无节点树。

- **mcpproxy-go(实现最成熟,最值得借鉴)**:单进程单端点。核心创新 **BM25 工具检索**(`retrieve_tools`)——不把所有工具塞进客户端上下文,按需检索省 token(目标 1000 工具 <100ms);工具级 quarantine/审批(TPA 防护)、agent token + profile 的 per-client 能力子集、每请求重解析作用域 + 配置热加载、四操作面(REST/MCP/CLI/Web)。这是「工具爆炸 + 多租户隔离」最完整的工程答案。

- **MCPJungle**:统一 `/mcp` 端点,Tool Groups 对不同客户端暴露工具子集,stateless/stateful 会话,cold-start 处理。偏团队级多 server 管理,无低代码编排。

- **Supergateway**:纯 transport 转换器(stdio↔SSE/WS/StreamableHTTP),1:1 透传不聚合,解决「我的 MCP server 只支持 stdio,但客户端要 SSE/WS」接驳问题。

---

## 3. 低代码/编排平台矩阵

| 项目 | 仓库 | 定位 | 可视化编辑器 | 支持 MCP(方式) | 自定义代码执行 |
|---|---|---|---|---|---|
| **n8n** | n8n-io/n8n | 工作流自动化 | ✅ React 节点画布 | ✅ 双向最深:Client 节点 + Trigger(工作流→MCP server)+ Registry + Agent skills | Code 节点(JS,vm) |
| **Langflow** | langflow-ai/langflow | LLM flow builder | ✅ React Flow | ✅ 双向:`MCP Tools`(client)+ `lfx-mcp`(flow→server) | UI 内写 Python component(同进程,弱沙箱) |
| **Flowise** | flowiseai/Flowise | LLM flow builder | ✅ React Flow | ✅ 双向:chatflow 配 MCP server 输出 | 自定义函数(JS,vm) |
| **Dify** | langgenius/dify | LLM 应用平台 | ✅ 自研 React 画布 | ✅ 双向:应用→MCP server + 消费 MCP 工具 | Code 节点(Python/JS,沙箱) |
| **Activepieces** | activepieces/activepieces | 开源 Zapier 替代 | ✅ 流程编辑器 | ✅ 双向:per-project MCP server + AgentMcpTool(client) | pieces(连接器) |
| **MCP Inspector** | modelcontextprotocol/inspector | 官方调试 UI | ✅ 调试面板(非编排) | ✅ 纯 MCP 客户端调试视角 | 无代码编辑 |

### 关键发现

- **主流编排器(Langflow/Flowise)清一色 React Flow(节点-边图模型)**;n8n/Dify 自研 React 画布。数据模型都是「节点 + 连接」的**工作流图**。
- 在编排平台里,**自定义代码几乎总是工作流内的一个节点**,而非「写一段代码 → 立刻成为可被外部 MCP 客户端调用的单条能力」。即便 Langflow 允许 UI 写 Python component,仍需挂进 flow、再经 `lfx-mcp` **整体暴露为 server**,不是以单条 Tool/Resource/Prompt 即时发布。
- MCP Inspector 最贴近「MCP 可视化」,但定位**调试/测试**,无能力树分组/启停/健康徽标,无脚本编辑器,无即时发布。

---

## 4. 三个关键结论

### 结论 1:「WS 反向发布」机制已有同类(MCPPort),不构成独家差异
MCPPort 已实现「边缘 stdio server 经出站 WS 主动注册到中心 + NAT 穿透 + 鉴权」,与 EverMCP 计划的远程客户端反向通道在架构上**同构**。但 MCPPort 是薄 transport 网关:**注册粒度是整个 server 进程**,无低代码 UI、无能力声明编排、无资源/提示发布。

### 结论 2:「MCP 网关 + 低代码脚本编辑器 + 能力节点树可视化」组合无直接竞品
- 纯 MCP 网关(MetaMCP/MCPJungle/mcpproxy-go/MCPPort)**都没有低代码脚本/工作流编辑器**——UI 是「配置上游 server、审批工具、管理 namespace/profile」。
- 低代码平台(n8n/Langflow/Flowise/Dify)**不是 MCP 中心化网关**——MCP 是众多集成之一,且其「网关」角色是「把自身暴露为 MCP server」,可视化对象是**工作流图**而非**能力注册树**。
- **以「MCP 能力(Tool/Resource/Prompt)为第一类对象」的注册中心式可视化 + UI 内写脚本即时发布为单条能力**,在所调研项目中均未见到。

### 结论 3:差异化定位
> **EverMCP = 「以 MCP 能力为一等公民的低代码发布台 + 聚合网关」**

区别于:
- **工作流平台**(n8n/Langflow/Flowise/Dify):以**工作流图**为一等公民,MCP 只是 IO 通道。
- **薄 transport 网关**(MCPPort/Supergateway):无编排,无 UI,粒度粗。
- **调试器**(MCP Inspector):只测不发布。

核心卖点:
1. 远程客户端用 API key 经 WS 反向**声明式发布单个工具/资源/提示**(粒度到单能力,非整 server)。
2. Web UI 低代码编辑脚本与声明,以**能力节点树**(树而非图)可视化聚合后的能力图谱。
3. SQLite 本地持久化,单进程零运维。

---

## 5. 值得借鉴的实现细节

| 借鉴对象 | 借鉴点 | 落到 EverMCP |
|---|---|---|
| **mcpproxy-go** | BM25 `retrieve_tools` 按需检索(应对工具爆炸省 token) | Phase 4 工具数增长后引入;初期可不做 |
| **mcpproxy-go** | 工具级 quarantine/审批(TPA 防护) | Phase 4 远程/内联能力审核流 |
| **mcpproxy-go** | agent token + profile 的 per-client 能力子集 + 每请求重解析 | Phase 2/4 节点树裁剪与多租户 |
| **MetaMCP** | namespace 分组 + 单 server/单工具启停 + private/public | Phase 1/3 节点树与可见性 |
| **MetaMCP** | 注解覆盖(自定义 UI 提示与上游 metadata 合并) | Phase 3 能力展示增强 |
| **MetaMCP** | OAuth + API key 双轨鉴权 | Phase 2 远程客户端鉴权 |
| **MCPPort** | 出站 WS 反向注册 + NAT 穿透连接/心跳/重连 | Phase 2 反向通道直接参考 |
| **n8n** | RFC 8707 resource indicator + protected-resource-metadata + `WWW-Authenticate` 标准化 MCP 鉴权 | Phase 2 HTTP 端点鉴权 |
| **n8n** | MCP registry 表结构 + SSRF 防护 MCP 客户端 URL | Phase 1 持久化 + 复用现有 SafeURL |
| **MCPJungle** | Tool Groups(对不同客户端暴露工具子集)+ stateless/stateful 会话 | Phase 2 多客户端差异化暴露 |
| **MCP Inspector** | tools/resources/prompts 三类分 Tab + 调用测试 + JSON-RPC 错误码分层 | Phase 3 节点树交互/试用调用 |
| **Langflow** | UI 内写 Python component 的编辑 UX + 子进程生命周期清理 | Phase 3 Monaco 编辑器形态 |

---

## 6. 据此对 `gateway-plan.md` 的调整建议

调研基本**验证了原计划方向**,差异化空间真实存在。建议做 3 处调整:

1. **Phase 2 反向通道明确「声明式单能力发布」作为对 MCPPort 的差异**:MCPPort 注册整 server,EverMCP 注册单条 Tool/Resource/Prompt + 在 UI 可编辑。在计划文档中显式标注此差异点,避免被理解为「重做 MCPPort」。

2. **Phase 3 强化「能力节点树」为一等可视化对象**:区别于编排器的「工作流图」,这是核心差异。计划中已提及,建议提升为**卖点首位**(优于「低代码脚本编辑器」),因为后者 Langflow 已有近似形态,而「以能力为节点的注册树」无竞品。

3. **Phase 4 引入 mcpproxy-go 的工具检索/profile 思路作为可选增强**:BM25 检索与 per-client profile 是应对规模化的关键,但初期(能力数 <50)可不做,标记为 Phase 4 可选。同时把「工具审批/quarantine」纳入 Phase 4(远程/内联能力需要审核闸门)。

其余(Provider 抽象、双 transport、SQLite、API key 鉴权、Monaco 沙箱)均与竞品实践一致,维持原计划。

---

## 7. 数据来源

### 7.1 GitHub 代码搜索(均带 owner/repo scope,有命中)
- `metatool-ai/metamcp` — namespace 聚合、上游 transport、注解覆盖、Docker/K8s、OAuth
- `fangyinc/mcpport` — 架构图:中心 Gateway + 边缘 stdio-to-ws 适配器、出站 WS 注册
- `smart-mcp-proxy/mcpproxy-go` — BM25 retrieve_tools、quarantine、profile、4 操作面
- `mcpjungle/MCPJungle` — 统一端点、Tool Groups、stateful/bearer/Web dashboard
- `supercorp-ai/supergateway` — 双向 transport 转换矩阵
- `n8n-io/n8n` — McpTrigger/McpClientTool/McpRegistryClientTool/availableInMCP/mcp-registry/agent-builder/mcp-browser
- `langflow-ai/langflow` — LFX_MCP.md、api/v2/mcp.py、mcp_cleanup.py、MCP Tools 组件
- `flowiseai/Flowise` — ChatflowConfigurationDialog 的 mcpServer 配置
- `langgenius/dify` — api/controllers/mcp/mcp.py、api/core/mcp/types.py
- `activepieces/activepieces` — .agents/features/mcp.md、AgentMcpTool
- `modelcontextprotocol/inspector` — README(调试定位)、mcpProxy.ts、AGENTS.md(V2 进行中)
- `modelcontextprotocol/python-sdk` — client/session_group.py、streamable_http.py、auth.py(客户端侧聚合,方向与 EverMCP 相反)

### 7.2 未本次验证(建议人工复核)
- 各仓库当前星数与精确活跃度。
- MetaMCP 持久化 DB 类型、MCPPort 鉴权具体机制。
- Lobe Chat / Node-RED 的 MCP 支持现状(主仓库搜索无命中)。
- 开放式发现:在 GitHub 网页搜 `"mcp gateway" ui`、`"mcp registry" dashboard`、`"mcp studio"`、`low-code mcp`,以发现 `github_text_search`(需指定 owner/repo)无法覆盖的小众新项目。
