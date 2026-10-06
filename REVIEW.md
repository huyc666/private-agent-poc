# 架构体检报告（v0.12.0）

> **状态更新（v0.13.0，2026-10-02）**：P1、P2、P3 均已完成并回归验证通过。
> - P1/P3（重构）：agent.py 937 → 237 行（只留图装配 + 流式编排）；8 个模块级全局收口为
>   `server/state.py` 的 `AppState` 单例；7 处审批样板收敛为 `approval.require_approval()`；
>   新增 `tools_builtin.py` / `tools_admin.py` / `mock.py`。
> - P2（无状态化）：会话记录迁 SQLite（`sessions.py`，重启不丢）；审批迁 DB 状态机
>   （`approval.py`，pending/approved/rejected/expired，跨进程可决议，启动自动清理残留）；
>   顺带修复了真实模式「全量历史 + checkpointer 追加」的上下文重复缺陷
>   （现只传新消息，历史由 checkpointer 接续，DeepSeek 两轮记忆实测通过）。
>
> **第二轮复审（同日，针对 v0.13.0 新代码）发现 4 个问题并已当场修复**：
> 1. 审批超时与决议的竞态窗口（极端时刻会出现「显示批准但未执行」）——
>    `wait_decision` 超时分支改为「只有自己成功标记 expired 才算超时，落败则以决议为准」；
> 2. SQLite 默认日志模式下多进程读写会互斥——`approval.py`/`sessions.py` 统一加
>    `PRAGMA journal_mode=WAL + busy_timeout=3000`（多副本共享库文件的前提）；
> 3. 包管理工具名字规范化不统一（enable 不规范化、disable 检查与执行用不同名字）——
>    tools_admin 四个包管理工具入口统一 `name.strip().lower()`；
> 4. 陈旧文档 2 处（README 内置工具指引、MCP 示例注释仍指向旧 agent.py）——已更新。
> 验证：审批状态机单测全过、WAL 生效确认、规范化路径单测、冒烟 e2e（启动/健康/审批删除）通过。
>
> 以下为原始评审内容。

体检范围：`server/` 全部模块 + `tools/local_tools_server.py` + `web/index.html`，共约 2825 行 Python。
体检视角：单一职责（SRP）、高内聚低耦合、接口分离（ISP）、开闭（OCP）、依赖倒置（DIP）。
目的：在进入「通用能力层 / 并发扩展 / 可信安全」下一期之前，确认地基是否稳。

## 总体结论

**架构健康度：良好。** 分层方向正确（内核 → 平台包 → 领域包三层能力模型）、注册中心模式统一、
fail-closed 贯穿全链路，扩展性骨架已经立住。但有 **1 个高优先级问题** 和 3 个中优先级问题，
其中 P1（agent.py 上帝文件 + 全局可变状态）正是下一期「无状态化 / 多副本扩展」的直接障碍，
建议在写新功能之前先还这笔债。

## 现状分层

```
web/index.html (263 行)          前端：SSE 对话 + 审批卡片 + 用量展示
        │ HTTP/SSE
server/main.py (127)             网关：路由、会话字典、CORS
        │
server/agent.py (937)  ⚠️         图构建 + 15 个管理工具 + 流式编排 + Mock 演示
        │
        ├── server/skills.py (138)        技能注册中心（内核 + 领域包多来源）
        ├── server/custom_tools.py (275)  自定义工具注册中心 + 沙箱/本地执行
        ├── server/packs.py (432)         领域包注册中心（复用 custom_tools 构建器）
        ├── server/weather.py (139)       天气降级链（内置工具与 MCP 共用）
        └── server/multi_agent.py (153)   编排器-工人多 Agent
        │
server/approval.py (54)          审批中枢（内存 PENDING + fail-closed）
server/usage.py (184)            Token 计量（contextvars 归属 + SQLite）
server/telemetry.py (99)         Langfuse 观测（惰性初始化 + 降级安全）
server/config.py (72)            配置集中
sandbox/server.py (141)          沙箱执行服务
tools/local_tools_server.py (74) MCP 示例服务器
```

## 逐项体检

### 1. 单一职责（SRP） ⚠️

| 模块 | 评级 | 证据 |
|---|---|---|
| config / approval / usage / telemetry / weather / skills | ✅ | 每个文件一个明确的职责边界，点名即可说清 |
| custom_tools | ✅ | 校验、构建、执行模式判定都在「自定义工具」这一个语义内 |
| packs | ✅ | 432 行但全部围绕「包」的生命周期与加载 |
| multi_agent | ✅ | 153 行只做编排器-工人模式 |
| **agent.py** | ❌ | **937 行混合 6 个职责**（见 P1） |
| main.py | ⚠️ | 路由注册之外还持有 SESSIONS 内存态（已知 POC 取舍，见 P2） |
| web/index.html | ⚠️ | 263 行单文件前端，POC 规模可接受；超过 500 行就应拆 |

### 2. 高内聚低耦合 ⚠️

做得好的：

- **三个注册中心同构**：skills / custom_tools / packs 都是「扫描目录 → 索引 → 加载 → 变更检测」，
  心智模型统一；packs 复用 `custom_tools.build_tool()`（custom_tools.py:234），没有复制实现。
- **weather.py 一处实现、两路消费**：内置工具与 MCP 服务器共用降级链
  （tools/local_tools_server.py:24），行为一致性有保证。
