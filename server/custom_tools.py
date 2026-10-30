"""自定义工具注册中心：custom_tools/ 目录下的 .py 文件即工具，创建后免重启生效。

约定（与业界 function-calling 工具格式对齐）：
- 文件名即工具名：custom_tools/dice_roll.py → 工具 dice_roll
- 文件内必须定义与文件名同名的函数，函数的 docstring 即工具描述（展示给模型）
- 参数需带类型注解（StructuredTool 据此生成 JSON Schema），返回 str
- 不支持 *args / **kwargs（无法生成稳定的参数 Schema）

执行隔离（CUSTOM_TOOLS_EXEC=auto|sandbox|local，默认 auto）：
- sandbox：SANDBOX_URL 可达时，只通过 ast 静态提取签名（不在主进程执行任何
  自定义代码），实际执行走沙箱服务的 /run_tool 端点（隔离子进程 + 容器限额）
- local：沙箱不可达时回退到主进程内执行（POC 信任已通过审批的代码）
- 显式设置 sandbox 而沙箱不可达时 fail-closed：不加载任何自定义工具

安全策略：
- 创建/删除都要先过 ast 静态校验；通用工具再经人工审批（interrupt，fail-closed）；
  user 角色创建的专属工具仅落本人专属域（custom_tools_personal/<域>/，v0.17.12，
  影响面限于本人会话，自助落盘免审批，与技能层同原则）
- 执行时归属校验：专属工具只对本域会话（及全局视图角色）可用，防提示词注入
  绕过清单直呼他人专属工具
"""
import ast
import importlib.util
import inspect
import json
import os
import re
import urllib.request
from pathlib import Path

from . import config

NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,49}$")
MAX_CODE_BYTES = 20_000


def _dir() -> Path:
    d = config.CUSTOM_TOOLS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------- 专属域（v0.17.12）

def current_scope() -> str | None:
    """当前对话上下文的专属工具域。认证模式按 usage.current_user 分域
    （与 skills.current_scope / 文件域 config.user_scope 同规则，v0.17.32
    SEC-2 哈希防碰撞）；开放模式/匿名 → None（无专属域概念，行为同旧版全局层）。"""
    from . import auth, usage   # 函数内导入：避免模块级循环依赖
    u = usage.current_user.get() or ""
    if not auth.enabled() or not u or u == "anonymous":
        return None
    return config.user_scope(u)


def _personal_root() -> Path:
    d = config.PERSONAL_CUSTOM_TOOLS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _personal_dir(scope: str | None) -> Path | None:
    """scope 对应的专属工具目录 custom_tools_personal/<域>/；scope 为空返回 None。"""
    if not scope:
        return None
    return _personal_root() / scope


def personal_scopes() -> list[str]:
    """全部专属域名清单（目录名排序，供全局视图角色遍历）。"""
    root = _personal_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def _global_tool_names() -> set[str]:
    """通用层已有工具名（遮蔽判定：同名专属工具被通用层遮蔽不生效）。"""
    return {p.stem for p in _dir().glob("*.py") if NAME_PATTERN.match(p.stem)}


def personal_tool_path(name: str, scope: str) -> Path | None:
    """本人专属域内某工具的路径；不存在返回 None（调用方用于分流判断）。"""
    if not NAME_PATTERN.match(name):
        return None
    pdir = _personal_dir(scope)
    if pdir is None:
        return None
    path = pdir / f"{name}.py"
    return path if path.is_file() else None


def list_personal_tools(scope: str) -> list[dict]:
    """单个专属域的工具索引 [{name, description}]（user 角色本人视图）。
    与通用层同名的专属工具被遮蔽（实际不可用），跳过并告警。"""
    result: list[dict] = []
    pdir = _personal_dir(scope)
    if pdir is None or not pdir.is_dir():
        return result
    shadowed = _global_tool_names()
    for p in sorted(pdir.glob("*.py")):
        if not NAME_PATTERN.match(p.stem):
            continue
        if p.stem in shadowed:
            print(f"[custom-tools] 专属工具「{p.stem}」与通用工具同名被遮蔽，"
                  f"跳过（域：{scope}）")
            continue
        doc = _extract_doc(p)
        result.append({
            "name": p.stem,
            "description": doc if doc is not None else "⚠️ 文件语法错误或缺少同名函数",
        })
    return result


