"""agent 运行时（LLM 链路离线部分）：MCP 加载降级、动态提示词注入、
checkpointer 生命周期、图热重载检测、中断提取、会话锁表、上下文压缩准备、
真实模式流式编排（事件回放/异常脱敏/审批 resume 全流程）。"""
import asyncio
from types import SimpleNamespace

import pytest

from server import agent, approval, config, custom_tools, skills
from server.state import STATE

from langchain_core.messages import (AIMessageChunk, HumanMessage, SystemMessage,
                                     ToolMessage)


def _code_for(name: str) -> str:
    """按工具名生成合法源码（文件名 = 函数名是落盘校验约定）。"""
    return (f'def {name}(x: int = 1) -> str:\n'
            f'    """演示工具。"""\n'
            f'    return f"x={{x}}"\n')


_MISSING = object()   # system_prompt_static 是真实模式图重建时才动态创建的属性


@pytest.fixture(autouse=True)
def _restore_state():
    """STATE 单例与 agent 模块内存态测试间复位。"""
    saved = (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm,
             STATE.tool_names, STATE.custom_sig, STATE.mcp_tool_list,
             dict(STATE.mcp_tools), dict(STATE.pack_tools))
    saved_sps = getattr(STATE, "system_prompt_static", _MISSING)
    saved_locks = dict(agent._session_locks)
    saved_cache = dict(agent._compact_cache)
    yield
    (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm,
     STATE.tool_names, STATE.custom_sig, STATE.mcp_tool_list) = saved[:7]
    STATE.mcp_tools, STATE.pack_tools = saved[7], saved[8]
    if saved_sps is _MISSING:
        if hasattr(STATE, "system_prompt_static"):
            delattr(STATE, "system_prompt_static")
    else:
        STATE.system_prompt_static = saved_sps
    agent._session_locks.clear()
    agent._session_locks.update(saved_locks)
    agent._compact_cache.clear()
    agent._compact_cache.update(saved_cache)


def _patch_ctx(monkeypatch, max_tokens, ratio, recent, hysteresis):
    monkeypatch.setattr(config, "CONTEXT_MAX_TOKENS", max_tokens)
    monkeypatch.setattr(config, "CONTEXT_COMPACT_RATIO", ratio)
    monkeypatch.setattr(config, "CONTEXT_RECENT_TOKENS", recent)
    monkeypatch.setattr(config, "CONTEXT_SUMMARY_HYSTERESIS_TOKENS", hysteresis)


# ---------------------------------------------------------------- MCP 与图构建

def test_load_mcp_tools_degrades_on_failure(monkeypatch):
    """MCP Server 不可用（脚本缺失/库未装）→ 空列表，仅用内置工具继续。"""
    monkeypatch.setattr(config, "MCP_SERVER_SCRIPT",
                        config.TOOL_WORKSPACE / "no_such_server.py")
    assert asyncio.run(agent.load_mcp_tools()) == []


def test_build_agent_mock_mode(monkeypatch):
    monkeypatch.setattr(config, "MOCK_LLM", True)
    asyncio.run(agent.build_agent())
    assert STATE.llm is None and STATE.agent is None   # 不连接模型
    assert STATE.tool_names                            # 仅登记工具名
    assert STATE.mcp_tool_list == []                    # MCP_ENABLED=false


def test_build_agent_real_mode(monkeypatch):
    """真实模式装配：LLM 实例 + MemorySaver + Agent 图 + 静态系统提示词。"""
    pytest.importorskip("langchain_openai")
    monkeypatch.setattr(config, "MOCK_LLM", False)
    asyncio.run(agent.build_agent())
    assert STATE.llm is not None
    assert STATE.agent is not None
    from langgraph.checkpoint.memory import MemorySaver
    assert isinstance(STATE.checkpointer, MemorySaver)
    assert "read_text_file" in STATE.tool_names
    assert STATE.system_prompt_static                 # 图重建时刷新的静态提示词


def test_dynamic_prompt_injects_personal_assets(login_as):
    """动态提示词：静态基座 + 本人专属技能/工具索引按请求身份注入。"""
    login_as("alice")
    skills.create_skill("al-skill", "专属技能", "# 指令")
    custom_tools.write_custom_tool(
        "al_tool", _code_for("al_tool"), scope=config.user_scope("alice"))
    STATE.system_prompt_static = "静态基座"
    out = agent._dynamic_prompt({"messages": [HumanMessage("你好")]})
    assert isinstance(out[0], SystemMessage)
    assert "静态基座" in out[0].content
    assert "al-skill" in out[0].content               # 专属技能索引
    assert "al_tool" in out[0].content               # 专属工具索引
    assert out[1].content == "你好"                   # 消息原样透传


