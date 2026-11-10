# 2026-11-10 流程合规审计：v0.17.51 / v0.17.52

审计对象是两个交付的**实际执行记录**：v0.17.51（PLAN 4.3，文档生成流程草稿，L 级、带页面）和 v0.17.52（PLAN 6.8，run 墙钟时长，S 级但新增数据表）。判据取 `.trae/skills/spec-first/SKILL.md`、`docs/CODING-STANDARDS.md`、`docs/ENGINEERING-METRICS.md`、`docs/SECURITY.md`，证据取 git 记录、两份 spec、`docs/reviews/` 下的评审报告、`docs/PLAN.md`，以及对当前 HEAD 的真实命令输出。

先说结论。4.3 的流程主体是走全了的：spec、AC 账本、契约关/红态、实现、门控真实模型、两轮修复后复审、全量回归都有记录。问题出在最后一道门上：页面人工验收没做，spec 自己把它登记为“未清零缺口”，但代码评审和提交都已经发生。6.8 是这两个交付里更完整的一次：spec 两轮独立评审、代码评审、红态、全量回归、性能、覆盖率都有实测，缺口集中在依赖审计和几处留痕规范上。覆盖率方面，报告确实写的是“新增行覆盖率”，但三个被点名文件的全文件实测都 ≥90%，“覆盖率不达标”这个怀疑不成立。

## 一、两个交付实际走过的关卡

| 关卡 | v0.17.51（4.3） | v0.17.52（6.8） |
| --- | --- | --- |
| spec 与用户确认 | 有；Q1–Q5 已裁决（`4.3-doc-to-workflow.md:364-372`） | 有；Q1–Q4 与 D5 已裁决（`6.8-run-wallclock-duration.md:234-245`） |
| AC 账本 / 红态 | 有；红态 `54 failed, 1042 passed, 20 skipped`（`4.3:465`） | 有；红态 `11 failed, 2 passed`（`6.8:390`） |
| 契约关 | 完成；账本 §4 列齐全（`4.3:398-423`），测试先行红态见 §6（`:458-495`） | 完成；账本 §4（`6.8:317-337`），红态见 §6（`:374-406`） |
| spec 独立评审 | **声称完成，报告缺失**（`4.3:7`；`docs/reviews/` 无对应文件） | 两轮完成，报告存在，但命名与归档顺序不合规（`6.8:6`） |
| 页面原型 / 人工测试 | **触发，未完成**；页面已改（`ae9d8c5` 含 `web/index.html`），人工点击未做（`4.3:636`、`PLAN.md:51`） | 裁剪为免（`6.8:13-15`）；`a2917ff` 未改 `web/`，代码层面成立 |
| 兑现关 | **存在未清零缺口仍进入代码评审**（`4.3:634-636`） | 通过 |
| 代码评审 | 做了，首轮报告未归档；两轮复审报告存在 | 有报告，P2-1/P2-2 已修复 |
| 修复后复审 | 做了两轮（`rereview.md`、`rereview-r2.md`） | 未触发（P0=0/P1=0，仅 3 条 P2） |
| 全量回归 | `1180 passed / 20 skipped`（`4.3:632`） | `1196 passed / 20 skipped`（本次实测一致） |
| 覆盖率 | 只报新增行 99.23%（`4.3:637`） | 只报新增逻辑行口径（`6.8:467`） |
| 成对提交 | `ae9d8c5` + `886cd01`，但 docs commit 夹带旧 spec 回填 | `a2917ff` + `3056522`，spec 评审报告未先行单独提交 |

## 二、必须修

### M1. 4.3 页面人工测试未过，代码评审和提交却已经发生

这是本次审计里最硬的一条。4.3 的 spec 头部写的是“页面关卡按三分口径判定为触发”，裁剪为“无”（`docs/specs/4.3-doc-to-workflow.md:3-5`）。页面确实改了：`ae9d8c5` 的提交内容包含 `web/index.html` 和 `docs/prototypes/4.3-draft-confirm/index.html`。但 spec 的兑现关把“页面人工点击验收待执行”登记为“未清零缺口”（`4.3:634-636`），`docs/PLAN.md:51` 也写着 `⏳（实现、自动测试与门控已过；页面人工验收未做）`，提交正文同样承认“页面人工验收未执行”。