def list_personal_all() -> list[dict]:
    """全部专属域工具索引 [{name, description, scope}]（approver/admin 全局视图，
    带 scope 供「域/工具名」消歧）。与通用层同名的被遮蔽工具同样跳过。"""
    result: list[dict] = []
    shadowed = _global_tool_names()
    for scope in personal_scopes():
        pdir = _personal_dir(scope)
        if pdir is None or not pdir.is_dir():
            continue
        for p in sorted(pdir.glob("*.py")):
            if not NAME_PATTERN.match(p.stem) or p.stem in shadowed:
                continue
            doc = _extract_doc(p)
            result.append({
                "name": p.stem,
                "scope": scope,
                "description": doc if doc is not None else "⚠️ 文件语法错误或缺少同名函数",
            })
    return result


def signature() -> tuple:
    """目录内容签名（文件名 + 修改时间纳秒），用于热重载变更检测。
    纳秒精度（st_mtime_ns）：秒级 mtime 在快速连续更新时不变，会导致图不重建、
    新工具不生效（全项目评审问题6，与 skills.signature 约定对齐）。"""
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in _dir().glob("*.py")))


def _extract_doc(path: Path) -> str | None:
    """用 ast 静态读取与文件同名函数的 docstring（不执行代码，安全）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == path.stem:
            doc = ast.get_docstring(node)
            return (doc or "（无描述）").strip().splitlines()[0][:120]
    return None


def list_custom_tools() -> list[dict]:
    """返回全部自定义工具索引 [{name, description}]，语法损坏的文件也会列出并标注。"""
    result = []
    for p in sorted(_dir().glob("*.py")):
        doc = _extract_doc(p)
        result.append({
            "name": p.stem,
            "description": doc if doc is not None else "⚠️ 文件语法错误或缺少同名函数",
        })
    return result


def validate_tool_code(name: str, code: str) -> str | None:
    """静态校验工具代码。返回 None 表示通过，否则返回错误原因。"""
    if not NAME_PATTERN.match(name):
        return "工具名只允许小写字母、数字、下划线，且以字母/下划线开头（如 dice_roll）"
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return f"代码超过 {MAX_CODE_BYTES // 1000}KB 上限"
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Python 语法错误: {e}"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            if not ast.get_docstring(node):
                return f"函数 {name}() 缺少 docstring（docstring 会作为工具描述展示给模型）"
            if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
                return f"函数 {name}() 不支持 *args/**kwargs/仅关键字参数（无法生成稳定的参数 Schema）"
            return None
    return f"代码中必须定义与工具同名的顶层函数 {name}()"


def write_custom_tool(name: str, code: str, scope: str | None = None) -> str:
    """通过校验后落盘（调用方负责先走审批；scope 非 None 时为 user 角色本人的
    专属域自助写入，免审批）。专属域与通用层同名会被遮蔽——直接拒绝防混淆
    （与技能层 create_skill 同规则）。"""
    err = validate_tool_code(name, code)
    if err:
        return f"创建失败：{err}"
    if scope:
        if name in _global_tool_names():
            return (f"创建失败：工具名「{name}」与已有通用自定义工具同名（会被通用"
                    "工具遮蔽无法生效），请换一个名称。")
        base = _personal_dir(scope)
        shown = f"custom_tools_personal/{scope}/{name}.py"
        note = "（仅本人可见可用的专属工具，下一轮对话起即可经 run_custom_tool 调用）"
    else:
        base = _dir()
        shown = f"custom_tools/{name}.py"
        note = "（无需重启，下一轮对话起即可调用）"
    path = base / f"{name}.py"
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")
    action = "已更新" if existed else "已创建"
    # 只呈现逻辑路径（相对工作区），不暴露服务器部署布局（安全修复）
    return f"自定义工具「{name}」{action}：{shown}\n{note}"


def delete_custom_tool_file(name: str, scope: str | None = None) -> str:
    """删除工具文件（调用方负责先走审批；scope 非 None 时删本人专属域工具，
    user 自助免审批）。"""
    if not NAME_PATTERN.match(name):
        return "删除失败：非法工具名"
    base = _personal_dir(scope) if scope else _dir()
    path = base / f"{name}.py"
    if not path.is_file():
        where = (f"专属域 custom_tools_personal/{scope}/" if scope
                 else "custom_tools/")
        return f"自定义工具「{name}」不存在（{where}目录下）"
    path.unlink()
    # 只呈现逻辑路径（相对工作区），不暴露服务器部署布局（安全修复）
    shown = (f"custom_tools_personal/{scope}/{name}.py" if scope
             else f"custom_tools/{name}.py")
    return (f"自定义工具「{name}」已删除：{shown}\n"
            "（无需重启，下一轮对话起不再可调用）")


_TYPE_MAP = {"str": str, "int": int, "float": float, "bool": bool}


def _ann_to_type(node) -> type:
    """ast 注解节点 → Python 类型，不认识的注解一律按 str 处理。"""
    if isinstance(node, ast.Name):
        return _TYPE_MAP.get(node.id, str)
    return str


def _extract_signature(path: Path) -> dict | None:
    """ast 静态提取工具签名与 docstring（不执行代码，沙箱模式下主进程零执行）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == path.stem:
            params = []
            defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + list(node.args.defaults)
            for arg, default in zip(node.args.args, defaults):
                params.append({
                    "name": arg.arg,
                    "type": _ann_to_type(arg.annotation) if arg.annotation else str,
                    "required": default is None,
                    "default": default.value if isinstance(default, ast.Constant) else None,
                })
            return {"name": path.stem,
                    "description": (ast.get_docstring(node) or path.stem).strip(),
                    "params": params, "is_async": isinstance(node, ast.AsyncFunctionDef)}
    return None


