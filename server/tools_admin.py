"""管理类工具：技能 / 自定义工具 / 领域包 / 多 Agent 调研，以及内核工具装配清单。

这些工具是 Agent 对自身能力的「运行时管理接口」，全部经审批门
（approval.require_approval）保护危险操作，fail-closed。

能力分层约定：
- 内核工具（tools_builtin.py）：安全边界相关的最小集合
- 平台包/领域包工具：经 packs.load_pack_tools() 挂载，不在本文件
- 本文件工具：上述两类能力的生命周期管理

注意：enable/disable/create/delete_pack 改变能力边界后需重建 Agent 图，
通过 _reload_graph() 懒导入 agent 模块实现（避免与 agent.py 的循环依赖）。
"""
from . import approval, custom_tools, packs, skills
from .state import STATE
from .tools_builtin import (delete_workspace_file, list_uploaded_docs,
                            read_text_file, run_python_code, save_output_file)


def _reload_graph():
    """管理操作改变能力边界后重建 Agent 图（懒导入避免与 agent.py 循环依赖）。"""
    from . import agent
    agent.maybe_reload_custom_tools()


# ---------------------------------------------------------------- 技能工具

def list_skills_tool() -> str:
    """列出当前全部可用技能（名称 + 一句话描述）。"""
    items = skills.list_skills()
    if not items:
        return "当前没有可用技能。可以用 create_skill 工具创建一个。"
    return "\n".join(f"- {s['name']}: {s['description']}" for s in items)


def load_skill(name: str) -> str:
    """加载指定技能的完整指令内容（SKILL.md）。任务匹配某个技能描述时先调用本工具获取指令。"""
    content = skills.load_skill(name)
    return content if content is not None else f"技能「{name}」不存在，可用 list_skills_tool 查看全部技能"


def create_skill(name: str, description: str, instructions: str,
                 overwrite: bool = False) -> str:
    """创建一个新技能，或在技能已存在时（overwrite=true）整体替换其指令内容。
    name 为小写字母/数字/连字符（如 weekly-report）；description 一句话说明何时使用；
    instructions 为完整 Markdown 指令。技能已存在且未显式传 overwrite=true 时会拒绝
    （防止误覆盖）；更新场景请把 overwrite 设为 true 并在回复中说明将替换原内容。"""
    return skills.create_skill(name, description, instructions, overwrite=overwrite)


# ---------------------------------------------------------------- 自定义工具（对话式创建，免重启）

def list_custom_tools() -> str:
    """列出当前全部自定义工具（custom_tools/ 目录中的动态工具，含名称与功能描述）。"""
    items = custom_tools.list_custom_tools()
    if not items:
        return "当前没有自定义工具。可以用 create_custom_tool 工具创建一个。"
    return "\n".join(f"- {t['name']}: {t['description']}" for t in items)


async def create_custom_tool(name: str, code: str) -> str:
    """创建一个新的自定义工具（危险操作：写入可执行 Python 代码，执行前需用户批准）。
    name 为小写字母/数字/下划线（如 dice_roll）；code 为完整 Python 源文件，
    必须定义一个与 name 同名、带 docstring 的顶层函数（docstring 即工具描述，会展示给模型），
    参数带类型注解、返回 str。创建后无需重启，下一轮对话即可调用。"""
    err = custom_tools.validate_tool_code(name, code)
    if err:
        return f"创建失败：{err}"
    return approval.require_approval(
        "create_custom_tool", {"name": name, "code_preview": code[:300]},
        "工具创建", lambda: custom_tools.write_custom_tool(name, code))


async def delete_custom_tool(name: str) -> str:
    """删除一个已存在的自定义工具（危险操作，执行前需用户批准）。删除后无需重启，下一轮对话起不再可调用。"""
    if not any(t["name"] == name for t in custom_tools.list_custom_tools()):
        return f"自定义工具「{name}」不存在，可用 list_custom_tools 查看全部自定义工具"
    return approval.require_approval(
        "delete_custom_tool", {"name": name}, "工具删除",
        lambda: custom_tools.delete_custom_tool_file(name))


