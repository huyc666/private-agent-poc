"""Agent 运行时：LangGraph 图装配 + checkpointer 生命周期 + 流式输出编排。

职责边界（v0.13.0 重构后）：
- 内核工具定义        → server/tools_builtin.py（安全边界最小集合）
- 管理类工具与装配清单 → server/tools_admin.py（技能/自定义工具/领域包/多 Agent）
- Mock 演示剧本       → server/mock.py（与生产链路隔离）
- 进程级运行时状态     → server/state.py 的 STATE（AppState）
- 审批门              → server/approval.py 的 require_approval
本文件只负责：MCP 工具加载、Agent 图构建/热重载、checkpointer 生命周期、SSE 流式编排。

真实模式：连接 vLLM 的 OpenAI 兼容接口（工具调用走 hermes parser）。
Mock 模式：MOCK_LLM=true 时不依赖任何 GPU/模型，用于验证全链路。
"""
import asyncio
import sys
import threading
from typing import AsyncIterator

from . import approval, config, custom_tools, packs, skills, telemetry, usage, tools_admin
from .state import STATE


# ---------------------------------------------------------------- MCP 工具

async def load_mcp_tools() -> list:
    """通过 stdio 拉起本地 MCP Server 并获取其工具列表。失败时返回空列表。"""
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient({
            "local-tools": {
                "command": sys.executable,
                "args": [str(config.MCP_SERVER_SCRIPT)],
                "transport": "stdio",
            }
        })
        return await client.get_tools()
    except Exception as e:
        print(f"[agent] MCP 工具加载失败（忽略，仅用内置工具继续）: {e}")
        return []


# ---------------------------------------------------------------- Agent 构建

def _rebuild_graph():
    """组装 内置 + MCP + 自定义 + 领域包工具 并重建 Agent 图。
    自定义工具/领域包变化时调用本函数即可免重启生效（checkpointer 复用，会话不丢）。"""
    tools = tools_admin.builtin_langchain_tools() + list(STATE.mcp_tool_list)
    tools += custom_tools.load_langchain_tools()
    pack_tool_list = packs.load_pack_tools()
    tools += pack_tool_list
    STATE.pack_tools = {t.name: t for t in pack_tool_list}
    STATE.tool_names = [t.name for t in tools]
    # 签名同时包含沙箱可达性与领域包状态：任一变化都会触发重绑
    STATE.custom_sig = (custom_tools.signature(), custom_tools._sandbox_up(),
                        packs.packs_signature())

    # 技能索引 = 内核 skills/ + 当前启用领域包的 skills/（禁用包后其技能随之消失）
    skills.reset_extra_dirs()
    packs.register_pack_skills()

    if config.MOCK_LLM or STATE.llm is None:
        return

    from langgraph.prebuilt import create_react_agent

    # checkpointer 是 interrupt 审批流的前提（中断现场需要落盘才能恢复）
    # 技能索引 + 领域包提示词片段注入系统提示词（渐进式披露：模型按需调用 load_skill 拉取完整指令）
    system = config.SYSTEM_PROMPT + skills.skill_index_prompt()
    fragments = packs.prompt_fragments()
    if fragments:
        system += "\n\n" + fragments
    STATE.agent = create_react_agent(STATE.llm, tools, prompt=system,
                                     checkpointer=STATE.checkpointer)


async def build_agent():
    """在应用启动时调用一次。Mock 模式下只登记工具名。"""
    STATE.mcp_tool_list = []
    if config.MCP_ENABLED:
        STATE.mcp_tool_list = await load_mcp_tools()
        STATE.mcp_tools = {t.name: t for t in STATE.mcp_tool_list}

    if config.MOCK_LLM:
        _rebuild_graph()
        print("[agent] MOCK_LLM=true，使用 Mock 模式（不连接模型）")
        return

    from langchain_openai import ChatOpenAI

    STATE.llm = ChatOpenAI(
        model=config.MODEL_NAME,
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        temperature=config.TEMPERATURE,
        max_tokens=config.MAX_TOKENS,
        streaming=True,
        stream_usage=True,  # 流式末帧携带 usage_metadata，token 计量依赖
        callbacks=[usage.UsageCallback().handler],  # 挂在 LLM 上：主 Agent 与 worker 全覆盖
    )
    await _make_checkpointer()
    _rebuild_graph()
    print(f"[agent] 已连接模型 {config.MODEL_NAME} @ {config.LLM_BASE_URL}，工具: {STATE.tool_names}")


