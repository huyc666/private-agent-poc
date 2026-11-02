"""删除与在途 chat 并发的终局语义（spec：docs/specs/security-hardening-v0.17.38.md AC1-AC6）。

复核报告 docs/reviews/2026-11-01-v0.17.37-commit-rereview.md §1 P0 / §5 准入条件：
tombstone 只拒绝新请求，不终止在途写入——DELETE 返回成功后不得出现新消息、
新 pending 审批、新 checkpoint，批准竞争下危险工具不得 resume 执行。

确定性原则：全部 threading.Event / 受控交错点驱动（fake 图在 aget_state /
wait_decision 内联执行删除），零 sleep 依赖。
"""
import asyncio
from types import SimpleNamespace

import pytest

from langgraph.checkpoint.memory import MemorySaver

from server import agent, approval, auth, config, sessions, usage
from server.state import STATE


@pytest.fixture(autouse=True)
def _restore_state():
    """STATE 单例与 agent 内存态测试间复位（沿用 test_agent_llm 先例）。"""
    saved = (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm)
    saved_locks = dict(agent._session_locks)
    saved_cache = dict(agent._compact_cache)
    yield
    (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm) = saved
    agent._session_locks.clear()
    agent._session_locks.update(saved_locks)
    agent._compact_cache.clear()
    agent._compact_cache.update(saved_cache)


@pytest.fixture
def api(client_factory):
    with client_factory() as c:
        yield c


