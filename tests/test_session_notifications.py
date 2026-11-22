"""PLAN 8.4：admin 跨会话操作通知（spec：docs/specs/8.4-admin-session-notification.md）。

- AC1 admin 查看他人会话创建通知
- AC2 通知持久化与查询
- AC3 标记通知已读
- AC4 admin 介入他人会话创建通知
- AC5 审计日志记录
"""
import os
import sqlite3
import time

import pytest

from server import audit, auth, config, db, notifications, sessions, usage


PG_URL = os.environ.get(
    "PG_URL", "postgresql://postgres:postgres@127.0.0.1:5432/agent_poc")


def _with_schema(url: str, schema_name: str) -> str:
    return f"{url}{('&' if '?' in url else '?')}options=-c%20search_path%3D{schema_name}"


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _rows(sql, params=()):
    conn = sqlite3.connect(config.STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- AC1 admin 查看通知

def test_ac1_admin_view_creates_notification():
    """admin 查看 alice 的会话，session_notifications 表新增记录。"""
    notifications.init()

    # alice 创建会话
    session_id = "test-session-001"
    sessions.register_session(session_id, "alice")

    # admin 查看该会话（模拟路由触发）
    notifications.create(
        session_id=session_id,
        owner_id="alice",
        admin_id="admin",
        action="view"
    )

    rows = _rows("SELECT * FROM session_notifications WHERE session_id = ?",
                 (session_id,))
    assert len(rows) == 1
    assert rows[0]["owner_id"] == "alice"
    assert rows[0]["admin_id"] == "admin"
    assert rows[0]["action"] == "view"
    assert rows[0]["read_at"] is None


# ---------------------------------------------------------------- AC2 通知持久化与查询

def test_ac2_notification_persistence_and_query(client_factory):
    """alice 有未读通知，GET /api/sessions/{id}/notifications 返回列表。"""
    notifications.init()

    session_id = "test-session-002"
    sessions.register_session(session_id, "alice")

    # 创建两条通知（不同时间）
    now = time.time()
    notifications.create(session_id, "alice", "admin", "view")
    time.sleep(0.01)
    notifications.create(session_id, "alice", "admin2", "intervene")

    key = auth.add_user("alice", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})

    r = c.get(f"/api/sessions/{session_id}/notifications")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    # 按时间倒序（最新在前）
    assert data[0]["admin_id"] == "admin2"
    assert data[0]["action"] == "intervene"
    assert data[1]["admin_id"] == "admin"
    assert data[1]["action"] == "view"


# ---------------------------------------------------------------- AC3 标记通知已读

def test_ac3_mark_notification_read(client_factory):
    """alice 标记通知已读，read_at 更新，再次查询不再返回。"""
    notifications.init()

    session_id = "test-session-003"
    sessions.register_session(session_id, "alice")

    notif_id = notifications.create(session_id, "alice", "admin", "view")

    key = auth.add_user("alice", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})

    # 标记已读
    r = c.post(f"/api/sessions/{session_id}/notifications/{notif_id}/read")
    assert r.status_code == 200

    # 再次查询（应返回空）
    r = c.get(f"/api/sessions/{session_id}/notifications")
    assert r.status_code == 200
    assert len(r.json()) == 0

    # 验证 read_at 已更新
    rows = _rows("SELECT read_at FROM session_notifications WHERE id = ?",
                 (notif_id,))
    assert rows[0]["read_at"] is not None


# ---------------------------------------------------------------- AC4 admin 介入通知

def test_ac4_admin_intervene_creates_notification():
    """admin 在 alice 会话中发消息，创建 intervene 类型通知。"""
    notifications.init()

    session_id = "test-session-004"
    sessions.register_session(session_id, "alice")

    # 模拟 admin 介入（POST /api/chat 触发）
    notifications.create(
        session_id=session_id,
        owner_id="alice",
        admin_id="admin",
        action="intervene"
    )

    rows = _rows("SELECT * FROM session_notifications WHERE session_id = ?",
                 (session_id,))
    assert len(rows) == 1
    assert rows[0]["action"] == "intervene"


# ---------------------------------------------------------------- AC5 审计日志

def test_ac5_audit_log_admin_view_session(client_factory):
    """admin 查看 alice 会话，审计日志记录 admin_view_session。"""
    notifications.init()
    audit.init()

    session_id = "test-session-005"
    sessions.register_session(session_id, "alice")

    # admin 登录
    admin_key = auth.add_user("admin", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {admin_key}"})

    # admin 查看 alice 会话（触发通知 + 审计）
    r = c.get(f"/api/sessions/{session_id}/history")
    # 预期 200（admin 有权访问）
    assert r.status_code in (200, 404)  # 会话可能无消息记录

    # 验证审计日志
    recs = audit.query(limit=10)
    view_logs = [r for r in recs if r["action"] == "admin_view_session"]
    assert len(view_logs) == 1
    assert view_logs[0]["actor"] == "admin"
    assert view_logs[0]["target"] == "alice"
