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
import hashlib
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


def current_scope() -> str | None:
    """当前对话上下文的专属技能域（v0.17.9）。认证模式按 usage.current_user
    分域（与文件域 config.user_scope 同规则，v0.17.32 SEC-2 哈希防碰撞）；
    开放模式/匿名 → None（无专属域概念，技能创建与加载全部走通用层，行为同旧版）。"""
    from . import auth, usage   # 函数内导入：避免模块级循环依赖
    u = usage.current_user.get() or ""
    if not auth.enabled() or not u or u == "anonymous":
        return None
    return config.user_scope(u)


def _personal_root() -> Path:
    d = config.PERSONAL_SKILLS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _personal_dir(scope: str | None) -> Path | None:
    """scope 对应的专属技能根目录 skills_personal/<域>/；scope 为空返回 None。"""
    if not scope:
        return None
    return _personal_root() / scope


# 额外技能来源（领域包注册），条目 (父目录, 来源标签, 允许的技能目录名集合,
# 包根目录, SKILL.md 内容快照)。v0.17.35 R4（复验报告）：父目录注册必须附
# 「已验证接受的具体技能目录名集合」——无过滤注册会让未声明、甚至被解析期
# 拒绝（叶子出包等）的兄弟技能经共同 skills/ 父目录重新进入索引/加载/签名。
# allowed=None 维持旧行为（整个目录可扫，内核 skills/ 与既有测试直调用）；
# root 给定时对实际可加载的每个 SKILL.md 做加载期真实路径包含校验（见
# _skill_entry_ok）。v0.17.36 R8（复验报告）：texts 为扫描接受时物化的
# 内容快照——索引/加载只读快照、不再访问磁盘，检查与读取之间的换链窗口
# 由此闭合（texts=None 的来源维持直读：内核 skills/ 不在信任链内）。
_extra_dirs: list[tuple[Path, str, frozenset[str] | None, Path | None,
                        dict[str, str] | None]] = []


def register_extra_dir(path, source: str = "", allowed=None, root=None,
                       texts=None) -> None:
    """注册额外的技能扫描目录（领域包调用）。重复注册自动去重（同一父目录
    再次注册时合并允许集合与快照，防御性——正常路径各包父目录唯一）。
    v0.17.36 R9：resolve 遇链接循环等异常时告警并跳过注册（fail-closed，
    注册入口不上抛——报告修复要求 2）。"""
    allowed_set = frozenset(allowed) if allowed is not None else None
    # 评审 F1（P1）：空 dict 与 None 必须区分——None = 无快照来源（内核
    # skills/ 维持直读）；显式空快照 = fail-closed 注册（服务端全部条目因
    # 缺失快照被拒）——静默回退直读会重开 R8 换链窗口
    texts_map = dict(texts) if texts is not None else None
    try:
        p = Path(path).resolve()
        root_p = Path(root).resolve() if root is not None else None
    except (OSError, RuntimeError) as e:
        print(f"[skills] 额外技能目录注册失败（路径解析异常"
              f"{type(e).__name__}），跳过: {path}")
        return
    for i, (d, _s, a, _r, _t) in enumerate(_extra_dirs):
        if d == p:
            if allowed_set:
                _extra_dirs[i] = (d, _s, (a or frozenset()) | allowed_set,
                                  root_p if root_p is not None else _r,
                                  {**(_t or {}), **texts_map}
                                  if texts_map else _t)
            return
    if p.is_dir():
        _extra_dirs.append((p, source, allowed_set, root_p, texts_map))


def reset_extra_dirs() -> None:
    """清空额外技能来源（重建 Agent 图时按当前启用的领域包重新注册）。"""
    _extra_dirs.clear()


def _all_skill_dirs() -> list[tuple[Path, str, frozenset[str] | None,
                                    Path | None, dict[str, str] | None]]:
    return [(_skills_dir(), "", None, None, None)] + list(_extra_dirs)


def _skill_entry_ok(smd: Path, allowed, root, source: str) -> bool:
    """包来源技能条目准入（v0.17.35 R4，索引/加载/签名三入口同一条规则）：
    1) 目录名必须在注册时给定的允许集合内——即清单声明并经 packs._parse_pack
       四重校验（规范路径/resolve 包含/叶子包含/去重）的集合，未声明或被拒
       兄弟不得经父目录复活；
    2) root 给定时 SKILL.md 真实路径仍须在包根内——解析期校验之后磁盘仍可能
       被换成指向包外的链接，加载期再校验一次（报告 R4 修复要求 3）。
    v0.17.36 R8（复验报告）：本函数只把「名字与路径」准入；包来源的内容
    一律取注册时物化的快照（_extra_dirs 条目 texts）——准入通过后不再有
    第二次磁盘读取，检查-读取间隙的换链改变不了服务返回的内容（磁盘变化
    经签名检测触发重建后，快照整体替换）。
    任一不满足 → 跳过并告警（fail-closed：宁误拒不误放，且可定位）。"""
    if allowed is not None and smd.parent.name not in allowed:
        print(f"[skills] 技能目录「{smd.parent.name}」不在来源允许集合内，"
              f"跳过（来源：{source}）")
        return False
    if root is not None and root not in smd.resolve().parents:
        print(f"[skills] SKILL.md 指向包目录之外，跳过（{smd}）")
        return False
    return True


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


