# 安全加固 v0.17.39 spec：SSO 身份一致性（请求级身份贯通执行层）

日期：2026-11-02　来源：11-01 架构审核报告 §5（P1，docs/reviews/2026-11-01-v0.17.36-architecture-review.md）+ 复核报告 §2 P1 保持开放声明（docs/reviews/2026-11-01-v0.17.37-commit-rereview.md）+ PLAN.md 后续轮次待办 #1。

## 一、背景与目标

HTTP 层与 Agent 工具执行层对**同一请求**的角色/部门判定口径不一致：

- HTTP 层：`resolve_identity()`（server/auth.py L421-447）让受信 SSO 部门头**覆盖**本地表部门；kb 原文端点等用 `request.state.user` 判定（server/main.py L1275-1284）。
- 执行层：`usage.current_user` ContextVar 只携带**用户名**（server/agent.py L534）；`auth.current_role()`（auth.py L381-390）与 `auth.current_department()`（auth.py L393-404）再**按用户名重查本地表**重建角色/部门。
- 执行层消费方：提示词装配（agent.py L144-145 `_dynamic_prompt`）、包级 RBAC（custom_tools.py L427/457/559/644、packs.py L654）、知识库工具（tools_builtin.py L67）、技能域（skills.py L276、tools_admin.py L97/115）。

**后果**：调岗后 HTTP 端点按 SSO 新部门拒绝、工具/提示层按本地旧部门放行——撤权不及时，同一用户对同一知识库得到不同授权结果，破坏最小权限。

**目标**：请求级不可变身份（username/role/department/auth_source）贯通中间件 → 端点 → 执行层；执行层禁止按用户名重查重建；本地表不得覆盖已认证的 SSO 请求声明；SSO 部门空值按明确 fail-closed 规则处理。

**用户补充关注点（并入范围）**：SSO 头 / Bearer / HttpOnly Cookie 三凭据共存时的解析优先级一致性（头部不得被已登录 Cookie 身份覆盖或混用）；认证中间件与各端点身份判定口径统一（端点只用 `request.state.user`，chat 链路把同一份身份传入执行层）。

**不做（显式排除）**：共享技能审批收口与高风险审批职责分离（待办 #2）、存量混合归属收紧（待办 #3）、Cookie 生产化（待办 #4）、真实浏览器 E2E（待办 #5）、6.2 部门报表的查询侧 join 口径（报表读取不属于请求身份链路）。

## 二、现状证据（当前 HEAD 行号已核实）

| 层 | 位置 | 口径 |
|---|---|---|
| 中间件 | main.py L120-168 | 优先级 SSO 头 > Bearer > Cookie；SSO 用户不存在/吊销 → 401 不回退；Bearer 失效回退 Cookie（v0.17.37 迁移行为）；非白名单来源身份头忽略（v0.17.4 F2 fail-closed） |
| 身份解析 | auth.py L421-447 | SSO 部门头**非空才覆盖**本地表（头空 → 落回本地，与「IdP 权威」矛盾） |
| 端点 | main.py 各端点 | 统一用 `request.state.user`（_acl_identity L631-638 仅透传），口径一致 |
| 执行层注入 | agent.py L534 | 只 `usage.current_user.set(user_id)`——角色/部门未传递 |
| 执行层重建 | auth.py L381-404 | `current_role/current_department` 按用户名重查本地表（每次调用一次 SQLite 连接+SELECT） |

## 三、方案

### 3.1 请求级不可变身份载体（usage.py）

```python
# usage.py（与 current_user 并列，同模块避免新增循环依赖）
@dataclasses.dataclass(frozen=True)
class Identity:
    username: str
    role: str
    department: str
    auth_source: str          # "sso" / "bearer" / "cookie" / "open"

current_identity = contextvars.ContextVar("usage_identity", default=None)
```

- frozen dataclass：执行层拿到的是不可变快照，禁止跨请求共享可变身份对象（报告修复建议 4）。
- 默认值 None：非对话路径（启动/后台扫描/CLI）无身份，走既有契约。

### 3.2 中间件/端点：同一份身份贯通（main.py）

- chat 端点在 `request.state.user` 基础上构造 `Identity`（auth_via "header"→"sso" 映射；开放模式→`Identity("anonymous","admin","","open")`），作为新参数传入 `agent.stream_reply(..., identity=ident)`。
- 端点身份判定维持只读 `request.state.user`（已统一，本轮加测试固化，见 AC5）。

### 3.3 执行层注入（agent.py）

- `stream_reply` 在现有 `usage.current_user.set(user_id)` 处同时 `usage.current_identity.set(identity)`。
- `identity` 参数为 None（测试/程序化直调）时按现规则从本地表构造（认证模式）或开放模式默认——HTTP 链路恒传，回退仅服务直调路径。
- 并发语义：asyncio.create_task / asyncio.to_thread 默认复制 contextvars → worker 与并发请求各自持有本请求快照，天然不串用（AC4 固化）。

### 3.4 执行层只读请求身份（auth.py）

`current_role()` / `current_department()` 改判：

```
identity = usage.current_identity.get()
if identity is not None:            # 对话上下文：以请求身份为准，禁止查表重建
    return identity.role / identity.department
# 非对话路径维持现契约（不变）：
# current_role：开放/匿名 → "admin"；current_department：开放/匿名 → ""
```