`.trae/skills/spec-first/SKILL.md:126` 的红线是“页面未经用户人工测试确认，不进入代码评审”；`:131` 又把“兑现关存在未清零缺口”列为不得进入代码评审的情形。两条都命中了。实际执行却是：代码评审做了，复审做了两轮，`feat v0.17.51` 提交了，README 也写了 v0.17.51 的功能。这不是“记录了缺口”就能抵消的——缺口还在，门已经过了。

同一件事还有一层：spec 自己说“原型审核与人工测试记录留存在 `docs/reviews/` 或 §7”（`4.3:438`），但 `docs/reviews/` 下只有两份代码复审报告，没有任何 4.3 的原型审核或页面验收记录。也就是说，原型人工审核这一步同样没有可复核的留痕。

**修法**：要么补做页面人工验收并把记录落到 `docs/reviews/`（覆盖个人域 pending/确认中/confirmed/丢弃与 global 审批入口），验收发现的问题走后续修复提交；要么由用户明确同意降级并改 spec 的关卡声明——但三分口径第①条“新增页面/路由/交互”已经命中，按条文没有降级空间。在补齐之前，v0.17.51 不应再被当作完整交付。

### M2. 4.3 的两份强制评审报告都没有归档

`4.3:7` 写“spec 独立评审：首轮已完成”，`4.3:655` 又写“代码评审：两名独立子代理交叉验证后确认 2 条 P2……另有 2 条误报”。但 `docs/reviews/` 下没有 `2026-11-09-plan-4.3-spec-review.md`，也没有 `2026-11-09-v0.17.51-<短hash>-review.md`。现有的两份文件是 `2026-11-09-v0.17.51-rereview.md` 和 `-rereview-r2.md`，它们是“修复后复审”，不是首轮 spec 评审或首轮代码评审。

评审本身大概率是发生了：`rereview.md` 的“上轮问题逐条关闭情况”列了 11 项（P0-1、P1-1～P1-8 等），能反推出首轮 spec 评审的存在；`4.3:655` 也总结了代码评审的 2 条 P2 和 2 条误报。但 `CODING-STANDARDS.md:230-232` 要求的是具名报告产物，且 spec 评审报告要包含 P0/P1/P2 与“不成立/误报”区。现在只有 spec 正文里的摘要和复审的关闭表，首轮的确认范围、误报理由、评审基线都不在独立文件里，审计无法复核。

**修法**：把首轮 spec 评审和首轮代码评审的报告补归档；如果原始报告只在会话里，按会话记录重建并在文件里明确标注“事后重建”，不要伪装成当时产物。补归档时至少保留：评审基线 hash、问题分级、每条的证据、误报及理由、关闭状态。

### M3. 6.8 改了 `requirements.txt`，但没有跑 `pip-audit`

`a2917ff` 在 `requirements.txt` 里新增了 `anyio>=4.0,<5`（提交正文也写了“依赖：显式声明 anyio>=4.0,<5”）。`docs/CODING-STANDARDS.md:281` 的提交前清单是“requirements 变更时跑过 `pip-audit --vulnerability-service osv --timeout 60`，直接依赖命中为 0”，并且写明“逐条打勾，任一不满足不得提交”。但 6.8 的 spec、代码评审报告和提交正文里都没有 `pip-audit` 的执行记录。

这条的实际风险不高：`anyio` 本来就是 `starlette` 的硬依赖，这次只是把传递依赖显式写进 `requirements.txt`，安装版本没有变化。但清单项是无条件的，没跑就是没跑；而且这正是“不做也没人发现”的典型——没有任何产物或 CI 步骤会因为缺少这次扫描而失败。

**修法**：补跑并把结果（含命中数、扫描时间、工具版本）写进 spec 的验证记录或代码评审报告；如果当时环境不能联网，写清离线例外和后续补扫时间。

## 三、建议修

### S1. 覆盖率口径：报告写的是“新增行”，条文要的是“单文件全文件 + 整体”

4.3 的 AC10 直接把验收标准定义成“新增代码覆盖率 ≥90%”（`4.3:414`），§7.3 又用 `git diff --unified=0 HEAD -- server` 取未提交新增行，与 `--cov=server` 的 executed/missing 求交，报出 515/519=99.23%（`4.3:637`）。6.8 同样写“覆盖率（新增逻辑行口径，2026-11-10 复验）”（`6.8:467`）。而 `docs/ENGINEERING-METRICS.md:101-105` 的 S2 指标是 12 个安全敏感文件**逐个全文件** ≥90%、整体（`server/+sandbox/`）≥90%，执行方式是 `pytest --cov=server --cov=sandbox` 后 `coverage report --include=<清单>`。两个 spec 都没有按这个口径出数，也没有给出整体覆盖率。

