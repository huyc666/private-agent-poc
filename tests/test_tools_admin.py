"""tools_admin：管理类工具包装层——技能/自定义工具/领域包生命周期、
worker 安全工具装配、多 Agent 编排入口、内置工具装配清单。审批分支以
patch_interrupt 模拟用户决议（与 test_approval 同一模式）。"""
import asyncio

import pytest
import yaml

from server import approval, config, custom_tools, packs, skills, tools_admin
from server.state import STATE

TOOL_SRC = 'def greet(name: str) -> str:\n    """问候"""\n    return f"hi {name}"\n'


def _code_for(name: str) -> str:
    """按工具名生成合法源码（文件名 = 函数名是落盘校验约定）。"""
    return (f'def {name}(x: int = 1) -> str:\n'
            f'    """演示工具。"""\n'
            f'    return f"x={{x}}"\n')


def _valid_manifest(name: str, **extra) -> dict:
    m = {"name": name, "version": "0.1.0", "description": "测试领域包",
         "core_version": ">=0.1.0"}
    m.update(extra)
    return m


def _mk_pack(name: str, manifest: dict, files: dict | None = None):
    """在 tmp PACKS_DIR 下落一个包（与 test_packs 同一约定）。"""
    d = config.PACKS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for rel, text in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    packs.invalidate_scan_cache()
    return d


@pytest.fixture
def patch_interrupt(monkeypatch):
    """让审批门立即返回给定决议（模拟用户在审批卡片点击）。"""
    import langgraph.types as lt

    def _patch(decision):
        monkeypatch.setattr(lt, "interrupt", lambda payload: decision)

    return _patch


# ---------------------------------------------------------------- 技能工具

def test_list_skills_tool_empty_and_created(login_as):
    login_as("alice")
    assert "当前没有可用技能" in tools_admin.list_skills_tool()
    skills.create_skill("al-skill", "演示技能", "# 指令")
    out = tools_admin.list_skills_tool()
    assert "al-skill" in out and "演示技能" in out


def test_load_skill_roundtrip_and_missing(login_as):
    login_as("alice")
    tools_admin.create_skill("demo-skill", "演示", "# 完整指令")
    assert "# 完整指令" in tools_admin.load_skill("demo-skill")
    out = tools_admin.load_skill("ghost-skill")
    assert "不存在" in out


# ---------------------------------------------------------------- 自定义工具视图

def test_list_custom_tools_personal_view(login_as):
    login_as("alice")                                # user 角色：本人专属平铺
    custom_tools.write_custom_tool(
        "mine_tool", _code_for("mine_tool"), scope=config.user_scope("alice"))
    out = tools_admin.list_custom_tools()
    assert "mine_tool" in out and "本人专属" in out


def test_list_custom_tools_admin_global_view(login_as):
    login_as("boss", role="approver")                # 全局视图：带「域/」前缀
    custom_tools.write_custom_tool("shared_name", _code_for("shared_name"), scope="alice")
    out = tools_admin.list_custom_tools()
    assert "alice/shared_name" in out and "专属域" in out


def test_list_custom_tools_empty_hint():
    assert "当前没有自定义工具" in tools_admin.list_custom_tools()