def _mk_user(username, role="user"):
    key = auth.add_user(username, role=role)
    assert key, f"用户 {username} 创建失败"
    return key


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _collect_stream(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.run(_run())


def _sse_events(text):
    """从 SSE 响应文本解析事件 dict 列表（data: 行）。"""
    import json
    out = []
    for ln in text.splitlines():
        if ln.startswith("data: "):
            try:
                out.append(json.loads(ln[6:]))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------- AC1 流中途删除


def test_ac1_delete_mid_stream_discards_assistant(client_factory, monkeypatch):
    """AC1（报告验收 1）：模型流中途删除——DELETE 完成后 assistant 残段不得
    落库（fenced append 拒），用户消息已被 delete_session 清除，客户端收到
    删除中止 error 事件。受控交错：伪造流在两个 token 之间经独立客户端内联
    执行 DELETE（服务端顺序 await 保证交错点，零 sleep、零客户端流式依赖——
    httpx ASGITransport 缓冲整包响应，TestClient 无法增量消费 SSE）。"""
    key = _mk_user("alice")
    sid = "s-dc1"

    def _do_delete():
        with client_factory() as c2:
            assert c2.delete(f"/api/sessions/{sid}",
                             headers=_hdr(key)).status_code == 200

    async def fake_stream(messages, session_id="default", user_id="anonymous",
                          run_id="", **kw):
        yield {"type": "token", "content": "部分回复"}
        # 受控交错点：流在途（本轮 chat 仍持有锁与租约）→ 独立连接完成 DELETE
        await asyncio.to_thread(_do_delete)
        yield {"type": "token", "content": "后续残段"}
        yield {"type": "done", "usage": {}}

    monkeypatch.setattr(agent, "stream_reply", fake_stream)
    with client_factory() as api:
        r = api.post("/api/chat", headers=_hdr(key),
                     json={"session_id": sid, "message": "机密"})
    assert r.status_code == 200
    evs = _sse_events(r.text)
    assert any(e.get("type") == "token" and "部分回复" in e.get("content", "")
               for e in evs), "前置：流确实在途产出过部分回复"
    # DELETE 后存储不增：assistant 残段被 fenced append 拒，user 消息已被清除
    assert sessions.history(sid) == []
    # 客户端收到删除中止 error 事件
    assert any(e.get("type") == "error" and "删除" in e.get("content", "")
               for e in evs), f"应收到删除中止 error 事件，实际：{evs}"


# -------------------------------------- AC2 pending 创建点 fencing（agent 层）


class _DelAtInterruptAgent:
    """round1 完成后的中断检查（aget_state）内联执行会话删除——删除发生在
    「创建审批」提交点之前，恰好构成 AC2 的确定性交错。"""

    def __init__(self, sid):
        self.sid = sid
        self.streams = 0

    async def astream_events(self, payload, config=None, version="v2"):
        self.streams += 1
        return
        yield

    async def aget_state(self, cfg):
        sessions.mark_deleted(self.sid)
        intr = SimpleNamespace(value={"kind": "approval",
                                      "tool": "create_skill", "args": {"name": "x"}})
        return SimpleNamespace(tasks=[SimpleNamespace(interrupts=[intr])])


def test_ac2_approval_creation_refused_after_delete(monkeypatch):
    """AC2（报告验收 2）：pending 创建点 fencing——interrupt 出现时该会话
    已删除：审批不得创建（无新 pending）、流中止不等待不 resume、SSE error。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None                             # 压缩准备短路
    sid = "s-dc2"
    assert sessions.register_session(sid, "alice") == "ok"
    STATE.agent = _DelAtInterruptAgent(sid)
    agent._rebuild_graph()                       # 对齐签名，避免热重载噪音

    out = _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "删"}], session_id=sid, user_id="alice",
        run_id="", generation=0))
    types = [e["type"] for e in out]
    # 审批未创建：无 approval_required 事件、approvals 表无该会话新记录
    assert "approval_required" not in types
    assert [r for r in approval.list_recent(limit=50) if r["session_id"] == sid] == []
    # 流中止：error 事件收尾，无 done
    assert types[-1] == "error"
    assert "删除" in out[-1]["content"]
    assert STATE.agent.streams == 1             # 无 resume 轮


# -------------------------------------- AC3 删除与批准竞争（agent 层）


class _RaceAgent:
    """round1 完成 → 审批中断；round2（resume 轮，含危险工具执行）绝不应发生。"""

    def __init__(self):
        self.streams = 0

    async def astream_events(self, payload, config=None, version="v2"):
        self.streams += 1
        return
        yield

    async def aget_state(self, cfg):
        if self.streams == 1:
            intr = SimpleNamespace(value={
                "kind": "approval", "tool": "delete_workspace_file",
                "args": {"filename": "x.txt"}})
            return SimpleNamespace(tasks=[SimpleNamespace(interrupts=[intr])])
        return SimpleNamespace(tasks=[])


def test_ac3_approve_delete_race_no_dangerous_resume(monkeypatch):
    """AC3（报告验收 3）：批准与删除竞争——批准落库后、resume 执行前会话被删：
    危险工具绝不执行（无 resume 轮），审批留 approved-unexecuted（不
    mark_executed，交给 abandoned 扫描），SSE error 收尾。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    sid = "s-dc3"
    assert sessions.register_session(sid, "alice") == "ok"
    fake_agent = _RaceAgent()
    STATE.agent = fake_agent
    agent._rebuild_graph()

    async def fake_wait(aid, timeout=approval.APPROVAL_TIMEOUT):
        """受控交错点：批准落库 → 会话删除 → 返回 True（模拟『批准刚到、
        删除随后完成』的竞争窗口），验证 resume 前的终局校验兜住。"""
        approval.resolve(aid, True, "boss", "approver")
        sessions.mark_deleted(sid)
        return True

    monkeypatch.setattr(approval, "wait_decision", fake_wait)
    out = _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "删除文件"}], session_id=sid,
        user_id="alice", run_id="", generation=0))
    types = [e["type"] for e in out]
    # 危险工具未执行：resume 轮（第二轮 astream_events）绝未发生
    assert fake_agent.streams == 1, "resume 轮不得启动——危险工具会被执行"
    assert types[-1] == "error"
    assert "删除" in out[-1]["content"]
    # 审批留 approved-unexecuted：不 mark_executed（abandoned 扫描的 fail-closed 链路）
    rec = [r for r in approval.list_recent(limit=10) if r["session_id"] == sid][0]
    assert rec["status"] == "approved" and rec["executed_at"] is None
    assert all(e["type"] != "done" for e in out)


# --------------------------------------------- AC4 checkpoint fencing（单元）


