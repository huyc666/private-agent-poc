"""编排身份与权限传递（v0.17.44，spec §3 / AC4–AC8）。

必测拒绝路径：
- 低权限发起编排，子任务越权工具被拒绝（装配层 + 直调层 + 无数据泄漏）；
- 定义作者高权限、发起者低权限，检索范围按发起者收窄。
身份必须是显式不可变快照，不依赖调用栈恰好共享 ContextVar。
"""
import asyncio
from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool

from server import auth, config, custom_tools, orchestration as orch, packs, usage
from server import state


def _identity(username="alice", role="user", department="finance",
              auth_source="bearer") -> usage.Identity:
    return usage.Identity(username=username, role=role,
                          department=department, auth_source=auth_source)


def _pack_info(name: str, tools: list[str], *, roles=None, departments=None) -> dict:
    return {
        "name": name,
        "status": "ok",
        "tools": [Path(f"{t}.py") for t in tools],
        "allowed_roles": roles or [],
        "allowed_departments": departments or [],
        "platform": False,
        "prompt": None,
    }


def _guarded_pack_tool(tool_name: str, pack: dict, result="检索命中：员工名单") -> StructuredTool:
    tool = StructuredTool.from_function(
        func=lambda: result,
        name=tool_name,
        description="HR 员工检索工具")
    return custom_tools._guard_pack_tool(
        tool, pack["name"], pack["allowed_roles"], pack["allowed_departments"])


def _install_fake_react(monkeypatch, *, answer="子任务结果"):
    """替换 create_react_agent；记录调用时的身份快照与绑定工具。"""
    import langgraph.prebuilt as prebuilt
    made = []

    class _Message:
        def __init__(self, text):
            self.content = text
            self.type = "ai"

    class FakeAgent:
        def __init__(self, llm, tools):
            self.llm = llm
            self.tools = list(tools)
            self.calls = []
            made.append(self)

        async def ainvoke(self, payload, config=None):
            self.calls.append({
                "identity": usage.current_identity.get(),
                "user": usage.current_user.get(),
                "role": auth.current_role(),
                "department": auth.current_department(),
                "tools": [t.name for t in self.tools],
                "messages": payload["messages"],
                "config": config,
            })
            return {"messages": [_Message(answer)]}

    monkeypatch.setattr(prebuilt, "create_react_agent", FakeAgent)
    return made


def _install_pack(monkeypatch, pack: dict, tools: dict):
    monkeypatch.setattr(state.STATE, "pack_tools", dict(tools))
    monkeypatch.setattr(packs, "enabled_packs", lambda: [pack])
    # 测试夹具中的包工具需显式进入静态安全清单（生产常量只含只读工具）
    monkeypatch.setattr(orch, "SAFE_PACK_TOOL_NAMES", tuple(tools), raising=False)


def _definition(tools: list[str], *, steps: int = 1) -> dict:
    return {
        "name": "identity-demo",
        "version": "1",
        "roles": [{"id": "r", "name": "角色", "prompt": "执行任务", "tools": tools}],
        "steps": [{"id": f"s{i}", "role": "r", "mode": "single"}
                  for i in range(steps)],
    }


def test_subagent_receives_initiator_identity_snapshot_not_ambient(monkeypatch):
    made = _install_fake_react(monkeypatch)
    identity = _identity(username="alice", role="user", department="finance")
    usage.current_identity.set(None)
    usage.current_user.set("anonymous")

    result = asyncio.run(orch.execute(_definition(["read_text_file"]), "做任务",
                                     identity=identity, session_id="s1", run_id="r1",
                                     llm=object()))
    assert result["ok"] is True
    rec = made[0].calls[0]
    assert rec["identity"] == identity
    assert rec["user"] == "alice"
    assert rec["role"] == "user"
    assert rec["department"] == "finance"


def test_identity_is_reapplied_before_each_step(monkeypatch):
    made = _install_fake_react(monkeypatch)
    identity = _identity(username="bob", role="user", department="ops")
    result = asyncio.run(orch.execute(_definition(["read_text_file"], steps=2),
                                     "做任务", identity=identity, llm=object()))
    assert result["ok"] is True
    assert len(made) == 2
    for agent in made:
        assert agent.calls[0]["identity"] == identity
        assert agent.calls[0]["user"] == "bob"


