# API 认证与多用户设计方案 · Private Agent POC

版本：v1.1（**已全部实施**：v0.14.0 认证基座 / v0.14.1 ACL / v0.14.2 Key 轮换 /
v0.15.0 密码登录 / v0.16.0 SSO 身份头 / v0.17.4 身份头来源白名单） · 2026-10-07
关联：CODE_REVIEW.md P2-1（API 无认证 + 审批无身份绑定）——已核销

## 一、背景与问题

当前框架的「用户」维度完全由调用方自报，服务端不做任何校验：

| 现状 | 风险 |
|------|------|
| `/api/chat` 的 `user_id` 由请求体自由填写 | token 计量归属可伪造，成本核算失真 |
| `/api/approve` 任何人可决议任何 pending 审批 | 危险操作的审批门形同虚设（内网任何能访问端口的人都可批准删除/建工具） |
| 审批记录无创建人/决议人字段 | 审计链断裂：出了问题无法回答「谁批的」 |
| 服务默认绑 `0.0.0.0` 且无认证 | 内网全可达，等于把 Agent 能力（含写文件、执行代码）开放给整个网段 |

约束（不破坏现有优势）：
- **私有化零额外组件**：不引入外部 IdP/Redis；存储继续用 SQLite
- **平滑过渡**：POC 演示场景可一键回到开放模式
- **前端零构建**：单文件 HTML，认证交互要足够轻

## 二、设计目标

1. 所有 API 调用可归属到**服务端确认的身份**（不再是客户端自报）
2. 审批的创建人、决议人、决议时间全部落库可审计
3. 决议权限可控：不是「任何人都能批」
4. 开放模式（AUTH_ENABLED=false）行为与现状完全一致，演示不受影响

非目标（本期不做）：
- SSO / LDAP / OAuth2 企业集成（预留接口，见 §七）
- 会话级数据隔离的完整 RBAC（本期只做「身份 + 角色」，不做资源级 ACL）
- 前端多用户协作界面

## 三、方案选型

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| A. API Key（Bearer） | 零组件、CLI 可管、与私有化部署习惯一致 | 无登录页体验 | ✅ 本期采用 |
| B. 账号密码 + Session/JWT | 体验好 | 要维护口令散列、会话表、登录页 | 二期可选 |
| C. 直接对接企业 SSO | 最省事（对运维） | 每家 IdP 不同，POC 阶段过度设计 | 预留钩子 |

**核心决策：每人一把 API Key，HTTP Bearer 认证；服务端中间件解析身份并注入请求上下文。**
与现有架构的衔接点现成：`usage.py` 已用 contextvars 传递 `current_user`，
认证中间件只需成为这个值的**唯一可信来源**。

## 四、详细设计

### 4.1 角色模型

| 角色 | 能力 |
|------|------|
| `user` | 聊天、查看自己的会话与用量、创建审批（触发危险操作请求） |
| `approver` | 同 user + **决议任何待审批**（批准/拒绝） |
| `admin` | 同 approver + 用户管理（签发/吊销 Key）、查看全量会话/审批/用量 |

设计意图：把「使用 Agent 的人」和「有权批准危险操作的人」分开——
这正是「人在回路」要落地为多用户时的最小角色拆分。
资源级隔离（只能看自己的会话）本期只对普通 `user` 生效；approver/admin 为全局视图。

### 4.2 凭据存储（SQLite，STATE_DB 同库）

```sql
CREATE TABLE IF NOT EXISTS api_users (
    username   TEXT PRIMARY KEY,     -- 登录名/标识，如 "zhangsan"
    key_hash   TEXT NOT NULL,        -- sha256(api_key)，明文不落库
    role       TEXT NOT NULL,        -- user / approver / admin
    created    REAL NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);
```

- Key 格式：`pak-<32位hex>`（private-agent-key 前缀，日志中易识别、防误提交）
- 只存散列：Key 只在签发时展示一次
- 吊销不删除：保留审计痕迹，中间件拒绝 `revoked=1`
- 管理 CLI：`python tools/manage_users.py add zhangsan --role approver` /
  `revoke zhangsan` / `list`（复用 sign_pack.py 的 CLI 模式）

### 4.3 认证中间件（FastAPI）

```
请求 → Authorization: Bearer pak-xxx
     → 查 key_hash → 命中且未吊销 → request.state.user = {username, role}
     → 未启用认证（AUTH_ENABLED=false）→ user = {anonymous, admin}（现状行为）
     → 启用但缺失/无效 → 401 {error: "unauthorized"}
```

- 豁免端点：`/`（页面）、`/api/health`（健康检查脱敏：开放模式返回全量，
  认证模式未登录只回 `{"status":"ok"}`）
- **user_id 不再信任请求体**：`/api/chat` 的 user_id 一律取中间件注入值，
  请求体里的 user_id 字段直接忽略（保留字段不报错，平滑兼容旧客户端）
- contextvars 衔接：`stream_reply` 开头 `usage.current_user.set(user_id)`
  的值改为来自中间件，计量归属即刻可信，usage.py 零改动

### 4.4 审批身份绑定（核心安全修复）

