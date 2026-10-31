# 安全加固 v0.17.35：技能挂载同一集合与验签异常收口

状态：已实现并通过全量回归（614 passed / 18 skipped / 0 failed）与
TRAE-code-review 双子代理评审（修复 C2/C5/C6、清理死分支 E2、有意保留
E1、证伪排除 C1/C4/C9/C10，裁决与理由见第五节）；复验报告随本 commit
入库。

## 一、背景

外部复验报告 `docs/reviews/2026-10-31-v0.17.34-73f9e1d-review.md` 判定
v0.17.34（`73f9e1d`，基线 603 passed / 17 skipped）两项核心整改闭环，但技能
实际挂载链路仍有旁路，并新确认两个签名检测/异常处理问题，**尚不能判定领域包
信任链完全闭环**。三项问题均已在 `73f9e1d` 代码逐行核实属实：

- R4【高】`packs.register_pack_skills` 注册 `skill_dir.parent`（共同
  `skills/` 父目录），`skills.list_skills` / `load_skill` / `signature`
  无过滤扫父目录全部 `*/SKILL.md`——只要同包有一个有效技能注册父目录，
  未声明技能、甚至因叶子出包被拒绝的兄弟技能都会重新进入索引/加载/签名。
  不依赖符号链接即可复现：清单只声明 `skills/good`、另放未声明
  `skills/bad/SKILL.md`，bad 可见可加载（报告实测
  `undeclared_sibling_visible=True`、`undeclared_sibling_loadable=True`）。
  位置：`server/packs.py::register_pack_skills`（L632-636）、
  `server/skills.py::list_skills/load_skill/signature`。
- R5【中高】`packsign._canonical_parts` 直接遍历并 `read_bytes()`，
  `verify()` / `trust_status()` 不捕获读取/遍历异常——签名资源读取
  `OSError`（文件占用/权限/I/O）会中断整个扫描而不是仅使该包失效
  （报告实测注入读失败后 `verify_on_read_failure=raised OSError`），
  启动与热重载整条链路跟着失败。
- R6【中】`packs.packs_signature` 技能目录条目只含排序后的内容哈希、
  无相对路径——附属文件纯重命名（`references/a.md`→`b.md`，内容不变）
  不触发热检测；但 packsign HMAC 载荷含路径，强制重扫后 invalid，缓存与
  磁盘信任状态不一致（报告实测 `reference_rename_hot_signature_unchanged=
  True`、`reference_rename_after_rescan=unavailable invalid`）。

**已通过项不回退**（v0.17.34 复验确认）：工具规范路径白名单（平铺
`tools/<文件名>.py` 与签名/沙箱执行一致）、Skills 内容哈希重验、知识库
原子替换。

## 二、方案

实施顺序 R4 → R5 → R6（报告第 4 节要求）。不做无关重构。

### AC1+AC2（R4）：注册/索引/加载/签名统一同一份「已验证接受的具体技能集合」

注册形态选「共同父目录 + 允许集合 + 包根」：v0.17.34 AC3 的规范路径白名单
已保证全部声明技能共居 `<pack>/skills/` 同一父目录，父目录注册 + 集合过滤
与三入口现有扫描结构（`glob("*/SKILL.md")` / `rglob("SKILL.md")`）最小适配。

1. `skills._extra_dirs` 条目从 `(目录, 来源)` 扩展为
   `(父目录, 来源, 允许的技能目录名集合, 包根目录)`；
   `register_extra_dir(path, source, allowed=None, root=None)`——
   `allowed=None` 维持旧行为（整个目录可扫，内核 `skills/` 与测试直调
   来源零改动），`root` 给定时对每个实际可加载的 `SKILL.md` 做**加载期
   真实路径包含校验**（`resolve()` 后须在包根内，报告修复要求 3：解析期
   校验之后磁盘仍可能被替换成指向包外的链接）。重复注册同一父目录时合并
   允许集合（防御性，正常路径各包父目录唯一）。
2. 三入口统一过滤（新助手 `_skill_entry_ok`，同一条规则）：
   - `list_skills`：目录名不在允许集合 / 叶子 `resolve()` 出包根 →
     跳过并告警（fail-closed：未声明或被拒兄弟不得经父目录重新入索引）；
   - `load_skill`：直查分支按「请求名 ∈ 允许集合」准入（frontmatter 名与
     目录名不同的技能经既有兜底扫描路径加载，兜底扫描同受
     `_skill_entry_ok` 过滤）；
   - `signature`：同 `_skill_entry_ok` 过滤，被拒条目不进技能层签名。
