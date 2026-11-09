# 安全加固 v0.17.37：会话删除生命周期封禁与知识库原文 XSS 防护（spec）

状态：已交付（v0.17.37；基线 2ec462f，622 passed / 18 skipped；本轮为 11-01 架构审核
整改顺序第 1 步——报告 §9「安全阻断项」P0 两项）。

## 一、背景

审核报告 `docs/reviews/2026-11-01-v0.17.36-architecture-review.md` 判定
P0 两项为安全阻断项，须先于其余整改完成。两项证据已在 `2ec462f` 代码逐行
核实属实：

- P0-1 删除会话后复用 session_id 可接续旧模型上下文（报告 §3）：
  1. `main.py:620-626` 删除端点只调 `sessions.delete_session()`；
  2. `sessions.py:139-149` 仅删 `messages` 表——checkpointer、
     `agent._compact_cache`（agent.py:296-297）、会话租约（session_locks 表）
     均未动；
  3. `main.py:585-597` 归属靠 messages 推断，owners 空即视为可创建新会话
     ——删除后同 ID 复用无任何拦截；
  4. `agent.py:453-454` 客户端 session_id 恒为 LangGraph thread_id，
     复用即接续旧上下文（跨用户泄露 + 隐私语义不一致）。
- P0-2 知识库 Word 原文视图存储型 XSS（报告 §4）：
  1. `main.py:1122-1125` `parse_document()` 文本直拼 `<p>`/`data-a` 属性，
     未 `_esc()`——docx `w:t` 可携带 `<img src=x onerror=...>` 字面量；
  2. `main.py:1126-1159` 文件名 `doc`（title）、逻辑路径 `logical` 直拼；
     `:1153` Mammoth 输出直写 `innerHTML`——恶意 docx 可借超链接产出
     `javascript:` URL 等主动内容；
  3. `web/index.html:247` / `web/console.html:444` Bearer 存 localStorage；
  4. `docs/SECURITY.md:107`「无 innerHTML 注入点」表述与实际不符。

范围裁决（用户已确认）：P0~P2 全部整改，按报告 §9 顺序多轮推进；本轮仅
P0 两项。后续轮次预告：v0.17.38 SSO 身份一致性（§5）→ 管控面（§6）→
数据面 Repository/PostgreSQL（§7）→ 交付基线（§8）。

## 二、方案总览

P0-1 治理语义：**删除 = 不可逆终局**。独立所有权表 + tombstone 永久封禁 +
删除覆盖全存储面。P0-2 输出安全边界：**服务端拼接必转义、Mammoth 输出必
消毒、原文页必设 CSP**；浏览器登录态完整迁移 HttpOnly Cookie + CSRF 防线
（用户裁决本轮完成）。

不做（本轮明确排除）：P1/P2 全部条目；真实浏览器 E2E 自动化（无
Playwright 基建，列为部署人工验收项）。

## 三、P0-1 会话删除生命周期

### 3.1 会话元数据与所有权表（sessions.py 新增 session_meta）

```sql
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    created    REAL NOT NULL,
    deleted    REAL              -- NULL=活跃；非空=删除时刻（tombstone）
);
```

- `register_session(sid, owner) -> "ok"|"taken"|"deleted"`：
  `INSERT OR IGNORE` 原子注册；冲突回读实际 owner 判定归属/封禁。并发
  争抢同一新 ID 时恰一人 "ok"（SQLite 写锁原子性）。
- `get_meta(sid) -> dict | None`：返回 owner 与删除状态。
- `mark_deleted(sid)`：tombstone 写入（保留行，永不物理删除——封禁依据）。
- **存量回填**：meta 无记录而 messages 有 → owner 取该会话**最早一条
  user 消息**的 user_id（开放模式遗留 anonymous 归 anonymous），INSERT OR
  IGNORE 回填后回读校验。回填语义与现有 `owners <= {本人, anonymous}`
  宽恕口径对齐。

### 3.2 chat 端点与归属判定改造（main.py）

1. 服务端生成 ID：`req.session_id` 为空时 `secrets.token_hex(8)` 替代
   `uuid.uuid4().hex[:8]`（不可预测性增强）。
