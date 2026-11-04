"""编排计量归属（v0.17.44，spec §7 / AC13）。

口径：全部计入父 run，source=orchestration；禁止计到定义作者或系统账号。
"""
import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from server import orchestration as orch, usage


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


class _Resp:
    def __init__(self):
        msg = AIMessage(content="ok")
        msg.usage_metadata = {"input_tokens": 11, "output_tokens": 7,
                              "total_tokens": 18}
        msg.response_metadata = {"model_name": "fake-model"}
        self.generations = [[SimpleNamespace(message=msg)]]


def test_usage_callback_marks_orchestration_source(monkeypatch):
    captured = []

    def fake_record(model, u, source="main", duration_ms=0, status="ok"):
        captured.append({"model": model, "usage": u, "source": source,
                         "user": usage.current_user.get(),
                         "session": usage.current_session.get(),
                         "run": usage.current_run.get()})

    monkeypatch.setattr(usage, "record", fake_record)
    usage.current_user.set("alice")
    usage.current_session.set("s1")
    usage.current_run.set("r1")
    usage.current_identity.set(_identity())

    h = usage.UsageCallback().handler
    h.on_llm_end(_Resp(), run_id="x", tags=["orchestration"])
    assert captured == [{
        "model": "fake-model",
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        "source": "orchestration",
        "user": "alice",
        "session": "s1",
        "run": "r1",
    }]


def test_orchestration_source_wins_over_inner_worker_tag():
    captured = []
    from server import usage as u
    orig = u.record
    try:
        u.record = lambda model, usage_, source="main", **kw: captured.append(source)
        h = u.UsageCallback().handler
        h.on_llm_end(_Resp(), run_id="x", tags=["orchestration", "ma_worker"])
        assert captured == ["orchestration"]
    finally:
        u.record = orig


def test_plain_call_keeps_main_source():
    captured = []
    from server import usage as u
    orig = u.record
    try:
        u.record = lambda model, usage_, source="main", **kw: captured.append(source)
        h = u.UsageCallback().handler
        h.on_llm_end(_Resp(), run_id="x", tags=["ma_worker"])
        assert captured == ["worker"]
        h.on_llm_end(_Resp(), run_id="y", tags=[])
        assert captured[-1] == "main"
    finally:
        u.record = orig


def test_orchestration_engine_rebinds_initiator_before_each_model_call(monkeypatch):
    """清空上下文后执行编排：每一步都重新绑定父 run 与发起者，不落到 anonymous。"""
    captured = []

    def fake_record(model, u, source="main", duration_ms=0, status="ok"):
        captured.append((source, usage.current_user.get(),
                         usage.current_session.get(), usage.current_run.get()))

    monkeypatch.setattr(usage, "record", fake_record)

    import langgraph.prebuilt as prebuilt
    made = []

    class FakeAgent:
        def __init__(self, llm, tools):
            self.tools = list(tools)
            made.append(self)

        async def ainvoke(self, payload, config=None):
            # 模拟一次真实 LLM 回调：使用当前上下文落账
            usage.UsageCallback().handler.on_llm_end(
                _Resp(), run_id=f"r{len(made)}",
                tags=list((config or {}).get("tags") or []))
            return {"messages": [AIMessage(content="ok")]}

    monkeypatch.setattr(prebuilt, "create_react_agent", FakeAgent)

    usage.current_user.set("anonymous")
    usage.current_session.set("")
    usage.current_run.set("")
    usage.current_identity.set(None)

    definition = {
        "name": "meter-demo", "version": "1",
        "roles": [{"id": "r", "prompt": "执行", "tools": ["read_text_file"]}],
        "steps": [{"id": f"s{i}", "role": "r", "mode": "single"} for i in range(2)],
    }
    result = asyncio.run(orch.execute(
        definition, "任务", identity=_identity(username="alice"),
        session_id="s1", run_id="parent-run", llm=object()))
    assert result["ok"] is True
    assert captured
    for source, user, session, run in captured:
        assert source == "orchestration"
        assert user == "alice"
        assert session == "s1"
        assert run == "parent-run"


def test_record_count_matches_model_calls_and_all_marked_orchestration(monkeypatch):
    """plan/worker/aggregate 每次模型调用各落一条，全部 source=orchestration。"""
    captured = []
    monkeypatch.setattr(
        usage, "record",
        lambda model, u, source="main", duration_ms=0, status="ok":
        captured.append(source))

    import langgraph.prebuilt as prebuilt

    class FakeLLM:
        def __init__(self, replies):
            self.replies = list(replies)

        async def ainvoke(self, messages, config=None):
            usage.UsageCallback().handler.on_llm_end(
                _Resp(), run_id="direct",
                tags=list((config or {}).get("tags") or []))
            return AIMessage(content=self.replies.pop(0))

    class FakeAgent:
        def __init__(self, llm, tools):
            self.tools = list(tools)

        async def ainvoke(self, payload, config=None):
            usage.UsageCallback().handler.on_llm_end(
                _Resp(), run_id="worker",
                tags=list((config or {}).get("tags") or []))
            return {"messages": [AIMessage(content="子结果")]}

    monkeypatch.setattr(prebuilt, "create_react_agent", FakeAgent)
    definition = {
        "name": "count-demo", "version": "1", "max_parallel": 2,
        "roles": [
            {"id": "p", "prompt": "拆分", "tools": []},
            {"id": "w", "prompt": "执行", "tools": ["read_text_file"]},
            {"id": "a", "prompt": "汇总", "tools": []},
        ],
        "steps": [
            {"id": "plan", "role": "p", "mode": "single"},
            {"id": "work", "role": "w", "mode": "parallel",
             "fanout": {"from": "plan", "limit": 2}, "inputs": ["plan"]},
        ],
        "aggregate": {"role": "a", "inputs": ["work"]},
    }
    result = asyncio.run(orch.execute(
        definition, "任务", identity=_identity(), session_id="s1",
        run_id="parent", llm=FakeLLM(['["甲","乙"]', "汇总"])))
    assert result["ok"] is True
    assert len(captured) == 4  # plan + 2 worker + aggregate
    assert captured == ["orchestration"] * 4


def test_definition_author_field_is_rejected_so_no_author_account_attribution():
    """定义不携带作者身份字段；归属只认发起者快照，杜绝计到定义作者/系统账号。"""
    definition = {
        "name": "author-demo", "version": "1",
        "author": "boss",
        "roles": [{"id": "r", "prompt": "执行", "tools": []}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }
    errors = orch.validate_definition(definition)
    assert any("author" in e for e in errors)