但“口径被偷换”这次没有掩盖跌破。我在当前 HEAD（`3056522`）用 `.venv\Scripts\python.exe -m pytest -q --cov=server.orchestration --cov=server.usage --cov=server.agent --cov-report=term-missing` 实测：

```
server\agent.py            396     22    94%   59-60, 62, 181-182, ...
server\orchestration.py   1419     84    94%   196, 199, 216, ...
server\usage.py            254      6    98%   109-111, 300-301, 329-330
TOTAL                     2069    112    95%
1196 passed, 20 skipped, 2 warnings in 267.64s
```

`rereview.md:21-24` 当时记录的 `.coverage` 也是 `approval.py` 100%、`orchestration.py` 94%、`routes/orchestration_drafts.py` 96%。所以三个被点名文件全文件都 ≥90%，`usage.py` 还是 S2 清单里的文件，98% 也过线。

再把 4.3 改到的三个 S2 清单文件复测一遍（`.venv\Scripts\python.exe -m pytest -q --cov=server.auth --cov=server.approval --cov=server.tools_builtin --cov-report=term-missing`）：

```
server\approval.py          147      0   100%
server\auth.py              296      3    99%   197-198, 247
server\tools_builtin.py     181     16    91%   118-121, 137-138, 181, 254-258, ...
TOTAL                       624     19    97%
1196 passed, 20 skipped in 269.94s
```

三个也都 ≥90%。所以“新增行口径”这次没有掩盖任何已测文件的跌破；问题在“没有按 S2 口径出数”，不是“数字不达标”。

真正的问题是可复核性：4.3 只报了 `orchestration.py` 的新增行 490/494，没给它的全文件 94%；`auth.py`、`approval.py`、`tools_builtin.py` 这三个被 4.3 改到的 S2 清单文件，报告里只有 `tools_builtin.py` 的新增行 4/4，`auth.py` 和 `approval.py` 的全文件数字没有出现在 4.3 的验证记录里（`approval.py` 只在复审里出现）。6.8 好一些，`usage.py` 明确写了“全量报告中未覆盖行仅 109-111、300-301、329-330”（`6.8:482`），和本次实测完全一致；`agent.py` 只列了新增行，没给全文件 94%。

**修法**：spec 的 AC 不要自设“新增代码覆盖率”这个更弱的指标，直接引用 S2 的命令和 12 文件清单；每次交付附一行“S2 口径：12 文件全绿 + 整体 X%”，不要只给新增行。

### S2. 4.3 的 AC 账本前后计数不一致

同一份 spec 里，§4 的兑现表写 AC1“16 例全绿”、AC5“27 例”、AC6“14 例”（`4.3:404-409`），§7.1 写的是 17、30、31（`4.3:593-598`）。§6 的红转绿记录写“转绿后 96”（`4.3:446`）、“共 98 例”（`4.3:525`），§7.3 又写“四个测试文件共 156 例”（`4.3:632`）。

我用 `pytest --collect-only` 实测当前收集数：`test_workflow_drafting.py` 95、`test_workflow_draft_confirm_api.py` 56、`test_workflow_draft_e2e.py` 3、`test_workflow_draft_llm_gated.py` 2，合计 156；按前缀数 AC1 17、AC5 30、AC6 31。也就是说 §7.1/§7.3 是最终值，§4/§6 是红态当时的快照，但 spec 没有把这两类数字分开标注。更直接的是同一张 §4 表内：AC6 行写“14 例”（`4.3:409`），紧跟着的表注写“AC6 … 31 例全绿”（`4.3:418`）。评审按 §4 机械核对会得到错误结论。

**修法**：§4 的“兑现”列改成最终值，或者明确写“红态快照（2026-11-08）/ 最终值见 §7.1”；§6 的 96/98 也标注为阶段性数字。

### S3. 6.8 的 AC7/AC9 用了账本不允许的红态取值