# ---------------------------------------------------------------- 领域包管理

def list_packs() -> str:
    """列出全部领域包（Domain Pack）及其状态：名称、版本、描述、是否启用、包含的工具与技能。领域包是通用内核之上的可插拔领域能力单元。"""
    items = packs.list_packs()
    if not items:
        return "当前没有领域包（packs/ 目录为空）。"
    lines = []
    trust_map = {"signed": "🔏已签名", "unsigned": "🔓未签名",
                 "invalid": "⚠️签名无效", "signed-unverified": "🔏已签名(未校验)"}
    for p in items:
        if p["status"] != "ok":
            state = f"⚠️ 不可用（{p['error']}）"
        elif p.get("platform"):
            state = "🏛 平台包（始终启用）"
        else:
            state = "✅ 已启用" if p["enabled"] else "⏸ 已禁用"
        caps = []
        if p["tools"]:
            caps.append("工具: " + ", ".join(p["tools"]))
        if p["skills"]:
            caps.append("技能: " + ", ".join(p["skills"]))
        if p.get("permissions"):
            caps.append("权限声明: " + ", ".join(p["permissions"]))
        caps.append(trust_map.get(p.get("trust", "unsigned"), "🔓未签名"))
        lines.append(f"- **{p['name']}** v{p['version']} {state}\n"
                     f"  {p['description']}" + ("\n  " + "；".join(caps) if caps else ""))
    return "\n".join(lines)


async def enable_pack(name: str) -> str:
    """启用一个领域包（危险操作：会为 Agent 挂载新的可执行工具，执行前需用户批准）。name 为包名（如 weather-ops），可用 list_packs 查看。启用后无需重启立即生效（运行时内存态）。"""
    name = name.strip().lower()  # 统一规范化：检查与执行用同一个名字
    known = {p["name"] for p in packs.list_packs()}
    if name not in known:
        return f"领域包「{name}」不存在，可用 list_packs 查看全部领域包"

    def _do():
        result = packs.enable_pack(name)
        _reload_graph()  # 立即重建图，免重启生效
        return result

    # 审批卡片上展示信任状态与权限声明，让审批人知情决策
    info = {p["name"]: p for p in packs.list_packs()}.get(name, {})
    args = {"name": name, "trust": info.get("trust", "unsigned"),
            "permissions": info.get("permissions", [])}
    return approval.require_approval("enable_pack", args, "启用", _do)


async def disable_pack(name: str) -> str:
    """禁用一个领域包（危险操作：会改变 Agent 能力边界，执行前需用户批准）。禁用后该包的工具/技能/提示词立即从 Agent 移除，无需重启（运行时内存态，重启后按 ENABLED_PACKS 配置恢复）。平台包（platform=true，跨领域通用能力）不可禁用。"""
    name = name.strip().lower()  # 统一规范化：检查与执行用同一个名字
    info = {p["name"]: p for p in packs.list_packs()}
    if name not in info:
        return f"领域包「{name}」不存在，可用 list_packs 查看全部领域包"
    if info[name].get("platform"):
        # 平台包治理规则：运行时不可禁用，直接拒绝（不发起审批）
        return packs.disable_pack(name)

    def _do():
        result = packs.disable_pack(name)
        _reload_graph()  # 立即重建图，免重启生效
        return result

    return approval.require_approval("disable_pack", {"name": name}, "禁用", _do)


