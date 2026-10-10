"""危险操作人工审批注册中心（DB 持久化版，P2 无状态化）。

存储：SQLite（STATE_DB，与会话记录同库），替代原进程内存 PENDING dict。
- 审批记录重启可查（审计列：状态 + 时间戳）；
- 同机多进程部署时，创建审批的 worker 与处理 /api/approve 的 worker 可以不同进程：
  决定落库，等待方轮询读取（0.4s 间隔，开销可忽略），跨进程协作成立。

状态机：pending → approved / rejected / expired / abandoned
- expired：等待超时由等待方标记（fail-closed 语义等价于拒绝，但审计上可区分）；
- abandoned：approved 但从未执行（v0.17.2）——批准决议已落库，但负责 resume
  的等待协程先死（如客户端断开被取消），危险操作实际未执行。由启动/周期扫描
  标记，审计可见「已批准但执行丢失」，fail-closed 兜底（绝不补执行）。

执行连续性追踪（v0.17.2）：
- worker_id：创建审批的进程（hostname:pid），用于定位谁负责 resume；
- executed_at：Agent 把批准决议带进图并完成 resume 后标记。
- approved 且 executed_at 为空 = 批准了但没执行 → 扫描转 abandoned。

安全原则不变：fail-closed —— 审批超时、记录丢失、审批服务异常，
一律视为拒绝，危险操作绝不执行。
"""
import asyncio
import os
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import config
from .usage import current_session, current_user

APPROVAL_TIMEOUT = 120  # 秒，超时自动按拒绝处理（标记 expired）
POLL_INTERVAL = 0.4     # 等待方轮询数据库的间隔（跨进程协作的代价上限）

# 本进程标识（hostname:pid）：审批记录的创建者，即负责等待并 resume 图的 worker。
# 单副本 MemorySaver 下 resume 只能由创建它的进程完成（README 已声明），
# 该标识用于审计定位「批准了但没人执行」是哪台机哪个进程的责任。
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

_lock = threading.Lock()


def _db_path() -> Path:
    p = Path(config.STATE_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    """打开连接并设置同机多进程协作所需的 pragma：
    WAL 读写不互斥（同机多进程共享同一库文件的关键），busy_timeout 容忍短暂写锁竞争。"""
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            aid TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            args TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created REAL NOT NULL,
            decided REAL
        )""")
    # v0.14.0 身份绑定迁移：旧库自动加列（旧记录为 NULL，
    # 其决议权限收紧为仅 approver/admin，见 resolve()）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(approvals)")}
    for col in ("user_id", "session_id", "decided_by"):
        if col not in cols:
            conn.execute(f"ALTER TABLE approvals ADD COLUMN {col} TEXT")
    # v0.17.2 执行连续性迁移：worker_id = 创建审批的进程（审计定位）；
    # executed_at = Agent 完成 resume 的时间（approved 且为空 = 批准但未执行）。
    # 旧记录两列均为 NULL：approved 旧记录会在启动扫描时被标记 abandoned（无法追溯
    # 是否执行过，fail-closed 视为未执行），pending 旧记录照常 expired。
    for col in ("worker_id", "executed_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE approvals ADD COLUMN {col} TEXT")


def _execute(sql: str, params: tuple = ()) -> int:
    """执行写操作，返回受影响行数（在连接关闭前取 rowcount）。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def _query_one(sql: str, params: tuple = ()) -> dict | None:
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def create_approval(tool: str, args: dict) -> str:
    """登记一条待审批请求，返回 approval_id。
    创建人/来源会话取自请求上下文（contextvars，由 stream_reply 设置）；
    开放模式下为 anonymous / 空串。worker_id 记录创建进程（v0.17.2）。"""
    import json
    aid = uuid.uuid4().hex[:12]
    _execute(
        "INSERT INTO approvals (aid, tool, args, status, created, user_id, session_id, worker_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (aid, tool, json.dumps(args, ensure_ascii=False)[:2000], "pending",
         time.time(), current_user.get(), current_session.get(), WORKER_ID))
    return aid


async def wait_decision(aid: str, timeout: float = APPROVAL_TIMEOUT):
    """等待用户审批决定。返回 True(批准) / False(拒绝) / None(超时或记录丢失)。

    轮询数据库而非进程内事件：审批决定可能由另一个 worker 进程写入。
    超时时把记录标记为 expired（仍 pending 才标记），保证迟到的决议不会
    产生「显示批准但实际未执行」的误导。
    """
    deadline = time.time() + timeout
    while True:
        rec = await asyncio.to_thread(
            _query_one, "SELECT status FROM approvals WHERE aid = ?", (aid,))
        if rec is None:
            return None
        if rec["status"] == "approved":
            return True
        if rec["status"] in ("rejected", "expired"):
            return False if rec["status"] == "rejected" else None
        if time.time() >= deadline:
            # 与 resolve 的竞态：只有成功把 pending 标成 expired 才算超时；
            # 落败说明决议已抢先落库，以决议为准（杜绝「显示批准但未执行」）
            n = await asyncio.to_thread(
                _execute,
                "UPDATE approvals SET status = 'expired', decided = ?"
                " WHERE aid = ? AND status = 'pending'",
                (time.time(), aid))
            if n == 0:
                rec = await asyncio.to_thread(
                    _query_one, "SELECT status FROM approvals WHERE aid = ?", (aid,))
                if rec and rec["status"] == "approved":
                    return True
                if rec and rec["status"] == "rejected":
                    return False
            return None
        await asyncio.sleep(POLL_INTERVAL)


