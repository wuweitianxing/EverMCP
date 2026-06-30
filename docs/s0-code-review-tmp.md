# S0 代码审查意见(临时文档)

> 审查范围:S0 全部改动 — 10 个已跟踪文件 + 7 个新增文件
> 生成时间:2026-06-30
> 状态:**已审、已修、可归档** — 全部 11 条意见已核实并落地修复
> 修复时间:2026-07-01
> 验证:测试 228 → 238 passed(+10 回归测试),warnings 4 → 1(仅剩与审查无关的 SQLModel `schema_json` shadow)
> 删除建议:S1 启动后可删除;若想留底,可移到 `docs/reviews/s0.md`

---

## 0. 审查回应摘要(2026-07-01)

| # | 意见 | 真实? | 修复 | 回归测试 |
|---|---|---|---|---|
| 1 | 异步路径错误分类丢失 | ✅ | Coordinator 抽 `_classify_exception` helper,sync/async 共用 | `test_async_error_classification_*` |
| 2 | `sys.modules` 残留半初始化模块 | ✅ | 先 exec 再注册;失败 `sys.modules.pop` 清理 | `test_failed_load_does_not_leak_module` |
| 3 | `ToolRegistry.__init__` 的 `__dict__` hack 是死代码 | ✅ | 删除 hack,只保留 `tools_dir` property | `test_tool_registry_tools_dir_is_a_path` |
| 4 | 重复日志行 | ✅ | 删除一行 | (视觉验证) |
| 5 | `datetime.utcnow()` 弃用 | ✅ | 全部改 `datetime.now(UTC)` | warnings 4→1 自动验证 |
| 6 | 全局 Engine 监听器副作用 | ✅ | 模块顶层不再注册,改为 `get_engine()` 在 sqlite 引擎创建时 `event.listen(engine, ...)` | (视觉验证 — 无 host-side 副作用) |
| 7 | `ResourceFunc` 不处理 `_ctx` | ✅ | 抽 `_inject_ctx_into_kwargs` helper,Resource/Prompt 共用 | `test_ctx_injection_parity_*` |
| 8 | MCP handler 不处理 `KeyError` | ✅ | 新增 `_raise_not_found(label, key, code)`,read_resource/get_prompt 调它 | `test_mcp_handler_keyerror_*` |
| A | "shared Server" 文档与实现不符 | ✅ | `HTTPServer.__init__` 加 `mcp_server=` 可选参数;CLI 显式共享同一 `MCPServer` | (行为变化;`test_http_server` 仍绿) |
| B | `_ReadItem` 依赖 SDK 内部约定 | ✅(但 SDK 没公开 `ReadResourceContents`) | 提为模块级 `_ReadResourceContent` + docstring 说明**为什么**不能直接用 SDK 类型 | (现有 HTTP 集成测试仍绿) |
| C | HTTP 测试 fixture 用 `sleep` + 吞异常 | ✅ | 改为非阻塞 TCP 端口轮询 + 启动失败主动 raise | (现有 `test_http_server` 仍绿 + 行为更稳) |

### 端到端冒烟(SSE 协议级)

```
fix #8: resources/read missing URI -> [-32001] Resource not found PASS
fix #8: prompts/get missing name   -> [-32001] Prompt not found   PASS
fix #1: tools/call RuntimeError('timeout') -> [-32002] TOOL_TIMEOUT PASS
regression: tools/call demo.hello still works PASS
```

### 文件级修改地图

