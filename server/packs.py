"""领域包（Domain Pack）注册中心：通用内核 + 可插拔领域能力。

包结构（packs/<name>/）：

    pack.yaml      清单：name/version/description/core_version/requires_env/tools/skills/prompt
    tools/*.py     领域工具（与 custom_tools 同一约定：文件名=工具名=函数名，
                   docstring 即描述；文件必须完全自包含，不得 import server 模块）
    skills/*/      领域技能（SKILL.md，并入全局技能索引）
    prompt.md      领域提示词片段（注入系统提示词）

生命周期：
- 启动/热重载时 scan_packs() 扫描校验，enabled_packs() 过滤后加载
- 包工具沿用 custom_tools 的构建器（沙箱/本地双模式，沙箱走 /run_tool 的 pack 字段）
- disable/enable 运行时立即生效并持久化到 STATE_DB（disabled_packs 表，PLAN 3.2
  Q2：重启不丢）；ENABLED_PACKS 环境变量保持部署层白名单语义（两者都通过才启用）
- 工具名不做命名空间（LangChain 工具名不允许点号），重名冲突 → 跳过并告警
"""
import hashlib
import os
import re
import sqlite3
from pathlib import Path, PurePosixPath

import yaml

from . import config, custom_tools, packsign, skills

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
KB_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # 知识库库名（pack.yaml 声明）

# 运行时被禁用的包名（启动时从 STATE_DB 加载的镜像；启停动作同步写库）
_disabled: set[str] = set()
# scan_packs 结果缓存（v0.16.1）：包目录的写入口只有 create/delete/sign，
# 三处都会失效缓存；手工改盘需重启生效（与启动加载语义一致）
_scan_cache: list[dict] | None = None

# 内核版本：领域包清单里的 core_version（如 ">=0.8.0"）与此比对，不满足则整包不加载
CORE_VERSION = "0.17.7"


