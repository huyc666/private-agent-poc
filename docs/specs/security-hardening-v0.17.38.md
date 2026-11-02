# 安全加固 v0.17.38：删除与在途 chat 并发的终局语义（代际 fencing，spec）

状态：待确认（基线 96acbc0，651 passed / 18 skipped；本轮为 11-01 提交后
复核报告 `docs/reviews/2026-11-01-v0.17.37-commit-rereview.md` §1 P0
整改——用户已裁决优先于 SSO 身份一致性）。

## 一、背景

复核报告判定：v0.17.37 的 tombstone 只拒绝**新请求**，不能终止**在途
写入**——DELETE 返回成功后仍可能出现新 checkpoint、新 pending 审批、新
assistant 消息。证据（96acbc0 逐行核实）：

- `main.py:567-570`：chat 流只在开始前查一次 tombstone，执行期无终局校验；
- `main.py:594-605`：`finally` 无条件把已收集的 assistant 内容写回
  （可能已删除的）会话；
- `agent.py:465-529`：在途执行可继续推进图（写 checkpoint）、创建
  pending 审批、批准后 resume 危险操作；
- `main.py:579-584`：lease.lost 只在下一个 SSE 事件观察；等审批期间
  （心跳间隔 15s）无及时检查；
- `main.py:711-722`：`_purge_session` 顺序清理，无在途取消/代际协议。

报告 §5 准入条件：删除代际/fencing，覆盖消息、审批、checkpoint、危险
工具实际执行点——写提交前验证会话活跃且代际未变，旧代际 fail-closed；
补 3 类确定性并发测试（流中途删除 / pending 后删除 / 删除与批准竞争），
断言 DELETE 后存储不增、无新 pending、危险工具不执行，事件驱动勿 sleep。

## 二、方案总览

**删除 = 在途执行的终局边界**。引入 `session_meta.generation` 代际：
chat 运行开场绑定当前代际，所有关键提交点提交前校验「活跃且代际未变」，
旧代际一律 fail-closed 中止。checkpoint 层以 `FencedCheckpointer` 委托
代理在写提交点拦截。

不做（本轮明确排除）：SSO 身份一致性 / 共享技能审批 / 混合归属收紧
（复核 P1，后续轮次）；真实浏览器 E2E（部署人工验收）。

## 三、代际 fencing 设计

### 3.1 session_meta.generation（sessions.py）

- `session_meta` 加列 `generation INTEGER NOT NULL DEFAULT 0`（老库
  `PRAGMA table_info` 探测补列，存量行视为 0）；
- `mark_deleted`：现有 tombstone 逻辑 + `generation = generation + 1`
  ——删除是唯一递增来源，且 ID 永不复用（v0.17.37 终局语义），代际
  单调递增后旧代际必然失效；
- `get_meta` 返回值增加 `generation`；
- `fence_value(sid) -> int | None`：tombstoned → None；活跃 → 当前代际
  （无 meta 行 = 开放模式新会话，视为代际 0 活跃）；
- `fence_ok(sid, generation) -> bool`：活跃且 `meta.generation == 代际`
  （无 meta 行 = 代际 0 匹配）；进程内 tombstone 缓存（`_deleted_cache`：
  一旦观察到 deleted 即永久缓存——tombstone 单调，缓存永远可靠）。

### 3.2 消息写入终局校验：append_fenced（sessions.py）

`append_fenced(sid, generation, user_id, role, content, extra) -> bool`：

- 单事务 `BEGIN IMMEDIATE`：先取写锁，再 SELECT 代际/deleted 校验，
  通过则 INSERT 消息并提交，不通过回滚返回 False；
- `BEGIN IMMEDIATE` 使「校验 + 写入」与 `mark_deleted` 的写事务互斥
  （SQLite 同库写锁），关闭 check-then-act 窗口——写要么先于删除提交
  （随后被 delete_session 清掉），要么看到 tombstone 被拒，不存在
  「删除完成后落库」的中间态；
