# 安全加固 v0.17.33：v0.17.32 复验遗留项闭环

状态：已实现并通过全量回归（597 passed / 16 skipped）与 TRAE-code-review
双子代理评审（评审修复 C1/C2、补注释与测试 C4、证伪排除 C3，见第五节）。

## 一、背景

外部复验对 v0.17.32 判定 5/7 部分修复（仅 SEC-2/SEC-5 完全通过），且实测复现
三处绕过。本 spec 承接复验确认的 5 项问题 + 1 项提交说明偏差修正，全部已在
`105765f` 代码上核实属实（证据见各节）。

复验确认（baseline `105765f`）：
- R1 chat 归属检查在锁外（main.py L483），锁内 append 前无二次检查——并发窗口；
- R2 packs.py 三处 `pack_dir / str(rel)` 无包含关系校验，`../outside.py` 实测
  status=ok；变更检测签名（custom_tools.signature L137 / packs_signature L580）
  只含 st_mtime_ns，篡改内容 + 恢复 mtime 不触发重扫重验；
- R3 exec_mode() L298 对非法配置值静默落 auto 分支 → 沙箱不可达回退 local；
- R4 CONTEXT_MAX_TOKENS 默认仍 128000（config.py L79）vs 自带 vLLM 32768
  ——v0.17.32 按当时 spec 只做文档提示，默认行为未闭环；
- R5 build_kb 两步 os.replace（db→bak，tmp→db）之间进程中断 → 现役库缺失
  只剩 bak；C4/C8 只覆盖 Python 异常不覆盖进程级中断；
- 提交说明偏差：commit 105765f / README / PLAN 中「包资源路径解析增加包目录
  包含关系校验」超出实际实现（spec v0.17.32 SEC-4 仅含 invalidate 重扫验签，
  packs.py 实际仅改 docstring）。

## 二、方案（六项）

### AC1（R1）：chat 锁内二次归属检查

`event_stream()` 内取得会话锁与租约后、首次 `sessions.append` 前再次调用
`_chat_session_denied`——并发场景下首轮写入此时已落库可判定归属；被拒以
SSE `error` 事件终止（已进入流无法改 HTTP 状态码）。锁外首查保留（省去
无谓进锁/建租约开销）。

### AC2（R2a）：包资源路径包含关系校验

packs.py 新增 `_resolve_in_pack(pack_dir, rel)`：`(pack_dir / rel).resolve()`
后校验 `relative_to(pack_dir.resolve())`，越界（`../`、绝对路径、符号链接出界）
跳过并告警；tools/skills/prompt 三处接入。fail-closed：越界项不进包。

### AC3（R2b）：变更检测签名改内容哈希

`custom_tools.signature()` 与 `packs.packs_signature()` 的文件签名从
`st_mtime_ns` 改为 `sha256(read_bytes())` 摘要——堵「篡改内容 + 恢复 mtime」
绕过。工具/清单/提示词均为小文件，每轮对话一次检测开销可忽略。
`skills.signature()` 本次不动（不在复验范围）。

### AC4（R3）：执行模式严格白名单

`exec_mode()`：`CUSTOM_TOOLS_EXEC` 显式配置但不在 `auto|sandbox|local` 白名单
内 → 按 `sandbox` 处理（沙箱不可达即 `sandbox-down` 拒绝，fail-closed）+ 首次
显著 WARNING（60s 限频）指明非法值。不再静默落 auto。

### AC5（R4）：CONTEXT_MAX_TOKENS 默认 32768

config.py 默认值 `128000 → 32768`；`.env.example` 注释示例有效值同步 32768
（换大窗口模型再改回）；README 环境变量表同步。取舍（fail-safe 方向）：大窗口
模型部署未显式配置会提前触发压缩（功能退化），优于 32k vLLM 部署忘配置导致
长会话在压缩触发前超窗（上下文截断、数据丢失级）。

### AC6（R5）：build_kb 无中断窗口原子替换

现役库不再转存改名：`shutil.copy2(db, bak)` 生成备份（现役库保持原位、全程
可查），随后单次 `os.replace(tmp, db)` 原子完成（保留 3 次重试）。任意时刻进程
中断：旧库或新库必有一个在位。C8 的 `moved_to_bak` 回滚分支随之删除（不再存在
「bak 已转存、现役缺失」状态）。

### 文档修正（不设 AC，随提交）

README/PLAN 中 v0.17.32 行「包资源路径解析增加目录边界校验」表述修正为实际
范围（该项 v0.17.33 才落地）；v0.17.33 版本记录如实登记。

## 三、测试映射（先行）

