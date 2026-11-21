# Human-Centered UI 与 PAP 实施计划

> 执行方式：当前会话按任务提交，每个阶段保留可核验证据；不把截图当作完整无障碍合规。

**目标：** 建立可发现、可分阶段执行的 `human-centered-ui` Skill，并实际改造、验证 PAP 聊天首页和管理控制台。

**架构：** 个人 Skill 负责路由与四阶段契约；PAP 保持原生 HTML/CSS/JS 与离线约束。先采集真实基线，再以共享 Token 和页面局部样式实施改造，用相同视口和可比状态复测。

**技术栈：** Codex Skill、Python/FastAPI、原生 HTML/CSS/JS、Playwright、现有 pytest 套件。

---

### 任务 1：建立并验证 Skill 契约
**文件：**
- 新增 `C:\Users\Lenovo\.codex\skills\human-centered-ui\SKILL.md`
- 新增 `C:\Users\Lenovo\.codex\skills\human-centered-ui\references\*.md`
- 新增仓库内 `docs/reviews/2026-11-21-human-centered-ui-skill-validation.md`

1. 写统一入口 frontmatter、路由、共享事实与审美判断分离原则。
2. 写 direction/taste/audit/self-check 契约及专家适配，确保各阶段独立可执行。
3. 运行 `quick_validate.py`，检查内部链接、placeholder、阶段独立性，记录结果。
4. 提交仓库内验证记录；个人 Skill 文件保留在个人技能目录。

### 任务 2：采集基线与可用性证据
**文件：** `.screenshots/human-centered-ui/`、`docs/reviews/2026-11-21-human-centered-ui-pap.md`
1. 确认 mock 服务启动方式、测试数据、浏览器工具可用性。
2. 采集 1440×900 的聊天首页、记录面板、控制台用户列表与审计筛选，补 390px 窄屏。
3. 实际查看截图，记录代码关联、任务影响、值得保留的元素和验证边界。

### 任务 3：建立共享视觉语言
**文件：** `web/tokens.css`、两页 HTML
1. 根据基线确定克制的中性底色、文本层级、少量语义强调、紧凑圆角与阴影。
2. 调整共享焦点、表单、按钮、状态反馈，保持现有选择器和业务 DOM 挂钩。
3. 无外部字体、图标、脚本或 CDN；不修改后端权限/API。

### 任务 4：优化聊天任务流
**文件：** `web/index.html`
1. 收敛欢迎区装饰，明确提问、附件和发送的主任务。
2. 优化顶栏、消息、输入、状态、历史面板与登录模态的层级和文案。
3. 保留上传、SSE、审批、用户菜单、历史恢复等事件和 ID，必要处补语义。

### 任务 5：优化控制台信息架构
**文件：** `web/console.html`
1. 收敛页头与导航视觉噪声，突出当前管理任务。
2. 优化表格、筛选、危险动作、空态和模态；保留角色显示和 admin 行为。
3. 处理 390px 下表格横向滚动、控件可达与内容不被遮挡。

### 任务 6：浏览器回归与视觉验证
**文件：** `.screenshots/human-centered-ui/`、必要测试文件
1. 相同视口采集修改后截图并实际查看，检查视觉差异与文字/状态完整性。
2. 验证聊天发送、面板开关、控制台筛选、危险模态退出、键盘、桌面与窄屏。
3. 记录控制台错误、请求失败、通过/失败/未检查项；既有问题与本次改动分开。

### 任务 7：测试、报告与最终审阅
**文件：** `docs/reviews/2026-11-21-human-centered-ui-pap.md`、相关测试
1. 运行 Skill 校验、受影响测试和全量离线 pytest。
2. 完成报告与前后截图对应，检查 `git diff --check` 和无 CDN。
3. 审阅 diff，提交实现与验证结果；不改无关业务缺陷。
