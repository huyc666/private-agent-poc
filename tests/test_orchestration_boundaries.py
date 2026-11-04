"""编排边界与拒绝路径（v0.17.44，spec AC9–AC15）。

覆盖：危险工具、沙箱令牌/失败、子进程环境剔除、代际栅栏、审批不旁路。
"""
import asyncio
import json
import urllib.error

import pytest
from langchain_core.tools import StructuredTool
from langchain_core.messages import AIMessage

from server import (approval, config, custom_tools, orchestration as orch,
                    packs, sessions, state, usage)


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _definition(tools: list[str], *, steps: int = 1, name="boundary-demo") -> dict:
    return {
        "name": name,
        "version": "1",
        "roles": [{"id": "r", "prompt": "执行", "tools": tools}],
        "steps": [{"id": f"s{i}", "role": "r", "mode": "single"}
                  for i in range(steps)],
    }


def _install_react(monkeypatch, factory=None, answer="ok"):
    import langgraph.prebuilt as prebuilt
    made = []

    class FakeAgent:
        def __init__(self, llm, tools):
            self.llm = llm
            self.tools = list(tools)
            made.append(self)

        async def ainvoke(self, payload, config=None):
            if factory is not None:
                return await factory(self, payload, config)
            return {"messages": [AIMessage(content=answer)]}

    monkeypatch.setattr(prebuilt, "create_react_agent", FakeAgent)
    return made


def _install_pack(monkeypatch, pack: dict, tools: dict):
    monkeypatch.setattr(state.STATE, "pack_tools", dict(tools))
    monkeypatch.setattr(packs, "enabled_packs", lambda: [pack])
    monkeypatch.setattr(orch, "SAFE_PACK_TOOL_NAMES", tuple(tools), raising=False)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_forbidden_tool_never_executes_even_if_injected(monkeypatch):
    marker = {"called": False}

    def _danger():
        marker["called"] = True
        return "已删除"

    dangerous = StructuredTool.from_function(
        func=_danger, name="delete_workspace_file", description="危险操作")
    guarded = orch.execution_guard(dangerous, _identity())
    out = guarded.invoke({})
    assert "拒绝" in out
    assert "delete_workspace_file" in out
    assert marker["called"] is False


def test_execution_guard_denies_tool_outside_safe_list(monkeypatch):
    """双层过滤：装配层之外，执行层也必须拒绝未在安全清单内的工具。"""
    marker = {"called": False}

    def _future():
        marker["called"] = True
        return "写了数据"

    future = StructuredTool.from_function(
        func=_future, name="future_writer", description="未来包工具")
    guarded = orch.execution_guard(future, _identity())
    out = guarded.invoke({})
    assert "拒绝" in out
    assert "future_writer" in out
    assert marker["called"] is False


def test_pack_tool_meta_keeps_first_pack_on_name_clash(monkeypatch):
    """同名工具元数据与 load_pack_tools 的先到先得一致，避免提示层用错包白名单。"""
    pack_a = {"name": "pack-a", "status": "ok", "tools": ["same.py"],
              "allowed_roles": [], "allowed_departments": [], "platform": False,
              "prompt": None}
    pack_b = {"name": "pack-b", "status": "ok", "tools": ["same.py"],
              "allowed_roles": [], "allowed_departments": [], "platform": False,
              "prompt": None}
    monkeypatch.setattr(packs, "enabled_packs", lambda: [pack_a, pack_b])
    assert orch._pack_tool_meta()["same"]["name"] == "pack-a"


def test_forbidden_tool_definition_rejected_before_any_run(monkeypatch):
    made = _install_react(monkeypatch)
    result = asyncio.run(orch.execute(
        _definition(["delete_workspace_file"]), "任务",
        identity=_identity(), llm=object()))
    assert result["ok"] is False
    assert "危险" in result["error"]
    assert made == []
    assert approval.pending_count() == 0