async def _make_checkpointer():
    """按 CHECKPOINTER 配置创建 checkpointer（会话与审批中断现场的持久化层）。
    postgres 模式首次连接自动建表；连接失败时给出明确指引（fail-fast）。"""
    if config.CHECKPOINTER == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        cm = AsyncPostgresSaver.from_conn_string(config.DATABASE_URL)
        try:
            STATE.checkpointer = await cm.__aenter__()
            await STATE.checkpointer.setup()  # 幂等建表
        except Exception as e:
            await cm.__aexit__(None, None, None)
            hint = ""
            if sys.platform == "win32":
                hint = ("（Windows 开发机不支持 postgres 模式：psycopg 异步与 "
                        "ProactorEventLoop 不兼容，请保持 CHECKPOINTER=memory，"
                        "到 Linux Docker 机器再启用）")
            raise RuntimeError(
                f"PostgreSQL checkpointer 连接失败: {e}{hint}。请确认已执行 "
                "docker compose -f docker-compose.postgres.yml up -d，"
                "或将 .env 改回 CHECKPOINTER=memory") from e
        STATE.checkpointer_cm = cm
        print(f"[agent] checkpointer: PostgresSaver @ {config.DATABASE_URL.split('@')[-1]}")
    else:
        from langgraph.checkpoint.memory import MemorySaver

        STATE.checkpointer = MemorySaver()
        print("[agent] checkpointer: MemorySaver（进程内存，重启即丢；生产请改 CHECKPOINTER=postgres）")


async def close_checkpointer():
    """应用退出时关闭 PostgresSaver 连接（lifespan 里调用）。"""
    if STATE.checkpointer_cm is not None:
        await STATE.checkpointer_cm.__aexit__(None, None, None)
        STATE.checkpointer_cm = None


_reload_lock = threading.Lock()


def maybe_reload_custom_tools():
    """自定义工具/领域包/沙箱可达性有变化时重建图（每轮对话开头检查，开销可忽略）。
    加锁串行化：并发会话同时检测到变化时只重建一次；调用方应经
    asyncio.to_thread 调用（沙箱探测是同步网络 IO，不能阻塞事件循环）。"""
    with _reload_lock:
        if (custom_tools.signature(), custom_tools._sandbox_up(),
                packs.packs_signature()) != STATE.custom_sig:
            print("[agent] 检测到自定义工具/领域包/沙箱状态变化，重新绑定工具")
            _rebuild_graph()


def _extract_interrupt(state) -> dict | None:
    """从 LangGraph 状态中取出待审批中断（如有）。"""
    try:
        for task in state.tasks:
            for intr in getattr(task, "interrupts", None) or []:
                if isinstance(intr.value, dict) and intr.value.get("kind") == "approval":
                    return intr.value
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 流式输出

# ---- 同会话串行化（v0.16.4，v0.17.1 加固跨进程）----
# 对话本质串行：同一 session_id 的并发请求会导致 checkpointer 状态写冲突、
# 对话记录交错。本锁是**进程内快速路径**（同一进程内互斥，避免频繁写库）；
# 跨进程互斥由 main.py 配合的 STATE_DB 分布式租约（server/session_lock.py）承担。
_session_locks: dict[str, asyncio.Lock] = {}
_SESSION_LOCKS_MAX = 1000  # 锁表阈值：超过时清理未持有项（防长运行内存膨胀）