- 保留原 `append`（无栅栏）供存量直写路径与测试兼容。

### 3.3 chat 端点改造（main.py）

1. 取得锁与租约、通过归属终判后：`gen = sessions.fence_value(sid)`；
   `gen is None`（已删除）→ SSE error「会话已删除，本轮写入被拒」并
   return（开放模式新行为：开放模式删除同样写 tombstone，chat 亦须
   终局拒绝——与认证模式口径统一）；
2. user 消息改 `append_fenced(sid, gen, ...)`，失败（False）→ SSE error
   并 return；
3. `stream_reply` 调用新增 `gen=gen` 透传；
4. `finally` 的 assistant 写回改 `append_fenced`，失败跳过落库并记日志一行
   供审计；
   修订（v0.17.38 实现，与 AC1 对齐）：跳过同时向客户端补发一条 error
   事件「会话已被删除，本轮回复已丢弃」——静默丢弃会让前端把已删除会话的
   回复当正常完成展示；事件在 finally 之后、租约释放前 yield，客户端
   断连路径（GeneratorExit）不经过该语句，无重入风险；
5. `finished` / 不完整标注语义不变（写回成功路径行为不变）。

### 3.4 agent.stream_reply 提交点校验（agent.py）

签名新增 `generation: int | None = None`（None = 非会话态调用方不栅栏）：
1. 每轮模型调用前（for _round 循环体首）：`fence_ok` 校验，失败 →
   yield error「会话已删除，本轮中止」并 return（避免删除后继续烧模型
   调用；checkpoint 层 3.5 仍兜底）；
2. interrupt 审批创建点：`create_approval` → `create_approval_fenced`
   （见 3.5），返回 None → yield error 并 return（不创建 pending、
   不等待、不 resume）；
3. 危险工具实际执行点：`wait_decision` 返回 True 后、resume 带
   approve 决议**之前**再 `fence_ok` 校验——批准与删除竞争时绝不把
   危险操作恢复执行；失败 → yield error 并 return；该审批留在
   approved-unexecuted（不 mark_executed），由既有启动/周期扫描转
   abandoned（fail-closed 审计链路不变）。

### 3.5 审批创建终局校验：create_approval_fenced（approval.py）

`create_approval_fenced(sid, generation, tool, args) -> str | None`：
单事务 `BEGIN IMMEDIATE` 校验 session_meta 活跃且代际匹配，通过则
INSERT approvals（含现有 contextvars 归属列），返回 aid；不通过返回
None。保留原 `create_approval`（无栅栏）供 tools_admin 共享技能路径
（复核 P1 后续轮次收口）。删除与「审批 INSERT」竞争时不存在删除
完成后新增 pending 的中间态（同 3.2 原子性）。

### 3.6 checkpoint 写提交校验：FencedCheckpointer（agent.py）

langgraph 1.2.14 + langgraph-checkpoint 4.2.0 已核实：Pregel 运行时对
checkpointer 无 isinstance 检查，纯委托代理可行。
（实现期修订，全量回归 test_build_agent_real_mode 暴露：图编译期
`StateGraph.compile → ensure_valid_checkpointer`（langgraph/types.py）
有 `isinstance(BaseCheckpointSaver)` 校验——此前只核查 Pregel 与
create_react_agent，漏了该处。修复：FencedCheckpointer 直接继承
BaseCheckpointSaver（普通 Generic 类，非 ABC、无抽象方法强制），isinstance
即通过；基类以 NotImplementedError 定义的其余公开方法会遮蔽
`__getattr__` 透传，故批量改写为对 inner 的显式委托——包装后读写路径
行为与被包装 saver 完全一致，委托设计不变。）

```python
class FencedCheckpointer:
    """写提交前终局校验：tombstoned 线程的一切 checkpoint 写入 fail-closed。"""
    def __init__(self, inner): self._inner = inner
    def __getattr__(self, name): return getattr(self._inner, name)  # 其余全透传
    async def aput(self, config, *a, **kw): self._check(config); await self._inner.aput(config, *a, **kw)
    async def aput_writes(self, config, *a, **kw): self._check(config); await self._inner.aput_writes(config, *a, **kw)
    # 同步 put/put_writes 同口径（防未来同步图执行路径）
```