class _RecordingCP:
    def __init__(self):
        self.calls = []

    async def aput(self, config, *a, **kw):
        self.calls.append(("aput", config["configurable"]["thread_id"]))

    async def aput_writes(self, config, *a, **kw):
        self.calls.append(("aput_writes", config["configurable"]["thread_id"]))

    def put(self, config, *a, **kw):
        self.calls.append(("put", config["configurable"]["thread_id"]))

    def put_writes(self, config, *a, **kw):
        self.calls.append(("put_writes", config["configurable"]["thread_id"]))

    async def adelete_thread(self, tid):
        self.calls.append(("adelete_thread", tid))

    async def aget_tuple(self, config):
        self.calls.append(("aget_tuple", config["configurable"]["thread_id"]))
        return None


def _cfg(sid):
    return {"configurable": {"thread_id": sid}}


async def _aiter_collect(agen):
    return [x async for x in agen]


def test_ac4_aput_returns_inner_config():
    """N2（外部复验 2026-11-02-v0.17.39-8bba994-review §5）：aput/put 必须
    透传 inner 返回值——BaseCheckpointSaver 契约返回 RunnableConfig，
    langgraph time-travel/update_state/fork 路径（pregel main.py aupdate_state
    等）用返回值构造 next_config，吞掉会静默丢 config。"""
    sentinel = {"configurable": {"thread_id": "t-ret", "checkpoint_id": "new-id"}}

    class _RetCP:
        async def aput(self, config, *a, **kw):
            return sentinel

        def put(self, config, *a, **kw):
            return sentinel

    fcp = agent.FencedCheckpointer(_RetCP())
    sessions.register_session("t-ret", "alice")
    assert asyncio.run(fcp.aput(_cfg("t-ret"), object(), None, {})) is sentinel
    assert fcp.put(_cfg("t-ret"), object(), None, {}) is sentinel


def test_ac4_alist_passthrough_async_generator():
    """N3（外部复验 §5）：基类 alist 是 async generator——委托不得经
    async def 包装成 coroutine（aget_state_history 等历史回溯路径直接
    async for，coroutine 无 __aiter__ 会 TypeError）。"""
    class _ListCP:
        async def alist(self, config, *a, **kw):
            yield "cp1"
            yield "cp2"

    fcp = agent.FencedCheckpointer(_ListCP())
    out = asyncio.run(_aiter_collect(fcp.alist(_cfg("any"))))
    assert out == ["cp1", "cp2"]


def test_ac4_real_graph_history_and_update_state():
    """N2+N3 集成回归：真实编译图经 FencedCheckpointer——aget_state_history
    （alist 路径）与 aupdate_state（aput 返回值构造 next_config 路径）均正常。"""
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class S(TypedDict):
        v: int

    g = StateGraph(S)
    g.add_node("bump", lambda s: {"v": s["v"] + 1})
    g.add_edge(START, "bump")
    g.add_edge("bump", END)
    cg = g.compile(checkpointer=agent.FencedCheckpointer(MemorySaver()))
    cfg = {"configurable": {"thread_id": "t-hist"}}
    sessions.register_session("t-hist", "alice")
    asyncio.run(cg.ainvoke({"v": 0}, cfg))
    # N3：历史回溯（内部走 checkpointer.alist）
    hist = asyncio.run(_aiter_collect(cg.aget_state_history(cfg)))
    assert len(hist) >= 2
    # N2：update_state 用 aput 返回值构造 next_config——吞返回值则
    # checkpoint_id 为 None
    new_cfg = asyncio.run(cg.aupdate_state(cfg, {"v": 5}))
    assert new_cfg["configurable"].get("checkpoint_id") is not None


