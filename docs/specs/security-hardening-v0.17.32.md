# 安全整改批次 spec（v0.17.32）

> 状态：已实现并通过 TRAE-code-review 双子代理评审（结论见第五节「评审记录」）。
> 实现与测试以本文为准；实现完成后在 PLAN.md「计划外交付记录」
> 登记 v0.17.32，README 版本记录顺延 v0.17.32（只写功能，不写内部细节）。

## 一、背景与目标

外部安全评审对本仓库（HEAD 41148b1，v0.17.31）提出多项问题，逐条代码核实后
确认 **7 项属实**（另 1 项「546/564 测试数漂移」为口径误解：564 = 548 passed +
16 skipped，16 个为需真实模型/Docker 的显式门控跳过，README 口径无漂移）。
用户已确认七项全部整改，本批作为 v0.17.32 交付。

七项分三类：**跨用户安全边界 4 项**（SEC-1/2/3/4，多用户部署前必须闭合）、
**部署链路正确性 2 项**（SEC-5/6，主链路按默认文档部署即错配）、
**数据可靠性 1 项**（SEC-7，重建窗口期丢索引）。

范围约束：只修七项及直接关联的文档/测试基线，不做无关重构；已知边界问题
（STATE_DB 仍 SQLite、文件本地化、水平扩展半成品）与 PLAN 自述一致，不在本批。

## 二、需求分析与约束澄清（七项问题 → 方案）

### SEC-1（P0）：chat 端点缺会话归属校验

**问题**：`POST /api/chat`（main.py L469-544）未做会话归属校验。历史/删除
端点（L579-594）都经 `_can_access_session()` 拦截，chat 却没有——登录用户
只要拿到他人 `session_id`，即可向其会话写入消息并以该 id 接续其 LangGraph
`thread_id` 模型上下文（跨用户上下文泄露 + 会话状态污染）。

**方案**：chat 入口在 user_id 解析后、进入 event_stream 前增加归属校验：
认证模式下（`_acl_identity` 非 None），admin 全局视图放行；其余角色查
`sessions.session_users(session_id)`——owners 为空（新会话首轮写入）放行；
owners 非空且含本人与 anonymous 之外的身份 → 403（文案/状态码对齐既有
`{"error": "forbidden"}`）。开放模式不校验（行为不变）。

**关键约束（不复用 `_can_access_session`）**：其语义是
`bool(owners) and owners <= {本人, anonymous}`——**空会话返回 False**，
对历史/删除端点合理（无内容可看），但 chat 对新会话必须放行（首轮写入），
语义不同，须独立小函数（如 `_chat_session_denied(ident, session_id)`）。

**已知边界（有意保留，不在本批）**：`delete_session` 只清 messages 表，
checkpointer 状态残留（memory 模式进程内）——已删除会话的 session_id
仍可被他人以「空会话」身份接续残留上下文。修复需删除会话时同步清理
checkpointer 线程，改动面另立任务。本批以 sessions 表为所有权判定源。

### SEC-2（P0）：用户目录名跨用户碰撞——用户已拍板：彻底方案（哈希域）

**问题**：`config.scope_clean`（config.py L38-43）把非法字符归一为 `_`，
`a@b` 与 `a_b` 清洗后同目录，两账号共享 uploads/outputs/skills_personal/
custom_tools_personal 四类文件域（`auth.add_user` L135 只拦 `shared` 保留名，
无碰撞检查）。

**方案**：新增 `config.user_scope(username)`——`scope_clean` 结果 +
`-sha256(原始用户名)[:8]` 后缀，不同用户名必得不同目录名：

```python
def user_scope(username: str) -> str:
    base = scope_clean(username)
    if base == "shared":          # 全非法字符/空名回退保留域，不带哈希
        return "shared"
    return f"{base}-{hashlib.sha256((username or '').strip().encode('utf-8')).hexdigest()[:8]}"
```

