"""编排执行：等效性、显式数据流、并发上限、失败语义（v0.17.44，spec AC2/AC3/AC5）。

红测试口径：新引擎与旧 orchestrate_subtasks 在相同输入下产出相同的子任务集合
与汇总结果；任一步失败即停，不自动重试；并行峰值受定义与全局硬顶约束。
"""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from server import config, multi_agent, orchestration as orch, usage


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _research_definition(max_parallel=2, fanout_limit=2) -> dict:
    return {
        "name": "research-demo",
        "version": "1",
        "max_parallel": max_parallel,
        "roles": [
            {"id": "planner", "name": "规划", "prompt": "把任务拆成子任务", "tools": []},
            {"id": "worker", "name": "调研", "prompt": "执行子任务",
             "tools": ["read_text_file"]},
            {"id": "aggregator", "name": "汇总", "prompt": "汇总结果", "tools": []},
        ],
        "steps": [
            {"id": "plan", "role": "planner", "mode": "single"},
            {"id": "research", "role": "worker", "mode": "parallel",
             "fanout": {"from": "plan", "limit": fanout_limit},
             "inputs": ["plan"]},
        ],
        "aggregate": {"role": "aggregator", "inputs": ["research"]},
    }


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append({"messages": messages, "config": config})
        text = self.replies.pop(0)
        return AIMessage(content=text)


def _install_react(monkeypatch, *, answer="子任务结果", factory=None):
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
            self.calls.append(payload)
            if factory is not None:
                return await factory(self, payload, config)
            return {"messages": [_Message(answer)]}

    monkeypatch.setattr(prebuilt, "create_react_agent", FakeAgent)
    return made


def _text_of(payload) -> str:
    parts = []
    for m in payload["messages"]:
        parts.append(getattr(m, "content", str(m)))
    return "\n".join(parts)


def test_research_definition_equivalent_to_legacy_orchestrate_subtasks(monkeypatch):
    plan = json.dumps(["子任务甲", "子任务乙"], ensure_ascii=False)
    summary = "调研结论：甲乙均已完成"
    made_old = _install_react(monkeypatch, answer="子任务执行结果")
    old = asyncio.run(multi_agent.run_orchestration(
        "调研甲乙", _FakeLLM([plan, summary]), []))
    old_subtasks = json.loads(old.split("子任务清单：", 1)[1].split("\n", 1)[0])

    made_new = _install_react(monkeypatch, answer="子任务执行结果")
    new = asyncio.run(orch.execute(
        _research_definition(), "调研甲乙", identity=_identity(),
        llm=_FakeLLM([plan, summary])))

    assert new["ok"] is True
    assert new["subtasks"] == old_subtasks
    assert len(made_old) == len(old_subtasks)
    assert len(made_new) == len(old_subtasks)
    assert {f["result"] for f in new["findings"]} == {"子任务执行结果"}
    assert new["summary"] == summary
    assert summary in old


def test_serial_pipeline_explicit_inputs_and_save_output(monkeypatch):
    outputs = {}

    async def factory(agent, payload, config):
        text = _text_of(payload)
        if len(outputs) == 2:
            # 第三步：真实调用 save_output_file，验证身份域落盘
            save = next(t for t in agent.tools if t.name == "save_output_file")
            save.invoke({"name": "复核报告.md", "content": "复核通过"})
        outputs.setdefault(len(outputs) + 1, text)
        return {"messages": [AIMessage(content=f"第{len(outputs)}步输出")]}

    made = _install_react(monkeypatch, factory=factory)
    definition = {
        "name": "review-demo",
        "version": "1",
        "roles": [
            {"id": "reviewer", "prompt": "审核初稿", "tools": ["read_text_file", "save_output_file"]},
            {"id": "reviser", "prompt": "修订", "tools": ["read_text_file", "save_output_file"]},
            {"id": "checker", "prompt": "复核", "tools": ["read_text_file", "save_output_file"]},
        ],
        "steps": [
            {"id": "review", "role": "reviewer", "mode": "single"},
            {"id": "revise", "role": "reviser", "mode": "single", "inputs": ["review"]},
            {"id": "check", "role": "checker", "mode": "single", "inputs": ["revise"]},
        ],
    }
    result = asyncio.run(orch.execute(definition, "审核这份方案",
                                     identity=_identity(), llm=object()))
    assert result["ok"] is True
    assert len(made) == 3
    assert "第1步输出" in outputs[2]
    assert "第2步输出" in outputs[3]
    # 第三个 agent 落盘到发起者域
    scope = config.user_scope("alice")
    saved = config.OUTPUTS_DIR / scope / "复核报告.md"
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == "复核通过"