# ---------------------------------------------------------------- checkpointer

def test_make_checkpointer_memory(monkeypatch):
    monkeypatch.setattr(config, "CHECKPOINTER", "memory")
    asyncio.run(agent._make_checkpointer())
    from langgraph.checkpoint.memory import MemorySaver
    assert isinstance(STATE.checkpointer, MemorySaver)


def test_make_checkpointer_postgres_fail_fast(monkeypatch):
    """postgres 连接失败 → 带指引的 RuntimeError（fail-fast，不静默回退）。"""
    pytest.importorskip("langgraph.checkpoint.postgres.aio")
    monkeypatch.setattr(config, "CHECKPOINTER", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    with pytest.raises(RuntimeError, match="连接失败"):
        asyncio.run(agent._make_checkpointer())


def test_close_checkpointer_noop_and_exit():
    STATE.checkpointer_cm = None
    asyncio.run(agent.close_checkpointer())           # None → no-op
    closed = []

    class _CM:
        async def __aexit__(self, *a):
            closed.append(a)
            return False

    STATE.checkpointer_cm = _CM()
    asyncio.run(agent.close_checkpointer())
    assert closed and STATE.checkpointer_cm is None


# ---------------------------------------------------------------- 热重载与中断

def test_maybe_reload_detects_tool_change(monkeypatch):
    monkeypatch.setattr(config, "MOCK_LLM", True)
    agent._rebuild_graph()                            # 基线签名
    custom_tools.write_custom_tool("fresh_tool", _code_for("fresh_tool"))
    agent.maybe_reload_custom_tools()
    assert "fresh_tool" in STATE.tool_names           # 免重启热挂载
    snap = list(STATE.tool_names)
    agent.maybe_reload_custom_tools()                 # 无变化 → 幂等
    assert STATE.tool_names == snap


def test_extract_interrupt():
    intr = SimpleNamespace(value={"kind": "approval", "tool": "t1", "args": {}})
    st = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[intr])])
    assert agent._extract_interrupt(st) == {"kind": "approval", "tool": "t1", "args": {}}
    assert agent._extract_interrupt(
        SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value="x")])])) is None
    assert agent._extract_interrupt(SimpleNamespace(tasks=[])) is None
    assert agent._extract_interrupt("bad-state") is None   # 异常 → None


def test_session_lock_reuse_and_eviction(monkeypatch):
    agent._session_locks.clear()
    l1 = agent.session_lock("a")
    assert agent.session_lock("a") is l1              # 未达阈值：已存在复用
    agent.session_lock("b")
    monkeypatch.setattr(agent, "_SESSION_LOCKS_MAX", 2)
    agent.session_lock("c")                           # 表满 → 清理未持锁项
    assert "c" in agent._session_locks
    assert "a" not in agent._session_locks and "b" not in agent._session_locks


# ---------------------------------------------------------------- 上下文压缩准备

class _FakeAgent:
    def __init__(self, msgs):
        self.msgs = msgs

    async def aget_state(self, cfg):
        return SimpleNamespace(values={"messages": self.msgs})


