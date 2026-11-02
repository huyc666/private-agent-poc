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
import json
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
    # 出处标注（v0.17.25，PLAN 2.2）：assistant 消息附带的 citations JSON，
    # 随消息持久化以便历史回放重渲染出处卡片（老库自动补列，不丢数据）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "extra" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN extra TEXT")
    # 会话所有权元数据（v0.17.37 P0-1）：owner 注册表 + 删除 tombstone。
    # deleted：NULL=活跃；非空=删除时刻。行一经写入永不物理删除——tombstone
    # 是「删除后该 ID 永不可复用」的封禁依据（防接续旧模型上下文）。
    # generation（v0.17.38 P0）：删除代际——删除时递增（唯一递增来源），
    # 在途 chat 运行开场绑定当前代际，提交点校验「活跃且代际未变」，
    # 旧代际 fail-closed（复核报告 §5 准入条件）。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_meta (
            session_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            created REAL NOT NULL,
            deleted REAL,
            generation INTEGER NOT NULL DEFAULT 0
        )""")
    meta_cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(session_meta)").fetchall()]
    if "generation" not in meta_cols:   # 老库自动补列（存量行视为代际 0）
        conn.execute(
            "ALTER TABLE session_meta ADD COLUMN generation"
            " INTEGER NOT NULL DEFAULT 0")


def append(session_id: str, user_id: str, role: str, content: str,
           extra: str | None = None) -> None:
    """追加一条消息。content 过长时截断（对话记录不需要超大 payload）。
    extra：可选附加 JSON（assistant 消息的 citations 出处列表）。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO messages (session_id, user_id, role, content, ts, extra)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, user_id or "anonymous", role,
                 content[:20_000], time.time(), extra))
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
                "SELECT role, content, ts, user_id, extra FROM messages"
                " WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n)).fetchall()
            out = [dict(r) for r in reversed(rows)]
            for m in out:  # citations 仅 assistant 消息可能携带；坏 JSON 视为无
                raw = m.pop("extra", None)
                try:
                    m["citations"] = json.loads(raw) if raw else []
                    if not isinstance(m["citations"], list):
                        m["citations"] = []
                except (ValueError, TypeError):
                    m["citations"] = []
            return out
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
    """删除一个会话的全部消息（隐私/治理用），返回删除条数。
    仅清展示记录；所有权封禁与全存储面清理由 main._purge_session 编排
    （v0.17.37 P0-1），本函数不再是删除会话的完整语义。"""
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


# ------------------------------------------------- 会话所有权（v0.17.37 P0-1）
#
# 删除代际 fencing（v0.17.38 P0，spec 3.1/3.2）：tombstone 进程内缓存——
# tombstone 单调（一经删除永不复活），观察到 deleted 即永久缓存，重复校验
# 零 DB 开销（FencedCheckpointer 每次 checkpoint 写入都会走此校验）。
_deleted_cache: set[str] = set()


class SessionDeletedError(RuntimeError):
    """会话已删除：一切写提交（消息/审批/checkpoint/resume）fail-closed。"""


def tombstoned(session_id: str) -> bool:
    """会话是否已删除（tombstone 终局判定，含进程内缓存）。"""
    if session_id in _deleted_cache:
        return True
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT deleted FROM session_meta WHERE session_id = ?",
                (session_id,)).fetchone()
            if row is not None and row[0] is not None:
                _deleted_cache.add(session_id)
                return True
            return False
        finally:
            conn.close()


def fence_value(session_id: str) -> int | None:
    """当前代际（chat 开场绑定）：tombstoned → None；无 meta 行 → 0
    （开放模式新会话，活跃口径）。"""
    if tombstoned(session_id):
        return None
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT generation FROM session_meta WHERE session_id = ?",
                (session_id,)).fetchone()
            return row[0] if row is not None else 0
        finally:
            conn.close()


def fence_ok(session_id: str, generation: int) -> bool:
    """提交点终局校验：会话活跃且代际未变（无 meta 行 = 代际 0 匹配）。"""
    if session_id in _deleted_cache:
        return False
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT generation, deleted FROM session_meta WHERE session_id = ?",
                (session_id,)).fetchone()
            if row is not None and row[1] is not None:
                _deleted_cache.add(session_id)
                return False
            if row is None:
                return generation == 0
            return row[0] == generation
        finally:
            conn.close()