def test_low_privilege_initiator_privileged_tool_denied(monkeypatch):
    """必测：低权限用户发起编排，子任务越权工具在装配层与直调层都被拒绝。"""
    pack = _pack_info("hr-pack", ["hr_search"], roles=["approver"])
    guarded = _guarded_pack_tool("hr_search", pack)
    _install_pack(monkeypatch, pack, {"hr_search": guarded})

    low = _identity(username="alice", role="user", department="finance")
    role = {"id": "worker", "prompt": "查员工", "tools": ["hr_search", "read_text_file"]}

    # 装配层：越权工具不进子 agent；安全工具仍可用
    bound = orch.tools_for_role(role, low)
    assert [t.name for t in bound] == ["read_text_file"]

    made = _install_fake_react(monkeypatch)
    result = asyncio.run(orch.execute(
        {"name": "deny-demo", "version": "1", "roles": [role],
         "steps": [{"id": "s", "role": "worker", "mode": "single"}]},
        "查员工", identity=low, llm=object()))
    assert result["ok"] is True
    assert made[0].calls[0]["tools"] == ["read_text_file"]
    assert "hr_search" not in made[0].calls[0]["tools"]

    # 直调层：即便拿到工具对象，按当前身份调用也被拒绝，且无数据泄漏
    usage.current_identity.set(low)
    usage.current_user.set(low.username)
    direct = guarded.invoke({})
    expected = custom_tools.pack_scope_denied(
        pack["name"], pack["allowed_roles"], pack["allowed_departments"],
        role=low.role, department=low.department)
    assert expected is not None
    assert direct == expected
    assert "员工名单" not in direct


def test_definition_author_privilege_does_not_expand_initiator_kb_scope(monkeypatch):
    """必测：定义作者是高权限 approver，发起者是低权限 user/其他部门，检索收窄。"""
    pack = _pack_info("hr-pack", ["hr_search"], departments=["hr"])
    guarded = _guarded_pack_tool("hr_search", pack)
    _install_pack(monkeypatch, pack, {"hr_search": guarded})
    role = {"id": "worker", "prompt": "查员工", "tools": ["hr_search"]}

    finance = _identity(username="alice", role="user", department="finance")
    hr = _identity(username="carol", role="user", department="hr")

    assert orch.tools_for_role(role, finance) == []
    assert [t.name for t in orch.tools_for_role(role, hr)] == ["hr_search"]

    usage.current_identity.set(finance)
    usage.current_user.set(finance.username)
    denied = guarded.invoke({})
    assert "员工名单" not in denied

    usage.current_identity.set(hr)
    usage.current_user.set(hr.username)
    assert "员工名单" in guarded.invoke({})


def test_cache_key_includes_department(monkeypatch):
    """同一 hash+用户，部门不同不能复用已过滤结果。"""
    pack = _pack_info("hr-pack", ["hr_search"], departments=["hr"])
    guarded = _guarded_pack_tool("hr_search", pack)
    _install_pack(monkeypatch, pack, {"hr_search": guarded})

    finance = _identity(username="alice", role="user", department="finance")
    hr = _identity(username="alice", role="user", department="hr")
    definition = {"name": "cache-demo", "version": "1",
                  "roles": [{"id": "w", "prompt": "x", "tools": ["hr_search"]}],
                  "steps": [{"id": "s", "role": "w", "mode": "single"}]}

    assert orch.role_tools(definition, "w", finance) == []
    assert [t.name for t in orch.role_tools(definition, "w", hr)] == ["hr_search"]


