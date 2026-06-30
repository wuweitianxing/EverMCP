# S1 代码审查意见(已归档)

> 审查范围:S1 全部改动 — 4 个已跟踪文件 + 5 个新增文件
> 生成时间:2026-07-01
> 状态:**已审、已修、已归档** — 全部 11 条意见(P0×2 + P1×3 + P2×4 + P3×3,含额外发现的 UTF-8 编码 bug)已核实并落地修复
> 修复时间:2026-07-01
> 验证:255 passed(基线一致,无回归);`evermcp serve --http --ui` 端到端实测通过(进程不崩、UI 端口监听、token 中间件 401 生效)
> 归档位置:docs/reviews/s1-code-review.md

---

## P0 — 阻塞性问题(必须先修,S1 功能不可用)

### 1. `--ui` 启动必崩:`uvicorn.run` 在 async 上下文里调用

**位置**:`evermcp/cli.py:201-216`(`_ui()` 协程)

```python
async def _ui() -> None:
    ...
    uvicorn.run(web_app, host=bind_host, port=bind_port + 1, ...)  # 同步入口
```

`uvicorn.run` 是**同步阻塞**函数,内部调用 `asyncio.run(server.serve())`。而 `_ui` 是 async 协程,在 `_run()` 里通过 `asyncio.create_task(_ui())` 调度,此时事件循环已在运行。

`--ui` 强制要求 `--http`(cli.py 有 `if ui and not http: raise`),所以启用 `--ui` 时 `tasks` 列表至少含 `http` + `ui` 两个任务,走 `asyncio.gather(*tasks)`(`cli.py:241`)→ `_ui()` 内 `uvicorn.run` 触发:

```
RuntimeError: asyncio.run() cannot be called from a running event loop
```

**影响**:任何 `evermcp serve --http --ui` 调用都会崩溃,Web UI 实际无法启动。S1 的核心交付物不可用。

**建议**:与 S0 的 `HTTPServer.run()` 保持一致,用 `uvicorn.Config` + `uvicorn.Server` + `await server.serve()`:

```python
async def _ui() -> None:
    from evermcp.web.app import create_app
    import uvicorn
    web_app = create_app(coordinator)
    config = uvicorn.Config(web_app, host=bind_host, port=bind_port + 1, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
```

---

### 2. 测试调用功能(`POST /api/test`)对所有能力都会失败

**位置**:`evermcp/web/rest.py:247-268`(`test_call`)

三个分支都有问题:

**(a) tool 路径 — name 带前缀,registry 匹配不到**

`/api/tree` 返回的 `name` 是 `local:io.read_file` / `inline:xxx`(rest.py:99-103 拼了 `local:`/`inline:` 前缀)。前端 `testCall()` 把这个带前缀的 name 原样回传(index.html `testCall`)。`test_call` 直接 `coord.call_tool_async(name, args)` → `registry.call("local:io.read_file")` → `LocalFilesystemProvider.get("local:io.read_file")` → `None`(实际 key 是 `io.read_file`)→ `KeyError` → `TOOL_NOT_FOUND`。

**(b) resource 路径 — 传了 name,但 `read_resource` 要的是 URI**

`coord.read_resource(uri)` 按 `desc.get("uri") == uri` 匹配(registry.py `read_resource`)。前端传的 `name` 是 `local:xxx`/`inline:xxx`,根本不是 URI(如 `evermcp://about`)→ 永远 `KeyError`。

**(c) prompt 路径 — 同样带前缀,registry 里 prompt name 不带前缀**

**(d) resource 返回 tuple 不可 JSON 序列化**

即便匹配到,`coord.read_resource` 返回 `tuple[Any, str]`(content, mime),`rest.py:258` 直接 `return {"success": True, "result": result}` —— tuple 无法 JSON 序列化,FastAPI 抛 500。

**影响**:UI 上点"测试调用"对**任何**能力都返回失败/500。测试 `test_api_test_call_endpoint` 只测了"不存在的 tool 返回 success:false",没测真实能力,所以没暴露这个 bug。

**建议**:
1. `test_call` 里先剥除前缀:`base_name = name.split(":", 1)[1] if ":" in name else name`,然后用 `base_name` 路由。
2. resource 分支需要前端传 URI(或后端按 name 反查 descriptor 拿 URI),不能直接用 name。
3. resource 返回拆成 `{"content": content, "mimeType": mime}` 而非塞 tuple。

---

## P1 — 安全 / 数据正确性

### 3. Web UI 完全无认证,且会跟随 `--host` 暴露到公网

**位置**:`evermcp/web/app.py:32-42`(`_get_or_create_token`,token 从未被校验)+ `evermcp/cli.py:209`(`_ui` 用 `bind_host`)

`app.py` 生成并持久化了本地 token(`_get_or_create_token`),但**没有任何中间件校验它**。所有 `/api/*` 端点完全开放:`create_app` 里只挂了 CORS,没有 auth 依赖。s1-summary.md 已知限制 #2 也承认了这点。