- **寻址口径单点化**：用户域寻址全部改走 `user_scope`——
  `main._user_scope`（L656-662，uploads/outputs 端点侧）、`tools_builtin._file_scope`
  （L41-48，工具侧落盘/读取）、`skills.current_scope`（L44）、
  `custom_tools.current_scope`（L53）、main.py 技能包导入 personal_scope（L866）。
  `scope_clean` 保留为清洗原语（`_safe_rel` 域段清洗 L683 对已含哈希后缀的
  域名幂等无害，继续使用）。
- **add_user 保留名拦截保留**（全非法字符用户名仍拒绝）；**不再需要**
  创建时碰撞拒绝（哈希天然唯一）。
- **存量迁移**：启动 lifespan 在 `_migrate_legacy_file_dirs` 之后新增
  `_migrate_scope_dirs()`：按用户表（含已吊销，目录保留审计价值）建立
  老域名→用户映射，遍历四类根目录下的子目录——
  1) 子目录名是某用户老域名且无人共享 → rename 到该用户新域名（重试 3 次，
     对齐 `_migrate_legacy_file_dirs` 的 Windows sharing violation 惯例）；
  2) 老域名被多用户映射（存量碰撞）→ **不动 + 启动 WARNING**（无法自动归属，
     人工整改）；
  3) 新域名目录已存在 → 跳过 + WARNING（视为已迁移残留）；
  4) 无人认领（非任何用户老域名）→ 不动（admin 全局视图仍可见可下载）。
  `shared/` 保留域不参与迁移。幂等：新命名目录带哈希后缀，重跑不重复迁移。

**已知边界（breaking，README 注明）**：历史消息/旧审计中的老域名逻辑路径
（如 `uploads/zhang_san/…`）迁移后失效；存量碰撞目录需人工整改（预期概率
极低——碰撞本身即本次评审发现的未利用漏洞）。

### SEC-3（P0）：自定义工具默认静默回退主进程——用户已拍板：运行模式联动

**问题**：`custom_tools.exec_mode()`（L289-297）默认 `auto`：沙箱不可达时
静默回退 `local`——在 FastAPI 主进程 `exec_module()` 导入工具代码（模块
顶层代码执行，获得主进程全部文件/网络/进程权限）；沙箱故障改变的是安全
边界而不只是执行位置。`CUSTOM_TOOLS_EXEC=sandbox` 的 fail-closed 行为已存在
但非默认。

**方案**：默认值与运行模式联动（对齐 `AUTH_ENABLED` 的既有先例
「Mock=开发者模式宽松默认，真实模式=生产默认」）：

- `config.py` 新增 `CUSTOM_TOOLS_EXEC = os.environ.get("CUSTOM_TOOLS_EXEC", "")`
  （空串 = 未显式配置）；
- `exec_mode()` 改为：未显式配置时 `auto if config.MOCK_LLM else "sandbox"`；
  显式配置值照旧生效（auto/sandbox/local 三值语义不变）；
- auto 回退 local 时打印显著 WARNING（模块级时间戳限频 60s，防刷屏），
  提示「自定义工具正在主进程执行（沙箱不可达），生产环境请部署沙箱或显式
  配置 CUSTOM_TOOLS_EXEC=sandbox」。

效果：Mock 模式开箱体验不变（auto）；真实 LLM 模式（生产链路）默认
fail-closed——沙箱不可达则不加载任何自定义/包工具（`load_langchain_tools`
既有 sandbox-down 分支自动生效）。

### SEC-4（P0）：包签名与热重载之间的信任缺口

**问题**：`packs._scan_cache` 缓存含验签结果（`packsign.trust_status`，
packs.py L192）；热重载链 `maybe_reload_custom_tools()`
（agent.py L245-253）按 mtime 签名检测变化后 `_rebuild_graph()`，但
`scan_packs()` 走缓存不重扫——强制验签模式下，运行中替换包内工具文件
会加载新内容（local 直接 exec 新文件 / sandbox 每次执行读新源码）却不重新
验签，「签名包」只保证启动时校验。