def list_skills(scope: str | None = None) -> list[dict]:
    """返回当前用户可见的技能索引 [{name, description}]：通用 skills/ + 领域包
    + 本人专属 skills_personal/<域>/（scope 为 None 只看通用层）。
    同名技能通用层优先（专属同名被遮蔽——创建时已拦截，此处兜底跳过并告警，
    便于排查手放文件/导入残留导致的「技能不出现在清单」问题）。"""
    result: list[dict] = []
    seen: set[str] = set()

    def _scan(base: Path, source: str, allowed=None, root=None, texts=None) -> None:
        for smd in sorted(base.glob("*/SKILL.md")):
            if not _skill_entry_ok(smd, allowed, root, source):
                continue
            if texts is not None:
                # v0.17.36 R8：包来源只读注册时物化的快照，不再读盘——
                # 准入通过后没有第二次磁盘访问，换链改变不了索引内容；
                # 快照缺失 fail-closed（正常路径与 allowed 同源，防御性）
                content = texts.get(smd.parent.name)
                if content is None:
                    print(f"[skills] 技能目录「{smd.parent.name}」无内容快照，"
                          f"跳过（来源：{source}）")
                    continue
            else:
                try:
                    content = smd.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
            try:
                meta = _parse_skill_md(content)
                name = meta["name"] or smd.parent.name
                if name in seen:
                    print(f"[skills] 技能名冲突「{name}」（{source} 被遮蔽），跳过")
                    continue
                seen.add(name)
                result.append({
                    "name": name,
                    "description": meta["description"] or "（无描述）",
                })
            except Exception:
                continue

    for base, source, allowed, root, texts in _all_skill_dirs():
        _scan(base, source or "内核", allowed, root, texts)
    pdir = _personal_dir(scope)
    if pdir is not None and pdir.is_dir():
        _scan(pdir, f"专属域/{scope}")
    return result


def load_skill(name: str, scope: str | None = None) -> str | None:
    """按名称（目录名或 frontmatter name）加载完整 SKILL.md，不存在返回 None。
    先查通用层（skills/ + 领域包），再查本人专属域 skills_personal/<scope>/
    ——user 角色只能加载自己域的专属技能（v0.17.9）。
    v0.17.35 R4：包来源条目统一过 _skill_entry_ok（目录名在允许集合内 + 真实
    路径在包根内）——未声明或被拒兄弟不得经父目录加载；frontmatter 名与
    目录名不同的声明技能经兜底扫描仍可按 frontmatter 名加载。"""
    if not NAME_PATTERN.match(name):
        return None
    for base, _src, allowed, root, texts in _all_skill_dirs():
        if allowed is not None and name not in allowed:
            continue    # 目录名直查：请求名不在允许集合（frontmatter 名走兜底）
        direct = base / name / "SKILL.md"
        if direct.is_file() and _skill_entry_ok(direct, allowed, root, _src):
            # v0.17.36 R8：包来源返回注册时物化的快照——准入通过后不再读盘，
            # 检查-读取间隙换入的链接改变不了返回内容（快照缺失 fail-closed）
            if texts is not None:
                content = texts.get(name)
                if content is None:
                    print(f"[skills] 技能目录「{name}」无内容快照，"
                          f"跳过（来源：{_src}）")
                    continue
                return content
            return direct.read_text(encoding="utf-8", errors="replace")
    pdir = _personal_dir(scope)
    if pdir is not None:
        direct = pdir / name / "SKILL.md"
        if direct.is_file():
            return direct.read_text(encoding="utf-8", errors="replace")
    for base, _src, allowed, root, texts in _all_skill_dirs():
        for smd in base.glob("*/SKILL.md"):
            if not _skill_entry_ok(smd, allowed, root, _src):
                continue
            if texts is not None:
                content = texts.get(smd.parent.name)    # R8：只读快照，不读盘
                if content is None:
                    # 评审 F3（P3）：与直查/索引同口径告警，兜底扫描不静默
                    print(f"[skills] 技能目录「{smd.parent.name}」无内容快照，"
                          f"跳过（来源：{_src}）")
                    continue    # 快照缺失 fail-closed
            else:
                try:
                    content = smd.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
            try:
                if _parse_skill_md(content)["name"] == name:
                    return content
            except Exception:
                continue
    if pdir is not None and pdir.is_dir():
        for smd in pdir.glob("*/SKILL.md"):
            try:
                if _parse_skill_md(smd.read_text(encoding="utf-8", errors="replace"))["name"] == name:
                    return smd.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return None


