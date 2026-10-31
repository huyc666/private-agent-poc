# 安全加固 v0.17.36：包内目录链接拒绝、技能加载快照与链接循环哨兵

状态：已实施（用户确认 R8 按方案 A 快照实施；TRAE-code-review 评审修复
5 项、有意保留 1 项，最终全量回归 622 passed / 18 skipped / 0 failed）。

## 一、背景

外部复验报告 `docs/reviews/2026-10-31-v0.17.35-c6b303a-review.md` 判定
v0.17.35（`c6b303a`，基线 614 passed / 18 skipped）R5 通过、R4/R6 部分通过，
又交叉确认三项边界，**v0.17.35 暂不能作为领域包信任链完全闭环版本**。
三项问题均已在 `c6b303a` 代码逐行核实属实：

- R7【高】包内目录链接绕过 HMAC：`server/packs.py::_resolve_in_pack`
  （L283-324）只要求技能目录 resolve 后位于包根内——`skills/x -> ../hidden`
  解析为 `<pack>/hidden` 可通过并挂载；但 `server/packsign.py::_canonical_parts`
  （L42-61）只遍历物理 `tools/**`、`skills/**`、`prompt.md`，POSIX
  `Path.rglob` 默认不跟随目录符号链接 → `hidden/SKILL.md` 不进 HMAC 载荷
  → **技能可加载且包 signed**（签名盲区）。Windows junction 会被 rglob
  跟随、内容经链接键入载荷，故该盲区为 POSIX 形态专属；但「挂载目录不在
  skills 树内」的布局违规两平台同构。
- R8【高】加载期 TOCTOU：`server/skills.py::_skill_entry_ok`（L95-112）
  先对原路径 resolve 检查，`list_skills`（L136）/ `load_skill`（L170，
  直查与 frontmatter 兜底两分支）/ `signature`（L412）随后仍按**原路径**
  read_text/read_bytes——检查与读取是两次独立磁盘访问，实测准入返回后
  换入指向包外的 junction（`toctou_swapped=True`、
  `outside_content_loaded=True`），包外内容被返回。「再 resolve/read 一次」
  不闭合（第二次检查与第三次读取仍有同类窗口）。
- R9【中】包根链接循环中断热检测：`server/packs.py::packs_signature`
  （L677-703）对每包直接 `pack["dir"].resolve()`（L690），注入
  `RuntimeError: Symlink loop` 后上抛——`_rel_key`（L665-674）只捕获
  ValueError，热检测每轮对话开头中断；`scan_packs` → `_parse_pack` →
  `_resolve_in_pack` 的 resolve 链、`packsign._canonical_parts` 的 rglob、
  `skills.register_extra_dir`（L70）的 resolve 同样无循环防护。

**已通过项不回退**（本报告与 v0.17.35 复验确认）：未声明兄弟技能过滤、
frontmatter 别名兼容、验签读异常 fail-closed、附属文件纯重命名与恢复
mtime 的重验、签名条目（相对路径, 哈希）二元组口径。

## 二、方案

实施顺序 R7 → R8 → R9（报告第 3 节顺序）。不做无关重构。R7 与 R8 共同
收敛到「实际挂载集合 = 签名集合 = 服务集合」：R7 把住解析期布局，R8 把住
加载期内容来源，R9 保证检测层永不因异常中断。

### AC1+AC2（R7）：技能目录解析后必须仍在 `<pack>/skills/` 树内（严格布局）

报告修复要求 3 明示「严格禁止目录链接是更小、更易验证的方案」，据此选
严格布局而非「把链接目标纳入 HMAC」：

1. `_resolve_in_pack` 技能分支在既有「resolve 后仍在包根内」校验之后，
   追加 `skills_root = (pack_dir / "skills").resolve()`：技能目录 resolve
   后必须满足 `skills_root in p.parents`——指向 `hidden/`、`tools/`、包根
   等包内其他位置的目录链接（POSIX symlink 与 Windows junction 同形拒绝，
   resolve 均穿透）、指向 skills 树自身的自挂载一并拒收（报告修复要求 1）。