def _version_tuple(v: str) -> tuple:
    """"0.9.0" -> (0, 9, 0)，无法解析的段按 0 处理。"""
    parts = []
    for seg in str(v).strip().split("."):
        digits = "".join(c for c in seg if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple((parts + [0, 0, 0])[:3])


def _check_core_version(spec: str) -> str | None:
    """校验 core_version 约束（支持 ">=x.y.z" 与精确版本；空 = 不约束）。
    返回 None 表示满足，否则返回错误文案。"""
    spec = spec.strip()
    if not spec:
        return None
    cur = _version_tuple(CORE_VERSION)
    if spec.startswith(">="):
        req = _version_tuple(spec[2:])
        if cur < req:
            return f"包要求内核版本 {spec}，当前内核 {CORE_VERSION}"
        return None
    req = _version_tuple(spec)
    if cur != req:
        return f"包要求内核版本 =={spec}，当前内核 {CORE_VERSION}"
    return None


def _packs_dir() -> Path:
    d = config.PACKS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_pack(pack_dir: Path) -> dict:
    """解析并校验单个包的 pack.yaml。返回 PackInfo dict。"""
    info = {
        "name": pack_dir.name,
        "version": "",
        "description": "",
        "status": "ok",          # ok | unavailable（缺环境变量）| error（清单损坏）
        "error": "",
        "tools": [],
        "skills": [],
        "prompt": "",
        "env": {},           # 包级环境变量默认值（pack.yaml 的 env 段）
        "platform": False,   # 平台包：跨领域通用能力，始终加载、运行时不可禁用
        "dir": pack_dir,
    }
    manifest = pack_dir / "pack.yaml"
    if not manifest.is_file():
        info["status"] = "error"
        info["error"] = "缺少 pack.yaml"
        return info
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except Exception as e:
        info["status"] = "error"
        info["error"] = f"pack.yaml 解析失败: {e}"
        return info
    if not isinstance(data, dict):
        info["status"] = "error"
        info["error"] = "pack.yaml 必须是键值结构"
        return info

    name = str(data.get("name", "")).strip()
    if not NAME_PATTERN.match(name):
        info["status"] = "error"
        info["error"] = f"非法包名「{name}」（只允许小写字母/数字/连字符）"
        return info
    core_err = _check_core_version(str(data.get("core_version", "")))
    if core_err:
        info["status"] = "unavailable"
        info["error"] = core_err
        return info
    if name != pack_dir.name:
        info["status"] = "error"
        info["error"] = f"包名「{name}」与目录名「{pack_dir.name}」不一致"
        return info
    info["name"] = name
    info["version"] = str(data.get("version", ""))
    info["description"] = str(data.get("description", ""))

    # 权限声明（信息性：展示在 list_packs 与启用审批卡片上，让审批人知情；
    # 执行层强制由沙箱/审批门负责，见 README「能力分层与信任模型」）
    perms = data.get("permissions") or []
    info["permissions"] = [str(p) for p in perms] if isinstance(perms, list) else []

    # 包级 RBAC（PLAN 1.2）：allowed_roles 角色白名单。执行侧在工具入口强制
    # （custom_tools.pack_role_denied），提示层过滤包片段（prompt_fragments）。
    # 未配置 = 不限制；fail-closed：显式空列表与非法角色名都判清单错误，
    # 防「角色名拼错 = 永不匹配 = 包静默不可用」。
    raw_roles = data.get("allowed_roles")
    if raw_roles is None:
        info["allowed_roles"] = []
    else:
        valid_roles = {"user", "approver", "admin"}
        if not isinstance(raw_roles, list) or not raw_roles:
            info["status"] = "error"
            info["error"] = "allowed_roles 必须是非空角色列表（不限制请删除该字段）"
            return info
        roles = [str(r).strip() for r in raw_roles]
        bad = sorted({r for r in roles if r not in valid_roles})
        if bad:
            info["status"] = "error"
            info["error"] = (f"allowed_roles 含非法角色 {bad}"
                             "（合法值: user / approver / admin）")
            return info
        info["allowed_roles"] = sorted(set(roles))

    # 知识库声明（PLAN 2.2）：本包检索工具覆盖的库名（kb/<库名>）。
    # 定位端点按「kb 名 → 声明包」归属校验，与 allowed_roles 同口径放行；
    # fail-closed：库名格式非法判清单错误，未声明 = 不可定位。
    raw_bases = data.get("knowledge_bases")
    if raw_bases is None:
        info["knowledge_bases"] = []
    else:
        if isinstance(raw_bases, str):
            raw_bases = [raw_bases]
        if (not isinstance(raw_bases, list) or not raw_bases
                or any(not isinstance(b, str) or not KB_NAME_PATTERN.match(b)
                       for b in raw_bases)):
            info["status"] = "error"
            info["error"] = ("knowledge_bases 必须是非空库名字符串列表"
                             "（小写字母/数字/连字符/下划线，≤64 字符）")
            return info
        info["knowledge_bases"] = sorted(set(raw_bases))

    # 部门白名单（PLAN 2.3）：与 allowed_roles 并列的第二维度，两维 AND。
    # 未配置 = 不限制部门（向后兼容，存量包零改动）；fail-closed：显式空列表、
    # 非列表、含空串/超长(>64)/控制字符 → 整包错误，防「部门名拼错或带空格 =
    # 永不匹配 = 包静默不可用」。命中比较在判定函数内归一化（strip + casefold）。
    raw_depts = data.get("allowed_departments")
    if raw_depts is None:
        info["allowed_departments"] = []
    else:
        if not isinstance(raw_depts, list) or not raw_depts:
            info["status"] = "error"
            info["error"] = ("allowed_departments 必须是非空部门名列表"
                             "（不限制请删除该字段）")
            return info
        bad = [str(d) for d in raw_depts
               if not isinstance(d, str) or not d.strip() or len(d.strip()) > 64
               or any(ord(ch) < 32 for ch in d)]
        if bad:
            info["status"] = "error"
            info["error"] = (f"allowed_departments 含非法部门名 {bad}"
                             "（须为非空字符串，≤64 字符，不含控制字符）")
            return info
        info["allowed_departments"] = sorted({d.strip() for d in raw_depts})

    # 可信签名校验（v0.13.0）：强制模式下未签名/签名无效/内容被篡改 → 整包不可用
    info["signature"] = str(data.get("signature", "") or "")
    info["trust"] = packsign.trust_status(pack_dir, info["signature"])
    if packsign.is_enforced() and info["trust"] != "signed":
        info["status"] = "unavailable"
        info["error"] = ("签名校验失败：包未签名或内容被篡改"
                         "（管理员可用 sign_pack 工具或 tools/sign_pack.py 重新签名）"
                         if info["trust"] != "invalid" else
                         "签名校验失败：签名与包内容不一致（疑似篡改，请审计后重新签名）")
        return info

    # 依赖环境变量检查：缺失则整个包不可用（例如需要内网数据服务的包）
    missing = [v for v in (data.get("requires_env") or []) if not os.environ.get(str(v))]
    if missing:
        info["status"] = "unavailable"
        info["error"] = f"缺少环境变量: {', '.join(missing)}"
        return info

    # Python 模块依赖检查（pip 依赖声明）：缺失则整包不可用。
    # fail-closed：私有化环境不在运行时自动 pip install，
    # 由部署侧先装到 Agent venv（local 模式）或打入沙箱镜像（sandbox 模式）。
    import importlib.util

    missing_mods = []
    for m in data.get("requires_modules") or []:
        try:
            if importlib.util.find_spec(str(m)) is None:
                missing_mods.append(str(m))
        except (ImportError, ValueError):
            missing_mods.append(str(m))
    if missing_mods:
        info["status"] = "unavailable"
        info["error"] = (f"缺少 Python 模块: {', '.join(missing_mods)}"
                         "（请先在 Agent 环境 pip install，或打入沙箱镜像后重启）")
        return info

    # 包级环境变量默认值（仅记录；挂载时仅注入外部未显式设置的 key）
    raw_env = data.get("env") or {}
    if isinstance(raw_env, dict):
        info["env"] = {str(k): str(v) for k, v in raw_env.items()}
    else:
        info["status"] = "error"
        info["error"] = "pack.yaml 的 env 段必须是键值结构"
        return info

    # 平台包标记：跨领域通用能力（治理规则见 is_enabled/disable_pack）
    info["platform"] = bool(data.get("platform"))

    # 能力清单（相对路径逐项校验存在性，缺失项跳过并告警；
    # 工具按 stem 去重——加载器以 stem 为工具名且包内重名本就静默跳过，
    # 清单重复条目不应进入跨包冲突判定造成误报，v0.17.27）
    tool_stems: set[str] = set()
    for rel in data.get("tools") or []:
        p = _resolve_in_pack(pack_dir, rel, "工具", name)
        if p is not None and p.is_file() and p.suffix == ".py":
            if p.stem in tool_stems:
                print(f"[packs] {name}: 工具名重复，跳过: {rel}")
                continue
            tool_stems.add(p.stem)
            info["tools"].append(p)
        else:
            print(f"[packs] {name}: 工具文件不存在，跳过: {rel}")
    skill_dirs: set[str] = set()                    # 已挂载技能目录（评审 E1 去重）
    for rel in data.get("skills") or []:
        p = _resolve_in_pack(pack_dir, rel, "技能", name)
        if p is None:
            continue
        leaf = (p / "SKILL.md").resolve()
        if not leaf.is_file():
            print(f"[packs] {name}: 技能目录缺少 SKILL.md，跳过: {rel}")
        elif pack_dir.resolve() not in leaf.parents:
            # v0.17.34 AC4（复验 R3）：目录包含校验通过，但叶子自身可以是
            # 指向包外的符号链接——is_file 会跟随链接，须对 resolve 后的
            # 真实路径再校验一次包含关系
            print(f"[packs] {name}: SKILL.md 指向包目录之外，跳过: {rel}")
        elif str(p) in skill_dirs:
            # 评审修复 E1：清单重复声明同一技能目录 → 去重（与工具分支的
            # tool_stems 去重对称，v0.17.27）；重复条目会重复进入签名载荷
            print(f"[packs] {name}: 技能目录重复声明，跳过: {rel}")
        else:
            skill_dirs.add(str(p))
            info["skills"].append(p)
    prompt_rel = data.get("prompt")
    if prompt_rel:
        p = _resolve_in_pack(pack_dir, prompt_rel, "提示词", name)
        if p is not None and p.is_file():
            info["prompt"] = str(p)
        else:
            print(f"[packs] {name}: 提示词文件不存在，跳过: {prompt_rel}")
    return info


def _resolve_in_pack(pack_dir: Path, rel, kind: str, name: str) -> Path | None:
    """包资源路径解析（v0.17.34 AC3 规范白名单 + v0.17.33 越界校验，
    均跳过并告警、fail-closed）：
    1. manifest 引用必须是规范相对路径——工具 tools/<文件名>.py（平铺
       一层，与沙箱执行路径 /packs/<pack>/tools/<name>.py 完全一致）、
       技能 skills/<一级目录>、提示词 prompt.md。复验实测此前允许包内
       任意路径：清单引用 code/x.py 会挂载但不在签名与变更检测载荷内
       （篡改后 status=ok 且 signed 双绿），声明路径与沙箱执行路径也可能
       脱钩；白名单一举闭合两处（可挂载资源全部落在 packsign 载荷的
       tools/** + skills/** + prompt.md 内）。
    2. resolve 后仍在包目录内（v0.17.33 AC2，复验 R2）：越界（../、
       绝对路径、符号链接出界）拒收——规范路径的文件名成分仍可能是
       指向包外的链接，两道检查各拦一类，缺一不可。
    """
    raw = str(rel)
    # 评审修复 C2：Windows 下反斜杠同为路径分隔符——"tools/a\b.py" 的
    # PurePosixPath.parts 是 ('tools','a\b.py') 会过白名单，物理 resolve
    # 却成嵌套文件 tools/a/b.py，声明与沙箱平铺执行 tools/b.py 再度错位
    # （实测仅审计错位、无未签名执行，仍须封堵以保平铺意图）。
    # manifest 路径的规范形态不含反斜杠，显式拒绝并单独告警。
    if "\\" in raw:
        print(f"[packs] {name}: {kind}路径非规范（不得包含反斜杠），跳过: {rel}")
        return None
    parts = PurePosixPath(raw).parts
    if kind == "工具":
        canonical = (len(parts) == 2 and parts[0] == "tools"
                     and parts[1].endswith(".py"))
    elif kind == "技能":
        canonical = (len(parts) == 2 and parts[0] == "skills"
                     and parts[1] not in ("", ".", ".."))
    else:
        canonical = raw == "prompt.md"
    if not canonical:
        print(f"[packs] {name}: {kind}路径非规范"
              f"（工具须为 tools/<文件名>.py 平铺 / 技能须为 skills/<一级目录>"
              f" / 提示词须为 prompt.md），跳过: {rel}")
        return None
    p = (pack_dir / raw).resolve()
    if pack_dir.resolve() not in p.parents:
        print(f"[packs] {name}: {kind}路径越界（不在包目录内），跳过: {rel}")
        return None
    return p


def invalidate_scan_cache() -> None:
    """包目录被创建/删除/签名后调用，下次扫描重新读盘（v0.16.1）。"""
    global _scan_cache
    _scan_cache = None


def scan_packs() -> list[dict]:
    """扫描 PACKS_DIR 下全部包（含不可用/损坏的），按名称排序。
    结果带内存缓存：包目录的写入口（create/delete/sign）均已失效缓存；
    手工改盘（含运行中篡改包文件）经 agent._rebuild_graph 开头强制失效
    缓存重扫并重新验签（SEC-4，v0.17.32）——热重载同样遵循强制验签语义。"""
    global _scan_cache
    if _scan_cache is None:
        result = []
        for sub in sorted(_packs_dir().iterdir()):
            if sub.is_dir() and not sub.name.startswith((".", "_")):
                result.append(_parse_pack(sub))
        _mark_shared_kb_tool_conflicts(result)
        _scan_cache = result
    return list(_scan_cache)


def _mark_shared_kb_tool_conflicts(result: list[dict]) -> None:
    """PLAN 2.4 校验层强约束（fail-closed）：多包声明**同一知识库**且存在**同名
    工具**时，相关包整包 status=error。共享库场景下重名检索工具会被加载器静默
    跳过（先加载者生效），导致其中一个包的检索能力悄然缺失——与其靠告警自觉，
    不如拒绝加载（spec 2.4 澄清 1 选 B；与 2.3 AC6 的配置错误 fail-closed 先例
    一致）。不共享库的同名工具不受影响（维持加载器既有「跳过 + 告警」）。
    判定覆盖全部扫描到的正常包（含被 ENABLED_PACKS 排除的）：运行时随时可能
    启用，扫描期把冲突暴露为显式错误更可预期。以包名作键（扫描期内唯一，
    评审修复：id() 作键对 dict 拷贝/重建易碎）。"""
    ok = [p for p in result if p["status"] == "ok"]
    kb_packs: dict[str, list[str]] = {}    # 库名 -> 声明它的正常包名
    by_name = {p["name"]: p for p in ok}
    for p in ok:
        for kb in p.get("knowledge_bases") or []:
            kb_packs.setdefault(kb, []).append(p["name"])
    shared = {kb for kb, names in kb_packs.items() if len(names) > 1}
    if not shared:
        return
    tool_owners: dict[str, list[str]] = {}  # 工具名 -> 声明它的正常包名
    for p in ok:
        for t in p["tools"]:
            tool_owners.setdefault(t.stem, []).append(p["name"])
    reasons: dict[str, str] = {}  # 包名 -> 错误说明（可能多条拼接）
    for kb in sorted(shared):
        names = set(kb_packs[kb])
        for tool, owners in tool_owners.items():
            group = sorted(set(owners) & names)
            if len(group) < 2:
                continue
            for name in group:
                others = "、".join(n for n in group if n != name)
                msg = (f"与包「{others}」共享知识库「{kb}」但存在同名工具"
                       f"「{tool}」——共享库各包必须使用不同的检索工具名"
                       "（重名工具会被静默跳过）")
                reasons[name] = f"{reasons[name]}；{msg}" if name in reasons else msg
    for name, msg in reasons.items():
        p = by_name[name]
        p["status"] = "error"
        p["error"] = msg


# 禁用名单持久化（PLAN 3.2 Q2）：STATE_DB 独立连接（audit._connect 同款，
# 不经过其他模块，防循环依赖）；读失败回退空名单 = 部署层 ENABLED_PACKS 语义
_DISABLED_SCHEMA = """
CREATE TABLE IF NOT EXISTS disabled_packs (
    name TEXT PRIMARY KEY
)
"""


def _disabled_db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.STATE_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_disabled_packs() -> None:
    """启动加载：从 STATE_DB 恢复禁用名单（重启不丢，Q2）。
    读失败保持空名单继续启动——fail-safe 方向：少禁用比多禁用安全，
    部署层仍可用 ENABLED_PACKS 兜底。"""
    try:
        conn = _disabled_db_connect()
        try:
            conn.execute(_DISABLED_SCHEMA)
            rows = conn.execute("SELECT name FROM disabled_packs").fetchall()
        finally:
            conn.close()
        _disabled.clear()
        _disabled.update(r[0] for r in rows)
    except Exception as e:
        print(f"[packs] 禁用名单加载失败，按空名单继续（{type(e).__name__}）")


def _persist_disabled(name: str, disabled: bool) -> bool:
    """启停动作同步写库，返回是否成功。

    写库失败由调用方回滚内存态并向调用者报错（PLAN 3.2 评审②）：
    「运行时假成功 + 重启后静默复活」比「当场失败可重试」更危险——
    回滚后 disable 保持启用（可用性优先）、enable 保持停用（安全优先），
    两个方向都与部署层 ENABLED_PACKS 兜底语义自洽。"""
    try:
        conn = _disabled_db_connect()
        try:
            conn.execute(_DISABLED_SCHEMA)
            if disabled:
                conn.execute(
                    "INSERT OR IGNORE INTO disabled_packs (name) VALUES (?)",
                    (name,))
            else:
                conn.execute("DELETE FROM disabled_packs WHERE name = ?",
                             (name,))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f"[packs] 禁用名单写库失败（{type(e).__name__}），操作将回滚")
        return False


