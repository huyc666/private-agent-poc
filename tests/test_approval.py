"""approval：审批生命周期 / 决议权限分离 / fail-closed / 执行连续性追踪。"""
import asyncio
import sqlite3
import time

import pytest

from server import approval, config


def _create(tool="delete_workspace_file", args=None):
    return approval.create_approval(tool, args or {"path": "x.md"})


def _set_status(aid, status, decided=None):
    conn = sqlite3.connect(config.STATE_DB)
    conn.execute("UPDATE approvals SET status = ?, decided = ? WHERE aid = ?",
                 (status, decided, aid))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 创建与决议权限

def test_create_approval_records_pending():
    aid = _create()
    assert len(aid) == 12
    recs = approval.list_recent()
    assert recs[0]["aid"] == aid and recs[0]["status"] == "pending"
    assert approval.pending_count() == 1


def test_resolve_user_role_forbidden():
    """v0.17.9 决议权分离：user 角色不可决议任何审批（含自己发起的）。"""
    aid = _create()
    assert approval.resolve(aid, True, role="user") == "forbidden"
    assert approval.pending_count() == 1       # 记录未被改动


def test_resolve_only_approver_or_admin():
    aid = _create()
    assert approval.resolve(aid, True, role="approver", username="ap1") == "ok"
    assert approval.resolve(_create(), False, role="admin", username="ad1") == "ok"


def test_resolve_double_decision_is_safe():
    """仅 pending → 决议算成功：并发重复提交 / 已决议记录再提交 → not_found。"""
    aid = _create()
    assert approval.resolve(aid, True, role="admin") == "ok"
    assert approval.resolve(aid, False, role="admin") == "not_found"


def test_resolve_unknown_approval():
    assert approval.resolve("nonexistent", True, role="admin") == "not_found"


def test_list_recent_acl_filters_creator():
    _create("t1")
    approval.create_approval("t2", {})      # 同 anonymous 上下文
    rows = approval.list_recent(user_id="alice")
    assert rows == []


# ---------------------------------------------------------------- 等待与超时

def test_wait_decision_returns_true_on_approve():
    aid = _create()
    approval.resolve(aid, True, role="admin")
    assert asyncio.run(approval.wait_decision(aid)) is True


def test_wait_decision_returns_false_on_reject():
    aid = _create()
    approval.resolve(aid, False, role="admin")
    assert asyncio.run(approval.wait_decision(aid)) is False


def test_wait_decision_timeout_marks_expired():
    aid = _create()
    assert asyncio.run(approval.wait_decision(aid, timeout=0.2)) is None
    rec = [r for r in approval.list_recent() if r["aid"] == aid][0]
    assert rec["status"] == "expired"        # fail-closed：超时语义等价拒绝


def test_wait_decision_lost_record_returns_none():
    assert asyncio.run(approval.wait_decision("ghost-aid", timeout=0.1)) is None


def test_wait_decision_polls_concurrent_decision():
    """等待期间由另一处（模拟另一进程）写入决议 → 等待方感知批准。"""
    aid = _create()

    async def run():
        async def decide_later():
            await asyncio.sleep(0.1)
            approval.resolve(aid, True, role="admin")
        task = asyncio.create_task(decide_later())
        result = await approval.wait_decision(aid)
        await task
        return result

    assert asyncio.run(run()) is True


# ---------------------------------------------------------------- 执行连续性

def test_mark_executed_idempotent():
    aid = _create()
    approval.resolve(aid, True, role="admin")
    approval.mark_executed(aid)
    approval.mark_executed(aid)              # 幂等：重复标记无副作用
    rec = [r for r in approval.list_recent() if r["aid"] == aid][0]
    assert rec["executed_at"]