2. 锁内终判（在现有 SEC-1 双查位置，`_chat_session_denied` 语义升级）：
   - meta 有 tombstone → 拒（403 / SSE error）；
   - meta 有 owner：本人或 anonymous 宽恕或 admin → 放行；否则拒；
   - meta 无：messages 有 → 回填后按上判定；messages 无 → 新会话，
     `register_session(sid, 本人)` 原子抢注，失败方拒。
   - 锁外快查保留（低成本 403 快路径），终判以锁内为准（沿 v0.17.33
     竞态二次检查先例）。
     修订（v0.17.37 评审 #2，用户裁决）：锁外快查的存量分支与锁内终判
     同口径（resolve_owner 最早作者归属，顺带回填 meta），不再沿用
     owners 全体推断——旧口径会把存量双作者会话的合法属主误拒 403
     （过严方向，无越权但伤可用性）。
3. `_can_access_session`（history/delete）：同一 meta 口径；tombstone
   会话按不存在处理（404）。
4. admin 接管语义（报告建议 5）：admin 保留跨用户读/写/删（治理职责），
   但 **meta.owner 不因 admin 写入而改变**——所有权漂移问题随 meta 引入
   自然消失（spec 明示，不再依赖消息记录推断）。

### 3.3 删除编排（main.py `_purge_session(sid)`，DELETE 端点调用）

顺序与覆盖面（报告建议 3/4）：

1. `sessions.mark_deleted(sid)`——先封禁入口（此后任何复用被拒）；
2. `sessions.delete_session(sid)`——展示记录（现有）；
3. `STATE.checkpointer.adelete_thread(sid)`——MemorySaver 与
   AsyncPostgresSaver 均原生支持（已验证），双后端同口径物理删除；
   异常捕获记日志不阻断（tombstone 已封禁，残留数据不可达——报告
   「checkpoint 无法可靠删除时至少永久封禁」的兜底语义）；
4. `agent._compact_cache.pop(sid, None)`——进程内压缩摘要缓存；
5. pending 审批 → `approval.reject_pending_for_session(sid)`（新增）：
   `status='rejected', decided_by='system'`——会话删除后其挂起审批
   不得执行（fail-closed）；等待方轮询读到 rejected 即中止；
6. `session_lock.purge(sid)`（新增）：删除 session_locks 行——运行中
   请求心跳续租失败自动中止（lease.lost 语义已存在）。

并发决策（spec 记录）：删除不取会话租约（治理动作即时性，不等待审批
挂起中的长租约）；与运行中 chat 极端并发下可能残留少量 checkpoint，但
tombstone 已封禁入口、残留不可达。

「逻辑删除 vs 物理删除」口径（报告建议 4）：展示记录与 checkpoint 物理
删除 + tombstone 逻辑封禁，双保险；tombstone 行永久保留。

### 3.4 红测试（AC1-AC5 → tests/test_session_lifecycle.py）

真实 checkpoint 用迷你 StateGraph + MemorySaver 写入（离线、不依赖模型）：

- AC1（报告验收 1）：A 产生真实 checkpoint 历史 → DELETE → B 同 ID chat
  403，且 MemorySaver 中该 thread 已不存在（storage 无 sid）。
- AC2（报告验收 2）：A 删除后本人复用同 ID → 403（tombstone，无新上下文
  可创建）。
- AC3（报告验收 3）：删除覆盖面——messages 清空、fake checkpointer 断言
  `adelete_thread(sid)` 被调（Postgres 语义接口级验证；MemorySaver AC1
  实删）、`_compact_cache` 清、pending 审批 → rejected/decided_by=system、
  session_locks 行清。
- AC4（报告验收 4）：tombstone 跨新连接（模拟重启，SQLite 每操作独立连接
  天然等价）仍拒；两用户并发注册同一新 ID 恰一人成功。
- AC5（存量兼容）：messages 有而 meta 无的存量会话，合法 owner 续聊放行
  并回填；他用户仍拒；`test_session_acl.py` 现有全部用例不回归。

## 四、P0-2 知识库原文视图 XSS 防护

### 4.1 服务端统一转义（main.py docx 渲染分支）

`title={_esc(doc)}`、`path={_esc(logical)}`、fallback 行
`<p>{_esc(ln)}</p>`（高亮分支 `data-a` 同转义）。文本片段页（line/sheet/
slide）现有 `_esc` 口径不动，其 title 同步补转义。

### 4.2 HTML 白名单消毒器（新模块 server/html_sanitize.py）

选型决策：**stdlib `html.parser` 自研白名单过滤器**。理由：私有化离线
部署约束（当前依赖无 bleach/nh3；lxml.html.clean 已从 lxml 5+ 剥离），新增
第三方依赖与离线打包链路冲突；以完备恶意样本测试矩阵（AC7）对冲「非成熟
库」风险。上生产前可评估 nh3 替换（接口不变）。