def _env_enabled(name: str) -> bool:
    raw = config.ENABLED_PACKS.strip()
    if not raw:
        return True
    return name in {x.strip() for x in raw.split(",") if x.strip()}


def is_enabled(pack) -> bool:
    """平台包始终启用（不受 ENABLED_PACKS 白名单与运行时禁用影响）；
    领域包受白名单与运行时禁用集合控制。兼容传入包名或 PackInfo。"""
    if isinstance(pack, dict):
        if pack.get("platform"):
            return pack["status"] == "ok"
        name = pack["name"]
    else:
        name = pack
    return _env_enabled(name) and name not in _disabled


def enabled_packs() -> list[dict]:
    """全部已启用且状态正常的包；平台包排最前（先加载，重名时领域包让路）。"""
    packs = [p for p in scan_packs() if p["status"] == "ok" and is_enabled(p)]
    return sorted(packs, key=lambda p: (not p["platform"], p["name"]))


def list_packs() -> list[dict]:
    """管理视图：全部包及其状态（健康检查 / list_packs 工具用）。"""
    return [{
        "name": p["name"],
        "version": p["version"],
        "description": p["description"],
        "status": p["status"],
        "enabled": p["status"] == "ok" and is_enabled(p),
        "platform": p["platform"],
        "error": p["error"],
        "tools": [t.stem for t in p["tools"]],
        "skills": [s.name for s in p["skills"]],
        "env_keys": sorted(p.get("env", {}).keys()),
        "trust": p.get("trust", "unsigned"),
        "permissions": p.get("permissions", []),
        "allowed_roles": p.get("allowed_roles", []),
        "allowed_departments": p.get("allowed_departments", []),
        "knowledge_bases": p.get("knowledge_bases", []),
    } for p in scan_packs()]