`6.8:327` 与 `6.8:329` 把 AC7、AC9 的红态登记为“无红态·守护”，§6 又明确说“按「无红态·守护」登记”（`6.8:371`，表内对应 `6.8:398`）。但账本的允许值固定为 `红态` / `结构性红` / `无红态·特征测试` / `无红态·回归` / `手工留证`（`4.3:416-417`，`SKILL.md:65-66` 同义）。`无红态·守护` 是本次自造的值，机械复核会判为非法。语义上它更接近“无红态·特征测试”或“无红态·回归”，建议换成允许值，或在 skill 里正式增加该取值（本次审计不改流程文件，只指出偏差）。

### S4. 6.8 的 spec 评审报告命名缺 `plan-`

`CODING-STANDARDS.md:230` 规定的命名是 `YYYY-MM-DD-plan-<任务号>-spec-review.md`，4.2 的先例也是 `2026-11-06-plan-4.2-spec-review.md`。6.8 用的是 `2026-11-09-6.8-spec-review.md` 和 `-r2`。内容质量没有问题，偏差只在文件名；但这类命名是给人和脚本按任务号检索用的，缺了 `plan-` 会让同名规则失效。

### S5. 6.8 的 spec 评审报告没有“先行独立 docs commit”

两份 spec 评审报告标的是 2026-11-09，功能提交 `a2917ff` 是 2026-11-10，评审报告直到 `3056522` 才和代码评审报告一起进 git。`CODING-STANDARDS.md:246` 的成对提交规则里专门写了“编码前的 spec 评审报告例外，先行独立 docs commit”。实际执行把 spec 评审、代码评审和功能提交混在同一个时间窗口里，事后才归档。这样做的直接后果是：功能提交发生时，仓库里没有可引用的 spec 评审记录；如果实现阶段需要回看 R1–R7 的关闭依据，只能翻工作区。

### S6. 4.3 的 docs commit 夹带了三个历史 spec 的状态回填

`886cd01` 的正文自己写明：除了两份 4.3 复审报告，还“顺带把 `6.3-audit-log.md`、`security-hardening-v0.17.37.md`、`security-hardening-v0.17.38.md` 的交付状态回填为已完成”。`CODING-STANDARDS.md:246` 要求评审/复核报告单独成 docs commit。这三个回填与 4.3 无关，混进来会破坏“一个 docs commit 对应一次交付”的审计粒度，也让回滚边界变模糊。不是代码风险，但属于提交卫生问题。

### S7. 6.8 的 README 写“并列展示”，页面实际没有渲染 `wall_ms`

`README.md:9` 的 v0.17.52 条目写“与原有的模型调用耗时并列展示”。但 `a2917ff` 没有改 `web/`，`web/index.html:454-465` 的 `addUsageNote` 只读 `total_tokens/prompt/completion`，`:1445-1450` 的 `by_run` 渲染只读 token 字段；在 `web/*.html` 里搜 `wall_ms` 没有任何命中。`wall_ms` 确实进了 `/api/usage` 的 `by_run` 和 done 事件，但页面没有展示。

这有两种解释：如果“展示”指接口/用量记录，README 的措辞应该改成“接口输出”或“用量记录新增”，不要写“并列展示”；如果产品意图是页面也要展示，那 6.8 就漏了页面改动，`6.8:13-15` 的“页面人工测试免”也不成立。按当前代码，前者是事实，后者是潜在的功能缺口。建议把 README 改成与代码一致的表述；如果确实要展示，按页面关卡补原型和人工测试。

### S8. 4.3 的 AC 证据行号在 P2 修复后没有重锚

`CODING-STANDARDS.md:219` 要求证据“可由评审机械复核”。4.3 的 AC 表形式上是 `文件:行号`，但行号已经漂了：

- AC1 写 `server/orchestration.py:1613-1645`（`4.3:404`），当前 1613 起是 `_iter_draft_scopes`/`_find_draft`，路径校验在 `_draft_source_path`（1589）和 `_resolve_doc_path`（1660）；
- AC5 写 `:1862-1882`（`4.3:408`），当前这段是 `draft_orchestration_from_doc` 的 meta/审计写入，`get_draft_detail` 在 1929，`_do_confirm` 在 1958，`_do_discard` 在 2094；
- AC2 写 `:1710-1834`（`4.3:406`），实际函数起点是 1761。