class _FakeLLM:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc = reply, exc
        self.calls = 0

    async def ainvoke(self, prompt, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return SimpleNamespace(content=self.reply)


def _long_msgs(n=6, chars=40):
    return [HumanMessage("数" * chars) for _ in range(n)]


def test_prepare_compaction_short_circuits(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_COMPACT_ENABLED", False)
    asyncio.run(agent.prepare_compaction("s-x"))       # 禁用
    monkeypatch.setattr(config, "CONTEXT_COMPACT_ENABLED", True)
    STATE.agent = None
    asyncio.run(agent.prepare_compaction("s-x"))       # 无图
    STATE.agent = _FakeAgent(_long_msgs())
    STATE.llm = None
    asyncio.run(agent.prepare_compaction("s-x"))       # 无 LLM
    STATE.llm = _FakeLLM("不该被调用")
    asyncio.run(agent.prepare_compaction("s-x"))       # 消息 < 6
    STATE.agent = _FakeAgent([HumanMessage("好") for _ in range(6)])
    asyncio.run(agent.prepare_compaction("s-x"))       # 总量 ≤ 触发线
    assert agent._compact_cache == {}


def test_prepare_compaction_summarizes(monkeypatch):
    _patch_ctx(monkeypatch, 200, 0.6, 30, 100)          # 触发线 120
    sid = "s-sum"
    STATE.agent = _FakeAgent(_long_msgs())              # 6 条 ≈ 288 tokens
    llm = _FakeLLM("滚动摘要文本")
    STATE.llm = llm
    asyncio.run(agent.prepare_compaction(sid))
    assert llm.calls == 1
    cache = agent._compact_cache[sid]
    assert cache["summary"] == "滚动摘要文本"
    assert cache["count"] == 5                          # 窗口外前 5 条已摘要


def test_prepare_compaction_hysteresis(monkeypatch):
    """已有摘要且新增段小于滞回阈值 → 沿用旧摘要不重摘（防每轮烧 token）。"""
    _patch_ctx(monkeypatch, 200, 0.6, 30, 200)
    sid = "s-hys"
    agent._compact_cache[sid] = {"summary": "旧摘要", "count": 2}
    STATE.agent = _FakeAgent(_long_msgs())
    llm = _FakeLLM("新摘要")
    STATE.llm = llm
    asyncio.run(agent.prepare_compaction(sid))
    assert llm.calls == 0                              # 未重摘
    assert agent._compact_cache[sid]["summary"] == "旧摘要"

    _patch_ctx(monkeypatch, 200, 0.6, 30, 100)          # 新增段 ≥ 滞回阈值 → 重摘
    asyncio.run(agent.prepare_compaction(sid))
    assert llm.calls == 1
    assert agent._compact_cache[sid]["summary"] == "新摘要"
    assert agent._compact_cache[sid]["count"] == 5


def test_prepare_compaction_already_covered_skips(monkeypatch):
    _patch_ctx(monkeypatch, 200, 0.6, 30, 100)
    sid = "s-done"
    agent._compact_cache[sid] = {"summary": "已覆盖", "count": 99}   # count ≥ cut
    STATE.agent = _FakeAgent(_long_msgs())
    llm = _FakeLLM("x")
    STATE.llm = llm
    asyncio.run(agent.prepare_compaction(sid))
    assert llm.calls == 0


def test_prepare_compaction_llm_failure_falls_back(monkeypatch):
    """摘要调用失败 → 本轮回退纯窗口模式（缓存不写入、不中断对话）。"""
    _patch_ctx(monkeypatch, 200, 0.6, 30, 100)
    sid = "s-fail"
    STATE.agent = _FakeAgent(_long_msgs())
    STATE.llm = _FakeLLM(exc=RuntimeError("summary api down"))
    asyncio.run(agent.prepare_compaction(sid))
    assert sid not in agent._compact_cache


def test_prepare_compaction_evicts_old_cache_entries(monkeypatch):
    _patch_ctx(monkeypatch, 200, 0.6, 30, 100)
    monkeypatch.setattr(agent, "_COMPACT_CACHE_MAX", 2)
    agent._compact_cache.clear()
    agent._compact_cache["old1"] = {"summary": "s", "count": 1}
    agent._compact_cache["old2"] = {"summary": "s", "count": 1}
    sid = "s-evict"
    STATE.agent = _FakeAgent(_long_msgs())
    STATE.llm = _FakeLLM("新摘要")
    asyncio.run(agent.prepare_compaction(sid))
    assert "old1" not in agent._compact_cache          # 超限先清一半
    assert sid in agent._compact_cache


# ---------------------------------------------------------------- 流式编排（真实分支）

class _StreamAgent:
    """按脚本回放 astream 事件；aget_state 无中断。"""

    def __init__(self, events):
        self.events = events

    async def astream_events(self, payload, config=None, version="v2"):
        for ev in self.events:
            yield ev

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


class _FailAgent:
    async def astream_events(self, payload, config=None, version="v2"):
        raise RuntimeError("C:\\abs\\path 与密钥细节")
        yield                                           # pragma: no cover

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


class _IntrAgent:
    """第一轮跑完报审批中断；resume 后的第二轮正常结束。"""

    def __init__(self, events1, events2):
        self.stage = 0
        self._events = [events1, events2]

    async def astream_events(self, payload, config=None, version="v2"):
        for ev in self._events[self.stage]:
            yield ev
        self.stage += 1

    async def aget_state(self, cfg):
        if self.stage == 1:
            intr = SimpleNamespace(value={"kind": "approval",
                                           "tool": "delete_workspace_file",
                                           "args": {"filename": "x.txt"}})
            return SimpleNamespace(tasks=[SimpleNamespace(interrupts=[intr])])
        return SimpleNamespace(tasks=[])


def _collect_stream(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.run(_run())


def test_stream_reply_event_pipeline(monkeypatch):
    """token/reasoning/tool 事件透出，ma_worker 中间 token 被过滤。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None                                   # 压缩准备短路
    STATE.agent = _StreamAgent([
        {"event": "on_chat_model_stream", "tags": ["ma_worker"],
         "data": {"chunk": AIMessageChunk(content="worker中间token")}},
        {"event": "on_chat_model_stream", "tags": [],
         "data": {"chunk": AIMessageChunk(
             content="答", additional_kwargs={"reasoning_content": "思考片段"})}},
        {"event": "on_tool_start", "name": "read_text_file"},
        {"event": "on_tool_end", "name": "read_text_file",
         "data": {"output": ToolMessage(content="文件内容", tool_call_id="c1")}},
    ])
    agent._rebuild_graph()                             # 对齐签名，避免热重载噪音
    out = _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "你好"}], session_id="s-ev", user_id="u1", run_id=""))
    types = [e["type"] for e in out]
    assert types[0] == "reasoning"                     # 思维链先透出（不落库正文）
    assert "token" in types and "tool" in types
    assert types[-1] == "done"
    tokens = "".join(e["content"] for e in out if e["type"] == "token")
    assert "worker中间token" not in tokens             # 多 Agent 子图 token 过滤
    assert "答" in tokens
    tool_evs = [e for e in out if e["type"] == "tool"]
    assert tool_evs[0]["status"] == "start" and tool_evs[0]["name"] == "read_text_file"
    assert tool_evs[1]["status"] == "end" and tool_evs[1]["result"] == "文件内容"
    assert {"run", "session"} <= set(out[-1]["usage"])


def test_stream_reply_error_sanitized(monkeypatch):
    """模型调用失败：error 事件脱敏（不带原始异常文本），提前结束无 done。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    STATE.agent = _FailAgent()
    agent._rebuild_graph()
    out = _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "你好"}], session_id="s-err", user_id="u1"))
    assert out[-1]["type"] == "error"
    assert "模型调用失败" in out[-1]["content"]
    assert "密钥细节" not in out[-1]["content"]       # 异常原文不进 SSE
    assert all(e["type"] != "done" for e in out)