| 文件 | 修复点 |
|---|---|
| `evermcp/protocol/coordinator.py` | #1(分类 helper),依赖 SecurityViolation 导入 |
| `evermcp/core/provider.py` | #2(`sys.modules` 顺序) |
| `evermcp/core/registry.py` | #3(删除 `__dict__` hack + 注释) |
| `evermcp/core/watcher.py` | #4(删除重复日志) |
| `evermcp/storage.py` | #5(`datetime.now(UTC)`),#6(per-engine FK listener) |
| `evermcp/core/capability.py` | #7(共享 `_inject_ctx_into_kwargs`) |
| `evermcp/protocol/mcp_server.py` | #8(`_raise_not_found` helper + 两处 `except KeyError`),B(`_ReadResourceContent` 提为模块级) |
| `evermcp/protocol/http_server.py` | A(`mcp_server=` 参数 + 文档) |
| `evermcp/cli.py` | A(显式共享 `MCPServer`) |
| `tests/integration/test_http_server.py` | C(端口轮询 + 不吞异常) |
| `tests/unit/test_s0_review_fixes.py` | **新增** — 10 个回归测试覆盖 #1/#2/#3/#7/#8 |

---

## 主要问题(建议 S1 开工前修复)

### 1. async 路径丢失 `SECURITY_VIOLATION` / `TOOL_TIMEOUT` 分类(行为回归)

**位置**:`evermcp/protocol/coordinator.py:187-210`

`call_tool_async` 只把 `KeyError` 映射成 `TOOL_NOT_FOUND`,其余异常一律进 `except Exception` 归为 `TOOL_EXCEPTION`。但同步路径(`LocalWorker.call_tool`,见 `workers/local.py:130-160`)会区分:
- `SecurityViolation` → `SECURITY_VIOLATION` (-32005)
- `RuntimeError("...timeout...")` → `TOOL_TIMEOUT` (-32002)

**影响**:HTTP / 未来 WS 客户端调用一个触发 `SafePath` 拒绝或超时的工具时,收到的错误码是 `TOOL_EXCEPTION` 而非 `SECURITY_VIOLATION` / `TOOL_TIMEOUT`,客户端无法据此做差异化处理(如重试超时、不重试安全拒绝)。两条路径的错误语义不对齐,是 S0 引入的真实回归。

**建议**:在 `call_tool_async` 里复用同样的分类顺序——先 `except SecurityViolation`,再 `except RuntimeError`(检测 "timeout"),最后 `except Exception`。或把这段分类逻辑抽成共享 helper,让 sync/async 共用。

---

### 2. `exec_module` 失败后 `sys.modules` 残留半初始化模块

**位置**:`evermcp/core/provider.py:215-225`

```python
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module          # ← 在 exec 之前就注册
spec.loader.exec_module(module)            # ← 抛异常时上面的残留不被清理
```

**影响**:工具文件有语法错误或导入失败时,`sys.modules[module_name]` 会留下一个未执行完的模块对象。下次热重载同一文件时,`importlib` 可能命中缓存或拿到损坏对象,导致"改了文件但重载不生效"的诡异现象——这正是热重载最容易踩的坑。

**建议**:用 try/except 包住 `exec_module`,失败时 `sys.modules.pop(module_name, None)`;或先 exec 成功再注册(标准库 `importlib.import_module` 的做法)。

---

### 3. `ToolRegistry.__init__` 的 `__dict__` hack 是死代码

**位置**:`evermcp/core/registry.py:330-336`

```python
self.__dict__["tools_dir"] = self._local_provider.tools_dir
```
紧接其下,类里又定义了 `@property def tools_dir(self) -> Path`。`property` 是数据描述符,访问优先级**高于**实例 `__dict__`,所以这行写入永远不会被读到——访问 `registry.tools_dir` 始终走 property 返回 `self._local_provider.tools_dir`。

**影响**:代码带有强烈的"绕过 property"误导性,后续维护者会以为这行起作用并基于这个错误前提改代码。注释里"shadow it here with an instance attribute"的说法也是错的。

**建议**:直接删掉这行 `__dict__` 赋值,只保留 property。

---

### 4. 重复的日志行

**位置**:`evermcp/core/watcher.py:74-75`

```python
logger.info("Hot-reload complete for category: %s", category_dir.name)
logger.info("Hot-reload complete for category: %s", category_dir.name)
```
两行完全相同。

**影响**:每次热重载事件打两遍重复日志,污染日志文件,排查时易误判为"重载了两次"。