- 标签白名单（Mammoth 排版输出集）：`p h1-h6 strong em b i u s sup sub
  ol ul li table thead tbody tfoot tr td th a img br blockquote code pre dl
  dt dd span`；
- 属性白名单：`a[href,title] img[src,alt] td/th[colspan,rowspan]`；
- URL scheme 白名单：http/https/mailto + 相对路径；img `src` 额外允许
  `data:image/*`（Mammoth inline 图片为 base64 data URI）；
- 主动内容（script/style/iframe/object/embed/applet/form/input/button/
  select/textarea/video/audio/source/link/meta/base）**连同内容整体移除**；
- 其他未知标签：剥标签保留文本（防内容丢失）；
- 实体重编码：输出侧全部经转义再拼合法标签（无二次注入）。

### 4.3 消毒通道与原文页改造（main.py）

前端 Mammoth 转换结果不再直写 innerHTML，改为 `POST /api/kb/sanitize`
（body `{html}`，限长 2MB，响应 `{html: 消毒结果}`；认证模式下须登录，
开放模式与 source 端点同口径）→ 消毒结果再写 innerHTML。消毒逻辑在
服务端 Python，pytest 离线全覆盖；浏览器仅传输。
修订（v0.17.37 评审 #1，用户裁决）：限长检查前置为 Content-Length
头部预检——原设计 body 全量读入后才判长度，超大请求仍先占内存与解析
开销（低危 DoS 面）；对齐上传端点 `_check_body_limit` 先例，超限头
直接 413，再手动 `await request.json()` 读取（保留 pydantic 模型签名
则 body 在 handler 前已被读入，预检无意义，故改为 `Request` 签名）。

### 4.4 CSP 与框架防护（报告建议 3）

docx 渲染页（含内联脚本）响应头：

```
Content-Security-Policy: default-src 'none';
  script-src 'self' 'nonce-{每次请求随机}';
  style-src 'unsafe-inline'; img-src 'self' data:;
  connect-src 'self'; frame-ancestors 'self'
X-Frame-Options: SAMEORIGIN
```

内联 `<script nonce=...>`；`/vendor/mammoth.browser.min.js` 走 `'self'`。
文本片段页加简版 CSP（`default-src 'none'; style-src 'unsafe-inline'`，
无脚本）。

### 4.5 凭据 HttpOnly Cookie 迁移（报告建议 4 完整实施，用户裁决本轮完成）

浏览器登录态从 localStorage Bearer 迁移到 HttpOnly Cookie：

1. **登录端点**：验证通过后 `Set-Cookie: pap_token=<pat-令牌>; HttpOnly;
   SameSite=Lax; Path=/; Max-Age=<AUTH_TOKEN_TTL_HOURS*3600>`；生产
   HTTPS 部署经配置 `AUTH_COOKIE_SECURE=true` 追加 `Secure`（本地 http
   测试不可带，默认 false）。响应体保留 token 字段（程序化 curl 兼容），
   浏览器端 JS 不读取不保存。SameSite 选 Lax 而非 Strict 的理由：跨站
   POST 同样不携带 cookie（CSRF 面等同收敛），但保留从外部链接/书签
   顶级导航进入时免重登的体验（Strict 下掉登录且难排查）。
2. **认证中间件**：凭据优先级 SSO 身份头 > `Authorization: Bearer` >
   Cookie `pap_token`（Bearer 路径全量保留，pak- 程序化调用不受影响）。
3. **CSRF 防线**：SameSite=Lax 下跨站 POST 不携带 cookie（主防线）；
   纵深——**cookie 来源身份的非 GET 请求必须带 `X-Requested-With:
   XMLHttpRequest`**，否则 403（豁免 login/logout：登录前无 cookie 身份、
   登出无危害面）。Bearer/SSO 头路径免校验（不自动附带凭据，无 CSRF 面）。
4. **登出端点**：按 Bearer 或 cookie 令牌注销 + `Set-Cookie Max-Age=0`
   清除浏览器侧。
5. **前端（index.html / console.html）**：删除全部 `poc_key` localStorage
   读写；`authHdr` 不再注入 Authorization（fetch 同源默认带 cookie），统一
   追加 `X-Requested-With`；登录后身份徽章沿用 health 轮询回传的
   identity（现有机制）；pak-「用 Key 进入」通道保留 Bearer——key 仅存
   JS 内存（不落任何存储，刷新即失，重粘即可）。
6. 兼容性：现有 Bearer 测试全量保留（不回归）；SSE chat（POST）同源 fetch
   自动带 cookie + XRW 头。

### 4.6 文档修正