**方案**：`_rebuild_graph()`（agent.py L71）开头调用
`packs.invalidate_scan_cache()`——所有重建路径统一强制重扫（读盘 + HMAC，
包数量少可接受，且仅在检测到变化时发生）。篡改包在强制模式下重扫后
trust=invalid → `enabled_packs()` 剔除 → 图中工具消失（fail-closed）。
`scan_packs()` docstring「手工改盘需重启生效」同步更新为
「热重载触发重建时强制重扫（含验签）」。

### SEC-5（部署）：LiteLLM 主模型地址在容器网络中指向自身

**问题**：`gateway/litellm_config.yaml` L21 `api_base: http://127.0.0.1:8000/v1`
——gateway 容器内 127.0.0.1 指向 gateway 自身而非 vLLM；靠 DeepSeek fallback
掩盖，按生产文档删 fallback 后主链路直接不可用。

**方案**：
- `litellm_config.yaml`：`api_base` 改为 `os.environ/VLLM_API_BASE` 引用
  （LiteLLM 既有 env 引用语法，同文件 `DEEPSEEK_API_KEY` 已用），YAML 保持
  纯 ASCII；头注释写明两个场景（Docker：compose 注入默认值；裸机：
  export VLLM_API_BASE=http://127.0.0.1:8000/v1）；
- `docker-compose.gateway.yml`：environment 加
  `VLLM_API_BASE: ${VLLM_API_BASE:-http://host.docker.internal:8000/v1}`
  （vLLM 容器映射宿主 8000 端口；远程 GPU 机在 .env 覆盖），并加
  `extra_hosts: ["host.docker.internal:host-gateway"]`（Linux 宿主网关解析，
  Docker Desktop 原生支持）；
- `docs/DEPLOYMENT.md` / `DEPLOY_RUNBOOK.md` / `PRODUCTION.md` 涉及网关
  段落同步补 VLLM_API_BASE 说明。

### SEC-6（部署）：压缩阈值与 vLLM 窗口基线不一致

**问题**：`CONTEXT_MAX_TOKENS` 默认 128000（config.py L52）→ 压缩触发线
76800 tokens；docker-compose.yml vLLM `--max-model-len 32768`——长会话在
压缩触发前先超模型窗口。

**方案**（文档基线对齐 + 配置联动提示，无法自动探测各部署的模型窗口）：
- `.env.example` 上下文压缩段补明确对齐提示：「使用 docker-compose.yml
  的 vLLM（--max-model-len 32768）时必须设 CONTEXT_MAX_TOKENS=32768」；
- `docker/docker-compose.yml` vllm 服务 command 上方注释补同向提示
  （改 CONTEXT_MAX_TOKENS 与本参数对齐）；
- README 环境变量表 CONTEXT_MAX_TOKENS 行补警示。
- 启动日志已有触发线打印（agent.build_agent），保持。

### SEC-7（可靠性）：知识库重建非原子

**问题**：`tools/build_kb.py` `build()` L299-301 先 `db_path.unlink()` 再
重建——重建期间检索查询直接失败；构建中途异常时旧索引已删（不可恢复）。

**方案**：临时库构建 + 校验后原子替换 + 保留上一版回滚：
1. 全部建表/写入在 `kb.db.tmp` 上进行（构建异常 → finally 清理 tmp，
   旧库完好无损）；
2. 构建完成后 `PRAGMA quick_check` 通过 → 先把现库 `os.replace` 为
   `kb.db.bak`（保留上一版，失败则跳过 bak 继续替换，降级不阻塞），
   再 `os.replace(kb.db.tmp, kb.db)` 原子替换（Windows 对被打开文件替换
   可能瞬时失败，重试 3 次，100ms 间隔——检索工具为短连接，碰撞概率低）；
3. `kb.db.bak` 每次构建覆盖，仅保留一版。

## 三、技术选型要点