def test_parallel_peak_respects_definition_limit(monkeypatch):
    peak = {"value": 0, "active": 0}

    async def factory(agent, payload, config):
        peak["active"] += 1
        peak["value"] = max(peak["value"], peak["active"])
        await asyncio.sleep(0.02)
        peak["active"] -= 1
        return {"messages": [AIMessage(content="ok")]}

    _install_react(monkeypatch, factory=factory)
    plan = json.dumps(["甲", "乙", "丙", "丁"], ensure_ascii=False)
    result = asyncio.run(orch.execute(
        _research_definition(max_parallel=2, fanout_limit=4), "调研",
        identity=_identity(), llm=_FakeLLM([plan, "汇总"])))
    assert result["ok"] is True
    assert peak["value"] <= 2
    assert len(result["subtasks"]) == 4


def test_parallel_peak_respects_global_ceiling(monkeypatch):
    peak = {"value": 0, "active": 0}

    async def factory(agent, payload, config):
        peak["active"] += 1
        peak["value"] = max(peak["value"], peak["active"])
        await asyncio.sleep(0.02)
        peak["active"] -= 1
        return {"messages": [AIMessage(content="ok")]}

    _install_react(monkeypatch, factory=factory)
    plan = json.dumps([f"任务{i}" for i in range(6)], ensure_ascii=False)
    result = asyncio.run(orch.execute(
        _research_definition(max_parallel=4, fanout_limit=6), "调研",
        identity=_identity(), llm=_FakeLLM([plan, "汇总"])))
    assert result["ok"] is True
    assert peak["value"] <= orch.GLOBAL_MAX_PARALLEL
    # 全局硬顶只约束并发，不应把上游规划出的子任务数量截断
    assert len(result["subtasks"]) == 6


def test_fail_fast_stops_later_steps_and_marks_incomplete(monkeypatch):
    counter = {"n": 0}

    async def factory(agent, payload, config):
        counter["n"] += 1
        if counter["n"] == 2:
            raise RuntimeError("内部异常 C:\\secret\\trace")
        return {"messages": [AIMessage(content="ok")]}

    made = _install_react(monkeypatch, factory=factory)
    definition = {
        "name": "fail-demo",
        "version": "1",
        "roles": [
            {"id": "planner", "prompt": "拆解", "tools": []},
            {"id": "worker", "prompt": "执行", "tools": ["read_text_file"]},
            {"id": "checker", "prompt": "复核", "tools": ["read_text_file"]},
        ],
        "steps": [
            {"id": "plan", "role": "planner", "mode": "single"},
            {"id": "work", "role": "worker", "mode": "parallel",
             "fanout": {"from": "plan", "limit": 2}, "inputs": ["plan"]},
            {"id": "check", "role": "checker", "mode": "single", "inputs": ["work"]},
        ],
    }
    plan = json.dumps(["甲", "乙"], ensure_ascii=False)
    result = asyncio.run(orch.execute(definition, "任务", identity=_identity(),
                                      llm=_FakeLLM([plan])))
    assert result["ok"] is False
    assert result["failed_step"] == "work"
    assert result["completed"] is False
    assert "RuntimeError" in result["error"]
    assert "C:\\secret" not in result["error"]
    assert "plan" in result["outputs"]
    assert "work" not in result["outputs"]
    assert "check" not in result["outputs"]
    # 后续步骤没有启动（并行阶段最多两个 worker，且复核未调用）
    assert len(made) <= 2


def test_total_model_call_budget_fails_closed(monkeypatch):
    monkeypatch.setattr(orch, "MAX_MODEL_CALLS", 2)

    async def factory(agent, payload, config):
        await agent.llm.ainvoke(payload["messages"])
        return {"messages": [AIMessage(content="ok")]}

    _install_react(monkeypatch, factory=factory)
    plan = json.dumps(["甲", "乙", "丙"], ensure_ascii=False)
    result = asyncio.run(orch.execute(
        _research_definition(max_parallel=2, fanout_limit=3), "调研",
        identity=_identity(), llm=_FakeLLM([plan, "汇总"])))
    assert result["ok"] is False
    assert "上限" in result["error"]
    assert result["completed"] is False
    assert result["failed_step"] == "research"


