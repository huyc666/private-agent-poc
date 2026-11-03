# 安全加固 v0.17.41：共享技能审批收口 + 审批职责分离（spec）

状态：**已确认（用户裁决：§四四个取舍点全部按建议执行——4.1 采纳 /
4.2 三项全按建议 / 4.3 采纳 / POST /api/skillpacks 通用导入本轮不做）**（基线 ad7f0c6，677 passed / 18 skipped；本轮为 PLAN
后续轮次待办 #2，来源：复核报告
`docs/reviews/2026-11-01-v0.17.37-commit-rereview.md` §2 P1 +
v0.17.38 spec「不做」注）。

本文含两项**对外行为/权限模型变更**，取舍点（§四）已经用户裁决，
按裁决结论进入红测试与实现。

## 一、背景与问题面（逐行核实）

复核报告 §2 P1 原文：「共享技能仍经 `server/tools_admin.py:44-50` 直接
写入，未统一审批；高风险审批仍缺少发起/批准职责分离。」

核实证据（ad7f0c6）：

1. **共享技能写入无审批**：`tools_admin.py:44-50` 的 `create_skill` 工具
   直调 `skills.create_skill`。存储分流（v0.17.9）：user 角色落本人专属
   域 `skills_personal/<域>/`（自助免审批，影响面限于本人，合理）；
   **approver/admin/开放模式写通用 `skills/`，全员可见、下轮即生效、
   全程无审批**。对照同类资产：`create_custom_tool` 的通用路径走
   `approval.require_approval`（tools_admin.py:104），包管理全系列
   （enable/disable/create/delete/sign_pack）全部过审批门——共享技能是
   唯一例外的全员可见能力资产写入路径。
2. **审批缺职责分离**：`approval.resolve()`（approval.py:200-219）在
   v0.17.9 收紧后仅要求决议人角色为 admin/approver——**发起者本人若为
   admin/approver，可批准自己发起的审批**（自审自批）。发起者身份已落库
   （`approvals.user_id`，v0.14.0 起），具备判定条件但未启用。
3. **无栅栏 `create_approval` 留存面**：v0.17.38 spec 3.5 注明「保留供
   tools_admin 共享技能路径」——实际该路径并未走审批；真实调用面为
   ① `agent.py:665` generation=None 防御分支（生产唯一调用方
   main.py:604 恒传 generation，该分支生产不可达）；② mock.py 三处演示
   （mock 演示流在 stream_reply 会话态内运行，但未传代际）；③ 测试。

## 二、方案总览

**A. 共享技能写入统一审批**：`tools_admin.create_skill` 的非专属域分支
（approver/admin/开放模式写通用 `skills/`）包 `approval.require_approval`
（tool=`create_skill`，args 含 name/description/overwrite + 指令前 300
字预览，与 create_custom_tool 的 code_preview 同口径），批准后落盘。
user 角色专属域路径保持自助免审批（原则不变：影响面限于本人）。
`skills.create_skill` 存储函数本身保持纯写盘（审批门在工具入口层，
与 create_custom_tool 分层一致）；既有的直接调用 `skills.create_skill`
的测试不受影响。mock.py 技能演示剧本同步改为「审批后落盘」演示口径
（与 create_pack 演示一致），保持演示与真实行为一致。

**B. 审批发起/批准职责分离**：`approval.resolve()` 增加自批校验——
审批记录 `user_id` 可判定（非 NULL、非 anonymous）且等于决议人
username 时，返回 "forbidden"（自审自批拒绝）。待裁决边界见 §四.2。

**C. 无栅栏 `create_approval` 收口**：mock 演示流的审批创建改走代际
栅栏——`mock_stream` 签名透传 `session_id/generation`（agent.py:568
调用点两者均在作用域内），mock 内 `create_approval` →
`create_approval_fenced`，栅栏拒绝（None）时按「会话已删除」中止演示
（与 O1 修复的入口栅栏同口径）。此后无栅栏 `create_approval` 仅存于
`agent.py:665` 防御分支（generation=None，生产不可达）与测试，保留作
非会话态兜底，不删除。

**不做（本轮明确排除）**：
- HTTP `POST /api/skillpacks` 通用导入路径：同步 HTTP 端点，
  interrupt 审批模型（图上下文）不适用；收口需「上传暂存 + 异步审批
  后生效」机制，规模明显超出本项。端点已有角色门槛（仅
  approver/admin/开放模式可导入通用层），维持现状并在 SECURITY.md
  标注为已知取舍。——**若用户要求一并收口，需另行评估规模**。
