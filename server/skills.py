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
import io
import os
import re
import shutil
import zipfile
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


def create_skill(name: str, description: str, instructions: str,
                 overwrite: bool = False) -> str:
    """创建技能文件（overwrite=true 时整体替换已存在技能）。名称只允许小写字母/
    数字/连字符，且限制在 skills/ 目录内。"""
    name = name.strip().lower()
    if not NAME_PATTERN.match(name):
        return "创建失败：技能名只允许小写字母、数字、连字符（如 weekly-report）"
    path = _skills_dir() / name / "SKILL.md"
    # 面向用户/模型只呈现逻辑路径（相对工作区），不暴露服务器部署布局（安全修复）
    shown = f"skills/{name}/SKILL.md"
    if path.exists() and not overwrite:
        return (f"技能「{name}」已存在：{shown}\n"
                "如需按新内容更新该技能，请再次调用 create_skill 并将 overwrite "
                "参数设为 true（整份指令会被替换，原内容不保留）。")
    action = "更新" if path.exists() else "创建"
    content = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n{instructions.strip()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if action == "更新":
        # 覆盖更新时清理该技能目录下的旧附属文件（references/、旧脚本等），
        # 防止旧内容残留与新指令混淆（评审 P3）。先写新 SKILL.md 再清理：
        # 清理中途失败最坏只是残留部分旧文件，技能本体不受损。
        for p in sorted(path.parent.iterdir(), reverse=True):
            if p == path:
                continue
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:
                continue
    return f"技能「{name}」已{action}：{shown}\n（下轮对话即可被 Agent 自动发现）"


# ---- 技能包（zip）导入（v0.17.7）：目录型技能的标准传输形态 ----

# zip 解包安全上限：条目数 / 解压后总字节（防 zip 炸弹）；扩展名白名单（可执行二进制一律拒）
_SKILLPACK_MAX_ENTRIES = 200
_SKILLPACK_MAX_TOTAL = 20 * 1024 * 1024
_SKILLPACK_TEXT_EXTS = {".md", ".markdown", ".txt", ".py", ".json", ".yaml", ".yml",
                        ".csv", ".tsv", ".toml", ".ini", ".cfg", ".conf", ".rst",
                        ".log", ".sh", ".bat", ".xml", ".html", ".htm", ".js", ".ts"}


def import_skill_zip(data: bytes, overwrite: bool = False) -> dict:
    """导入技能包 zip（目录型技能：SKILL.md + references/ 等多文件结构）。
    安全校验：条目路径防穿越（zip-slip）、条目数/解压后总量上限（zip 炸弹）、
    扩展名白名单。技能名取 SKILL.md frontmatter（与 create_skill 同一命名白名单）；
    已存在且未 overwrite 时抛 FileExistsError，覆盖时整目录替换。
    v0.17.7 评审：覆盖导入先解包临时目录完成全部校验与写盘再原子换名
    （失败原技能不受损）；返回值只含逻辑路径（不暴露服务器部署位置）。
    返回 {"name", "files": [相对路径...], "path": "skills/<name>"}。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 zip 文件")
    with zf:
        entries = [i for i in zf.infolist() if not i.is_dir()]
        if not entries:
            raise ValueError("压缩包为空")
        if len(entries) > _SKILLPACK_MAX_ENTRIES:
            raise ValueError(f"文件数超过上限（{_SKILLPACK_MAX_ENTRIES}）")
        if sum(i.file_size for i in entries) > _SKILLPACK_MAX_TOTAL:
            raise ValueError(f"解压后总大小超过上限（{_SKILLPACK_MAX_TOTAL // 1024 // 1024}MB）")
        # SKILL.md 可在 zip 根或其所在的一级目录下；包内其余内容必须同属该技能目录。
        # 归档含多个 SKILL.md（如目录压缩方式差异）时取路径最浅者——根条目优先，
        # 避免归档顺序决定选谁（评审 P3）
        candidates = [i for i in entries
                      if i.filename == "SKILL.md" or i.filename.endswith("/SKILL.md")]
        if not candidates:
            raise ValueError("压缩包中未找到 SKILL.md（技能包必须包含 SKILL.md）")
        skill_entry = min(candidates,
                          key=lambda i: (i.filename.count("/"), len(i.filename)))
        base = skill_entry.filename[:-len("SKILL.md")]  # 前缀目录（可为空串）
        if any(not i.filename.startswith(base) for i in entries):
            raise ValueError("压缩包含 SKILL.md 之外的顶层内容（应只含一个技能目录）")
        for i in entries:  # 逐条路径与类型校验（zip-slip / 炸弹 / 白名单）
            rel = i.filename[len(base):]
            if (not rel or rel.startswith("/") or "\\" in rel
                    or ".." in rel.split("/") or ":" in rel):
                raise ValueError(f"非法条目路径: {i.filename}")
            if Path(rel).suffix.lower() not in _SKILLPACK_TEXT_EXTS:
                raise ValueError(f"不支持的文件类型: {i.filename}（白名单："
                                 f"{', '.join(sorted(_SKILLPACK_TEXT_EXTS))}）")
        meta = _parse_skill_md(zf.read(skill_entry).decode("utf-8", errors="replace"))
        name = (meta.get("name") or "").strip().lower()
        if not NAME_PATTERN.match(name):
            raise ValueError(f"SKILL.md frontmatter 的 name 缺失或不合法（{name or '空'}）："
                             "只允许小写字母/数字/连字符")
        target = _skills_dir() / name
        if target.exists() and not overwrite:
            raise FileExistsError(f"技能「{name}」已存在：skills/{name}"
                                  "（覆盖导入将整体替换原内容）")
        # 覆盖导入原子化（评审 P2）：先解包临时目录（全部校验已通过），成功后
        # 「旧目录挪走 → 临时目录顶上 → 删旧」；中途任何失败都回滚，原技能不损坏。
        # Windows 上 rename 不能覆盖已存在目录，故三步换名。
        tmp = _skills_dir() / f".{name}.import-{os.getpid()}"
        old = _skills_dir() / f".{name}.old-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        files = []
        try:
            for i in entries:
                rel = i.filename[len(base):]
                dest = tmp / rel
                if not dest.resolve().is_relative_to(tmp.resolve()):  # 双保险
                    raise ValueError(f"非法条目路径: {i.filename}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(i))
                files.append(rel)
            if target.exists():
                target.rename(old)
                try:
                    tmp.rename(target)
                except OSError:
                    old.rename(target)      # 回滚：换名失败原技能复位
                    raise
                shutil.rmtree(old, ignore_errors=True)
            else:
                tmp.rename(target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return {"name": name, "files": sorted(files), "path": f"skills/{name}"}


def skill_index_prompt() -> str:
    """生成注入系统提示词的技能索引文本。"""
    all_skills = list_skills()
    if not all_skills:
        return ""
    lines = ["", "你可以使用以下技能（当用户任务匹配技能描述时，"
             "先调用 load_skill 工具获取完整指令，再按指令执行）："]
    for s in all_skills:
        lines.append(f"- {s['name']}: {s['description']}")
    return "\n".join(lines)


def signature() -> tuple:
    """技能文件签名（相对路径 + mtime 纳秒），纳入 Agent 图重建检测：
    create_skill / 技能包导入后下一轮自动重绑生效，无需重启。
    v0.17.7 评审 P2：改用 st_mtime_ns——秒级 mtime 在快速连续更新时不变，
    会导致图不重建、新技能不生效。"""
    items = []
    for d, _src in _all_skill_dirs():
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("SKILL.md")):
            try:
                items.append((p.relative_to(d).as_posix(), p.stat().st_mtime_ns))
            except OSError:
                continue
    return tuple(items)