3. `packs.register_pack_skills` 按包聚合注册一次：
   `allowed={s.name for s in pack["skills"]}`（即 `_parse_pack` 已完成
   规范路径/包含/叶子/去重四重校验的集合——清单声明的目录名）、
   `root=pack["dir"]`；无技能的包不注册父目录。

效果：AC1（未声明或被拒绝的兄弟技能不能经任何入口索引或加载）与
AC2（实际可加载技能集合与经过路径校验的集合同一）成立。正常包零行为变化
（声明技能全部照常索引/加载/签名，多声明兼容）。

### AC3（R5）：验签路径读异常 fail-closed，签名生成路径严格报错

1. `packsign.verify` 捕获载荷读取/遍历 `OSError` → 打印
   `[packsign]` 告警（含包名与异常类型，可定位、不吞异常状态）→ 返回
   False（即 invalid）。强制模式下 `_parse_pack` 既有 unavailable 分支
   自然使该包整包剔除；开放模式本就不读载荷（无密钥短路），行为不变。
   `trust_status` 无需改动（verify 不再上抛）。
2. `compute` / `sign_pack_files` 保持严格上抛——**绝不静默跳过不可读
   文件继续签出覆盖不完整的签名**。调用方 `packs.sign_pack` /
   `packs.create_pack` 包一层 `except OSError`，转明确错误文案
   （点明资源读取异常、未写入任何签名）；`sign_pack_files` 只在 compute
   成功后才写 manifest，异常路径天然不落残缺签名。CLI
   `tools/sign_pack.py` 维持 traceback 上抛（命令行即明确报错），
   自然受益于 verify 不再炸。
3. 其他正常包不受影响：异常被隔离在单包 verify 边界，扫描继续。

### AC4（R6）：packs_signature 条目统一「规范化相对路径 + 内容哈希」

1. 全部条目从裸哈希改为 `(p.relative_to(包目录).as_posix(), sha256)`
   二元组：`pack.yaml`（固定键）、`tools/<文件名>.py`、
   `skills/<一级目录>/...`（含 SKILL.md 与 references/ 等附属文件）、
   `prompt.md`（固定键）——报告修复要求 2「tools、prompt 和其他资源
   统一路径+内容哈希」一并落地，纯重命名与跨目录交换位置均可检测。
2. 与 packsign HMAC 载荷同键基准（`relative_to(pack_dir)`），缓存
   （热检测）与验签（强制重扫）两层口径一致，消除「缓存认为未变、
   重扫已 invalid」的不一致。
3. 读失败 `'?'` 兜底语义保留（`_file_sig` 不动）。

## 三、测试映射（先行）

基线：603 passed / 17 skipped（v0.17.34 提交后全量，含 1 个本机
Windows 无符号链接权限的既有 skip）。新增用例全部对准真实行为（AC5，
非源码字符串断言）：

| AC | 测试 | 说明 |
|---|---|---|
| AC1 | tests/test_packs.py 增 test_ac1_undeclared_sibling_skill_invisible | 清单只声明 good、磁盘另有未声明 bad → bad 不进索引、`load_skill("bad")` 为 None、good 正常（报告最小复现场景，不依赖符号链接） |
| AC1 | tests/test_packs.py 增 test_ac1_rejected_sibling_not_remounted_via_parent | 声明 good+hijack，hijack 因 SKILL.md 符号链接出包被解析期拒绝 → good 正常可用、hijack 不得经父目录重新加载（环境无符号链接权限时 skip，CI/Linux 恒跑，沿用 v0.17.34 AC4 先例） |
| AC2 | tests/test_packs.py 增 test_ac2_loadable_set_matches_verified_set | 多个声明技能（good1+good2）全部可索引/可加载（兼容回归），且索引 == 可加载 == skills.signature 载荷三口同一、未声明兄弟三口皆不可见 |
| AC3 | tests/test_packsign.py 增 test_ac3_verify_read_failure_fail_closed | verify 遇资源读 OSError 不上抛、判 False 且告警可定位；trust_status=invalid；compute 上抛（生成路径不静默跳过） |
| AC3 | tests/test_packs.py 增 test_ac3_scan_survives_resource_read_failure | 强制模式两包，注入一包资源读失败 → `scan_packs()` 不抛异常、病包 invalid/unavailable、健康包 signed/ok 不受影响 |
| AC3 | tests/test_packs.py 增 test_ac3_sign_failure_explicit_not_partial | `sign_pack` 与 `create_pack`（强制模式自动签名）遇资源读失败 → 返回明确错误文案、pack.yaml 不落任何 signature 行（无残缺签名） |
| AC4 | tests/test_packs.py 增 test_ac4_skill_reference_rename_changes_hot_signature | `skills/demo/references/a.md` 改名 `b.md` 且内容不变 → packs_signature 必须变化（报告复现场景） |
| AC4 | tests/test_packs.py 增 test_ac4_rename_triggers_reverification | 强制验签端到端：签名后纯改名 → 热检测触发重扫 → trust=invalid 整包剔除（fail-closed），缓存与磁盘信任状态一致 |

