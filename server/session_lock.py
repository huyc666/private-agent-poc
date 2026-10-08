"""跨进程会话锁（租约 + 心跳，STATE_DB 后端）。

背景（v0.16.4 → v0.17.1 加固）：同会话串行化原先只有进程内 asyncio.Lock，
多副本共享 checkpointer / STATE_DB 时互斥失效（两个 worker 各持各的锁，
同一 session 的并发请求可能同时推进，导致对话记录交错、checkpointer 状态写冲突）。
本模块以 STATE_DB 为后端提供**跨进程互斥**：

- 持有者持有期间定期心跳续租；worker 崩溃后租约自动过期，他人可接管；
- 同一进程内由调用方持有的进程内 asyncio.Lock 承担快速路径，避免频繁写库；
- 数据库写入全部走 WAL + busy_timeout，跨进程原子性由 SQLite 自身保证。

前提：跨进程互斥要求各副本指向同一 STATE_DB（与 checkpointer 共享存储、
会话/审批/用量协作是同一前提）。MemorySaver 模式本就不支持多副本共享上下文。
"""
import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

from . import config

LEASE_TTL = 60.0          # 秒：无心跳即视为持有者崩溃，租约到期可被接管
HEARTBEAT_INTERVAL = 15.0  # 心跳间隔（远小于 TTL，健康持有者租约不会过期）
POLL_INTERVAL = 0.4       # 等待租约时的轮询间隔
# 获取租约的最大等待时间（秒，架构评审 P3）：此前无界等待——持有者长任务
# （如审批 resume 挂起）时，同会话新请求会无限挂起且用户无任何反馈。
# 超时报错让用户重试，而不是干等；多轮排队场景可调大此值。
LEASE_ACQUIRE_TIMEOUT = float(os.environ.get("LEASE_ACQUIRE_TIMEOUT", "120"))


class LeaseTimeout(RuntimeError):
    """等待会话租约超时（同会话另一请求长时间持有，请稍后重试）。"""

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    p = Path(config.STATE_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_locks (
            session_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            ts REAL NOT NULL
        )""")


def _execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def acquire(session_id: str, owner: str, ttl: float = LEASE_TTL) -> bool:
    """尝试获取租约。成功返回 True；租约仍被他人持有（且未过期）返回 False。
    三步全原子：先接管过期租约，再尝试插入，最后确认自己是否已是持有者。"""
    now = time.time()
    if _execute("UPDATE session_locks SET owner=?, ts=? WHERE session_id=? AND ts+? < ?",
                (owner, now, session_id, ttl, now)):
        return True
    if _execute("INSERT OR IGNORE INTO session_locks(session_id, owner, ts)"
                " VALUES (?,?,?)", (session_id, owner, now)):
        return True
    return _execute("UPDATE session_locks SET ts=? WHERE session_id=? AND owner=?",
                    (now, session_id, owner)) > 0


def renew(session_id: str, owner: str) -> bool:
    """心跳续租：仅当租约仍归本人时更新时间戳。返回 False = 已被接管。"""
    return _execute("UPDATE session_locks SET ts=? WHERE session_id=? AND owner=?",
                    (time.time(), session_id, owner)) > 0


def release(session_id: str, owner: str) -> bool:
    """释放租约（幂等，仅删本人持有的行）。"""
    return _execute("DELETE FROM session_locks WHERE session_id=? AND owner=?",
                    (session_id, owner)) > 0


class SessionLease:
    """async 上下文：获取租约（必要时等待）→ 心跳续租 → 退出时释放。
    用法（配合进程内 asyncio.Lock 双重互斥）：

        async with agent.session_lock(session_id):
            async with session_lock.SessionLease(session_id, owner):
                ... 整轮对话 ...
    """

    def __init__(self, session_id: str, owner: str, ttl: float = LEASE_TTL,
                 wait_timeout: float = LEASE_ACQUIRE_TIMEOUT):
        self.session_id = session_id
        self.owner = owner
        self.ttl = ttl
        self.wait_timeout = wait_timeout
        self.acquired = False
        self.lost = False  # 心跳发现租约被他人接管 → 调用方应中止本轮对话
        self._hb: asyncio.Task | None = None

    async def __aenter__(self):
        # 有界等待（架构评审 P3）：此前 while 循环无上限，同会话另一请求
        # 长时间持有租约（如审批 resume 挂起）时新请求会无限挂起且无反馈。
        # 超时抛 LeaseTimeout，由调用方向用户返回可理解的错误事件。
        deadline = time.monotonic() + self.wait_timeout
        while not self.acquired:
            self.acquired = await asyncio.to_thread(
                acquire, self.session_id, self.owner, self.ttl)
            if self.acquired:
                break
            if time.monotonic() >= deadline:
                raise LeaseTimeout(
                    f"会话 {self.session_id} 的租约被其他请求占用超过 "
                    f"{self.wait_timeout:.0f}s，请稍后重试")
            await asyncio.sleep(POLL_INTERVAL)
        self._hb = asyncio.create_task(self._heartbeat())
        return self

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                ok = await asyncio.to_thread(renew, self.session_id, self.owner)
                if not ok:
                    # 租约已被他人接管（本进程持有的身份丢失，心跳再发无意义）。
                    # 置 lost 标志供调用方中止对话：继续跑意味着两个进程同时
                    # 推进同一会话——正是本模块要防的 checkpointer 写冲突场景，
                    # 只记日志继续跑等于互斥失效（架构评审 P2 修复）。
                    self.lost = True
                    print(f"[session_lock] 租约已被接管，中止本轮对话: {self.session_id}")
                    break
        except asyncio.CancelledError:
            pass

    async def __aexit__(self, *exc):
        if self._hb is not None:
            self._hb.cancel()
        if self.acquired:
            await asyncio.to_thread(release, self.session_id, self.owner)
            self.acquired = False