这是 P2 修复和新增测试把代码往后推了几十行，spec 只在复审后更新了覆盖率数字（`rereview-r2.md:40` 的 N3），没有重锚行号。机械评审按行号跳过去会看到不相干的代码。建议按提交后的 hash 重锚，或者在证据里只写函数名 + 测试名，行号作为辅助。

### S9. 4.3 的并发确认用例有竞态抖动，6.8 用“排除文件”绕过去了

`6.8:382-385` 记录：6.8 的基线全量运行出现过一次 `test_ac5_concurrent_confirm_only_one_succeeds` 失败，实际收到 `[200, 200]`、期望 `[200, 409]`，单独重跑通过；随后用 `--ignore=tests/test_usage_run_wallclock.py` 得到“干净基线”。这至少说明 4.3 的并发确认用例存在竞态或断言时序问题，6.8 的基线通过排除文件规避了它，没有修复根因。建议单独复现并修掉这个抖动，否则它会在后续每次全量回归里随机出现，削弱“全绿”的可信度。

## 四、不成立

### N1. “覆盖率不达标”——不成立

三个被点名文件的全文件覆盖率是 `orchestration.py` 94%、`usage.py` 98%、`agent.py` 94%，都 ≥90%；`usage.py` 是 S2 的 12 文件之一，也过线。4.3 改到的另外三个 S2 清单文件本次复测为 `approval.py` 100%、`auth.py` 99%、`tools_builtin.py` 91%，也都在线上。命令和输出见 S1。4.3 的复审记录也独立测到 `approval.py` 100%、`orchestration.py` 94%。所以“报告写新增行是为了掩盖跌破 90%”这个判断不成立。

需要区分的是：报告的口径确实不是 S2 口径（S1），而且两个 spec 都没给整体覆盖率；这是可复核性和流程设计问题，不是这三个文件的阈值问题。

### N2. “红测试没有真红态”——不成立

4.3 的红态记录是 `54 failed, 1042 passed, 20 skipped`（`4.3:465`），失败分类能对上“生成入口未实现 35、确认/丢弃工具未实现 2、端点未注册 13、跨域清单未收口 1、禁止清单 1、空产出 1、visible_tool_names 1”，并明确标了 7 例“因 404 同样满足断言而暂绿”的弱绿（`4.3:469-490`）。6.8 的红态是 `11 failed, 2 passed`，失败原因分别是表缺失、`record_run` 不存在、迁移缺失、显式缺表（`6.8:390-398`）。这些都不是“先写实现再补测试”能造出来的。

红态不能从 git 单独复现，因为测试和实现落在同一个 `feat` commit 里；但成对提交规则本身就把 spec、测试、实现放在一个功能 commit，红态记录在 spec 里是符合约定的。真正的问题只是 4.3 的计数快照没标清楚（S2），不是红态造假。

### N3. “AC 证据不是 `文件:行号`”——形式上不成立

两份 spec 的 AC 表都有 `文件:行号`，也有测试名，形式合规。4.3 的问题是行号漂移（S8），不是没有行号。

### N4. “提交 message 体例 / 成对提交不合规”——主体不成立

四个提交的 message 都是 `类型 v0.17.xx：主题（spec: ...）` 的体例，两个 `feat` 后面都有对应的 `docs`，`docs` 正文都写了“对应功能提交：<hash>”。`feat` 提交里没有 `uploads/`、`outputs/`、`data/` 这类用户生成内容，文件都是功能、测试、spec、README、PLAN。核心的成对提交是遵守了的。偏差只在 S5（spec 评审报告没有先行提交）和 S6（4.3 docs commit 夹带旧 spec 回填），不是“成对提交整体没做”。

### N5. “新增 `anyio` 违反 S5 只减不增”——不成立

`ENGINEERING-METRICS.md` 的 S5 允许“新增需 spec 说明理由”。6.8 的 §3 D5 写清了理由：`agent.py` 直接 `import anyio`，`anyio` 是 `starlette` 的硬依赖，显式声明是为了不依赖传递解析。理由成立，规则允许。缺的是 `pip-audit`（M3），不是依赖数量本身。

### N6. “6.8 页面免关判定错误”——代码层面不成立

`a2917ff` 没有改 `web/`，页面没有新增或调整用户可见信息，`wall_ms` 也没被前端读取，三分口径第①/②条没有命中，页面关卡免掉在代码层面是成立的。README 的“并列展示”是表述问题（S7）；如果产品要求页面展示，那才需要重新触发页面关卡。