预期全量：本机 610 passed / 18 skipped（+8 用例，其中 1 个 symlink 用例
本机环境降级 skip，CI/Linux 恒跑）；AC6 门槛：不低于 603 passed /
17 skipped，新增用例全部通过，不得意外扩大跳过范围（唯一新增 skip 即上述
symlink 环境降级，与上轮同类）。红测试先行：新增用例须先在当前代码上
以「对准目标行为」的方式失败（bad 可见可加载 / scan 抛 OSError /
重命名后签名不变等），再实现转绿。

实际结果（实施后回填）：8 个 AC 用例红测试先行确认失败对准目标行为
（7 failed + 1 symlink 用例本机环境降级 skip），R4→R5→R6 实现后全部
转绿；本机全量 610 passed / 18 skipped / 0 failed（AC6 门槛 603/17
达成）。

评审修复回归用例（TRAE-code-review 裁决后补，随本版同一 commit 入库）：

| 来源 | 测试 | 说明 |
|---|---|---|
| C2 | tests/test_packs.py::test_review_signature_survives_relative_to_mismatch | 包目录被换成指向别处的链接 → relative_to 抛 ValueError 被 _rel_key 兜住、packs_signature 不上抛（退化为绝对路径键，键形态变化触发重扫重验、缓存经重建自愈） |
| C5 | tests/test_packs.py::test_review_grouped_parent_registration_survives_link_split | 清单声明技能之一为指向包内 tools/ 的目录链接 → 按真实父目录分组注册：合法技能 y 不陪葬、链接技能不越界、可加载技能的物理源文件全部出现在技能层签名载荷（三口物理一致，链接拼接与 resolve 真实两种形态均安全） |
| C6 | tests/test_packsign.py::test_review_verify_survives_encode_error | verify 遇 UnicodeEncodeError（surrogateescape 文件名）与 OSError 同口径 fail-closed 判 False、不上抛、告警含异常类型可定位 |
| C6 | tests/test_packs.py::test_review_sign_pack_survives_encode_error | 生成路径同口径：sign_pack 捕获 UnicodeEncodeError 转明确错误文案（「签名失败」「未写入任何签名」）、pack.yaml 不落 signature 行 |

评审修复后全量：614 passed / 18 skipped / 0 failed（610 + 评审 4 用例）。

## 四、澄清与取舍记录

1. R4 注册形态选「父目录 + 允许集合 + 包根」而非「逐具体技能目录注册」：
   后者需为包来源引入独立于内核层的第二套扫描/命名逻辑（frontmatter 名与
   目录名双轨、签名键基准特例），改动面更大而收益相同。允许集合以**目录名**
   为键——清单声明与解析期四重校验（规范路径/resolve 包含/叶子包含/去重）
   的对象即目录，目录名是唯一经人工审批的稳定标识；frontmatter 名不同的
   声明技能经兜底扫描仍可加载。
2. R4 在索引/加载/签名期重复做真实路径包含校验（root）：这是对 v0.17.34
   spec 澄清 5「校验时机为扫描期、TOCTOU 与现状同水位」的**有意扩大**——
   R4 属旁路级问题，报告修复要求 3 明示「对实际可加载的每个 SKILL.md
   保留真实路径包含校验」；扫描后磁盘被换成指向包外的链接时，加载期校验
   直接闭合窗口（不依赖下一轮热检测）。工具/提示词维持扫描期校验水位，
   不随本次扩大。
