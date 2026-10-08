# 并发能力与压测报告 · Private Agent POC

版本：v0.17.8 · 2026-10-08（压测数据为 v0.16.1 实测；v0.17.1/v0.17.2 并发机制补充见 §八·五）
工具：`tools/load_test.py`（asyncio + httpx 阶梯并发，可重复执行）

## 一、结论（先看这里）

1. **单请求延迟健康**：热路径 /api/health 单连接复用约 **23ms**，首次约 34ms。
2. **发现的框架缺陷已修复**：认证解析、会话落库、健康检查的 SQLite/文件 IO
   原本是**同步调用跑在事件循环上**，高并发时把所有请求串行化——
   优化后轻端点吞吐 **2.9 倍**（24.6 → 70.1 req/s @并发50），p95 延迟 **6.8 倍**改善。
3. **扩展路径成立**：框架已是无状态设计（会话/审批在 SQLite/Postgres、
   模型上下文在 checkpointer），生产环境经「Linux + 多 worker + 网关」
   可近线性水平扩展；本报告数字是 Windows 开发机的**下限**，不是上限。

## 二、压测数据（Windows 开发机，单进程，MOCK_LLM + 认证模式）

### /api/health（轻端点）

| 并发 | 优化前 p50 / p95 / 吞吐 | 优化后 p50 / p95 / 吞吐 |
|---|---|---|
| 10 | 343ms / 2617ms / 8.9 req/s | 124ms / 2158ms / 12.0 req/s |
| 50 | 868ms / 5951ms / 24.6 req/s | **629ms / 878ms / 70.1 req/s** |
| 100 | 3445ms / 13445ms / 21.4 req/s | 2833ms / 4002ms / 34.7 req/s |
| 200 | 6294ms / 19509ms / 19.4 req/s | 5109ms / 13387ms / 28.1 req/s |

### /api/chat（Mock 流式）

| 并发 | 优化前 p50 / p95 / 吞吐 | 优化后 p50 / p95 / 吞吐 |
|---|---|---|
| 5 | 312ms / 702ms / 10.0 req/s | 215ms / 269ms / 21.1 req/s |
| 10 | 386ms / 842ms / 17.8 req/s | 358ms / 673ms / 19.9 req/s |
| 25 | 1151ms / 1956ms / 17.5 req/s | 990ms / 1434ms / 21.7 req/s |
| 50 | 2622ms / 4116ms / 17.1 req/s | 2058ms / 2757ms / 21.2 req/s |

## 三、定位过程（排查方法可复用）

1. health 并发 10 时 p50 就要 343ms → 单请求 curl 复测 240ms → 但同步组件
   进程内实测只有 7ms/个 → 矛盾，继续切分。
2. 裸 FastAPI「hello world」在本机也要 220ms（curl 每次新进程+新连接）→
   一度怀疑环境；换 httpx 长连接复测裸服务只有 **1.5ms** → 框架本身很快，
   curl 数字是测量伪影（Windows 上 curl 每次进程启动 + localhost 解析）。
3. 主服务长连接热请求 23ms ≈ 各同步组件耗时之和 → 坐实「同步 IO 阻塞
   事件循环」是并发场景的主瓶颈。

**教训**：Windows 上测本机 HTTP 延迟不要用 curl 单发（自带 ~220ms 伪影），
用长连接客户端；且压测要区分「单请求延迟」与「并发吞吐」两层指标。

## 四、修复内容（v0.16.1）

| 位置 | 问题 | 修复 |
|---|---|---|
| 认证中间件 | `resolve_identity` 同步查 SQLite，每请求阻塞事件循环 ~7ms | `asyncio.to_thread` 挪到线程池 |
| /api/chat | `sessions.append` 两处同步写库阻塞流式路径 | `asyncio.to_thread` |
| /api/health | `list_packs` 每次全量扫盘（yaml+签名校验 ~7ms）+ `pending_count` 同步查库 | 双双 `to_thread`；`scan_packs` 增加内存缓存（create/delete/sign 三处写入口失效缓存，手工改盘语义不变=重启生效） |

## 五、chat 流式的剩余瓶颈（解释，非缺陷）

chat 在并发 50 时延迟仍随并发近线性增长，原因是 Mock 模式用
`asyncio.sleep(0.015)` 模拟逐 token 输出，Windows ProactorEventLoop 的
定时器粒度约 15.6ms，几十条流并发时定时器唤醒互相排队。
**真实模式不受此影响**：token 到达由模型网络流驱动，不占本机定时器。
生产环境（Linux + uvloop）定时器粒度微秒级，此效应消失。

## 六、生产扩展路线（回答「用户增多能否优雅扩展」）