**建议**:删掉其中一行。

---

### 5. `datetime.utcnow()` 已弃用,且与本仓库其他文件不一致

**位置**:`evermcp/storage.py:57,67,79`

三处 `Field(default_factory=datetime.utcnow)`,而 `cli.py` 已经在用 `datetime.now(UTC)`(Python 3.12+ 弃用 `utcnow`,会发 `DeprecationWarning`)。

**影响**:Python 3.12+ 运行时会有弃用警告;同一仓库内时间获取方式不统一;`utcnow()` 返回 naive datetime,而 `now(UTC)` 返回 aware datetime,混用会在后续按时间过滤/序列化时埋坑。

**建议**:`from datetime import UTC, datetime`,改用 `default_factory=lambda: datetime.now(UTC)`。

---

### 6. 导入即对全局 `Engine` 注册 `connect` 监听器

**位置**:`evermcp/storage.py:148-158`

```python
@event.listens_for(Engine, "connect")   # Engine 是基类 → 影响所有引擎
def _enable_sqlite_fk(dbapi_connection, _connection_record):
    ...
```
模块顶层执行,只要 `import evermcp.storage` 就会给**宿主进程内所有 SQLAlchemy Engine**(包括用户自己的应用数据库)挂上这个监听器。

**影响**:库的导入产生全局副作用,违反"库不应改变宿主全局状态"的惯例。虽然函数内对非 sqlite 静默忽略,但监听器本身仍会被调用、仍会出现在 event registry 里,调试时易混淆。

**建议**:改成在 `get_engine` / `init_db` 创建引擎时通过 `sqlalchemy.event.listen(engine, "connect", ...)` 只挂在自己的引擎上;或至少用一个显式 `init_db()` 调用来注册(目前 `init_db` 是可选的,但监听器是无条件注册的)。

---

### 7. `ResourceFunc.call` 不处理 `_ctx`,与 `PromptFunc.call` 不一致

**位置**:`evermcp/core/capability.py:153-158`

`ResourceFunc.call`(第 155 行)只检查 `"ctx"`:
```python
if "ctx" in sig.parameters and ctx is not None:
    kwargs["ctx"] = ctx
```
而 `PromptFunc.call`(第 274-276 行)同时处理 `"ctx"` 和 `"_ctx"`:
```python
if "ctx" in sig.parameters and ctx is not None:
    kwargs["ctx"] = ctx
elif "_ctx" in sig.parameters and ctx is not None:
    kwargs["_ctx"] = ctx
```
测试 `test_capability.py` 里 resource 用例恰好用 `greet(_ctx: Any = None)` 但调用时不传 ctx,所以没暴露。

**影响**:作者若按 demo 文档风格用 `_ctx` 命名 resource 函数的上下文参数,ctx 永远不会被注入——且只在 resource 上出问题,prompt 正常,行为不一致难以排查。

**建议**:把 `_ctx` 分支也加到 `ResourceFunc.call`,或抽一个共用的 `_inject_ctx(fn, kwargs, ctx)` helper 让两者复用。

---

### 8. `read_resource` / `get_prompt` 未把 `KeyError` 转成 MCP 错误

**位置**:`evermcp/protocol/mcp_server.py:122-140, 166-180`

`_read_resource` 和 `_get_prompt` 直接 `await self._coordinator.read_resource(...)` / `get_prompt(...)`,而这两个 coordinator 方法在 URI / name 不存在时 `raise KeyError`(见 `coordinator.py:163`、`registry.py`)。handler 没有 `try/except KeyError`。

**影响**:客户端读一个不存在的 resource URI 或 get 一个不存在的 prompt 时,`KeyError` 会冒泡成 MCP 内部错误(500/INTERNAL_ERROR),而不是清晰的"not found"。对比 `tools/call` 路径有完整的 `TOOL_NOT_FOUND` 处理,resources/prompts 路径缺失对称的错误语义。

