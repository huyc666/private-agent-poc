"""4.1 流程定义与固化：列举工具可见性与新工具边界（spec AC6/AC8）。

红测试口径：实现前 list_orchestrations/list_workflow_instances 不存在
（AttributeError），新工具未登记 BUILTIN_TOOLS 时定义声明它的报错文案
落在「不存在」分支而非「不在编排安全清单内」。
"""
import pytest

from server import config, orchestration as orch, tools_admin, usage


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _definition(name, tools=None, params=None) -> dict:
    d = {
        "name": name, "version": "1", "description": f"{name} 描述",
        "roles": [{"id": "r", "prompt": "执行", "tools": tools or ["read_text_file"]}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }
    if params is not None:
        d["params"] = params
        d["task_template"] = "任务 {{" + params[0]["name"] + "}}"
    return d


def _seed_three_scopes():
    orch.save_definition(_definition("global-demo"), scope=None)
    orch.save_definition(
        _definition("alice-demo", params=[{"name": "week", "type": "string"}]),
        scope=config.user_scope("alice"))
    orch.save_definition(_definition("bob-demo"), scope=config.user_scope("bob"))


# ---------------------------------------------------------------- AC8 可见性


def test_ac8_list_orchestrations_user_sees_own_and_global():
    _seed_three_scopes()
    usage.current_identity.set(_identity("alice"))
    out = orch.list_orchestrations()
    assert "global-demo" in out and "alice-demo" in out
    assert "bob-demo" not in out
    # 参数声明随列举输出
    assert "week" in out


def test_ac8_list_orchestrations_approver_and_admin_see_all():
    _seed_three_scopes()
    for role in ("approver", "admin"):
        usage.current_identity.set(_identity("carol", role=role))
        out = orch.list_orchestrations()
        assert "global-demo" in out, role
        assert "alice-demo" in out and "bob-demo" in out, role


def test_ac8_list_orchestrations_open_mode_global_only(monkeypatch):
    _seed_three_scopes()
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    usage.current_identity.set(None)
    usage.current_user.set("anonymous")
    out = orch.list_orchestrations()
    assert "global-demo" in out
    assert "alice-demo" not in out and "bob-demo" not in out


def test_ac8_list_orchestrations_empty_and_invalid_definition_yaml():
    usage.current_identity.set(_identity("alice"))
    out = orch.list_orchestrations()
    assert isinstance(out, str) and out  # 空目录也给可用文案，不抛错
    # 损坏的个人定义文件不得让列举崩溃（跳过即可）
    bad_dir = config.PERSONAL_ORCHESTRATIONS_DIR / config.user_scope("alice")
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "broken.yaml").write_text("name: [unclosed", encoding="utf-8")
    out2 = orch.list_orchestrations()
    assert isinstance(out2, str) and "broken" not in out2


# ---------------------------------------------------------------- AC6 新工具边界


def test_ac6_listing_tools_not_in_safe_lists():
    for lst in (orch.SAFE_BUILTIN_TOOL_NAMES, orch.SAFE_PACK_TOOL_NAMES,
                orch.SAFE_MCP_TOOL_NAMES):
        assert "list_orchestrations" not in lst
        assert "list_workflow_instances" not in lst


@pytest.mark.parametrize("tool_name", ["list_orchestrations",
                                       "list_workflow_instances"])
def test_ac6_definition_declaring_listing_tools_rejected(tool_name):
    d = _definition("bad-demo", tools=[tool_name])
    errors = orch.validate_definition(d)
    assert errors, f"声明 {tool_name} 应被拒绝"
    assert any("安全清单" in e for e in errors), errors


def test_ac6_tools_registered_builtin():
    names = {getattr(t, "name", None) or getattr(t, "__name__", "")
             for t in tools_admin.BUILTIN_TOOLS}
    assert "list_orchestrations" in names
    assert "list_workflow_instances" in names
    langchain_names = {t.name for t in tools_admin.builtin_langchain_tools()}
    assert "list_orchestrations" in langchain_names
    assert "list_workflow_instances" in langchain_names


def test_ac6_tools_admin_wrappers_delegate():
    # tools_admin 包装层与注册入口同语义委托到 orchestration（工具装配可用）
    import asyncio

    usage.current_identity.set(_identity("alice"))
    assert isinstance(tools_admin.list_orchestrations(), str)
    assert isinstance(tools_admin.list_workflow_instances(), str)
    # Mock 模式（conftest 默认）下触发：包装层透传提示文案
    out = asyncio.run(tools_admin.run_orchestration("any-demo", "任务"))
    assert "Mock" in out
