"""4.1 流程定义与固化：步骤级进度事件链路（spec AC5）。

红测试口径：orchestration 模块实现前没有 adispatch_custom_event 引用、
execute 无进度时点、stream_reply 不透传 on_custom_event——
用例以 AttributeError/事件缺失失败。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk

from server import agent, config, orchestration as orch, state, usage


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    async def ainvoke(self, messages, config=None):
        return AIMessage(content=self.replies.pop(0))


def _install_react(monkeypatch, *, answer="步骤结果", factory=None):
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


def _capture_dispatch(monkeypatch):
    events = []

    async def fake_dispatch(name, payload):
        events.append((name, payload))

    monkeypatch.setattr(orch, "adispatch_custom_event", fake_dispatch)
    return events


def _two_step_definition(with_aggregate=True) -> dict:
    # 所有步骤角色都带工具 → 全部走 react 通道（FakeAgent 接管）；
    # aggregate 角色无工具 → 走直连 FakeLLM
    d = {
        "name": "progress-demo", "version": "1",
        "roles": [
            {"id": "planner", "prompt": "拆解", "tools": ["read_text_file"]},
            {"id": "worker", "prompt": "执行", "tools": ["read_text_file"]},
            {"id": "aggregator", "prompt": "汇总", "tools": []},
        ],
        "steps": [
            {"id": "plan", "role": "planner", "mode": "single"},
            {"id": "research", "role": "worker", "mode": "single",
             "inputs": ["plan"]},
        ],
    }
    if with_aggregate:
        d["aggregate"] = {"role": "aggregator", "inputs": ["research"]}
    return d


def _prepare(monkeypatch, with_aggregate=True):
    # aggregate（或无 aggregate 时的兜底摘要）可能走直连模型 → 给可应答的 FakeLLM
    monkeypatch.setattr(state.STATE, "llm", _FakeLLM(["汇总结果", "兜底"]))
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    usage.current_session.set("sess-p")
    usage.current_run.set("run-p")
    orch.save_definition(_two_step_definition(with_aggregate), scope=None)


def test_ac5_progress_event_sequence(monkeypatch):
    _prepare(monkeypatch)
    events = _capture_dispatch(monkeypatch)
    _install_react(monkeypatch)
    out = asyncio.run(orch.run_orchestration("progress-demo", "任务"))
    assert "多角色编排完成" in out

    assert events, "应产生进度事件"
    assert all(name == "wf_progress" for name, _ in events)
    seq = [(p["step"], p["status"]) for _, p in events]
    assert seq == [("plan", "start"), ("plan", "end"),
                   ("research", "start"), ("research", "end"),
                   ("aggregate", "start"), ("aggregate", "end")]
    # 同一实例贯穿；白名单字段齐备
    ids = {p["instance_id"] for _, p in events}
    assert len(ids) == 1 and ids.pop().startswith("wfi-")
    for _, p in events:
        assert p["definition"] == "progress-demo"
        assert p["total_steps"] == 3  # 2 步骤 + aggregate
        assert {"step_index", "detail"} <= set(p)
    agg = [p for _, p in events if p["step"] == "aggregate"][0]
    assert agg["step_index"] == 3


def test_ac5_progress_sequence_without_aggregate(monkeypatch):
    _prepare(monkeypatch, with_aggregate=False)
    events = _capture_dispatch(monkeypatch)
    _install_react(monkeypatch)
    out = asyncio.run(orch.run_orchestration("progress-demo", "任务"))
    assert "多角色编排完成" in out
    seq = [(p["step"], p["status"]) for _, p in events]
    assert seq == [("plan", "start"), ("plan", "end"),
                   ("research", "start"), ("research", "end")]
    assert {p["total_steps"] for _, p in events} == {2}


def test_ac5_progress_fail_event_on_step_error(monkeypatch):
    _prepare(monkeypatch)
    events = _capture_dispatch(monkeypatch)

    async def boom(agent_, payload, config=None):
        raise RuntimeError("boom")

    _install_react(monkeypatch, factory=boom)
    out = asyncio.run(orch.run_orchestration("progress-demo", "任务"))
    assert "执行失败" in out
    seq = [(p["step"], p["status"]) for _, p in events]
    assert seq[0] == ("plan", "start")
    assert seq[-1] == ("plan", "fail")
    assert ("plan", "end") not in seq


def test_ac5_dispatch_failure_is_best_effort(monkeypatch):
    _prepare(monkeypatch)

    async def bad_dispatch(name, payload):
        raise RuntimeError("no runnable context")

    monkeypatch.setattr(orch, "adispatch_custom_event", bad_dispatch)
    _install_react(monkeypatch)
    out = asyncio.run(orch.run_orchestration("progress-demo", "任务"))
    # dispatch 抛错不得影响执行结果
    assert "多角色编排完成" in out


# ---------------------------------------------------------------- SSE 透传


class _StreamAgent:
    """按脚本回放 astream_events；aget_state 无中断。"""

    def __init__(self, events):
        self.events = events

    async def astream_events(self, payload, config=None, version="v2"):
        for ev in self.events:
            yield ev

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


def _collect_stream(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.run(_run())


def test_ac5_stream_reply_only_wf_progress_whitelisted(monkeypatch):
    monkeypatch.setattr(config, "MOCK_LLM", False)
    state.STATE.llm = None
    state.STATE.agent = _StreamAgent([
        {"event": "on_custom_event", "name": "wf_progress",
         "data": {"instance_id": "wfi-000000000001", "definition": "d",
                  "step": "s", "step_index": 1, "total_steps": 2,
                  "status": "start", "detail": "",
                  "secret": "不应透传"}},
        {"event": "on_custom_event", "name": "evil_event", "data": {"x": 1}},
        {"event": "on_chat_model_stream", "tags": [],
         "data": {"chunk": AIMessageChunk(content="答")}},
    ])
    agent._rebuild_graph()
    out = _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "你好"}], session_id="s-wf", user_id="u1"))

    progress = [e for e in out if e["type"] == "progress"]
    assert len(progress) == 1
    p = progress[0]
    assert p["instance_id"] == "wfi-000000000001" and p["status"] == "start"
    assert "secret" not in p  # 仅白名单字段
    # 其他 custom event 名称一律不透传（防任意事件注入）
    assert not any("evil" in json.dumps(e, ensure_ascii=False) for e in out)
    assert out[-1]["type"] == "done"


# -------------------------------------------- 外部审核整改（v0.17.47，N5）


def test_ac5_progress_branch_does_not_precreate_bubble():
    # N5：progress 分支不得提前创建回答气泡——回答气泡仍在首个 token 时创建，
    # 保证工具调用标签显示在回答之前；进度行是独立容器（静态守护）
    text = (Path(__file__).resolve().parent.parent / "web" / "index.html"
            ).read_text(encoding="utf-8")
    start = text.index("ev.type === 'progress'")
    branch = text[start:text.index("else if", start)]
    assert "getBubble" not in branch
