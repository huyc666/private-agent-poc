# fastmcp 3.4.10 安全加固 Spec 独立评审

日期：2026-11-11
对象：`docs/specs/security-hardening-fastmcp-3.4.10.md`（未提交工作区文件）
基线：spec 自述 `3056522`；评审时 HEAD `52fc3b8`（`3056522` 的祖先关系已核对）
判据：`.trae/skills/spec-first/SKILL.md` 的 spec 独立评审要求、`docs/CODING-STANDARDS.md` §5.1/§5.2/§5.4/§7
方式：只读评审；未修改任何被评审文件；只写本报告

## 结论

**有条件通过。** 升级方向与安全结论成立：本机 fastmcp 3.4.10、fastmcp-slim 3.4.10（fastmcp 元包精确锁定 `fastmcp-slim==3.4.10`），适配层 0.3.2 的 `mcp.server.fastmcp` import 阻塞证据可复核；默认 PyPI 服务复扫为 79/6、fastmcp 消失，OSV 服务复扫为 105/6、fastmcp 同样消失，直接依赖命中 0 这一结论不受服务差异影响。但 6 条 P1 不关闭不得提交，其中版本口径、pip-audit 服务、测试基线三处属于硬门禁事实问题，不能靠措辞解释过去。

问题计数：P0 0 条，P1 6 条，P2 5 条。

## P1 问题

| 编号 | 位置 | 问题 | 建议 |
|---|---|---|---|
| P1-1 | spec L124-126、L272-277；`requirements.txt:11-13`；`tests/test_dependency_versions.py:19-42` | 版本口径不自洽。requirements 是 `fastmcp>=3.4.10,<4`，但 §8.2 写「允许 3.4.10 及以上的修复区间版本」，「修复区间」按 GHSA 同时包含 `>=4.1.2`，字面可推出 4.1.8 也允许；AC1 只断言「不低于 3.4.10」，AC2 只断言「不含 3.4.8、含 3.4.10」，两者在 4.1.8 或去掉 `<4` 的区间下都会绿。实测 `Requirement("fastmcp>=3.4.10").specifier` 仍能通过 AC2 的两条断言 | 把允许集写死为 `3.4.10 <= v < 4`，明确「4.x 即使已修复也不在本轮允许集内」；AC1 增加 `<4` 断言，AC2 增加上界存在性断言（如 `not specifier.contains("4.0.0")`），否则 §2.1 的 4.x 硬阻塞与 §3 的 `<4` 防线在测试里没有落地 |
| P1-2 | spec L184-194、L140、L122-131；`docs/CODING-STANDARDS.md:284`；`docs/ENGINEERING-METRICS.md:121-124`、`:186` | pip-audit 的服务与数字口径不一致。spec §7.2 记录的是 `pip_audit --local`（默认 PyPI 服务，79/6），§7 的 must 明确要求 `pip-audit --vulnerability-service osv --timeout 60`；同一轮改动里的 `docs/ENGINEERING-METRICS.md` 又写「固定 OSV 后端」并记录 79/6。评审用 OSV 复跑得到 **105 known vulnerabilities in 6 packages**，fastmcp 同样不在结果中；直接依赖命中仍是 0，但 79/6 不是 OSV 的数字。spec §3 的变更面还漏了这份被修改的 metrics 文档 | 统一服务口径：按 §7 用 OSV 重跑，更新 spec §7.2/§7.3 与 `ENGINEERING-METRICS.md` S5 的数字；把 `ENGINEERING-METRICS.md` 补进 §3 变更面。若确实要用 PyPI 服务，必须写明偏离规范的原因并同时保留 OSV 数字，不能让两份文档各说各的 |
| P1-3 | spec L3、L176-177；`tests/test_usage_run_wallclock.py:694-721`；`git show 3056522:README.md` | 测试基线标识错了。`3056522` 的 README 是 v0.17.52，不是 v0.17.53；v0.17.53 是其后 `b530009`，该提交给 `test_usage_run_wallclock.py` 新增了 2 个用例（16→18）。当前 `pytest --collect-only` 为 1221（=1201 passed + 20 skipped），减掉本轮 3 个依赖用例为 1218（=1198+20），所以 1198 对应的是 HEAD（含 b530009），不是 `3056522`；按 `3056522` 计应为 1196 passed / 20 skipped | 统一基线 commit、版本号和数字：要么写「升级前（HEAD 52fc3b8，含 b530009 的 2 个 6.8 用例）1198/20/0」，要么写「升级前（3056522）1196/20/0，升级后 1201/20/0，其中 3 个为本轮新增、2 个为 6.8 补充」。现在的写法同时错了两处 |
| P1-4 | spec L129-131、L279-285；`docs/PLAN.md:105` | 自相矛盾：§3 的「不动」清单明确写了 `docs/PLAN.md`，§8.3 又说已在该文件「后续轮次待办」表末尾新增第 6 行。`git diff` 显示 PLAN 确实被修改，且第 6 行内容与 §8.3 引用一致 | 把 §3 改成「不动 PLAN 的功能表与 6.8 条目；仅在后续轮次待办表追加跟踪行（§8.3）」；同时把 PLAN 的并行改动冲突风险写进 §3，避免提交时把别人的 PLAN 改动一起带上 |
| P1-5 | spec L137-141、L145-151；`.trae/skills/spec-first/SKILL.md:65-68`、`:92` | AC 账本不合规。红态列出现「绿（升级前后均为基线守护）」「不适用（以升级前基线对照）」等非允许值（允许值只有 `红态` / `结构性红` / `无红态·特征测试` / `无红态·回归` / `手工留证`）；AC1/AC2 的用例没有按 `test_acN_*` 命名，AC3 的用例也没有 `test_ac3_*` 或 `test_guard_*`；更矛盾的是 §5 说该项是「守护，不占 AC 红态」，AC3 却把它当成自己的验证方式。账本还缺「用例」与「收敛」列 | 按允许值重填红态（AC3 用 `无红态·回归`，AC4 用 `手工留证`，AC5 用 `无红态·回归`）；用例统一改成 `test_ac1_*`/`test_ac2_*`/`test_ac3_*`，或把它明确降为 `test_guard_*` 并从 AC3 移除；账本补上用例与收敛列，§7.4 的收敛结论逐条回填 |
| P1-6 | spec L130；`README.md:3`、`:9`；`docs/CODING-STANDARDS.md:249-250` | 提交口径缺口。§3 明确「不动 README.md」，但 §5.4 是 must：版本号逐次 +1，README 顶部版本号与版本记录同步更新。当前 README 仍是 v0.17.53，工作区也没有改它；spec 没有记录「本轮不计版本/不更新 README」的用户裁决。按用户既定偏好可以不计版本，但必须有裁决记录，不能只写一句「不改」 | 二选一：①按 §5.4 升到 v0.17.54，README 只写用户可感知行为（依赖安全修复，不写 CVE 细节）；②在 spec §7.4 或头部写明带日期的用户裁决「本轮安全修补不计版本、不更新 README」，并在提交 message 引用。没有记录的缺口按违规处理 |