def test_sandbox_token_forwarded_on_orchestrated_tool_call(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["url"] = req.full_url
        return _Resp({"ok": True, "result": "echo-result"})

    monkeypatch.setattr(config, "SANDBOX_URL", "http://sandbox.local")
    monkeypatch.setattr(config, "SANDBOX_AUTH_TOKEN", "tok-123")
    monkeypatch.setattr(custom_tools.urllib.request, "urlopen", fake_urlopen)

    async def echo():
        return custom_tools._sandbox_run_tool("echo", {})

    pack = {"name": "echo-pack", "status": "ok", "tools": ["echo.py"],
            "allowed_roles": [], "allowed_departments": [], "platform": False,
            "prompt": None}
    tool = StructuredTool.from_function(coroutine=echo, name="echo",
                                        description="回显")
    _install_pack(monkeypatch, pack, {"echo": tool})

    async def factory(agent, payload, config=None):
        result = await agent.tools[0].ainvoke({})
        return {"messages": [AIMessage(content=result)]}

    _install_react(monkeypatch, factory=factory)
    result = asyncio.run(orch.execute(_definition(["echo"]), "任务",
                                     identity=_identity(), llm=object()))
    assert result["ok"] is True
    header_keys = {k.lower(): v for k, v in captured["headers"].items()}
    assert header_keys["x-sandbox-token"] == "tok-123"
    assert captured["url"].endswith("/run_tool")
    assert "echo-result" in result["summary"]


def test_sandbox_wrong_token_returns_failure_without_data(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr(config, "SANDBOX_URL", "http://sandbox.local")
    monkeypatch.setattr(config, "SANDBOX_AUTH_TOKEN", "wrong")
    monkeypatch.setattr(custom_tools.urllib.request, "urlopen", fake_urlopen)

    async def echo():
        return custom_tools._sandbox_run_tool("echo", {})

    pack = {"name": "echo-pack", "status": "ok", "tools": ["echo.py"],
            "allowed_roles": [], "allowed_departments": [], "platform": False,
            "prompt": None}
    tool = StructuredTool.from_function(coroutine=echo, name="echo",
                                        description="回显")
    _install_pack(monkeypatch, pack, {"echo": tool})

    async def factory(agent, payload, config=None):
        result = await agent.tools[0].ainvoke({})
        return {"messages": [AIMessage(content=result)]}

    _install_react(monkeypatch, factory=factory)
    result = asyncio.run(orch.execute(_definition(["echo"]), "任务",
                                     identity=_identity(), llm=object()))
    assert result["ok"] is True
    assert "沙箱调用失败" in result["summary"]
    assert "echo-result" not in result["summary"]


def test_sandbox_down_fails_closed_before_any_subagent(monkeypatch):
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "sandbox-down")
    made = _install_react(monkeypatch)
    result = asyncio.run(orch.execute(_definition(["read_text_file"]), "任务",
                                     identity=_identity(), llm=object()))
    assert result["ok"] is False
    assert "沙箱不可达" in result["error"]
    assert made == []


def test_child_process_env_excludes_sandbox_secrets(monkeypatch):
    import sandbox.server as sbx

    monkeypatch.setenv("SANDBOX_AUTH_TOKEN", "host-secret")
    monkeypatch.setenv("SANDBOX_OPEN_MODE", "true")
    code = ("import os; print('T=' + str(os.environ.get('SANDBOX_AUTH_TOKEN')));"
            "print('O=' + str(os.environ.get('SANDBOX_OPEN_MODE')));"
            "print('P=' + str(os.environ.get('PACK_FLAG')))")
    out = sbx._run_isolated({"code": code}, {"PACK_FLAG": "1"})
    assert "T=None" in out["output"]
    assert "O=None" in out["output"]
    assert "P=1" in out["output"]


def test_stale_generation_stops_next_step(monkeypatch):
    calls = {"n": 0}

    def fake_fence(session_id, generation):
        calls["n"] += 1
        return calls["n"] <= 1

    monkeypatch.setattr(sessions, "fence_ok", fake_fence)
    made = _install_react(monkeypatch)
    before = approval.pending_count()
    result = asyncio.run(orch.execute(
        _definition(["read_text_file"], steps=2), "任务",
        identity=_identity(), session_id="s1", generation=1, llm=object()))
    assert result["ok"] is False
    assert "会话" in result["error"]
    assert result["completed"] is False
    assert len(made) == 1
    # AC15：代际中止后不产生下一步产出，也不新增审批
    assert result["failed_step"] == "s1"
    assert "s1" not in result["outputs"]
    assert approval.pending_count() == before


def test_running_orchestration_does_not_create_approvals(monkeypatch):
    _install_react(monkeypatch)
    before = approval.pending_count()
    result = asyncio.run(orch.execute(_definition(["read_text_file"]), "任务",
                                     identity=_identity(), llm=object()))
    assert result["ok"] is True
    assert approval.pending_count() == before