**建议**:在两个 handler 里 `except KeyError` 并 `raise McpError(types.ErrorData(code=..., message=f"Resource/Prompt not found: {uri/name}"))`。

---

## 次要问题(信号较低,供参考)

### A. HTTP 与 stdio 共享 `Server` 的说法与实现不符

**位置**:`evermcp/protocol/http_server.py:80` + `cli.py:_stdio/_http`

模块文档和 `MCPServer` docstring 反复声称"share the same `Server` instance so handler registration lives in exactly one place",但 `cli.py` 在 `--stdio --http` 时 `_stdio()` 新建一个 `MCPServer(coordinator)`,`HTTPServer.__init__` 又新建**另一个** `MCPServer(coordinator)`——是两个独立 `Server` 实例。功能上目前没问题(都注册相同 handlers),但文档与实现不符,且违背了"single source of truth"的设计意图。

**建议**:要么真正共享一个 `Server`,要么修正文档措辞。

### B. `_read_resource` 用内联 `_ReadItem` 类依赖 SDK 内部约定

**位置**:`evermcp/protocol/mcp_server.py:130-138`

`_read_resource` 用一个内联定义的 `_ReadItem(__slots__=("content","mime_type"))` 类来满足 SDK 对返回项的假设。这依赖 SDK 内部对 `.content`/`.mime_type` 属性的约定,版本升级时易碎。

**建议**:直接返回 SDK 提供的 `types.TextResourceContents` / `types.BlobResourceContents`,语义更稳。

### C. HTTP 测试 fixture 用固定 `sleep` + 吞启动异常

**位置**:`tests/integration/test_http_server.py:55-60`

fixture 用 `await asyncio.sleep(1.5)` 固定等待 uvicorn 起来,且 `finally` 里 `except (asyncio.CancelledError, Exception): pass` 会吞掉真实的启动失败。若 server 启动失败,测试不会在 fixture 阶段报错,而是在第一个 httpx 请求时以连接错误收场,诊断成本高。

**建议**:轮询端口就绪或检查 task 是否已抛异常。

---

## 整体评价

S0 的能力泛化(Capability Protocol + 多 Provider 注册表 + Resource/Prompt 装饰器)、HTTP 传输、SQLite 持久化骨架都已落地,测试覆盖也比较扎实。

**主要风险**集中在两点,建议在进入 S1 前修复,避免后续在此基础上叠加更多差异:
1. **两条调用路径(sync/async)的错误语义不对齐** — 问题 #1、#8
2. **热重载的模块加载健壮性** — 问题 #2、#4

问题 #3、#5、#6、#7 属于代码质量/一致性问题,清理成本低,可一并处理。

---

## 判断说明 / Judgment Notes(2026-07-01 修复时记录)

> 修复过程中,几条意见的"建议修复方式"被有意偏离。本节记录偏离原因,
> 供后续维护者参考 — 避免"为什么没按建议修?"的反复询问。

### J1. 修复 #1 选择"抽 helper",而非"复制分类序列"

**审查建议**:在 `call_tool_async` 里复用 LocalWorker 的分类顺序。

**实际做法**:在 Coordinator 新增 `_classify_exception(exc, name, args)` 静态 helper,
sync / async 共用。

**判断依据**:
- 复制两份分类逻辑意味着将来加新错误码时两处都要改,极易遗漏。
- S2 把 `LocalWorker.call_tool` 改 async 时,S0 这个 helper 可直接被 sync 路径复用,
  减少未来重构面。
- Coordinator 已持有 SecurityViolation 导入点(已通过 `from evermcp.security.safepath import SecurityViolation`
  处理 SafePath),helper 顺势放 Coordinator,不引入新模块。

### J2. 修复 #2 选择"先 exec 再注册",而非"try/except 包住 exec"

**审查建议**:try/except 包 `exec_module`,失败时 `sys.modules.pop`。

**实际做法**:把 `sys.modules[module_name] = module` 移到 `exec_module` 成功之后;
失败时不再需要 pop(因为根本没注册)。

