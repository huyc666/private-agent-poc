"""多 Agent 编排（orchestrator-worker 模式）：复杂任务 → 拆分子任务 → 并行执行 → 汇总。

适用于可拆分为多个可独立完成子任务的工作：多角度调研、方案对比、批量文档
处理、多维度分析等；单点问答不值得走此路径（拆分与汇总各耗一次 LLM 调用）。

结构（LangGraph StateGraph）：

    START → plan（orchestrator 拆成 2~4 个可独立完成的子任务）
          → worker × N（Send API 并行扇出，每个 worker 是带安全工具子集的 mini ReAct Agent）
          → aggregate（把各 worker 的结果整合为一份完整回答）
          → END

设计要点：
- worker 只持有安全工具（读文件/存产出/平台包通用能力等），不持有删文件、
  建工具、跑沙箱等危险能力——权限在架构层隔离，不靠 prompt 约束
- 整个子图以 tag "ma_worker" 运行：主对话流据此过滤 worker/汇总器的中间 token，
  避免污染用户可见的回答；但 worker 的工具调用事件会透出，界面上能看到并行执行过程
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
    task: str
    subtasks: list[str]
    findings: Annotated[list[dict], operator.add]  # worker 结果并行汇入
    summary: str


class WorkerState(TypedDict):
    subtask: str
    task: str


_PLAN_PROMPT = (
    "你是任务规划器（orchestrator）。把用户给出的任务拆成 2~4 个可以独立完成的"
    "子任务。\n"
    "要求：\n"
    "1. 每个子任务自包含，一个不联网、只带基础工具（读文件/存产出/计算等）的"
    "助手能独立完成；\n"
    "2. 子任务分别覆盖总任务的不同侧面，合起来能完整回答总任务，不重叠；\n"
    "3. 如果总任务本身很简单、不值得拆分，就只输出包含它自身的单元素数组；\n"
    "4. 只输出 JSON 数组，例如 [\"子任务1\", \"子任务2\", \"子任务3\"]，"
    "不要输出任何其他文字。"
)

_WORKER_PROMPT = (
    "你是一名执行者（worker）。完成分配给你的子任务：需要数据或文件内容时调用"
    "工具查证，不要编造；工具查不到就如实说明。最后用简洁的中文给出该子任务的"
    "结果（200 字以内，先结论后依据；若子任务要求产出文件，先存盘再报告结果）。"
)

_AGG_PROMPT = (
    "你是结果整合器。下面是各执行者对同一总任务不同子任务的处理结果。"
    "请整合为一份结构清晰的中文 Markdown 回答：先一段总体结论，"
    "再分小节呈现各子任务的结果要点，最后一行给出数据来源说明。"
    "不要编造执行结果中没有的信息。"
)


def _parse_subtasks(text: str, task: str) -> list[str]:
    """从规划器输出中解析子任务 JSON；解析失败时按行兜底，再兜底为整题直答。"""
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
    return lines[:MAX_WORKERS] or [task]


async def _plan(state: MAState, *, llm) -> dict:
    resp = await llm.ainvoke([
        ("system", _PLAN_PROMPT),
        ("user", f"总任务：{state['task']}"),
    ])
    subtasks = _parse_subtasks(resp.content, state["task"])
    print(f"[multi-agent] 拆分为 {len(subtasks)} 个子任务: {subtasks}")
    return {"subtasks": subtasks}


def _route_workers(state: MAState):
    """plan 之后按子任务并行扇出 worker（Send API，LangGraph 异步并发执行）。"""
    return [
        Send("worker", {"subtask": t, "task": state["task"]})
        for t in state["subtasks"][:MAX_WORKERS]
    ]


async def _worker(state: WorkerState, *, llm, tools) -> dict:
    from langgraph.prebuilt import create_react_agent

    worker = create_react_agent(llm, tools)
    result = await worker.ainvoke(
        {"messages": [
            ("system", _WORKER_PROMPT),
            ("user", f"总任务：{state['task']}\n你负责的子任务：{state['subtask']}"),
        ]},
        config={"recursion_limit": WORKER_MAX_TOOL_ROUNDS * 2 + 2},
    )
    answer = result["messages"][-1].content
    if not isinstance(answer, str):
        answer = str(answer)
    print(f"[multi-agent] worker 完成: {state['subtask'][:30]}... -> {len(answer)} 字")
    return {"findings": [{"subtask": state["subtask"], "result": answer}]}


async def _aggregate(state: MAState, *, llm) -> dict:
    parts = "\n\n".join(
        f"【子任务】{f['subtask']}\n【结果】{f['result']}" for f in state["findings"])
    resp = await llm.ainvoke([
        ("system", _AGG_PROMPT),
        ("user", f"总任务：{state['task']}\n\n{parts}"),
    ])
    return {"summary": resp.content}


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


async def run_orchestration(task: str, llm, tools) -> str:
    """执行一次多 Agent 编排，返回带拆分信息的整合结果（供主 Agent 作为工具结果）。"""
    graph = _build_graph(llm, tools)
    result = await graph.ainvoke(
        {"task": task, "subtasks": [], "findings": [], "summary": ""},
        config={"recursion_limit": 50, "tags": [MA_TAG]},
    )
    subtasks = result.get("subtasks", [])
    header = (f"多 Agent 编排完成：orchestrator 拆分为 {len(subtasks)} 个子任务，"
              f"{len(result.get('findings', []))} 个 worker 并行执行后整合。\n"
              f"子任务清单：{json.dumps(subtasks, ensure_ascii=False)}\n\n")
    return header + (result.get("summary") or "（整合结果为空）")