### N7. “4.3 修复后复审没做/只做一轮”——不成立

`docs/reviews/2026-11-09-v0.17.51-rereview.md` 和 `-rereview-r2.md` 是两轮复审。r2 关闭了 M1–M5，留下 N1（`_ABS_PATH_RE` 对“方案A:”的误报）作为阻塞项；当前代码已经用后边界 lookahead 修掉，并补了负向用例（`tests/test_workflow_drafting.py` 里的 `方案A` 等用例），所以“没复审”不成立。需要指出的是流程漏洞：r2 已经说“有条件通过 + N1 阻塞提交”，最后一轮之后没有第三方再确认，是靠作者自证关闭的（见第五节）。

### N8. “4.3 的 `conftest.py` / `.gitignore` 是夹带”——不成立

`tests/conftest.py:44-57` 的改动是把隔离默认值的保留绑定到 `RUN_LLM_TESTS`，非门控运行强制清空 `SANDBOX_URL`，并且有独立测试 `tests/test_conftest_env.py:33-38` 覆盖“非门控清空 / 门控保留”两个分支。这是为跑通门控测试做的隔离修复，r2 的 M2 也专门复审过。`.gitignore` 加的是 `orchestration_drafts/` 草稿目录，和 4.3 直接相关。两项都不属于无关夹带。

## 五、流程条文在真实执行中暴露的漏洞

这一节不是文字矛盾，是“照着做会出问题”或“不做也没人发现”的地方。

**1. 页面人工测试是软闸门。** 4.3 的提交正文自己写“页面人工验收未执行”，`PLAN.md:51` 也保留 `⏳`，但提交照样进了。skill 里的红线没有对应的 hook、CI 或提交脚本检查；只要 agent 在 spec 里如实登记缺口，就能带着缺口提交。照这个条文执行，最坏的情况是“缺口记录得很诚实，门却一直开着”。

**2. S2 覆盖率没有接进 AC 和 CI。** 4.3 把 AC10 定义成“新增代码覆盖率 ≥90%”，6.8 沿用“新增逻辑行口径”，两个 spec 都没有执行 `--cov=server --cov=sandbox` 的 12 文件清单检查，也没有整体覆盖率门槛。结果是：spec 自设的指标可以一直绿，S2 清单里的其他文件或整体覆盖率跌了也不会有人发现。这次三个文件刚好都过线，但流程本身没有兜住。

**3. `pip-audit` 是 checklist 项，没有产物。** requirements 变更后跑没跑审计，只靠人记得；没有报告文件、没有 CI 步骤、没有提交前脚本。6.8 这次就是一行 `anyio` 变更直接过了，没人发现审计缺失。

**4. 评审报告“必须有”但“没有也能交”。** 4.3 缺两份强制报告仍然交付了；6.8 的 spec 评审报告命名不对、归档顺序不对，也没有任何检查拦住。条文写了命名规范和“先行 docs commit”，但执行层没有“报告不存在即阻断”的机制。

**5. “最多 2 轮”复审会留下自证关闭的阻塞项。** r2 已经说 N1 是“P2（阻塞提交）”，但轮次上限到了，最后一轮的关闭只能由作者自己声明；没有第三方确认，也没有要求把“修复 + 负向用例”的回执单独归档。照这个规则，一个阻塞项可以在“最多 2 轮”之后靠作者自述关闭。

**6. 三分口径的页面免判定和 README 可以互相矛盾而不被发现。** 6.8 的 spec 说“不新增用户可见信息”，README 说“并列展示”；两者必有一个不准，但没有任何检查把 spec 的裁剪声明和 README 的用户可见描述对起来。如果 README 是对的，页面关卡就该触发；如果 spec 是对的，README 就该改。现在两种情况都不会被流程发现。

**7. AC 行号会随着修复漂移，但没有重锚检查。** 4.3 的 AC1/AC5 行号在 P2 修复后指向了别的函数，条文要求“可由评审机械复核”，但没有工具去解析 `文件:行号` 是否还落在声称的函数里。评审只能靠人肉跳转，跳错了也不会有提示。

## 六、复核命令与原始输出

以下命令均在 `e:\codebase\GitHub\private-agent-poc`、当前 HEAD `3056522` 下执行。