`approvals` 表加列（启动时 `ALTER TABLE ... ADD COLUMN`，旧库自动迁移，旧记录为 NULL）：

```sql
ALTER TABLE approvals ADD COLUMN user_id TEXT;      -- 创建人（谁触发的危险操作）
ALTER TABLE approvals ADD COLUMN session_id TEXT;   -- 来源会话
ALTER TABLE approvals ADD COLUMN decided_by TEXT;   -- 决议人（谁批的/拒的）
```

- **创建**：`create_approval(tool, args)` 内部改从 contextvars 取
  `current_user/current_session`（agent.py 与 mock.py 调用点零改动）
- **决议权限**（`/api/approve`）：
  - `admin` / `approver`：可决议任何 pending
  - `user`：只能决议 **自己创建的** pending（自己的危险操作自己确认，
    覆盖单用户场景直觉）——但**不能决议他人的**
  - 无权限 → 403；记录不存在/已决议 → 409（现状 ok=false 语义细化）
- **审计**：`resolve()` 写入 `decided_by`；`/api/approvals` 返回创建人/决议人
- 开放模式：一切如现状（任何人可决议），前端行为不变

### 4.5 前端改动（单文件 HTML）

- 首次打开：无有效 Key 时弹出一次性输入框，存 `localStorage('poc_key')`，
  之后所有 fetch 带 `Authorization: Bearer ...`
- 用户徽章从「点击自由改名」改为「显示服务端身份」（`/api/health` 认证后回显
  username + role）；Key 失效（401）时清缓存重新询问
- 审批面板条目显示 创建人/决议人；无权限决议时按钮置灰并提示
- 开放模式：不出现 Key 输入框，体验与现状完全一致

### 4.6 配置项（.env）

```bash
# ---- API 认证（v0.14.0）----
AUTH_ENABLED=               # 留空 = 联动运行模式（v0.17.6）：Mock 模式默认免登录（开发者模式），
                             # 真实 LLM 模式默认要求登录；显式 true/false 可覆盖默认
AUTH_BOOTSTRAP_ADMIN=       # 首次启动时若用户表为空，以此为用户名创建 admin 并打印一次性 Key
```

生产清单（PRODUCTION.md）同步增加一节：开启认证、引导 admin、为每个用户签发 Key、
按需提权 approver。

## 五、实施拆分（建议按序提交）

| 步骤 | 内容 | 影响面 |
|------|------|--------|
| 1 | `server/auth.py`：用户表 + 中间件 + contextvars 注入 | 新增模块 |
| 2 | `main.py`：挂中间件、user_id 改可信源、health 脱敏 | 入口 |
| 3 | `approval.py`：三列迁移 + 创建绑定 + 决议权限 + decided_by | 审批 |
| 4 | `tools/manage_users.py`：签发/吊销/列表 CLI | 新增 |
| 5 | `web/index.html`：Key 输入/携带、身份徽章、审批面板身份列 | 前端 |
| 6 | 文档：README 版本记录、PRODUCTION.md 认证一节、CODE_REVIEW 核销 P2-1 | 文档 |

每一步独立可测；步骤 1-3 完成后后端即安全，前端可随后跟上。

## 六、验证计划

1. 开放模式回归：现有冒烟脚本全过（行为零变化）
2. 认证模式：无 Key → 401；错误 Key → 401；有效 Key 聊天 → 用量归属正确
3. 越权：user A 的 Key 决议 user B 创建的审批 → 403；approver 决议 → 200
4. 审计：决议后 `decided_by` 落库，`/api/approvals` 可见创建人/决议人
5. 吊销：吊销后旧 Key 立即 401
6. 迁移：用 v0.13.1 的旧 state.db 启动，表结构自动升级、旧审批记录可决议

## 七、后续演进（本期不做）

- ~~**SSO 钩子**：中间件抽象为 `resolve_identity(request) -> User`，
  企业接入时替换为解析反向代理注入的身份头（如 X-Forwarded-User）或 OIDC~~（**已落地**，v0.16.0：`auth.resolve_identity` 成为身份解析唯一入口，内置反向代理身份头模式（配置 `AUTH_IDENTITY_HEADER`，认证在外授权在内、用户不存在 fail-closed），扩展 OIDC 等只需在该函数加分支）
- ~~**账号密码登录**：同一用户表加 password_hash 列即可平滑升级~~（**已落地**，v0.15.0：PBKDF2 加盐散列、POST /api/auth/login 签发 pat- 短期令牌（默认 12h，AUTH_TOKEN_TTL_HOURS 可调）、/api/auth/logout 注销、吊销用户同步清除令牌、前端登录框（账号密码 / API Key 双入口）、`passwd` CLI；登录失败不区分原因防用户名枚举）
- ~~**资源级 ACL**：会话/审批的所有权过滤~~（**已落地**，v0.14.1：user 角色的会话列表/历史/删除、审批列表、用量统计均按本人过滤，越权 403）
- ~~**Key 轮换与过期**：加 expires_at 列 + 自动过期~~（**已落地**，v0.14.2：`--days` 签发限期 Key、`rotate` 换发新 Key 旧 Key 立即失效、过期 Key 一律 401、`list` 显示有效期与状态）
