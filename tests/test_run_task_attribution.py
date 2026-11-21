"""PLAN 6.7：run ↔ 业务对象归因（task_id）。"""
import asyncio
import os
import sqlite3
import uuid

import pytest

from server import config, db, orchestration as orch, state, usage


PG_URL = os.environ.get(
    "PG_URL", "postgresql://postgres:postgres@127.0.0.1:5432/agent_poc")


def _with_schema(url: str, schema_name: str) -> str:
    return f"{url}{('&' if '?' in url else '?')}options=-c%20search_path%3D{schema_name}"


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _task_var():
    return getattr(usage, "current_task", None)


def _set_task(task):
    var = _task_var()
    return var.set(task) if var is not None else None


def _reset_task(token):
    if token is not None:
        _task_var().reset(token)


def _record(run_id, task, *, user="alice", session="s1", model="m",
            tokens=1, status="ok"):
    usage.current_user.set(user)
    usage.current_session.set(session)
    usage.current_run.set(run_id)
    token = _set_task(task)
    try:
        usage.record(model, {
            "prompt_tokens": tokens,
            "completion_tokens": tokens,
            "total_tokens": tokens * 2,
        }, status=status)
    finally:
        _reset_task(token)


def _rows(sql, params=()):
    conn = sqlite3.connect(config.USAGE_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _workflow_rows():
    conn = sqlite3.connect(config.STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM workflow_instances").fetchall()]
    finally:
        conn.close()


def _old_usage_db():
    conn = sqlite3.connect(config.USAGE_DB)
    conn.executescript("""
        CREATE TABLE usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            date TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            model TEXT NOT NULL,
            source TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            prompt_cost REAL NOT NULL DEFAULT 0,
            completion_cost REAL NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok'
        );
        INSERT INTO usage_records VALUES
            (1, 1770000000.0, '2026-02-02', 'alice', 's1', 'r1',
             'm', 'main', 1, 2, 3, 0.1, 0.2, 10, 'ok');
    """)
    conn.commit()
    conn.close()


def _install_definition(monkeypatch, *, template="处理 {{topic}}"):
    monkeypatch.setattr(orch, "resolve_definition",
                        lambda name, identity: {"status": "ok", "path": "x"})
    monkeypatch.setattr(orch, "load_definition",
                        lambda path: {"status": "ok",
                                      "definition": {
                                          "name": "demo", "version": "1",
                                          "task_template": template,
                                          "params": []},
                                      "hash": "h"})
    monkeypatch.setattr(orch.STATE, "llm", object())


def _result(ok=True):
    return {"ok": ok, "aborted": False, "failed_step": "", "error": "",
            "outputs": {"o": "x"}, "findings": [], "subtasks": [],
            "summary": "ok", "name": "demo", "version": "1"}


def test_ac1_usage_schema_adds_task_id_and_index(monkeypatch):
    _old_usage_db()
    monkeypatch.setattr(usage, "_PRICES", {})
    usage.record("m", {"total_tokens": 5})
    cols = {r[1] for r in sqlite3.connect(config.USAGE_DB).execute(
        "PRAGMA table_info(usage_records)").fetchall()}
    indexes = {r[1] for r in sqlite3.connect(config.USAGE_DB).execute(
        "PRAGMA index_list(usage_records)").fetchall()}
    assert "task_id" in cols
    assert "idx_usage_task" in indexes
    rows = _rows("SELECT id, task_id FROM usage_records ORDER BY id")
    assert rows[0]["task_id"] == ""
    assert rows[1]["task_id"] == ""


def test_ac2_record_reads_current_task_context(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", {})
    _record("r1", "task-A")
    rows = _rows("SELECT task_id FROM usage_records")
    assert rows == [{"task_id": "task-A"}]


def test_ac3_manual_orchestration_sets_task_id_for_all_calls(monkeypatch):
    _install_definition(monkeypatch)
    captured = []

    async def fake_execute(*args, **kwargs):
        for run_id in ("run-1", "run-2"):
            usage.current_run.set(run_id)
            captured.append(usage.current_task.get())
            usage.record("m", {"total_tokens": 3})
        return _result()

    monkeypatch.setattr(orch, "execute", fake_execute)
    usage.current_identity.set(_identity())
    token = usage.current_task.set("outside")
    try:
        asyncio.run(orch.run_orchestration("demo", task="做事"))
        assert usage.current_task.get() == "outside"
    finally:
        usage.current_task.reset(token)
    assert len(captured) == 2
    assert captured[0] == captured[1] != ""
    instance_id = _workflow_rows()[0]["instance_id"]
    assert all(row["task_id"] == instance_id for row in _rows(
        "SELECT task_id FROM usage_records"))


def test_ac4_triggered_orchestration_sets_task_id(monkeypatch):
    _install_definition(monkeypatch)
    monkeypatch.setattr(orch, "render_task_template",
                        lambda template, values: "自动做事")
    captured = []

    async def fake_execute(*args, **kwargs):
        captured.append(usage.current_task.get())
        usage.record("m", {"total_tokens": 4})
        return _result()

    monkeypatch.setattr(orch, "execute", fake_execute)
    token = usage.current_task.set("outside")
    try:
        asyncio.run(orch.run_triggered(
            "demo", {}, _identity(), trigger_source="scheduled"))
        assert usage.current_task.get() == "outside"
    finally:
        usage.current_task.reset(token)
    instance_id = _workflow_rows()[0]["instance_id"]
    assert captured == [instance_id]
    assert _rows("SELECT task_id FROM usage_records") == [
        {"task_id": instance_id}]


def test_ac4_failed_execution_preserves_task_id(monkeypatch):
    _install_definition(monkeypatch)
    monkeypatch.setattr(orch, "render_task_template",
                        lambda template, values: "自动做事")

    async def fake_execute(*args, **kwargs):
        usage.record("m", {"total_tokens": 4}, status="error")
        return _result(ok=False)

    monkeypatch.setattr(orch, "execute", fake_execute)
    asyncio.run(orch.run_triggered(
        "demo", {}, _identity(), trigger_source="scheduled"))
    instance_id = _workflow_rows()[0]["instance_id"]
    assert _rows("SELECT task_id, status FROM usage_records") == [
        {"task_id": instance_id, "status": "error"}]


def test_ac3_execution_exception_restores_task_context(monkeypatch):
    _install_definition(monkeypatch)

    async def fake_execute(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(orch, "execute", fake_execute)
    usage.current_identity.set(_identity())
    token = usage.current_task.set("outside")
    try:
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(orch.run_orchestration("demo", task="做事"))
        assert usage.current_task.get() == "outside"
    finally:
        usage.current_task.reset(token)


def test_ac5_by_task_aggregates_multiple_runs_and_costs(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES",
                        {"m": {"input": 1.0, "output": 2.0}})
    assert usage._PRICES["m"]["input"] == 1.0
    _record("r1", "task-A", tokens=2)
    _record("r2", "task-A", tokens=3)
    by_task = usage.summary()["by_task"]
    assert len(by_task) == 1
    row = by_task[0]
    assert row["task_id"] == "task-A"
    assert row["runs"] == 2
    assert row["calls"] == 2
    assert row["total_tokens"] == 10
    assert row["prompt_tokens"] == 5
    assert row["completion_tokens"] == 5
    assert row["duration_ms"] == 0
    assert row["failed_calls"] == 0
    assert row["prompt_cost"] + row["completion_cost"] > 0
    assert row["total_cost"] == round(
        row["prompt_cost"] + row["completion_cost"], 6)


def test_ac6_empty_task_id_and_acl_filters_unchanged(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", {})
    _record("r1", "", user="alice", tokens=1)
    _record("r2", "task-A", user="alice", tokens=2)
    _record("r3", "task-B", user="bob", tokens=3)
    agg = usage.summary(user_id="alice")
    assert agg["total"]["calls"] == 2
    assert [r["task_id"] for r in agg["by_task"]] == ["task-A"]
    assert agg["by_user"][0]["calls"] == 2


def test_ac7_instance_persistence_failure_does_not_block_or_misattribute(monkeypatch):
    _install_definition(monkeypatch)
    monkeypatch.setattr(orch, "create_instance_row",
                        lambda *args, **kwargs: None)
    captured = []

    async def fake_execute(*args, **kwargs):
        captured.append(usage.current_task.get())
        usage.record("m", {"total_tokens": 1})
        return _result()

    monkeypatch.setattr(orch, "execute", fake_execute)
    usage.current_identity.set(_identity())
    token = usage.current_task.set("outside")
    try:
        out = asyncio.run(orch.run_orchestration("demo", task="做事"))
    finally:
        usage.current_task.reset(token)
    assert "多角色编排完成" in out
    assert captured == [""]
    assert _rows("SELECT task_id FROM usage_records") == [{"task_id": ""}]


def test_ac8_postgres_schema_migration_and_pg_legal_grouping():
    assert "task_id TEXT NOT NULL DEFAULT ''" in db.PG_DDL
    assert "ALTER TABLE usage_records ADD COLUMN IF NOT EXISTS task_id" in db.PG_DDL
    assert "idx_usage_task" in db.PG_DDL


@pytest.mark.skipif(not os.environ.get("RUN_PG_TESTS"),
                    reason="RUN_PG_TESTS=1 required")
def test_ac8_postgres_schema_upgrade_and_grouping_contract(monkeypatch):
    import psycopg
    schema = f"pa_poc_task_{uuid.uuid4().hex[:10]}"
    conn = psycopg.connect(PG_URL)
    try:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute("""
            CREATE TABLE usage_records (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                ts DOUBLE PRECISION NOT NULL,
                date TEXT NOT NULL,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                model TEXT NOT NULL,
                source TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                prompt_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                completion_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ok'
            )
        """)
        db.ensure_postgres_schema(conn)
        conn.commit()
        col = conn.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema=%s AND table_name='usage_records'"
            " AND column_name='task_id'", (schema,)).fetchone()
        idx = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname=%s"
            " AND indexname='idx_usage_task'", (schema,)).fetchone()
        assert col and idx
        conn.execute("""
            INSERT INTO usage_records (ts, date, user_id, session_id, run_id,
                model, source, prompt_tokens, completion_tokens, total_tokens,
                prompt_cost, completion_cost, duration_ms, status, task_id)
            VALUES (1, '2026-01-01', 'alice', 's1', 'r1', 'm', 'main',
                    1, 2, 3, 0.1, 0.2, 10, 'ok', 'task-A'),
                   (2, '2026-01-01', 'alice', 's1', 'r2', 'm', 'main',
                    1, 2, 3, 0.1, 0.2, 10, 'ok', 'task-A')
        """)
        conn.commit()
        monkeypatch.setattr(config, "USAGE_BACKEND", "postgres")
        monkeypatch.setattr(config, "USAGE_DATABASE_URL",
                            _with_schema(PG_URL, schema))
        try:
            agg = usage.summary(session_id="s1")
        finally:
            db.close_pools()
        by_task = agg["by_task"]
        assert len(by_task) == 1
        assert by_task[0]["task_id"] == "task-A"
        assert by_task[0]["runs"] == 2
        assert by_task[0]["calls"] == 2
        assert by_task[0]["user_id"] == "alice"
        assert by_task[0]["session_id"] == "s1"
        assert usage.summary(session_id="other")["by_task"] == []
        assert usage.summary(session_id="s1", days=1)["by_task"] == []
    finally:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")
        conn.commit()
        conn.close()
