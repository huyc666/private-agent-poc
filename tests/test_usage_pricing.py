"""6.5 模型单价折算（成本金额化）：落账折算、视图金额字段、缺单价与非法配置路径、
老库迁移幂等。用例编号对应 docs/specs/6.5-model-pricing.md 验收标准 AC1~AC7。

单价单位：元/百万 tokens（用户 2026-10-17 确认）；历史口径：落账时单价固化。
"""
import logging
import sqlite3

import pytest

from server import config, usage


def _set_ctx(user="alice", session="s1", run="r1"):
    usage.current_user.set(user)
    usage.current_session.set(session)
    usage.current_run.set(run)


def _prices(**models):
    return models


# ---- AC1 落账折算 ----

def test_ac1_record_costs_calculated(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", _prices(
        **{"model-a": {"input": 2.0, "output": 8.0}}))
    _set_ctx()
    usage.record("model-a", {"input_tokens": 1000, "output_tokens": 2000})
    agg = usage.run_usage("r1")
    assert agg["prompt_cost"] == pytest.approx(0.002)      # 1000/1e6 * 2.0
    assert agg["completion_cost"] == pytest.approx(0.016)  # 2000/1e6 * 8.0
    assert agg["total_cost"] == pytest.approx(0.018)


# ---- AC2 各视图输出金额字段 ----

def test_ac2_all_views_expose_cost_fields(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", _prices(
        **{"m": {"input": 1.0, "output": 1.0}}))
    _set_ctx()
    usage.record("m", {"input_tokens": 1_000_000, "output_tokens": 500_000})
    run_agg = usage.run_usage("r1")
    sess_agg = usage.session_usage("s1")
    summary = usage.summary()
    for agg in (run_agg, sess_agg, summary["total"],
                summary["by_user"][0], summary["by_session"][0],
                summary["by_run"][0]):
        assert {"prompt_cost", "completion_cost", "total_cost"} <= set(agg)
    assert run_agg["prompt_cost"] == pytest.approx(1.0)
    assert run_agg["completion_cost"] == pytest.approx(0.5)
    assert sess_agg["total_cost"] == pytest.approx(1.5)
    assert summary["total"]["total_cost"] == pytest.approx(1.5)


# ---- AC3 缺单价模型不折算不报错 ----

def test_ac3_unpriced_model_token_only(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", _prices(
        **{"model-a": {"input": 2.0, "output": 2.0}}))
    _set_ctx(run="run-a")
    usage.record("model-a", {"input_tokens": 1_000_000, "output_tokens": 0})
    _set_ctx(run="run-b")
    usage.record("model-b", {"input_tokens": 500_000, "output_tokens": 0})
    # model-b：token 照记，金额为 0，无报错
    run_b = usage.run_usage("run-b")
    assert run_b["calls"] == 1
    assert run_b["total_tokens"] == 500_000
    assert run_b["total_cost"] == pytest.approx(0.0)
    # total_cost 只含已折算部分
    summary = usage.summary()
    assert summary["total"]["total_cost"] == pytest.approx(2.0)


# ---- AC4 未配置 MODEL_PRICES 向后兼容 ----

