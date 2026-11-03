# 真实浏览器端到端验收清单（E2E Acceptance）

来源：docs/PLAN.md 后续轮次待办 #5（AC11，部署期人工验收项，测试框架无法替代）；
docs/specs/security-hardening-v0.17.37.md（AC11 定义）；
docs/reviews/2026-11-01-v0.17.37-architecture-rereview.md §3（人工 E2E 须执行并留证）。

范围（限 AC11 原文，用户裁决 2026-11-03）：① 登录/登出/会话恢复；
② HttpOnly 凭据不可读；③ 恶意 docx 渲染无脚本执行；④ mock 演示流审批 UI
出现与决议后恢复执行。**Secure Cookie 链路为部署期（HTTPS 前置）待验项，
本机 localhost 无法验证，不得标记为通过。**

## 一、前置条件与启动

```powershell
# 1. 建验收用户（审批职责分离：发起人与决议人不得为同一人，v0.17.41）
.venv\Scripts\python.exe tools\manage_users.py list
.venv\Scripts\python.exe tools\manage_users.py add e2eapprover --role approver
#   ↑ 打印一次性 pak- Key，立即保存（控制台「用 Key 进入」用）
.venv\Scripts\python.exe tools\manage_users.py passwd admin --password "<测试口令>"
.venv\Scripts\python.exe tools\manage_users.py passwd e2eapprover --password "<测试口令>"

# 2. 投放恶意 docx（验收后删除）：kb/hr/evil-e2e.docx，正文含
#    <img src=x onerror=alert(1)> 等标签字面量（构造脚本见 tests/test_kb_source_xss.py
#    的 _mk_evil_docx；kb/hr 由 kb-demo 包声明，admin 或 hr 部门账号可见）

# 3. 启动（Mock 演示模式 + 强制认证；PowerShell 环境变量优先于 .env）
$env:MOCK_LLM="true"; $env:AUTH_ENABLED="true"
.venv\Scripts\python.exe server\main.py --port 7100
```

预期启动日志含 `AUTH_COOKIE_SECURE=false` 的 HTTPS WARNING（本地 HTTP 属预期，
不阻断）。访问入口：聊天页 http://127.0.0.1:7100/ ，控制台
http://127.0.0.1:7100/console 。

## 二、分链路步骤表

### E2E-1 登录 / 登出 / 会话恢复（Cookie 迁移全流程）

| 步骤 | 预期 | 实际 | 结论 |
|---|---|---|---|
| 1. 未登录访问 `/` | 出现登录遮罩，API 返回 401 | | |
| 2. 账号密码登录 admin | 登录成功进入聊天页；响应 `Set-Cookie: pap_token=...; HttpOnly; SameSite=Lax` | | |
| 3. 发一条消息再刷新页面 | 会话仍在（Cookie 会话恢复），无需重新登录 | | |
| 4. 点击登出 | 返回登录遮罩；`Set-Cookie` Max-Age=0；刷新后仍须登录 | | |

### E2E-2 HttpOnly 凭据不可读（AC11 核心）

| 步骤 | 预期 | 实际 | 结论 |
|---|---|---|---|
| 1. 登录后 DevTools Console 执行 `document.cookie` | 输出中**无** `pap_token` | | |
| 2. DevTools → Application → Cookies | 可见 `pap_token` 条目且 **HttpOnly 列勾选**（HttpOnly 含义：JS 不可读，非 DevTools 不可见条目本身） | | |
| 3. 页面 JS 全局搜索 localStorage | 无 `pat-` 令牌残留（v0.17.37 起登录态不落 localStorage） | | |

### E2E-3 恶意 docx 渲染无脚本执行（AC11 核心）

前置：登录态下打开
`/api/kb/source?kb=hr&doc=evil-e2e.docx&kind=para&a=1&b=99`。

| 步骤 | 预期 | 实际 | 结论 |
|---|---|---|---|
| 1. 打开原文页，全程监听 dialog/console/pageerror | 无 alert/confirm 弹窗；无注入脚本产生的 pageerror | | |
| 2. 检查渲染 DOM | 攻击载荷以**转义文本**可见（如 `<img src=x onerror=alert(1)>` 显示为文字），DOM 中不存在对应 `<img>`/`<script>` 节点 | | |
| 3. 检查响应头 | 渲染页带 `Content-Security-Policy`（nonce 版）与 `X-Frame-Options` | | |

### E2E-4 mock 演示流审批 UI 链路（中断 → 决议 → 恢复）