def test_ac4_fenced_checkpointer_blocks_deleted_thread():
    """AC4：FencedCheckpointer——活跃线程写正常透传；tombstoned 线程的
    aput/aput_writes（含同步 put/put_writes）抛 SessionDeletedError；
    adelete_thread 与读路径不受影响；非目标线程不受影响。"""
    inner = _RecordingCP()
    fcp = agent.FencedCheckpointer(inner)
    sessions.register_session("t-live", "alice")
    sessions.mark_deleted("t-dead")
    # 活跃线程正常
    asyncio.run(fcp.aput(_cfg("t-live"), object(), None, {}))
    asyncio.run(fcp.aput_writes(_cfg("t-live"), [], "task1"))
    assert ("aput", "t-live") in inner.calls
    # 已删除线程：一切写提交 fail-closed
    with pytest.raises(sessions.SessionDeletedError):
        asyncio.run(fcp.aput(_cfg("t-dead"), object(), None, {}))
    with pytest.raises(sessions.SessionDeletedError):
        asyncio.run(fcp.aput_writes(_cfg("t-dead"), [], "task1"))
    with pytest.raises(sessions.SessionDeletedError):
        fcp.put(_cfg("t-dead"), object(), None, {})
    with pytest.raises(sessions.SessionDeletedError):
        fcp.put_writes(_cfg("t-dead"), [], "task1")
    assert ("aput", "t-dead") not in inner.calls
    # 读路径 / 删除路径透传不受影响
    asyncio.run(fcp.aget_tuple(_cfg("t-dead")))
    asyncio.run(fcp.adelete_thread("t-live"))
    assert ("adelete_thread", "t-live") in inner.calls


def test_o1_mock_branch_also_fenced(monkeypatch):
    """O1（外部复验观察项，用户裁决修复）：MOCK_LLM 演示分支同样过代际
    fencing——在途删除（代际过期）后 mock 流不得产出，error 事件收尾。"""
    monkeypatch.setattr(config, "MOCK_LLM", True)
    sid = "s-mock-fence"
    assert sessions.register_session(sid, "alice") == "ok"
    sessions.mark_deleted(sid)                     # 代际过期（0 → 1）
    out = _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "hi"}], session_id=sid, user_id="alice",
        run_id="", generation=0))
    assert out[-1]["type"] == "error" and "删除" in out[-1]["content"]
    assert all(e["type"] not in ("token", "done") for e in out)


def test_ac4_make_checkpointer_wraps_memory(monkeypatch):
    """AC4：装配点——_make_checkpointer 产出 FencedCheckpointer 包装的
    MemorySaver（写提交校验在图执行链路上生效）。"""
    monkeypatch.setattr(config, "CHECKPOINTER", "memory")
    asyncio.run(agent._make_checkpointer())
    assert isinstance(STATE.checkpointer, agent.FencedCheckpointer)
    assert isinstance(STATE.checkpointer._inner, MemorySaver)


def test_ac4_with_allowlist_keeps_fence():
    """AC4 评审 C1 回归防线：langgraph 在 LANGGRAPH_STRICT_MSGPACK=true 时经
    checkpointer.with_allowlist(...) 取派生 saver——裸透传 inner 会返回无栅栏
    对象、该路径的 checkpoint 写静默绕过 tombstone 校验；须重套
    FencedCheckpointer，栅栏语义在任何派生 saver 上一致。"""
    inner = _RecordingCP()
    inner.with_allowlist = lambda allow: _RecordingCP()   # 模拟 inner 的派生包装
    fcp = agent.FencedCheckpointer(inner)
    derived = fcp.with_allowlist({("langchain_core", "AIMessage")})
    assert isinstance(derived, agent.FencedCheckpointer)
    assert isinstance(derived._inner, _RecordingCP)
    # 栅栏语义在派生对象上保持：tombstoned 线程写拒、活跃线程写透传
    sessions.mark_deleted("t-dead-al")
    with pytest.raises(sessions.SessionDeletedError):
        asyncio.run(derived.aput(_cfg("t-dead-al"), object(), None, {}))
    asyncio.run(derived.aput(_cfg("t-live-al"), object(), None, {}))
    assert ("aput", "t-live-al") in derived._inner.calls
    assert ("aput", "t-dead-al") not in derived._inner.calls


class _FencedDeathAgent:
    """AC4 评审 C4 回归防线：在途图执行在 checkpoint 写点死亡
    （FencedCheckpointer 抛 SessionDeletedError 沿 astream_events 冒泡）——
    stream_reply 须以删除中止文案收尾，不得落入通用分支误报「模型调用失败」。"""

    async def astream_events(self, payload, config=None, version="v2"):
        raise sessions.SessionDeletedError("会话已删除，checkpoint 写入被拒")
        yield  # noqa: unreachable——使本函数成为 async generator（与调用方 async for 契约一致）

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