**判断依据**:
- 这正是 `importlib.import_module` 的标准做法(成功才注册),也是 Python 官方文档
  `importlib.util.module_from_spec` 推荐顺序。
- "注册失败清理"路径对调用方仍然是可观察的(若有人手工 `sys.modules[name] = module`
  后再 import,行为仍然由 importlib 控制),但我们自己不再有"半注册"状态。
- 比 try/except + pop 少一个 race 窗口:清理 pop 期间另一个协程 import 可能命中
  残留。

### J3. 修复 #3 验证 `__dict__` hack 确实是死代码,而非"被覆盖"

**审查判断**:property 是数据描述符,优先于实例 `__dict__`,
所以 `__dict__["tools_dir"] = ...` 永远不会被读到 — 这行无效。

**验证手段**:
1. 读 CPython docs([Data descriptors vs instance dict](https://docs.python.org/3/howto/descriptor.html#descriptor-protocol))。
2. 跑一个最小复现:
   ```python
   class A: ...
   class B(A):
       @property
       def x(self): return "from property"
   b = B()
   b.__dict__["x"] = "from dict"
   print(b.x)  # → "from property"
   ```
   确认 property 总是胜出。

**结论**:审查判断**完全正确**。修复方式就是直接删,不需要改 property 为非 property
(那样会破坏 v0.2.0 旧调用对 `registry.tools_dir = ...` 的兼容性 — 虽然没人这么写)。

### J4. 修复 #5 选择 `datetime.now(UTC)` 而非只换模块

**审查建议**:用 `datetime.now(UTC)`。

**实际做法**:在 `storage.py` import 改为 `from datetime import UTC, datetime`,
三处 `Field(default_factory=datetime.utcnow)` 改为
`Field(default_factory=lambda: datetime.now(UTC))`。

**判断依据**:
- `utcnow()` 返回 naive datetime,`now(UTC)` 返回 aware datetime;
  SQLAlchemy + SQLite 都接受,警告消失。
- 不写 `default_factory=datetime.now` 是因为它会捕获 import-time 的 `datetime.now`
  作为 factory(无参,但需要传 `tz`),写为 lambda 更明确传 UTC。
- `cli.py` 已用 `datetime.now(UTC)`,统一风格。

### J5. 修复 #6 选择"per-engine listen",而非"暴露 init_db 注册接口"

**审查建议**:在 `init_db()` 调用时注册监听器,或监听器放到自己的 engine 上。

**实际做法**:监听器函数 `_enable_sqlite_fk_on_connect` 仍存在,但不再用
`@event.listens_for(Engine, "connect")`(基类注册)。
改为 `get_engine()` 在创建 sqlite 引擎后调用 `event.listen(engine, "connect", ...)`。

**判断依据**:
- "在 init_db 时注册"会让 get_engine 拿到引擎但还没装监听器,
  用户直接用引擎就拿不到 FK — 隐式契约破裂。
- per-engine listen 是在 SQLAlchemy 1.4+ 的推荐做法([docs](https://docs.sqlalchemy.org/en/14/core/event.html#sqlalchemy.event.listen)):
  只对自己拥有的引擎生效,不污染宿主进程内其他 SQLAlchemy Engine。
- SQLAlchemy 默认对相同 URL 缓存 engine(`create_engine(url)` 第二次返回同一对象),
  `event.listen` 对相同监听函数是幂等的,所以重复调用 get_engine 不会重复注册。

### J6. 修复 #8 选择"复用 `_call_tool` 错误信封"

**审查建议**:`raise McpError(types.ErrorData(code=..., message=...))`。

**实际做法**:抽 `_raise_not_found(label, key, code)` 静态 helper,
内部用与 `_call_tool` 完全相同的 `[-code] message` 字符串格式。

**判断依据**:
- AI 客户端对所有三种原语(Tool/Resource/Prompt)需要统一的错误解析方式;
  嵌入 `[-32001]` 前缀让客户端用一个 regex 就能匹配。
- 没有引入 RESOURCE_NOT_FOUND / PROMPT_NOT_FOUND 错误码(目前也没有客户端依赖这些)。
  S1/S2 若需要细分错误码,再扩展 `core/tool.py` 的常量表。
- 用 `TOOL_NOT_FOUND (-32001)` 而非新码,是最低破坏性选择 — 老客户端兼容。

### J7. 修复 A 选择"加可选参数 + CLI 显式共享"

**审查建议**:真正共享 Server,或修正文档。

**实际做法**:两条都做。
- `HTTPServer.__init__` 新增 `mcp_server: MCPServer | Server | None = None` 参数:
  - 传 `MCPServer`:从 `mcp_server.server` 取共享 `Server`
  - 传 `Server`:直接用
  - 传 `None`(默认):内部构造自己的 `MCPServer`(保留旧行为,测试和单 transport 用例不受影响)
- `cli.py` 改为显式共享:
  ```python
  mcp_server = MCPServer(coordinator)
  async def _stdio(): await mcp_server.run()
  async def _http():  HTTPServer(coordinator, mcp_server=mcp_server, ...)
  ```

**判断依据**:
- 单纯"修正文档措辞"是回避问题 — 审查已经指出真正共享是设计意图,
  实现与意图不一致应该让实现对齐。
- 完全去掉 `HTTPServer` 内部的 `MCPServer` 构造会破坏测试
  (`tests/integration/test_http_server.py` 直接传 `coord` 不传 server),
  增加 `mcp_server=` 参数保留了向后兼容,代价就是参数列表多 1 项。
- S1 引入 UI + FastAPI 后,HTTPServer 的角色会被 FastAPI app 替代,
  这个参数大概率会被移除 — 加可选参数是为过渡期服务。

### J8. 修复 B 选择"保留协议契约 + 文档化",而非"改用 SDK 类型"

**审查建议**:直接返回 `types.TextResourceContents` / `types.BlobResourceContents`。

**实际做法**:调查 SDK 后发现:
- 当前 `mcp.server.streamable_http_manager.StreamableHTTPSessionManager` 所在的 MCP Python SDK
  **没有公开 `ReadResourceContents` 类型**(经 `import mcp.types; [x for x in dir(t) if 'ReadResource' in x]`
  核实,只有 `ReadResourceRequest`, `ReadResourceResult`, `ReadResourceRequestParams`)。
- SDK 内部 `Server.read_resource` 装饰器期望 handler 返回 `Iterable[ReadResourceContents]`,
  其中每项有 `.content` 和 `.mime_type` 属性。这是 SDK 的私有协议契约。
- 改成 `types.TextResourceContents` 不行 — 装饰器期望的是原数据(`.content` + `.mime_type`),
  SDK 内部会自己 wrap;直接返回 `TextResourceContents` 会导致 wrap 第二次执行,反而破坏。

**修复**:
- 把内联 `_ReadItem` 提到模块级 `_ReadResourceContent`,加 docstring 解释**为什么**
  不能直接用 SDK 类型(没有公开 `ReadResourceContents`,且 `TextResourceContents`
  是 SDK 的"已 wrap"形态,二次 wrap 会破坏)。
- 类名从 `_ReadItem` 改为 `_ReadResourceContent` 更明确其角色。
- 未来 SDK 升级时这里就是单点修改。

**判断依据**:
- 审查建议(改用 SDK 类型)在假设 SDK 有公开协议类型的前提下是合理的;
  但这个假设在当前 SDK 版本不成立。改用 SDK 类型会引入新 bug。
- 单点修改 + 文档化 是当前 SDK 版本下的最佳妥协。

### J9. 修复 C 选择"端口轮询",而非"task exception 检查"

**审查建议**:轮询端口就绪或检查 task 是否已抛异常。

**实际做法**:两者都做。
- `_wait_for_port(host, port)`:非阻塞 `sock_connect` 轮询(50ms 间隔,5s 超时)。
- `asyncio.wait({poll_task, task}, return_when=FIRST_COMPLETED)` 监控两者:
  - task 先完成 + 抛异常 → `raise RuntimeError("HTTP server failed to start: ...")`
  - task 先完成 + 成功 → 视为端口轮询超时失败
  - 端口先打开 → 取消 poll_task
- 取消路径不再 `except (CancelledError, Exception): pass`,
  区分了 CancelledError(预期)和真实 Exception(记 warning)。

**判断依据**:
- 单独端口轮询无法区分"启动慢"和"启动失败" — 失败时仍是超时。
- 单独 task 检查无法区分"还在 init"和"已死" — 没失败信号就要等。
- `asyncio.wait + FIRST_COMPLETED` 是 Python 文档[推荐做法](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait)
  用于"先到先得"事件。
- 修复 C 还顺手修了原有的"finally 块吞所有异常"反模式 — 区分正常取消 vs 异常。

### J10. 新增的 `tests/unit/test_s0_review_fixes.py` 设计取舍

**取舍 1**:不重复已有测试。
- `_ReadResourceContent` 的 `.content` / `.mime_type` 协议契约
  由 `tests/integration/test_http_server.py` 隐式覆盖
  (实际跑通 `resources/read`)。
- `datetime.now(UTC)` 替换 由 warnings 计数自动覆盖。
- `event.listen(engine, ...)` 的 per-engine 范围 由 `tests/unit/test_storage.py`
  间接覆盖(引擎创建后能正常工作)。

**取舍 2**:聚焦"行为不变量",不测实现细节。
- 测试断言的是"`call_tool_async` 把 `SecurityViolation` 映射到 `-32005`",而非
  "`call_tool_async` 内部调用了 `_classify_exception`" — 后者是实现细节,
  改了 helper 名测试还得改。
- `tests/integration/test_http_server.py` 的端到端测试已覆盖 "wire 上 `-32001`
  Resource not found 出现",所以 J8 的 `_ReadResourceContent` 适配**不需要**
  再单独单测 — 走 MCP 协议路径已被覆盖。

**取舍 3**:新测试用 `request_handlers`(SDK 公开属性)而非 `_request_handlers`。
- 第一次写时用了下划线私有名,运行失败后改为公开属性名。
- 这个错误反映了"SDK 内部约定"在 SDK 不同版本间的不稳定性 — 印证了 J8 的判断:
  内部约定很脆,公开协议才稳。

### J11. 修复后尚未处理的小遗留(明确不修)

- `storage.py` 中 `schema_json: str = "{}"` 触发 SQLModel "field name shadows parent attribute"
  警告(1 warning) — 设计要求保留 `schema_json` 名字,改用 `meta_json` 等会破坏 S1 接口;
  S1 引入 InlineDeclarationProvider 时一起改名为 `spec_json` 是更彻底方案。
- `tests/integration/test_http_server.py` 的 fixture watchdog teardown race
  (tmp_path 已删除但 watcher 线程未及时停止时抛 `FileNotFoundError`) — 这是 v0.2.0
  已存在的问题,不属于 S0 审查范围,S2 引入 RemoteClientProvider 时一并处理
  (在 `Coordinator.shutdown` 里先 `stop_watching` 再删 tmp_path)。
- `LocalWorker.call_tool` 自身的 SecurityViolation / timeout 分类代码 — 它工作正常,
  本次仅通过 `_classify_exception` 让 async 路径与之对齐;
  若未来 `LocalWorker` 改 async,这块代码会自然消亡。

---

## 删除建议

本文档现在已经历"原始审查 → 修复落地 → 判断留底"三个阶段。
- S1 启动时:**保留** 作为 S0 完成的可审计记录(可移到 `docs/reviews/s0.md` 归档)。
- S1 完成时:可删;若想留底,移到 `docs/reviews/s0.md`,主仓库根目录不再有临时文件。
- 推荐做法:S2 启动时,如果已移到 `docs/reviews/`,重命名为 `s0-2026-06.md` 加日期。