def test_mark_abandoned_respects_grace():
    aid = _create()
    approval.resolve(aid, True, role="admin")
    # 决议时间 1s 前：宽限 300s → 不判丢失（正在执行的长任务不误伤）
    assert approval.mark_abandoned(grace=300) == 0
    # 决议时间 1000s 前：超出宽限 → abandoned（批准了但没人执行）
    conn = sqlite3.connect(config.STATE_DB)
    conn.execute("UPDATE approvals SET decided = ? WHERE aid = ?",
                 (time.time() - 1000, aid))
    conn.commit()
    conn.close()
    assert approval.mark_abandoned(grace=300) == 1
    rec = [r for r in approval.list_recent() if r["aid"] == aid][0]
    assert rec["status"] == "abandoned"


def test_cleanup_stale_expires_pending_and_abandons_approved():
    a1, a2 = _create(), _create()
    approval.resolve(a2, True, role="admin")
    # a2 决议时间推到宽限之外
    conn = sqlite3.connect(config.STATE_DB)
    conn.execute("UPDATE approvals SET decided = ? WHERE aid = ?",
                 (time.time() - 1000, a2))
    conn.commit()
    conn.close()
    n = approval.cleanup_stale(grace=300)
    assert n == 2
    statuses = {r["aid"]: r["status"] for r in approval.list_recent()}
    assert statuses[a1] == "expired"
    assert statuses[a2] == "abandoned"


def test_cleanup_stale_keeps_fresh_approved():
    aid = _create()
    approval.resolve(aid, True, role="admin")
    assert approval.cleanup_stale(grace=300) == 0   # 刚批准的留着给 resume 执行


# ---------------------------------------------------------------- 审批门

class _Fake:
    def __init__(self, value=None, raises=None):
        self.value, self.raises = value, raises
        self.calls = 0

    def __call__(self, *_a, **_k):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.value


@pytest.fixture
def patch_interrupt(monkeypatch):
    def _patch(fake):
        import langgraph.types
        monkeypatch.setattr(langgraph.types, "interrupt", fake)
        return fake
    return _patch


def test_require_approval_executes_on_approve(patch_interrupt):
    patch_interrupt(_Fake({"approve": True}))
    out = approval.require_approval("t", {}, "删除", lambda: "done!")
    assert out == "done!"


def test_require_approval_reject_denies_execution(patch_interrupt):
    patch_interrupt(_Fake({"approve": False}))
    out = approval.require_approval("t", {}, "删除", lambda: "done!")
    assert "拒绝" in out and "未执行" in out


def test_require_approval_malformed_decision_fail_closed(patch_interrupt):
    """决定格式异常 → fail-closed 不执行。"""
    patch_interrupt(_Fake("garbage-string"))
    assert "未执行" in approval.require_approval("t", {}, "删除", lambda: "boom")


def test_require_approval_bubbleup_passthrough(patch_interrupt):
    """interrupt 的图中断信号必须放行，不能吞掉（否则审批流断死）。"""
    from langgraph.errors import GraphBubbleUp
    patch_interrupt(_Fake(raises=GraphBubbleUp()))
    with pytest.raises(GraphBubbleUp):
        approval.require_approval("t", {}, "删除", lambda: "boom")


def test_require_approval_unavailable_fail_closed(patch_interrupt):
    patch_interrupt(_Fake(raises=RuntimeError("graph 不可用")))
    out = approval.require_approval("t", {}, "删除", lambda: "boom")
    assert "fail-closed" in out


def test_delete_workspace_file_gated_by_approval(patch_interrupt, login_as):
    """危险工具 delete_workspace_file 必须先过审批门才执行删除。"""
    from server import tools_builtin
    login_as("alice")
    (config.TOOL_WORKSPACE / "victim.md").write_text("data")
    executed = []
    patch_interrupt(_Fake({"approve": True}))
    # 审批门直接验证：批准 → 执行 do_delete
    out = approval.require_approval(
        "delete_workspace_file", {"path": "victim.md"}, "删除",
        lambda: executed.append(1) or tools_builtin.do_delete("victim.md"))
    assert "已删除" in out and executed == [1]