def test_ac4_no_prices_backward_compatible(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", {})
    _set_ctx()
    usage.record("m", {"input_tokens": 100, "output_tokens": 20})
    agg = usage.run_usage("r1")
    assert agg["calls"] == 1
    assert agg["prompt_tokens"] == 100
    assert agg["total_tokens"] == 120
    assert agg["prompt_cost"] == pytest.approx(0.0)
    assert agg["completion_cost"] == pytest.approx(0.0)
    assert agg["total_cost"] == pytest.approx(0.0)


# ---- AC5 非法单价配置拒绝路径 ----

def test_ac5_invalid_json_config_disables_costing(caplog, monkeypatch):
    with caplog.at_level(logging.WARNING):
        prices = usage.parse_model_prices("not-json{")
    assert prices == {}
    assert any("MODEL_PRICES" in r.message for r in caplog.records)


def test_ac5_negative_price_rejected(caplog, monkeypatch):
    with caplog.at_level(logging.WARNING):
        prices = usage.parse_model_prices('{"m": {"input": -1, "output": 2}}')
    assert prices == {}
    assert any("MODEL_PRICES" in r.message for r in caplog.records)


def test_ac5_missing_field_rejected(caplog, monkeypatch):
    with caplog.at_level(logging.WARNING):
        prices = usage.parse_model_prices('{"m": {"input": 1}}')
    assert prices == {}
    assert any("MODEL_PRICES" in r.message for r in caplog.records)


def test_ac5_partial_invalid_keeps_valid_entries(caplog, monkeypatch):
    raw = ('{"good": {"input": 1.0, "output": 2.0},'
           ' "bad": {"input": -5, "output": 2}}')
    with caplog.at_level(logging.WARNING):
        prices = usage.parse_model_prices(raw)
    assert set(prices) == {"good"}
    assert prices["good"] == {"input": 1.0, "output": 2.0}
    assert any("bad" in r.message for r in caplog.records)
    # 非法条目对应的模型不折算：落账 cost=0、token 照记
    monkeypatch.setattr(usage, "_PRICES", prices)
    _set_ctx()
    usage.record("bad", {"input_tokens": 1000, "output_tokens": 1000})
    assert usage.run_usage("r1")["total_cost"] == pytest.approx(0.0)
    assert usage.run_usage("r1")["total_tokens"] == 2000


def test_ac5_empty_config_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert usage.parse_model_prices("") == {}
    assert not caplog.records


def test_ac5_valid_config_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        prices = usage.parse_model_prices(
            '{"m": {"input": 0.5, "output": 2.0}}')
    assert prices == {"m": {"input": 0.5, "output": 2.0}}
    assert not caplog.records


# ---- AC6 老库迁移幂等 ----

def _create_legacy_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, date TEXT NOT NULL, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, run_id TEXT NOT NULL, model TEXT NOT NULL,
            source TEXT NOT NULL, prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL)""")
    conn.execute(
        "INSERT INTO usage_records (ts, date, user_id, session_id, run_id,"
        " model, source, prompt_tokens, completion_tokens, total_tokens)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (123.0, "2026-01-01", "alice", "s-old", "run-old", "m", "main", 10, 5, 15))
    conn.commit()
    conn.close()


def test_ac6_legacy_db_migration_adds_cost_columns():
    db = config.USAGE_DB
    _create_legacy_db(db)
    conn = sqlite3.connect(str(db))
    usage._ensure_schema(conn)   # 迁移：补金额列
    usage._ensure_schema(conn)   # 幂等：重复执行不报错
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_records)")}
    assert {"prompt_cost", "completion_cost"} <= cols
    conn.close()
    # 历史行：token 不丢，金额按 NULL→0 计入
    agg = usage.run_usage("run-old")
    assert agg["total_tokens"] == 15
    assert agg["total_cost"] == pytest.approx(0.0)


def test_ac6_fresh_schema_includes_cost_columns():
    _set_ctx()
    usage.record("m", {"input_tokens": 1, "output_tokens": 1})
    conn = sqlite3.connect(str(config.USAGE_DB))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_records)")}
    conn.close()
    assert {"prompt_cost", "completion_cost"} <= cols


# ---- AC7 单价为 0（自建 vLLM 内部核算）----

def test_ac7_zero_price_configured(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", _prices(
        **{"vllm-self": {"input": 0.0, "output": 0.0}}))
    _set_ctx()
    usage.record("vllm-self", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    agg = usage.run_usage("r1")
    assert agg["calls"] == 1
    assert agg["total_tokens"] == 2_000_000
    assert agg["prompt_cost"] == pytest.approx(0.0)
    assert agg["completion_cost"] == pytest.approx(0.0)
    assert agg["total_cost"] == pytest.approx(0.0)