## P2 问题

| 编号 | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | spec L1-8、L122-131 | 结构上缺显式的「背景与目标」「约束」「待澄清问题」小节；「范围与不做」散在开头、§3、§7.3、§8.1。安全加固档允许沿用一～七骨架，但用户本轮要求的是可评审的完整结构 | 在 §1 前补 5 行背景与目标；把「不做：4.x、传递依赖、业务代码、前端」集中成一条范围清单；补「约束：无新增运行时权限面、不改多用户隔离、全量回归为门槛」与「待澄清问题：无」 |
| P2-2 | spec L18-23 vs L73-77 | 同一天的版本快照不一致：§1 的 `pip index versions` 写 `LATEST: 4.1.7`，§2.1 的盘点写 4.1.8、3.x 最新 3.4.11；两处都没有各自的时间戳。另外「已安装版本落后于可解析版本」不准确——requirements 有 `<4`，项目可解析的是 3.x 线，不是 4.1.7 | 给两次快照加时间戳；把「可解析版本」改成「PyPI 最新稳定版」，或明确写「项目约束下可解析的最新 3.x 为 3.4.11」 |
| P2-3 | spec L35-36 | 「按 requirements.txt 声明扫是零命中，因为 `fastmcp>=2.3` 允许解析到漏洞版本」因果写反了：零命中的原因是解析器会取满足约束的最新版（3.4.11），不是因为宽区间安全；宽区间恰恰是风险来源。该结论也没有命令与输出 | 补一条 `pip-audit -r requirements.txt` 的命令与输出；把因果改成「本次解析恰好落到已修复的 3.4.11，但宽区间在锁文件/离线缓存/旧解析结果下仍可能装回 3.4.8，所以要加安全下限」 |
| P2-4 | spec L57-64、L81-116、L213-217、L246-249 | 外部与手工证据缺可复核入口：`fastmcp-slim` 3.4.10/3.4.8 的 `requires_dist` 比对、mcp 2.3.0 wheel 内无 `mcp/server/fastmcp/`、4.1.8 元数据、litellm extra 的 `mcp>=2.3,<3`、官方升级指南与 httpx2 truststore 说法，均为转述，没有抓取时间/URL/落盘输出；AC3 的「真实 stdio 手工实测」也只有结论，没有命令或脚本路径 | 至少给关键几条补抓取时间与来源 URL，或把 `pip download`/元数据输出落一份到仓库外留证并在 spec 引用；stdio 实测补完整命令（含 `MCP_ENABLED=true` 与启动脚本）和原始输出 |
| P2-5 | spec L238-242 | 三条跟踪触发条件不能全部客观判定：①「高危 CVE」没有严重度阈值（本轮 GHSA 是 CVSS 6.8，按「高危」反而可能不触发）；②「升级成本降到可接受」是主观判断；③「项目确实需要 4.x 的独有能力」是产品裁决，不是可观测事实 | ①改成可判定谓词，如「3.4.x 出现任何已有修复版的公开 CVE/GHSA」或写明 CVSS 阈值；②拆成客观条件（适配层 `requires_dist` 含 `mcp>=2.3`）加用户成本裁决；③显式标注「由用户裁决」，并给出需要的能力清单 |


