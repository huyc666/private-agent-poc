"""6.6 run 健康度（时长与状态）：明细 duration_ms/status 落账、回调计时配对、
失败标记、by_run/run_usage 健康度聚合、老库迁移幂等、回调红线。
用例编号对应 docs/specs/6.6-run-health.md 验收标准 AC1~AC6。

口径：by_run 总时长 = 各次模型调用时长之和（用户 2026-10-17 确认）；
run 墙钟时长（口径 B）单列为 PLAN 6.8，本套用例不涉及。
"""
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import config, usage


def _set_ctx(user="alice", session="s1", run="r1"):
    usage.current_user.set(user)
    usage.current_session.set(session)
    usage.current_run.set(run)


def _resp(model="m", in_tok=10, out_tok=5):
    """构造 on_llm_end 的 response 替身（仅回调实际读取的属性）。"""
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": in_tok, "output_tokens": out_tok,
                        "total_tokens": in_tok + out_tok},
        response_metadata={"model_name": model})
    return SimpleNamespace(generations=[[SimpleNamespace(message=msg)]])


def _row(run_id="r1"):
    rows = usage._query(
        "SELECT * FROM usage_records WHERE run_id = ? ORDER BY id", (run_id,))
    return rows


# ---- AC1 落账明细与向后兼容 ----

def test_ac1_error_row_recorded_without_tokens():
    _set_ctx(run="r1")
    usage.record("m", {}, source="main", duration_ms=1234, status="error")
    rows = _row()
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["duration_ms"] == 1234
    assert rows[0]["total_tokens"] == 0
    assert rows[0]["prompt_cost"] == 0.0


def test_ac1_default_params_backward_compatible():
    _set_ctx(run="r1")
    usage.record("m", {"input_tokens": 100, "output_tokens": 50})  # 既有调用形态
    rows = _row()
    assert rows[0]["status"] == "ok"
    assert rows[0]["duration_ms"] == 0


def test_ac1_empty_usage_ok_still_skipped():
    _set_ctx(run="r1")
    usage.record("m", {})  # ok + 空 usage 仍跳过（现状不变）
    assert _row() == []


# ---- AC2 回调计时配对 ----

def test_ac2_callback_timing_paired():
    _set_ctx(run="r1")
    cb = usage.UsageCallback().handler
    cb.on_llm_start(None, ["q"], run_id="x1", tags=[])
    time.sleep(0.02)
    cb.on_llm_end(_resp(), run_id="x1", tags=[])
    rows = _row()
    assert rows[0]["status"] == "ok"
    assert rows[0]["duration_ms"] >= 10
    assert rows[0]["total_tokens"] == 15


def test_ac2_on_llm_end_without_start_tolerated():
    _set_ctx(run="r1")
    cb = usage.UsageCallback().handler
    cb.on_llm_end(_resp(), run_id="x2")  # 无配对 start：duration 0、不报错
    rows = _row()
    assert rows[0]["status"] == "ok"
    assert rows[0]["duration_ms"] == 0


# ---- AC3 失败标记与并行隔离 ----

def test_ac3_on_llm_error_records_failure():
    _set_ctx(run="r-err")
    cb = usage.UsageCallback().handler
    cb.on_llm_start(None, ["q"], run_id="y1")
    time.sleep(0.02)
    cb.on_llm_error(RuntimeError("boom"), run_id="y1")
    rows = _row("r-err")
    assert rows[0]["status"] == "error"
    assert rows[0]["duration_ms"] >= 10
    assert rows[0]["total_tokens"] == 0
    assert rows[0]["model"] == config.MODEL_NAME  # 失败行模型名取当前配置
    agg = usage.run_usage("r-err")
    assert agg["failed_calls"] == 1
    assert agg["calls"] == 1