- `_check`：从 `config["configurable"]["thread_id"]` 取 sid，
  `sessions.tombstoned(sid)`（进程内缓存命中零 DB 开销；未命中直查
  SQLite——同步调用，单键 SELECT，换取「校验→写入」之间无 await
  间隙）→ 命中抛 `SessionDeletedError`；
- 异常沿 astream_events 冒泡 → stream_reply 既有 `except Exception`
  分支 yield error 事件并 return → main finally 栅栏拒绝 assistant
  落库 → **在途执行终止**（图在下一个写点死亡，不再推进）；
- 装配点：`_make_checkpointer` 末尾
  `STATE.checkpointer = FencedCheckpointer(STATE.checkpointer)`
  （Postgres 先 `setup()` 再包）；`adelete_thread` 经 `__getattr__`
  透传，_purge_session 编排不变；`close_checkpointer` 走独立的
  `checkpointer_cm`，不受影响；
- 栅栏口径为 tombstone（不携代际）：删除终局语义下 ID 永不复用，
  「已删除」与「代际变旧」严格等价；chat/审批侧携代际是同一栅栏的
  显式形式（对报告「绑定代际」验收的可测化）。

### 3.7 竞态窗口的精确说明（spec 记录）

- 单进程 MemorySaver：`_check` 与 `aput` 之间无 await → 事件循环内
  原子，删除协程无法插入，**无孤儿 checkpoint 窗口**；
- AsyncPostgresSaver：aput 内部有真实网络 await，存在「校验通过 →
  写入提交」亚毫秒窗口被删除插入——该写入为孤儿数据但 tombstone
  已封禁、不可达（v0.17.37 已记录的兜底语义，属跨进程共享存储的
  固有限制，报告验收以确定性单进程测试为准）；
- 删除不取会话租约的决策维持（v0.17.37）：fencing 本身就是及时、
  可靠的执行边界，不再依赖「清租约 + 下一拍心跳」——回应报告
  「清租约不构成及时边界」的批评；lease.lost 检查保留（跨进程接管
  语义与已删除无关）。

## 四、红测试（tests/test_session_delete_concurrency.py）

确定性原则：全事件驱动（threading.Event 钩子 / 顺序调用控制交错点），
零 sleep。TestClient `with client.stream(...)` 逐行消费 SSE，
monkeypatch `agent.stream_reply` 伪造在途流，在流内定义钩子让测试
在受控交错点执行 DELETE。

- AC1（流中途删除，报告验收 1）：伪造 stream 在 token 中途挂起于
  threading.Event → 测试消费到首 token 后发 DELETE → 放行续流 →
  断言：assistant 残段未落库（fenced append 拒）、messages 表该会话
  为空、客户端收到删除中止 error 事件、用户消息仍先于删除提交故
  被 delete_session 清除。
- AC2（pending 后删除，报告验收 2）：伪造流产出 approval_required
  前挂起 → DELETE 完成 → 续流：断言 create_approval_fenced 拒绝
  （无新 pending，approvals 表该会话无 pending 行）、流中止不等待
  不 resume、SSE error 提示删除中止。
- AC3（删除与批准竞争，报告验收 3）：真实 pending 创建（approve 端
  点/`approval.resolve` 批准）→ DELETE → 伪造流 wait_decision 返回
  True 的交错下 fence 校验失败：断言危险工具未执行（伪造 execute 计
  数为 0）、无 resume、审批未 mark_executed（approved-unexecuted 留
  给 abandoned 扫描）、SSE error。
- AC4（checkpoint fencing 单元）：MemorySaver 包 FencedCheckpointer：
  活跃线程正常 aput/aget_tuple；mark_deleted 后 aput/aput_writes 抛
  SessionDeletedError；adelete_thread 透传正常；非目标线程不受影响。