2. 叶子 SKILL.md 既有真实路径包含校验与去重校验不动（报告修复要求 2）。
   侧效果：`skills/x -> ../skills/y` 类树内别名经去重分支拒收或按真实目录
   名挂载，其内容必在物理 `skills/**` 载荷内（AC1：实际可挂载目录 ⊆ HMAC
   覆盖）。工具平铺文件/提示词维持包根校验水位（文件符号链接的内容经
   rglob 链接键入载荷，两平台无盲区，不随本次扩大）。
3. 效果：签名集合、热检测集合、索引集合、加载集合再次同一（报告修复
   要求 4；上轮 R4 的 allowed 集合机制继续承担「未声明兄弟过滤」，两者
   正交叠加）。

### AC3（R8）：技能内容快照——服务入口只读快照，检测入口只读磁盘

采用报告优先方案（修复要求 2）「受控不可变技能内容快照」，四入口统一
防护（修复要求 4）：

1. **快照物化**：`_parse_pack` 技能循环四重校验接受后**立即单次**读取
   `SKILL.md` 内容，存入 `info["skill_texts"]`（键 = 解析后技能目录名，
   与 allowed 集合同源同键）——读取紧贴校验，随后注册/索引/加载不再访问
   磁盘。验签（HMAC 载荷读）与快照读之间残余的窗口为同一调用栈内两条
   语句间隔，且任何后续磁盘变化必被热检测捕获（见 3），实现「验签/扫描
   时建立快照、内容变化经签名检测后整体替换」语义。
2. **注册传递**：`skills.register_extra_dir(path, source, allowed, root,
   texts=None)` 扩展第五参数；`_extra_dirs` 条目 4 元组 → 5 元组
   `(父目录, 来源, allowed, 包根, 快照 dict)`。`texts=None` 维持旧行为
   （内核 skills/ 与既有测试直调直读磁盘——内核层不在信任链内）；重复
   注册同一父目录时合并 allowed 与 texts。
3. **服务入口只读快照**（索引 / 直查加载 / frontmatter 别名兜底）：
   `list_skills._scan`、`load_skill` 直查与兜底分支在 `_skill_entry_ok`
   准入后，内容一律取 `texts[dir_name]`（缺失即跳过，fail-closed），
   不再 read_text。目录枚举仍走磁盘 glob——枚举只决定「哪些名字可见」，
   换链后的越界条目被 entry_ok 的 root 校验拒收；名字合法但被换链的
   条目返回的仍是**快照内受验签内容**，攻击者换入任何磁盘形态都改变
   不了服务内容（TOCTOU 窗口从「整个进程生命周期」收敛为「重建周期」）。
4. **检测入口只读磁盘、不返回内容**：`skills.signature` 与
   `packs.packs_signature` 维持读盘（这是热检测的职责本身）——签名
   遍历不向任何调用方返回内容，且快照只在重建时刷新、重建只由签名变化
   触发，故「可加载内容变化 ⇒ 必先反映为签名变化 ⇒ 重建重验 ⇒ 快照整体
   替换」。换链本身同样改变签名（junction 会被 rglob/resolve 反映、
   POSIX symlink 使条目消失），下一轮对话自愈（报告修复要求 1 的
   「不得以再 read 一次宣称闭合」由此满足：服务路径无第二次磁盘读取）。
5. **强制模式闭环**：快照内容即验签通过时盘上内容；重建时 `_parse_pack`
   重新验签（HMAC mismatch → 整包 unavailable），快照随重建整体替换。

### AC4（R9）：链接循环按包/按目录收口为可比较异常哨兵，fail-closed 不中断

1. `packs.packs_signature`：每包条目构建包一层 `except (OSError,
   RuntimeError)`（RecursionError 是 RuntimeError 子类，Windows junction
   环的深层递归形态一并覆盖）→ 生成哨兵条目
   `("__error__:<异常类型>", "<异常信息截断>")`——可比较、随异常变化而
   变化、故障恢复后条目回归正常形态（差异触发重扫重验，缓存自愈，报告
   修复要求 1+3）；其他包与对话不中断。
2. `packs.scan_packs`：每包 `_parse_pack` 调用包一层同元组捕获 → 该包
   落错误态条目（status=error，`_parse_pack` 开头的初始 dict 提取为
   `_new_pack_info` 供错误条目复用，保证下游 list_packs/enabled_packs/
   packs_signature 消费的键完整），健康包继续扫描（修复要求 2 的
   「扫描」入口）。
