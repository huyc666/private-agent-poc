# 代码评审报告 · Private Agent POC v0.13.0

评审范围：server/ 全部模块、sandbox/、tools/、packs/、gateway/、web/index.html（共 4282 行）
评审日期：2026-10-02

## 总体结论

架构分层清晰、安全原则（fail-closed）贯彻一致，达到 POC 交付质量标准。
发现 **3 个建议修复项（P1）**、3 个路线图确认项（P2）、4 个小项（P3）。
无阻断性缺陷。

**值得肯定的实践**：
- 依赖方向严格：`state.py` 零依赖处于最底层，`tools_admin` 懒导入 `agent` 避免循环依赖
- 审批状态机的竞态处理正确：`resolve` 与超时标记都是原子 `UPDATE ... WHERE status='pending'`，杜绝「显示批准但未执行」
- 沙箱链路 fail-closed 完整：`CUSTOM_TOOLS_EXEC=sandbox` 且沙箱不可达时拒绝加载任何自定义工具
- 包签名覆盖全部可执行/可注入内容，重签幂等，比较用 `hmac.compare_digest`
- 计量回调「失败绝不阻塞主链路」

---

## P1 · 建议修复

### 1. 路径越界校验存在前缀陷阱（4 处）

`tools_builtin.py:19/49`、`tools/local_tools_server.py:35/49`：

```python
if not str(target).startswith(str(config.TOOL_WORKSPACE)):
```

字符串前缀比较有两个漏洞：
- **兄弟目录逃逸**：工作区为 `D:\proj\agent-poc` 时，`D:\proj\agent-poc-evil\secret.txt` 同样通过前缀检查
- **Windows 大小写**：`startswith` 区分大小写，`c:\...` vs `C:\...` 盘符大小写不一致时误拒或误放

建议统一改为（Python 3.9+）：

```python
if not target.is_relative_to(config.TOOL_WORKSPACE):
```

`is_relative_to` 按路径组件比较，无前缀陷阱；`resolve()` 已先行处理符号链接与 `..`。

### 2. `_sandbox_up()` 在事件循环中同步阻塞

`custom_tools.py:148` 的 `urlopen(timeout=2)` 是同步调用，而调用链
`agent.stream_reply` → `maybe_reload_custom_tools()` → `_sandbox_up()`
运行在 asyncio 事件循环里。缓存（5s TTL）过期时，**所有并发会话会被一起卡住最多 2 秒**；
`SANDBOX_URL` 指向不可达地址时每次缓存过期都必现。

建议：`maybe_reload_custom_tools()` 改为 async 并把探测包进 `asyncio.to_thread`，
或将探测下沉到后台周期任务，调用路径只读缓存。

### 3. `usage.py` 缺少 WAL / busy_timeout

`sessions.py` 与 `approval.py` 的 `_connect()` 都设置了
`PRAGMA journal_mode=WAL` + `busy_timeout=3000`，`usage.py` 没有。
高并发对话（多会话同时落账）下写-写/读-写互斥会出现 `database is locked` —
计量虽然不阻塞主链路（异常被吞），但会**静默丢账**。

建议：`usage.py` 的 `record()` / `_query()` 复用同款 `_connect()` 模式。

---

## P2 · 路线图确认项（已知，记录备查）

1. ~~**API 无认证 + 审批无身份绑定**~~（**已修复**，v0.14.0）：Bearer API Key 认证
   （pak- 前缀，sha256 存储，可吊销）；三级角色 user/approver/admin；user_id 改由
   中间件注入（计量归属可信）；approvals 表加 user_id/session_id/decided_by 三列
   （旧库自动迁移），user 只能决议自己创建的审批，approver/admin 可决议任何；
   开放模式（AUTH_ENABLED=false，默认）行为与现状一致。设计见 AUTH_DESIGN.md。
2. ~~**客户端断连丢失 assistant 回复**~~（**已修复**，v0.13.1 当日）：落库移入 `finally`，
   浏览器中途关闭时已产出的部分回复仍会写入对话记录，并追加「（连接中断，回复不完整）」
   标记供审计识别；正常完成的回复不受影响（两种场景均已实测）。