_up_cache = {"t": 0.0, "v": False}


def _sandbox_up() -> bool:
    """沙箱可达性探测（5 秒 TTL 缓存，避免每轮对话都探测一次）。"""
    import time
    now = time.time()
    if now - _up_cache["t"] < 5:
        return _up_cache["v"]
    _up_cache["t"] = now
    if not config.SANDBOX_URL:
        _up_cache["v"] = False
        return False
    try:
        with urllib.request.urlopen(config.SANDBOX_URL.rstrip("/") + "/health", timeout=2) as resp:
            _up_cache["v"] = resp.status == 200
    except Exception:
        _up_cache["v"] = False
    return _up_cache["v"]


_auto_warn_cache = {"t": 0.0}


def exec_mode() -> str:
    """解析执行模式（SEC-3，v0.17.32）：显式配置 CUSTOM_TOOLS_EXEC=
    auto/local/sandbox 优先；未显式配置时按运行模式联动默认——Mock 模式
    （开发）= auto（沙箱优先，不可达回退本地），真实 LLM 模式 = sandbox
    （沙箱不可达 fail-closed：生产不得静默把工具代码放进主进程执行）。
    auto 回退本地不再静默：显著 WARNING + 60s 限频防刷屏。"""
    mode = config.CUSTOM_TOOLS_EXEC or ("auto" if config.MOCK_LLM else "sandbox")
    up = _sandbox_up()
    if mode == "local":
        return "local"
    if mode == "sandbox":
        return "sandbox" if up else "sandbox-down"
    if not up:
        import time
        now = time.time()
        if now - _auto_warn_cache["t"] >= 60:
            _auto_warn_cache["t"] = now
            print("[custom-tools] WARNING: CUSTOM_TOOLS_EXEC=auto 且沙箱不可达，"
                  "工具代码将在主进程执行（exec_module，拥有服务全部权限）——"
                  "仅限受控开发环境；生产请配置 SANDBOX_URL，或显式设置"
                  "CUSTOM_TOOLS_EXEC=sandbox 以 fail-closed。")
        return "local"
    return "sandbox"


