"""multi_agent：orchestrator-worker 编排——子任务解析、Send 扇出、全链路整合。
全部离线（FakeLLM 按调用序回放，ReAct worker 换成假 agent 工厂）。"""
import asyncio
import json
from types import SimpleNamespace

from server import multi_agent


class _Resp:
    """最小 LLM 回复桩（只被读 .content）。"""
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """按调用序返回预设回复（plan → aggregate 各一次）。"""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, msgs, **kw):
        self.calls.append(msgs)
        return _Resp(self.replies.pop(0))


# ---------------------------------------------------------------- 子任务解析

def test_parse_subtasks_json_array():
    assert multi_agent._parse_subtasks('["任务一", "任务二"]', "任务") == ["任务一", "任务二"]


def test_parse_subtasks_json_embedded_in_text():
    text = '好的，拆分如下：\n["甲", "乙", "丙"]\n以上。'
    assert multi_agent._parse_subtasks(text, "任务") == ["甲", "乙", "丙"]


def test_parse_subtasks_fallback_lines():
    """非法 JSON → 按行兜底（剥序号/项目符号，滤掉过短行）。"""
    text = "1. 第一个子任务足够长\n2. 第二个子任务也足够长\nok"
    out = multi_agent._parse_subtasks(text, "任务")
    assert "第一个子任务足够长" in out[0]
    assert "第二个子任务也足够长" in out[1]
    assert len(out) == 2


def test_parse_subtasks_empty_falls_back_to_task():
    assert multi_agent._parse_subtasks("", "整题直答") == ["整题直答"]


def test_parse_subtasks_caps_at_max_workers():
    qs = json.dumps([f"子任务{i}" for i in range(10)])
    assert len(multi_agent._parse_subtasks(qs, "t")) == multi_agent.MAX_WORKERS


# ---------------------------------------------------------------- Send 扇出

def test_route_workers_send_fanout():
    state = {"task": "总任务", "subtasks": ["a", "b", "c"]}
    sends = multi_agent._route_workers(state)
    assert [s.arg["subtask"] for s in sends] == ["a", "b", "c"]
    assert all(s.node == "worker" for s in sends)
    assert all(s.arg["task"] == "总任务" for s in sends)


# ---------------------------------------------------------------- 全链路（离线）

def test_run_orchestration_end_to_end(monkeypatch):
    """plan 拆分 → 2 个 worker 并行 → 整合结果 + 带拆分信息的头部。"""
    import langgraph.prebuilt as _pre

    made = []

    class _FakeReAct:
        """假 create_react_agent：记录工具集，回放固定执行结果。"""
        def __init__(self, llm, tools):
            self.tools = tools
            made.append(self)

        async def ainvoke(self, payload, config=None):
            return {"messages": [_Resp("子任务执行结果")]}

    monkeypatch.setattr(_pre, "create_react_agent", _FakeReAct)

    llm = _FakeLLM(['["子任务一", "子任务二"]', "整合结果正文"])
    tools_in = ["安全工具列表"]
    out = asyncio.run(multi_agent.run_orchestration("测试任务", llm, tools_in))

    assert "2 个子任务" in out
    assert "2 个 worker" in out
    assert "整合结果正文" in out
    assert json.dumps(["子任务一", "子任务二"], ensure_ascii=False) in out
    assert len(made) == 2                          # 每个子任务一个 worker
    assert all(w.tools is tools_in for w in made)  # 安全工具子集原样授予
    # plan 与 aggregate 各一次 LLM 调用
    assert len(llm.calls) == 2


def test_run_orchestration_non_str_answer_coerced(monkeypatch):
    """worker 末消息 content 非 str（结构化块）→ str() 兜底不崩。"""
    import langgraph.prebuilt as _pre

    class _FakeReAct:
        def __init__(self, llm, tools):
            pass

        async def ainvoke(self, payload, config=None):
            return {"messages": [_Resp(["结构化", "内容"])]}

    monkeypatch.setattr(_pre, "create_react_agent", _FakeReAct)
    llm = _FakeLLM(['["唯一子任务"]', "整合正文"])
    out = asyncio.run(multi_agent.run_orchestration("任务", llm, tools=[]))
    assert "整合正文" in out


def test_run_orchestration_single_subtask_direct(monkeypatch):
    """拆分兜底为整题直答（1 个 worker）时头部如实描述。"""
    import langgraph.prebuilt as _pre

    class _FakeReAct:
        def __init__(self, llm, tools):
            pass

        async def ainvoke(self, payload, config=None):
            return {"messages": [_Resp("答案")]}

    monkeypatch.setattr(_pre, "create_react_agent", _FakeReAct)
    llm = _FakeLLM(["无法解析", "整合"])
    out = asyncio.run(multi_agent.run_orchestration("直答任务", llm, tools=[]))
    assert "1 个子任务" in out
    assert "直答任务" in out