## 不成立/误报

- **AC1 只查 `fastmcp` 元包，不查 `fastmcp-slim`，不构成漏洞。** 本机 `fastmcp 3.4.10` 的 `Requires-Dist` 是 `fastmcp-slim[client,server]==3.4.10`，精确锁定；正常安装下两者不会漂移。
- **适配层 import 阻塞证据成立。** `.venv/Lib/site-packages/langchain_mcp_adapters/tools.py:27-28` 确有 `from mcp.server.fastmcp.tools import Tool` 与 `from mcp.server.fastmcp.utilities.func_metadata import ...`；`server/agent.py:68-83` 的 `except Exception` 确实会把失败降级为空列表，`tests/test_agent_llm.py:59-63` 也固化了这个语义。
- **79/6 的内部算术自洽。** §7.3 的 8+2+17+1+39+12 = 79，包数为 6；升级前加上 fastmcp 即 80/7。问题在服务口径（P1-2），不在加总。
- **1201 = 1198 + 3 的加法本身没错。** 错的是 1198 对应的基线 commit/版本标签（P1-3）；当前 collect 1221 与 1201 passed + 20 skipped 对得上。
- **三条新用例当前是绿的。** 评审复跑 `pytest tests/test_dependency_versions.py -q` 为 `3 passed in 6.55s`，与 §6 的 6.15s 同量级；红态记录 2 failed/1 passed 也与用例结构一致。
- **PLAN 第 6 行确实存在。** 内容与 §8.3 引用一致；问题只是 §3 说「不动 PLAN」与事实相反（P1-4）。
- **跟踪项的预案没有被误读成当前任务。** §2.1 写「本轮不动 4.x」、§8.1 写「预案，非当前任务」、PLAN 行写「触发后再评估」，三处口径一致；这一项不成立为缺陷。
- **流程顺序回溯不构成违规。** spec L6-8 已明确记录「按用户 2026-11-11 要求，spec 评审与代码评审是提交前硬门禁，本轮不提交；结论回写后才允许进入提交环节」，本轮任务也重申了同一口径。spec-first 的编码前评审是默认顺序，用户显式裁决可以覆盖，不需要另补流程说明。
- **spec 与报告的文件名都不符合 CODING-STANDARDS 的模板，但本轮由用户指定路径，不作为 spec 缺陷。** spec 名未用 §5.1 的 `<主题>-v0.17.xx.md`；报告名未用 §5.2 的 `YYYY-MM-DD-plan-<任务号>-spec-review.md`。两项都是用户指定的既有路径/输出名；提交前必须在 spec 内引用本报告的准确路径（`docs/reviews/2026-11-11-fastmcp-spec-review.md`），否则按 §5.2「报告不存在等于关卡未做」处理，并在提交说明里注明命名偏离的用户裁决。

## 证据复核记录

- 本机版本：`fastmcp 3.4.10`、`fastmcp-slim 3.4.10`、`mcp 1.30.0`、`langchain-mcp-adapters 0.3.2`。
- 目标用例：`3 passed in 6.55s`（`tests/test_dependency_versions.py`）。
- 收集数：`1221 tests collected`，与 `1201 passed + 20 skipped` 一致。
- pip-audit 默认服务：`79 known vulnerabilities in 6 packages`，fastmcp 不在结果中。
- pip-audit OSV：`105 known vulnerabilities in 6 packages`，fastmcp 不在结果中。
- 变更面核对：工作区除 `requirements.txt` 与新增测试外，`docs/ENGINEERING-METRICS.md` 也被修改（S5 条，记录 79/6 并声称固定 OSV 后端），spec §3 未列该文件。
- 基线核对：`3056522` 的 README 为 v0.17.52；`b530009` 给 `tests/test_usage_run_wallclock.py` 新增 2 个用例（16→18）；`3056522` 是该提交的祖先。

## 复审准入

关闭 P1-1 至 P1-6，并按 P2-4/P2-5 补齐关键证据或口径后复审。复审只核对上述 P1 是否关闭、AC 账本是否可按允许值机械复核，以及 spec 是否已回写本报告路径。安全修复本身（升级到 3.4.10、4.x 转跟踪项）不需要改方向。