| 项 | 落点 | 要点 |
|---|---|---|
| SEC-1 | server/main.py | 独立 `_chat_session_denied`（空会话放行语义），403 JSONResponse |
| SEC-2 | server/config.py + main.py + tools_builtin.py + skills.py + custom_tools.py | `user_scope` 单点定义；寻址调用点全量切换；迁移函数放 main.py（lifespan，与 `_migrate_legacy_file_dirs` 并列） |
| SEC-3 | server/config.py + custom_tools.py | 默认值联动 + 回退告警限频；沙箱探测缓存既有逻辑不动 |
| SEC-4 | server/agent.py + packs.py | `_rebuild_graph` 开头 invalidate；注释同步 |
| SEC-5 | gateway/litellm_config.yaml + docker/docker-compose.gateway.yml + docs | env 引用 + compose 默认注入 + extra_hosts；YAML 纯 ASCII |
| SEC-6 | .env.example + docker/docker-compose.yml + README.md | 注释/文档对齐 32768 |
| SEC-7 | tools/build_kb.py | tmp → bak → 原子 replace + 重试 |

不引入新依赖；不改 API 契约（上传端域由服务端计算，前端无改动）。

## 四、验收标准（AC1-AC7）

- **AC1（SEC-1）**：认证模式下 user B 向 user A 已有消息的 session 发
  chat → 403；B 向全新 session → 放行正常写入；A 续聊本人会话 → 放行；
  admin → 放行；开放模式行为不变。跨用户读写回归用例固化。
  测试：`tests/test_session_acl.py`（新）。
- **AC2（SEC-2）**：`user_scope("a@b") != user_scope("a_b")`，同用户名结果
  稳定，全非法字符/空名回退 `shared`，中文清洗保留；四类目录寻址调用点
  全部经 user_scope（B 的域目录与 A 不同，B 无法读写 A 的域内文件）；
  存量迁移：老域名目录 rename 至新域名、幂等重跑不动、碰撞目录跳过并告警、
  新目录已存在跳过；add_user 保留名拦截行为不变、不同清洗碰撞用户名均
  可创建且域不同。
  测试：`tests/test_auth.py`（增）+ `tests/test_scope_migration.py`（新）
  + `tests/test_tools_builtin.py`（域断言改 user_scope 计算）。
- **AC3（SEC-3）**：未显式配置时 MOCK_LLM=true → exec_mode 取 auto；
  MOCK_LLM=false → 取 sandbox（沙箱不可达即 fail-closed，load_langchain_tools
  空列表）；显式 auto/sandbox/local 三值照旧覆盖；auto 回退 local 打印
  WARNING（限频生效）。`.env.example` 注释更新。
  测试：`tests/test_custom_tools.py`（增）。
- **AC4（SEC-4）**：强制验签模式下签名包已加载 → 直接改盘替换包内工具
  文件 → `maybe_reload_custom_tools()` 触发重建 → `scan_packs()` 重扫后
  trust=invalid、包 unavailable、图中无该包工具（fail-closed）；开放模式
  下（未配密钥）行为不变；`scan_packs` 缓存在无变化重建路径下仍生效
  （create/delete/sign 入口既有失效逻辑回归不破）。
  测试：`tests/test_packs.py`（增）。
- **AC5（SEC-5）**：litellm_config.yaml 的 api_base 为 os.environ/VLLM_API_BASE
  引用且文件纯 ASCII；docker-compose.gateway.yml 注入 VLLM_API_BASE 默认值
  且含 extra_hosts host-gateway；三份 docs 网关段落含 VLLM_API_BASE 说明。
  测试：`tests/test_deploy_config.py`（新，读文件断言防回退）。
- **AC6（SEC-6）**：.env.example 压缩段含 32768 对齐提示；docker-compose.yml
  含 --max-model-len 32768 且注释互指；README 表格行含警示。
  测试：并入 `tests/test_deploy_config.py`（同值断言防单侧改动）。
