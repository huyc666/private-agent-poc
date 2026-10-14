"""usage：token 三维度计量（用户/任务/会话）与聚合视图。"""
from server import usage


def _set_ctx(user="alice", session="s1", run="r1"):
    usage.current_user.set(user)
    usage.current_session.set(session)
    usage.current_run.set(run)


def test_record_persists_usage_row():
    _set_ctx()
    usage.record("test-model", {"input_tokens": 100, "output_tokens": 20})
    agg = usage.run_usage("r1")
    assert agg["calls"] == 1
    assert agg["prompt_tokens"] == 100
    assert agg["total_tokens"] == 120


def test_record_skips_zero_usage():
    _set_ctx()
    usage.record("m", {})
    usage.record("m", {"input_tokens": 0, "output_tokens": 0})
    assert usage.run_usage("r1")["calls"] == 0


def test_record_accepts_provider_field_aliases():
    _set_ctx()
    usage.record("m", {"prompt_tokens": 5, "completion_tokens": 5})
    assert usage.run_usage("r1")["total_tokens"] == 10


def test_run_usage_and_session_usage_split():
    _set_ctx(session="sess-a", run="run-1")
    usage.record("m", {"total_tokens": 30})
    _set_ctx(session="sess-a", run="run-2")
    usage.record("m", {"total_tokens": 10})
    assert usage.run_usage("run-1")["total_tokens"] == 30
    assert usage.session_usage("sess-a")["total_tokens"] == 40


def test_summary_groups_by_user(monkeypatch):
    _set_ctx(user="alice", session="s1", run="r1")
    usage.record("m", {"total_tokens": 50})
    _set_ctx(user="bob", session="s2", run="r2")
    usage.record("m", {"total_tokens": 10})
    agg = usage.summary()
    assert agg["total"]["calls"] == 2
    assert agg["by_user"][0]["user_id"] == "alice"   # 按消耗倒序
    # 资源级 ACL：按用户过滤
    assert usage.summary(user_id="bob")["total"]["calls"] == 1


def test_summary_days_window_excludes_old_records():
    import sqlite3
    import time
    from server import config
    _set_ctx()
    usage.record("m", {"total_tokens": 99})
    conn = sqlite3.connect(config.USAGE_DB)
    conn.execute("UPDATE usage_records SET ts = ?", (time.time() - 10 * 86400,))
    conn.commit()
    conn.close()
    assert usage.summary(days=1)["total"]["calls"] == 0


def test_summary_marks_worker_source():
    _set_ctx()
    # source 维度由 UsageCallback 的 tags 决定；直接验证 summary 的 by_run 字段可用
    usage.record("m", {"total_tokens": 5}, source="worker")
    agg = usage.summary()
    assert agg["by_run"][0]["worker_tokens"] == 5
