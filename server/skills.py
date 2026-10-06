"""Skills 注册中心：扫描 skills/ 目录下的 SKILL.md，提供索引/加载/创建。

技能格式（与业界 SKILL.md 约定一致）：

    skills/
    └── weather-report/
        └── SKILL.md
            ---
            name: weather-report
            description: 一句话描述（模型据此判断何时使用）
            ---
            # 标题
            具体指令……

渐进式披露：系统提示词只注入索引（name + description），
模型需要时通过 load_skill 工具拉取完整指令。
"""
import re
from pathlib import Path

from . import config

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")


def _skills_dir() -> Path:
    d = config.SKILLS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# 额外技能来源（领域包注册），格式 [(目录, 来源标签)]
_extra_dirs: list[tuple[Path, str]] = []


def register_extra_dir(path, source: str = "") -> None:
    """注册额外的技能扫描目录（领域包调用）。重复注册自动去重。"""
    p = Path(path).resolve()
    if p.is_dir() and all(p != d for d, _ in _extra_dirs):
        _extra_dirs.append((p, source))


def reset_extra_dirs() -> None:
    """清空额外技能来源（重建 Agent 图时按当前启用的领域包重新注册）。"""
    _extra_dirs.clear()


def _all_skill_dirs() -> list[tuple[Path, str]]:
    return [(_skills_dir(), "")] + list(_extra_dirs)


def _parse_skill_md(text: str) -> dict:
    """解析 SKILL.md：优先 frontmatter，缺省时回退到首个标题/首行。"""
    name, description = "", ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
    if not name:
        hm = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        name = hm.group(1).strip() if hm else ""
    if not description:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-")):
                description = line[:120]
                break
    return {"name": name, "description": description}


def list_skills() -> list[dict]:
    """返回全部技能索引 [{name, description}]，多来源合并（内核 skills/ + 领域包）。
    同名技能以先注册的来源为准（内核优先），后到的跳过并告警。"""
    result = []
    seen: set[str] = set()
    for base, source in _all_skill_dirs():
        for smd in sorted(base.glob("*/SKILL.md")):
            try:
                meta = _parse_skill_md(smd.read_text(encoding="utf-8", errors="replace"))
                name = meta["name"] or smd.parent.name
                if name in seen:
                    print(f"[skills] 技能名冲突「{name}」（{source or '内核'}），跳过")
                    continue
                seen.add(name)
                result.append({
                    "name": name,
                    "description": meta["description"] or "（无描述）",
                })
            except Exception:
                continue
    return result


def load_skill(name: str) -> str | None:
    """按名称（目录名或 frontmatter name）加载完整 SKILL.md，不存在返回 None。"""
    if not NAME_PATTERN.match(name):
        return None
    for base, _ in _all_skill_dirs():
        direct = base / name / "SKILL.md"
        if direct.is_file():
            return direct.read_text(encoding="utf-8", errors="replace")
    for base, _ in _all_skill_dirs():
        for smd in base.glob("*/SKILL.md"):
            try:
                if _parse_skill_md(smd.read_text(encoding="utf-8", errors="replace"))["name"] == name:
                    return smd.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return None


def create_skill(name: str, description: str, instructions: str) -> str:
    """创建技能文件。名称只允许小写字母/数字/连字符，且限制在 skills/ 目录内。"""
    name = name.strip().lower()
    if not NAME_PATTERN.match(name):
        return "创建失败：技能名只允许小写字母、数字、连字符（如 weekly-report）"
    path = _skills_dir() / name / "SKILL.md"
    if path.exists():
        return f"技能「{name}」已存在：{path}"
    content = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n{instructions.strip()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"技能「{name}」已创建：{path}\n（下次启动或新会话即可被 Agent 发现）"


def skill_index_prompt() -> str:
    """生成注入系统提示词的技能索引文本。"""
    skills = list_skills()
    if not skills:
        return ""
    lines = ["", "你可以使用以下技能（当用户任务匹配技能描述时，"
             "先调用 load_skill 工具获取完整指令，再按指令执行）："]
    for s in skills:
        lines.append(f"- {s['name']}: {s['description']}")
    return "\n".join(lines)