def _toggle_pack(name: str, *, enable: bool) -> tuple[str, str]:
    """启停核心。返回 (状态, 文案)，状态 ∈ ok / not_found / refused / error——
    调用方（HTTP 端点）按结构化状态分流，不再靠文案子串猜测（PLAN 3.2 评审①：
    此前文案含「不存在」的包名会被误路由 404 且漏审计）。
    校验顺序：存在性 → 平台包 → status（仅启用侧）→ ENABLED_PACKS → 写库。"""
    info = {p["name"]: p for p in scan_packs()}.get(name)
    if info is None:
        return "not_found", f"领域包「{name}」不存在"
    if info["platform"]:
        if enable:
            return "refused", f"「{name}」是平台包，始终处于启用状态，无需操作。"
        return ("refused",
                f"「{name}」是平台包（跨领域通用基础能力），运行时不可禁用；"
                "如确需下线，请管理员在部署层移除 packs/ 下对应目录。")
    if enable:
        if info["status"] != "ok":
            return "refused", f"领域包「{name}」不可启用：{info['error']}"
        if not _env_enabled(name):
            return "refused", (f"领域包「{name}」被 ENABLED_PACKS 环境变量排除，"
                               "请先调整配置")
        _disabled.discard(name)
        if not _persist_disabled(name, False):
            _disabled.add(name)   # 写库失败回滚：保持停用（安全优先）
            return "error", (f"领域包「{name}」启用未生效（停用名单写库失败），"
                             "请稍后重试")
        return "ok", (f"领域包「{name}」已启用：其工具/技能/提示词已挂载到 Agent，"
                      "停用名单已同步清除。")
    if not _env_enabled(name):
        return "refused", f"领域包「{name}」已被 ENABLED_PACKS 环境变量排除，无需禁用"
    _disabled.add(name)
    if not _persist_disabled(name, True):
        _disabled.discard(name)   # 写库失败回滚：保持启用（可用性优先）
        return "error", (f"领域包「{name}」停用未生效（停用名单写库失败），"
                         "请稍后重试")
    return "ok", (f"领域包「{name}」已禁用（立即生效）：其工具/技能/提示词已从 Agent 移除，"
                  "停用名单已持久化（重启不丢）。")


