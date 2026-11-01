"""P0-1 会话删除生命周期封禁（spec：docs/specs/security-hardening-v0.17.37.md AC1-AC5）。

审核报告 docs/reviews/2026-11-01-v0.17.36-architecture-review.md §3（P0）：
- DELETE 端点只清 messages——checkpointer / 进程内压缩缓存 / 挂起审批 / 会话
  租约全部残留，删除后「模型记忆」仍可被接续；
- 归属判定靠 messages 推断，owners 空 = 可创建新会话——删除后同 ID 复用
  可接续旧模型上下文（跨用户上下文泄露 + 隐私语义不一致）。

修复语义（spec 第三节）：删除 = 不可逆终局。session_meta 所有权表 + tombstone
永久封禁 + _purge_session 覆盖全存储面 + 服务端生成不可预测新会话 ID。
"""
import asyncio
import re
import sqlite3

import pytest

from server import agent, approval, auth, config, session_lock, sessions, usage
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


# ---------------------------------------------------------------- 工具


def _mk_user(username, role="user"):
    key = auth.add_user(username, role=role)
    assert key, f"用户 {username} 创建失败"
    return key


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _write_real_checkpoint(sid):
    """离线产真实 LangGraph 历史：迷你 StateGraph + MemorySaver 写 checkpoint
    （不依赖任何模型），并把 checkpointer 挂进 STATE（删除编排经 STATE 生效）。"""
    from typing_extensions import TypedDict

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    class _S(TypedDict, total=False):
        x: str

    cp = MemorySaver()
    g = StateGraph(_S)
    g.add_node("n", lambda s: {"x": "done"})
    g.add_edge(START, "n")
    g.add_edge("n", END)
    graph = g.compile(checkpointer=cp)
    asyncio.run(graph.ainvoke({"x": "机密历史"}, {"configurable": {"thread_id": sid}}))
    STATE.checkpointer = cp
    return cp


def _thread_exists(cp, sid):
    return asyncio.run(cp.aget_tuple({"configurable": {"thread_id": sid}})) is not None