async def create_pack(name: str, description: str, prompt: str = "",
                      tool_name: str = "", tool_code: str = "",
                      skill_name: str = "", skill_description: str = "",
                      skill_instructions: str = "",
                      env: dict = None,
                      requires_env: list = None,
                      requires_modules: list = None,
                      permissions: list = None,
                      platform: bool = False) -> str:
    """创建一个新的领域包（危险操作：会在 packs/ 下写入可执行代码与配置，执行前需用户批准）。
    name 为小写字母/数字/连字符（如 finance-ops）；description 一句话说明该领域包的能力；
    可同时携带一个初始工具（tool_name + tool_code，与 create_custom_tool 同样的代码约定：
    同名顶层函数、docstring 即描述、参数带类型注解、返回 str、文件完全自包含不得 import server 模块）、
    一个初始技能（skill_name + skill_description + skill_instructions）和领域提示词 prompt。
    包级配置（可选）：env 为环境变量默认值字典（仅外部未显式设置时注入，如 {"FINANCE_API_URL": "http://internal:8080"}）；
    requires_env 为必须由部署方提供的环境变量名列表（缺失则整包不加载）；
    requires_modules 为依赖的 Python 模块名列表（缺失则整包不加载，私有化环境不自动安装）；
    permissions 为权限声明列表（如 ["network", "filesystem"]，展示在审批卡片与 list_packs 上供审批人知情决策）；
    platform=True 创建平台包（跨领域通用基础能力，始终启用、运行时不可禁用，谨慎使用）。
    创建后无需重启，下一轮对话起自动挂载；强制信任模式（PACK_SIGNING_KEY）下落盘即自动签名。"""
    name = name.strip().lower()  # 统一规范化
    known = {p["name"] for p in packs.list_packs()}
    if name in known:
        return f"领域包「{name}」已存在，可用 list_packs 查看全部领域包"

    def _do():
        result = packs.create_pack(name, description, prompt, tool_name, tool_code,
                                   skill_name, skill_description, skill_instructions,
                                   env=env, requires_env=requires_env,
                                   requires_modules=requires_modules,
                                   permissions=permissions, platform=platform)
        _reload_graph()  # 立即重建图，免重启生效
        return result

    return approval.require_approval(
        "create_pack",
        {"name": name, "description": description,
         "with_tool": bool(tool_name), "with_skill": bool(skill_name),
         "permissions": permissions or [],
         "code_preview": tool_code[:300] if tool_code else ""},
        "创建", _do)


async def sign_pack(name: str) -> str:
    """为手工放入 packs/ 的领域包签名（危险操作：签名即授予信任，执行前需用户批准）。
    仅在配置 PACK_SIGNING_KEY 的强制信任模式（见 README「能力分层与信任模型」）下有意义；
    签名前请先人工审计包内代码——签名后任何修改都会使签名失效（篡改即自动失效）。"""
    name = name.strip().lower()
    known = {p["name"] for p in packs.list_packs()}
    if name not in known:
        return f"领域包「{name}」不存在，可用 list_packs 查看全部领域包"

    def _do():
        result = packs.sign_pack(name)
        _reload_graph()  # 签名后信任状态变化，重建图立即生效
        return result

    return approval.require_approval("sign_pack", {"name": name}, "签名", _do)


async def delete_pack(name: str) -> str:
    """删除一个领域包（危险操作：整个包目录连同工具/技能/提示词被删除且不可恢复，执行前需用户批准）。删除后无需重启，下一轮对话起其能力全部消失。"""
    name = name.strip().lower()  # 统一规范化
    known = {p["name"] for p in packs.list_packs()}
    if name not in known:
        return f"领域包「{name}」不存在，可用 list_packs 查看全部领域包"

    def _do():
        result = packs.delete_pack(name)
        _reload_graph()  # 立即重建图，免重启生效
        return result

    return approval.require_approval("delete_pack", {"name": name}, "删除", _do)


# ---------------------------------------------------------------- 多 Agent 编排

def _research_tools() -> list:
    """worker 调研员可用的安全工具子集（不含删文件/建工具等危险能力）。"""
    from langchain_core.tools import StructuredTool

    tools = [
        StructuredTool.from_function(
            func=read_text_file, name="read_text_file",
            description=read_text_file.__doc__),
        StructuredTool.from_function(
            func=list_uploaded_docs, name="list_uploaded_docs",
            description=list_uploaded_docs.__doc__),
        StructuredTool.from_function(
            func=save_output_file, name="save_output_file",
            description=save_output_file.__doc__),
    ]
    # 平台包/领域包工具也授予 worker（与主 Agent 一致的能力边界，重名不重复添加；
    # 时间/计算等通用能力自 v0.12.0 起来自平台包 core-utils，经此路径进入）
    have = {t.name for t in tools}
    for t in STATE.pack_tools.values():
        if t.name not in have:
            tools.append(t)
    for n in ("get_city_weather", "word_count", "list_workspace_files"):
        if n in STATE.mcp_tools:  # MCP 工具可用时一并授予 worker
            tools.append(STATE.mcp_tools[n])
    return tools


