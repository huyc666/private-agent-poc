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
    # 签名同时包含沙箱可达性/领域包状态/技能文件：任一变化都会触发重绑
    STATE.custom_sig = (custom_tools.signature(), custom_tools._sandbox_up(),
                        packs.packs_signature(), skills.signature())

    # 技能索引 = 内核 skills/ + 当前启用领域包的 skills/（禁用包后其技能随之消失）
    skills.reset_extra_dirs()
    packs.register_pack_skills()

    if config.MOCK_LLM or STATE.llm is None:
        return

    from langgraph.prebuilt import create_react_agent

    # checkpointer 是 interrupt 审批流的前提（中断现场需要落盘才能恢复）
    # 技能索引 + 领域包提示词片段注入系统提示词（渐进式披露：模型按需调用 load_skill 拉取完整指令）
    system = config.SYSTEM_PROMPT + skills.skill_index_prompt() + config.UPLOAD_PROMPT
    fragments = packs.prompt_fragments()
    if fragments:
        system += "\n\n" + fragments
    STATE.agent = create_react_agent(STATE.llm, tools, prompt=system,
                                     checkpointer=STATE.checkpointer,
                                     pre_model_hook=_pre_model_hook)


async def build_agent():
    """在应用启动时调用一次。Mock 模式下只登记工具名。"""
    # 启动即打印压缩配置（评审 P3：阈值配错/忘配时在日志一眼可见，不必等触发）
    if config.CONTEXT_COMPACT_ENABLED:
        print(f"[agent] 上下文压缩：触发线 "
              f"{int(config.CONTEXT_MAX_TOKENS * config.CONTEXT_COMPACT_RATIO):,} tokens"
              f"（窗口 {config.CONTEXT_MAX_TOKENS:,} × {config.CONTEXT_COMPACT_RATIO:g}），"
              f"近窗 {config.CONTEXT_RECENT_TOKENS:,}")
    STATE.mcp_tool_list = []
    if config.MCP_ENABLED:
        STATE.mcp_tool_list = await load_mcp_tools()
        STATE.mcp_tools = {t.name: t for t in STATE.mcp_tool_list}

    if config.MOCK_LLM:
        _rebuild_graph()
        print("[agent] MOCK_LLM=true，使用 Mock 模式（不连接模型）")
        return

    from langchain_core.messages import AIMessageChunk
    from langchain_openai import ChatOpenAI

    class _ReasoningChatOpenAI(ChatOpenAI):
        """ChatOpenAI + DeepSeek 风格思维链透传（v0.17.7）。
        langchain-openai 的通用转换器会丢弃 delta.reasoning_content（其文档明示
        「reasoning 字段不提取，请用 provider 专属子类」），而思维链是前端
        「💭 思考过程」展示的数据来源。这里以最小侵入把该字段挂回
        AIMessageChunk.additional_kwargs，供 agent 流式管线透出 reasoning 事件。
        覆盖的是私有转换方法：升级 langchain-openai 后需复核签名。"""

        def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class,
                                               base_generation_info):
            gen = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info)
            if gen is not None and isinstance(gen.message, AIMessageChunk):
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                rc = delta.get("reasoning_content")
                if rc:
                    gen.message.additional_kwargs["reasoning_content"] = rc
            return gen

    STATE.llm = _ReasoningChatOpenAI(
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
    """自定义工具/领域包/沙箱可达性/技能文件有变化时重建图（每轮对话开头检查，开销可忽略）。
    加锁串行化：并发会话同时检测到变化时只重建一次；调用方应经
    asyncio.to_thread 调用（沙箱探测是同步网络 IO，不能阻塞事件循环）。"""
    with _reload_lock:
        if (custom_tools.signature(), custom_tools._sandbox_up(),
                packs.packs_signature(), skills.signature()) != STATE.custom_sig:
            print("[agent] 检测到自定义工具/领域包/沙箱/技能变化，重新绑定工具")
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


# ---------------------------------------------------------------- 上下文压缩
# v0.17.7：历史超阈值时「窗口外旧消息 → 滚动摘要」，只改模型视野（pre_model_hook
# 产出 llm_input_messages），checkpointer 原始历史完整保留（界面恢复/审计不受影响）。
# 触发条件按 token 估算而非轮数（纯聊天一轮几百 token，读一次文档就是几千，方差太大）。

_compact_cache: dict[str, dict] = {}   # session_id -> {"summary": 摘要文本, "count": 已摘要消息数}
_COMPACT_CACHE_MAX = 1000              # 摘要缓存上限（摘要可随时重算，超限丢弃最早的）

_SUMMARY_TMPL = (
    "你是会话上下文压缩器。下面是同一会话的「已有摘要」和「新落入窗口外的消息记录」。"
    "把它们合并为一份工作摘要（600 字以内），必须保留：① 用户的任务目标；② 已完成的"
    "工作与产物（文件名/下载链接/技能/工具/包名原样保留）；③ 关键决策与用户明确约定；"
    "④ 未完成事项与待办；⑤ 涉及的文档与数据要点。用简洁的条目式中文，不要评论，"
    "不要编造记录里没有的信息。\n\n"
    "【已有摘要】\n{old}\n\n【新落入窗口外的消息】\n{transcript}\n\n直接输出摘要："
)


def _msg_text(msg) -> str:
    c = getattr(msg, "content", "")
    if isinstance(c, list):
        c = " ".join(str(p) for p in c)
    return str(c)


def _est_tokens(msg) -> int:
    """粗估单条消息 token：中日韩字符 ≈1 token/字，其余 ≈4 字符/token，加 8 壳开销。
    推理模型的思维链（additional_kwargs.reasoning_content）同样占用窗口，一并计入，
    否则推理型会话会低估触发时机。混合文本误差 ±30% 级别——触发线留了 40% 余量。"""
    c = _msg_text(msg)
    rc = str((getattr(msg, "additional_kwargs", None) or {}).get("reasoning_content", "") or "")
    text = c + rc
    if not text:
        return 8
    combined = text
    cjk = sum(1 for ch in combined if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(combined) - cjk) // 4 + 8


def _find_cut(msgs: list, budget_tokens: int) -> int:
    """从尾部向前保留 budget 内的消息，返回切割下标。
    成对完整性：切割点落在 ToolMessage 上时前移（避免工具结果悬空、其父
    AIMessage(tool_calls) 被裁掉导致 API 400）。"""
    total, i = 0, len(msgs)
    while i > 0:
        t = _est_tokens(msgs[i - 1])
        if total + t > budget_tokens and i < len(msgs):
            break
        total += t
        i -= 1
    while i < len(msgs) and getattr(msgs[i], "type", "") == "tool":
        i += 1
    return i


async def prepare_compaction(session_id: str) -> None:
    """轮前压缩：历史估算超阈值时，把窗口外新增消息滚动摘要进缓存。
    独立 LLM 调用（在主图事件流之外，不会向 SSE 漏事件/不产生假消息）；
    失败只回退纯窗口模式，不影响本轮对话。"""
    if not config.CONTEXT_COMPACT_ENABLED or STATE.agent is None or STATE.llm is None:
        return
    try:
        snap = await STATE.agent.aget_state({"configurable": {"thread_id": session_id}})
        msgs = snap.values.get("messages") or []
        if len(msgs) < 6:   # 过短没有压缩价值
            return
        total = sum(_est_tokens(m) for m in msgs)
        trigger = int(config.CONTEXT_MAX_TOKENS * config.CONTEXT_COMPACT_RATIO)
        if total <= trigger:
            return
        cache = _compact_cache.get(session_id) or {}
        already = int(cache.get("count", 0))
        cut = _find_cut(msgs, config.CONTEXT_RECENT_TOKENS)
        if cut <= already:
            return
        seg_tokens = sum(_est_tokens(m) for m in msgs[already:cut])
        # 滞回：已有摘要且新增段不大时沿用旧摘要（避免每轮都烧一次摘要调用）
        if cache.get("summary") and seg_tokens < config.CONTEXT_SUMMARY_HYSTERESIS_TOKENS:
            return
        parts = []
        for m in msgs[already:cut]:
            role = getattr(m, "type", "?")
            text = _msg_text(m).strip()
            if getattr(m, "tool_calls", None):
                calls = ", ".join(c.get("name", "?") for c in m.tool_calls)
                parts.append(f"{role}: [调用工具 {calls}]")
            elif text:
                parts.append(f"{role}: {text[:300]}")
        transcript = "\n".join(parts) or "（无文本内容）"
        resp = await STATE.llm.ainvoke(_SUMMARY_TMPL.format(
            old=cache.get("summary") or "（无）", transcript=transcript))
        text = _msg_text(resp).strip()
        if text:
            if len(_compact_cache) >= _COMPACT_CACHE_MAX:
                for k in list(_compact_cache)[:_COMPACT_CACHE_MAX // 2]:
                    _compact_cache.pop(k, None)
            _compact_cache[session_id] = {"summary": text, "count": cut}
            print(f"[agent] 会话 {session_id} 上下文压缩：估算 {total} tokens 超阈值 "
                  f"{trigger}，已摘要前 {cut} 条消息（原始历史仍完整保留）", flush=True)
    except Exception as e:
        print(f"[agent] 上下文压缩失败（本轮回退纯窗口模式）: {e}", flush=True)


async def _pre_model_hook(state: dict, config=None):   # 参数名须为 config（RunnableCallable 按名注入）
    """pre_model_hook：每轮模型调用前改写模型视野。
    ① 命中摘要缓存时裁掉已摘要前缀、注入摘要 SystemMessage（尾部超触发线才裁）；
    ② 无摘要时总量超触发线才裁到近期窗口（否则全量保留）；
    ③ 切割点成对完整性同 _find_cut。
    返回 {"llm_input_messages": ...}——state 历史不动，只影响本次模型输入。"""
    from langchain_core.messages import SystemMessage

    from . import config as _cfg   # 局部别名：形参 config 会遮蔽模块名
    msgs = state.get("messages") or []
    sid = ""
    try:
        sid = (config or {}).get("configurable", {}).get("thread_id", "")
    except Exception:
        pass
    trigger = int(_cfg.CONTEXT_MAX_TOKENS * _cfg.CONTEXT_COMPACT_RATIO)
    view = list(msgs)
    cache = _compact_cache.get(sid)
    if cache and cache.get("summary"):
        # 摘要分支：裁掉已摘要前缀注入摘要；尾部只在超过触发线时裁
        # （预算用触发线而非近期窗口——尾部是摘要未覆盖的新内容，误裁会丢）
        n = min(int(cache["count"]), len(msgs))
        view = [SystemMessage(content=(
            "[会话早期工作摘要（原始消息已归档；以下摘要用于延续上下文）]\n"
            + cache["summary"]))] + view[n:]
        tail = _find_cut(view[1:], trigger)
        view = [view[0]] + view[1 + tail:]
    else:
        # 无摘要分支：总量仍在模型窗口内（≤触发线）就全量保留——
        # 此前无条件裁到近期窗口，导致 8k~触发线 区间的历史既无摘要又被裁丢
        total = sum(_est_tokens(m) for m in view)
        if total > trigger:
            view = view[_find_cut(view, _cfg.CONTEXT_RECENT_TOKENS):]
    return {"llm_input_messages": view}


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
    # 上下文压缩（v0.17.7）：轮前检查历史是否超阈值，超则滚动摘要（独立调用，
    # 不进主流事件流）；模型视野由 _pre_model_hook 在每次模型调用前裁剪
    await prepare_compaction(session_id)
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
                    # 推理模型的思维链（deepseek-reasoner/v4 类返回
                    # reasoning_content）：透出为 reasoning 事件供前端展示
                    # 参考；不落库正文（落库仍只收 token）
                    rc = (chunk.additional_kwargs or {}).get("reasoning_content")
                    if rc:
                        yield {"type": "reasoning", "content": rc}
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
