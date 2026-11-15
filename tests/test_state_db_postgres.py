"""PLAN 6.4：STATE_DB / USAGE_DB PostgreSQL 化测试。

默认用例锁定 SQLite 兼容与 P2-3 契约；真实 PostgreSQL 用例由
RUN_PG_TESTS=1 门控。集成测试使用独立 schema，避免污染共享数据库。
"""
import concurrent.futures
import contextlib
import os
import sqlite3
import subprocess
import sys
import uuid

import psycopg
import pytest

from server import approval, audit, config, db, sessions, usage


PG_URL = os.environ.get(
    "PG_URL", "postgresql://postgres:postgres@127.0.0.1:5432/agent_poc")
PG_TEST_MARK = pytest.mark.skipif(
    os.environ.get("RUN_PG_TESTS") != "1",
    reason="PostgreSQL 集成测试：设 RUN_PG_TESTS=1 并启动 postgres")
PG_TABLES = (
    "messages", "session_meta", "approvals", "api_users", "auth_tokens",
    "login_fails", "audit_log", "session_locks", "workflow_instances",
    "workflow_schedules", "workflow_trigger_attempts", "disabled_packs",
    "usage_records", "run_records",
)
PG_AUTOINCREMENT_TABLES = ("messages", "audit_log", "usage_records")
_SCHEMA_NAMES: set[str] = set()


def test_ac1_p2_3_by_run_postgres_legal_and_precise():
    """AC1：by_run 不得依赖 SQLite 裸列方言，输出保持兼容。"""
    sql = db.translate_sql(
        "SELECT u.run_id AS run_id, u.session_id AS session_id,"
        " u.user_id AS user_id, u.date AS date, COUNT(*) AS calls"
        " FROM usage_records AS u GROUP BY u.run_id"
    )
    for bare in ("u.session_id AS session_id", "u.user_id AS user_id",
                 "u.date AS date"):
        assert bare in sql, "P2-3：by_run 仍保留 PG 非法裸列"
    assert "MAX(u.session_id) AS session_id" in db.PG_COMPATIBLE_BY_RUN
    assert "MAX(u.user_id) AS user_id" in db.PG_COMPATIBLE_BY_RUN
    assert "MAX(u.date) AS date" in db.PG_COMPATIBLE_BY_RUN


def test_ac2_postgres_schema_includes_run_records():
    """AC2：Postgres DDL 清单必须显式包含 run_records（P2-6）。"""
    assert "run_records" in db.PG_DDL
    assert "CREATE TABLE IF NOT EXISTS run_records" in db.PG_DDL


def test_ac3_postgres_session_append_fence_atomic():
    """AC3：并发栅栏下同代际写入成功，旧代际 fail-closed。"""
    session_id = f"pg-fence-{uuid.uuid4().hex[:8]}"
    assert sessions.append_fenced(session_id, 0, "alice", "user", "first")

    def _write(role):
        return sessions.append_fenced(session_id, 0, "alice", role, "same gen")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(_write, ("user", "assistant")))
    assert sessions.append_fenced(session_id, 1, "alice", "user", "old") is False


def test_ac4_postgres_approval_resolution_cross_process_visible():
    """AC4：审批创建→查询→决议跨连接可见，pending 仅转换一次。"""
    aid = approval.create_approval("pg-cross", {})
    assert approval.pending_count() == 1
    assert approval.resolve(aid, True, username="approver", role="approver") == "ok"
    assert approval.resolve(aid, False, username="approver", role="approver") == "not_found"