| 步骤 | 预期 | 实际 | 结论 |
|---|---|---|---|
| 1. admin 聊天页发送「创建一个领域包」 | 流式回复中出现审批卡片（approval_required，含审批单号），流挂起等待决议 | | |
| 2. 另开控制台标签页，用 e2eapprover 的 pak- Key「用 Key 进入」→ 审批面板 | 可见该 pending 审批单（发起人 admin） | | |
| 3. e2eapprover 在面板批准 | 决议成功（职责分离：决议人 ≠ 发起人，不自批） | | |
| 4. 回到聊天标签页 | 流恢复执行，输出「审批已通过」与执行结果，done 事件收尾 | | |
| 5. 对照：admin 尝试决议本人发起的审批单 | 被拒（forbidden，v0.17.41 自批分离） | | |

### E2E-5 部署期待验项（本机不可验，HTTPS 前置部署时执行）

| 步骤 | 预期 | 实际 | 结论 |
|---|---|---|---|
| 1. HTTPS 前置（反向代理终止 TLS）+ `AUTH_COOKIE_SECURE=true` 部署 | 登录响应 `Set-Cookie` 含 `Secure`；浏览器仅在 HTTPS 回传 Cookie | — | **部署期待验** |
| 2. HTTP 直连绕过代理访问 | Cookie 不回传，会话失效 | — | **部署期待验** |

## 三、留证要求

- 每条链路至少一张关键截图（登录后 Cookie 面板 / 恶意文档渲染页 / 审批卡片与
  面板决议），截图**不入 git**，存仓库外目录并在下方执行记录中登记位置。
- 结论只写「通过 / 未通过（原因）/ 未验证（原因）」，不得编造。
- 部署期待验项（E2E-5）在 HTTPS 部署后补验并回填。

## 四、本轮执行记录

### 2026-11-03 本机执行（v0.17.43，验收人：Trae Agent 驱动真实浏览器）

环境：Windows 本机 + 内嵌 Chromium 浏览器；`MOCK_LLM=true AUTH_ENABLED=true`，
端口 7100；验收账号 e2e-admin（admin，Cookie 登录）/ e2e-approver（approver，
控制台 pak- Key 进入，职责分离）。恶意文档 kb/hr/evil-e2e.docx（正文含
`<img src=x onerror=alert(1)>`、`<script>alert(2)</script>` 字面量），验收后已删除。

| 链路 | 结论 | 关键观测 |
|---|---|---|
| E2E-1 登录/恢复/登出 | **通过** | 未登录出遮罩且 `/api/usage` 401；登录 200，`Set-Cookie: pap_token=pat-…; HttpOnly; Max-Age=43200; Path=/; SameSite=lax`；发消息后刷新页面会话恢复（遮罩关闭、历史 2 条）；登出后刷新仍须登录（Cookie 服务端失效） |
| E2E-2 HttpOnly 不可读 | **通过** | 登录态下 `document.cookie` 为空串（无 pap_token）；localStorage 仅 `poc_session`（8 位会话 ID，非令牌）；Set-Cookie 头实测含 HttpOnly。DevTools Application 面板项未单独截图（自动化工具无法操作 DevTools 面板），由响应头 + JS 不可读两项功能证据佐证 |
| E2E-3 恶意 docx 无脚本执行 | **通过** | 原文页正常加载（无 alert 阻塞、无注入相关 console/pageerror）；DOM 中无 `<img>`/注入节点，攻击载荷以转义文本显示；响应头实测 `Content-Security-Policy: default-src 'none'; script-src 'self' 'nonce-…'` + `X-Frame-Options: SAMEORIGIN`；服务端日志含 `/api/kb/sanitize` 200（消毒通道生效） |
| E2E-4 审批 UI 链路 | **通过** | 「创建一个领域包」触发审批卡片（approval_required，create_pack）；admin 点本人审批单「批准」被 403 forbidden（自批分离，服务器日志 `POST /api/approve 403`）；e2e-approver 控制台批准（审批单 0629584aa06b，决策人 e2e-approver）；聊天页流恢复输出「审批已通过」+ create_pack 执行结果。注：首轮因处置耗时超 APPROVAL_TIMEOUT 已超时，第二轮在时限内完成批准 |
| E2E-5 Secure Cookie | **未验证（部署期待验）** | localhost HTTP 下浏览器不接受 Secure Cookie，无法本机验证；HTTPS 前置部署后按 §二 E2E-5 补验 |

证据（截图，不入 git）：`C:\Users\Lenovo\AppData\Local\Temp\trae\screenshots\`
e2e1-logged-in.png、e2e1-session-restored.png、e2e1-after-logout.png、
e2e3-evil-docx.png、e2e4-self-approve-forbidden.png、e2e4-approval-resumed.png 等 7 张。
（Temp 目录重启后可能清理；部署期复验时重新留证。）
