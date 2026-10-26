"""统一操作审计日志（PLAN 6.3，spec：docs/specs/6.3-audit-log.md）。

设计要点（沿项目既有约定）：
- 独立于 Langfuse 观测：观测服务可断连/清空，审计须本地持久（STATE_DB）
- 审计写入失败不阻塞主链路（与计量失败处理一致：log warning 继续）
- 明文密码 / Key 值绝不入审计；target 只存用户名/文件名/审批单 ID（域内相对路径）
- 容量上限 CAPACITY（默认 10 万条），超限裁最旧 10%（对齐 login_fails 裁剪先例）
- STATE_DB 单机多进程共享；Postgres 化随 6.4 演进
- config.STATE_DB 运行时读取（测试 monkeypatch 隔离依赖此约定）
"""
import sqlite3
import threading
import time

from server import config

CAPACITY = 100_000   # 容量上限；超限触发裁剪
TRIM_RATIO = 0.9     # 裁剪后保留比例（裁最旧 10%）

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,          -- 毫秒时间戳（与 usage_records 口径一致）
    actor  TEXT NOT NULL DEFAULT '', -- 操作人（登录失败记尝试的用户名）
    role   TEXT NOT NULL DEFAULT '', -- 操作时角色（工具级可能为空）
    action TEXT NOT NULL,            -- 中文动作名（与 _audit 现有口径一致）
    target TEXT NOT NULL DEFAULT '', -- 对象：用户名/文件名/审批单 ID（相对路径）
    detail TEXT NOT NULL DEFAULT ''  -- 补充：来源 IP 等（不含敏感值）
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.STATE_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def _execute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """DML 执行（单语句）。独立成函数便于测试模拟写入失败（不阻塞主链路）。"""
    return conn.execute(sql, params).rowcount


def _trim_if_needed(conn: sqlite3.Connection) -> None:
    """超容量裁最旧：保留 floor(CAPACITY * TRIM_RATIO)，余量删除。"""
    (total,) = conn.execute(
        "SELECT COUNT(*) FROM audit_log").fetchone()
    if total > CAPACITY:
        keep = int(CAPACITY * TRIM_RATIO)
        _execute(conn,
                 "DELETE FROM audit_log WHERE id IN "
                 "(SELECT id FROM audit_log ORDER BY id ASC LIMIT ?)",
                 (total - keep,))


def log(actor: str, role: str, action: str, target: str = "",
        detail: str = "") -> None:
    """写一条审计记录。任何异常只打印告警，绝不阻塞调用方主链路（AC6）。"""
    ts = int(time.time() * 1000)
    try:
        with _lock:
            conn = _connect()
            try:
                _ensure_schema(conn)
                _execute(
                    conn,
                    "INSERT INTO audit_log (ts, actor, role, action, target,"
                    " detail) VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, (actor or "")[:64], (role or "")[:16],
                     (action or "")[:32], (target or "")[:200],
                     (detail or "")[:200]))
                _trim_if_needed(conn)
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        # 不回显原始异常文本（防绝对路径泄露），仅类型告警
        print(f"[audit] 写入失败，不阻塞主链路（{type(e).__name__}）")


def query(actor: str = "", action: str = "", since_ms: int = 0,
          limit: int = 200) -> list[dict]:
    """按条件查询审计流水（时间倒序）。limit 收敛到 [1, 1000]。"""
    limit = min(max(int(limit or 200), 1), 1000)
    sql = ("SELECT ts, actor, role, action, target, detail"
           " FROM audit_log")
    conds, params = [], []
    if actor:
        conds.append("actor = ?")
        params.append(actor.strip())
    if action:
        conds.append("action = ?")
        params.append(action.strip())  # 与 actor 同口径（评审 C5 一致性小修）
    if since_ms:
        conds.append("ts >= ?")
        params.append(int(since_ms))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def init() -> None:
    """启动时建表并执行一次容量裁剪（老库首次启用即收敛）。"""
    try:
        with _lock:
            conn = _connect()
            try:
                _ensure_schema(conn)
                _trim_if_needed(conn)
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"[audit] 初始化失败（{type(e).__name__}）——首条写入时会重试建表")
