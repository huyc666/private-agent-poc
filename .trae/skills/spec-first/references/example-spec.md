# Spec — 1.2 包级 RBAC（示例）

> 本文件是 spec-first 流程的填写示例，以已交付的 PLAN 1.2（v0.17.18）为实例，
> 展示四部分结构的实际粒度。新任务写 spec 时参照此粒度，不要照抄内容。

## 1. 背景与目标

出处：docs/PLAN.md「P1 权限体系收尾」1.2 行。

现状：包（pack）对所有角色一律注入提示词、其工具所有角色均可调用，无法按角色
收敛能力边界。目标：pack.yaml 支持 `allowed_roles` 白名单——未授权角色的会话
看不到该包的工具提示，直接调用也被执行层拒绝。

## 2. 需求分析与约束条件澄清

**功能边界**

- 做：解析 pack.yaml 的 `allowed_roles` 字段；提示层按当前角色过滤包片段；
  工具调用入口执行侧拦截。
- 不做：用户级 / 单工具粒度 RBAC（另立任务）；管理界面配置；审计记录。

**约束（沿用项目既有安全约定）**

- fail-closed：空白名单、非法角色名、非列表类型 → 整包标记错误，所有角色不可用
- 未配置 `allowed_roles` → 所有角色可用（向后兼容，存量包零改动）
- 双层拦截：提示层过滤防误触，执行层拦截才是权限边界（防提示词注入绕过）
- 降权即时生效：包片段每轮按当前角色动态注入，不做静态绑定缓存

**待澄清问题（已在 spec 确认环节向用户核实）**

- 问：未配置 `allowed_roles` 时语义？答：所有角色可用，不视为配置错误。
- 问：`allowed_roles: []` 语义？答：按配置错误走 fail-closed，而非「全部禁用」。

## 3. 关键技术与选型决策

| 决策点 | 候选方案 | 选定 | 取舍理由 |
|---|---|---|---|
| 拦截层次 | 仅提示层 / 仅执行层 / 双层 | 双层 | 提示层防误触但不防注入；执行层是最终权限边界 |
| 配置错误语义 | 静默忽略非法值 / fail-closed 整包标错 | fail-closed | 静默不可用难排查，配置错误应显式暴露 |
| 片段注入时机 | 图构建时静态绑定 / 每轮动态注入 | 每轮动态（agent.py `_dynamic_prompt`） | 降权即时生效，无需重建图 |
| 角色来源 | 会话建立时快照 / 每轮实时读取 | 实时读取（`auth.current_role()`） | 与双层拦截的即时性一致 |

沿用：packs.py 现有加载链路、custom_tools.py 的 guard 包装点、tests/conftest.py
离线隔离体系；不引入新框架。

## 4. 验收标准

| # | 标准（Given/When/Then） | 验证方式 |
|---|---|---|
| AC1 | Given 包未配置 allowed_roles，When 任一角色会话组装工具清单，Then 包片段与工具可见、可调用 | 单测 |
| AC2 | Given 包设 `allowed_roles: [approver]`，When user 会话组装清单，Then 该包片段与工具不出现在提示中 | 单测 |
| AC3 | Given 同 AC2，When user 绕过提示层直接调用该包工具，Then 返回权限拒绝（执行层拦截） | 单测（拒绝路径） |
| AC4 | Given `allowed_roles: []` 或含未定义角色名或非列表值，When 加载包，Then 整包标记错误，所有角色均不可用 | 单测（fail-closed） |
| AC5 | Given 运行中的 approver 会话，When 管理员收窄包的 allowed_roles 后进入下一轮对话，Then 该包工具对该会话不再可见可调用 | 单测 |

安全/拒绝路径：AC2/AC3/AC4 成对覆盖提示层与执行层的拒绝侧，不留放行-only 盲区。

测试对应：`tests/test_pack_rbac.py`，用例名与 AC 编号对应
（test_ac1_unset_roles_all_allowed、test_ac2_user_prompt_filtered、
test_ac3_user_call_denied、test_ac4_empty_roles_fail_closed …），
spec 确认后先写用例跑红，再实现转绿。
