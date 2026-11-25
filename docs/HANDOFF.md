# 会话交接（HANDOFF）

维护规则：本文件只描述「当前状态」，每次交接时整体覆盖重写；变更历史由 git 和 `docs/specs/` 承担，不在本文件累积。

## 1. 现状

- 项目：Private Agent Platform（PAP），仓库 `E:\codebase\GitHub\private-agent-poc`，分支 `master`，版本 **v0.23.0**。
- 工作区干净（2026-11-25 核对）。
- 最近提交：`dd8c1de` 顶栏徽章/按钮美化 + 首页截图；`627874d` 文档与界面润色；`1e8957e` POC→Platform 更名；`441127b` PLAN 8.5 记忆 LLM 判重（v0.23.0）；`975ba0a` 文档同步。

## 2. 环境与启动

- Python：`.venv\Scripts\python.exe`；依赖缺失时先 `pip install -r requirements.txt`。
- 启动：`.venv\Scripts\python.exe server/main.py --host 127.0.0.1 --port 7100`（先确认 7100 未被旧进程占用）。
- `.env`：`MODEL_NAME=deepseek-v4-flash`（研发/测试默认 flash，改后需重启服务）。
- 前端无构建步骤，改 `web/*.html` 后浏览器需强制刷新（`Ctrl+Shift+R`）。
- 全量回归：`.venv\Scripts\python.exe -m pytest -q`（约 10 分钟；最近一次 1249 passed, 28 skipped）。

## 3. 验收账号

- `mem-admin`（admin）与 `mem-user`（user，测试部）已创建。
- 口令不入库：见本地 `tools/_login.json`（已 gitignore）或向用户索取；任何情况下不得提交 `cookies.txt`、`_login.json` 等凭据文件。
- 重建账号：`tools\manage_users.py add <name> --role <role>` + `tools\manage_users.py passwd <name> --password "<口令>"`，详见 `docs/E2E-ACCEPTANCE.md` 前置条件。

## 4. 进度与验收

- 批次一~四：全部交付。
- 批次五（序号 17–24）：6.7 ✅ v0.19.0；6.4 ✅ v0.18.0（真实 Postgres 门控用例待有 PG 环境复验）；6.8 ✅。
  **4.3 ⏳（序号 17）**：实现、自动测试与门控已通过，仅剩页面人工验收——当前唯一阻塞批次五收口的项。
  7.3 包市场（序号 20）未启动；7.1 / 7.2 / 7.4 未启动。
- 批次六（记忆）：8.1 ✅ v0.20.0；8.2 ✅（由 8.1 覆盖）；8.3 ✅ v0.21.0；8.4 ✅ v0.22.0；8.5 ✅ v0.23.0。
  8.4.1 通知中心目前只记录在 `docs/specs/8.4-admin-session-notification.md` 末尾，尚未入 PLAN。
- PLAN「后续轮次待办」中的安全评审遗留项保持原状，未计入任何已交付版本。

## 5. 下一步建议

1. **4.3 页面人工验收**（序号 17）：按 `docs/specs/4.3-doc-to-workflow.md` 走一遍上传文档→生成草稿→确认启用，完成后在 PLAN 标 ✅ 并补版本号。
2. **7.3 包市场**（序号 20，M）：批次五下一个未启动项，按 spec-first 流程先出 spec。
3. 7.1 企业连接器 → 7.2 AD/LDAP → 7.4 沙箱镜像（按全局序号）。
4. 可选小项：8.4.1 通知中心（先补进 PLAN 再动工）。

## 6. 文档索引

- 计划与进度：`docs/PLAN.md`
- 规格：`docs/specs/`（记忆与通知：8.1 / 8.3 / 8.4 / 8.5）
- 功能、安全、部署：`docs/FEATURES.md`、`docs/SECURITY.md`、`docs/DEPLOYMENT.md`、`docs/PRODUCTION.md`
- 人工验收：`docs/E2E-ACCEPTANCE.md`（E2E-6 记忆链路、E2E-7 管理通知链路）
- 项目约定：`AGENTS.md`（模型选型、用户画像、spec-first 工作流）

## 7. 新会话启动

先读 `AGENTS.md` 与本文件，核对 `git status`、服务端口与 `git log -5`，然后从「下一步建议」第 1 项继续。