def disable_pack(name: str) -> str:
    """运行时禁用（立即生效并持久化）。Agent 工具入口，返回结果文案。"""
    return _toggle_pack(name, enable=False)[1]


def enable_pack(name: str) -> str:
    """运行时启用。Agent 工具入口，返回结果文案。"""
    return _toggle_pack(name, enable=True)[1]


def effective_env(pack: dict) -> dict:
    """包级环境变量的生效值：外部（.env / 系统环境）显式设置优先，否则用包声明的默认值。"""
    return {k: os.environ.get(k, v) for k, v in pack.get("env", {}).items()}


def load_pack_tools() -> list:
    """加载全部启用包的工具，返回 StructuredTool 列表。重名冲突跳过并告警。
    包级 env 默认值在挂载时注入进程环境（setdefault，不覆盖外部显式设置），
    沙箱模式则随 /run_tool 请求显式透传给隔离子进程。
    v0.17.2：跨包同名 env 键冲突检测——进程 env 只有一份，setdefault 先到先得
    （平台包先加载，先到者永久生效且热重载不重算），冲突时 WARNING 点名两包与值。
    检测只告警，不改变运行时语义：工具代码仍按原名读 env。"""
    tools = []
    seen: set[str] = set()
    external_keys = set(os.environ)  # 加载开始前的键 = 外部（.env/系统）显式设置，
    # 用于区分「外部覆盖」与「包间先到先得」，避免告警文案误判
    env_owners: dict[str, tuple[str, str]] = {}  # env 键 -> (先声明它的包, 值) = 生效方
    for pack in enabled_packs():
        # 跨包同名 env 键冲突检测（在任何注入发生前点名，先到先得）
        for k, v in pack.get("env", {}).items():
            prev = env_owners.get(k)
            if prev is not None and prev[0] != pack["name"]:
                if k in external_keys:
                    hint = "当前进程 env 已由外部显式设置，两包默认值均被覆盖"
                else:
                    hint = f"先加载的「{prev[0]}」生效，「{pack['name']}」的注入被忽略"
                print(f"[packs] WARNING: env 键「{k}」跨包冲突——包「{prev[0]}」（值 "
                      f"{prev[1]}）与「{pack['name']}」（值 {v}）均声明默认值；"
                      f"进程 env 只有一份，{hint}（setdefault 先到先得，热重载不重算）。"
                      "如两包需要不同值请改用不同变量名")
            else:
                env_owners[k] = (pack["name"], v)
        eff_env = effective_env(pack)
        for k, v in eff_env.items():
            os.environ.setdefault(k, v)  # local 模式生效路径；进程生命周期内有效
        loaded = 0
        for path in pack["tools"]:
            name = path.stem
            if name in seen:
                print(f"[packs] 工具名冲突「{name}」（包 {pack['name']}），跳过")
                continue
            if not custom_tools.NAME_PATTERN.match(name):
                print(f"[packs] 非法工具名「{name}」（包 {pack['name']}），跳过")
                continue
            tool = custom_tools.build_tool(path, pack=pack["name"], env=eff_env,
                                           allowed_roles=pack.get("allowed_roles"),
                                           allowed_departments=pack.get("allowed_departments"))
            if tool is None:
                print(f"[packs] {pack['name']}/{name} 构建失败，跳过")
                continue
            seen.add(name)
            tools.append(tool)
            loaded += 1
        if loaded:
            print(f"[packs] 包「{pack['name']}」已挂载 {loaded} 个工具"
                  + (f"（注入 env: {', '.join(eff_env)}）" if eff_env else ""))
    return tools


