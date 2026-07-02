# S2 代码审查意见(已归档)

> 审查范围:S2 全部改动 — 6 个已跟踪文件 + 4 个新增文件(共 ~1180 新增行)
> 生成时间:2026-07-01
> 状态:**已审、已修、已归档** — 全部 14 条意见(P0×1 + P1×3 + P2×5 + P3×5)已核实并落地修复
> 修复方式:3 个子代理串行修复(按 P0→P1→P2/P3 分批,文件无重叠)
> 修复时间:2026-07-01
> 验证:263 passed, 9 warnings(基线一致,无回归);本次改动的 8 个文件 ruff 全部通过(既有 42 个 lint 错误属历史遗留,与本次无关)
> 归档位置:docs/reviews/s2-code-review.md

---

## P0 — 严重(阻断 S2 验收)

### 1. Admin REST 端点完全未接入 API key 认证 ✅(子代理 A 修复)
`evermcp/protocol/rest_api.py`(全部 handler)+ 挂载点 `evermcp/web/app.py:134-144`

模块 docstring 明确声明「All endpoints require an API key with the `admin` scope」,但 `get_clients`/`create_client_endpoint`/`delete_client_endpoint`/`get_keys`/`create_key`/`revoke_key`/`delete_key`/`get_logs` **没有任何一个**使用 `Depends(require_api_key_http)`。它们经 `app.include_router(admin_router)` 裸挂载,无认证依赖。`evermcp/security/auth.py` 的 `require_api_key_http`/`require_api_key_ws`/`api_key_has_scope` 及 `_bearer` 是死代码(零引用)。

**修复**:app.py 挂载改为 `app.include_router(admin_router, dependencies=[Depends(require_api_key_http)])`,8 个 admin 端点统一要求 admin scope 的 API key;删除 auth.py 无用的 `_bearer = HTTPBearer(...)`;适配测试(新增 `admin_api_key` fixture,请求带 `Authorization: Bearer`)。

---

## P1 — 高

### 2. `websockets` 依赖未写入 `pyproject.toml` ✅(子代理 A 修复)
`evermcp/connect/stdio_ws_bridge.py:16` `import websockets`,但 `pyproject.toml` 依赖列表无 `websockets`。全新 `pip install evermcp` 后 `evermcp-connect` 启动即 `ModuleNotFoundError`。S2 计划要求新增 `websockets>=12.0`。
**修复**:dependencies 列表新增 `"websockets>=12.0",`。

### 3. `evermcp-connect` 桥在本地 MCP server 退出后挂死 ✅(子代理 A 修复)
`evermcp/connect/stdio_ws_bridge.py:139-145` `asyncio.gather(...)` 等待全部任务。子进程退出时 `_relay_stdin_to_ws`/`_log_stderr` 返回,但 `_relay_ws_to_stdin` 阻塞在 `async for message in websocket`,网关不主动关 WS,任务永不返回,`finally` 清理(关 WS、terminate 子进程)永远不执行,桥进程挂死。
**修复**:改用 `asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)`,任一 relay 完成即取消其余 pending 任务并 await(吞 CancelledError),保留原有 finally 清理。

### 4. 承诺的 WS 心跳未实现 ✅(子代理 B 修复)
`evermcp/protocol/ws_channel.py:23-24`(docstring 声称「ping/pong heartbeats 30s」)+ `ws_channel.py:128-130`(「Heartbeat helper」段落标题后为空)。没有任何 ping/pong 代码。经反代/LB 的空闲连接会被静默切断,网关要等到一次调用 60s 超时才发现半开连接。
**修复**:填充心跳实现(`HEARTBEAT_INTERVAL_S=30.0`、`HEARTBEAT_POLL_S=1.0`),驻留循环每 30s 调用 `update_client_last_seen` 刷新存活;Starlette WebSocket 无公开 `send_ping` API,协议级 ping/pong 委托底层 uvicorn/websockets transport,应用层以 last_seen 刷新作存活标记。

---

## P2 — 中

### 5. 远程工具 `isError` 被丢弃,错误调用被报为成功 ✅(子代理 B 修复)
`evermcp/core/provider.py:688-691`(call 返回 `_mcp_call_tool_result_to_json(result)`)+ `provider.py:707-727`(该函数只取 `content`,忽略 `isError`)。远程 MCP 工具返回 `isError: true` 时,网关将其 text content 包成 `{"success": True, "result": ...}`,Agent 侧看不到错误信号。
**修复**:新增 `RemoteToolError(Exception)`;`RemoteClientProvider.call` 检测 `result.isError`,为 True 时抛 `RemoteToolError`(含 content 文本),让 coordinator 走 `TOOL_EXCEPTION`(-32003)分支返回 `success=False`。用专用异常类(而非 `RuntimeError`)以规避 `_classify_exception` 对含 "timeout" 字样的误判。

### 6. `list_call_logs` 用「全量物化再 len」计数 ✅(子代理 C 修复)
`evermcp/storage.py:444` `total = len(list(session.exec(count_statement).all()))` —— 把所有匹配行载入内存只为计数。审计日志无界增长后是内存/性能隐患。
**修复**:改用 `select(func.count()).select_from(CallLog)` + `session.scalar(count_statement) or 0`,where 条件同步应用到 count 与查询语句。