更严重:`cli.py` 的 `_ui()` 用 `bind_host`(与 HTTP 共用 `--host`)。用户执行 `evermcp serve --http --ui --host 0.0.0.0` 时,UI 也绑 `0.0.0.0:8788` 且无认证 → 任何能访问该端口的人可:
- CRUD 能力(`POST/PUT/DELETE /api/capabilities`)
- 测试调用任意 tool/resource/prompt(`POST /api/test`),等于任意执行本地工具(含 `io.read_file` 等文件/网络工具)

**影响**:把 S0 的"默认 loopback、最小暴露面"安全姿态破坏了。token 机制形同虚设,且 UI 端口隐式跟随 HTTP host 绑定,用户很难察觉。

**建议**(S2 本就要做 auth,但 S1 至少要堵住暴露面):
- UI 强制绑 `127.0.0.1`,除非新增显式 `--ui-host` 选项(不跟随 `--host`)。
- 实现一个最小 `token` 校验依赖(从 cookie 或 `Authorization: Bearer` 读,比对 `_get_or_create_token()`),挂在 `/api/*` router 上。
- `serve --ui --host 0.0.0.0` 时若 UI 仍绑 loopback,需在日志显式提示"UI 仅本地可访问"。

---

### 4. 内联 resource 的 descriptor 用 `uriTemplate`,MCP 层完全不可见

**位置**:`evermcp/core/provider.py` `_InlineResourceCapability.descriptor()`

```python
return {
    "name": self.name,
    "description": self.description,
    "uriTemplate": schema.get("uriTemplate", ""),   # ← 字段名是 uriTemplate
    "mimeType": schema.get("mimeType", "text/plain"),
}
```

但 S0 的 `ResourceFunc.descriptor()`(`capability.py`)返回的是 `"uri"` 键,且 `mcp_server.py:_list_resources` 读 `d["uri"]`、`registry.read_resource` 按 `desc.get("uri") == uri` 匹配。

**影响**:内联声明的 resource 在 MCP `resources/list` 里 `uri` 为 None/缺失,`resources/read` 永远匹配不到 → 内联 resource 在 MCP 客户端层完全不可见、不可读。S1 的"表单声明 resource"对 MCP 客户端等于没声明。

**建议**:统一字段名为 `uri`(与 S0 `ResourceFunc` 对齐);若要保留 template 概念,另设 `uriTemplate` 但必须同时提供具体 `uri`,并在 `mcp_server._list_resources` 兜底读 `uriTemplate`。

---

### 5. REST 层直接访问 `registry._providers` 私有属性,破坏封装

**位置**:`evermcp/web/rest.py:48,90,139`(`getattr(coord.registry, "_providers", [])`)

`CapabilityRegistry` 已提供 `providers` property(`registry.py`,返回 list copy)。REST 层绕过它直接读 `_providers`,而且拿到的是**可变列表引用**——测试 `test_web_api.py` 正是靠 `coordinator.registry._providers.append(provider)` 注入 provider。

**影响**:
- 破坏封装:若 registry 改内部存储(改名/加锁/改返回类型),所有 REST 端点静默返回空,无编译期提示。
- 测试依赖私有可变性,与"providers 通过 `add_provider` 管理"的公开契约不符,误导后续开发者。

**建议**:改用 `coord.registry.providers`(已有 public property)。若需修改,用 `registry.add_provider()` / `remove_provider()`。

---

## P2 — 功能 / UX 缺陷

### 6. `PUT /api/capabilities` 只能改 `enabled`,前端编辑框是空实现 → 静默丢失编辑

**位置**:`evermcp/web/rest.py:188-198`(`update_capability` 只处理 `enabled`)+ `evermcp/web/static/index.html`(`updateCapability()` / `updateSchema()` 都是 `// Placeholder`)

UI 上"描述"和"参数 Schema"是可编辑的 textarea,绑了 `@blur="updateCapability"` / `@blur="updateSchema"`,但这两个函数是空实现。用户编辑后失焦,以为保存了,实际什么都没发生。后端 PUT 也只接受 `enabled`。

**影响**:静默数据丢失,严重误导用户。

**建议**(任选其一):
- 实现:后端 PUT 支持 `description` / `schema_json` 更新,前端实现 `updateCapability`/`updateSchema` 发请求。
- 或:前端把这两个 textarea 设为 `readonly` + 提示"内联能力编辑待 S2 支持",避免误导。

---

### 7. CORS `allow_origins` 通配符配置无效

**位置**:`evermcp/web/app.py:66-72`

```python
allow_origins=["http://localhost:*", "http://127.0.0.1:*"],
```

Starlette 的 `CORSMiddleware` **不支持通配符字符串**:`http://localhost:*` 是字面量,不会匹配 `http://localhost:5173`。通配只在 `allow_origins=["*"]` 单独使用时生效;要匹配端口范围必须用 `allow_origin_regex`。

**影响**:当前前后端同源(都从 UI 端口服务)所以暂时无感;一旦前端拆到 Vite dev server(`localhost:5173`),CORS 会拦截所有请求。配置是错的,误导后续开发。

**建议**:
```python
allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
```

---

### 8. 前端 `el-tree` 搜索失效 + 节点 label 字段缺失