3. R4 过滤告警每次扫描/签名检测都会打印：正常包零输出；仅未声明/越界
   兄弟在场时可见，作为持续的注入信号（fail-closed + 可观测，与
   「技能名冲突被遮蔽」告警同风格）。
4. R6 键基准取包目录而非报告示例的 `relative_to(sdir)`（报告该处标注
   「例如」）：包目录基准使「跨技能目录交换位置」不依赖条目次序即可
   检测（sdir 基准下两技能内容互换仅靠元组次序巧合区分），且与 packsign
   HMAC 载荷完全同键，两层天然一致——报告修复要求 2 恰以「避免交换位置
   问题」为动机。
5. R5 验证路径读异常判 False（归入 invalid）而非新增第四信任态：验签
   失败原因（篡改 vs I/O）对准入决策无差别——都是「不可信，不得加载」；
   真实原因经 `[packsign]` 服务端告警定位，不吞异常状态（报告修复要求 3）。
   开放模式（无密钥）本就不读载荷，不受影响。
6. packs_signature 载荷形状变化（裸哈希 → (路径, 哈希)）会使升级后首轮
   比较必不等、多触发一次无害重建：签名仅进程内比较、无持久化依赖，与
   历次载荷口径调整同语义（v0.17.33 mtime→哈希、v0.17.34 补 skills）。
7. `_all_skill_dirs()` 返回形状同步扩展为四元组：仅 skills.py 内部三处
   消费 + docs 引用，无外部调用方；`register_extra_dir` 生产调用方仅
   `packs.register_pack_skills` 一处，扩展参数带默认值，测试直调零破坏。
8. 范围外不动：mock.py:77 同步调用 maybe_reload 的既有阻塞；16 个真实
   模型/vLLM/Docker 门控 skip；`read_signature`（CLI/测试调用）维持现状；
   CLI `tools/sign_pack.py` 除自然受益外不改；共享知识库同名工具冲突、
   RBAC/部门白名单等既有校验零触碰。

## 五、评审记录（TRAE-code-review 双子代理交叉验证）

范围：本 spec 全部变更（3 源码改 + 2 测试改 + 2 文档新，+315/-43）；
主代理自查候选 10 项（C1-C10），双子代理独立验证（互不可见），按 2/2
确认纳入 / 1/2 带疑纳入（标注分歧与处置）/ 0/2 证伪排除裁决；修复范围
经用户确认为「修 C2+C5+C6 + 清理 E2，E1 有意保留」：

- **C1【0/2 证伪，排除】** 疑 _rel_key 相对路径键受 Windows resolve
  大小写规范化影响（工作区盘符 e: 与磁盘 E: 大小写不同 → relative_to
  抛 ValueError 误触发兜底）——双子代理实验一致确认 Path.resolve() 会
  将盘符与各级目录统一为磁盘实际大小写（resolve('Good').name ==
  'good'），键双方同为 resolve 后形态，键稳定无现实抛点。
- **C2【1/2 带疑，已修复】** R6 引入 relative_to 后的时变场景：扫描
  缓存填充后、下一轮比较前，包目录（或其祖先）被换成指向别处的链接 →
  缓存条目路径与现算包根前缀分叉，relative_to 抛 ValueError 直接上抛
  会炸掉每轮对话开头的热检测且缓存永不失效（持续故障而非一次性）。
  分歧：证伪方认为触发前提极窄（需运行期对包目录换链接）。处置：一行
  兜底成本低、收益是热检测永不因键形态崩溃——_rel_key 捕获 ValueError
  退化为绝对路径键继续参与比较，键形态变化即触发重扫重验，缓存经重建
  自愈。回归：test_review_signature_survives_relative_to_mismatch。
- **C3【2/2 确认，无问题】** 三入口（list_skills / load_skill /
  signature）对 _extra_dirs 的过滤闭环核对——全部经 _skill_entry_ok
  统一规则，无第四扫描入口；load_skill 直查分支按「请求名 ∈ 允许
  集合」准入，兜底扫描同受过滤，frontmatter 名与目录名不同的声明技能
  经兜底路径正常加载。