- AC5（栅栏原子性单元）：append_fenced/create_approval_fenced——代际
  匹配写入成功；tombstone 后拒；持有旧代际拒；顺序交错（先删后写 /
  先写后删）下无孤儿行：DELETE 完成后任何 fenced 写返回 False 且库
  中无新增。
- AC6（代际与开放模式语义）：mark_deleted 使 generation 递增；
  开放模式（AUTH_ENABLED 关）chat tombstoned 会话 → SSE error 不
  落库；开放模式活跃会话 fenced 写正常。
- 回归守护：test_session_lifecycle.py / test_session_acl.py 全量不回归
  （现有 append 语义、tombstone 403/404、并发注册口径不变）。

## 五、验收与回归门槛

1. 红测试先行：AC1-AC6 先写先失败，失败对准目标行为；
2. 实现转绿后全量回归 `.venv\Scripts\python.exe -m pytest -q`，门槛
   **≥651 passed, 0 failed**（新增用例全过，18 skipped 语义不变）；
3. TRAE-code-review 双子代理评审 → 修复选择 → 复回归；
4. 文档同步（README 版本行 + 记录、docs/PLAN.md 计划外交付记录、
   SECURITY.md 删除语义如涉及）→ 单一 commit。

## 六、测试影响面与风险

修订（实现期记录）：
- AC1 确定性交错方式调整——httpx ASGITransport 缓冲整包响应，TestClient
  无法增量消费 SSE，客户端侧流式交错不可行；改为伪造流在两个 token 之间
  经独立 TestClient 连接内联执行 DELETE（服务端顺序 await 保证交错点），
  确定性不变；
- `test_agent_llm.py` 两处 `isinstance(STATE.checkpointer, MemorySaver)`
  断言改为 `._inner` 解包判定（统一包装 FencedCheckpointer 后口径更新）；
- `tests/conftest.py` isolated_env 增加 `sessions._deleted_cache.clear()`
  （tombstone 进程内缓存随 STATE_DB 逐测试切换一并清空，防跨库污染）。

- `test_sessions.py`：get_meta 返回结构新增 generation——现有断言按
  key 取值不受影响，新增代际用例；
- `test_session_lifecycle.py`：行为不变（fenced 路径对活跃会话等价
  append；AC4 tombstone 用例继续通过）；
- `test_agent_llm.py`：stream_reply 新增默认参数，既有调用不回归；
- mock.py 演示流不经真实图——FencedCheckpointer 不参与（mock 无
  checkpoint 写），AC1 走伪造 stream + 直连 fenced 断言；
- 风险：langgraph 升级若对 checkpointer 改变 isinstance 语义则代理
  失效——编译期校验经继承 BaseCheckpointSaver 满足（3.6 修订注），
  运行时无检查已核实；锁版本（requirements 固定）+ 回归守护
  （test_build_agent_real_mode 即编译期守护用例）；
  开放模式 tombstone chat 拒绝是新行为（此前写库成功），属语义统一，
  无既有用例依赖旧行为（已核对 test_sessions/test_session_acl）。

## 修订注（v0.17.40 外部复验整改，2026-11-02）

- **FencedCheckpointer 契约对齐（N2/N3）**：aput/put 补返回值透传（基类契约 RunnableConfig，aupdate_state/fork 路径依赖）；alist 移出 _delegate_async 委托清单改类体显式生成器直传——基类 alist 是 async generator，经 async def 包装成 coroutine 会使 aget_state_history 历史回溯路径 TypeError。回归用例：test_session_delete_concurrency.py test_ac4_aput_returns_inner_config / test_ac4_alist_passthrough_async_generator / test_ac4_real_graph_history_and_update_state。
- **MOCK_LLM 分支 fencing（O1，用户裁决修复）**：mock 演示分支入口补 `_fenced_off()` 校验，在途删除后不产出，与真实模型路径同口径。回归用例：test_o1_mock_branch_also_fenced。
