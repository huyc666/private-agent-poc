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
- enable/disable 为运行时内存态（挂审批）；ENABLED_PACKS 环境变量决定重启后的默认集
- 工具名不做命名空间（LangChain 工具名不允许点号），重名冲突 → 跳过并告警
"""
import os
import re
from pathlib import Path

import yaml

from . import config, custom_tools, packsign, skills

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
KB_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # 知识库库名（pack.yaml 声明）

# 运行时被禁用的包名（内存态，重启后以 ENABLED_PACKS 为准）
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
        p = pack_dir / str(rel)
        if p.is_file() and p.suffix == ".py":
            if p.stem in tool_stems:
                print(f"[packs] {name}: 工具名重复，跳过: {rel}")
                continue
            tool_stems.add(p.stem)
            info["tools"].append(p)
        else:
            print(f"[packs] {name}: 工具文件不存在，跳过: {rel}")
    for rel in data.get("skills") or []:
        p = pack_dir / str(rel)
        if (p / "SKILL.md").is_file():
            info["skills"].append(p)
        else:
            print(f"[packs] {name}: 技能目录缺少 SKILL.md，跳过: {rel}")
    prompt_rel = data.get("prompt")
    if prompt_rel:
        p = pack_dir / str(prompt_rel)
        if p.is_file():
            info["prompt"] = str(p)
        else:
            print(f"[packs] {name}: 提示词文件不存在，跳过: {prompt_rel}")
    return info


def invalidate_scan_cache() -> None:
    """包目录被创建/删除/签名后调用，下次扫描重新读盘（v0.16.1）。"""
    global _scan_cache
    _scan_cache = None


def scan_packs() -> list[dict]:
    """扫描 PACKS_DIR 下全部包（含不可用/损坏的），按名称排序。
    结果带内存缓存：包目录的写入口（create/delete/sign）均已失效缓存，
    手工改盘需重启生效（与启动加载语义一致）。"""
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


def disable_pack(name: str) -> str:
    """运行时禁用（内存态）。返回结果文案。平台包不可禁用。"""
    packs = {p["name"]: p for p in scan_packs()}
    if name not in packs:
        return f"领域包「{name}」不存在"
    if packs[name]["platform"]:
        return (f"「{name}」是平台包（跨领域通用基础能力），运行时不可禁用；"
                "如确需下线，请管理员在部署层移除 packs/ 下对应目录。")
    if not _env_enabled(name):
        return f"领域包「{name}」已被 ENABLED_PACKS 环境变量排除，无需禁用"
    _disabled.add(name)
    return (f"领域包「{name}」已禁用（运行时生效）：其工具/技能/提示词已从 Agent 移除。\n"
            f"注意：此为内存态，重启后恢复；要持久禁用请在 .env 配置 ENABLED_PACKS 白名单。")


def enable_pack(name: str) -> str:
    """运行时启用（从内存禁用集合中移除）。"""
    packs = {p["name"]: p for p in scan_packs()}
    if name not in packs:
        return f"领域包「{name}」不存在"
    if packs[name]["platform"]:
        return f"「{name}」是平台包，始终处于启用状态，无需操作。"
    if packs[name]["status"] != "ok":
        return f"领域包「{name}」不可启用：{packs[name]['error']}"
    if not _env_enabled(name):
        return f"领域包「{name}」被 ENABLED_PACKS 环境变量排除，请先调整配置"
    _disabled.discard(name)
    return f"领域包「{name}」已启用，其工具/技能/提示词已挂载到 Agent。"


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
    """把全部启用包的 skills/ 子目录注册进全局技能索引。"""
    for pack in enabled_packs():
        for skill_dir in pack["skills"]:
            skills.register_extra_dir(skill_dir.parent, source=f"pack:{pack['name']}")


def packs_signature() -> tuple:
    """包目录签名（清单/工具/提示词的修改时间纳秒 + 运行时禁用集合），用于热重载检测。
    纳秒精度（st_mtime_ns，全项目评审问题6）：与 skills/custom_tools 签名约定
    一致——秒级 mtime 在快速连续更新时不变，会导致图不重建、包变更不生效。"""
    sig = []
    for pack in scan_packs():
        mtimes = []
        manifest = pack["dir"] / "pack.yaml"
        if manifest.is_file():
            mtimes.append(manifest.stat().st_mtime_ns)
        for p in pack["tools"]:
            mtimes.append(p.stat().st_mtime_ns)
        if pack["prompt"] and Path(pack["prompt"]).is_file():
            mtimes.append(Path(pack["prompt"]).stat().st_mtime_ns)
        sig.append((pack["name"], tuple(mtimes)))
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
        # 可信路径：内容已过人工审批，落盘即签名（强制模式下不签名无法加载）
        sig = packsign.sign_pack_files(pack_dir)
        signed_note = "\n已自动签名（可信路径：内容经审批后落盘）。" if sig else \
            "\n⚠️ 强制模式已开启但签名失败（未配置 PACK_SIGNING_KEY？），包将不可用。"
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
    sig = packsign.sign_pack_files(pack_dir)
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
