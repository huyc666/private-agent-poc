"""会话历史持久化：SQLite 存储（STATE_DB），重启不丢、同机多进程可协作。

替代 main.py 原内存 SESSIONS dict（P2 无状态化）。

职责边界：
- 本模块是「对话记录」——供前端展示、审计、重启后恢复会话视图；
- 真实模式下模型的上下文事实来源是 LangGraph checkpointer
  （memory / postgres，见 agent._make_checkpointer），两边各管各的：
  checkpointer 丢了模型失忆，但对话记录仍完整可查。

存储与 usage.py 同款模式：每次操作独立连接 + threading.Lock，
私有化部署零额外组件；生产跨机多副本须替换为 Postgres——SQLite 的
WAL + busy_timeout 只支持同机多进程共享同一库文件，不适合网络共享存储。
"""
import sqlite3
import threading
import time
from pathlib import Path

from . import config

_lock = threading.Lock()


def _db_path() -> Path:
    p = Path(config.STATE_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    """打开连接并设置多进程协作 pragma（WAL 读写不互斥 + busy_timeout 容忍写锁竞争）。"""
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")


def append(session_id: str, user_id: str, role: str, content: str) -> None:
    """追加一条消息。content 过长时截断（对话记录不需要超大 payload）。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO messages (session_id, user_id, role, content, ts)"
                " VALUES (?,?,?,?,?)",
                (session_id, user_id or "anonymous", role,
                 content[:20_000], time.time()))
            conn.commit()
        finally:
            conn.close()


def history(session_id: str, limit: int = 0) -> list[dict]:
    """取一个会话最近 N 条消息（时间正序）。limit<=0 时用 SESSION_MAX_HISTORY。"""
    n = limit if limit > 0 else config.SESSION_MAX_HISTORY
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT role, content, ts, user_id FROM messages"
                " WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n)).fetchall()
            return [dict(r) for r in reversed(rows)]
        finally:
            conn.close()


def list_sessions(limit: int = 50, user_id: str = "") -> list[dict]:
    """会话列表（按最后活跃倒序）：供管理界面/审计使用。
    user_id 非空时只返回该用户拥有消息的会话（资源级 ACL，v0.14.1）。"""
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            cond = " WHERE m.user_id = ?" if user_id else ""
            params = (user_id, limit) if user_id else (limit,)
            rows = conn.execute(
                "SELECT session_id, MAX(user_id) AS user_id, COUNT(*) AS messages,"
                " MAX(ts) AS last_active,"
                " (SELECT content FROM messages m2"
                "  WHERE m2.session_id = m.session_id AND m2.role = 'user'"
                "  ORDER BY m2.id LIMIT 1) AS first_message"
                f" FROM messages m{cond} GROUP BY session_id"
                " ORDER BY MAX(id) DESC LIMIT ?", params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def session_users(session_id: str) -> set[str]:
    """一个会话中出现过的全部用户标识（ACL 所有权判定用）。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM messages WHERE session_id = ?",
                (session_id,)).fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()


def delete_session(session_id: str) -> int:
    """删除一个会话的全部消息（隐私/治理用），返回删除条数。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM messages WHERE session_id = ?",
                               (session_id,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