- Cookie 生产化（待办 #4）、真实浏览器 E2E（待办 #5）。

## 三、验收标准（AC，红测试先行）

- AC1（共享技能审批门·工具层）：admin 上下文调用
  `tools_admin.create_skill`（通用路径、非图上下文）→ 返回
  fail-closed 拒绝文案（审批流不可用），`skills/<name>/` 未落盘。
  （红态：当前直写成功。）
- AC2（专属域不受波及）：user 上下文调用 `tools_admin.create_skill`
  → 仍自助落盘 `skills_personal/<域>/`，无审批。
- AC3（审批通过才落盘）：图上下文 mock interrupt 决议 approve →
  执行落盘；reject/非 dict 决议 → 不落盘（require_approval 既有语义，
  新增 create_skill 分支覆盖）。
- AC4（职责分离）：admin「alice」发起的 pending（user_id=alice），
  alice 自己决议 → forbidden，状态仍 pending；另一 approver「bob」
  决议 → ok。（红态：当前 alice 自批成功。）
- AC5（分离豁免面）：开放模式（user_id=anonymous）发起 → 开放模式
  决议正常通过（不死锁）；存量 NULL user_id 记录决议不受影响；
  user 角色发起的审批由 admin 决议正常通过（发起者≠决议者）。
- AC6（mock 栅栏收口）：mock 演示流在会话态运行，
  `create_approval_fenced` 代际不匹配 → 演示中止不创建 pending；
  代际匹配 → 正常创建。mock_stream 全调用面签名更新。
- AC7（回归）：全量 ≥677+N passed / 0 failed / 18 skipped。

## 四、待用户裁决的取舍点（权限模型/对外行为变更）

### 4.1 共享技能审批门（方案 A）

- 变更面：admin/approver 在对话中创建/覆盖**共享技能**从「立即生效」
  变为「需另一 admin/approver 在审批面板批准」；开放模式同样出现
  审批卡片（与其他危险工具口径一致）。个人专属技能不变。
- 影响范围：tools_admin.create_skill 工具行为、mock 演示剧本、
  前端审批卡片新增 create_skill 类型（前端对 approval_required 事件
  通用渲染，无需改动）。
- 取舍：统一了「全员可见能力资产必审批」口径；代价是管理员多一步
  审批操作。**建议采纳**。

### 4.2 职责分离边界（方案 B）——三项子裁决

1. **分离范围**：（a）全部审批统一分离（口径简单一致，含
   delete_workspace_file 等低危项）；或（b）仅高风险子集
   （create_pack/delete_pack/enable_pack/disable_pack/sign_pack/
   create_custom_tool/create_skill）分离，低危豁免。**建议 (a)**：
   按工具名单维护豁免集是长期负担，统一分离的误伤面只是「低危操作
   也需他人批准」，与审批门本身代价同阶。
2. **开放模式**：发起与决议身份同为 anonymous，硬分离必然死锁
   （无人能批）。**建议豁免**（开放模式定位单机自用，SECURITY.md
   标注「开放模式无职责分离」）。
3. **单决议人部署死锁**：AUTH 开启但只配置一个 admin/approver 时，
   该账号发起的审批无人能批，危险操作整体不可用（fail-closed 方向的
   可用性代价）。选项：（a）硬分离 + 文档要求至少两名决议角色
   （README/SECURITY 部署注意事项）；（b）配置开关（如
   APPROVAL_SELF_DECIDE=1 显式退出分离，默认分离）；（c）软分离
   （允许自批但审计打标）。**建议 (a)**：与全仓 fail-closed 口径一致，
   不引入新配置面；部署注意事项写清即可。若用户重视单管理员 POC
   演示便利性可选 (b)。

### 4.3 mock 演示流栅栏（方案 C）

mock 演示内审批创建改走代际栅栏，是 v0.17.38「不做」注的自然收口，
对外行为变化仅在「会话已删除时 mock 演示不再创建 pending」——
fail-closed 方向。**建议采纳**。

## 五、测试策略

红测试先行：AC1/AC4 应先确认红态（当前直写成功 / 自批成功）再实现。
新增用例约 7-9 个：AC1-AC3（tests/test_tools_admin.py 或新文件）、
AC4-AC5（tests/test_approval.py）、AC6（tests/test_mock_weather.py）。
既有 `skills.create_skill` 直调测试（存储层）不变；既有审批测试
（test_approval.py 等）中「同一 identity 创建+决议」的用例若存在需
按新口径修订（预期决议方与被决议方身份区分）。