def test_cache_key_includes_role_id(monkeypatch):
    """同一身份下两个角色声明不同工具，不能因缓存复用互相串工具集。"""
    pack = _pack_info("any-pack", ["any_search"])
    guarded = _guarded_pack_tool("any_search", pack, result="ok")
    _install_pack(monkeypatch, pack, {"any_search": guarded})
    ident = _identity()
    definition = {
        "name": "role-cache-demo", "version": "1",
        "roles": [
            {"id": "reader", "prompt": "读", "tools": ["read_text_file"]},
            {"id": "searcher", "prompt": "搜", "tools": ["any_search"]},
        ],
        "steps": [{"id": "a", "role": "reader", "mode": "single"},
                  {"id": "b", "role": "searcher", "mode": "single"}],
    }
    assert [t.name for t in orch.role_tools(definition, "reader", ident)] \
        == ["read_text_file"]
    assert [t.name for t in orch.role_tools(definition, "searcher", ident)] \
        == ["any_search"]


def test_pack_tool_outside_static_allowlist_is_not_available(monkeypatch):
    """R3：新包工具默认不进编排，必须显式评审加入静态清单。"""
    pack = _pack_info("future-pack", ["future_writer"])
    tool = _guarded_pack_tool("future_writer", pack, result="wrote")
    monkeypatch.setattr(state.STATE, "pack_tools", {"future_writer": tool})
    monkeypatch.setattr(packs, "enabled_packs", lambda: [pack])
    monkeypatch.setattr(orch, "SAFE_PACK_TOOL_NAMES", (), raising=False)

    assert "future_writer" not in orch.available_tools()
    errors = orch.validate_definition(_definition(["future_writer"]))
    assert any("future_writer" in e for e in errors)


def test_personal_assets_are_not_borrowed_from_definition_author(monkeypatch):
    """编排工具清单只认安全工具/包工具，不加载任何人的个人技能或自定义工具。"""
    from server import state
    monkeypatch.setattr(state.STATE, "pack_tools", {})
    monkeypatch.setattr(packs, "enabled_packs", lambda: [])

    orch_dir = config.PERSONAL_CUSTOM_TOOLS_DIR / "alice"
    orch_dir.mkdir(parents=True, exist_ok=True)
    (orch_dir / "secret_tool.py").write_text("def run():\n    return 'secret'\n",
                                             encoding="utf-8")

    data = _definition(["secret_tool"])
    errors = orch.validate_definition(data)
    assert any("secret_tool" in e for e in errors)
    assert "secret_tool" not in orch.available_tools()
    assert orch.tools_for_role(data["roles"][0], _identity()) == []


def test_approver_author_does_not_escalate_user_run(monkeypatch):
    made = _install_fake_react(monkeypatch, answer="step")
    identity = _identity(username="alice", role="user", department="finance")
    result = asyncio.run(orch.execute(_definition(["read_text_file"], steps=2),
                                     "做任务", identity=identity, llm=object()))
    assert result["ok"] is True
    for agent in made:
        assert agent.calls[0]["role"] == "user"
        assert agent.calls[0]["identity"].role == "user"


def test_open_mode_identity_is_explicit_snapshot(monkeypatch):
    made = _install_fake_react(monkeypatch, answer="ok")
    identity = usage.Identity(username="guest", role="admin", department="",
                              auth_source="open")
    result = asyncio.run(orch.execute(_definition(["read_text_file"]), "任务",
                                     identity=identity, llm=object()))
    assert result["ok"] is True
    rec = made[0].calls[0]
    assert rec["identity"] == identity
    assert rec["identity"].auth_source == "open"


@pytest.mark.parametrize("role,department,expected", [
    ("approver", "hr", True),
    ("user", "hr", True),
    ("user", "finance", False),
    ("admin", "", True),
])
def test_assembly_verdict_matches_execution_guard(monkeypatch, role, department, expected):
    pack = _pack_info("hr-pack", ["hr_search"], departments=["hr"])
    guarded = _guarded_pack_tool("hr_search", pack)
    _install_pack(monkeypatch, pack, {"hr_search": guarded})
    ident = _identity(role=role, department=department)
    bound = [t.name for t in orch.tools_for_role(
        {"id": "w", "prompt": "x", "tools": ["hr_search"]}, ident)]
    assert (bound == ["hr_search"]) is expected

    usage.current_identity.set(ident)
    usage.current_user.set(ident.username)
    denied = custom_tools.pack_scope_denied(
        pack["name"], pack["allowed_roles"], pack["allowed_departments"],
        role=ident.role, department=ident.department)
    assert (denied is None) is expected