3. `packsign.verify`：捕获元组 `(OSError, UnicodeEncodeError)` 扩展
   `RuntimeError`——载荷 rglob 遇循环 fail-closed 判 False，不上抛
   （「验签」入口）；compute/sign 生成路径维持严格上抛（R5 语义不变）。
4. `skills.register_extra_dir` / `skills.signature`：注册入口的
   `Path.resolve()` 包同元组捕获（解析失败 → 告警并跳过注册，fail-closed）；
   signature 每来源目录 rglob 包同元组捕获 → 哨兵条目（检测层不中断，
   哨兵变化触发重建）。`_rel_key` 既有 ValueError 兜底保留。
5. `list_skills` 的深度 1 glob 不存在递归环形态，且包来源内容已快照化，
   维持现状（范围外，见澄清 6）。

## 三、测试映射（先行）

基线：614 passed / 18 skipped（v0.17.35 提交后全量）。红测试先行：新增
用例须先在当前代码上以「对准目标行为」的方式失败，再实现转绿。测试命名
对应 AC 编号；链接构造沿用 `_mk_link` helper（os.symlink → mklink /J →
skip），junction 用例本机 Windows 真跑、真 symlink 用例 CI/Linux 恒跑
（AC6：不得长期依赖 skip）。

| AC | 测试 | 说明 |
|---|---|---|
| AC1 | tests/test_packs.py 增 test_ac1_pack_internal_dir_link_rejected | 强制模式下 `skills/x -> ../hidden`（含 hidden/SKILL.md）+ 正常声明 y，签名后扫描：x 拒收（解析后不在 skills 树内）、skills==[y]、x/hidden 均不可加载、y 正常且 trust=signed——当前代码 x 经包根包含校验挂载（junction/symlink 同红） |
| AC2 | tests/test_packs.py 增 test_ac2_mounted_set_covers_all_collections | 多声明技能 + frontmatter 别名技能：索引名集合 == 可加载集合 == 技能层签名目录集合 == 热检测技能条目集合，且每个已挂载技能的物理文件键都出现在 packsign HMAC 载荷内（四集合同一，报告 AC1/AC2） |
| AC3 | tests/test_packs.py 增 test_ac3_swap_in_check_read_gap_no_outside_content | hook `_skill_entry_ok` 在准入通过瞬间换入指向包外的 junction（精确复现报告 toctou_swapped 场景）：直查加载不得返回包外内容；正常技能与 frontmatter 别名技能仍可加载；signature 检测到变化（检测层职责）。当前代码 load/list 返回包外内容（红） |
| AC4 | tests/test_packs.py 增 test_ac4_scan_survives_root_resolve_loop | 注入 `Path.resolve` 对特定包抛 RuntimeError（沿 C2 注入先例）：scan_packs 不上抛、该包 error 态、健康包 ok 继续工作 |
| AC4 | tests/test_packs.py 增 test_ac4_signature_sentinel_on_root_loop | 同注入下 packs_signature 不上抛、异常包条目为哨兵、健康包条目正常；哨兵与正常态不同（触发重扫） |
| AC4 | tests/test_packsign.py 增 test_ac4_verify_survives_runtimeerror | 载荷遍历 RuntimeError 与 OSError/UnicodeEncodeError 同口径：verify 判 False 不上抛、trust=invalid；compute 仍上抛（生成路径严格） |
| AC4 | tests/test_packs.py 增 test_ac4_real_root_loop_fail_closed_and_self_heals | 真链接：包目录 rmtree 后建自循环链接 → packs_signature 不上抛且哨兵条目变化；恢复真实目录并重扫后 trust 重新 signed（自愈，报告修复要求 3）。构造沿用 _mk_link（symlink → junction 回退）：POSIX 用 symlink、Windows 本机以 junction 真跑（评审 F4 纠正「junction 无法自环」的失实陈述——实测 mklink /J 自环可创建；自愈恢复前先 unlink 自环链接再重建目录） |
| AC5 | tests/test_packs.py 改 test_review_grouped_parent_registration_survives_link_split | R7 收紧后 `skills/x -> ../tools` 在解析期拒收：断言 skills==[y]、y 不陪葬、三口一致（原 len==2 与「x 可见」断言按新布局规则更新，y 核心断言保留） |