def append_fenced(session_id: str, generation: int, user_id: str, role: str,
                 content: str, extra: str | None = None) -> bool:
    """代际栅栏消息写入（spec 3.2）：单事务 BEGIN IMMEDIATE——先取写锁再
    校验代际/deleted，通过才 INSERT——「校验 + 写入」与 mark_deleted 的写
    事务互斥（SQLite 同库写锁），关闭 check-then-act 竞态：写要么先于删除
    提交（随后被 delete_session 清掉），要么看到 tombstone 被拒，不存在
    「删除完成后落库」的中间态。返回 True=落库 / False=被拒（fail-closed）。
    截断口径与 append 一致（对话记录不需要超大 payload）。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.isolation_level = None     # 手动事务：BEGIN IMMEDIATE 原子栅栏
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT generation, deleted FROM session_meta WHERE session_id = ?",
                (session_id,)).fetchone()
            ok = (row is None and generation == 0) or (
                row is not None and row[1] is None and row[0] == generation)
            if not ok:
                if row is not None and row[1] is not None:
                    _deleted_cache.add(session_id)
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO messages (session_id, user_id, role, content, ts, extra)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, user_id or "anonymous", role,
                 content[:20_000], time.time(), extra))
            conn.commit()
            return True
        finally:
            conn.close()


def get_meta(session_id: str) -> dict | None:
    """会话所有权元数据：{"owner_id", "deleted", "generation"}（deleted
    None=活跃，非 None=删除时刻 tombstone）；无记录返回 None。"""
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT owner_id, deleted, generation FROM session_meta"
                " WHERE session_id = ?", (session_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def register_session(session_id: str, owner_id: str) -> str:
    """新会话所有权原子抢注。返回 "ok" / "taken" / "deleted"：
    - "ok"：注册成功，或本人幂等重注，或既有归属为 anonymous（开放态宽恕）；
    - "taken"：已被他人注册；
    - "deleted"：tombstone——删除是封禁依据，该 ID 永不再注册。
    INSERT OR IGNORE + 冲突回读：并发争抢同一新 ID 时恰一人 "ok"
    （SQLite 写锁原子性，多进程共享同一 STATE_DB 亦成立）。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                "INSERT OR IGNORE INTO session_meta (session_id, owner_id, created)"
                " VALUES (?,?,?)",
                (session_id, owner_id or "anonymous", time.time()))
            inserted = cur.rowcount > 0
            conn.commit()
        finally:
            conn.close()
    if inserted:
        return "ok"
    meta = get_meta(session_id)
    if meta is None:
        return "taken"   # 理论不可达（冲突回读必有行）；fail-closed 口径
    if meta["deleted"] is not None:
        return "deleted"
    return "ok" if meta["owner_id"] in (owner_id, "anonymous") else "taken"


def mark_deleted(session_id: str) -> None:
    """写 tombstone（删除终局标记）：deleted=删除时刻 + generation 递增
    （v0.17.38 代际 fencing——在途运行绑定旧代际，此后一切提交点校验失败），
    行永久保留。会话此前无 meta 行（开放模式删除等场景）时先补 anonymous
    归属行再打标，保证任何路径的删除都留下封禁依据。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            now = time.time()
            conn.execute(
                "INSERT OR IGNORE INTO session_meta (session_id, owner_id, created)"
                " VALUES (?,?,?)", (session_id, "anonymous", now))
            conn.execute(
                "UPDATE session_meta SET deleted = ?,"
                " generation = generation + 1 WHERE session_id = ?",
                (now, session_id))
            conn.commit()
        finally:
            conn.close()
    _deleted_cache.add(session_id)


def resolve_owner(session_id: str) -> str | None:
    """会话归属解析（存量回填）：meta 有则返回其 owner；无记录而 messages
    有 → owner = 最早一条 user 消息的 user_id，INSERT OR IGNORE 回填后回读
    （并发他方已抢注则以回读为准）；消息亦无 → None（全新会话，归属由
    调用方 register_session 抢注）。
    v0.17.40（外部复验 N4，用户裁决收紧）：混合存量会话——存在非 anonymous
    发言时 owner 归「最早非 anonymous 发言者」，不归 anonymous——否则
    anonymous 宽恕会让任意登录用户读写含真人内容的会话（授权放宽方向）。
    全部发言均为 anonymous 的纯开放遗留会话仍归 anonymous（宽恕保留）。"""
    meta = get_meta(session_id)
    if meta is not None:
        return meta["owner_id"]
    owner = None
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            # 优先最早非 anonymous 发言者（N4）；全 anonymous 才回退 anonymous
            row = conn.execute(
                "SELECT user_id FROM messages WHERE session_id = ? AND role = 'user'"
                " AND user_id != 'anonymous' ORDER BY id LIMIT 1",
                (session_id,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT user_id FROM messages WHERE session_id = ? AND role = 'user'"
                    " ORDER BY id LIMIT 1", (session_id,)).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO session_meta (session_id, owner_id, created)"
                    " VALUES (?,?,?)", (session_id, row["user_id"], time.time()))
                conn.commit()
                owner = conn.execute(
                    "SELECT owner_id FROM session_meta WHERE session_id = ?",
                    (session_id,)).fetchone()["owner_id"]
        finally:
            conn.close()
    return owner