def _lock_rows(sid):
    conn = sqlite3.connect(config.STATE_DB)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM session_locks WHERE session_id = ?",
            (sid,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- AC1 真实 checkpoint


def test_ac1_reuse_after_delete_rejected_and_checkpoint_gone(api):
    """AC1（报告验收 1）：A 产生真实 checkpoint 历史并删除 → B 同 ID chat 必须
    403，且 MemorySaver 中该 thread 已物理删除（模型上下文不可接续）。"""
    key_a = _mk_user("alice")
    key_b = _mk_user("bob")
    sid = "s-ac1"
    r = api.post("/api/chat", headers=_hdr(key_a),
                 json={"session_id": sid, "message": "机密内容"})
    assert r.status_code == 200
    cp = _write_real_checkpoint(sid)
    assert _thread_exists(cp, sid)          # 前置：真实历史在场
    assert api.delete(f"/api/sessions/{sid}", headers=_hdr(key_a)).status_code == 200
    # B 复用同 ID：拒绝，旧模型上下文不可接续
    r = api.post("/api/chat", headers=_hdr(key_b),
                 json={"session_id": sid, "message": "接续旧上下文"})
    assert r.status_code == 403
    # checkpointer 物理删除：thread 不存在
    assert not _thread_exists(cp, sid)


def test_ac2_owner_reuse_after_delete_403(api):
    """AC2（报告验收 2）：A 删除后本人复用同 ID → 403——删除是不可逆终局，
    不得以「旧 ID 开新会话」语义重建（无新上下文可创建）。"""
    key_a = _mk_user("alice")
    sid = "s-ac2"
    assert api.post("/api/chat", headers=_hdr(key_a),
                    json={"session_id": sid, "message": "第一轮"}).status_code == 200
    assert api.delete(f"/api/sessions/{sid}", headers=_hdr(key_a)).status_code == 200
    r = api.post("/api/chat", headers=_hdr(key_a),
                 json={"session_id": sid, "message": "本人复用"})
    assert r.status_code == 403


def test_ac3_purge_covers_all_stores(api):
    """AC3（报告验收 3）：删除覆盖面——展示记录 / checkpointer（接口级语义，
    MemorySaver 实删见 AC1）/ 压缩摘要缓存 / 挂起审批 fail-closed / 会话租约。"""
    key_a = _mk_user("alice")
    sid = "s-ac3"

    class _FakeCP:
        """Postgres 语义接口替身：仅记录 adelete_thread 调用。"""

        def __init__(self):
            self.deleted = []

        async def adelete_thread(self, tid):
            self.deleted.append(tid)

    fake = _FakeCP()
    STATE.checkpointer = fake
    agent._compact_cache[sid] = {"summary": "旧摘要", "count": 3}
    assert api.post("/api/chat", headers=_hdr(key_a),
                    json={"session_id": sid, "message": "机密"}).status_code == 200
    usage.current_user.set("alice")
    usage.current_session.set(sid)
    aid = approval.create_approval("create_skill", {"name": "demo"})
    # 模拟并发在途请求持有租约（在 chat 之后抢注，避免本请求的 SessionLease
    # 空等 TTL 过期；DELETE 编排须清租约，使其下一拍续租失败自动中止）
    assert session_lock.acquire(sid, "owner-run-1")
    assert api.delete(f"/api/sessions/{sid}", headers=_hdr(key_a)).status_code == 200
    assert sessions.history(sid) == []                 # 展示记录清空
    assert fake.deleted == [sid]                       # checkpointer 已删
    assert sid not in agent._compact_cache             # 压缩摘要缓存已清
    rec = [a for a in approval.list_recent() if a["aid"] == aid][0]
    assert rec["status"] == "rejected"                 # 挂起审批 fail-closed
    assert rec["decided_by"] == "system"
    assert _lock_rows(sid) == 0                        # 会话租约已清


def test_ac4_tombstone_persists_and_concurrent_registration(api, client_factory):
    """AC4（报告验收 4）：tombstone 跨「重启」（新客户端实例 = 新进程）仍拒；
    两用户并发注册同一新 ID 恰一人成功；已删除 ID 永不再注册。"""
    key_a = _mk_user("alice")
    key_b = _mk_user("bob")
    sid = "s-ac4"
    assert api.post("/api/chat", headers=_hdr(key_a),
                    json={"session_id": sid, "message": "x"}).status_code == 200
    assert api.delete(f"/api/sessions/{sid}", headers=_hdr(key_a)).status_code == 200
    with client_factory() as c2:   # 新实例等价重启（SQLite 每操作独立连接）
        r = c2.post("/api/chat", headers=_hdr(key_a),
                    json={"session_id": sid, "message": "重启后复用"})
        assert r.status_code == 403
        meta = sessions.get_meta(sid)
        assert meta is not None and meta["deleted"] is not None
    # 并发注册同一新 ID：恰一人 ok（SQLite 写锁原子性）
    r1 = sessions.register_session("fresh-id", "alice")
    r2 = sessions.register_session("fresh-id", "bob")
    assert sorted((r1, r2)) == ["ok", "taken"]
    assert sessions.get_meta("fresh-id")["owner_id"] in ("alice", "bob")
    # tombstone ID 不得被任何人注册
    assert sessions.register_session(sid, "bob") == "deleted"


def test_ac5_legacy_session_backfill(api):
    """AC5（存量兼容）：messages 有而 meta 无的遗留会话——合法 owner 续聊
    放行并回填归属；他用户仍拒。既有行为回归由 test_session_acl.py 全量守护。"""
    key_a = _mk_user("alice")
    key_b = _mk_user("bob")
    sid = "s-legacy5"
    sessions.append(sid, "alice", "user", "存量消息")   # 直写库 = 升级遗留态
    r = api.post("/api/chat", headers=_hdr(key_a),
                 json={"session_id": sid, "message": "续聊"})
    assert r.status_code == 200
    meta = sessions.get_meta(sid)
    assert meta is not None and meta["owner_id"] == "alice"
    assert meta["deleted"] is None
    r = api.post("/api/chat", headers=_hdr(key_b),
                 json={"session_id": sid, "message": "越权"})
    assert r.status_code == 403


def test_tombstone_history_delete_404(api):
    """spec 3.2.3：tombstone 会话对非 admin 按不存在处理（history/delete 404）；
    admin 全局视图不受影响（消息已清，但治理语义保留）。"""
    key_a = _mk_user("alice")
    sid = "s-gone"
    assert api.post("/api/chat", headers=_hdr(key_a),
                    json={"session_id": sid, "message": "x"}).status_code == 200
    assert api.delete(f"/api/sessions/{sid}", headers=_hdr(key_a)).status_code == 200
    assert api.get(f"/api/sessions/{sid}/history",
                   headers=_hdr(key_a)).status_code == 404
    assert api.delete(f"/api/sessions/{sid}",
                     headers=_hdr(key_a)).status_code == 404
    key_ad = _mk_user("root_g", role="admin")
    assert api.get(f"/api/sessions/{sid}/history",
                   headers=_hdr(key_ad)).status_code == 200


# ---------------------------------------------------------------- 语义补强（spec 3.2）


def test_admin_write_does_not_shift_owner(api):
    """admin 接管语义（报告建议 5）：admin 可跨用户写（治理职责），但 meta.owner
    不因 admin 消息混入漂移——本人不被踢出自己的会话（旧 owners 推断口径下
    admin 写入会让原 owner 后续续聊被拒，所有权漂移回归）。"""
    key_a = _mk_user("alice")
    key_ad = _mk_user("root_ly", role="admin")
    sid = "s-admin-join"
    assert api.post("/api/chat", headers=_hdr(key_a),
                    json={"session_id": sid, "message": "我的会话"}).status_code == 200
    r = api.post("/api/chat", headers=_hdr(key_ad),
                 json={"session_id": sid, "message": "管理员接入"})
    assert r.status_code == 200
    assert sessions.get_meta(sid)["owner_id"] == "alice"
    r = api.post("/api/chat", headers=_hdr(key_a),
                 json={"session_id": sid, "message": "继续我的会话"})
    assert r.status_code == 200


def test_new_session_id_server_generated_token_hex(api):
    """新会话 ID 服务端生成（spec 3.2.1）：secrets.token_hex(8)（16 位 hex，
    不可预测），替代 uuid4().hex[:8]（可预测且空间小）。"""
    key = _mk_user("nancy")
    r = api.post("/api/chat", headers=_hdr(key), json={"message": "新会话"})
    assert r.status_code == 200
    m = re.search(r'"session_id": "([0-9a-f]{16})"', r.text)
    assert m, f"服务端应生成 16 位 hex 会话 ID，实际：{r.text[:300]}"