def prompt_fragments(role: str | None = None,
                     department: str | None = None) -> str:
    """拼接全部启用包的 prompt.md 片段（注入系统提示词）。
    v0.17.18 包级 RBAC（PLAN 1.2）：role 给定时按 allowed_roles 过滤（提示层
    ——模型不看到受限包的用法说明；真正拦截在工具执行入口，防提示词注入绕过）。
    v0.17.26 部门维度（PLAN 2.3）：role 给定时同步按 allowed_departments 过滤
    （department 未显式给出则取当前对话上下文部门）。
    role=None 不过滤（管理/兼容口径）。"""
    dept = department
    if role and dept is None:
        from . import auth   # 函数内导入：避免模块级循环依赖
        dept = auth.current_department()
    parts = []
    for pack in enabled_packs():
        if pack["prompt"]:
            if role and custom_tools.pack_scope_denied(
                    pack["name"], pack.get("allowed_roles") or [],
                    pack.get("allowed_departments") or [],
                    role=role, department=dept):
                continue
            try:
                text = Path(pack["prompt"]).read_text(
                    encoding="utf-8", errors="replace").strip()
                if text:
                    parts.append(text)
            except Exception as e:
                print(f"[packs] {pack['name']} 提示词读取失败: {e}")
    return "\n\n".join(parts)


def register_pack_skills() -> None:
    """把全部启用包的技能目录注册进全局技能索引。
    v0.17.35 R4（复验报告）：注册父目录时附「已验证接受的具体技能目录名
    集合」（_parse_pack 已完成规范路径/resolve 包含/叶子包含/去重四重
    校验的 info["skills"]）与包根目录——索引/加载/签名三入口统一按这份
    集合过滤（skills._skill_entry_ok），未声明或被解析拒绝的兄弟技能
    不再经父目录进入任何入口。
    评审 C5：按真实父目录分组注册——解析期 resolve 可因包内链接使声明
    技能的父目录偏离 <pack>/skills（如 skills/x → ../tools），原
    skills[0].parent 的「共同父目录」前提被打破时会静默只注册第一个
    技能的父目录、其余合法技能陪葬（三口不一致）；逐父目录注册后畸形
    者自身仍被 allowed 过滤拒收（fail-closed）。正常包全部技能共居
    同一父目录，行为不变。"""
    for pack in enabled_packs():
        if not pack["skills"]:
            continue
        by_parent: dict[Path, set[str]] = {}
        for s in pack["skills"]:
            by_parent.setdefault(s.parent, set()).add(s.name)
        for parent, names in by_parent.items():
            skills.register_extra_dir(parent, source=f"pack:{pack['name']}",
                                      allowed=names, root=pack["dir"])


