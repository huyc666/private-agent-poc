"""Token 计量：按 用户 / 任务（单次运行）/ 会话 三个维度记录与聚合。

采集原理：
- ChatOpenAI 开启 stream_usage=True 后，每次模型调用的结束事件携带
  usage_metadata（prompt_tokens / completion_tokens / total_tokens）
- UsageCallback 挂在 LLM 对象上，主 Agent 与多 Agent worker 的每次调用都会触发
- stream_reply 开头用 contextvars 设置 (user_id, session_id, run_id)，
  回调读取上下文完成归属（asyncio 任务创建时复制上下文，嵌套调用同样生效）

存储：SQLite（USAGE_DB，默认项目根 usage.db），私有化部署零额外组件；
生产可替换为 Postgres（表结构见 _ensure_schema）。

维度定义：
- 用户 user_id：请求方标识（API 认证落地前由调用方传入，缺省 anonymous）
- 任务 run_id：一次 /api/chat 请求的完整运行（可能含多轮模型调用与 worker 子调用）
- 会话 session_id：同一对话的多次运行累计
"""
import contextvars
import sqlite3
import threading
import time
from pathlib import Path

from . import config

# 请求上下文（stream_reply 设置，UsageCallback 读取）
current_user = contextvars.ContextVar("usage_user", default="anonymous")
current_session = contextvars.ContextVar("usage_session", default="")
current_run = contextvars.ContextVar("usage_run", default="")

_lock = threading.Lock()


def _db_path() -> Path:
    p = Path(config.USAGE_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    """打开连接并设置多进程协作 pragma（与 sessions/approval 同款：
    WAL 读写不互斥 + busy_timeout 容忍写锁竞争，避免高并发落账时
    database is locked 导致静默丢账）。"""
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_records (
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
            total_tokens INTEGER NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_run ON usage_records(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_records(date)")


def record(model: str, usage: dict, source: str = "main") -> None:
    """记录一次模型调用的 token 消耗（归属信息取自 contextvars）。
    usage 为空或全零（提供方未返回 usage）时跳过。"""
    pt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    ct = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    tt = int(usage.get("total_tokens") or pt + ct)
    if tt <= 0:
        return
    now = time.time()
    row = (now, time.strftime("%Y-%m-%d", time.localtime(now)),
           current_user.get(), current_session.get(), current_run.get(),
           model or config.MODEL_NAME, source, pt, ct, tt)
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO usage_records (ts, date, user_id, session_id, run_id,"
                " model, source, prompt_tokens, completion_tokens, total_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            conn.commit()
        finally:
            conn.close()


class UsageCallback:
    """LangChain 回调：每次 LLM 调用结束时提取 usage_metadata 落账。
    挂在 ChatOpenAI 上，主 Agent 与多 Agent worker（共用 _llm）全覆盖。"""

    def __init__(self):
        from langchain_core.callbacks import BaseCallbackHandler

        class _Handler(BaseCallbackHandler):
            def on_llm_end(self, response, **kwargs):
                try:
                    msg = response.generations[0][0].message
                    u = getattr(msg, "usage_metadata", None) or {}
                    model = (getattr(msg, "response_metadata", None) or {}).get(
                        "model_name") or config.MODEL_NAME
                    # 多 Agent worker 的调用带 ma_worker 标签时标记来源
                    source = "worker" if "ma_worker" in (kwargs.get("tags") or []) else "main"
                    record(model, u, source)
                except Exception:
                    pass  # 计量失败绝不阻塞主链路

        self._handler = _Handler()

    @property
    def handler(self):
        return self._handler


def _query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def run_usage(run_id: str) -> dict:
    """单次任务（一次 /api/chat 运行）的消耗合计。"""
    rows = _query(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens),0) AS total_tokens"
        " FROM usage_records WHERE run_id = ?", (run_id,))
    return rows[0] if rows else {"calls": 0, "prompt_tokens": 0,
                                 "completion_tokens": 0, "total_tokens": 0}


def session_usage(session_id: str) -> dict:
    """一个会话的累计消耗。"""
    rows = _query(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens),0) AS total_tokens"
        " FROM usage_records WHERE session_id = ?", (session_id,))
    return rows[0] if rows else {"calls": 0, "prompt_tokens": 0,
                                 "completion_tokens": 0, "total_tokens": 0}


def summary(user_id: str = "", session_id: str = "", days: int = 0) -> dict:
    """聚合视图：总量 + 按用户 / 按会话 / 按任务（run）分组。
    days>0 时只统计最近 N 天。"""
    where, params = [], []
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if session_id:
        where.append("session_id = ?")
        params.append(session_id)
    if days > 0:
        where.append("ts >= ?")
        params.append(time.time() - days * 86400)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    group_cols = ("prompt_tokens", "completion_tokens", "total_tokens")

    total = _query(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens),0) AS total_tokens"
        f" FROM usage_records{cond}", tuple(params))
    by_user = _query(
        "SELECT user_id, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens,"
        " SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens"
        f" FROM usage_records{cond} GROUP BY user_id ORDER BY total_tokens DESC LIMIT 50",
        tuple(params))
    by_session = _query(
        "SELECT session_id, user_id, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens"
        f" FROM usage_records{cond} GROUP BY session_id ORDER BY total_tokens DESC LIMIT 50",
        tuple(params))
    by_run = _query(
        "SELECT run_id, session_id, user_id, date, COUNT(*) AS calls,"
        " SUM(total_tokens) AS total_tokens,"
        " SUM(CASE WHEN source='worker' THEN total_tokens ELSE 0 END) AS worker_tokens"
        f" FROM usage_records{cond} GROUP BY run_id ORDER BY MAX(ts) DESC LIMIT 50",
        tuple(params))
    return {"total": total[0] if total else {}, "by_user": by_user,
            "by_session": by_session, "by_run": by_run}
