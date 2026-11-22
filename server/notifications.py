"""PLAN 8.4：admin 跨会话操作通知（spec：docs/specs/8.4-admin-session-notification.md）。

- 通知存储：session_notifications 表（SQLite/Postgres 双后端统一适配）
- 通知创建：admin 查看/介入他人会话时触发
- 通知查询：归属用户查询未读通知（按时间倒序）
- 通知已读：用户标记已读（不可删除，仅标记）
"""
import logging
import sqlite3
import threading
import time

from . import audit, config, db

logger = logging.getLogger(__name__)

# schema 初始化（每进程、每个库路径只跑一次）
_schema_lock = threading.Lock()
_schema_ready_path: str | None = None


def init() -> None:
    """启动时建表（幂等）。"""
    global _schema_ready_path
    path = config.STATE_DB
    if _schema_ready_path == path:
        return
    with _schema_lock:
        if _schema_ready_path == path:
            return
        conn = db.connect("state")
        if isinstance(conn, sqlite3.Connection):
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS session_notifications (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        admin_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        read_at REAL
                    )""")
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_session_notifications_owner_unread
                    ON session_notifications (owner_id, read_at)""")
                conn.commit()
            finally:
                conn.close()
        else:
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS session_notifications (
                        id SERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        admin_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        read_at DOUBLE PRECISION
                    )""")
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_session_notifications_owner_unread
                    ON session_notifications (owner_id, read_at)""")
                conn.commit()
            finally:
                conn.close()
        _schema_ready_path = path


def create(session_id: str, owner_id: str, admin_id: str, action: str) -> int:
    """创建通知。返回通知 ID。"""
    init()
    now = time.time()
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        try:
            cur = conn.execute(
                "INSERT INTO session_notifications "
                "(session_id, owner_id, admin_id, action, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, owner_id, admin_id, action, now))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    else:
        try:
            cur = conn.execute(
                "INSERT INTO session_notifications "
                "(session_id, owner_id, admin_id, action, created_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (session_id, owner_id, admin_id, action, now))
            notif_id = cur.fetchone()[0]
            conn.commit()
            return notif_id
        finally:
            conn.close()


def list_unread(session_id: str, owner_id: str) -> list[dict]:
    """查询会话的未读通知（按时间倒序）。"""
    init()
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, session_id, owner_id, admin_id, action, "
                "created_at, read_at FROM session_notifications "
                "WHERE session_id = ? AND owner_id = ? AND read_at IS NULL "
                "ORDER BY created_at DESC",
                (session_id, owner_id)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    else:
        try:
            cur = conn.execute(
                "SELECT id, session_id, owner_id, admin_id, action, "
                "created_at, read_at FROM session_notifications "
                "WHERE session_id = %s AND owner_id = %s AND read_at IS NULL "
                "ORDER BY created_at DESC",
                (session_id, owner_id))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()


def mark_read(notification_id: int, owner_id: str) -> bool:
    """标记通知已读（仅归属用户可标记）。返回是否成功。"""
    init()
    now = time.time()
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        try:
            cur = conn.execute(
                "UPDATE session_notifications SET read_at = ? "
                "WHERE id = ? AND owner_id = ?",
                (now, notification_id, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    else:
        try:
            cur = conn.execute(
                "UPDATE session_notifications SET read_at = %s "
                "WHERE id = %s AND owner_id = %s",
                (now, notification_id, owner_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