- `user_by_name()` 保留（管理端点与 resolve_identity 的正常查询路径），执行层不再调用。
- 附带收益：消除执行层每次调用的 SQLite 连接+SELECT（原每个受限工具判定/每轮提示词装配各一次）。

### 3.5 SSO 部门空值 fail-closed（auth.py resolve_identity）

配置了 `AUTH_IDENTITY_DEPARTMENT_HEADER` 且经 SSO 头认证的请求：

```python
if header_user:
    ident = user_by_name(header_user)
    if ident is not None and config.AUTH_IDENTITY_DEPARTMENT_HEADER:
        ident["department"] = header_department.strip()[:64]   # 空即空，IdP 权威，不回退本地
    return ident
```

- 空部门 = 不命中任何部门白名单（custom_tools/packs/kb 现有 fail-closed 语义直接生效）。
- 未配置部门头 → 维持本地表（部署未启用 SSO 部门透传）。
- Bearer/Cookie 路径 → 本地表（无 SSO 声明，现行为）。

### 3.6 不变量（测试固化）

1. 同一请求内 HTTP 层与执行层 role/department **必然相等**（同源 Identity）。
2. SSO 声明优先于本地表（含空值）；非 SSO 凭据不受 SSO 头影响（头仅在白名单+SSO 分支生效）。
3. 执行层不触发 `user_by_name` 查询（AC6 用 monkeypatch 爆炸证明）。
4. 并发请求身份互不串用（ContextVar 隔离）。

## 四、验收标准（红测试先行，tests/test_sso_identity_consistency.py）

对应报告 §5「必补验收」4 条 + 用户补充凭据一致性：

- **AC1 调岗全口径生效**：本地表部门 HR + SSO 头 Finance → 对话内 `current_department()=="Finance"`、`current_role()` 取请求身份；`_dynamic_prompt` 包片段按 Finance 过滤；kb 原文端点同值（HTTP/执行层一致）。
- **AC2 撤权即时性**：本地表仍为 HR，SSO 头 Finance → HR 部门白名单包的提示词片段与工具判定**不**放行（无需等本地表同步）。
- **AC3 空部门 fail-closed + 三路径对比**：已配置部门头且 SSO 头部门为空 → `department==""`，部门白名单包拒绝（不回退本地 HR）；同用户 Bearer 登录 → 本地 HR 生效；开放模式 → 既有放行契约。
- **AC4 并发隔离**：两并发对话（SSO Finance / SSO HR）→ 各自 `current_department()` 全程不串值。
- **AC5 凭据优先级一致性**：①SSO 头+有效 Cookie 共存 → SSO 身份生效；②SSO 头用户不存在+有效 Cookie → 401 不回退；③Bearer 失效+Cookie 有效 → Cookie 身份（v0.17.37 迁移行为固化）；④非白名单来源 SSO 头+Cookie → 头忽略、Cookie 生效；⑤三凭据请求的 `request.state.user` 与执行层 Identity 同源一致。
- **AC6 执行层禁查表**：monkeypatch `auth.user_by_name` 抛异常 → 对话内 `current_role/current_department` 正常返回请求身份值。

## 五、回归门槛

- 全量 `.venv\Scripts\python.exe -m pytest -q`：**≥661 passed / 0 failed**（新增 AC 用例全过，18 skipped 语义不变）。
- 重点守护：test_auth.py / test_auth_cookie.py / test_deny_paths.py（凭据优先级既有行为）、test_agent_llm.py（stream_reply 签名兼容——identity 为可选参数，直调路径回退本地表构造）。

## 六、测试影响面与风险

- 新增 tests/test_sso_identity_consistency.py（AC1-AC6）；conftest.py 的 isolated_env 视需要复位 `usage.current_identity`（ContextVar 默认 None，防跨测试泄漏）。
- 风险 1：非对话路径（后台扫描/worker）`current_identity` 为 None → 维持现契约（current_role→admin 开放契约），已在 3.4 声明，行为不变。
- 风险 2：tools_admin/custom_tools 中 role=="user" 的专属域判定改以请求身份 role 为准——SSO 透传角色与本地不一致时以 SSO 为准（与「认证在外、授权在内」原则的张力：角色仍查本地表是 v0.16.0 设计声明，本轮**不改变角色来源**，只保证同一请求内同源；角色以本地表为权威的设计不变——SSO 头不含角色，本地表是唯一角色来源，故角色一致性天然成立）。
- 测试数据：SSO 头认证需 monkeypatch `config.AUTH_IDENTITY_HEADER` + `config.AUTH_PROXY_ALLOWED_IPS`（test_deny_paths.py 已有同款模式可沿用）。

## 七、设计澄清记录

1. **角色来源不变**：报告要求「角色和部门均以同一份认证结果为准」——本系统角色权威源是本地表（SSO 头只透传用户名+部门，v0.16.0「认证在外、授权在内」），故「同一份认证结果」= resolve_identity 输出的 dict（角色来自本地表、部门按 3.5 规则）；执行层改读该结果的快照而非重查，即满足。
2. **usage.current_user 保留**：计量/审计/会话归属的用户名来源不动；current_identity 是角色/部门的权威来源，两者 username 同源构造。