预期与实际（实施后回填）：spec 初稿预期 621 passed / 19 skipped，系把 AC5
（既有 C5 用例原地更新断言）误计为新增用例——实际新增测试函数 7 个，实现
完成后本机全量 620 passed / 19 skipped / 0 failed（R9 真链自循环用例当时
构造仅走 symlink、本机降级 skip；注入式 R9 用例本机恒跑）。评审修复后最终
622 passed / 18 skipped / 0 failed（F4 纠正 junction 陈述并令该用例以
junction 回退本机真跑：+1 passed / -1 skip；F5 补同 basename 嵌套别名串包
防线回归用例：+1 passed）。AC6 门槛（passed ≥ 614 且新增用例在支持环境全部
通过）达成；存量用例无「注册后改盘即时可见」依赖——全量回归 0 failed 佐证，
快照化属预期行为变更未破坏任何既有用例。

## 四、澄清与取舍记录

1. **R8 方案对比**（报告给出快照/句柄两条路，禁链不独立成立）：
   - **方案 A 快照（推荐，本 spec 采纳）**：服务入口零磁盘读取，窗口收敛
     至重建周期、且重建必经签名检测；实现集中在 `_parse_pack`/注册传递/
     三服务入口取数，改动面清晰可测；技能均为小文本，内存开销可忽略。
   - 方案 B 句柄：`os.open` 单句柄 + 句柄最终路径核验（POSIX 需
     /proc/self/fd、Windows 需 ctypes GetFinalPathNameByHandle），跨平台
     复杂度高、每入口每次调用开销，句柄身份核验自身又是待审计面；POC
     阶段验证成本不成比例。
   - 方案 C 禁链：加载期 is_symlink/reparse 检测拒绝链接——Windows
     junction 的 is_symlink 为 False 需另做 reparse 检测，且「检查时非
     链接、打开时已换链」窗口依旧，单独不成立；其意图已被 A 的
     「枚举过 entry_ok + 内容过快照」覆盖。
2. **快照边界**：仅包来源技能走快照（信任链对象）；内核 skills/ 与
   skills_personal/ 无签名体系、历史直读水位不变（`texts=None` 回退）。
   快照键 = 解析后技能目录名，与 allowed 集合、清单声明同源——frontmatter
   名与目录名不同的技能经兜底扫描按快照内容匹配，行为不变。
3. **R7 与 C5 用例**：v0.17.35 评审 C5 用例构造的 `skills/x -> ../tools`
   恰为 R7 明令拒绝的形态（报告修复要求 1 点名 tools），该用例断言按
   「解析期拒收」更新，其核心回归价值（合法技能 y 不陪葬、三口一致）
   保留。by_parent 分组注册机制保留不动——严格布局下正常包仍收敛为单一
   父目录，机制空转为零成本防御纵深。
4. **R9 哨兵口径**：哨兵条目 `("__error__:类型", "信息")` 与 `_file_sig`
   的 '?' 兜底同动机（异常状态参与比较、变化触发重扫）；RuntimeError 的
   捕获天然覆盖 RecursionError（Windows junction 环的深层递归形态）。
   真自循环 symlink 在 scan_packs 的 `sub.is_dir()` 处即 fail-closed
   （ELOOP → 非目录 → 不入扫描），R9 的可达上抛路径是「缓存内包目录被
   换成环」的热检测场景——注入式用例覆盖该精确路径。
5. **packs_signature 载荷形态本轮不变**（条目仍为 (相对路径, 哈希) 二元
   组，v0.17.35 R6 口径）；快照不进任何签名载荷（它是服务层缓存，非信任
   凭据，重建时由磁盘+验签重新物化）。
6. **范围外不动**：list_skills 深度 1 glob（无递归环形态、内容已快照化）；
   mock.py 既有阻塞；16 个真实模型/vLLM/Docker 门控 skip；tools/prompt
   的包根校验水位；`_toggle_pack`/知识库/RBAC/部门白名单既有逻辑；
   `read_signature` 与 CLI sign_pack.py（自然受益于 verify 收口）。