- **C4【0/2 证伪，排除】** 疑 register_extra_dir 重复注册合并允许集合
  时空集合被吞（空集合 = 全放行的不变式被打破）——合并分支仅发生在
  同一父目录重复注册（正常路径各包父目录唯一、不可达），且空集合并集
  仍为空集合，「全放行」仅对 allowed=None 成立，空集合语义即「全部
  拒绝」，无矛盾。
- **C5【1/2 带疑，已修复】** register_pack_skills 取
  pack["skills"][0].parent 单父目录注册的前提（v0.17.34 AC3 规范路径
  白名单保证声明技能共居 skills/ 下）可被包内目录链接打破：清单声明
  skills/x（链接 → 包内 tools/）时，_resolve_in_pack resolve 穿透后
  条目真实路径为 <包根>/tools，其 parent 即包根；与真实目录技能
  skills/y 的父目录分叉时只注册包根一层，y 的物理文件（两层深）从
  索引/加载/签名三口消失——合法技能陪葬、AC2 目标本身被打破。
  分歧：证伪方认为触发前提需要包内链接（边缘形态）。处置：该形态
  无需恶意即可由误用触发，且陪葬方向是可用性 + 一致性缺陷——按真实
  父目录分组注册（by_parent），正常包全部声明共居同一父目录、行为
  不变。回归：
  test_review_grouped_parent_registration_survives_link_split（链接
  拼接与 resolve 真实两种形态均断言安全，Windows junction 与真符号
  链接实测同构）。
- **C6【1/2 带疑，已修复】** Linux surrogateescape 文件名（lone
  surrogate）使 packsign._payload 的 rel.encode("utf-8") 抛
  UnicodeEncodeError——不属于 OSError，R5 只扩 OSError 时可穿过
  verify 炸掉 scan_packs（与 R5 同动机的中断面）。分歧：证伪方认为
  该文件名场景边缘。处置：异常元组扩展 (OSError, UnicodeEncodeError)
  成本一行，verify fail-closed 判 False、告警含异常类型；生成路径
  sign_pack/create_pack 同口径捕获转明确文案；compute /
  sign_pack_files / trust_status 维持严格上抛。回归：
  test_review_verify_survives_encode_error +
  test_review_sign_pack_survives_encode_error。
- **C7【2/2 确认，无问题】** create_pack 强制模式自动签名与
  sign_pack 的行为/文案一致性核对（成功/密钥不可用/资源读失败三态）
  ——可达路径行为一致、无静默分支。
- **C8【2/2 确认，无问题】** 测试注入精确性核对——monkeypatch
  Path.read_bytes 按文件名定向（不影响 pack.yaml 读取）、
  UnicodeEncodeError 构造参数合法、断言锚定真实行为而非源码字符串。
- **C9【0/2 证伪，排除】** 疑 _rel_key 链路存在未兜底的 resolve
  抛点——扫描与签名检测链路的路径在各自入口已物化（resolve 发生在
  _parse_pack / _resolve_in_pack 注册期），_rel_key 本体仅调
  relative_to（ValueError 已由 C2 兜底），无现实抛点。
- **C10【0/2 证伪，排除】** 疑 agent.py custom_sig 先捕获后
  reset+register 的时序使端到端用例依赖脆弱顺序——与 v0.17.34 C7
  同型：两种顺序下用例均正确（先捕获后注册 → 稳定调用吸收一次重建；
  先注册后捕获 → 稳定调用无害空转），断言不依赖具体顺序。

评审附注（按用户确认处置）：
- **E1【有意保留，经用户确认】** 未声明兄弟文件不在热检测载荷
  （pack.yaml 声明未变即不触发重扫），但在 packsign HMAC 全量载荷
  内（强制模式重扫即 invalid）——篡改不触发热重建、信任展示陈旧至
  下次重扫。保留理由：R4 后未声明内容无任何运行时读取通道（索引/
  加载/签名三口全拒），仅管理视图展示层陈旧，属 v0.17.34 既有口径、
  非本次引入。
- **E2【已清理】** create_pack 强制模式签名分支存在不可达死代码：
  is_enforced() 为真时 PACK_SIGNING_KEY 必非空，sign_pack_files 只
  会成功或上抛，「无密钥返回空」路径不可达（C7 三态一致性验证中双方
  独立确认）。清理：三态收敛两态（成功 → 已自动签名；异常 → 明确
  失败文案），哨兵统一空串。