- **usage.py 用 contextvars 做归属**（不侵入业务函数签名），telemetry.py 惰性初始化、
  Langfuse 不可达时零影响降级——横切关注点没有污染业务模块。

问题：

- **agent.py 与所有下游模块双向耦合**：它既是调用方（构建图时读各注册中心），
  又在自己内部定义了调用各注册中心的管理工具，还持有全部全局状态。
  改任何一个注册中心的接口，都要在 agent.py 里改两处（构建逻辑 + 管理工具）。
- **Mock 演示剧本硬编码在 agent.py 656–940 行**（约 280 行、占文件近 1/3），
  与生产链路共用 `stream_reply` 的 SSE 出口，演示逻辑变更会触碰生产文件。

### 3. 接口分离（ISP） ✅（一处小瑕疵）

- 各注册中心暴露的接口小而正交：`list_skills/load_skill/create_skill/skill_index_prompt`、
  `validate/write/delete/build_tool/load_langchain_tools`——调用方只依赖自己用的那几个函数。
- 小瑕疵：`custom_tools.build_tool(path, pack, env)` 的 `pack`/`env` 参数对纯自定义工具调用方
  无意义（默认空值），是 packs 复用时塞进来的。可接受，但若再长出一个消费方就值得拆
  「签名提取」与「执行包装」两个独立接口。

### 4. 开闭（OCP） ✅

框架的立身之本，目前是最强的一项：

- 新增技能 = 放一个 SKILL.md 目录；新增工具 = 放一个 .py 文件；新增领域 = 放一个包目录。
  **全部不需要改内核代码**，热重载靠签名比对自动生效。
- 审批样板有重复（见 P3），但那是实现细节，不影响扩展方式本身。

### 5. 依赖倒置（DIP） ⚠️

- 上层（agent.py）直接 import 下层具体模块，没有抽象层。POC 阶段这是务实选择：
  注册中心本身就是稳定接口，抽象早了反而是过度设计。
- 真正的问题不是缺抽象，而是**状态没有属主**（见 P1/P2）——这才是阻碍替换和扩展的耦合。

## 问题清单

| 编号 | 严重度 | 位置 | 问题 | 建议 |
|---|---|---|---|---|
| **P1** | 🔴 高 | server/agent.py 全文 | **上帝文件 + 8 个模块级可变全局**（`_agent/_llm/_checkpointer/_mcp_tool_list/tool_names/_mcp_tools/_pack_tools/_custom_sig`）。多副本部署时每个 worker 状态漂移；热重载与并发请求共用全局，存在竞态窗口 | ① 拆文件：`tools_builtin.py`（内核工具）、`tools_admin.py`（管理工具）、`mock.py`（演示剧本），agent.py 只留图构建 + stream_reply；② 引入 `AppState` 类集中持有全局，由 lifespan 构建/销毁，为无状态化铺路 |
| **P2** | 🟡 中 | main.py / approval.py | SESSIONS 会话字典与 PENDING 审批字典均为单进程内存态。已知 POC 取舍，但它是「用户量增长 → 多副本」路线上的硬墙（会话粘在单机上，审批跨副本丢失） | 下一期无状态化时一并解决：会话走 checkpointer（已有 SQLite/Redis 抽象）、审批挂到持久存储 + 轮询/通知。本期内不单独动 |
| **P3** | 🟡 中 | agent.py:75-83, 134-142, 157-164, 205-212, 233-240, 274-283, 304-311 | **7 处几乎相同的审批样板**（interrupt → GraphBubbleUp 放行 → decision 判断，约 20 行/处，共约 140 行）。新增一个需审批的工具就得抄一遍，抄错一处就是安全洞 | 抽一个 `_approved(action_desc, payload, fn)` 公共包装，7 处收敛为 7 行调用。零行为变化，P1 拆文件时顺手做 |
| **P4** | 🟢 低 | custom_tools.py:234 | `build_tool` 的 pack/env 参数对单一调用方有冗余（见 ISP 小节） | 暂不动，等出现第三个消费方再拆 |
| **P5** | 🟢 低 | web/index.html | 单文件前端 263 行，目前可维护 | 设 500 行红线，触线再拆 |

## 明确不建议现在动的

- **tools/local_tools_server.py 保留**：它是 MCP 接入的演示与联调样本，74 行成本极低。
- **Mock 演示剧本保留**：无 GPU/无密钥环境下它是唯一可演示路径，有独立价值——
  但应该搬到独立文件，不再与生产链路共居。
- **不引入插件框架/抽象基类**：三个注册中心已经够用，形式化抽象等第四个同类出现时再做。

## 与下一期的衔接

用户已定的下一步：①并发可优雅扩展（无状态化）；②通用能力层归属；③可靠/可信/安全 + token 计量增强。

体检结论对路线的影响：

1. **P1 是无状态化的前置**——全局态不收口，多副本无从谈起。建议把「P1 重构 + P2 会话/审批持久化」
   合并为下一期的第一步，纯重构不新增功能，风险可控。
2. **通用能力层**可以直接复用现有注册中心模式：内核只留生命周期管理，
   「各领域都可能用到的基础能力」进平台包（v0.12 的 core-utils 已验证这条路）。
3. **可信安全**方面，fail-closed 已经贯穿（沙箱/审批/env/模块缺失/checkpointer 降级），
   下一期补的是纵深：包签名、可信来源、工具级权限声明。

## 交付说明

本报告为纯评审，**未改动任何业务代码**。最新交付包仍为任务根目录的
`private-agent-poc-v0.12.0.zip`（不含本报告）。
