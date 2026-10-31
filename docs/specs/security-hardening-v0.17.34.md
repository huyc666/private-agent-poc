# 安全加固 v0.17.34：领域包信任链闭环

状态：已实现并通过全量回归（603 passed / 17 skipped）与 TRAE-code-review
双子代理评审（评审修复 C2/E1、有意保留 C4/C6、证伪排除 C1/C3/C5/C7，
见第五节）。

## 一、背景

外部复验以 v0.17.33（`ab3488f`，全量 597 passed / 16 skipped）为基线，判定
五项整改中四类闭环、领域包信任链仍有遗留，并实测复现三处绕过。本 spec 承接
该复验结论，四项问题全部已在 `ab3488f` 代码上核实属实（证据见各节）。

复验确认（baseline `ab3488f`）：
- R1 `skills.signature()`（skills.py L356-373）仍用 `st_mtime_ns`，且
  `packs.packs_signature()` 载荷只含 pack.yaml + tools + prompt、不含
  skills 文件 → 篡改已签名包的 `SKILL.md` 并还原 mtime，技能层与包级
  双层变更检测全漏（复验实测 `skill_reload_signature_unchanged=True`，
  不触发图重建与重新验签）。注：该项系 v0.17.33 spec AC3 的**明示范围
  决策**（「skills.signature() 本次不动（不在复验范围）」），本轮补齐
  闭环，非上轮遗漏；
- R2 packsign 签名载荷只含 `pack.yaml` + `tools/**` + `skills/**` +
  `prompt.md`，而 v0.17.33 的 `_resolve_in_pack` 允许清单引用**包内任意
  路径** → manifest 引用 `code/demo_tool.py` 的签名包，篡改该工具后
  `status=ok` 且 `signed` 双绿（复验实测 `noncanonical_tool_after_tamper=
  ok signed`）——签名与变更检测双重盲区，本轮最优先问题；
- R3 技能叶子 `(技能目录 / "SKILL.md")` 仅 `is_file()` 检查（跟随符号
  链接出包）；且沙箱固定执行 `/packs/<pack>/tools/<name>.py`
  （sandbox/server.py L165），与清单声明路径脱钩——声明路径与沙箱实际
  执行路径可能指向不同文件。

## 二、方案（四项）

### AC1（R1a）：skills.signature() mtime → 内容哈希

`skills.signature()` 的文件签名从 `st_mtime_ns` 改为 `sha256(read_bytes())`
摘要，读失败返回 `'?'` 兜底（对齐 custom_tools._file_sig / packs._file_sig
口径——每轮对话开头都做签名检测，读失败上抛会打断 SSE 流；`'?'` 与任何
成功哈希都不同，异常状态仍触发一次重载，不静默吞掉）。技能文件为小文本，
每轮对话一次检测的开销可忽略。v0.17.7 评审 P2「纳秒 mtime 解决秒级精度」
的决策随之被内容哈希全面取代（内容变化必然引起哈希变化，时间戳不再参与
签名）。

### AC2（R1b）：packs_signature() 载荷补 skills 文件

`packs.packs_signature()` 在清单/工具/提示词之外补入各挂载技能目录的
全部文件内容哈希（`rglob` 递归、排序后逐文件），与 packsign 的
`skills/**` 载荷口径对齐。效果：包级篡改 SKILL.md（含还原 mtime）触发
重扫重验，强制模式下 trust=invalid 整包剔除。与 AC1 双层互备——包被
禁用（技能未注册进技能层索引）或仅技能附属文件被篡改时，包级签名仍是
防线。

### AC3（R2+R3b）：清单规范路径白名单（fail-closed）

manifest 引用只接受规范相对路径，非规范跳过并告警：
- 工具：`tools/<文件名>.py`（平铺一层，与沙箱执行路径
  `/packs/<pack>/tools/<name>.py` 完全一致）；
- 技能：`skills/<一级目录>`；
- 提示词：`prompt.md`。

一举闭合两处：签名盲区（所有可挂载资源全部落在 packsign 载荷的
`tools/**` + `skills/**` + `prompt.md` 内）与沙箱路径脱钩（声明路径即
沙箱执行路径）。保留 v0.17.33 的 resolve 包含校验作第二道防线（规范
路径中的文件名成分仍可能是符号链接）。仓库现有 5 个包（core-knowledge /
core-utils / kb-demo / kb-finance-demo / weather-ops）清单全部规范，
零破坏。