3. ~~**审批卡片不可恢复**~~（**已修复**，v0.13.1 当日）：记录面板「审批记录」页的 pending
   条目新增 批准/拒绝 按钮（带 fail-closed 提示：原对话已断开时决议仅关闭记录、操作不补执行），
   页面加载时自动检查遗留待审批并在对话区给出引导提示。内置浏览器实测：
   提醒条 ✓、按钮渲染 ✓、面板决议写库 ✓、记录状态迁移 ✓。

---

## P3 · 小项

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| 1 | `tools_admin.py` / `skills.py` | `create_skill` 无审批即可写入 prompt 文本；SKILL.md 会注入系统提示词，构成 prompt 注入面（风险低于可执行代码，但与其他「写能力」操作不一致） | 挂审批或在文档明确豁免理由 |
| 2 | `packs.py:288` | 包级 env 用 `os.environ.setdefault` 注入进程，包禁用后环境变量仍残留在进程内 | 禁用包时按清单 `pop`，或文档注明 |
| 3 | `packsign.py:29` | `_SIG_LINE` 正则定义后从未使用（`read_signature` 用的是行解析） | 删除死代码 |
| 4 | `weather.py` / `get_current_weather.py` | User-Agent 硬编码 `private-agent-poc/0.1`，未随内核版本演进 | 改为从统一版本常量读取 |

---

## 逐模块简评

| 模块 | 行数 | 评价 |
|------|------|------|
| `agent.py` | 237 | 重构后职责单一（图装配 + 流式编排），worker token 过滤、5 轮审批上限防死循环都到位 |
| `state.py` | 27 | 集中式 AppState，为无状态化预留了替换点，干净 |
| `approval.py` | 191 | 状态机 + 竞态处理 + 启动清理残留 pending，是全项目质量最高的模块之一 |
| `sessions.py` | 115 | 与 checkpointer 职责划分清楚（展示记录 vs 模型上下文），WAL 配置正确 |
| `tools_builtin.py` | 63 | 最小安全集合，职责边界文档写得好；路径校验见 P1-1 |
| `tools_admin.py` | 322 | 16 个管理工具全部挂审批门；`BUILTIN_TOOLS` 列表与 `builtin_langchain_tools()` 双份维护，可精简（仅冗余，无错误） |
| `packs.py` | 476 | 校验链完整（名称/版本/签名/env/模块依赖）；平台包治理规则清晰 |
| `packsign.py` | 122 | canonical 形式稳定、重签幂等；见 P3-3 |
| `custom_tools.py` | 275 | ast 静态提取 + 沙箱/本地双模式设计精良；见 P1-2 |
| `multi_agent.py` | 153 | 编排干净，worker 权限架构层隔离（不靠 prompt 约束）是正确做法 |
| `skills.py` | 138 | 渐进式披露实现正确；同名冲突内核优先合理 |
| `usage.py` | 184 | contextvars 归属方案正确；见 P1-3 |
| `telemetry.py` | 99 | 观测失败降级得体，绝不拖累主链路 |
| `main.py` | 160 | 端点简洁；见 P2-1/P2-2 |
| `mock.py` | 301 | 与生产链路完全隔离，事件序列与真实模式同构；离线演示数据有明确标注 |
| `sandbox/server.py` | 141 | 隔离子进程 + `-I` 模式 + env 白名单校验，防御到位 |
| `web/index.html` | 403 | 全部用 `textContent` 赋值（无 XSS 面），零外部依赖可断网运行 |

---

## 修复记录（2026-10-02，评审当日闭环）

三个 P1 已全部修复并验证：

1. **路径校验**：4 处 `startswith` 全部改为 `Path.is_relative_to()`（`tools_builtin.py` ×2、
   `tools/local_tools_server.py` ×2）。实测：`../` 穿越、Windows 反斜杠穿越、
   兄弟目录 `agent-poc-evil` 前缀攻击（含 `do_delete` 路径）均拦截，正常文件读取不受影响。
2. **事件循环阻塞**：`maybe_reload_custom_tools()` 加 `_reload_lock` 串行化（并发会话只重建一次），
   调用点改为 `await asyncio.to_thread(...)`，沙箱探测的同步网络 IO 不再阻塞事件循环。
3. **usage.py WAL**：新增与 sessions/approval 同款的 `_connect()`（WAL + busy_timeout=3000），
   `record()` / `_query()` 均已切换。实测落账、聚合查询正常，`PRAGMA journal_mode` 确认为 `wal`。

冒烟测试：Mock 模式启动 → `/api/health` 200 → SSE 对话（时间工具调用）全链路正常。