def test_budget_counts_each_model_call_not_step(monkeypatch):
    """AC12：预算按真实模型调用计数，ReAct 每轮工具调用后的模型调用都要计入。"""
    monkeypatch.setattr(orch, "MAX_MODEL_CALLS", 2)

    async def factory(agent, payload, config):
        for _ in range(3):
            await agent.llm.ainvoke(payload["messages"])
        return {"messages": [AIMessage(content="ok")]}

    _install_react(monkeypatch, factory=factory)
    definition = {
        "name": "budget-demo", "version": "1",
        "roles": [{"id": "r", "prompt": "执行", "tools": ["read_text_file"]}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }
    result = asyncio.run(orch.execute(
        definition, "任务", identity=_identity(),
        llm=_FakeLLM(["a", "b", "c"])))
    assert result["ok"] is False
    assert "上限" in result["error"]
    assert result["failed_step"] == "s"


def test_single_step_without_tools_calls_llm_directly(monkeypatch):
    llm = _FakeLLM(["直接回答"])
    made = _install_react(monkeypatch)
    definition = {
        "name": "direct-demo", "version": "1",
        "roles": [{"id": "solo", "prompt": "回答", "tools": []}],
        "steps": [{"id": "s", "role": "solo", "mode": "single"}],
    }
    result = asyncio.run(orch.execute(definition, "问题", identity=_identity(), llm=llm))
    assert result["ok"] is True
    assert made == []
    assert llm.calls and result["summary"] == "直接回答"


# ---- 工具入口、定义解析与失败信封 ----

def _tool_definition(name="tool-demo") -> dict:
    return {
        "name": name, "version": "1",
        "roles": [{"id": "r", "prompt": "执行", "tools": ["read_text_file"]}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }


def test_run_orchestration_tool_mock_mode(monkeypatch):
    from server import state
    monkeypatch.setattr(state.STATE, "llm", None)
    assert "Mock" in asyncio.run(orch.run_orchestration("tool-demo", "任务"))


def test_run_orchestration_tool_success_and_missing_definition(monkeypatch):
    from server import state
    monkeypatch.setattr(state.STATE, "llm", object())
    _install_react(monkeypatch)
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")

    assert "编排未执行" in asyncio.run(orch.run_orchestration("nope", "任务"))
    orch.save_definition(_tool_definition(), scope=None)
    out = asyncio.run(orch.run_orchestration("tool-demo", "任务"))
    assert "多角色编排完成" in out
    assert "tool-demo" in out


def test_run_orchestration_tool_reports_failure(monkeypatch):
    from server import state

    async def boom(agent, payload, config=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(state.STATE, "llm", object())
    _install_react(monkeypatch, factory=boom)
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    orch.save_definition(_tool_definition("fail-tool"), scope=None)
    out = asyncio.run(orch.run_orchestration("fail-tool", "任务"))
    assert "执行失败" in out
    assert "RuntimeError" in out


def test_identity_from_context_falls_back_once_without_snapshot(monkeypatch):
    usage.current_identity.set(None)
    usage.current_user.set("alice")
    ident = orch._identity_from_context()
    assert isinstance(ident, usage.Identity)
    assert ident.username == "alice"


def test_resolve_definition_personal_wins_and_invalid_name(monkeypatch):
    ident = _identity(username="alice")
    scope = config.user_scope("alice")
    personal_path = config.PERSONAL_ORCHESTRATIONS_DIR / scope / "same.yaml"
    personal_path.parent.mkdir(parents=True, exist_ok=True)
    personal_path.write_text("name: same\n", encoding="utf-8")
    global_path = config.ORCHESTRATIONS_DIR / "same.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text("name: same\n", encoding="utf-8")

    resolved = orch.resolve_definition("same", ident)
    assert resolved["status"] == "ok"
    assert scope in resolved["path"]
    assert orch.resolve_definition("../etc", ident)["status"] == "error"
    assert orch.resolve_definition("absent", ident)["status"] == "error"


def test_non_mapping_definition_returns_failure(monkeypatch):
    result = asyncio.run(orch.execute("not-a-dict", "任务",
                                     identity=_identity(), llm=object()))
    assert result["ok"] is False
    assert "对象" in result["error"]


def test_whitelisted_mcp_tool_is_available(monkeypatch):
    from langchain_core.tools import StructuredTool
    from server import state
    monkeypatch.setattr(state.STATE, "mcp_tools", {
        "get_city_weather": StructuredTool.from_function(
            func=lambda city="x": "晴", name="get_city_weather",
            description="天气")})
    data = _tool_definition("weather-demo")
    data["roles"][0]["tools"] = ["get_city_weather"]
    assert orch.validate_definition(data) == []
    assert "get_city_weather" in orch.available_tools()