def test_ac4_session_deleted_error_not_misreported(monkeypatch):
    """AC4 评审 C4 回归防线：SessionDeletedError ≠ 模型调用失败——文案须与
    三处栅栏点统一为「会话已被删除，本轮回复已中止」（错误归因误导用户）。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    sid = "s-dc4c"
    assert sessions.register_session(sid, "alice") == "ok"
    STATE.agent = _FencedDeathAgent()
    agent._rebuild_graph()
    out = _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "触发在途删除"}], session_id=sid,
        user_id="alice", run_id="", generation=0))
    assert [e["type"] for e in out] == ["error"]
    assert out[-1]["content"] == "会话已被删除，本轮回复已中止"
    assert "模型调用失败" not in out[-1]["content"]


# ------------------------------------------------- AC5 fenced 原子性（单元）


def test_ac5_append_fenced_and_generation():
    """AC5：append_fenced——代际匹配写入成功；删除后（tombstone + 旧代际）拒
    且库中无新增；fence_value/fence_ok 口径；mark_deleted 使 generation 递增。"""
    sid = "s-dc5a"
    assert sessions.register_session(sid, "alice") == "ok"
    assert sessions.get_meta(sid)["generation"] == 0
    assert sessions.fence_value(sid) == 0
    assert sessions.fence_ok(sid, 0) is True
    assert sessions.append_fenced(sid, 0, "alice", "user", "hi") is True
    assert len(sessions.history(sid)) == 1
    sessions.mark_deleted(sid)
    # 删除后：代际递增 + 旧代际写入全拒
    assert sessions.get_meta(sid)["generation"] == 1
    assert sessions.fence_value(sid) is None
    assert sessions.fence_ok(sid, 0) is False
    assert sessions.append_fenced(sid, 0, "alice", "assistant", "残段") is False
    assert len(sessions.history(sid)) == 1           # 无新增
    # 无 meta 行的活跃会话 = 代际 0（开放模式新会话）
    assert sessions.fence_value("never-seen") == 0
    assert sessions.fence_ok("never-seen", 0) is True
    assert sessions.append_fenced("never-seen", 0, "u", "user", "x") is True


def test_ac5_create_approval_fenced():
    """AC5：create_approval_fenced——活跃代际允许并落 pending（含归属列）；
    删除后拒（无新 pending）；无 meta 行的新会话按代际 0 允许。"""
    sid = "s-dc5b"
    usage.current_user.set("alice")
    usage.current_session.set(sid)
    aid = approval.create_approval_fenced(sid, 0, "create_skill", {"name": "x"})
    assert aid
    rec = [r for r in approval.list_recent(limit=10) if r["aid"] == aid][0]
    assert rec["status"] == "pending" and rec["user_id"] == "alice"
    sessions.mark_deleted(sid)
    assert approval.create_approval_fenced(sid, 0, "create_skill", {}) is None
    aid2 = approval.create_approval_fenced("fresh-5b", 0, "create_skill", {})
    assert aid2


# ------------------------------------------------- AC6 开放模式与代际语义


def test_ac6_open_mode_tombstone_chat_rejected(api, monkeypatch):
    """AC6：开放模式口径统一——tombstoned 会话 chat 以 SSE error 终局拒绝、
    不落库；活跃会话 fenced 写正常。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    sid = "s-dc6"
    r1 = api.post("/api/chat", json={"session_id": sid, "message": "第一轮"})
    assert r1.status_code == 200
    assert len(sessions.history(sid)) == 2            # user + mock assistant
    assert api.delete(f"/api/sessions/{sid}").status_code == 200
    r2 = api.post("/api/chat", json={"session_id": sid, "message": "复用已删会话"})
    assert r2.status_code == 200
    evs = _sse_events(r2.text)
    assert any(e.get("type") == "error" and "删除" in e.get("content", "")
               for e in evs), f"应收到删除中止 error 事件，实际：{evs}"
    assert sessions.history(sid) == []                # DELETE 后存储不增