def _sandbox_run_tool(name: str, args: dict, pack: str = "", env: dict | None = None,
                      code: str = "") -> str:
    """调用沙箱 /run_tool。code 非空时直传工具源码（专属工具场景，v0.17.12：
    沙箱侧只读挂载通用 custom_tools/，本人专属工具文件不在其中；沙箱按 /run
    同等隔离执行，不新增攻击面）。"""
    url = config.SANDBOX_URL.rstrip("/") + "/run_tool"
    body = {"name": name, "args": args, "pack": pack, "env": env or {}}
    if code:
        body["code"] = code
    data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.SANDBOX_AUTH_TOKEN:
        headers["X-Sandbox-Token"] = config.SANDBOX_AUTH_TOKEN
    req = urllib.request.Request(url, data=data_bytes, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=config.SANDBOX_TIMEOUT + 5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # 不向模型/用户回显原始异常文本（P1-3 约定），细节仅进服务端日志
        print(f"[custom-tools] 沙箱调用失败: {e}")
        return f"沙箱调用失败（{type(e).__name__}），请检查沙箱服务状态"
    if data.get("ok"):
        return str(data.get("result", ""))
    return f"沙箱执行失败: {data.get('error', '未知错误')}"


def _make_sandbox_tool(meta: dict, pack: str = "", env: dict | None = None,
                       code: str = ""):
    """用 ast 提取的签名动态生成 args_schema，实际执行代理到沙箱服务。
    code 非空时随请求直传源码（专属工具场景）。"""
    from langchain_core.tools import StructuredTool
    from pydantic import create_model

    fields = {}
    for p in meta["params"]:
        fields[p["name"]] = (p["type"], ...) if p["required"] else (p["type"], p["default"])
    schema = create_model(f"{meta['name'].title().replace('_', '')}Args", **fields)
    tool_name = meta["name"]

    def _proxy(**kwargs):
        return _sandbox_run_tool(tool_name, kwargs, pack, env, code)

    return StructuredTool(name=tool_name, description=meta["description"],
                          args_schema=schema, func=_proxy)


def make_local_tool(path: Path):
    """local 模式：主进程内导入单个工具文件并包装为 StructuredTool。
    失败返回 None（调用方打印告警跳过）。领域包加载复用本函数。"""
    from langchain_core.tools import StructuredTool

    name = path.stem
    try:
        spec = importlib.util.spec_from_file_location(f"custom_tool_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, name, None)
        if not callable(fn):
            return None
        if inspect.iscoroutinefunction(fn):
            return StructuredTool.from_function(
                coroutine=fn, name=name, description=fn.__doc__ or name)
        return StructuredTool.from_function(
            func=fn, name=name, description=fn.__doc__ or name)
    except Exception as e:
        print(f"[custom-tools] 加载 {name} 失败（跳过，不影响其他工具）: {e}")
        return None


def pack_role_denied(pack: str, allowed_roles: list | None,
                     role: str | None = None) -> str | None:
    """领域包 allowed_roles 执行侧校验（PLAN 1.2 包级 RBAC）。返回拒绝文案或
    None（放行）。语义：未配置（空列表）不限制；admin 全局视图放行（开放模式/
    匿名按 auth.current_role 契约同为 admin，行为等同不限制）；其余角色严格
    白名单。执行入口校验是最终权限边界——提示层过滤（包片段）只防误触，
    防不了提示词注入，二者缺一不可。"""
    if not allowed_roles:
        return None
    if role is None:
        from . import auth
        role = auth.current_role()
    if role == "admin" or role in allowed_roles:
        return None
    return (f"领域包「{pack}」仅限 {'、'.join(allowed_roles)} 角色使用，"
            "当前身份无权限调用")


def _norm_dept(value: str | None) -> str:
    """部门值归一化（strip + casefold，PLAN 2.3）：防空格/大小写差异造成
    「部门名看似相同却不匹配」的静默失效。"""
    return (value or "").strip().casefold()


def pack_scope_denied(pack: str, allowed_roles: list | None,
                      allowed_departments: list | None = None,
                      role: str | None = None,
                      department: str | None = None) -> str | None:
    """包级 RBAC 双维度执行侧校验（PLAN 1.2 角色 + PLAN 2.3 部门，两维 AND）。
    返回拒绝文案或 None（放行）。语义：任一维度未配置 = 该维度不限制；admin
    全局视图对两个维度均放行（开放模式/匿名按 auth.current_role 契约同为
    admin，行为等同不限制）；部门命中用归一化比较（strip + casefold），
    未分配部门（空串）不命中任何白名单（fail-closed，最小权限）。执行入口
    校验是最终权限边界——提示层过滤只防误触，防不了提示词注入。"""
    denied = pack_role_denied(pack, allowed_roles, role=role)
    if denied:
        return denied
    if not allowed_departments:
        return None
    if role is None:
        from . import auth
        role = auth.current_role()
    if role == "admin":
        return None  # 全局视图：跨部门放行
    if department is None:
        from . import auth
        department = auth.current_department()
    norm = _norm_dept(department)
    if norm and norm in {_norm_dept(d) for d in allowed_departments}:
        return None
    shown = "、".join(str(d) for d in allowed_departments)
    return (f"领域包「{pack}」仅限 {shown} 部门使用，当前身份无权限调用")


def _guard_pack_tool(tool, pack: str, allowed_roles: list | None,
                     allowed_departments: list | None = None):
    """包级 RBAC 执行侧包装：替换 StructuredTool 的执行入口（func/coroutine），
    调用前先做角色+部门校验（PLAN 2.3）。args_schema 与描述已按原函数构建，
    仅替换执行函数不影响参数定义；同步/异步两条路径分别包装。"""
    if tool is None or not (allowed_roles or allowed_departments):
        return tool
    if tool.func is not None:
        orig = tool.func

        def _guarded(*args, **kwargs):
            denied = pack_scope_denied(pack, allowed_roles, allowed_departments)
            if denied:
                return denied
            return orig(*args, **kwargs)

        tool.func = _guarded
    if getattr(tool, "coroutine", None) is not None:
        orig_coro = tool.coroutine

        async def _guarded_async(*args, **kwargs):
            denied = pack_scope_denied(pack, allowed_roles, allowed_departments)
            if denied:
                return denied
            return await orig_coro(*args, **kwargs)

        tool.coroutine = _guarded_async
    return tool


def build_tool(path: Path, pack: str = "", env: dict | None = None,
               allowed_roles: list | None = None,
               allowed_departments: list | None = None):
    """按当前执行模式构建单个工具的 StructuredTool（领域包加载复用）。
    返回 None 表示构建失败（sandbox-down / 签名提取失败 / 本地导入失败）。
    env 为包级环境变量生效值，仅沙箱模式随请求透传（local 模式已注入进程环境）。
    allowed_roles / allowed_departments 非空时在执行入口外包一层校验
    （包级 RBAC 双维度，v0.17.18/2.3；主图工具绑定是静态的，schema 无法按
    角色/部门摘除，故以执行侧拦截为权限边界）。"""
    mode = exec_mode()
    if mode == "sandbox-down":
        return None
    if mode == "sandbox":
        meta = _extract_signature(path)
        if meta is None:
            print(f"[custom-tools] {path.name} 签名提取失败，跳过")
            return None
        return _guard_pack_tool(_make_sandbox_tool(meta, pack, env),
                                pack, allowed_roles, allowed_departments)
    return _guard_pack_tool(make_local_tool(path), pack, allowed_roles,
                            allowed_departments)


def load_langchain_tools() -> list:
    """把 custom_tools/ 下所有合法工具包装为 LangChain StructuredTool。
    沙箱模式下主进程只做 ast 静态解析、不执行任何自定义代码；
    损坏的文件跳过并打印警告，不影响其他工具与主链路。"""
    from langchain_core.tools import StructuredTool

    mode = exec_mode()
    if mode == "sandbox-down":
        print("[custom-tools] CUSTOM_TOOLS_EXEC=sandbox 但沙箱不可达，"
              "fail-closed：本轮不加载任何自定义工具")
        return []

    tools = []
    for path in sorted(_dir().glob("*.py")):
        name = path.stem
        if not NAME_PATTERN.match(name):
            print(f"[custom-tools] 跳过非法工具名: {path.name}")
            continue
        tool = build_tool(path)
        if tool is not None:
            tools.append(tool)
        elif mode != "sandbox-down":
            print(f"[custom-tools] {name}.py 加载失败，跳过")
    if tools:
        print(f"[custom-tools] 已加载 {len(tools)} 个自定义工具（执行模式: {mode}）")
    return tools


# ---------------------------------------------------------- 专属工具执行与索引

def _resolve_personal(name: str) -> tuple[str, Path] | str:
    """按当前请求身份解析专属工具 → (域, 路径)。user 角色只解析本人专属域；
    approver/admin 全局视图（name 支持「域/工具名」前缀消歧，跨域同名未消歧
    时拒绝）；开放模式/匿名/越域/不存在 → 返回错误文案。执行时归属校验是
    最终权限边界（清单过滤只是提示层，防提示词注入绕过）。"""
    from . import auth
    role = auth.current_role()
    if not auth.enabled():
        return "专属自定义工具仅在认证模式下可用（开放模式无专属域概念）"
    domain, sep, tool = name.strip().partition("/")
    if not sep:
        tool, domain = name.strip(), ""
    if not NAME_PATTERN.match(tool):
        return f"非法工具名: {name}"
    if tool in _global_tool_names():
        return (f"「{tool}」是通用自定义工具，已直接挂载，直接调用即可，"
                "无需经 run_custom_tool")
    if role == "user":
        scope = current_scope()
        if not scope:
            return "专属自定义工具仅对登录用户可用"
        if domain and domain != scope:
            return f"专属工具「{name}」不属于你的专属域（跨域访问被拒绝）"
        path = personal_tool_path(tool, scope)
        if path is None:
            return (f"专属工具「{tool}」不存在或不可见，"
                    "可用 list_custom_tools 查看全部自定义工具")
        return scope, path
    if role not in ("approver", "admin"):
        return "专属自定义工具仅对登录用户可用"
    scopes = [s for s in personal_scopes() if not domain or s == domain]
    hits = [(s, p) for s in scopes
            if (p := personal_tool_path(tool, s)) is not None]
    if not hits:
        return (f"专属工具「{name}」不存在或不可见，"
                "可用 list_custom_tools 查看全部自定义工具")
    if len(hits) > 1:
        doms = "、".join(sorted(s for s, _ in hits))
        return (f"工具名「{tool}」在多个专属域中同名（{doms}），"
                "请以「域/工具名」形式指定域")
    return hits[0]


async def run_personal_tool(name: str, args: dict) -> str:
    """执行专属自定义工具（归属校验后在沙箱中隔离执行：源码随请求直传）。
    local 模式 fail-closed（审查问题1）：专属工具未经人工审批，不允许在
    主进程内执行（同 run_python_code 的未隔离代码立场），须部署沙箱后方可执行。
    返回工具输出；失败返回错误文案（工具层约定：不向模型抛异常）。"""
    if not isinstance(args, dict):
        return "调用失败：args 必须是参数对象（键与工具函数参数一致）"
    resolved = _resolve_personal(name)
    if isinstance(resolved, str):
        return resolved
    _scope, path = resolved
    mode = exec_mode()
    if mode == "sandbox-down":
        return "沙箱不可达（CUSTOM_TOOLS_EXEC=sandbox，fail-closed），工具暂不可用"
    if mode == "local":
        # 审查问题1修复：专属工具未经人工审批，不允许在主进程内执行
        # （与 run_python_code 的未隔离代码 fail-closed 立场一致），
        # 须部署沙箱（SANDBOX_URL）后专属工具才可执行
        return ("专属工具需沙箱隔离执行（fail-closed）：当前为本地执行模式"
                "（沙箱未部署或不可达），免审批的专属工具不允许在未隔离环境中执行。")
    if mode != "sandbox":
        return "沙箱不可达（CUSTOM_TOOLS_EXEC=sandbox，fail-closed），工具暂不可用"
    try:
        code = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "工具源码读取失败"
    meta = _extract_signature(path)
    if meta is None:
        return f"工具「{name}」签名提取失败（语法错误或缺少同名函数）"
    tool = _make_sandbox_tool(meta, code=code)
    try:
        return str(await tool.ainvoke(args))
    except Exception as e:
        # 审查问题4修复：不向模型/用户回显原始异常文本（P1-3 约定：
        # 异常可能携带服务器绝对路径），异常细节仅进服务端日志
        print(f"[custom-tools] 专属工具「{name}」执行失败: {e}")
        return f"工具「{name}」执行失败（内部错误）"


def personal_index_prompt() -> str:
    """按当前请求身份动态注入的专属工具索引段（v0.17.12，随技能层
    personal_index_prompt 同机制：每轮模型调用时实时扫描，不参与图重建签名，
    变更即时生效）。user 角色列本人专属域；approver/admin 全局视图（全部专属
    域，工具名带「域/」前缀，与 run_custom_tool 的消歧参数一致）；开放模式/
    匿名 → 空串。被通用层遮蔽的工具实际不可用，不进索引。"""
    from . import auth
    if not auth.enabled():
        return ""
    role = auth.current_role()
    if role == "user":
        scope = current_scope()
        if not scope:
            return ""
        entries = [(t["name"], t["description"], scope, False)
                   for t in list_personal_tools(scope)]
    elif role in ("approver", "admin"):
        entries = [(t["name"], t["description"], t["scope"], True)
                   for t in list_personal_all()]
    else:
        return ""
    items = []
    for name, desc, scope, qualified in entries:
        meta = None
        pdir = _personal_dir(scope)
        if pdir is not None and (pdir / f"{name}.py").is_file():
            meta = _extract_signature(pdir / f"{name}.py")
        if meta is None:   # 签名提取失败（损坏文件）不进索引，加载时会被跳过
            continue
        shown = f"{scope}/{name}" if qualified else name
        params = ", ".join(f"{p['name']}: {getattr(p['type'], '__name__', str)}"
                           + ("" if p["required"] else "（可选）")
                           for p in meta["params"])
        line = f"- {shown}: {desc[:120]}"
        if params:
            line += f"（参数: {params}）"
        if qualified:
            line += f"（专属域：{scope}）"
        items.append(line)
    if not items:
        return ""
    return ("\n\n以下是你的专属自定义工具（通过 run_custom_tool 工具调用：name 传"
            "工具名，args 传参数对象；他人专属工具不可见也不可调用）：\n"
            + "\n".join(items))
