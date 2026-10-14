"""multi_agent：orchestrator-worker 编排——拆题解析、Send 扇出、全链路汇总。
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


# ---------------------------------------------------------------- 拆题解析

def test_parse_questions_json_array():
    assert multi_agent._parse_questions('["问题一", "问题二"]', "主题") == ["问题一", "问题二"]


def test_parse_questions_json_embedded_in_text():
    text = '好的，拆题如下：\n["甲", "乙", "丙"]\n以上。'
    assert multi_agent._parse_questions(text, "主题") == ["甲", "乙", "丙"]


def test_parse_questions_fallback_lines():
    """非法 JSON → 按行兜底（剥序号/项目符号，滤掉过短行）。"""
    text = "1. 第一个问题足够长\n2. 第二个问题也足够长\nok"
    out = multi_agent._parse_questions(text, "主题")
    assert "第一个问题足够长" in out[0]
    assert "第二个问题也足够长" in out[1]
    assert len(out) == 2


def test_parse_questions_empty_falls_back_to_topic():
    assert multi_agent._parse_questions("", "整题直答") == ["整题直答"]


def test_parse_questions_caps_at_max_workers():
    qs = json.dumps([f"子问题{i}" for i in range(10)])
    assert len(multi_agent._parse_questions(qs, "t")) == multi_agent.MAX_WORKERS


# ---------------------------------------------------------------- Send 扇出

def test_route_workers_send_fanout():
    state = {"topic": "总主题", "sub_questions": ["a", "b", "c"]}
    sends = multi_agent._route_workers(state)
    assert [s.arg["question"] for s in sends] == ["a", "b", "c"]
    assert all(s.node == "worker" for s in sends)
    assert all(s.arg["topic"] == "总主题" for s in sends)


# ---------------------------------------------------------------- 全链路（离线）

def test_run_research_end_to_end(monkeypatch):
    """plan 拆题 → 2 个 worker 并行 → 汇总报告 + 带拆题信息的头部。"""
    import langgraph.prebuilt as _pre

    made = []

    class _FakeReAct:
        """假 create_react_agent：记录工具集，回放固定调研结论。"""
        def __init__(self, llm, tools):
            self.tools = tools
            made.append(self)

        async def ainvoke(self, payload, config=None):
            return {"messages": [_Resp("调研结论")]}

    monkeypatch.setattr(_pre, "create_react_agent", _FakeReAct)

    llm = _FakeLLM(['["子问题一", "子问题二"]', "汇总报告正文"])
    tools_in = ["安全工具列表"]
    out = asyncio.run(multi_agent.run_research("测试主题", llm, tools_in))

    assert "2 个子问题" in out
    assert "2 个 worker" in out
    assert "汇总报告正文" in out
    assert json.dumps(["子问题一", "子问题二"], ensure_ascii=False) in out
    assert len(made) == 2                          # 每个子问题一个 worker
    assert all(w.tools is tools_in for w in made)  # 安全工具子集原样授予
    # plan 与 aggregate 各一次 LLM 调用
    assert len(llm.calls) == 2


def test_run_research_non_str_answer_coerced(monkeypatch):
    """worker 末消息 content 非 str（结构化块）→ str() 兜底不崩。"""
    import langgraph.prebuilt as _pre

    class _FakeReAct:
        def __init__(self, llm, tools):
            pass

        async def ainvoke(self, payload, config=None):
            return {"messages": [_Resp(["结构化", "内容"])]}

    monkeypatch.setattr(_pre, "create_react_agent", _FakeReAct)
    llm = _FakeLLM(['["唯一问题"]', "报告正文"])
    out = asyncio.run(multi_agent.run_research("主题", llm, tools=[]))
    assert "报告正文" in out


def test_run_research_single_question_direct(monkeypatch):
    """拆题兜底为整题直答（1 个 worker）时头部如实描述。"""
    import langgraph.prebuilt as _pre

    class _FakeReAct:
        def __init__(self, llm, tools):
            pass

        async def ainvoke(self, payload, config=None):
            return {"messages": [_Resp("答案")]}

    monkeypatch.setattr(_pre, "create_react_agent", _FakeReAct)
    llm = _FakeLLM(["无法解析", "报告"])
    out = asyncio.run(multi_agent.run_research("直答主题", llm, tools=[]))
    assert "1 个子问题" in out
    assert "直答主题" in out
