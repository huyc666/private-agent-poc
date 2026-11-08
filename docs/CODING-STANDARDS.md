# 编码规范（CODING-STANDARDS）

本规范从仓库现有代码的真实做法中提炼，每条规则附仓库内实例（文件路径 + 行号）。
规则分三档：

- **强制（must）**：违反即视为缺陷，评审必须拦下。
- **建议（should）**：新代码默认遵守，存量代码不强求回填。
- **现状/待收敛**：当前代码普遍不统一或暂不执行的方向，仅作记录，不写成硬性规则。

版本基线：v0.17.40（commit `d5fb5cc`）。行号随代码演进可能漂移，引用时以语义为准。

---

## 1. Python 后端代码风格

### 1.1 命名

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 模块、函数、变量用 `snake_case`；配置常量与模块级开关用 `UPPER_CASE` | [config.py](file:///e:/codebase/GitHub/private-agent-poc/server/config.py#L19-L24) `MOCK_LLM / LLM_BASE_URL / MAX_TOKENS` |
| must | 模块内部使用的辅助函数加 `_` 前缀，不进入对外契约 | [agent.py](file:///e:/codebase/GitHub/private-agent-poc/server/agent.py#L130-L155) `_dynamic_prompt`；[usage.py](file:///e:/codebase/GitHub/private-agent-poc/server/usage.py#L83-L99) `_num` |
| must | 不可变记录对象用 `@dataclasses.dataclass(frozen=True)`；可变运行时状态用普通 `@dataclass` | [usage.py](file:///e:/codebase/GitHub/private-agent-poc/server/usage.py#L43-L56) `Identity`（frozen）；[state.py](file:///e:/codebase/GitHub/private-agent-poc/server/state.py#L14-L27) `AppState` |

### 1.2 类型标注

| 级别 | 规则 | 实例 |
|---|---|---|
| should | 新函数标注参数与返回类型，尤其是跨模块调用与工具函数 | [agent.py](file:///e:/codebase/GitHub/private-agent-poc/server/agent.py#L34-L50) `parse_citations(...) -> tuple[str, list \| None]` |
| should | 联合类型用 PEP 604 写法（`str \| None`），不用 `Optional[str]` | 同上；[auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L416) `remote_host: str \| None` |
| 现状/待收敛 | ~~存量标注覆盖不均~~ 批 4 后半已回填：server/+sandbox 覆盖率约 88%（317/362，单行签名口径）。残留缺口为有意留空——返回第三方对象或动态结构处标 `Any`/留空（如 `usage.UsageCallback.handler`、`tools/build_kb._get_engine`），不猜不错标。注意：FastAPI 端点若返回 dict 与 Response 子类的混合 union，注解只能写 `-> Any`（union 含 Response 子类会让 FastAPI 响应建模报错） | — |

### 1.3 异常处理

| 级别 | 规则 | 实例 |
|---|---|---|
| must | **对外三通道零容忍拼接原始异常 `{e}`**：HTTP 响应/SSE 流/页面可见文本、工具返回给模型的文本、审计记录（这三类受众可能离开部署机）。返回固定文案或归类原因（OSError 等会携带服务器绝对路径）。**本机日志通道**（uvicorn/应用日志，只落部署机磁盘）允许保留原始异常文本——日志不出内网、排障需要完整堆栈，但禁止外发；外泄面由 §1.4 print 禁令与 §2.6 密钥条款覆盖。**存量 5 处对外通道命中列一期补齐**（非永久豁免）：custom_tools.py:195、packs.py:112/385、skills.py:506、weather.py:133，逐一改为类型名+泛化文案（参照 tools_builtin.py:248 先例），每处配「错误文本不含绝对路径/异常原文」断言用例 | [tools_builtin.py](file:///e:/codebase/GitHub/private-agent-poc/server/tools_builtin.py#L91-L111) `read_text_file` 捕获通用异常返回标准化错误文本；[tools_builtin.py](file:///e:/codebase/GitHub/private-agent-poc/server/tools_builtin.py#L204-L248) `save_output_file` 区分 OSError 与通用异常；通道口径详见 ENGINEERING-METRICS.md S4 |
| must | 计量、审计等旁路回调的失败不得阻塞主链路：`except Exception` 吞掉并降级 | [usage.py](file:///e:/codebase/GitHub/private-agent-poc/server/usage.py#L193-L219) `UsageCallback` 明确"任何回调内异常吞掉" |
| must | 安全判定路径的异常必须 fail-closed（拒绝），不得 fail-open 放行；参见 §2 | [auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L416-L427) `proxy_ip_allowed` 白名单未配置/非法 IP 均返回 False |
| should | HTTP 层错误统一用 `HTTPException(status_code, detail=固定文案)` | [sandbox/server.py](file:///e:/codebase/GitHub/private-agent-poc/sandbox/server.py#L38-L51) `check_token` 401/503 |

### 1.4 日志

| 级别 | 规则 | 实例 |
|---|---|---|
| should | 模块级 `logging.getLogger(__name__)`，告警用 `logger.warning` 带格式化参数 | [usage.py](file:///e:/codebase/GitHub/private-agent-poc/server/usage.py#L33-L35)、[usage.py](file:///e:/codebase/GitHub/private-agent-poc/server/usage.py#L83-L99) |
| must | 日志不得输出明文密码、API Key、token、Cookie 值（对齐 §2.6） | 审计设计约束见 README v0.17.30 条目（"明文密码/Key 与绝对路径绝不入审计"） |
| must | 服务端业务代码（server/、sandbox/）一律走 `logging`，禁止 `print`；`print` 仅允许出现在 tools/ 下的独立 CLI 脚本（面向终端输出）。存量 print（如 [agent.py](file:///e:/codebase/GitHub/private-agent-poc/server/agent.py#L68-L70) 的 `[agent] ...`）触碰即改 | — |

> 豁免脚注：两类 stdout 直写不受上条约束——① sandbox/server.py 的 `print("__RESULT__" + json.dumps(...))`（L66-L68）是沙箱父子进程 IPC 协议帧（stdout 数据通道），不是日志；② [auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L545-L552) 引导管理员一次性 Key 直写 stdout（部署终端可见），系 §2.6 的明文凭据例外——该 Key 只允许存在于部署终端，禁止进入 logging/日志采集通道（2026-11-03 用户裁决方案 B）。除这两类外，server/ 与 sandbox/ 一律 logging。

### 1.5 配置读取（config.py 约定）

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 环境变量集中在 [config.py](file:///e:/codebase/GitHub/private-agent-poc/server/config.py) 读取，其他模块 `from . import config` 后按 `config.X` 引用，**不得**在业务模块里直接 `os.environ[...]` | config.py 全文件；消费侧如 [auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L454-L456) 引用 `config.AUTH_IDENTITY_DEPARTMENT_HEADER` |
| must | 每个配置项就地给默认值与类型归一（布尔 `.lower() in ("1","true","yes")`、数值 `int()/float()`） | [config.py](file:///e:/codebase/GitHub/private-agent-poc/server/config.py#L19-L24) |
| must | config 值在**调用时读取**而非 import 时固化——conftest 依赖 `monkeypatch.setattr(config, ...)` 逐测试打补丁 | [conftest.py](file:///e:/codebase/GitHub/private-agent-poc/tests/conftest.py#L48-L62) `isolated_env` 的设计前提（注释 L50） |
| must | 包级环境变量注入用 `os.environ.setdefault`（不覆盖外部显式设置），同 key 冲突必须 WARNING 告警，不得静默覆盖 | [packs.py](file:///e:/codebase/GitHub/private-agent-poc/server/packs.py#L591-L618) |
| must | 环境变量读取统一用 `os.environ.get`，不用 `os.getenv`。策略为「触碰即改」：改动到含 `os.getenv` 的代码段时顺手统一（现存混用见 [config.py](file:///e:/codebase/GitHub/private-agent-poc/server/config.py#L19-L24) vs [L73-L85](file:///e:/codebase/GitHub/private-agent-poc/server/config.py#L73-L85)），不做专项重构 | — |

### 1.6 异步边界（LangGraph 节点与 Runnable 契约）

| 级别 | 规则 | 实例 |
|---|---|---|
| must | LangGraph 节点为 `async def`，接收 state 返回部分状态 dict；图构建集中在 `build_agent` / `_rebuild_graph` | [agent.py](file:///e:/codebase/GitHub/private-agent-poc/server/agent.py#L158-L210) |
| must | async 上下文中调用同步阻塞操作（SQLite、文件 IO、锁）必须 `asyncio.to_thread(...)` 包装，不得直接阻塞事件循环 | [session_lock.py](file:///e:/codebase/GitHub/private-agent-poc/server/session_lock.py#L122-L160) `SessionLease.__aenter__/__aexit__` 用 `to_thread` 包 `acquire/renew/release` |
| must | 实现/代理 LangGraph 组件接口（如 Checkpointer）时必须**透传返回值**，不得吞掉基类契约返回值；`alist` 等 async generator 不得包装成 coroutine | [agent.py](file:///e:/codebase/GitHub/private-agent-poc/server/agent.py#L280-L338) `FencedCheckpointer`（v0.17.40 修复的两项缺陷即此规则的反例教训） |
| must | 委托代理（wrapper）对基类未实现的占位方法要批量显式委托，派生实例（`with_allowlist` 等）必须重套包装，不得静默丢栅栏 | 同上，`FencedCheckpointer` 全方法委托 |
| should | 同步/异步混用时，异步边界画在 I/O 与锁上；纯计算函数保持 sync，不为了"看起来一致"强加 async | — |

---

## 2. 安全编码硬约束

本节规则全部为 **must**。依据 [SECURITY.md](file:///e:/codebase/GitHub/private-agent-poc/docs/SECURITY.md) 与 v0.17.26–v0.17.40 加固版本的实际实现。

### 2.1 fail-closed 默认

| 规则 | 实例 |
|---|---|
| 配置缺失、非法值、解析失败时，安全判定一律拒绝（不可用/不信任），不得静默放行 | 包 RBAC：`allowed_roles/allowed_departments` 空列表或非法值 → 整包标记 `error` 全员不可用（[packs.py](file:///e:/codebase/GitHub/private-agent-poc/server/packs.py#L139-L211)）；SSO 代理白名单未配置 → 不信任任何身份头（[auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L32-L42)、[L416-L427](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L416-L427)） |
| 已启用部门头配置时，IdP 空部门头以空生效（不回退本地表）——空部门不命中任何部门白名单 | [auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L449-L457) `resolve_identity`（spec 3.5 有意取舍，部署警示见 SECURITY.md） |
| 沙箱默认要求认证 token，未配置即拒绝服务而非开放；开放模式须显式 `SANDBOX_OPEN_MODE=true` | [sandbox/server.py](file:///e:/codebase/GitHub/private-agent-poc/sandbox/server.py#L38-L51) |
| 白名单字段**未配置**与**配置为空列表**语义不同：未配置 = 不限制该维度（向后兼容）；显式空列表/非法 = 整包 error。写新白名单逻辑时保持此区分 | [packs.py](file:///e:/codebase/GitHub/private-agent-poc/server/packs.py#L139-L211) |

### 2.2 身份解析口径（Identity 快照）

| 规则 | 实例 |
|---|---|
| 请求级身份在 HTTP 中间件解析一次，构造 `usage.Identity`（frozen dataclass）放入 `usage.current_identity` ContextVar；执行层**只读快照，禁止按用户名重查用户表重建** | [usage.py](file:///e:/codebase/GitHub/private-agent-poc/server/usage.py#L43-L56)；[auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L381-L395) `current_role`、[auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L398-L413) `current_department` |
| 凭据优先级固定：SSO 头 > Bearer > Cookie。新增身份源在 `resolve_identity` 加分支，不改下游 | [auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L430-L460) |
| 非对话路径（快照为 None：测试/回放/后台）维持旧契约回退（开放模式/匿名 → admin/空），不得借回退路径绕过授权 | [auth.py](file:///e:/codebase/GitHub/private-agent-poc/server/auth.py#L392-L395) |
| 开放模式下 `identity.username` 与请求自报 `user_id` 对齐，不得固定写死 "anonymous" 造成身份分叉 | main.py 开放模式分支（v0.17.39 评审修复项） |

### 2.3 权限判定三处同口径

| 规则 | 实例 |
|---|---|
| 同一授权语义必须在**提示层**（包片段/工具清单过滤）、**执行层**（工具调用入口拦截）、**端点层**（如知识库出处定位）复用同一实现函数（`pack_scope_denied` / `kb_allowed`），禁止各层内联重写——口径漂移是历史 P0 教训 | 执行层：[custom_tools.py](file:///e:/codebase/GitHub/private-agent-poc/server/custom_tools.py#L416-L520) `_guard_pack_tool`；v0.17.26 修复记录：kb_allowed 由内联重写改为复用 `pack_scope_denied`（commit `afe40eb`） |
| 执行层拦截在工具调用入口重新判定，保证降权即时生效（不依赖提示层过滤的完整性） | [custom_tools.py](file:///e:/codebase/GitHub/private-agent-poc/server/custom_tools.py#L416-L520) |
| 越权/未声明的资源访问返回 404 而非 403+清单，防枚举旁路 | v0.17.26 出处定位端点（README 版本记录） |

### 2.4 HTML 消毒与 XSS

| 规则 | 实例 |
|---|---|
| 任何第三方/用户内容转出的 HTML（如 Mammoth docx 转换）**不得直写 innerHTML**，必须经 `POST /api/kb/sanitize` 服务端白名单消毒 | [html_sanitize.py](file:///e:/codebase/GitHub/private-agent-poc/server/html_sanitize.py#L1-L15)（stdlib 自研：标签/属性/scheme 三级白名单）；端点 [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L1224-L1247) |
| 消毒器对重复属性取"首次出现"（浏览器 first-wins 语义），校验与输出同一取值——v0.17.40 修复的绕过案例 | v0.17.40 整改记录（docs/reviews/2026-11-02-v0.17.39-8bba994-review-response.md） |
| 服务端拼接的展示页全量转义（标题/路径/正文/锚点），配 nonce CSP + X-Frame-Options | v0.17.37 P0-2（README 版本记录） |

### 2.5 领域包信任链

| 规则 | 实例 |
|---|---|
| 包清单只允许规范路径：工具平铺 `tools/<文件名>.py`、技能 `skills/<一级目录>`、提示词 `prompt.md`；非规范引用跳过并告警 | v0.17.34（README 版本记录） |
| 技能/包内路径 resolve 后必须仍在包目录内（符号链接、目录链接出界一律拒收），技能内容走物化快照、加载零磁盘读取 | v0.17.36 技能挂载同一集合（README 版本记录） |
| 变更检测签名用**内容哈希**（技能签名含 `st_mtime_ns`），不得用秒级 mtime；验签异常 fail-closed 判 invalid | v0.17.33/v0.17.34（README 版本记录） |
| 强制签名模式下未签名/篡改包标记 unavailable，不得降级加载 | [packs.py](file:///e:/codebase/GitHub/private-agent-poc/server/packs.py#L139-L211) |

### 2.6 密钥与敏感数据

| 规则 | 实例 |
|---|---|
| token/凭据比较一律 `hmac.compare_digest`（常量时间） | auth.py / sandbox token 校验（项目硬约束，v0.17.6 起） |
| 明文密码、Key、token 不进日志、不进审计、不进错误消息 | README v0.17.30 审计条目 |
| `read_text_file` / `do_delete` 拦截 `.env*`、`*.db/-wal/-shm`；文件内容中 TOKEN/KEY/SECRET/PASSWORD 等键值掩码 | [tools_builtin.py](file:///e:/codebase/GitHub/private-agent-poc/server/tools_builtin.py#L91-L111) 拦截段 |
| 上传端点先查 `Content-Length` 预检（10MB）再读 body；docx 解压限 20MB 防 zip bomb | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L891-L901) |
| 沙箱子进程不继承 `SANDBOX_AUTH_TOKEN/SANDBOX_OPEN_MODE`；沙箱容器 iptables 阻断出站 | sandbox/Dockerfile、entrypoint.sh、docker/docker-compose.sandbox.yml |

> 例外（2026-11-03 用户裁决方案 B）：引导管理员一次性 Key 允许且仅允许直写部署终端 stdout（auth.py `bootstrap_admin`），不得进入 logging/审计/错误消息；除该场景外明文凭据无例外。

---

## 3. FastAPI 与 API 层约定

### 3.1 路由组织

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 路由前缀统一 `/api/*`；资源路径 kebab/小写，路径参数做归一化校验（防空格变体绕过 ACL） | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L760-L797) 会话 REST 路由 |
| must | 认证、SSO/Bearer/Cookie 解析、CSRF 校验集中在 `@app.middleware("http")` 单点处理，端点内不重复解析凭据 | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L106-L173) |
| 现状/待收敛 | ~~main.py 单体路由~~ 已于批 4 拆分落地：端点按资源域迁至 `server/routes/`（admin_users / files / sessions / usage / kb / approvals / auth / chat），跨域共享辅助在 `server/webdeps.py`，main.py 仅保留 lifespan、认证/CSRF 中间件、静态页/health、启动迁移与 `register(app)`。**后续触发条件（仍有效）**：单路由文件超 1200 行或新增成组端点（同资源 ≥3 个新路由）时继续按域引出，不回流 main.py | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py)、server/routes/ |

### 3.2 SSE 事件格式

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 帧格式 `data: <json>\n\n`，经 `StreamingResponse(..., media_type="text/event-stream")` 返回 | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L519-L643) |
| must | 事件类型用既有词表（`session` / `token` / `citations` / `error` 等），新增类型须同步前端消费端；错误以 `error` 事件下发用户可读文案，不得只断流 | 同上；租约超时、会话删除等终局均发 error 事件（v0.17.37/38） |
| must | 流中的终局语义（删除、租约丢失）以明确事件收尾，前端据事件而非连接状态判断结局 | v0.17.38 代际 fencing（README 版本记录） |

### 3.3 错误响应与请求校验

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 请求体用 Pydantic `BaseModel` 声明，不用裸 dict | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L176-L204) `LoginRequest`；[L465-L470](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L465-L470) `ChatRequest` |
| must | `detail` 为固定文案或归类原因，不嵌 `{e}`、不泄露内部路径（同 §1.3） | §1.3 实例 |
| must | 读 body 前先 `Content-Length` 预检：超限 413、非法 400 | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L891-L901) |

### 3.4 凭据与 CSRF

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 登录令牌只进 HttpOnly Cookie（`samesite="lax"`，`secure` 随 `config.AUTH_COOKIE_SECURE`），前端不得把凭据写 localStorage | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L176-L204)；v0.17.37 凭据迁移记录 |
| must | Cookie 身份的非 GET 请求必须带 `X-Requested-With` 头（CSRF 纵深），中间件统一校验 | [main.py](file:///e:/codebase/GitHub/private-agent-poc/server/main.py#L106-L173) |
| must | 管理端点统一守卫函数（未认证 401 / 开放模式 400 / 非 admin 403），不各自手写 | v0.17.29 `/api/admin/*` 七端点 `_admin_guard`（README 版本记录） |

---

## 4. 测试规范

### 4.1 流程：red-test-first

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 依据 spec 验收标准**先写测试跑红**（功能 + 安全/拒绝路径），再实现转绿——spec-first 流程固化要求 | [.trae/skills/spec-first/SKILL.md](file:///e:/codebase/GitHub/private-agent-poc/.trae/skills/spec-first/SKILL.md#L30-L50) |
| must | 安全防线测试必须成对覆盖**放行路径 + 拒绝路径**；拒绝路径零覆盖是历史教训（"裸奔"） | [test_deny_paths.py](file:///e:/codebase/GitHub/private-agent-poc/tests/test_deny_paths.py#L1-L14) 文件级说明 + [L37-L82](file:///e:/codebase/GitHub/private-agent-poc/tests/test_deny_paths.py#L37-L82) 逐个触发拒绝分支 |

### 4.2 命名与组织

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 测试文件 `tests/test_<模块或主题>.py`；测试函数 `test_<场景>_<期望>` 描述性命名 | tests/ 全目录（test_auth.py、test_pack_rbac.py、test_sso_identity_consistency.py 等） |
| should | 同一特性的验收用例与 spec AC 编号可对应（AC1-AC6 注释或函数名体现） | [test_sso_identity_consistency.py](file:///e:/codebase/GitHub/private-agent-poc/tests/test_sso_identity_consistency.py)（8 用例覆盖 AC1-AC6） |

### 4.3 fixture 与隔离

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 测试不得触碰项目运行数据（`state.db / usage.db / uploads/ outputs/ skills/ packs/ custom_tools/ .env`）；用 `tmp_path + monkeypatch.setattr(config, ...)` 隔离 | [conftest.py](file:///e:/codebase/GitHub/private-agent-poc/tests/conftest.py#L1-L10) 头注 + [L48-L62](file:///e:/codebase/GitHub/private-agent-poc/tests/conftest.py#L48-L62) `isolated_env`（autouse） |
| must | ContextVar 必须逐测试 set + reset（用 `set()` 返回的 token `reset()`），不得只 set 不 reset——跨用例泄漏是已踩过的坑 | [conftest.py](file:///e:/codebase/GitHub/private-agent-poc/tests/conftest.py#L72-L81) `current_user/current_session/current_run/current_identity` 复位 |
| must | 进程内缓存（包扫描、tombstone 等）随测试库切换一并清空 | [conftest.py](file:///e:/codebase/GitHub/private-agent-poc/tests/conftest.py#L63-L70) `packs.invalidate_scan_cache()`、`sessions._deleted_cache.clear()` |

### 4.4 门控（mock / 真模型 / Docker）

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 默认全量回归离线可跑：会话级默认 `MOCK_LLM=true`、`AUTH_ENABLED=true`、`MCP_ENABLED=false` | [conftest.py](file:///e:/codebase/GitHub/private-agent-poc/tests/conftest.py#L20-L40) |
| must | 真模型用例 `RUN_LLM_TESTS=1` 门控、Docker 用例 `RUN_DOCKER_TESTS=1` 门控，`pytest.mark.skipif` 显式声明，默认跳过 | [test_integration_llm.py](file:///e:/codebase/GitHub/private-agent-poc/tests/test_integration_llm.py#L22-L25)；[test_integration_docker.py](file:///e:/codebase/GitHub/private-agent-poc/tests/test_integration_docker.py#L28-L31) |
| should | 并发/竞态用例用事件驱动（asyncio.Event 等）做到确定性，零 sleep | v0.17.38 "3 类确定性并发用例，全事件驱动零 sleep"（README 版本记录） |

### 4.5 断言风格与回归门槛

| 级别 | 规则 | 实例 |
|---|---|---|
| must | pytest 原生 `assert`；HTTP 用例同时断言状态码 + 响应体关键字段；禁用恒真断言（`X or True` 是历史评审修复项） | commit `afe40eb` 修复记录 |
| must | 提交前全量回归通过且 skipped 语义不变。回归基线随每次提交演进，本文数字以 v0.17.40 时点为准（677 passed / 18 skipped，门控用例默认跳过），后续提交后以最新全量结果更新此处 | README v0.17.40 条目 |
| should | 新增防线用例随修复同 commit 入库，评审修复项附回归用例 | v0.17.38–40 各版本条目 |

---

## 5. 文档与流程

### 5.1 spec（docs/specs/）

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 需要 spec 的档位（见 spec-first「任务档位与裁剪」）开发前先产 spec，含四部分：①背景与目标 ②需求分析与约束澄清 ③关键技术与选型决策 ④验收标准（AC 可检验）；需用户确认的档位经确认才开发（缺陷修复档只落极简复现 spec，见下条） | [spec-first/SKILL.md](file:///e:/codebase/GitHub/private-agent-poc/.trae/skills/spec-first/SKILL.md#L30-L50)；[security-hardening-v0.17.39.md](file:///e:/codebase/GitHub/private-agent-poc/docs/specs/security-hardening-v0.17.39.md#L1-L30) |
| must | spec 的 §4 验收标准表直接扩为 AC 账本（验证方式/用例/红态/实现证据/收敛；S 级可只留验证方式/用例/收敛三列），并含 §5 红测试清单、§6 红运行记录（含基线 passed/skipped）、§7 收敛结论；红态取值与 `test_guard_*` 守护用例命名按 spec-first 账本定义；证据须为测试名或 `文件:行号`，可由评审机械复核 | spec-first「契约关」「兑现关」；[4.2-scheduled-triggers.md](file:///e:/codebase/GitHub/private-agent-poc/docs/specs/4.2-scheduled-triggers.md#L84-L170)（仅 §5/§6 红清单与红运行记录）、[4.1-workflow-solidification.md](file:///e:/codebase/GitHub/private-agent-poc/docs/specs/4.1-workflow-solidification.md#L255-L310)（§6.1-6.3 多轮红运行记录） |
| must | 任务档位与裁剪按 spec-first「任务档位与裁剪」执行；纯文档档免 spec，缺陷修复档保留极简 spec（复现用例 + 验证方式），其余档在交付汇报声明档位与裁剪；全关卡任务写「裁剪：无」；轻档的「事实核对」指名词核对 + 相关既有用例跑绿；声明与实际不符按 P1 处理 | spec-first 同名章节 |
| must | 命名二选一：PLAN 任务用 `<任务号>-<短名>.md`（`2.3-kb-permissions.md`）；版本加固用 `<主题>-v0.17.xx.md`（`security-hardening-v0.17.39.md`） | docs/specs/ 目录现状（两种各 8+ 篇并存） |
| should | spec 含"现状证据"表（层/位置/口径 + 行号）与"回归门槛"；评审裁决（修复/误报/有意保留）回写 spec 留痕 | [security-hardening-v0.17.39.md](file:///e:/codebase/GitHub/private-agent-poc/docs/specs/security-hardening-v0.17.39.md#L21-L30)、[L99-L113](file:///e:/codebase/GitHub/private-agent-poc/docs/specs/security-hardening-v0.17.39.md#L99-L113) |
| must | 有意保留的行为变更（如 fail-closed 收紧）须在 spec 写明并在 SECURITY.md/部署文档加警示 | v0.17.39 SSO 空部门头（SECURITY.md 部署警示） |

### 5.2 评审留痕（docs/reviews/）

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 提交前必须过一次代码评审（TRAE-code-review）；命中复审条件时按 spec-first「代码评审（含触发式复审）」执行；未评审不提交 | spec-first 流程「代码评审（含触发式复审）」（project 约定） |
| must | 编码前 spec 独立评审（触发条件见 spec-first「任务档位与裁剪」）报告命名 `YYYY-MM-DD-plan-<任务号>-spec-review.md`；结论分 P0/P1/P2 并含「不成立/误报」区；P0/P1 修复或书面拒绝后才可请求用户确认 | [2026-11-06-plan-4.2-spec-review.md](file:///e:/codebase/GitHub/private-agent-poc/docs/reviews/2026-11-06-plan-4.2-spec-review.md) |
| must | 修复后复审报告命名 `YYYY-MM-DD-v0.17.xx-<修复后短hash>-rereview.md`；只查上轮问题关闭与本轮新改动，最多 2 轮，每轮须「上轮问题全部关闭且本轮新增 P0/P1 不高于上轮新增」 | [2026-11-07-v0.17.49-a8198ef-rereview.md](file:///e:/codebase/GitHub/private-agent-poc/docs/reviews/2026-11-07-v0.17.49-a8198ef-rereview.md) |
| must | 评审报告命名 `YYYY-MM-DD-v0.17.xx-<短hash>-review.md`；整改回执 `-review-response.md`；结论按「确认缺陷已修复 / 误报证伪（附证据）/ 有意保留（附理由）」分类 | docs/reviews/ 目录；[2026-11-02-v0.17.39-8bba994-review-response.md](file:///e:/codebase/GitHub/private-agent-poc/docs/reviews/2026-11-02-v0.17.39-8bba994-review-response.md#L8-L18) 逐条处置表 |

### 5.3 PLAN.md 待办

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 表格条目内联标记交付状态 `✅（v0.17.xx 已交付）`，不另起已完成清单 | [PLAN.md](file:///e:/codebase/GitHub/private-agent-poc/docs/PLAN.md#L24-L43) |
| should | 评审/复核遗留事项记入"后续轮次待办"表（事项 + 来源 + 证据链接） | [PLAN.md](file:///e:/codebase/GitHub/private-agent-poc/docs/PLAN.md#L91-L107) |

### 5.4 commit message 与版本

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 版本提交信息格式：`类型 v0.17.xx：<一句话主题>（spec: docs/specs/xxx.md）`。类型统一用英文词：`feat`（新功能）/ `fix`（缺陷修复）/ `security`（安全加固）/ `docs`（纯文档/报告归档，可声明不计版本）/ `refactor` / `chore` / `ui`。历史提交中的中文前缀（`安全加固 vX`、`存档：`）与 `feat(v0.17.xx):` 写法不追溯、不回改 | `8bba994` `安全加固 v0.17.39：…`（历史形态）；新提交示例：`security v0.17.41：…（spec: docs/specs/security-hardening-v0.17.41.md）` |
| must | 提交按「成对提交」：功能 commit（spec + 测试 + 实现 + README）在前；评审/复核报告单独 `docs` commit 在后，message 引用被评审 commit 短 hash；两个 commit 同批推送、不跨版本积压；编码前的 spec 评审报告例外，先行独立 `docs` commit。理由：功能 commit 须可独立回滚/bisect（报告混入会连带回滚审计记录、污染代码路径 log），且报告须引用被评审 hash、必然晚于功能 commit 产生。本条优先于其他文档中的旧表述 | `3ddfaad`（功能交付同 commit）；`2bb6919`（复核报告单独 `docs` commit）；两例分属 v0.17.46 / v0.17.49——历史无同版本内干净分离的成对提交。`18d4a43` 属旧口径历史形态，不追溯、不作范例 |
| must | 版本号 `v0.17.xx` 逐次 +1；README 顶部版本号与版本记录同步更新 | [README.md](file:///e:/codebase/GitHub/private-agent-poc/README.md#L3) |
| must | README 版本记录只写**实现的功能与用户可感知行为**，不写技术细节；其他章节随功能迭代同步 | [README.md](file:///e:/codebase/GitHub/private-agent-poc/README.md#L7-L40)（用户既定偏好） |
| must | docs/ 下文档随功能迭代同步更新，与交付状态保持一致 | 项目既定约定 |

### 5.5 带页面功能的加严流程

| 级别 | 规则 | 实例 |
|---|---|---|
| must | spec 确认后先出页面原型（trae-design:solo-design）经人工审核 → TDD；实现完成全量回归 → 页面功能展示与人工测试 → 用户确认后才代码评审 → 提交。纯后端/CLI/工具及按三分口径判定为免的页面调整跳过这两关 | spec-first SKILL.md「页面原型」「页面功能展示与人工测试」步骤（用户 2026-10-22 约定）；commit `51b980c` |

---

## 6. 前端约定（web/ 原生 JS 单文件）

| 级别 | 规则 | 实例 |
|---|---|---|
| must | 无外部 CDN：样式与脚本全部本地化/内联；第三方库放 `web/vendor/` 用相对路径引用 | [index.html](file:///e:/codebase/GitHub/private-agent-poc/web/index.html#L7-L8) 头注声明；[console.html](file:///e:/codebase/GitHub/private-agent-poc/web/console.html#L7-L8) |
| must | 样式 token 唯一来源 [tokens.css](file:///e:/codebase/GitHub/private-agent-poc/web/tokens.css#L2-L15)（`--bg/--panel/--border/--text/--accent/--success/--warning/--danger/--radius-*`），页面内联样式经 `var(--...)` 消费；新增语义色先加 token | [index.html](file:///e:/codebase/GitHub/private-agent-poc/web/index.html#L10-L17) |
| must | 动态内容优先 `textContent` / `createTextNode`；HTML 片段只在受控场景（服务端已消毒/本地产出）使用，外链拼接需转义 | [index.html](file:///e:/codebase/GitHub/private-agent-poc/web/index.html#L472-L494) `addMsg` |
| must | API 调用统一 `/api/...` 前缀，Cookie 凭据随同源请求自动携带（不手动塞 token 头、不写 localStorage）；流式响应用 `fetch + resp.body.getReader()` | [index.html](file:///e:/codebase/GitHub/private-agent-poc/web/index.html#L293-L314) 登录；[L671-L679](file:///e:/codebase/GitHub/private-agent-poc/web/index.html#L671-L679) chat 流式 |
| should | 事件绑定优先 `addEventListener`（存量 `onclick` 赋值混用见 [index.html L293-L314](file:///e:/codebase/GitHub/private-agent-poc/web/index.html#L293-L314)，触碰即改）；SSE 事件类型与后端词表同步维护 | [index.html](file:///e:/codebase/GitHub/private-agent-poc/web/index.html#L228-L238) `addEventListener('input', autoGrow)` |
| should | 两页面重复的工具函数允许抽取为本地共享 `web/common.js`（相对路径 `<script src>` 引入）。前提不变：零 CDN、无构建步骤、无框架；不为此引入打包器或模块系统 | index.html 与 console.html 当前各自内联全部脚本、公共函数复制维护 |

---

## 7. 提交前自检清单

逐条打勾，任一不满足不得提交：

- [ ] spec 已确认（docs/specs/ 对应文件存在，AC 可检验），且测试先行跑红过
- [ ] 新增/变更的安全防线有**拒绝路径**用例；防线判定 fail-closed
- [ ] 对外三通道（HTTP 响应/SSE/页面、工具返回模型文本、审计记录）无 `{e}` 拼接、无明文凭据/token、无服务器绝对路径（本机日志允许保留原始异常，不得外发）
- [ ] 权限相关改动在提示层/执行层/端点层复用同一判定函数，无内联重写
- [ ] 变更触及端点/中间件/认证时跑过 `python tests\perf_baseline.py`，各端点 P95 ≤ 500ms（ENGINEERING-METRICS U2）
- [ ] requirements 变更时跑过 `pip-audit --vulnerability-service osv --timeout 60`，直接依赖命中为 0；离线环境改为有网环境定期扫描并把清单带回（ENGINEERING-METRICS S5）
- [ ] 新增配置集中在 config.py 读取（含默认值与类型归一），业务代码按 `config.X` 调用时引用
- [ ] async 代码中无裸同步阻塞调用（DB/文件/锁均 `asyncio.to_thread`）；代理类透传基类返回值
- [ ] 新 ContextVar 已在 conftest 的 `isolated_env` 中 set+reset
- [ ] 全量回归通过，passed 数增加、skipped 语义不变（基线随提交演进，v0.17.40 时点为 677/18，以最新全量结果为准）
- [ ] 前端改动：无 CDN 引入、颜色走 tokens.css、动态内容用 textContent、无 localStorage 凭据
- [ ] 代码评审（TRAE-code-review）已执行；确认缺陷已修复并重跑回归；误报/有意保留已写入 spec 或评审回执
- [ ] 带页面功能：原型人工审核 + 页面人工测试两道关卡已过
- [ ] README 版本号与版本记录（只写功能）、docs/ 相关文档、PLAN.md 待办标记已同步
- [ ] commit message 符合 §5.4 格式并引用 spec 路径；用户生成内容（uploads/、outputs/、data/）未被误入暂存

---

## 附：裁决记录（2026-11-03 用户裁决，已回填正文）

规范初稿列出 5 处"现状与规则方向冲突"，用户裁决如下，正文对应条目已按结论更新：

1. **main.py 单体化** → 保留「现状/待收敛」，写明拆分触发条件（main.py 超 1200 行或新增成组路由 ≥3 个时引出 APIRouter），本轮不重构（§3.1）。
2. **日志通道** → 升级为 must：server/ 与 sandbox/ 一律 logging、禁止 print，print 仅允许 tools/ 独立 CLI 脚本，存量触碰即改（§1.4）。
3. **commit 类型前缀** → 统一英文类型词（feat/fix/security/docs/refactor/chore/ui）+ 保留 v 号，历史中文前缀不追溯（§5.4）。
4. **config 读取写法** → 升级为 must：统一 `os.environ.get`，「触碰即改」、不做专项重构（§1.5）。
5. **前端公共逻辑** → 允许抽取本地共享 `web/common.js`，前提零 CDN、无构建、无框架（§6）。

另：回归基线数字（677/18）以 v0.17.40 时点为准，随每次提交演进，提交后以最新全量结果更新 §4.5（§4.5、§7 已注明）。