### 7. `CallLog` 表无索引、无保留策略 ✅(子代理 C 修复)
`evermcp/storage.py:84-92` `name`/`source`/`success`/`started_at` 都被过滤/排序,但模型字段无 `index=True`,查询走全表扫描;且无任何 TTL/上限裁剪,日志无限增长。
**修复**:`name`/`source`/`started_at` 加 `index=True`;新增 `prune_call_logs(keep=10000)` 辅助函数(保留最新 keep 条,删除其余,返回删除数)。注:生产 DB 已有表需手动迁移(`create_all` 不改已有表结构)。

### 8. `delete_client` 有一行冗余/死查询 ✅(子代理 C 修复)
`evermcp/storage.py:285-286` 第一行 `session.exec(select(ApiKey).where(...))` 执行 SELECT 却丢弃结果,紧接着第二行重复同一查询。
**修复**:删除第一行冗余 SELECT。

### 9. 调用日志在 async 热路径上做同步阻塞 DB 写 ✅(子代理 C 修复)
`evermcp/protocol/coordinator.py:298-322`(`_log_call` → `create_call_log`)每次 `call_tool_async` 都在事件循环内直接做同步 SQLite 写,未包 `asyncio.to_thread`。并发调用下会阻塞 loop。
**修复**:`_log_call` 改为 async,内部 `await asyncio.to_thread(storage.create_call_log, ...)`,6 处调用点加 `await`。混合策略:SQLite `:memory:` 在事件循环线程同步写(避免 SingletonThreadPool 跨线程空库问题,保证测试可见),文件型/生产走 `to_thread`。

---

## P3 — 低

### 10. WS token 进 URL 且未编码 ✅(子代理 A 修复)
`evermcp/connect/stdio_ws_bridge.py:111` `uri = f"{gateway_url}?token={token}"` —— API key 落入网关访问日志。
**修复**:改用 `websockets.connect(gateway_url, additional_headers={"X-EverMCP-Key": token})`;ws_channel.py 的 `_ws_endpoint` 补充从 `X-EverMCP-Key` header 读 token(query param 保留向后兼容)。

### 11. `last_seen_at` 仅在注册时更新一次 ✅(子代理 B 修复)
`evermcp/protocol/ws_channel.py:185` `update_client_last_seen` 只在 `session.initialize()` 后调用一次,之后再无更新。`last_seen_at` 永远等于连接建立时间。
**修复**:在心跳循环里每 30s 调用 `update_client_last_seen(client_id)`,字段语义与名字一致。

### 12. `handle_websocket` 的 `remote_call_timeout_s` 形参是死参数 ✅(子代理 B 修复)
`evermcp/protocol/ws_channel.py:163` 接收但从未转发(超时实际在 Coordinator 经 config 生效)。
**修复**:删除该形参及默认值,删除模块级 `DEFAULT_REMOTE_CALL_TIMEOUT_S` 常量(grep 确认仅此文件引用)。

### 13. 同步 `call_tool` 与 async 路径行为不一致 ✅(子代理 C 修复)
`evermcp/protocol/coordinator.py:177` 同步版既无 `CallLog` 持久化也无远程超时。grep 确认生产代码零调用方(仅 e2e 测试测试 shim 本身)。
**修复**:docstring 标注 `.. deprecated:: S2`,方法体顶部加 `logger.warning` 运行时提示,明确说明该方法不持久化 CallLog、不应用远程超时、应改用 `call_tool_async`。未强行补日志(同步无法 await async `_log_call`,且 e2e 测试未初始化存储)。

### 14. `_RemoteToolCapability.call` 抛 `NotImplementedError` 但语义不明 ✅(子代理 B 修复)
`evermcp/core/provider.py:657-660`。确认 `registry.call` 走 `provider.call` 而非 `cap.call`,故该方法永不被调用。
**修复**:完善 docstring 与 message,明确说明「registry 经 provider 级路由,此方法仅为满足 Capability 协议占位,永不被触达,raise 仅作防护」。

---

## 修复执行小结

| 批次 | 子代理 | 负责问题 | 改动文件 | 测试结果 |
|------|--------|----------|----------|----------|
| A | 认证+依赖+桥 | P0#1, P1#2#3, P3#10 | rest_api.py, auth.py, app.py, pyproject.toml, stdio_ws_bridge.py, ws_channel.py(_ws_endpoint), test_ws_channel.py | 263 passed |
| B | WS 心跳+远程语义 | P1#4, P2#5, P3#11#12#14 | ws_channel.py, provider.py | 263 passed |
| C | 存储+日志+协调器 | P2#6#7#8#9, P3#13 | storage.py, coordinator.py | 263 passed |

三个子代理串行执行,文件无重叠,各自独立验证 263 passed 无回归。最终全量回归:263 passed, 9 warnings;本次改动 8 文件 ruff 全部通过。