### AC4（R3a）：SKILL.md 叶子包含校验

`_parse_pack` 技能分支对 `(技能目录 / "SKILL.md").resolve()` 再做一次
包目录包含校验——叶子自身是指向包外的符号链接时跳过并告警（此前
`is_file()` 会跟随链接导致越界读取注入索引）。

## 三、测试映射（先行）

| AC | 测试 | 说明 |
|---|---|---|
| AC1 | tests/test_skills.py 改/增 | 篡改内容+还原 mtime → 签名变化（test_ac1_*，取代旧 test_signature_uses_nanosecond_mtime，其断言基于 mtime 检测、AC1 后语义反转）；仅改 mtime → 签名稳定；读失败 → '?' 兜底 |
| AC2 | tests/test_packs.py 增 | 篡改包内技能+还原 mtime → packs_signature 变化；端到端：强制验签下篡改已签名包 SKILL.md+还原 mtime → 重扫 trust=invalid 整包剔除 |
| AC3 | tests/test_packs.py 增 | `code/demo_tool.py`（复验场景）/嵌套/`skills/a/b`/`skills/x/SKILL.md`/`docs/prompt.md` 全部拒收，规范项不受影响 |
| AC4 | tests/test_packs.py 增 | SKILL.md 叶子符号链接出包 → 技能拒收；包内正常技能不受影响（环境无法创建符号链接时 skip） |

基线：597 passed / 16 skipped（v0.17.33 提交后全量）。完成后全量回归零失败。
实际结果：AC 实现后 602 passed / 17 skipped（+5 通过：packs 3 + skills 净增 2；
+1 skip 为本机无符号链接特权、AC4 用例环境降级，Linux/CI 恒运行）；评审修复
C2/E1 后 603 passed / 17 skipped（C2 回归断言扩展进 test_ac3，E1 补用例
test_review_skill_manifest_duplicate_entries_deduped）。唯一 warning 为既有
LangGraph create_react_agent 弃用提醒，与本次变更无关。

## 四、澄清与取舍记录

1. AC1 读失败兜底沿用 '?' 口径而非上抛：与 custom_tools（v0.17.33 评审
   C1 修复）和 packs._file_sig 既有约定一致，异常状态触发重载优于打断
   本轮对话。
2. AC2 载荷取「挂载技能目录全量文件」而非仅 SKILL.md：与 packsign
   `skills/**` 覆盖对齐，技能附属文件（references/ 等）被篡改时同样
   触发重扫重验；技能目录均为小文本，开销可忽略。
3. AC3 采用「清单声明白名单」而非「扩大 packsign 载荷至包内任意文件」：
   签名应覆盖**约定布局**而非物理目录——任意路径入载荷等于给
   「签名与布局解耦」背书，且沙箱固定执行 `tools/<name>.py` 的路径
   脱钩问题只有白名单能闭合。非规范路径属打包方错误，fail-closed 跳过
   + 告警与既有「工具文件不存在，跳过」同语义，不升级为整包 error
   （包内其余能力仍可用，list_packs 可见告警定位）。
4. AC3 只约束结构不约束字符集：工具文件名字符集由加载器（custom_tools
   NAME_PATTERN）与沙箱（_PACK_NAME + 文件名拼接）继续把关，两道检查
   语义不同，不在清单层重复。
5. AC4 校验时机为扫描期（与全部既有检查一致），读取期的 TOCTOU 窗口
   与项目现状同水位，不在本轮扩大。
6. 旧用例 test_signature_uses_nanosecond_mtime 改写：其场景（秒级
   mtime 无法分辨快速更新）在内容哈希口径下不再成立，保留会误判回归。
7. 范围外（不随本次处理，沿用 v0.17.33 spec 附注）：mock.py:77 同步调用
   maybe_reload 的既有阻塞问题；真实模型/vLLM/Docker 运行时验证（16 个
   门控 skip 维持）。均留待用户决定是否后续单独处理。

## 五、评审记录（TRAE-code-review 双子代理交叉验证）

范围：本 spec 全部变更（4 改 + 1 新）；主代理自查候选 7 项（C1-C7），
双子代理独立验证（互不可见），按 2/2 确认纳入 / 1/2 带疑纳入（标注分歧
与处置）/ 0/2 证伪排除裁决；修复范围经用户确认为「修 C2+E1，C4/C6 有意
保留」：