7. **行为变更提示**：注册后、重建前修改技能文件将不再即时反映到索引/
   加载（下轮对话热检测触发重建后生效）——与 pack.yaml/tools 的既有
   「重建周期」语义一致，属 R8 修复的预期效果而非回归。

## 五、评审记录（TRAE-code-review 双子代理交叉验证）

双子代理独立验证裁决：修复 5 项（F1-F5，其中 F4/F5 为子代理实测附注
发现经确认修复）、有意保留 1 项（F6）、证伪排除 2 项（P4/P6）、验证型
候选确认通过 4 项（P7-P10）。

**修复 5 项（F1-F5）**：

- F1（P1，2/2 确认）：`register_extra_dir` 以 `dict(texts) if texts else
  None` 把显式空 dict 混同 None——某来源注册显式空快照（如包内技能全部
  被拒后的注册）会静默回落直读磁盘，重开 R8 换链窗口。改 `texts is not
  None` 判空：显式空快照 fail-closed，该来源条目全部因缺快照被服务端拒绝。
- F2（P2，2/2 确认）：`packs_signature` 哨兵键不带来源且无告警，多包异常
  无法定位。键补来源标签（包来源 `pack:<名>`、内核用目录名），补 print
  告警对齐 scan_packs 的可定位口径。
- F3（P3，2/2 确认）：frontmatter 兜底扫描分支快照缺失静默跳过，与直查/
  索引分支口径不一致。补同口径告警后 continue（仍 fail-closed）。
- F4（子代理 A 实测附注，1/2 带疑 → 确认）：本 spec 与用例 docstring 中
  「junction 无法自环」陈述失实——`mklink /J d d` 实测创建成功（rc=0）。
  docstring 纠正，构造改 `_mk_link`（symlink → junction 回退），R9 真链
  自循环用例 Windows 本机由降级 skip 转真跑（skip 19→18）；自愈步骤恢复
  目录前先 unlink 自环链接（POSIX 下 mkdir 对已存在路径抛 EEXIST）。
- F5（子代理 A 附注发现 A2，确认）：包级 `skill_texts` 以目录名裸键存储，
  同 basename 的树内嵌套别名（`skills/y/z` 经 `skills/x` 挂载）与平铺
  真实技能 `skills/z` 后写覆盖先写，快照串包。键改解析后真实路径
  `str(p)`（全局唯一），`register_pack_skills` 组内再按目录名重建键
  （同一父目录内唯一）；补嵌套别名回归用例（链接环境不可用才 skip，
  CI/Linux 恒跑）。

**有意保留 1 项（F6）**：`register_extra_dir` 重复注册同父目录的合并
分支 `{**(_t or {}), **texts_map} if texts_map else _t` 真值判断——语义
为「新注册 texts 为空则保留旧快照」，空不覆盖合理；且现网全部调用路径
texts 均非 None，空 dict 分支正常不可达。记录不改。

**证伪排除 2 项**：P4 `_resolve_in_pack` 的 `relative_to` 构造性安全
（相对路径由受控清单拼接、父子关系成立不抛——双子代理实验证实）；P6
`_new_pack_info` 错误态条目缺 permissions/allowed_roles 等键（消费方全部
`.get()`/成员判断防御，与既有早期 error 路径条目同形状）。

**验证型候选确认通过 4 项（排除）**：P7 R7 拒收矩阵（hidden/tools/包根/
自挂载/树内别名各形态两平台一致）；P8 R8 三入口（索引/直查/兜底）换链
窗口闭合——monkeypatch 在 `_skill_entry_ok` 准入瞬间换入包外链接，仅
返回快照内容；P9 R9 异常收口完备（scan/signature/verify/注册四入口均
不中断）；P10 旧「注册后改盘即时可见」语义无残留（存量用例零依赖，全量
回归 0 failed 佐证）。

**回归演进**：基线 614 passed / 18 skipped → 实现后 620 passed /
19 skipped / 0 failed → 评审修复后 622 passed / 18 skipped / 0 failed
（+2 passed：R9 真链用例本机真跑、串包防线新用例；-1 skip）。