**覆盖率（三个被点名文件）**

```
.\.venv\Scripts\python.exe -m pytest -q --cov=server.orchestration --cov=server.usage --cov=server.agent --cov-report=term-missing
...
server\agent.py            396     22    94%   59-60, 62, 181-182, 236, 248-249, 284, 315, 423, 486-487, 518-519, 640-642, 671, 682, 711-712
server\orchestration.py   1419     84    94%   196, 199, 216, 238, 241, 245, 268, 381, 432-435, ...
server\usage.py            254      6    98%   109-111, 300-301, 329-330
TOTAL                     2069    112    95%
1196 passed, 20 skipped, 2 warnings in 267.64s (0:04:27)
```

**覆盖率（4.3 改到的三个 S2 清单文件）**

```
.\.venv\Scripts\python.exe -m pytest -q --cov=server.auth --cov=server.approval --cov=server.tools_builtin --cov-report=term-missing
server\approval.py          147      0   100%
server\auth.py              296      3    99%   197-198, 247
server\tools_builtin.py     181     16    91%   118-121, 137-138, 181, 254-258, ...
TOTAL                       624     19    97%
1196 passed, 20 skipped, 2 warnings in 269.94s (0:04:29)
```

**4.3 用例收集数**

```
pytest --collect-only -q tests/test_workflow_drafting.py
  => 95 tests collected
pytest --collect-only -q tests/test_workflow_draft_confirm_api.py
  => 56 tests collected
pytest --collect-only -q tests/test_workflow_draft_e2e.py
  => 3 tests collected
pytest --collect-only -q tests/test_workflow_draft_llm_gated.py
  => 2 tests collected
合计 156；按前缀 test_ac1_ 17、test_ac5_ 30、test_ac6_ 31。
```

**提交与文件清单**

```
ae9d8c5 feat v0.17.51：文档生成流程草稿（spec: docs/specs/4.3-doc-to-workflow.md）
886cd01 docs v0.17.51：4.3 复审报告归档与历史 spec 状态回填
a2917ff feat v0.17.52：run 墙钟时长（PLAN 6.8，spec: docs/specs/6.8-run-wallclock-duration.md）
3056522 docs v0.17.52：6.8 spec 评审（两轮）与实现代码评审报告归档
```

`ae9d8c5` 含 `web/index.html`、`docs/prototypes/4.3-draft-confirm/index.html`、`server/auth.py`、`server/approval.py`、`server/tools_builtin.py` 等；`a2917ff` 含 `requirements.txt`、`server/agent.py`、`server/usage.py`、`server/routes/chat.py`、`tests/perf_baseline.py`，不含 `web/`。`886cd01` 除两份 4.3 复审报告外，还含 `docs/specs/6.3-audit-log.md`、`docs/specs/security-hardening-v0.17.37.md`、`docs/specs/security-hardening-v0.17.38.md` 的状态回填。

**页面是否渲染 wall_ms**

```
Select-String -Path web\*.html -Pattern "wall_ms|wall"
=> No matches found
```

`web/index.html:454-465` 的 `addUsageNote` 只读 `total_tokens/prompt/completion`；`:1445-1450` 的 `by_run` 渲染只读 token 字段。

**`docs/reviews/` 中与本审计相关的文件**

```
2026-11-06-plan-4.2-spec-review.md
2026-11-09-6.8-spec-review.md
2026-11-09-6.8-spec-review-r2.md
2026-11-09-v0.17.51-rereview.md
2026-11-09-v0.17.51-rereview-r2.md
2026-11-10-v0.17.52-code-review.md
```

目录里还有更早的历史评审文件；关键点是上述列表中没有 `2026-11-09-plan-4.3-spec-review.md`，也没有 `2026-11-09-v0.17.51-*-review.md`。

---

本次审计没有修改代码、测试、spec、PLAN 或流程文档，只新增本报告，未提交。

---

## 七、建议修落地记录（2026-11-11，后续会话）

本节记录对「三、建议修」的处置，未改动任何产品代码或测试，未提交。