def _approve_when_pending(tool: str, approve: bool):
    """后台任务：轮询审批表，出现 pending 的目标审批立即决议。"""
    async def _task():
        for _ in range(300):
            recs = approval.list_recent(limit=20)
            pend = [r for r in recs
                    if r["tool"] == tool and r["status"] == "pending"]
            if pend:
                approval.resolve(pend[0]["aid"], approve, "boss", "approver")
                return
            await asyncio.sleep(0.05)
        raise AssertionError("审批记录未出现")
    return _task()


def test_stream_reply_approval_resume_flow(monkeypatch):
    """审批中断全链路：中断登记 → approval_required → 批准 resume → executed 标记。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    tok = lambda s: {"event": "on_chat_model_stream", "tags": [],
                     "data": {"chunk": AIMessageChunk(content=s)}}
    STATE.agent = _IntrAgent([tok("准备删除")], [tok("已执行")])
    agent._rebuild_graph()

    async def scenario():
        evs = []
        t = asyncio.create_task(_approve_when_pending("delete_workspace_file", True))
        try:
            async for ev in agent.stream_reply(
                    [{"role": "user", "content": "删除文件"}],
                    session_id="s-appr", user_id="alice", run_id=""):
                evs.append(ev)
        finally:
            await t
        return evs

    evs = asyncio.run(scenario())
    types = [e["type"] for e in evs]
    assert "approval_required" in types
    assert types[-1] == "done"
    ap = [e for e in evs if e["type"] == "approval_required"][0]
    assert ap["tool"] == "delete_workspace_file"
    # 批准且 resume 完成 → executed_at 已标记（审计可区分「批准但未执行」）
    rec = [r for r in approval.list_recent(limit=10)
           if r["aid"] == ap["approval_id"]][0]
    assert rec["status"] == "approved" and rec["executed_at"]
    tokens = "".join(e["content"] for e in evs if e["type"] == "token")
    assert "准备删除" in tokens and "已执行" in tokens   # resume 轮事件继续透出