def create_skill(name: str, description: str, instructions: str,
                 overwrite: bool = False) -> str:
    """创建技能文件（overwrite=true 时整体替换已存在技能）。名称只允许小写字母/
    数字/连字符。存储分流（v0.17.9）：user 角色只写入本人专属域
    skills_personal/<域>/（技能是全员可见的能力资产，普通用户不应有能力向
    全员发布，与审批决议收紧同原则）；approver/admin/开放模式写通用 skills/。"""
    name = name.strip().lower()
    if not NAME_PATTERN.match(name):
        return "创建失败：技能名只允许小写字母、数字、连字符（如 weekly-report）"
    from . import auth
    scope = current_scope() if auth.current_role() == "user" else None
    if scope is not None:
        # 与通用技能同名会被通用层遮蔽（加载顺序通用优先）——直接拒绝防混淆
        if load_skill(name, None) is not None:
            return (f"创建失败：技能名「{name}」与已有通用技能同名（会被通用技能"
                    "遮蔽无法生效），请换一个名称。")
        base = _personal_dir(scope)
        shown = f"skills_personal/{scope}/{name}/SKILL.md"
        note = "（仅本人可见的专属技能，下轮对话即可使用）"
    else:
        base = _skills_dir()
        shown = f"skills/{name}/SKILL.md"
        note = "（下轮对话即可被 Agent 自动发现）"
    path = base / name / "SKILL.md"
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
    return f"技能「{name}」已{action}：{shown}\n{note}"


# ---- 技能包（zip）导入（v0.17.7）：目录型技能的标准传输形态 ----

# zip 解包安全上限：条目数 / 解压后总字节（防 zip 炸弹）；扩展名白名单（可执行二进制一律拒）
_SKILLPACK_MAX_ENTRIES = 200
_SKILLPACK_MAX_TOTAL = 20 * 1024 * 1024
_SKILLPACK_TEXT_EXTS = {".md", ".markdown", ".txt", ".py", ".json", ".yaml", ".yml",
                        ".csv", ".tsv", ".toml", ".ini", ".cfg", ".conf", ".rst",
                        ".log", ".sh", ".bat", ".xml", ".html", ".htm", ".js", ".ts"}