当前架构已具备水平扩展前提（无状态化 v0.13.0 完成）：

| 层 | 扩展手段 | 现状 |
|---|---|---|
| 接入层 | Nginx/网关负载均衡到多个 Agent 副本 | 网关模式已支持（SSO 头、LiteLLM） |
| Agent 进程 | uvicorn `--workers N` 或多副本容器 | ✅ 可叠加：会话/审批在 SQLite（可换 Postgres），上下文在 checkpointer（memory→postgres 已预留），用量在 usage.db（可换库） |
| 模型层 | vLLM 本身做 continuous batching；多副本走 LiteLLM 负载均衡 | ✅ LiteLLM 网关已支持 |
| 共享状态 | SQLite → Postgres（DATABASE_URL 已预留） | ✅ 迁移路径已备好 |

**待办（真正上量前）**：多副本验证（两个进程共用 STATE_DB 跑审批跨进程决议）；
SQLite 在高写入并发下建议换 Postgres；生产务必 Linux（Windows 仅开发用）。

## 七、复现方法

```bash
# 终端 1：启动服务
set MOCK_LLM=true && set AUTH_ENABLED=true && python server/main.py --port 7100
# 终端 2：压测（认证模式先建用户拿 Key）
python tools/manage_users.py add loadtest
python tools/load_test.py --host http://127.0.0.1:7100 --key pak-xxx
python tools/load_test.py --scenario health   # 只测轻端点
python tools/load_test.py --scenario chat     # 只测聊天流式
python tools/load_test.py --scenario session  # 同会话并发（串行化验证）
```

## 八、记忆与并发（v0.16.4 补充）

「Agent 的记忆会不会成为并发瓶颈」的完整结论——记忆分两层，分开看：

| 层 | 实现 | 并发结论 |
|---|---|---|
| 展示层（会话记录） | SQLite `state.db`，每轮 2 次写 | 当前量级远未碰到单写者上限；上量后换 Postgres（`STATE_DB` 配置已留） |
| 上下文层（模型记忆） | LangGraph checkpointer（memory / postgres） | **memory 模式不是吞吐瓶颈，但锁死多副本扩展**（各副本记忆不共享、重启即丢）；生产必须切 postgres（清单已列），代价是每步检查点写放大，靠连接池调优与上下文截断消化 |

同会话并发（API 调用方并行打同一 session_id）曾会导致 checkpointer
状态写冲突与记录交错——**v0.16.4 已加固**：整轮对话在同会话 asyncio 锁内执行，
不同会话互不干扰。验证：同一 session 并行 3/6/10 请求全部成功、总耗时线性、
19 回合消息记录严格 user/assistant 交替；不同会话并发吞吐与加锁前持平。

## 八·五、跨进程串行化（v0.17.1 补充）

v0.16.4 的 asyncio 锁只在单进程内生效，多副本部署时同会话并发仍会跨进程交错。
v0.17.1 落地**分布式租约锁**（`server/session_lock.py`，STATE_DB 后端）：

- chat 流「进程内 asyncio 锁 + 跨进程租约」双重互斥，多副本共享 STATE_DB 时
  同会话并发请求真正排队（租约 + 心跳续约，持有进程崩溃后租约到期自动被接管）；
- 租约被其他进程接管时本进程心跳打印「租约已被接管」日志并停止续约（v0.17.3）；
  **v0.17.5 起进一步中止本轮对话**——继续跑等于两进程同时推进同一会话（互斥失效、
  checkpointer 写冲突），chat 流检测到接管即发 error 事件终止，部分回复落库标注
  「租约被接管」；
- **v0.17.6 起获取租约有界等待**（架构评审 P3）：此前无上限轮询，同会话另一请求
  长时间持有租约（如审批 resume 挂起）时新请求无限挂起且用户零反馈；现超过
  `LEASE_ACQUIRE_TIMEOUT`（默认 120s，环境变量可调）抛 `LeaseTimeout`，chat 流
  返回 error 事件「租约被占用，请稍后重试」；
- 审批执行连续性同步加固：决议记录带 `worker_id`/`executed_at`，
  「已批准但执行丢失」周期扫描转 `abandoned` 供审计（v0.17.2）；
  **v0.17.5 起扫描带宽限期**（审批超时 + 扫描间隔）——刚批准正在 resume 执行的
  长任务处于「approved 且未标记」的正常中间态，不再被误标 abandoned；

注意：SQLite 的 WAL + busy_timeout 只保证**同机多进程**共享同一库文件；
跨机多副本须把 STATE_DB/USAGE_DB 换 Postgres（见 PRODUCTION.md 第六节）。