`docs/SECURITY.md` 8.1 改写：如实描述原文视图风险与本轮防线（转义 +
白名单消毒 + CSP + sessionStorage），HttpOnly Cookie 列为生产化方向。

### 4.7 红测试（AC6-AC10 → tests/test_kb_source_xss.py）

- AC6（报告验收 1）：构造 `w:t` 含 `<img src=x onerror=alert(1)>`、引号、
  `&lt;script&gt;` 实体的恶意 docx → 渲染页响应中全部为转义文本，
  无可执行节点。
- AC7（报告验收 2）：消毒矩阵单测——白名单标签/属性保留（p/table/
  `a href=https`/`img data:image`）；script、事件属性、`javascript:`、
  `data:text/html`、iframe/object 移除；script 文本内容不泄露为文本。
- AC8（报告验收 3）：kb 目录直投含 `"'<>` 的恶意文件名 → 渲染页
  title/path/URL 属性上下文不突破。
- AC9：CSP 头断言（default-src 'none'、script-src 含 nonce、
  frame-ancestors 'self'）+ 内联脚本带 nonce + X-Frame-Options。
- AC10：sanitize 端点——认证模式未登录 401、超长输入拒绝、恶意输入
  回显消毒结果。
- AC12（cookie 认证）：登录响应 `Set-Cookie`（HttpOnly / SameSite=Lax /
  Max-Age 对齐令牌 TTL；AUTH_COOKIE_SECURE=true 时含 Secure）；cookie
  访问受保护 API 200；现有 Bearer 路径全部回归通过。
- AC13（CSRF 纵深）：cookie 身份的非 GET 请求缺 `X-Requested-With` → 403；
  带头通过；login/logout 豁免；Bearer 请求无需该头。
- AC14（登出清 cookie）：logout 后 auth_tokens 行删除、cookie 失效、
  `Set-Cookie Max-Age=0`。
- AC11（人工验收，本轮代码就绪后部署环境执行）：浏览器打开恶意文档
  无脚本执行、无法读取登录凭据（HttpOnly 生效：DevTools 不可见 token、
  document.cookie 无 pap_token）。

## 五、验收与回归门槛

1. 红测试先行：AC1-AC10、AC12-AC14 用例先写先失败，失败对准目标行为；
2. 实现转绿后全量回归 `.venv\Scripts\python.exe -m pytest -q`，
   门槛 **≥622 passed, 0 failed**（新增用例全部通过，18 skipped 语义
   不变）；
3. TRAE-code-review 双子代理评审 → 修复选择 → 复回归；
4. 文档同步（README 版本行 + 记录、docs/PLAN.md 计划外交付记录、
   SECURITY.md 8.1）→ 单一 commit（含 11-01 报告入库）。

## 六、测试影响面与风险

- `test_sessions.py`：delete_session 返回值语义不变（删消息条数），
  新增 meta 函数用例；
- `test_session_acl.py`：预期全兼容（AC5 守护）；
- `test_agent_llm.py`：不动（purge 编排在 main，fake checkpointer 注入
  STATE 验证）；
- cookie 认证：现有 Bearer 测试（test_auth.py / test_admin_users.py 等）
  全量保留验证不回归；新增 test_auth_cookie.py（AC12-AC14）；
- 风险：secrets.token_hex 生成的 16 字符 ID 与前端 8 字符展示兼容
  （服务端透传，前端无格式假设）；cookie 改造涉及两个页面全部 fetch
  请求路径（authHdr 语义变化逐点核对）；cookie 迁移后浏览器侧登录/登出/
  会话恢复全流程需人工过一遍（AC11 一并验证）。

## 修订注（v0.17.40 外部复验整改，2026-11-02）

- **消毒器重复同名属性（N1）**：原 `dict(attrs).get(url_attr)` 取末次校验、输出全量保留，「危险在前、掩护在后」可绕过「输出即可信 HTML」契约。现 URL 校验与输出同取首次出现（浏览器 first-wins 语义），同名属性只发首个。回归用例：test_kb_source_xss.py::test_ac7_duplicate_attribute_first_wins。
- **存量混合会话回填（N4，用户裁决收紧，对外行为变更）**：resolve_owner 回填规则由「最早 user 消息作者」收紧为「最早非 anonymous 发言者；全部 anonymous 才归 anonymous」——混合会话不再触发 anonymous 宽恕（任意登录用户可读写），fail-closed 方向，仅影响 v0.17.37 前存量数据。回归用例：test_session_acl.py test_n4_*（×3，含宽恕保留对照）。