def test_ac3_parallel_run_ids_isolated(monkeypatch):
    """并行 worker 的 run_id 互不串扰：clock 序列 100.0/100.5/101.0/102.0 →
    x 时长 1000ms、y 时长 1500ms（确定值，不经 sleep）。"""
    _set_ctx(run="r-par")
    clock = iter([100.0, 100.5, 101.0, 102.0])
    monkeypatch.setattr(usage, "_mono", lambda: next(clock))
    cb = usage.UsageCallback().handler
    cb.on_llm_start(None, ["q"], run_id="wx", tags=[])          # 100.0
    cb.on_llm_start(None, ["q"], run_id="wy", tags=["ma_worker"])  # 100.5
    cb.on_llm_end(_resp(), run_id="wx", tags=[])                # 101.0 → 1000
    cb.on_llm_end(_resp(), run_id="wy", tags=["ma_worker"])     # 102.0 → 1500
    rows = {r["source"]: r for r in _row("r-par")}
    assert rows["main"]["duration_ms"] == 1000
    assert rows["worker"]["duration_ms"] == 1500
    assert rows["worker"]["source"] == "worker"


# ---- AC4 视图健康度聚合 ----

def test_ac4_by_run_and_run_usage_aggregates():
    _set_ctx(run="r4")
    usage.record("m", {"input_tokens": 100, "output_tokens": 50},
                 duration_ms=1000)
    usage.record("m", {"input_tokens": 100, "output_tokens": 50},
                 duration_ms=500)
    usage.record("m", {}, duration_ms=250, status="error")
    agg = usage.run_usage("r4")
    assert agg["calls"] == 3
    assert agg["duration_ms"] == 1750
    assert agg["failed_calls"] == 1
    assert agg["total_tokens"] == 300  # token 聚合不受影响
    by_run = usage.summary()["by_run"]
    assert by_run[0]["duration_ms"] == 1750
    assert by_run[0]["failed_calls"] == 1


def test_ac4_empty_run_zero_health():
    agg = usage.run_usage("no-such-run")
    assert agg["duration_ms"] == 0
    assert agg["failed_calls"] == 0


# ---- AC5 老库迁移幂等 ----

def test_ac5_legacy_db_migration_idempotent():
    # 造一个 v0.17.20 形态的老库（有金额列、无健康度列）并预置历史行
    db = Path(config.USAGE_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, date TEXT NOT NULL,
            user_id TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
            model TEXT NOT NULL, source TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            prompt_cost REAL NOT NULL DEFAULT 0,
            completion_cost REAL NOT NULL DEFAULT 0)""")
    conn.execute("INSERT INTO usage_records (ts, date, user_id, session_id,"
                 " run_id, model, source, prompt_tokens, completion_tokens,"
                 " total_tokens, prompt_cost, completion_cost)"
                 " VALUES (0, '2026-10-01', 'old', 'olds', 'oldr', 'oldm',"
                 " 'main', 100, 200, 300, 0.001, 0.002)")
    conn.commit()
    conn.close()

    # 新调用触发迁移；再跑一遍查询确认幂等
    _set_ctx(run="new-r")
    usage.record("m", {"input_tokens": 10, "output_tokens": 5})
    usage._query("SELECT COUNT(*) AS c FROM usage_records")

    cols = {r["name"] for r in usage._query(
        "PRAGMA table_info(usage_records)")}
    assert {"duration_ms", "status"} <= cols
    old = [r for r in usage._query("SELECT * FROM usage_records"
                                   " WHERE run_id='oldr'")][0]
    assert old["duration_ms"] == 0 and old["status"] == "ok"
    assert old["total_tokens"] == 300  # 历史 token 不丢
    assert old["prompt_cost"] + old["completion_cost"] == pytest.approx(0.003)


# ---- AC6 回调红线：异常吞掉不阻塞 ----

def test_ac6_handler_exceptions_swallowed():
    _set_ctx(run="r1")
    cb = usage.UsageCallback().handler
    # 非法 response / 非法 prompts / 无 start 的 error / 非法 run_id 均不得抛出
    assert cb.on_llm_end("garbage", run_id="z1") is None
    assert cb.on_llm_start(None, object(), run_id="z2") is None
    assert cb.on_llm_error(RuntimeError("boom"), run_id="z3") is None
    assert cb.on_llm_start(None, ["q"], run_id=None) is None
