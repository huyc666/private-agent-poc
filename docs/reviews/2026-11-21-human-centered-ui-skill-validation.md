# Human-Centered UI Skill 验证

## 契约测试

- 统一入口包含有效 frontmatter，且同时描述用途与触发语。
- 四个阶段可独立调用，并分别引用对应契约；入口只承载路由和共享原则。
- 不引入必须联网的资源，不把自动截图等同于完整无障碍合规。
- 仓库保留验证记录；个人 Skill 安装于 `%USERPROFILE%\.codex\skills\human-centered-ui`。

执行日期：2026-11-21。状态：通过。

## 执行结果

- 用项目 `.venv` 运行系统 Skill Creator 的 `quick_validate.py`，实际输出 `Skill is valid!`。
- `C:\Users\Lenovo\.codex\skills\human-centered-ui\SKILL.md` 提供统一路由，四个独立参考文件分别定义方向、品味、审计、自检。
- `C:\Users\Lenovo\.codex\skills\human-centered-ui\agents\openai.yaml` 补充了可读名称与用途说明。
- 无外部网络资源；自动事实与审美判断、未检查边界在入口与阶段文档中均有明确约束。
- 系统 Python 缺少 `yaml`；改用带依赖的项目解释器并启用 `PYTHONUTF8=1` 后通过。

## 实战检验

PAP 实际改造与截图检验见 `docs/reviews/2026-11-21-human-centered-ui-pap.md`。这次使用说明：窄屏问题不能只靠 `scrollWidth` 判断；隐藏侧栏必须检查可见性和控件边界，截图还须等待动画完成。这些经验已落实到测试与自检契约。
