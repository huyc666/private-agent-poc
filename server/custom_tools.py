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
- 创建/删除都要先过 ast 静态校验，再经过人工审批（interrupt，fail-closed）
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


def signature() -> tuple:
    """目录内容签名（文件名 + 修改时间），用于热重载变更检测。"""
    return tuple(sorted((p.name, p.stat().st_mtime) for p in _dir().glob("*.py")))


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


def write_custom_tool(name: str, code: str) -> str:
    """通过校验后落盘（调用方负责先走审批）。"""
    err = validate_tool_code(name, code)
    if err:
        return f"创建失败：{err}"
    path = _dir() / f"{name}.py"
    existed = path.exists()
    path.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")
    action = "已更新" if existed else "已创建"
    # 只呈现逻辑路径（相对工作区），不暴露服务器部署布局（安全修复）
    return (f"自定义工具「{name}」{action}：custom_tools/{name}.py\n"
            "（无需重启，下一轮对话起即可调用）")


def delete_custom_tool_file(name: str) -> str:
    """删除工具文件（调用方负责先走审批）。"""
    if not NAME_PATTERN.match(name):
        return "删除失败：非法工具名"
    path = _dir() / f"{name}.py"
    if not path.is_file():
        return f"自定义工具「{name}」不存在"
    path.unlink()
    # 只呈现逻辑路径（相对工作区），不暴露服务器部署布局（安全修复）
    return (f"自定义工具「{name}」已删除：custom_tools/{name}.py\n"
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


def exec_mode() -> str:
    """解析执行模式：auto（沙箱优先，不可达回退本地）/ sandbox（强制，不可达则 fail-closed）/ local。"""
    mode = os.environ.get("CUSTOM_TOOLS_EXEC", "auto").lower()
    up = _sandbox_up()
    if mode == "local":
        return "local"
    if mode == "sandbox":
        return "sandbox" if up else "sandbox-down"
    return "sandbox" if up else "local"


def _sandbox_run_tool(name: str, args: dict, pack: str = "", env: dict | None = None) -> str:
    url = config.SANDBOX_URL.rstrip("/") + "/run_tool"
    body = json.dumps({"name": name, "args": args, "pack": pack, "env": env or {}},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.SANDBOX_AUTH_TOKEN:
        headers["X-Sandbox-Token"] = config.SANDBOX_AUTH_TOKEN
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=config.SANDBOX_TIMEOUT + 5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return f"沙箱调用失败: {e}"
    if data.get("ok"):
        return str(data.get("result", ""))
    return f"沙箱执行失败: {data.get('error', '未知错误')}"


def _make_sandbox_tool(meta: dict, pack: str = "", env: dict | None = None):
    """用 ast 提取的签名动态生成 args_schema，实际执行代理到沙箱服务。"""
    from langchain_core.tools import StructuredTool
    from pydantic import create_model

    fields = {}
    for p in meta["params"]:
        fields[p["name"]] = (p["type"], ...) if p["required"] else (p["type"], p["default"])
    schema = create_model(f"{meta['name'].title().replace('_', '')}Args", **fields)
    tool_name = meta["name"]

    def _proxy(**kwargs):
        return _sandbox_run_tool(tool_name, kwargs, pack, env)

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


def build_tool(path: Path, pack: str = "", env: dict | None = None):
    """按当前执行模式构建单个工具的 StructuredTool（领域包加载复用）。
    返回 None 表示构建失败（sandbox-down / 签名提取失败 / 本地导入失败）。
    env 为包级环境变量生效值，仅沙箱模式随请求透传（local 模式已注入进程环境）。"""
    mode = exec_mode()
    if mode == "sandbox-down":
        return None
    if mode == "sandbox":
        meta = _extract_signature(path)
        if meta is None:
            print(f"[custom-tools] {path.name} 签名提取失败，跳过")
            return None
        return _make_sandbox_tool(meta, pack, env)
    return make_local_tool(path)


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