- **AC7（SEC-7）**：成功构建后 kb.db 可查询、kb.db.bak 为上一版；构建中
  注入异常 → 旧库完好、无 tmp 残留；连续两次构建验证 bak 滚动覆盖。
  测试：`tests/test_kb_ingestion.py`（增）。

**回归红线**：全量 `.venv\Scripts\python.exe -m pytest` 不得低于基线
548 passed / 16 skipped（SEC-2 触及既有测试的域路径断言，随测试先行阶段
一并修正为 user_scope 计算）。

## 提交与文档同步

- 单一 commit：spec + 测试 + 实现 + PLAN.md（计划外交付记录 v0.17.32）+
  README（版本记录 v0.17.32：安全整改与部署修正，只写功能）+ .env.example。
- 提交前必过 TRAE-code-review 双子代理评审；push 由用户操作。

## 五、评审记录（TRAE-code-review 双子代理交叉验证，随 v0.17.32 提交）

评审范围：本批全部未提交变更。主代理自查候选由两个子代理独立验证，按
2/2 确认纳入、1/2 带疑纳入（主代理复核成立）、0/2 证伪排除裁决。

**已修复（4 项，附回归测试，随本 commit）**：

- **C2（中）** `main._safe_rel` 对 URL 域段沿用 scope_clean 的 48 字截断，
  基名 ≥40 字用户（哈希域名 49-57 字符）的后缀被截 → approver/admin 全局
  视图按 URL 寻址其域 404。修复：新增 `config.scope_segment`（同字符集、
  上限放宽 57）承担 URL 侧域段清洗；回归：test_config.py（单元）+
  test_main_api.py（下载/删除端到端）。
- **C4（中低）** build_kb 在 connect 后、close 前异常不关连接，Windows 下
  外层 finally 的 unlink 抛 PermissionError 掩盖原始异常；tmp 残留 +
  CREATE TABLE 无 IF NOT EXISTS 会毒化下次构建。修复：conn 包 try/finally
  强制关闭 + 构建前清历史 tmp 残留；回归：test_kb_ingestion.py。
- **C8（中低）** bak 转存成功后 tmp→db 替换 3 连败 → finally 删 tmp →
  现役库缺失只剩 bak，违反本 spec「任何失败现役库保持完好」。修复：替换
  失败时回滚 bak→db（仅当本轮转存确实成功，防把陈旧 bak 覆盖到仍在原位
  的完好旧库）；回归：test_kb_ingestion.py。
- **C5（低）** 本 spec SEC-6 方案第 2 条「docker-compose.yml vllm command
  上方补 CONTEXT_MAX_TOKENS 对齐注释」未兑现（AC6 测试亦未覆盖）。修复：
  补注释 + test_deploy_config.py 断言互指。

**有意保留（3 项，低危，理由如下）**：

- **C1** admin 接续写入会话把 admin 加入 owners，原属主此后 chat/历史/删除
  均 403——与 `_can_access_session` 既有语义一致的 fail-closed 继承行为
  （修复前历史端点在 admin 写入后同样拒绝原属主），非本次引入；admin 自身
  仍可访问，属管理操作副作用。
- **C6** chat 归属校验（同步）与流内写入（多个 await 让出点）间存在毫秒级
  TOCTOU：两用户并发首轮写同一空 session_id 可致双属主互 403。触发需预知
  他人新 session_id 且精确并发，影响为 fail-closed 会话锁死，POC 单副本可
  接受。
- **C7** CUSTOM_TOOLS_EXEC 显式非法值（如 foo）回落 auto 语义（不可达→
  local+WARNING）而非拒绝——与旧版宽松语义一致、非回归；.env.example 已
  写明合法取值 auto|sandbox|local。

**证伪排除（1 项）**：C3「注册字面量『基名-他人哈希』用户名共享目录」——
user_scope 对任何输入都再叠加本人哈希，机制不成立；同基名 32 位生日碰撞
n=10 时约 1e-8；且本项目无自助注册（建号仅 admin 端点）。