def test_list_custom_tools_open_mode(monkeypatch):
    """开放模式：无专属域概念，不列专属工具、不误建 personal 目录。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    assert "当前没有自定义工具" in tools_admin.list_custom_tools()
    assert not config.PERSONAL_CUSTOM_TOOLS_DIR.exists()


# ---------------------------------------------------------------- 自定义工具创建/删除/调用

def test_create_custom_tool_personal_no_approval(login_as):
    login_as("alice")                                # user：专属域自助落盘
    alice = config.user_scope("alice")
    out = asyncio.run(tools_admin.create_custom_tool("al_tool", _code_for("al_tool")))
    assert f"custom_tools_personal/{alice}/al_tool.py" in out
    assert (config.PERSONAL_CUSTOM_TOOLS_DIR / alice / "al_tool.py").is_file()


def test_create_custom_tool_global_gated(login_as, patch_interrupt):
    login_as("boss", role="approver")                # 通用工具：审批门
    patch_interrupt({"approve": True})
    out = asyncio.run(tools_admin.create_custom_tool("glob_tool", _code_for("glob_tool")))
    assert "custom_tools/glob_tool.py" in out
    patch_interrupt({"approve": False})              # 拒绝 → 不落盘
    out = asyncio.run(tools_admin.create_custom_tool("glob2", _code_for("glob2")))
    assert "拒绝了" in out
    assert not (config.CUSTOM_TOOLS_DIR / "glob2.py").exists()


def test_create_custom_tool_invalid_code(login_as):
    login_as("alice")
    out = asyncio.run(tools_admin.create_custom_tool("bad_tool", "def broken(:"))
    assert "创建失败" in out


def test_delete_custom_tool_personal_self_service(login_as):
    login_as("alice")
    custom_tools.write_custom_tool(
        "al_tool", _code_for("al_tool"), scope=config.user_scope("alice"))
    out = asyncio.run(tools_admin.delete_custom_tool("al_tool"))
    assert "已删除" in out


def test_delete_custom_tool_global_gated(login_as, patch_interrupt):
    login_as("boss", role="approver")
    custom_tools.write_custom_tool("glob_tool", _code_for("glob_tool"))
    patch_interrupt({"approve": False})              # 拒绝 → 保留
    out = asyncio.run(tools_admin.delete_custom_tool("glob_tool"))
    assert "拒绝了" in out
    assert (config.CUSTOM_TOOLS_DIR / "glob_tool.py").is_file()


def test_delete_custom_tool_missing(login_as):
    login_as("alice")
    out = asyncio.run(tools_admin.delete_custom_tool("ghost"))
    assert "不存在" in out


def test_run_custom_tool_missing(login_as):
    login_as("alice")
    out = asyncio.run(tools_admin.run_custom_tool("ghost_tool", {}))
    assert "不存在" in out


# ---------------------------------------------------------------- 领域包管理

def test_list_packs_empty_hint():
    assert "当前没有领域包" in tools_admin.list_packs()


def test_list_packs_states_and_trust():
    _mk_pack("normal-ops", _valid_manifest("normal-ops"))
    _mk_pack("plat-ops", _valid_manifest("plat-ops", platform=True))
    _mk_pack("off-ops", _valid_manifest("off-ops"))
    packs._disabled.add("off-ops")
    _mk_pack("broken-ops", _valid_manifest("broken-ops",
              requires_env=["PA_MISSING_ENV_XYZ"]))   # 依赖缺失 → 不可用
    out = tools_admin.list_packs()
    assert "**normal-ops**" in out and "✅ 已启用" in out
    assert "🏛 平台包" in out
    assert "⏸ 已禁用" in out
    assert "⚠️ 不可用" in out
    assert "🔓未签名" in out                          # 未配签名密钥


def test_enable_pack_missing_and_gated(login_as, patch_interrupt):
    login_as("boss", role="approver")
    assert "不存在" in asyncio.run(tools_admin.enable_pack("ghost"))
    _mk_pack("gate-ops", _valid_manifest("gate-ops", tools=["tools/greet.py"]),
             {"tools/greet.py": TOOL_SRC})
    patch_interrupt({"approve": True})
    out = asyncio.run(tools_admin.enable_pack("gate-ops"))
    assert "greet" in STATE.tool_names                 # _reload_graph 免重启挂载


def test_disable_pack_platform_rejected_directly(monkeypatch):
    """平台包运行时不可禁用：直接拒绝，不发起审批。"""
    _mk_pack("plat-ops", _valid_manifest("plat-ops", platform=True))
    import langgraph.types as lt
    fired = []

    def _spy(payload):                                 # 探针：不应被调用
        fired.append(payload)
        return {"approve": True}

    monkeypatch.setattr(lt, "interrupt", _spy)
    out = asyncio.run(tools_admin.disable_pack("plat-ops"))
    assert "平台包" in out and "不可禁用" in out
    assert fired == []                                 # 未发起审批


def test_disable_pack_gated_roundtrip(login_as, patch_interrupt):
    login_as("boss", role="approver")
    _mk_pack("gate-ops", _valid_manifest("gate-ops"))
    patch_interrupt({"approve": True})
    asyncio.run(tools_admin.disable_pack("gate-ops"))
    assert "⏸ 已禁用" in tools_admin.list_packs()
    patch_interrupt({"approve": True})
    asyncio.run(tools_admin.enable_pack("gate-ops"))
    assert "✅ 已启用" in tools_admin.list_packs()


def test_create_pack_missing_and_gated(login_as, patch_interrupt):
    login_as("boss", role="approver")
    _mk_pack("dup-ops", _valid_manifest("dup-ops"))
    assert "已存在" in asyncio.run(tools_admin.create_pack(
        "dup-ops", "重复包"))
    patch_interrupt({"approve": True})
    out = asyncio.run(tools_admin.create_pack(
        "  New-Ops  ", "新包", tool_name="greet", tool_code=TOOL_SRC))
    assert "New-Ops" in out or "new-ops" in out       # 名字规范化
    assert (config.PACKS_DIR / "new-ops" / "pack.yaml").is_file()


def test_sign_pack_missing_and_signed(login_as, patch_interrupt, monkeypatch):
    login_as("boss", role="approver")
    assert "不存在" in asyncio.run(tools_admin.sign_pack("ghost"))
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack("sig-ops", _valid_manifest("sig-ops"))
    patch_interrupt({"approve": True})
    out = asyncio.run(tools_admin.sign_pack("sig-ops"))
    assert "已签名" in out
    assert "🔏已签名" in tools_admin.list_packs()


def test_delete_pack_missing_and_gated(login_as, patch_interrupt):
    login_as("boss", role="approver")
    assert "不存在" in asyncio.run(tools_admin.delete_pack("ghost"))
    d = _mk_pack("del-ops", _valid_manifest("del-ops"))
    patch_interrupt({"approve": True})
    out = asyncio.run(tools_admin.delete_pack("del-ops"))
    assert "已删除" in out or "删除" in out
    assert not d.exists()


# ---------------------------------------------------------------- worker 工具装配

def test_worker_tools_assembly(monkeypatch):
    from langchain_core.tools import StructuredTool

    def _f():
        return "x"
    fake_pack = StructuredTool.from_function(func=_f, name="pack_tool", description="d")
    fake_dup = StructuredTool.from_function(func=_f, name="read_text_file", description="d")
    fake_mcp = StructuredTool.from_function(func=_f, name="get_city_weather", description="d")
    saved = (dict(STATE.pack_tools), dict(STATE.mcp_tools))
    STATE.pack_tools = {"pack_tool": fake_pack, "read_text_file": fake_dup}
    STATE.mcp_tools = {"get_city_weather": fake_mcp}
    try:
        tools = tools_admin._worker_tools()
        names = [t.name for t in tools]
        assert {"read_text_file", "list_uploaded_docs", "save_output_file",
                "pack_tool", "get_city_weather"} <= set(names)
        assert names.count("read_text_file") == 1     # 与内置同名不重复挂载
        # 危险能力不给 worker（权限在架构层隔离，不靠 prompt 约束）
        assert "delete_workspace_file" not in names
        assert "run_python_code" not in names
        assert "create_skill" not in names
    finally:
        STATE.pack_tools, STATE.mcp_tools = saved


# ---------------------------------------------------------------- 多 Agent 入口

def test_orchestrate_subtasks_mock_mode():
    from server.state import STATE
    saved = STATE.llm
    STATE.llm = None
    try:
        out = asyncio.run(tools_admin.orchestrate_subtasks("随便什么"))
        assert "需要真实模型支持" in out
    finally:
        STATE.llm = saved


def test_orchestrate_subtasks_success_and_failure_sanitized(monkeypatch):
    from server.state import STATE
    saved = STATE.llm
    STATE.llm = object()                               # 有真实模型才进入编排
    try:
        async def _ok(task, llm, tools):
            return "整合结果正文"
        monkeypatch.setattr("server.multi_agent.run_orchestration", _ok)
        assert asyncio.run(tools_admin.orchestrate_subtasks("T")) == "整合结果正文"

        async def _boom(task, llm, tools):
            raise RuntimeError("部署路径/密钥等敏感细节")
        monkeypatch.setattr("server.multi_agent.run_orchestration", _boom)
        out = asyncio.run(tools_admin.orchestrate_subtasks("T"))
        assert "执行失败" in out
        assert "敏感细节" not in out                   # 异常原文不回显
    finally:
        STATE.llm = saved


# ---------------------------------------------------------------- 装配清单

def test_builtin_langchain_tools_registry():
    tools = tools_admin.builtin_langchain_tools()
    names = {t.name for t in tools}
    expected = {"read_text_file", "list_uploaded_docs", "save_output_file",
                "run_python_code", "delete_workspace_file",
                "list_skills_tool", "load_skill", "create_skill",
                "list_custom_tools", "create_custom_tool", "delete_custom_tool",
                "run_custom_tool",
                "list_packs", "enable_pack", "disable_pack", "create_pack",
                "delete_pack", "sign_pack", "orchestrate_subtasks"}
    assert expected <= names
    assert all(t.description for t in tools)          # docstring 即模型可见描述