**位置**:`evermcp/web/static/index.html`

**(a) 搜索失效**:`filterNode` 定义了并绑到 `:filter-node-method`,但 `watch(searchQuery)` 是空函数体(只有注释)。Element Plus 的 tree 过滤需**主动调用** `treeRef.value.filter(val)`,否则过滤不触发。

**(b) label 字段缺失**:`treeProps = {label: 'label', children: 'children'}`,但树节点数据:
- 顶层 `{source, label, children}` — 有 label ✓
- 中层 `{category, children}` — **无 label** ✗
- 叶子 `{name, kind, enabled, ...}` — **无 label** ✗

Element Plus 对没有 label 的节点显示空白。

**影响**:搜索框输入无反应;树里中层分类和叶子能力大部分显示为空白节点。

**建议**:
- `treeProps.label` 改为函数:`label: (data) => data.label || data.category || data.name`
- `el-tree` 加 `ref="treeRef"`,`watch(searchQuery, (val) => treeRef.value?.filter(val))`

---

### 9. 测试调用成功时不显示返回结果

**位置**:`evermcp/web/static/index.html` `testCall` 模板块

```html
{{ testResult.success ? '✓ 成功' : '✗ 失败: ' + testResult.error }}
```

成功时只渲染"✓ 成功",`testResult.result` 完全不显示。

**影响**:测试调用面板失去核心意义——用户调用了能力却看不到返回值。

**建议**:成功时渲染 `JSON.stringify(testResult.result, null, 2)` 到 `.test-result` 区域。

---

## P3 — 代码质量 / 一致性

### 10. `InlineDeclarationProvider` 每次写操作都调 `init_db`

**位置**:`evermcp/core/provider.py` `add_capability` / `delete_capability` / `update_capability_enabled`

三个方法每次都 `init_db(eng)`(跑 `SQLModel.metadata.create_all` 的 DDL 检查)。虽幂等,但每次 CRUD 都发 PRAGMA 查询是浪费;且 engine 解析逻辑 `eng = self._engine if self._engine else get_engine()` 在每个方法里重复,分散且易错。

**建议**:`__init__` 时 resolve 一次:`self._engine = engine if engine is not None else get_engine()`;构造时调一次 `init_db(self._engine)`;后续方法直接用 `self._engine`,移除重复的 `init_db` 调用。

---

### 11. `delete_capability` 用 `session.exec` 执行 DML,rowcount 在 sqlite 上不可靠

**位置**:`evermcp/core/provider.py` `delete_capability`

```python
stmt = sql_delete(InlineCapability).where(...)
result = session.exec(stmt)
deleted_count = result.rowcount
```

SQLAlchemy 2.0 中 `Session.exec` 主要面向 select;对 delete/update 应使用 `session.execute(stmt)`。且 sqlite 驱动对 DELETE 的 `rowcount` 行为不稳定(部分驱动返回 -1)。

**影响**:在某些 sqlite 驱动版本下 `deleted_count` 可能恒为 0 或 -1,导致删除成功却返回 `False`。

**建议**:改为先 `select` 确认存在再 `delete`,或用 `session.execute(stmt)` 并显式处理 rowcount。测试 `test_delete_capability` 当前通过,但不能覆盖所有驱动行为。

---

### 12. 三个 stub capability 类的 `descriptor()` 里重复 `import json`

**位置**:`evermcp/core/provider.py` `_InlineToolCapability` / `_InlineResourceCapability` / `_InlinePromptCapability` 的 `descriptor()`

每个 `descriptor()` 内部都有 `import json` + 相同的 try/except 解析逻辑,重复 3 次。

**建议**:提取 `_parse_schema(self) -> dict` helper 放基类或模块函数,三个类复用。

---

## 整体评价

S1 的架构方向(InlineDeclarationProvider + FastAPI REST + Vue 前端)是合理的,但**交付质量不达标**:

- **两个 P0 阻塞**:`--ui` 根本起不来(P0 #1)、测试调用对所有能力都失败(P0 #2)。这意味着 S1 声称"已完成"的核心功能——UI 启动和能力测试——实际都无法使用,且现有测试用例恰好绕开了这两个失败路径(没测 `--ui` 实启、没测真实能力的 test_call)。
- **一个 P1 安全缺陷**:无认证 + 跟随 `--host` 暴露(P1 #3),直接削弱了 S0 建立的安全姿态。
- **测试覆盖的盲区**:17 个测试都走 mock/空 registry/不存在的能力,没有一个端到端验证"启动 UI → 声明能力 → 在 MCP 层可见 → 测试调用成功",所以 P0 问题全部漏网。

**建议修复顺序**:
1. 先修 P0 #1(`uvicorn.Server.serve`)和 P0 #2(test_call 前缀/URI/序列化),补一个端到端测试。
2. 修 P1 #3(UI 绑定 + 最小 token 中间件)和 P1 #4(resource descriptor `uri` 字段)。
3. P1 #5、P2 一并清理。
4. P3 可延到 S2。

修完 P0 后建议跑一次真实 `evermcp serve --http --ui` 手动验证,再进入 S2。