def _file_sig(p: Path) -> str:
    """单文件内容哈希签名；读失败（权限/占用）返回 '?' 参与签名——异常
    状态的变化同样应触发重扫（检测层 fail-open 等于没有检测）。"""
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return "?"


def _rel_key(p: Path, root: Path) -> str:
    """签名条目键（包目录规范化相对路径，与 packsign HMAC 载荷同键）。
    relative_to 兜底（评审 C2）：扫描缓存填充后包目录（或其祖先）若被
    换成指向别处的链接，缓存条目路径与现算包根前缀分叉 → ValueError
    上抛会炸掉每轮对话开头的热检测且缓存永不失效——退化为绝对路径键
    继续参与比较，键形态变化即触发重扫重验，缓存经重建自愈。"""
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def packs_signature() -> tuple:
    """包目录签名（清单/工具/技能/提示词 + 运行时禁用集合），用于热重载检测。
    v0.17.35 R6（复验报告）：条目统一「规范化相对路径 + 内容哈希」二元组，
    键基准 = 包目录（pack.yaml / tools/<文件名>.py / skills/<目录>/… /
    prompt.md），与 packsign HMAC 载荷同键——附属文件纯重命名（内容不变，
    如 references/a.md→b.md）也触发热检测，跨目录交换位置同样可检，消除
    「缓存认为未变、强制重扫已 invalid」的信任状态不一致。读失败 '?'
    兜底保留（_file_sig）：异常状态的变化同样应触发重扫。
    v0.17.34 AC2 载荷补挂载技能目录全量文件、v0.17.33 mtime→内容哈希的
    决策沿用（双层互备口径不变）。"""
    sig = []
    for pack in scan_packs():
        hs = []
        root = pack["dir"].resolve()
        manifest = pack["dir"] / "pack.yaml"
        if manifest.is_file():
            hs.append(("pack.yaml", _file_sig(manifest)))
        for p in pack["tools"]:
            hs.append((_rel_key(p, root), _file_sig(p)))
        for sdir in pack["skills"]:
            for p in sorted(sdir.rglob("*")):
                if p.is_file():
                    hs.append((_rel_key(p, root), _file_sig(p)))
        if pack["prompt"] and Path(pack["prompt"]).is_file():
            hs.append(("prompt.md", _file_sig(Path(pack["prompt"]))))
        sig.append((pack["name"], tuple(hs)))
    return (tuple(sig), tuple(sorted(_disabled)))


# ---------------------------------------------------------------- 包脚手架（对话式创建/删除，v0.9.0）