| AC | 测试 | 说明 |
|---|---|---|
| AC1 | tests/test_session_acl.py 增 | 窗口注入（锁外放行后进锁前归属已变 → SSE error 且无写入）；本人续聊锁内放行回归 |
| AC2 | tests/test_packs.py 增 | `../outside.py` 与绝对路径 → 跳过不进包；正常相对路径不受影响 |
| AC3 | tests/test_custom_tools.py / test_packs.py 增 | 恢复 mtime 篡改内容 → 签名变化 → 重建重扫 → 强制验签下 trust=invalid 剔除 |
| AC4 | tests/test_custom_tools.py 增 | 非法值（真实模式）→ sandbox-down；MOCK 模式非法值同样不再落 auto 本地回退 |
| AC5 | tests/test_deploy_config.py 增 | 默认 32768；.env.example 有效示例同值（README 表格行断言为评审 C2 补） |
| AC6 | tests/test_kb_ingestion.py 改/增 | 替换失败旧库在原位（改 C8 断言）；替换点注入 BaseException（模拟 kill）→ 现役库完好 |
| 评审 | test_custom_tools.py 增 / test_deploy_config.py 补断言 | C1 读失败 '?' 兜底（test_review_c1）；C4 MOCK+非法值组合（test_review_c4）；C2 README 表格行默认列断言 |

基线：587 passed / 16 skipped（v0.17.32 提交后全量）。完成后全量回归零失败。
实际结果：AC 实现后 595 passed / 16 skipped（+8 新用例）；评审修复后
597 passed / 16 skipped（+2 评审用例）。

## 四、澄清与取舍记录

1. AC1 不采用「请求级 403」处理二次拒绝：SSE 已开流，状态码不可改；error 事件
   是既有约定（租约超时/接管同此）。
2. AC4 采用「按 sandbox 处理 + 告警」而非「拒绝启动」：与本项目 fail-closed
   惯例一致（身份头白名单缺失时忽略 + 告警），避免新增启动期硬失败路径。
3. AC5 改默认值影响范围：显式配置过的部署不受影响；DeepSeek 等 128k 模型部署
   需在 .env 显式设 128000（README/示例注明）。
4. AC6 copy 较 mv 多一次读旧库（索引通常 MB 级，秒级内），换取任意中断零窗口。
5. 既有 mtime 检测相关用例兼容性：内容变化必然引起哈希变化，既有「改文件触发
   重载」用例应全部保持绿；若有用例直接断言签名含 mtime 结构则同步更新断言。

## 五、评审记录（TRAE-code-review 双子代理交叉验证）

范围：本 spec 全部变更（11 改 + 1 新，+272/-58）；主代理自查候选 4 项，
双子代理独立验证（互不可见），按 2/2 确认纳入 / 0/2 证伪排除裁决：

- **C1【2/2 确认，已修复】** custom_tools.signature() 的 read_bytes() 无
  try 保护：双子代理均以独占句柄实测复现 PermissionError；调用点在
  agent.py 每轮对话开头，SSE 流的 try 只有 finally 无 except，读失败直接
  打断本轮对话；与 packs._file_sig 的 '?' 兜底口径不对称。修复：新增
  custom_tools._file_sig() 对齐 '?' 兜底（'?' 不同于任何成功哈希，异常
  状态仍触发重载，不吞）；用例 test_review_c1_signature_read_failure_falls_back。
- **C2【2/2 确认，已修复】** README 环境变量表 CONTEXT_MAX_TOKENS 默认值
  列残留 128000（AC5 漏同步；旧测试只断言正文含 32768 未覆盖表格行）。
  修复：默认列改 32768、指引改「128k 模型须显式设 128000」；既有用例补
  表格行默认列断言。有意保留的 128000 提及：.env.example 指引、config.py
  注释、spec 历史记录。
- **C3【0/2 证伪，排除】** 非法值告警与 auto 回退告警共享 _auto_warn_cache
  属实，但 CUSTOM_TOOLS_EXEC 进程内单值固化：非法值分支改 sandbox 后提前
  返回，永不进 auto 分支——生产中两告警互斥，不可能互相压制；测试隔离
  已被现有用例显式重置处理。不改（误报排除，理由如上）。
- **C4【2/2 带疑，有意保留 + 补注释与测试】** MOCK=true + 显式非法值 →
  sandbox-down → 自定义工具全清空，行为链属实；双子代理一致定性：符合
  本 spec AC4/澄清 2 的有意 fail-closed（MOCK 只联动「未显式配置」时的
  默认值），非缺陷。处置：行为保留，exec_mode docstring 补明示（含
  v0.17.32→v0.17.33 行为变化说明），补用例
  test_review_c4_mock_mode_invalid_value_still_fail_closed。

范围外附注（不随本次提交）：mock.py 同步调用 maybe_reload_custom_tools
（agent.py 侧为 to_thread），C1 修复后无崩溃风险、仅剩阻塞事件循环的既有
问题，留待用户决定是否后续单独处理。
