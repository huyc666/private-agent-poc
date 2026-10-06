"""多 Agent 编排（orchestrator-worker 模式）：复杂调研主题 → 拆题 → 并行调研 → 汇总。

结构（LangGraph StateGraph）：

    START → plan（orchestrator 拆成 2~4 个子问题）
          → worker × N（Send API 并行扇出，每个 worker 是带安全工具子集的 mini ReAct Agent）
          → aggregate（把各 worker 的发现汇总成中文 Markdown 报告）
          → END

设计要点：
- worker 只持有安全工具（查天气/计算/读文件等），不持有删文件、建工具等危险能力——
  权限在架构层隔离，不靠 prompt 约束
- 整个子图以 tag "ma_worker" 运行：主对话流据此过滤 worker/汇总器的中间 token，
  避免污染用户可见的回答；但 worker 的工具调用事件会透出，界面上能看到并行调研过程
- worker 上限 4 个、单 worker 最多 4 轮工具调用，控制 token 与耗时
"""
import json
import operator
import re
from functools import partial
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

MAX_WORKERS = 4
WORKER_MAX_TOOL_ROUNDS = 4

MA_TAG = "ma_worker"  # 主对话流据此过滤子图 token


class MAState(TypedDict):
    topic: str
    sub_questions: list[str]
    findings: Annotated[list[dict], operator.add]  # worker 结果并行汇入
    report: str


class WorkerState(TypedDict):
    question: str
    topic: str


_PLAN_PROMPT = (
    "你是调研任务的规划器（orchestrator）。把用户给出的调研主题拆成 2~4 个可以"
    "独立回答的子问题。\n"
    "要求：\n"
    "1. 每个子问题自包含，一个不联网、只带基础工具（天气/计算/读文件）的助手能独立完成；\n"
    "2. 子问题分别覆盖主题的不同侧面，不重叠；\n"
    "3. 只输出 JSON 数组，例如 [\"子问题1\", \"子问题2\", \"子问题3\"]，不要输出任何其他文字。"
)

_WORKER_PROMPT = (
    "你是一名调研员（worker）。针对分配给你的子问题：需要数据时调用工具查证，"
    "不要编造数字；工具查不到就如实说明。最后用简洁的中文给出该子问题的结论"
    "（200 字以内，先结论后依据）。"
)

_AGG_PROMPT = (
    "你是报告撰写器。下面是各调研员对同一主题不同子问题的调研结论。"
    "请汇总成一份结构清晰的中文 Markdown 报告：先一段总体结论，"
    "再分小节呈现各子问题的要点，最后一行给出数据来源说明。"
    "不要编造结论中没有的信息。"
)


def _parse_questions(text: str, topic: str) -> list[str]:
    """从规划器输出中解析子问题 JSON；解析失败时按行兜底，再兜底为整题直答。"""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            qs = json.loads(m.group(0))
            if isinstance(qs, list):
                out = [str(q).strip() for q in qs if str(q).strip()]
                if out:
                    return out[:MAX_WORKERS]
        except json.JSONDecodeError:
            pass
    lines = [l.strip(" \t-•*0123456789.、") for l in text.splitlines()]
    lines = [l for l in lines if len(l) > 4]
    return lines[:MAX_WORKERS] or [topic]


async def _plan(state: MAState, *, llm) -> dict:
    resp = await llm.ainvoke([
        ("system", _PLAN_PROMPT),
        ("user", f"调研主题：{state['topic']}"),
    ])
    questions = _parse_questions(resp.content, state["topic"])
    print(f"[multi-agent] 拆题为 {len(questions)} 个子问题: {questions}")
    return {"sub_questions": questions}


def _route_workers(state: MAState):
    """plan 之后按子问题并行扇出 worker（Send API，LangGraph 异步并发执行）。"""
    return [
        Send("worker", {"question": q, "topic": state["topic"]})
        for q in state["sub_questions"][:MAX_WORKERS]
    ]


async def _worker(state: WorkerState, *, llm, tools) -> dict:
    from langgraph.prebuilt import create_react_agent

    worker = create_react_agent(llm, tools)
    result = await worker.ainvoke(
        {"messages": [
            ("system", _WORKER_PROMPT),
            ("user", f"总主题：{state['topic']}\n你负责的子问题：{state['question']}"),
        ]},
        config={"recursion_limit": WORKER_MAX_TOOL_ROUNDS * 2 + 2},
    )
    answer = result["messages"][-1].content
    if not isinstance(answer, str):
        answer = str(answer)
    print(f"[multi-agent] worker 完成: {state['question'][:30]}... -> {len(answer)} 字")
    return {"findings": [{"question": state["question"], "answer": answer}]}


async def _aggregate(state: MAState, *, llm) -> dict:
    parts = "\n\n".join(
        f"【子问题】{f['question']}\n【结论】{f['answer']}" for f in state["findings"])
    resp = await llm.ainvoke([
        ("system", _AGG_PROMPT),
        ("user", f"调研主题：{state['topic']}\n\n{parts}"),
    ])
    return {"report": resp.content}


def _build_graph(llm, tools):
    g = StateGraph(MAState)
    g.add_node("plan", partial(_plan, llm=llm))
    g.add_node("worker", partial(_worker, llm=llm, tools=tools))
    g.add_node("aggregate", partial(_aggregate, llm=llm))
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", _route_workers, ["worker"])
    g.add_edge("worker", "aggregate")
    g.add_edge("aggregate", END)
    return g.compile()


async def run_research(topic: str, llm, tools) -> str:
    """执行一次多 Agent 调研，返回带拆题信息的汇总报告（供主 Agent 作为工具结果）。"""
    graph = _build_graph(llm, tools)
    result = await graph.ainvoke(
        {"topic": topic, "sub_questions": [], "findings": [], "report": ""},
        config={"recursion_limit": 50, "tags": [MA_TAG]},
    )
    questions = result.get("sub_questions", [])
    header = (f"多 Agent 调研完成：orchestrator 拆分为 {len(questions)} 个子问题，"
              f"{len(result.get('findings', []))} 个 worker 并行调研后汇总。\n"
              f"子问题清单：{json.dumps(questions, ensure_ascii=False)}\n\n")
    return header + (result.get("report") or "（汇总报告为空）")