- **C1【0/2 证伪，排除】** 疑 skills.signature() 移除旧 try/OSError 引入
  新异常路径——旧 try 只包 stat/relative_to（rglob 物化迭代异常两代均
  上抛，relative_to 抛 ValueError 本就不在保护内），新代码 read 失败由
  _file_sig 捕获返 '?'，保护范围等价，失败模式从「跳过条目」变「'?'
  条目」即 AC1 设计意图。
- **C2【1/2 带疑，已修复】** Windows 混合分隔符 `tools/mix\tool.py`：
  PurePosixPath.parts 判其为单段文件名而放行白名单，物理 resolve 却成
  嵌套文件 tools/mix/tool.py——声明与沙箱平铺执行 tools/tool.py 错位，
  AC3 平铺意图被打破（证伪方未实测该形态；实测仅审计错位、文件仍在
  tools/** 签名载荷内，无未签名执行）。修复：白名单显式拒绝含反斜杠
  的声明并单独告警（manifest 规范路径不含反斜杠）；回归断言扩展进
  test_ac3（非规范告警计数 5→7）。
- **C3【0/2 证伪，排除】** 疑 AC4 叶子指向包内的符号链接（如
  skills/a/SKILL.md → tools/x.py）构成越界——属同包信任域，且
  packsign rglob 与双层检测签名、skills 层 read_bytes 三处均跟随链接
  读目标内容入哈希，篡改目标或链接必使验签与检测双失效，无盲区。
- **C4【2/2 确认，有意保留】** 三处 _file_sig 重复（custom_tools /
  packs / skills）属实：skills 反向导入 packs 即成循环导入（packs.py:26
  模块级 import skills，_file_sig 在模块尾部，先导入场景必 ImportError；
  导入 custom_tools 版无环但引入跨模块耦合）。双子代理均判「受环约束
  的务实重复、可接受」。处置：保留三份口径一致实现，skills._file_sig
  docstring 补明保留理由。
- **C5【0/2 证伪，排除】** 疑检测签名（只含清单声明的技能目录）与
  packsign 验签载荷（skills/** 全量）差集构成缺口——清单声明本身在
  清单哈希内（改声明 → 重扫 → 重验签 → 因未声明目录已变而 invalid，
  fail-closed）；未声明 skills 目录经 register_extra_dir(父目录) 注册
  仍进技能层签名；AC3 后未声明资源不可挂载，无运行时读取通道，闭环。
- **C6【1/2 带疑，有意保留】** 声明技能目录内的 Windows junction 会把
  包外文件拉进检测载荷（实测 Py3.12 rglob 跟随 junction；证伪方判签名
  同机同进程稳定可比）——方向 fail-closed（伪重载/验签失效），且
  packsign 同款 rglob 口径、双载荷一致，无未签名执行。处置：保留该
  行为并记录为已知语义（与检测/验签共享同一遍历口径是特性而非缺陷）。
- **C7【0/2 证伪，排除】** 疑端到端用例的基线稳定步骤依赖 agent.py
  「先捕获签名后注册技能目录」顺序——两种顺序下用例均正确：现状下
  稳定调用吸收一次目录新增重建、使篡改成唯一触发；若改为先注册后
  捕获，稳定调用退化为无害空转，篡改后仍检出，断言同样成立。

评审附注（单方额外发现，未过双验证、按用户确认处置）：
- **E1【已修复】** skills 清单条目未像工具分支（v0.17.27 tool_stems）
  去重——重复声明同一目录会重复进入 info["skills"] 与签名载荷（无
  运行时危害，register_extra_dir 去重兜底，仅分支不对称）。修复：
  skill_dirs 集合去重（与工具分支对称）+ 用例
  test_review_skill_manifest_duplicate_entries_deduped。
- **排除附注**：`tools/.py`、`tools/..py` 等空 stem 边界由加载器
  NAME_PATTERN fail-closed 兜底（实验验证 suffix/stem 语义后确认链路
  完整，告警文案略偏但可定位）；「agent 工具名允许连字符 vs 沙箱
  _TOOL_NAME 不允许」为既有不一致、非本次引入，范围外留待用户决定。