| 条目 | 改动位置 | 改后口径 | 状态 |
|---|---|---|---|
| S1 | `docs/specs/4.3-doc-to-workflow.md:414`（AC10）、`:642`（§7.3 缺口 2）、`:608`（§7.1）；`docs/specs/6.8-run-wallclock-duration.md:485-496` | 覆盖率不再以「新增行」自称达标，改为 `ENGINEERING-METRICS.md:101-105` 的 S2 口径（12 个安全敏感文件逐文件全文件 ≥90% + `server/+sandbox/` 整体 ≥90%），并附 2026-11-11 实测：整体 6355/6794=93.54%，12 文件全部 ≥90%（最低 `tools_builtin.py` 91.2%）；`usage.py` 97.7%、`agent.py` 94.4%、`orchestration.py` 94.1% | 闭环 |
| S2 | `4.3:404-408`（AC1/AC5/AC6 收敛列）、`:446`、`:455-458`、`:530` | §4 收敛列改为最终值 17/30/31；§6 的 96/98 与 §4 表标注为 2026-11-08 阶段性快照，并给出最终 95/56/3/2=156 | 闭环 |
| S3 | `6.8:333`、`:335`、`:377`、`:404`、`:410` | `无红态·守护` 全部改为账本允许值 `无红态·特征测试`（AC7/AC9） | 闭环 |
| S7 | 未改；`README.md:9`、`web/index.html:465-468`、`:1476-1477` | v0.17.53 工作区改动已真正渲染 `wall_ms`（模型耗时与端到端等待并列；未观测显示「未观测」），README 与代码一致 | 闭环（非本会话改动） |
| S8 | `4.3:404-412`、`:598-606` | AC1/AC2/AC3/AC4/AC5/AC6/AC6b/AC7/AC8 的 `server/orchestration.py` 证据按当前实现重锚（`_draft_source_path:1589`、`_resolve_doc_path:1660`、`draft_orchestration_from_doc:1761`、`_do_confirm:1958`、`_do_discard:2094` 等） | 闭环 |
| S4 | `docs/CODING-STANDARDS.md:230` | 不重命名、不新增副本：在命名条款补版本边界——v0.17.52 及更早报告沿用旧文件名，新规则自 v0.17.53 起适用，历史文件不改名。理由见下方说明 | 闭环 |
| S5 | `docs/reviews/2026-11-09-6.8-spec-review.md:3`、`-r2.md:3`、`docs/specs/6.8-run-wallclock-duration.md:8-10` | 加「事后归档」标注：报告为功能提交 `a2917ff` 之后补归档（`3056522`），评审实际发生在提交前，未满足「先行独立 docs commit」；只补留痕，结论与证据不改 | 闭环 |
| S6 | 仅本报告登记，不改文件 | `886cd01` 除归档 4.3 复审报告外，还回填了 `6.3-audit-log.md`、`security-hardening-v0.17.37.md`、`security-hardening-v0.17.38.md` 的头部状态；影响是 docs commit 不再是纯报告归档，文档变更无法按 commit 单独回溯，不涉代码/测试，无功能风险 | 已登记（历史偏差） |
| S9 | `tests/test_workflow_draft_confirm_api.py`（未改） | 并发确认用例存在竞态（间歇性冲突/抖动），6.8 全量回归曾用 `--ignore` 绕开；修它要改测试代码并重跑基线，超出本轮口径/留痕范围，转后续任务 | 待办（本轮不做） |

实测环境说明：覆盖率命令 `pytest -q --cov=server --cov=sandbox` 于 2026-11-11 在当前工作区（HEAD `3056522` + v0.17.53 未提交改动）执行，结果 `1200 passed / 20 skipped / 1 failed`；唯一失败为并发 fastmcp 升级中的 `tests/test_dependency_versions.py`（3.4.8 < 3.4.10），与 4.3/6.8 无关。

**S4 为什么选择不改名**：`3056522` 已把 `2026-11-09-6.8-spec-review.md`、`-r2.md` 作为历史提交内容固化；在工作区改名会让工作区文件名与提交记录不一致，git 历史仍指向旧名，无法真正「回溯改名」，还会让 spec 与提交 message 中的旧路径变成半失效引用。因此选择「历史文件不动、规则只对新报告生效」，并在命名条款写明版本边界。

**后续任务承接**：S9 需在 `tests/test_workflow_draft_confirm_api.py` 复现并发确认竞态并修复，禁止再用 `--ignore` 绕过；修复后恢复 4.3/6.8 全量回归基线。S6 只作历史偏差登记，无后续动作。