async def research_topic(topic: str) -> str:
    """多 Agent 并行调研：把一个复杂调研主题拆成 2~4 个子问题，多个调研 Agent 并行查证
    （各自可用天气/计算/读文件等工具），最后汇总为一份中文 Markdown 报告。
    适用于需要多角度查证、对比、汇总的复杂任务；简单问答、单点查询不要使用本工具。
    topic 为完整的调研主题描述。"""
    if STATE.llm is None:
        return "多 Agent 调研需要真实模型支持（当前为 Mock 模式）"
    from . import multi_agent
    try:
        return await multi_agent.run_research(topic, STATE.llm, _research_tools())
    except Exception as e:
        return f"多 Agent 调研执行失败: {e}"


# ---------------------------------------------------------------- 工具装配清单

BUILTIN_TOOLS = [read_text_file, list_uploaded_docs, save_output_file,
                 run_python_code, delete_workspace_file,
                 list_skills_tool, load_skill, create_skill,
                 list_custom_tools, create_custom_tool, delete_custom_tool,
                 list_packs, enable_pack, disable_pack, create_pack, delete_pack,
                 sign_pack, research_topic]


def builtin_langchain_tools():
    """包装为 LangChain StructuredTool（内置工具不依赖第三方，始终可用）。
    注意：通用业务能力（时间/计算等）不在此处——它们是平台包 core-utils 的工具，
    经 packs.load_pack_tools() 挂载，与内核运行时管理工具分层。"""
    from langchain_core.tools import StructuredTool

    return [
        StructuredTool.from_function(
            func=read_text_file, name="read_text_file",
            description=read_text_file.__doc__),
        StructuredTool.from_function(
            func=list_uploaded_docs, name="list_uploaded_docs",
            description=list_uploaded_docs.__doc__),
        StructuredTool.from_function(
            func=save_output_file, name="save_output_file",
            description=save_output_file.__doc__),
        StructuredTool.from_function(
            func=run_python_code, name="run_python_code",
            description=run_python_code.__doc__),
        StructuredTool.from_function(
            coroutine=delete_workspace_file, name="delete_workspace_file",
            description=delete_workspace_file.__doc__),
        StructuredTool.from_function(
            func=list_skills_tool, name="list_skills_tool",
            description=list_skills_tool.__doc__),
        StructuredTool.from_function(
            func=load_skill, name="load_skill",
            description=load_skill.__doc__),
        StructuredTool.from_function(
            func=create_skill, name="create_skill",
            description=create_skill.__doc__),
        StructuredTool.from_function(
            func=list_custom_tools, name="list_custom_tools",
            description=list_custom_tools.__doc__),
        StructuredTool.from_function(
            coroutine=create_custom_tool, name="create_custom_tool",
            description=create_custom_tool.__doc__),
        StructuredTool.from_function(
            coroutine=delete_custom_tool, name="delete_custom_tool",
            description=delete_custom_tool.__doc__),
        StructuredTool.from_function(
            func=list_packs, name="list_packs",
            description=list_packs.__doc__),
        StructuredTool.from_function(
            coroutine=enable_pack, name="enable_pack",
            description=enable_pack.__doc__),
        StructuredTool.from_function(
            coroutine=disable_pack, name="disable_pack",
            description=disable_pack.__doc__),
        StructuredTool.from_function(
            coroutine=create_pack, name="create_pack",
            description=create_pack.__doc__),
        StructuredTool.from_function(
            coroutine=delete_pack, name="delete_pack",
            description=delete_pack.__doc__),
        StructuredTool.from_function(
            coroutine=sign_pack, name="sign_pack",
            description=sign_pack.__doc__),
        StructuredTool.from_function(
            coroutine=research_topic, name="research_topic",
            description=research_topic.__doc__),
    ]