def import_skill_zip(data: bytes, overwrite: bool = False,
                     personal_scope: str | None = None) -> dict:
    """导入技能包 zip（目录型技能：SKILL.md + references/ 等多文件结构）。
    安全校验：条目路径防穿越（zip-slip）、条目数/解压后总量上限（zip 炸弹）、
    扩展名白名单。技能名取 SKILL.md frontmatter（与 create_skill 同一命名白名单）；
    已存在且未 overwrite 时抛 FileExistsError，覆盖时整目录替换。
    v0.17.7 评审：覆盖导入先解包临时目录完成全部校验与写盘再原子换名
    （失败原技能不受损）；返回值只含逻辑路径（不暴露服务器部署位置）。
    v0.17.9：personal_scope 非空时导入本人专属域 skills_personal/<域>/
    （user 角色上传，调用方传入），并拒绝与通用技能同名（会被遮蔽）。
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
        if personal_scope:
            # 专属导入：同名通用技能会遮蔽专属技能——直接拒绝防混淆（v0.17.9）
            if load_skill(name, None) is not None:
                raise ValueError(f"技能名「{name}」与已有通用技能同名（会被通用技能"
                                 "遮蔽无法生效），请换一个名称。")
            base_dir = _personal_dir(personal_scope)
            prefix = f"skills_personal/{personal_scope}"
        else:
            base_dir = _skills_dir()
            prefix = "skills"
        target = base_dir / name
        if target.exists() and not overwrite:
            raise FileExistsError(f"技能「{name}」已存在：{prefix}/{name}"
                                  "（覆盖导入将整体替换原内容）")
        # 覆盖导入原子化（评审 P2）：先解包临时目录（全部校验已通过），成功后
        # 「旧目录挪走 → 临时目录顶上 → 删旧」；中途任何失败都回滚，原技能不损坏。
        # Windows 上 rename 不能覆盖已存在目录，故三步换名。
        tmp = base_dir / f".{name}.import-{os.getpid()}"
        old = base_dir / f".{name}.old-{os.getpid()}"
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
        return {"name": name, "files": sorted(files), "path": f"{prefix}/{name}"}


def skill_index_prompt(scope: str | None = None) -> str:
    """生成注入系统提示词的技能索引文本（scope 非 None 时含本人专属技能）。"""
    all_skills = list_skills(scope)
    if not all_skills:
        return ""
    lines = ["", "你可以使用以下技能（当用户任务匹配技能描述时，"
             "先调用 load_skill 工具获取完整指令，再按指令执行）："]
    for s in all_skills:
        lines.append(f"- {s['name']}: {s['description']}")
    return "\n".join(lines)


def personal_index_prompt(scope: str | None) -> str:
    """本人专属技能索引段（动态注入，v0.17.9）。通用技能索引在图构建时静态
    注入（随 signature 重建刷新）；专属技能变更无需重建图——本函数在每轮
    模型调用时由动态 prompt 按当前请求身份实时扫描。scope 为空返回空串。
    跳过与通用层同名的技能（load_skill 通用层优先，索引与实际加载内容
    必须一致——手放的同名专属技能实际不可用，不进索引）。"""
    pdir = _personal_dir(scope)
    if pdir is None or not pdir.is_dir():
        return ""
    shadowed = {s["name"] for s in list_skills(None)}
    items = []
    for smd in sorted(pdir.glob("*/SKILL.md")):
        try:
            meta = _parse_skill_md(smd.read_text(encoding="utf-8", errors="replace"))
            name = meta["name"] or smd.parent.name
            if name in shadowed:
                print(f"[skills] 专属技能「{name}」与通用技能同名被遮蔽，"
                      f"未注入索引（域：{scope}）")
                continue
            items.append(f"- {name}: {meta['description'] or '（无描述）'}")
        except Exception:
            continue
    if not items:
        return ""
    return ("\n\n以下是你的专属技能（仅本人可见，使用方式同通用技能："
            "任务匹配时先调用 load_skill 获取完整指令）：\n" + "\n".join(items))


def _file_sig(p: Path) -> str:
    """单文件内容哈希；读失败返回 '?'（v0.17.34 AC1，对齐 custom_tools /
    packs 的 _file_sig 口径）：每轮对话开头都做签名检测，读失败上抛会
    打断本轮对话的 SSE 流；'?' 与任何成功哈希都不同，异常状态仍会触发
    一次重载，不被静默吞掉。
    有意不复用另两处实现（评审 C4 保留）：packs 模块级 import skills，
    反向导入即成循环导入；为一个 6 行助手做跨模块耦合不值当。"""
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return "?"


def signature() -> tuple:
    """技能文件签名（相对路径 + 内容哈希，v0.17.34 AC1），纳入 Agent 图
    重建检测：create_skill / 技能包导入后下一轮自动重绑生效，无需重启。
    v0.17.33 复验实测：此前 st_mtime_ns 口径可被「篡改内容 + 还原 mtime」
    完全绕过（不重建、不重验签），与 custom_tools / packs 同源问题本轮
    一并闭环；v0.17.7 评审 P2 的纳秒精度决策随之作废——内容变化必然
    引起哈希变化，时间戳不再参与签名。技能文件均为小文本，每轮对话一次
    检测的开销可忽略。
    注意：只统计通用层（skills/ + 领域包）——用户专属技能
    （skills_personal/<域>/）的索引按请求身份动态注入，变更即时生效，
    不参与图重建判定（v0.17.9）。
    v0.17.35 R4：包来源条目统一过 _skill_entry_ok，与索引/加载同一份
    「已验证接受的具体技能集合」——未声明或被拒兄弟不进技能层签名。
    v0.17.36 R8（复验报告）：签名是检测层职责本身，维持读盘、不返回内容
    ——快照只在重建时刷新、重建只由签名变化触发（可加载内容变化 ⇒ 必先
    反映为签名变化 ⇒ 重建重验 ⇒ 快照整体替换）。
    v0.17.36 R9：每来源目录 rglob/条目构建遇链接循环（OSError/RuntimeError，
    含 RecursionError）→ 哨兵条目参与比较，不上抛中断热检测；哨兵随异常
    变化、故障恢复后回归正常形态，差异触发重扫（与 packs.packs_signature
    同口径）。"""
    items = []
    for d, _src, allowed, root, _texts in _all_skill_dirs():
        if not d.is_dir():
            continue
        try:
            for p in sorted(d.rglob("SKILL.md")):
                if not _skill_entry_ok(p, allowed, root, _src):
                    continue
                items.append((p.relative_to(d).as_posix(), _file_sig(p)))
        except (OSError, RuntimeError) as e:
            # 评审 F2（P2）：哨兵键带来源标签（包来源 pack:<名>，内核用
            # 目录名）便于定位；补告警对齐 scan_packs 的可定位口径
            print(f"[skills] 来源「{_src or d.name}」技能签名遍历遇链接循环/IO"
                  f" 异常（{type(e).__name__}），哨兵条目参与比较待重建自愈")
            items.append((f"__error__:{_src or d.name}", f"{type(e).__name__}:{e}"))
    return tuple(items)