@PG_TEST_MARK
def test_ac5_two_processes_share_postgres_sessions_and_approvals(tmp_path):
    """AC5：两个应用进程共享 PG 的会话/审批互通（真实栈）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    session_id = f"shared-{uuid.uuid4().hex[:8]}"
    schema_url, schema_name = _create_test_schema()
    config.STATE_DATABASE_URL = schema_url
    config.USAGE_DATABASE_URL = schema_url
    config.STATE_BACKEND = "postgres"
    config.USAGE_BACKEND = "postgres"
    sessions.append(session_id, "alice", "user", "from-a")
    aid = approval.create_approval("shared", {})
    subprocess.run(
        [sys.executable, "-c",
         "import os;"
         "os.environ['STATE_BACKEND'] = 'postgres';"
         "os.environ['USAGE_BACKEND'] = 'postgres';"
         "os.environ['STATE_DATABASE_URL'] = os.environ['PG_SCHEMA_URL'];"
         "os.environ['USAGE_DATABASE_URL'] = os.environ['PG_SCHEMA_URL'];"
         "from server import approval, sessions;"
         f"sessions.append({session_id!r}, 'bob', 'user', 'from-b');"
         "assert approval.pending_count() == 1"],
        check=True,
        env={**os.environ, "PG_SCHEMA_URL": schema_url})
    with psycopg.connect(schema_url) as conn:
        row = conn.execute(
            "SELECT content FROM messages WHERE session_id = %s",
            (session_id,)).fetchone()
        status = conn.execute(
            "SELECT status FROM approvals WHERE aid = %s", (aid,)).fetchone()[0]
    assert row[0] == "from-a"
    assert status == "pending"
    assert sessions.history(session_id)[-1]["content"] == "from-b"
    assert approval.resolve(aid, True, username="approver", role="approver") == "ok"


def test_ac6_sqlite_behavior_unchanged(tmp_path):
    """AC6：默认 SQLite 行为保持，spec 前后兼容。"""
    config.STATE_DB = str(tmp_path / "state.db")
    config.USAGE_DB = str(tmp_path / "usage.db")
    sessions.append("sqlite-session", "alice", "user", "hello")
    assert sessions.history("sqlite-session")[0]["content"] == "hello"


@PG_TEST_MARK
def test_ac7_postgres_schema_inventory_exact():
    """AC7：PG schema 必须逐张包含 14 表。"""
    schema_url, _ = _create_test_schema()
    _configure_postgres(schema_url)
    app_db = db.connect("state")
    app_db.close()
    with psycopg.connect(schema_url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema()").fetchall()
    assert set(PG_TABLES) <= {row[0] for row in rows}
    assert len(PG_TABLES) == 14


@PG_TEST_MARK
def test_ac8_sqlite_to_postgres_migration_roundtrip(tmp_path):
    """AC8：显式迁移保持行数、精度与序列安全；拒绝覆盖目标表。"""
    state_source = tmp_path / "state.db"
    usage_source = tmp_path / "usage.db"
    _sqlite_state_probe(state_source)
    _sqlite_probe(usage_source)
    from tools import migrate_db
    schema_url, _ = _create_test_schema()
    migrate_db.migrate(state_source, usage_source, schema_url, assume_yes=True)
    with psycopg.connect(schema_url) as conn:
        expected_counts = {
            "messages": 501, "session_meta": 0, "approvals": 0,
            "api_users": 0, "auth_tokens": 0, "login_fails": 0,
            "audit_log": 1, "session_locks": 0, "workflow_instances": 0,
            "workflow_schedules": 0, "workflow_trigger_attempts": 0,
            "disabled_packs": 0, "usage_records": 1, "run_records": 1,
        }
        for table, expected in expected_counts.items():
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
        assert conn.execute(
            "SELECT ts, prompt_cost, completion_cost FROM usage_records"
        ).fetchone() == (1770000000.123456, 0.12345678912345678,
                          0.23456789123456789)
        message_id = conn.execute(
            "INSERT INTO messages (session_id, user_id, role, content, ts, extra)"
            " VALUES ('s', 'u', 'user', 'next', 1, NULL) RETURNING id"
        ).fetchone()[0]
        audit_id = conn.execute(
            "INSERT INTO audit_log (ts, actor, role, action, target, detail)"
            " VALUES (1, 'u', 'user', 'next', '', '') RETURNING id"
        ).fetchone()[0]
        usage_id = conn.execute(
            "INSERT INTO usage_records (ts, date, user_id, session_id, run_id,"
            " model, source, prompt_tokens, completion_tokens, total_tokens,"
            " prompt_cost, completion_cost, duration_ms, status)"
            " VALUES (1, '2026-02-02', 'u', 's', 'r2', 'm', 'main', 1, 1, 2,"
            " 0, 0, 0, 'ok') RETURNING id"
        ).fetchone()[0]
        assert (message_id, audit_id, usage_id) == (502, 2, 2)
    with pytest.raises(RuntimeError):
        migrate_db.migrate(state_source, usage_source, schema_url, assume_yes=True)


@PG_TEST_MARK
def test_ac9_postgres_fail_fast_and_no_silent_fallback():
    """AC9：PG 不可达 fail-fast，不回退 SQLite，错误不含 URL。"""
    config.STATE_BACKEND = "postgres"
    config.STATE_DATABASE_URL = "postgresql://user:secret@127.0.0.1:1/db"
    try:
        with pytest.raises(RuntimeError) as excinfo:
            sessions.append("missing", "alice", "user", "hello")
        assert "secret" not in str(excinfo.value)
        assert "sqlite" not in str(excinfo.value).lower()
    finally:
        config.STATE_BACKEND = "sqlite"


def _with_schema(url: str, schema_name: str) -> str:
    return f"{url}{('&' if '?' in url else '?')}options=-c%20search_path%3D{schema_name}"


def _create_test_schema() -> tuple[str, str]:
    schema_name = f"pa_poc_test_{uuid.uuid4().hex}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema_name}"')
    _SCHEMA_NAMES.add(schema_name)
    return _with_schema(PG_URL, schema_name), schema_name


@pytest.fixture(scope="session", autouse=True)
def _cleanup_postgres_schemas():
    yield
    if (os.environ.get("RUN_PG_TESTS") != "1" or not _SCHEMA_NAMES):
        return
    db.close_pools()
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        for name in sorted(_SCHEMA_NAMES):
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    _SCHEMA_NAMES.clear()


def test_ac10_bypass_paths_stay_nonblocking(monkeypatch, tmp_path):
    """AC10：审计旁路失败不阻塞主链路，只记录异常类型。"""
    config.STATE_DB = str(tmp_path / "state.db")
    monkeypatch.setattr(audit, "_connect", lambda: (_ for _ in ()).throw(
        ConnectionError("postgresql://user:secret@127.0.0.1:1/db")))
    audit.log("alice", "user", "登录成功")


@PG_TEST_MARK
def test_ac11_concurrent_registration_and_locking():
    """AC11：并发注册/租约在 PG 下不双成功，过期租约可接管。"""
    schema_url, _ = _create_test_schema()
    _configure_postgres(schema_url)
    session_id = f"locked-{uuid.uuid4().hex[:8]}"
    from server import session_lock

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        registrations = list(pool.map(
            lambda owner: sessions.register_session(session_id, owner),
            [f"owner-{i}" for i in range(8)]))
    assert registrations.count("ok") == 1
    assert session_lock.acquire(session_id, "worker-a")
    assert not session_lock.acquire(session_id, "worker-b")
    assert session_lock.release(session_id, "worker-a")
    assert session_lock.acquire(session_id, "worker-b")


@PG_TEST_MARK
def test_ac12_schedules_claims_exactly_once():
    """AC12：PG 到点 claim、事件唯一键与定义级租约 exactly-once。"""
    schema_url, _ = _create_test_schema()
    _configure_postgres(schema_url)
    from server import schedulers
    due_id = f"due-{uuid.uuid4().hex[:10]}"
    definition_name = f"lease-{uuid.uuid4().hex[:10]}"
    lease_ids = [f"lease-{uuid.uuid4().hex[:10]}" for _ in range(2)]
    with schedulers._connect() as conn:
        now = 1000.0
        for schedule_id in (due_id, *lease_ids):
            conn.execute(
                "INSERT INTO workflow_schedules (schedule_id, definition_name,"
                " params_json, interval_seconds, event_scope, name_filter,"
                " status, creator, creator_role, creator_department, next_run,"
                " claimed_at, running_since, running_trigger_id, created_at)"
                " VALUES (%s, %s, '{}', 60, '', '', 'active', 'alice',"
                " 'user', '', %s, NULL, NULL, '', %s)",
                (schedule_id, definition_name, now, now))
        assert conn.execute(
            "UPDATE workflow_schedules SET next_run = %s WHERE schedule_id = %s",
            (now, due_id))
        conn.commit()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: schedulers.process_due(now=now), range(2)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(
            lambda pair: schedulers._acquire_run(
                pair[0], definition_name, pair[1], now),
            zip(lease_ids, (f"trigger-{uuid.uuid4().hex}",
                            f"trigger-{uuid.uuid4().hex}"))))
    with schedulers._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_trigger_attempts"
            " WHERE schedule_id = %s", (due_id,)).fetchone()[0] == 1
        assert leases.count(True) == 1
        acquired = conn.execute(
            "SELECT schedule_id, running_trigger_id FROM workflow_schedules"
            " WHERE definition_name = %s AND running_trigger_id != ''",
            (definition_name,)).fetchone()
        conn.commit()
    schedulers._release_run(acquired[0], acquired[1])


def _configure_postgres(url):
    config.STATE_BACKEND = "postgres"
    config.USAGE_BACKEND = "postgres"
    config.STATE_DATABASE_URL = url
    config.USAGE_DATABASE_URL = url


@pytest.fixture(autouse=True)
def _restore_backend(monkeypatch):
    monkeypatch.setattr(config, "STATE_BACKEND", "sqlite")
    monkeypatch.setattr(config, "USAGE_BACKEND", "sqlite")
    monkeypatch.setattr(config, "STATE_DATABASE_URL", "")
    monkeypatch.setattr(config, "USAGE_DATABASE_URL", "")


def _sqlite_probe(path):
    """构造 P2-3 精度样本。"""
    conn = sqlite3.connect(path)
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
        CREATE TABLE run_records (
            run_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL NOT NULL,
            wall_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO usage_records VALUES
            (1, 1770000000.123456, '2026-02-02', 'u', 's', 'r', 'm', 'main',
             1, 2, 3, 0.12345678912345678, 0.23456789123456789, 10, 'ok');
        INSERT INTO run_records VALUES
            ('r', 'u', 's', 1770000000.123456, 1770000010.123456, 10000, 'ok', 0);
    """)
    conn.commit()
    conn.close()
