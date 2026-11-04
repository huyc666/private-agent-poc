"""通用多角色编排引擎（v0.17.44，PLAN 5.1，spec docs/specs/5.1-general-orchestration.md）。

设计要点：
- 定义是数据（YAML），引擎不 exec/eval；加载期与执行期双重校验，fail-closed。
- 子执行身份是发起者 usage.Identity 的不可变快照：入口读一次、显式参数下传、
  每个执行边界与每次模型调用前重设，禁止依赖调用栈恰好共享 ContextVar。
- 工具装配层按发起者身份过滤（包级 RBAC），执行层再包一层危险工具拒绝 +
  身份重设；两层同时存在。
- 复用 packs / custom_tools / sandbox 既有构建器与守卫，不另造权限判定。
- 失败语义 fail-fast、不重试；计量全部计入父 run，source=orchestration。
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from . import auth, config, custom_tools, packs, sessions, tools_builtin, usage
from .state import STATE

logger = logging.getLogger(__name__)

ORCH_TAG = "orchestration"
WORKER_TAG = "ma_worker"

MAX_STEPS = 8
MAX_ROLES = 8
GLOBAL_MAX_PARALLEL = 4
MAX_MODEL_CALLS = 24
MAX_PROMPT_LEN = 4000
MAX_FANOUT_LIMIT = 20
WORKER_TOOL_ROUNDS = 4

SAFE_BUILTIN_TOOL_NAMES = ("read_text_file", "list_uploaded_docs", "save_output_file")
# 只读类包工具需在此显式登记；新包工具默认不进编排，避免写盘/外联类能力扩大攻击面。
SAFE_PACK_TOOL_NAMES = ("search_common_knowledge", "calculator", "get_current_time",
                        "search_hr_knowledge", "search_finance_knowledge",
                        "get_current_weather")
SAFE_MCP_TOOL_NAMES = ("get_city_weather",)

# R3：审批类/管理类/危险类工具永久排除，定义只能从安全清单里选子集。
FORBIDDEN_TOOL_NAMES = frozenset({
    "delete_workspace_file", "create_skill", "delete_skill", "update_skill",
    "create_custom_tool", "delete_custom_tool", "update_custom_tool",
    "run_custom_tool", "enable_pack", "disable_pack", "create_pack",
    "delete_pack", "sign_pack", "run_python_code", "orchestrate_subtasks",
    "create_orchestration", "run_orchestration", "add_user", "delete_user",
    "list_users", "delete_uploaded_file",
})

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_ABS_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?:^|[^\w])/(?:etc|home|root|var|usr|tmp|opt|proc|sys)/)")

_TOP_KEYS = {"name", "version", "description", "max_parallel", "on_error",
             "roles", "steps", "aggregate"}
_ROLE_KEYS = {"id", "name", "prompt", "tools"}
_STEP_KEYS = {"id", "role", "mode", "fanout", "inputs"}
_AGG_KEYS = {"role", "inputs"}

_CACHE_LOCK = threading.Lock()
_TOOL_CACHE: dict[tuple, tuple] = {}
_CACHE_MAX = 256


class _Abort(Exception):
    """编排主动中止（栅栏/预算），与普通执行异常区分。"""


class _FenceAbort(_Abort):
    pass


class _BudgetAbort(_Abort):
    pass


def definition_hash(definition: dict) -> str:
    """定义内容摘要（执行前快照用；排序键保证同一内容稳定）。"""
    raw = json.dumps(definition, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _abs_path_in(value) -> bool:
    if isinstance(value, str):
        return bool(_ABS_PATH_RE.search(value))
    if isinstance(value, list):
        return any(_abs_path_in(v) for v in value)
    if isinstance(value, dict):
        return any(_abs_path_in(v) for v in value.values())
    return False


def validate_definition(definition: dict) -> list[str]:
    """返回错误列表；空列表 = 可执行。错误文案不回显绝对路径。"""
    errors: list[str] = []
    if not isinstance(definition, dict):
        return ["定义必须是 YAML 对象"]

    unknown = sorted(set(definition) - _TOP_KEYS)
    if unknown:
        errors.append(f"定义包含未知字段：{', '.join(unknown)}")

    name = definition.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append("name 必须是 1~50 位小写字母/数字/连字符（slug）")

    version = definition.get("version", "1")
    if not isinstance(version, (str, int)) or not str(version).strip():
        errors.append("version 必须是非空字符串或数字")

    max_parallel = definition.get("max_parallel", GLOBAL_MAX_PARALLEL)
    if (not isinstance(max_parallel, int) or isinstance(max_parallel, bool)
            or max_parallel < 1):
        errors.append("max_parallel 必须是正整数")
    elif max_parallel > GLOBAL_MAX_PARALLEL:
        errors.append(f"max_parallel 超过全局上限 {GLOBAL_MAX_PARALLEL}")

    on_error = definition.get("on_error", "fail-fast")
    if on_error != "fail-fast":
        errors.append("on_error 仅支持 fail-fast（不自动重试）")

    roles = definition.get("roles")
    if not isinstance(roles, list) or not roles:
        errors.append("roles 必须是非空列表")
        roles = []
    elif len(roles) > MAX_ROLES:
        errors.append(f"角色数超过上限 {MAX_ROLES}")
    role_ids: set[str] = set()
    for i, role in enumerate(roles):
        if not isinstance(role, dict):
            errors.append(f"第 {i + 1} 个角色必须是对象")
            continue
        unknown = sorted(set(role) - _ROLE_KEYS)
        if unknown:
            errors.append(f"角色包含未知字段：{', '.join(unknown)}")
        rid = role.get("id")
        if not isinstance(rid, str) or not rid.strip():
            errors.append(f"第 {i + 1} 个角色缺少 id")
        elif rid in role_ids:
            errors.append(f"角色 id「{rid}」重复")
        else:
            role_ids.add(rid)
        prompt = role.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"角色「{rid or i + 1}」的 prompt 不能为空")
        elif len(prompt) > MAX_PROMPT_LEN:
            errors.append(f"角色「{rid or i + 1}」的 prompt 超过 {MAX_PROMPT_LEN} 字")
        tools = role.get("tools", [])
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            errors.append(f"角色「{rid or i + 1}」的 tools 必须是字符串列表")
            tools = []
        for tool_name in tools:
            if tool_name in FORBIDDEN_TOOL_NAMES:
                errors.append(f"角色「{rid or i + 1}」声明了危险工具「{tool_name}」，"
                              "编排禁止使用")
            elif tool_name not in available_tools():
                errors.append(f"角色「{rid or i + 1}」声明了未知工具「{tool_name}」")

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps 必须是非空列表")
        steps = []
    elif len(steps) > MAX_STEPS:
        errors.append(f"步骤数超过上限 {MAX_STEPS}")

    seen_steps: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"第 {i + 1} 个步骤必须是对象")
            continue
        unknown = sorted(set(step) - _STEP_KEYS)
        if unknown:
            errors.append(f"步骤包含未知字段：{', '.join(unknown)}")
        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"第 {i + 1} 个步骤缺少 id")
        elif sid in seen_steps:
            errors.append(f"步骤 id「{sid}」重复")
        else:
            seen_steps.add(sid)
        role = step.get("role")
        if role not in role_ids:
            errors.append(f"步骤「{sid or i + 1}」引用了未定义角色「{role}」")
        mode = step.get("mode", "single")
        if mode not in ("single", "parallel"):
            errors.append(f"步骤「{sid or i + 1}」的 mode 必须是 single/parallel")
        inputs = step.get("inputs", [])
        if not isinstance(inputs, list) or any(not isinstance(x, str) for x in inputs):
            errors.append(f"步骤「{sid or i + 1}」的 inputs 必须是字符串列表")
            inputs = []
        for ref in inputs:
            if ref not in seen_steps or ref == sid:
                errors.append(f"步骤「{sid or i + 1}」的 inputs 只能引用前序步骤，"
                              f"「{ref}」非法")
        fanout = step.get("fanout")
        if mode == "parallel":
            if not isinstance(fanout, dict):
                errors.append(f"并行步骤「{sid or i + 1}」缺少 fanout")
            else:
                src = fanout.get("from")
                if src not in seen_steps or src == sid:
                    errors.append(f"步骤「{sid or i + 1}」的 fanout 只能引用前序步骤")
                limit = fanout.get("limit", GLOBAL_MAX_PARALLEL)
                if (not isinstance(limit, int) or isinstance(limit, bool)
                        or limit < 1 or limit > MAX_FANOUT_LIMIT):
                    errors.append(f"步骤「{sid or i + 1}」的 fanout.limit 必须在 "
                                  f"1~{MAX_FANOUT_LIMIT}")
        elif fanout is not None:
            errors.append(f"串行步骤「{sid or i + 1}」不应声明 fanout")

    aggregate = definition.get("aggregate")
    if aggregate is not None:
        if not isinstance(aggregate, dict):
            errors.append("aggregate 必须是对象")
        else:
            unknown = sorted(set(aggregate) - _AGG_KEYS)
            if unknown:
                errors.append(f"aggregate 包含未知字段：{', '.join(unknown)}")
            if aggregate.get("role") not in role_ids:
                errors.append(f"aggregate 引用了未定义角色「{aggregate.get('role')}」")
            agg_inputs = aggregate.get("inputs", [])
            if not isinstance(agg_inputs, list):
                errors.append("aggregate.inputs 必须是字符串列表")
                agg_inputs = []
            for ref in agg_inputs:
                if ref not in seen_steps:
                    errors.append(f"aggregate.inputs 只能引用已声明步骤，「{ref}」非法")

    if _abs_path_in(definition):
        errors.append("定义文本包含绝对路径（已拒绝，错误信息不回显原路径）")
    return errors


def load_definition_text(text: str) -> dict:
    """从 YAML 文本加载定义；任何异常都转成结构化错误。"""
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        return {"status": "error", "error": f"YAML 解析失败（{type(e).__name__}）",
                "definition": None, "hash": ""}
    if not isinstance(data, dict):
        return {"status": "error", "error": "定义必须是 YAML 对象",
                "definition": None, "hash": ""}
    errors = validate_definition(data)
    if errors:
        return {"status": "error", "error": "；".join(errors),
                "definition": data, "hash": definition_hash(data)}
    return {"status": "ok", "error": "", "definition": data,
            "hash": definition_hash(data)}


def load_definition(path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "error", "error": f"读取定义失败（{type(e).__name__}）",
                "definition": None, "hash": ""}
    return load_definition_text(text)


def _pack_tool_meta() -> dict:
    meta: dict[str, dict] = {}
    try:
        for pack in packs.enabled_packs():
            for path in pack.get("tools") or []:
                stem = getattr(path, "stem", None) or Path(str(path)).stem
                # 与 packs.load_pack_tools 一致：重名工具先到先得，后到者被跳过
                if stem not in meta:
                    meta[stem] = pack
    except Exception:
        logger.error("[orchestration] 读取包元数据失败，按无包工具处理")
        return {}
    return meta


def _builtin_tool(name: str):
    func = {
        "read_text_file": tools_builtin.read_text_file,
        "list_uploaded_docs": tools_builtin.list_uploaded_docs,
        "save_output_file": tools_builtin.save_output_file,
    }[name]
    return StructuredTool.from_function(
        func=func, name=name, description=func.__doc__)


def available_tools() -> dict:
    """引擎安全工具清单的实际可用对象（每次调用按当前运行态构建）。"""
    out = {name: _builtin_tool(name) for name in SAFE_BUILTIN_TOOL_NAMES}
    meta = _pack_tool_meta()
    for name in SAFE_PACK_TOOL_NAMES:
        tool = STATE.pack_tools.get(name)
        if tool is not None and name in meta and name not in out:
            out[name] = tool
    for name in SAFE_MCP_TOOL_NAMES:
        if name in STATE.mcp_tools:
            out[name] = STATE.mcp_tools[name]
    return out


def _bind_identity(identity: usage.Identity, session_id: str | None = None,
                   run_id: str | None = None) -> None:
    usage.current_identity.set(identity)
    usage.current_user.set(identity.username or "anonymous")
    if session_id is not None:
        usage.current_session.set(session_id)
    if run_id is not None:
        usage.current_run.set(run_id)


def _deny_text(name: str) -> str:
    return (f"拒绝执行：工具「{name}」不在编排安全清单内（编排不得绕过审批门）")


def _is_safe_tool_name(name: str) -> bool:
    return (name in SAFE_BUILTIN_TOOL_NAMES or name in SAFE_PACK_TOOL_NAMES
            or name in SAFE_MCP_TOOL_NAMES)


class _GuardedLLM:
    """每次模型调用前重设身份快照并校验代际（含 bind_tools 后的运行链）。"""

    def __init__(self, inner, ctx: "_Context"):
        self._inner = inner
        self._ctx = ctx

    async def ainvoke(self, input, config=None, **kwargs):
        self._ctx.before_model_call()
        return await self._inner.ainvoke(input, config=config, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return _GuardedLLM(self._inner.bind_tools(tools, **kwargs), self._ctx)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Context:
    def __init__(self, *, identity, session_id, generation, run_id):
        self.identity = identity
        self.session_id = session_id
        self.generation = generation
        self.run_id = run_id
        self.calls = 0

    def bind(self):
        _bind_identity(self.identity, self.session_id, self.run_id)

    def check_fence(self):
        if self.generation is None or not self.session_id:
            return
        if not sessions.fence_ok(self.session_id, self.generation):
            raise _FenceAbort("会话已被删除或代际已变更，编排中止")

    def before_model_call(self):
        self.bind()
        self.check_fence()
        self.take_call()

    def take_call(self):
        if self.calls >= MAX_MODEL_CALLS:
            raise _BudgetAbort(f"编排模型调用次数已达上限（{MAX_MODEL_CALLS}），已中止")
        self.calls += 1


def execution_guard(tool, identity: usage.Identity):
    """执行层兜底：危险工具直接拒绝；调用前重设发起者身份快照。"""
    if tool is None:
        return None

    def _guard_sync(*args, **kwargs):
        _bind_identity(identity)
        if tool.name in FORBIDDEN_TOOL_NAMES or not _is_safe_tool_name(tool.name):
            return _deny_text(tool.name)
        if tool.func is not None:
            return tool.func(*args, **kwargs)
        return _deny_text(tool.name)

    async def _guard_async(*args, **kwargs):
        _bind_identity(identity)
        if tool.name in FORBIDDEN_TOOL_NAMES or not _is_safe_tool_name(tool.name):
            return _deny_text(tool.name)
        if tool.coroutine is not None:
            return await tool.coroutine(*args, **kwargs)
        if tool.func is not None:
            return tool.func(*args, **kwargs)
        return _deny_text(tool.name)

    kwargs: dict = {"name": tool.name, "description": tool.description,
                    "args_schema": getattr(tool, "args_schema", None)}
    if tool.func is not None:
        kwargs["func"] = _guard_sync
    if tool.coroutine is not None:
        kwargs["coroutine"] = _guard_async
    return StructuredTool(**kwargs)


def tools_for_role(role: dict, identity: usage.Identity) -> list:
    """装配层：按发起者身份过滤角色声明的工具子集（绑定给模型之前）。"""
    if not isinstance(role, dict):
        return []
    avail = available_tools()
    meta = _pack_tool_meta()
    out = []
    for name in role.get("tools") or []:
        if name in FORBIDDEN_TOOL_NAMES:
            continue
        tool = avail.get(name)
        if tool is None:
            continue
        pack = meta.get(name)
        if pack is not None and custom_tools.pack_scope_denied(
                pack.get("name", ""), pack.get("allowed_roles") or [],
                pack.get("allowed_departments") or [],
                role=identity.role, department=identity.department):
            continue
        out.append(execution_guard(tool, identity))
    return out


def _registry_signature() -> tuple:
    meta = _pack_tool_meta()
    return (tuple(SAFE_PACK_TOOL_NAMES),
            tuple(sorted((name, pack.get("name", ""),
                          tuple(pack.get("allowed_roles") or []),
                          tuple(pack.get("allowed_departments") or []))
                         for name, pack in meta.items())))


def role_tools(definition: dict, role_id: str, identity: usage.Identity) -> list:
    """带缓存的角色工具集（键 = 定义 hash + 角色 id + 身份四元组，缓存只存工具名）。"""
    key = (definition_hash(definition), role_id, identity.username, identity.role,
           identity.department, identity.auth_source)
    sig = _registry_signature()
    with _CACHE_LOCK:
        cached = _TOOL_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        names = cached[1]
    else:
        role = next((r for r in definition.get("roles") or []
                     if isinstance(r, dict) and r.get("id") == role_id), None)
        bound = tools_for_role(role or {}, identity) if role else []
        names = tuple(t.name for t in bound)
        with _CACHE_LOCK:
            if len(_TOOL_CACHE) >= _CACHE_MAX:
                _TOOL_CACHE.pop(next(iter(_TOOL_CACHE)))
            _TOOL_CACHE[key] = (sig, names)
    avail = available_tools()
    current_meta = _pack_tool_meta()
    out = []
    for name in names:
        tool = avail.get(name)
        if tool is None:
            continue
        pack = current_meta.get(name)
        if pack is not None and custom_tools.pack_scope_denied(
                pack.get("name", ""), pack.get("allowed_roles") or [],
                pack.get("allowed_departments") or [],
                role=identity.role, department=identity.department):
            continue
        out.append(execution_guard(tool, identity))
    return out


def _system_prompt(role: dict, identity: usage.Identity) -> str:
    prompt = str(role.get("prompt") or "")
    try:
        fragments = packs.prompt_fragments(
            identity.role, identity.department) if identity.role else ""
    except Exception as e:
        logger.error(f"[orchestration] 注入包提示片段失败: {e}")
        fragments = ""
    if fragments:
        return f"{prompt}\n\n{fragments}"
    return prompt


def _step_messages(role: dict, identity: usage.Identity, task: str,
                   outputs: dict, inputs: list, extra: str = "") -> list:
    user = f"总任务：{task}"
    for ref in inputs or []:
        text = outputs.get(ref)
        if text:
            user += f"\n\n【上游步骤 {ref} 输出】\n{text}"
    if extra:
        user += f"\n\n{extra}"
    return [SystemMessage(content=_system_prompt(role, identity)),
            HumanMessage(content=user)]


def _parse_fanout(text: str, limit: int, task: str) -> list[str]:
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if m:
        try:
            qs = json.loads(m.group(0))
            if isinstance(qs, list):
                out = [str(q).strip() for q in qs if str(q).strip()]
                if out:
                    return out[:limit]
        except json.JSONDecodeError:
            pass
    lines = [l.strip(" \t-•*0123456789.、") for l in (text or "").splitlines()]
    lines = [l for l in lines if len(l) > 4]
    return lines[:limit] or [task]


def _content_of(response) -> str:
    text = getattr(response, "content", response)
    if not isinstance(text, str):
        text = str(text)
    return text


async def _run_direct(ctx: _Context, llm, messages: list) -> str:
    guarded = _GuardedLLM(llm, ctx)
    resp = await guarded.ainvoke(messages, config={"tags": [ORCH_TAG]})
    return _content_of(resp)


async def _run_react(ctx: _Context, llm, tools: list, messages: list) -> str:
    from langgraph.prebuilt import create_react_agent

    agent = create_react_agent(_GuardedLLM(llm, ctx), tools)
    result = await agent.ainvoke(
        {"messages": messages},
        config={"recursion_limit": WORKER_TOOL_ROUNDS * 2 + 2,
                "tags": [ORCH_TAG, WORKER_TAG]},
    )
    return _content_of(result["messages"][-1])


def _failure(definition: dict, hash_value: str, *, error: str,
             failed_step: str = "", outputs: dict | None = None,
             findings: list | None = None, subtasks: list | None = None) -> dict:
    return {
        "ok": False,
        "name": definition.get("name", "") if isinstance(definition, dict) else "",
        "version": str(definition.get("version", "1")) if isinstance(definition, dict) else "",
        "hash": hash_value,
        "outputs": outputs or {},
        "findings": findings or [],
        "subtasks": subtasks or [],
        "summary": "",
        "completed": False,
        "failed_step": failed_step,
        "error": error,
    }


async def execute(definition: dict, task: str, *, identity: usage.Identity,
                  session_id: str = "", generation: int | None = None,
                  run_id: str | None = None, llm=None) -> dict:
    """执行一份定义。所有失败都以结构化结果返回，不抛裸异常。"""
    if not isinstance(identity, usage.Identity):
        return _failure({}, "", error="编排需要显式身份快照，已拒绝")
    if custom_tools.exec_mode() == "sandbox-down":
        return _failure(definition if isinstance(definition, dict) else {}, "",
                        error="沙箱不可达（fail-closed），编排未启动")

    errors = validate_definition(definition) if isinstance(definition, dict) else ["定义必须是对象"]
    hash_value = definition_hash(definition) if isinstance(definition, dict) else ""
    if errors:
        return _failure(definition if isinstance(definition, dict) else {}, hash_value,
                        error="；".join(errors))

    use_llm = llm if llm is not None else STATE.llm
    if use_llm is None:
        return _failure(definition, hash_value, error="编排需要真实模型支持（当前为 Mock 模式）")

    ctx = _Context(identity=identity, session_id=session_id,
                   generation=generation,
                   run_id=run_id if run_id is not None else usage.current_run.get())
    ctx.bind()
    outputs: dict[str, str] = {}
    findings: list = []
    subtasks: list = []
    failed_step = ""
    try:
        for step in definition["steps"]:
            sid = step["id"]
            failed_step = sid
            ctx.check_fence()
            ctx.bind()
            role = next(r for r in definition["roles"] if r["id"] == step["role"])
            tools = role_tools(definition, step["role"], identity)
            inputs = step.get("inputs") or []
            if step.get("mode", "single") == "parallel":
                src = step["fanout"]["from"]
                fanout_limit = int(step["fanout"].get("limit", GLOBAL_MAX_PARALLEL))
                # fanout.limit 决定子任务数量；全局硬顶只约束同时执行数
                parse_limit = fanout_limit
                concurrency = min(fanout_limit,
                                  int(definition.get("max_parallel", GLOBAL_MAX_PARALLEL)),
                                  GLOBAL_MAX_PARALLEL)
                subtasks = _parse_fanout(outputs.get(src, ""), parse_limit, task)
                findings_for_step = await _run_parallel(
                    ctx, use_llm, tools, role, identity, task, outputs,
                    inputs, subtasks, concurrency)
                findings.extend(findings_for_step)
                outputs[sid] = "\n\n".join(
                    f"【子任务】{f['subtask']}\n【结果】{f['result']}"
                    for f in findings_for_step)
            else:
                messages = _step_messages(role, identity, task, outputs, inputs)
                if tools:
                    out = await _run_react(ctx, use_llm, tools, messages)
                else:
                    out = await _run_direct(ctx, use_llm, messages)
                outputs[sid] = out

        aggregate = definition.get("aggregate")
        if aggregate:
            failed_step = "aggregate"
            role = next(r for r in definition["roles"]
                        if r["id"] == aggregate["role"])
            tools = role_tools(definition, aggregate["role"], identity)
            messages = _step_messages(role, identity, task, outputs,
                                      aggregate.get("inputs") or [])
            if tools:
                summary = await _run_react(ctx, use_llm, tools, messages)
            else:
                summary = await _run_direct(ctx, use_llm, messages)
            outputs["aggregate"] = summary
        else:
            summary = outputs.get(definition["steps"][-1]["id"], "")
        summary = summary or "（整合结果为空）"
    except _FenceAbort as e:
        return _failure(definition, hash_value, error=str(e),
                        failed_step=failed_step, outputs=outputs,
                        findings=findings, subtasks=subtasks)
    except _BudgetAbort as e:
        return _failure(definition, hash_value, error=str(e),
                        failed_step=failed_step, outputs=outputs,
                        findings=findings, subtasks=subtasks)
    except Exception as e:
        failed_step = failed_step or _current_step_id(definition, outputs)
        logger.error(f"[orchestration] 执行失败（定义 {definition.get('name')}）: {e}")
        return _failure(definition, hash_value,
                        error=f"编排执行失败（{type(e).__name__}）",
                        failed_step=failed_step, outputs=outputs,
                        findings=findings, subtasks=subtasks)

    return {
        "ok": True,
        "name": definition["name"],
        "version": str(definition.get("version", "1")),
        "hash": hash_value,
        "outputs": outputs,
        "findings": findings,
        "subtasks": subtasks,
        "summary": summary,
        "completed": True,
        "failed_step": "",
        "error": "",
    }


def _current_step_id(definition: dict, outputs: dict) -> str:
    for step in definition.get("steps") or []:
        if step.get("id") not in outputs:
            return step.get("id", "")
    return "aggregate"


async def _run_parallel(ctx: _Context, llm, tools: list, role: dict,
                        identity: usage.Identity, task: str, outputs: dict,
                        inputs: list, subtasks: list[str],
                        concurrency: int) -> list:
    """阶段内并行；峰值受 semaphore（已按双重上限取 min）约束。"""
    if not subtasks:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))
    tasks = []

    async def _one(sub: str) -> dict:
        async with sem:
            ctx.check_fence()
            ctx.bind()
            messages = _step_messages(role, identity, task, outputs, inputs,
                                      extra=f"你负责的子任务：{sub}")
            out = await _run_react(ctx, llm, tools, messages)
            return {"step": role.get("id", ""), "subtask": sub, "result": out}

    for sub in subtasks:
        tasks.append(asyncio.create_task(_one(sub)))
    try:
        return list(await asyncio.gather(*tasks))
    except Exception:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# ---------------------------------------------------------------- 资产与入口

def _target_dir(scope: str | None) -> Path:
    if scope is None:
        return Path(config.ORCHESTRATIONS_DIR)
    return Path(config.PERSONAL_ORCHESTRATIONS_DIR) / scope


def save_definition(definition: dict, scope: str | None = None) -> str:
    """纯落盘（存储层不做审批）；scope=None 为全局定义。"""
    target = _target_dir(scope)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{definition['name']}.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(definition, allow_unicode=True,
                                  sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def resolve_definition(name: str, identity: usage.Identity) -> dict:
    """按发起者可见范围解析定义文件：个人定义优先，其次全局。"""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return {"status": "error", "error": "定义名不合法", "path": ""}
    if identity.role == "user" and auth.enabled() and identity.username \
            and identity.username != "anonymous":
        scope = config.user_scope(identity.username)
        personal = Path(config.PERSONAL_ORCHESTRATIONS_DIR) / scope / f"{name}.yaml"
        if personal.exists():
            return {"status": "ok", "error": "", "path": str(personal)}
    global_path = Path(config.ORCHESTRATIONS_DIR) / f"{name}.yaml"
    if global_path.exists():
        return {"status": "ok", "error": "", "path": str(global_path)}
    return {"status": "error", "error": f"编排定义「{name}」不存在或无权限", "path": ""}


def _identity_from_context() -> usage.Identity:
    snapshot = usage.current_identity.get()
    if isinstance(snapshot, usage.Identity):
        return snapshot
    # 非对话路径的一次性入口快照（之后全程显式下传，不再查表重建）
    role = auth.current_role()
    dept = auth.current_department()
    return usage.Identity(
        username=usage.current_user.get() or "anonymous",
        role=role, department=dept,
        auth_source="open" if not auth.enabled() else "local")


async def run_orchestration(name: str, task: str) -> str:
    """按已创建的编排定义执行一次多角色流程（工具入口）。"""
    if STATE.llm is None:
        return "多角色编排需要真实模型支持（当前为 Mock 模式）"
    identity = _identity_from_context()
    resolved = resolve_definition(name, identity)
    if resolved["status"] != "ok":
        return f"编排未执行：{resolved['error']}"
    loaded = load_definition(resolved["path"])
    if loaded["status"] != "ok":
        return f"编排未执行：定义校验失败（{loaded['error']}）"
    result = await execute(
        loaded["definition"], task, identity=identity,
        session_id=usage.current_session.get(),
        generation=usage.current_generation.get(),
        run_id=usage.current_run.get(), llm=STATE.llm)
    if not result["ok"]:
        step = f"步骤「{result['failed_step']}」" if result["failed_step"] else ""
        return f"多角色编排执行失败{step}（{result['error']}）"
    return (f"多角色编排完成：定义「{result['name']}」（v{result['version']}），"
            f"{len(result['outputs'])} 个产出节点，"
            f"{len(result['findings'])} 个并行子任务。\n"
            f"子任务清单：{json.dumps(result['subtasks'], ensure_ascii=False)}\n\n"
            f"{result['summary']}")


def _write_scope(identity: usage.Identity) -> str | None:
    if identity.role == "user" and auth.enabled() and identity.username \
            and identity.username != "anonymous":
        return config.user_scope(identity.username)
    return None


async def create_orchestration(content: str, overwrite: bool = False) -> str:
    """创建编排定义：个人域自助；全局域走审批（职责分离沿用 v0.17.41）。"""
    loaded = load_definition_text(content)
    if loaded["status"] != "ok":
        return f"创建失败：{loaded['error']}"
    definition = loaded["definition"]
    name = definition["name"]
    identity = _identity_from_context()
    scope = _write_scope(identity)
    path = _target_dir(scope) / f"{name}.yaml"
    if path.exists() and not overwrite:
        return f"创建失败：编排定义「{name}」已存在（如需覆盖请显式确认）"

    def _do_write() -> str:
        # 审批窗口内可能已有同名文件；overwrite=False 时再次检查，避免静默覆盖
        if path.exists() and not overwrite:
            return f"创建失败：编排定义「{name}」已存在（如需覆盖请显式确认）"
        saved = save_definition(definition, scope)
        where = "个人域" if scope else "全局"
        return f"已创建{where}编排定义「{name}」（版本 {definition.get('version', '1')}）：{saved}"

    if scope is not None:
        return _do_write()
    # 全局定义审批门（graph 外/拒绝/异常一律 fail-closed，不落盘）
    from . import approval
    return approval.require_approval(
        "create_orchestration",
        {"name": name, "version": str(definition.get("version", "1")),
         "roles": len(definition.get("roles") or []),
         "steps": len(definition.get("steps") or [])},
        "编排定义创建", _do_write)