def resolve(aid: str, approve: bool, username: str = "", role: str = "admin") -> str:
    """前端提交审批决定。返回 "ok" / "forbidden" / "not_found"。

    决议权限（v0.14.0，v0.17.9 收紧）：仅 admin / approver 可决议任何 pending。
    此前「普通 user 可决议自己创建的」存在自审自批漏洞——发起者自己批准即可
    放行危险操作（创建可执行工具、启停领域包等全局影响），审批门形同虚设。
    user 角色发起的审批等待 approver/admin 决议；开放模式调用方传 role="admin"。
    仅 pending → 决议 算成功（原子 UPDATE，并发重复提交安全）。
    """
    rec = _query_one("SELECT status FROM approvals WHERE aid = ?", (aid,))
    if rec is None or rec["status"] != "pending":
        return "not_found"
    if role not in ("admin", "approver"):
        return "forbidden"
    status = "approved" if approve else "rejected"
    n = _execute(
        "UPDATE approvals SET status = ?, decided = ?, decided_by = ?"
        " WHERE aid = ? AND status = 'pending'",
        (status, time.time(), username or None, aid))
    return "ok" if n > 0 else "not_found"


def pending_count() -> int:
    rec = _query_one(
        "SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'")
    return rec["n"] if rec else 0


def mark_executed(aid: str) -> None:
    """Agent 已把批准决议带进图并完成 resume：标记 executed_at。
    仅对 approved 记录生效（幂等，重复调用无副作用）。
    未标记的 approved 记录 = 「批准了但没执行」，由 mark_abandoned 转为 abandoned。"""
    _execute(
        "UPDATE approvals SET executed_at = ? WHERE aid = ? AND status = 'approved'",
        (time.time(), aid))


def mark_abandoned(grace: float = 0.0) -> int:
    """把「已批准但从未执行」的记录标记为 abandoned（审计可见执行丢失）。
    触发点：服务启动（cleanup_stale 内）与周期扫描（main.py 后台任务），
    两者都传 grace>0——共享 STATE_DB 时可能有其他进程/协程的在途 resume。

    grace（秒）：只清理决议时间早于 now-grace 的记录。用户刚批准 → resume
    图执行中 → 跑完才 mark_executed，这条链路上记录天然处于「approved 且
    executed_at 为空」；周期扫描若无宽限会误伤正在执行的长任务（误标后
    mark_executed 静默失效，审计反而失真）。决议在 grace 窗口内的记录留给
    下一轮扫描判定。fail-closed 不变：无法证明执行过就不算执行过。"""
    return _execute(
        "UPDATE approvals SET status = 'abandoned', decided = ?"
        " WHERE status = 'approved' AND executed_at IS NULL"
        " AND (decided IS NULL OR decided <= ?)",
        (time.time(), time.time() - grace))


def cleanup_stale(grace: float = 0.0) -> int:
    """服务启动时调用：
    1) 残留 pending（等待方已随进程消亡）→ expired；
    2) approved 但从未执行（等待协程先死 / resume 未发生）→ abandoned，
       审计可见「已批准但执行丢失」（v0.17.2）。
    grace 透传给 mark_abandoned：同机多进程共享 STATE_DB 时，本进程启动
    不代表其他进程没有在途 resume，调用方应传宽限期（main.py lifespan）。"""
    n = _execute(
        "UPDATE approvals SET status = 'expired', decided = ?"
        " WHERE status = 'pending'", (time.time(),))
    return n + mark_abandoned(grace)


def list_recent(limit: int = 50, user_id: str = "") -> list[dict]:
    """最近审批记录（审计用）。user_id 非空时只返回该创建人的记录（资源级 ACL）。"""
    with _lock:
        conn = sqlite3.connect(str(_db_path()))
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            cond = " WHERE user_id = ?" if user_id else ""
            params = (user_id, limit) if user_id else (limit,)
            rows = conn.execute(
                "SELECT aid, tool, args, status, created, decided,"
                " user_id, session_id, decided_by, worker_id, executed_at"
                " FROM approvals"
                f"{cond} ORDER BY created DESC LIMIT ?", params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def require_approval(tool: str, args: dict, action: str, execute) -> str:
    """审批门：在 LangGraph 上下文中发起 interrupt 审批，用户批准后才调用 execute()。

    收敛所有「危险工具」的审批样板（原 7 处重复代码）：
    - tool: 工具名（展示在前端审批卡片）
    - args: 审批展示的参数摘要
    - action: 操作中文名（用于拒绝提示语，如 "删除"/"工具创建"）
    - execute: 零参数可调用对象，仅批准后执行

    fail-closed：不在图上下文 / 未配置 checkpointer / 用户拒绝 / 决定格式异常，
    一律不执行。interrupt 的中断信号（GraphBubbleUp）必须放行，不能吞掉。
    """
    try:
        from langgraph.types import interrupt

        decision = interrupt({"kind": "approval", "tool": tool, "args": args})
    except Exception as e:
        from langgraph.errors import GraphBubbleUp
        if isinstance(e, GraphBubbleUp):
            raise
        return "危险操作未执行：审批流不可用（fail-closed 安全策略）"
    if not isinstance(decision, dict) or not decision.get("approve"):
        return f"用户拒绝了该{action}操作，未执行。"
    return execute()