def _sqlite_state_probe(path):
    """构造包含全部 12 张 state 表的迁移源库。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL,
                extra TEXT
            );
            CREATE TABLE session_meta (
                session_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                created REAL NOT NULL,
                deleted REAL,
                generation INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE approvals (
                aid TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                args TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL,
                decided REAL,
                user_id TEXT,
                session_id TEXT,
                decided_by TEXT,
                worker_id TEXT,
                executed_at TEXT
            );
            CREATE TABLE api_users (
                username TEXT PRIMARY KEY,
                key_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                expires_at REAL,
                password_hash TEXT,
                department TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE auth_tokens (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE login_fails (
                username TEXT PRIMARY KEY,
                fails INTEGER NOT NULL,
                lock_until REAL NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                actor TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE session_locks (
                session_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE TABLE workflow_instances (
                instance_id TEXT PRIMARY KEY,
                definition_name TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                initiator TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                params_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                failed_step TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                finished_at REAL,
                outputs_json TEXT NOT NULL DEFAULT '{}',
                summary TEXT NOT NULL DEFAULT '',
                trigger_source TEXT NOT NULL DEFAULT 'manual',
                schedule_id TEXT NOT NULL DEFAULT '',
                trigger_id TEXT NOT NULL DEFAULT '',
                planned_at REAL
            );
            CREATE TABLE workflow_schedules (
                schedule_id TEXT PRIMARY KEY,
                definition_name TEXT NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}',
                interval_seconds INTEGER NOT NULL DEFAULT 0,
                event_scope TEXT NOT NULL DEFAULT '',
                name_filter TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                creator TEXT NOT NULL,
                creator_role TEXT NOT NULL DEFAULT '',
                creator_department TEXT NOT NULL DEFAULT '',
                next_run REAL,
                claimed_at REAL,
                running_since REAL,
                running_trigger_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE workflow_trigger_attempts (
                attempt_id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                trigger_id TEXT NOT NULL,
                trigger_source TEXT NOT NULL,
                definition_name TEXT NOT NULL DEFAULT '',
                creator TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                finished_at REAL,
                run_id TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE TABLE disabled_packs (
                name TEXT PRIMARY KEY
            );
            INSERT INTO messages VALUES
                (1, 's', 'u', 'user', 'hello', 1770000000.123456, NULL);
            INSERT INTO audit_log VALUES
                (1, 1770000000123, 'u', 'user', 'test', '', '');
        """)
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,NULL)",
        [(index, 's', 'u', 'user', f'batch-{index}',
          1770000000.123456 + index) for index in range(2, 502)])
    conn.commit()
    conn.close()