def session_lock(session_id: str) -> asyncio.Lock:
    """取该会话的 asyncio 锁（惰性创建）。调用方用 async with 持锁执行整轮对话。"""
    if len(_session_locks) >= _SESSION_LOCKS_MAX:
        for k in [k for k, v in _session_locks.items() if not v.locked()]:
            _session_locks.pop(k, None)
    return _session_locks.setdefault(session_id, asyncio.Lock())


async def stream_reply(messages: list[dict], session_id: str = "default",
                       user_id: str = "anonymous", run_id: str = "") -> AsyncIterator[dict]:
    """yield SSE 事件 dict：
    {"type":"token","content":...} / {"type":"tool",...} / {"type":"approval_required",...}
    / {"type":"done","usage":{...}} / {"type":"error"}
    """
    # token 计量与审批身份上下文：UsageCallback / create_approval 读取这三个值做归属
    # （用户/会话/任务）——必须在 Mock 分支之前设置，Mock 演示剧本同样走审批流
    usage.current_user.set(user_id)
    usage.current_session.set(session_id)
    usage.current_run.set(run_id)

    if config.MOCK_LLM:
        from . import mock
        async for ev in mock.mock_stream(messages[-1]["content"] if messages else ""):
            yield ev
        return

    from langgraph.types import Command

    # 自定义工具有变化时免重启热加载；探测含同步网络 IO，放到线程执行避免阻塞事件循环
    await asyncio.to_thread(maybe_reload_custom_tools)

    run_config = {
        "configurable": {"thread_id": session_id},
        "callbacks": telemetry.langchain_callbacks(),
    }
    payload = {"messages": messages}
    # 上一轮带批准决议 resume 的 aid：本轮跑完（未抛错）后标记 executed_at，
    # 供审计区分「已批准已执行」与「已批准但执行丢失」（后者由启动/周期扫描转 abandoned）
    resuming_aid = ""

    for _round in range(5):  # 一次对话最多 5 轮审批，防死循环
        try:
            async for event in STATE.agent.astream_events(payload, config=run_config, version="v2"):
                kind = event["event"]
                if kind == "on_chat_model_stream":
                    # 多 Agent 子图（tag ma_worker）的中间 token 不进入用户可见流；
                    # 其工具调用事件仍然透出，界面上可观察并行调研过程
                    if any(t == "ma_worker" for t in event.get("tags", [])):
                        continue
                    chunk = event["data"]["chunk"]
                    if chunk.content:
                        yield {"type": "token", "content": chunk.content}
                elif kind == "on_tool_start":
                    yield {"type": "tool", "status": "start",
                           "name": event.get("name", "?")}
                elif kind == "on_tool_end":
                    output = event["data"].get("output")
                    text = getattr(output, "content", output)
                    yield {"type": "tool", "status": "end",
                           "name": event.get("name", "?"),
                           "result": str(text)[:2000]}
        except Exception as e:
            # resume 轮抛错：executed_at 不标记，记录保持「已批准但未执行」
            # （审计可见，绝不让失败被误报为已执行）
            yield {"type": "error", "content": f"模型调用失败: {e}"}
            return
        if resuming_aid:
            await asyncio.to_thread(approval.mark_executed, resuming_aid)
            resuming_aid = ""

        # 检查是否因危险操作审批而中断
        try:
            state = await STATE.agent.aget_state(run_config)
            intr = _extract_interrupt(state)
        except Exception:
            intr = None
        if intr is None:
            break

        tool = intr.get("tool", "?")
        args = intr.get("args", {})
        aid = approval.create_approval(tool, args)
        yield {"type": "approval_required", "approval_id": aid,
               "tool": tool, "args": args}
        decision = await approval.wait_decision(aid)
        # decision: True 批准 / False 拒绝 / None 超时（fail-closed，一律视为拒绝）
        payload = Command(resume={"approve": decision is True})
        if decision is True:
            resuming_aid = aid  # 批准：下一轮 resume 跑完即标记已执行

    # done 事件携带本次任务与本会话的 token 消耗（前端展示 + 调用方可自行累计）
    yield {"type": "done", "usage": {
        "run": usage.run_usage(run_id) if run_id else {},
        "session": usage.session_usage(session_id),
    }}