def create_pack(name: str, description: str, prompt: str = "",
                tool_name: str = "", tool_code: str = "",
                skill_name: str = "", skill_description: str = "",
                skill_instructions: str = "",
                env: dict | None = None,
                requires_env: list | None = None,
                requires_modules: list | None = None,
                permissions: list | None = None,
                platform: bool = False) -> str:
    """创建领域包骨架（调用方负责先走审批）。任选一个初始工具/技能/提示词，
    以及包级配置（env 默认值 / requires_env / requires_modules）。
    platform=True 创建平台包（跨领域通用能力，始终启用、运行时不可禁用）。
    落盘即被热重载签名捕获，下一轮对话起挂载。"""
    name = name.strip().lower()
    if not NAME_PATTERN.match(name):
        return "创建失败：包名只允许小写字母、数字、连字符（如 weather-ops）"
    pack_dir = _packs_dir() / name
    if pack_dir.exists():
        # 只呈现逻辑路径（相对工作区），不暴露服务器部署布局（安全修复）
        return f"领域包「{name}」已存在：packs/{name}"
    if not description.strip():
        return "创建失败：description 不能为空（展示给模型判断何时使用）"

    tool_name = tool_name.strip()
    if tool_name:
        if "/" in tool_name or "\\" in tool_name:
            return "创建失败：工具名不能包含路径分隔符"
        err = custom_tools.validate_tool_code(tool_name, tool_code)
        if err:
            return f"创建失败（工具未通过校验）：{err}"
    skill_name = skill_name.strip().lower()
    if skill_name and not skills.NAME_PATTERN.match(skill_name):
        return "创建失败：技能名只允许小写字母、数字、连字符（如 weekly-report）"

    # 清单（yaml.safe_dump 自动转义中文/特殊字符）
    manifest: dict = {
        "name": name,
        "version": "0.1.0",
        "description": description.strip(),
        "core_version": f">={CORE_VERSION}",
        "requires_env": [str(v) for v in (requires_env or [])],
    }
    if env:
        bad_keys = [k for k in env if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k))]
        if bad_keys:
            return f"创建失败：env 包含非法变量名 {bad_keys}"
        manifest["env"] = {str(k): str(v) for k, v in env.items()}
    if requires_modules:
        manifest["requires_modules"] = [str(m) for m in requires_modules]
    if permissions:
        manifest["permissions"] = [str(p) for p in permissions]
    if platform:
        manifest["platform"] = True
    if tool_name:
        manifest["tools"] = [f"tools/{tool_name}.py"]
    if skill_name:
        manifest["skills"] = [f"skills/{skill_name}"]
    if prompt.strip():
        manifest["prompt"] = "prompt.md"

    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    made = []
    if prompt.strip():
        (pack_dir / "prompt.md").write_text(prompt.strip() + "\n", encoding="utf-8")
        made.append("提示词 prompt.md")
    if tool_name:
        tdir = pack_dir / "tools"
        tdir.mkdir(exist_ok=True)
        (tdir / f"{tool_name}.py").write_text(
            tool_code if tool_code.endswith("\n") else tool_code + "\n",
            encoding="utf-8")
        made.append(f"工具 {tool_name}")
    if skill_name:
        sdir = pack_dir / "skills" / skill_name
        sdir.mkdir(parents=True, exist_ok=True)
        content = (f"---\nname: {skill_name}\n"
                   f"description: {(skill_description or skill_name).strip()}\n---\n\n"
                   f"{(skill_instructions or '（待补充指令）').strip()}\n")
        (sdir / "SKILL.md").write_text(content, encoding="utf-8")
        made.append(f"技能 {skill_name}")

    caps = "、".join(made) if made else "（空骨架，可后续往目录里加 tools/skills/prompt）"
    invalidate_scan_cache()
    signed_note = ""
    if packsign.is_enforced():
        # 可信路径：内容已过人工审批，落盘即签名（强制模式下不签名无法加载）。
        # v0.17.35 R5：签名生成不静默跳过不可读文件（会签出覆盖不完整的
        # 签名）——资源读取/编码异常明确报错，包未获签名。评审 C6：捕获
        # 范围补 UnicodeEncodeError（Linux surrogateescape 文件名）。
        # 评审 E2：is_enforced 与 compute 同读 PACK_SIGNING_KEY，「已强制
        # 但未配密钥」分支不可达，三态清理为两态（失败统一走明确文案）。
        try:
            sig = packsign.sign_pack_files(pack_dir)
        except (OSError, UnicodeEncodeError) as e:
            print(f"[packs] {name}: 自动签名失败——包资源读取异常"
                  f"（{type(e).__name__}），未写入签名")
            sig = ""
        signed_note = ("\n已自动签名（可信路径：内容经审批后落盘）。" if sig else
                       "\n⚠️ 自动签名失败：包资源读取异常（文件被占用/权限"
                       "不足？），包将不可用；请排查后用 sign_pack 重新签名。")
    return (f"领域包「{name}」已创建：packs/{name}\n包含：{caps}{signed_note}\n"
            "（无需重启，下一轮对话起挂载；可用 list_packs 查看，对话中说"
            "「禁用/启用该包」可即时切换能力边界）")


def sign_pack(name: str) -> str:
    """为手工放入的包签名（调用方负责先走审批）。仅在配置 PACK_SIGNING_KEY 后可用。
    签名使包进入可信集；签名前管理员应人工审计包内代码。"""
    if not packsign.is_enforced():
        return ("未配置 PACK_SIGNING_KEY（开放模式），所有包本就可加载，无需签名。\n"
                "要启用强制信任模式，请在 .env 配置 PACK_SIGNING_KEY 后重启。")
    name = name.strip().lower()
    pack_dir = _packs_dir() / name
    if not (pack_dir / "pack.yaml").is_file():
        return f"领域包「{name}」不存在或缺少 pack.yaml"
    try:
        sig = packsign.sign_pack_files(pack_dir)
    except (OSError, UnicodeEncodeError) as e:
        # v0.17.35 R5：签名生成遇资源读取/编码异常（评审 C6 补
        # UnicodeEncodeError：Linux surrogateescape 文件名）→ 明确报错且
        # 不写入任何签名（sign_pack_files 只在 compute 成功后才写
        # manifest，天然无残缺签名）
        print(f"[packs] 「{name}」签名生成失败：包资源读取异常"
              f"（{type(e).__name__}），未写入签名")
        return ("签名失败：包资源读取异常（文件被占用/权限不足？），"
                "请排查后重试；未写入任何签名。")
    if not sig:
        return "签名失败：PACK_SIGNING_KEY 不可用"
    invalidate_scan_cache()
    return (f"领域包「{name}」已签名：{sig[:40]}…\n"
            "（签名随内容变化失效：今后任何修改都需重新签名，篡改即自动失效）")


def delete_pack(name: str) -> str:
    """删除整个领域包目录（调用方负责先走审批）。"""
    import shutil

    name = name.strip().lower()
    if not NAME_PATTERN.match(name):
        return "删除失败：非法包名"
    pack_dir = _packs_dir() / name
    if not pack_dir.is_dir():
        return f"领域包「{name}」不存在"
    _disabled.discard(name)
    shutil.rmtree(pack_dir)
    invalidate_scan_cache()
    return (f"领域包「{name}」已删除：packs/{name}\n"
            "（无需重启，下一轮对话起其工具/技能/提示词全部消失）")
