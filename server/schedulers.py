"""定时/事件触发调度器（v0.17.49，PLAN 4.2）。

spec：docs/specs/4.2-scheduled-triggers.md。设计要点：
- 定时语义仅 interval：首次到点 = 登记时间 + interval，下限 60s；
  跨周期推进不追赶，时钟回拨无动作，停机错过不补跑。
- 原子防重：到点判断/claim/next_run 推进与触发尝试留痕在同一事务
  （BEGIN IMMEDIATE，沿 v0.17.38 写锁先例）；事件以（事件 ID × 调度）为
  唯一触发键，重复派发幂等。锁在调度行，实例历史不做锁。
- 身份时效：调度行只存创建者主体；每次触发重新解析当前有效身份
  （存在、未吊销、角色满足 Q3、部门白名单经包权限自然生效），不满足即
  拒绝并留可查询的触发尝试记录。
- 异步边界：process_due/on_file_uploaded 是同步 claim-only 函数，不在
  事件循环线程执行编排；执行经 asyncio.create_task 派发，任务集合 +
  done 回调观察异常，shutdown 取消并等待。
- 旁路不阻塞：上传事件挂钩自带 try/except 完整隔离，失败只记日志，
  不影响上传主链路。
- 部署守护 fail-closed：进程内循环单例；多 worker（WEB_CONCURRENCY>1）
  拒启调度循环并 WARNING。
"""
import asyncio
import fnmatch
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import audit, auth, config, custom_tools, orchestration as orch, usage
from .state import STATE

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 60        # interval_seconds 生产下限（注入时钟不影响）
SCHED_LOOP_INTERVAL = 30.0      # 调度循环轮询周期
STALE_RUNNING_GRACE = 3600.0    # 启动清扫：running 超过该时长视为重启遗留
ATTEMPT_CAPACITY = 100_000      # 触发尝试表容量上限（对齐 audit 10 万条先例）
ATTEMPT_TRIM_RATIO = 0.9        # 超限后保留比例（裁最旧 10%）

_SCHED_LOCK = threading.Lock()  # 建表互斥（首次调用并发场景）


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.STATE_DB, timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str,
                    columns: tuple) -> None:
    """存量库幂等增列（沿 v0.17.22 ALTER 先例）。"""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, ddl in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_schedules (
            schedule_id        TEXT PRIMARY KEY,
            definition_name    TEXT NOT NULL,
            params_json        TEXT NOT NULL DEFAULT '{}',
            interval_seconds   INTEGER NOT NULL DEFAULT 0,
            event_scope        TEXT NOT NULL DEFAULT '',
            name_filter        TEXT NOT NULL DEFAULT '',
            status             TEXT NOT NULL DEFAULT 'active',
            creator            TEXT NOT NULL,
            creator_role       TEXT NOT NULL DEFAULT '',
            creator_department TEXT NOT NULL DEFAULT '',
            next_run           REAL,
            claimed_at         REAL,
            running_since      REAL,
            running_trigger_id TEXT NOT NULL DEFAULT '',
            created_at         REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflow_trigger_attempts (
            attempt_id     TEXT PRIMARY KEY,
            schedule_id    TEXT NOT NULL,
            trigger_id     TEXT NOT NULL,
            trigger_source TEXT NOT NULL,
            definition_name TEXT NOT NULL DEFAULT '',
            creator        TEXT NOT NULL DEFAULT '',
            result         TEXT NOT NULL,
            reason         TEXT NOT NULL DEFAULT '',
            outcome        TEXT NOT NULL DEFAULT '',
            finished_at    REAL,
            payload_json   TEXT NOT NULL DEFAULT '{}',
            created_at     REAL NOT NULL
        )
    """)
    # v0.17.49 补丁：运行租约（P1-1）与尝试终态（P2-3）列
    _ensure_columns(conn, "workflow_schedules", (
        ("running_since", "REAL"),
        ("running_trigger_id", "TEXT NOT NULL DEFAULT ''")))
    _ensure_columns(conn, "workflow_trigger_attempts", (
        ("outcome", "TEXT NOT NULL DEFAULT ''"),
        ("finished_at", "REAL")))
    # 事件 × 调度唯一触发键（幂等）
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_wf_attempt_key"
                 " ON workflow_trigger_attempts(schedule_id, trigger_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wf_sched_due"
                 " ON workflow_schedules(status, next_run)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wf_sched_event"
                 " ON workflow_schedules(status, event_scope)")


# ---- 身份时效（P1-2：每次触发重新解析，不存永久快照）----------------------


def _context_identity() -> usage.Identity:
    return orch._identity_from_context()


def _resolve_creator(creator: str) -> tuple[dict | None, str]:
    """重解析创建者当前有效身份：账号存在、未吊销、未过期、角色满足 Q3。"""
    if not auth.enabled():
        # 开放模式无账号体系：按 admin 口径放行（登记入口本身就无 RBAC）
        return {"username": creator or "anonymous", "role": "admin",
                "department": ""}, ""
    user = auth.user_by_name(creator)
    if user is None:
        return None, "创建者身份失效（账号不存在或已吊销）"
    expires = user.get("expires_at")
    if isinstance(expires, (int, float)) and expires <= time.time():
        return None, "创建者账号已过期"
    if user.get("role") not in ("approver", "admin"):
        return None, "创建者角色不满足（需 approver/admin）"
    return {"username": creator, "role": user["role"],
            "department": user.get("department", "")}, ""


def _definition_dept_ok(definition: dict, user: dict) -> tuple[bool, str]:
    """创建者调岗后，定义引用的包工具部门白名单仍须满足。"""
    meta = orch._pack_tool_meta()
    role, department = user["role"], user.get("department", "")
    for r in definition.get("roles") or []:
        if not isinstance(r, dict):
            continue
        for tool_name in r.get("tools") or []:
            pack = meta.get(tool_name)
            if not isinstance(pack, dict):
                continue
            denied = custom_tools.pack_scope_denied(
                pack.get("name", ""), pack.get("allowed_roles") or [],
                pack.get("allowed_departments") or [],
                role=role, department=department)
            if denied:
                return False, f"部门权限不满足：{denied}"
    return True, ""


def _load_for_claim(schedule: dict, user: dict) -> tuple[dict | None, str]:
    """claim 时校验定义可加载、登记参数仍合法。"""
    identity = usage.Identity(username=user["username"], role=user["role"],
                              department=user.get("department", ""),
                              auth_source="local")
    resolved = orch.resolve_definition(schedule["definition_name"], identity)
    if resolved["status"] != "ok":
        return None, f"定义不存在或已失效：{schedule['definition_name']}"
    loaded = orch.load_definition(resolved["path"])
    if loaded["status"] != "ok":
        return None, f"定义校验失败：{loaded['error']}"
    definition = loaded["definition"]
    try:
        params = json.loads(schedule["params_json"])
    except (ValueError, TypeError):
        params = {}
    values, perr = orch._resolve_trigger_params(definition, params)
    if perr:
        return None, f"登记参数不合法：{perr}"
    return {"definition": definition, "params": values}, ""


def _event_vars(payload: dict | None) -> dict:
    """上传事件白名单变量（不含正文/preview/绝对路径）。"""
    if not payload:
        return {}
    rel = str(payload.get("rel_path", "") or "")
    return {"event_path": f"uploads/{rel}" if rel else "",
            "event_id": str(payload.get("event_id", "") or ""),
            "event_uploader": str(payload.get("uploader", "") or "")}


def _job(schedule: dict, params: dict, trigger_id: str, source: str,
         planned_at: float, event: dict | None = None) -> dict:
    return {"schedule_id": schedule["schedule_id"],
            "definition_name": schedule["definition_name"],
            "params": params,
            "creator": schedule["creator"],
            "trigger_source": source, "trigger_id": trigger_id,
            "planned_at": planned_at,
            "event": event or {}}


# ---- 登记 / 删除 / 列举（管理工具核心）------------------------------------


def create_schedule(definition_name: str, interval_seconds: int = 0,
                    params: dict | None = None, event_scope: str = "",
                    name_filter: str = "", now: float | None = None) -> dict:
    """登记调度：interval 定时和/或文件上传事件（精确 scope 绑定）。

    Q3：仅 approver/admin 可登记（开放模式放行）。登记要求定义含
    task_template（自动触发无对话任务）。
    """
    if now is None:
        now = time.time()
    identity = _context_identity()
    if auth.enabled() and identity.role not in ("approver", "admin"):
        audit.log(identity.username or "", identity.role, "调度拒绝",
                  target=definition_name, detail="仅 approver/admin 可登记调度")
        return {"status": "error", "error": "仅 approver/admin 可登记调度"}

    # interval 契约：整数、非负、下限 60
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        return {"status": "error", "error": "interval_seconds 必须为整数"}
    if interval_seconds < 0:
        return {"status": "error", "error": "interval_seconds 必须为非负整数"}
    if interval_seconds and interval_seconds < MIN_INTERVAL_SECONDS:
        return {"status": "error",
                "error": f"interval_seconds 不得低于 {MIN_INTERVAL_SECONDS} 秒"}
    if not interval_seconds and not event_scope:
        return {"status": "error",
                "error": "需要有效 interval_seconds 或事件绑定（event_scope）"}
    scope_clean = ""
    if event_scope:
        # 精确绑定：登记必须传规范 scope 串（清洗后与输入不一致即拒绝，
        # 防止「../etc」类输入被静默归一成他人域）
        scope_clean = config.scope_segment(event_scope)
        if not event_scope.strip() or scope_clean != event_scope.strip():
            return {"status": "error", "error": "event_scope 不合法"}

    resolved = orch.resolve_definition(definition_name, identity)
    if resolved["status"] != "ok":
        return {"status": "error",
                "error": f"编排定义「{definition_name}」不存在或无权限"}
    loaded = orch.load_definition(resolved["path"])
    if loaded["status"] != "ok":
        return {"status": "error", "error": f"定义校验失败：{loaded['error']}"}
    definition = loaded["definition"]
    template = definition.get("task_template")
    if not (isinstance(template, str) and template.strip()):
        return {"status": "error",
                "error": "定义缺少 task_template，无法登记自动触发"}
    values, perr = orch._resolve_trigger_params(definition, params)
    if perr:
        return {"status": "error", "error": perr}

    schedule_id = "wfs-" + secrets.token_hex(6)
    next_run = now + interval_seconds if interval_seconds else None
    # 存登记时的原始参数（默认值在 claim 时按当前定义重解析：改参致登记
    # 失效的语义由 claim 留痕承接）
    try:
        with _SCHED_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute(
                    "INSERT INTO workflow_schedules (schedule_id,"
                    " definition_name, params_json, interval_seconds,"
                    " event_scope, name_filter, status, creator,"
                    " creator_role, creator_department, next_run, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (schedule_id, definition_name,
                     json.dumps(dict(params or {}), ensure_ascii=False,
                                default=str),
                     interval_seconds, scope_clean, name_filter, "active",
                     identity.username or "anonymous",
                     identity.role or "", identity.department or "",
                     next_run, now))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] 调度登记写库失败（%s）", type(e).__name__)
        return {"status": "error", "error": "调度登记失败，请稍后重试"}
    audit.log(identity.username or "anonymous", identity.role or "",
              "登记调度", target=schedule_id, detail=definition_name)
    return {"status": "ok", "schedule_id": schedule_id, "next_run": next_run,
            "next_run_text": _fmt_ts(next_run) if next_run else "",
            "event_scope": scope_clean, "name_filter": name_filter}


def delete_schedule(schedule_id: str) -> dict:
    """删除调度：只停未来触发，在途实例自然结束（契约见 spec AC1）。"""
    identity = _context_identity()
    try:
        with _SCHED_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                row = conn.execute(
                    "SELECT * FROM workflow_schedules WHERE schedule_id = ?",
                    (schedule_id,)).fetchone()
                if row is None:
                    return {"status": "error", "error": "调度不存在"}
                if auth.enabled() and identity.role != "admin" \
                        and identity.username != row["creator"]:
                    audit.log(identity.username or "", identity.role,
                              "调度拒绝", target=schedule_id,
                              detail="仅创建者本人或 admin 可删除调度")
                    return {"status": "error",
                            "error": "仅创建者本人或 admin 可删除调度"}
                conn.execute(
                    "UPDATE workflow_schedules SET status = 'deleted'"
                    " WHERE schedule_id = ?", (schedule_id,))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] 调度删除写库失败（%s）", type(e).__name__)
        return {"status": "error", "error": "调度删除失败，请稍后重试"}
    audit.log(identity.username or "anonymous", identity.role or "",
              "删除调度", target=schedule_id, detail=row["definition_name"])
    return {"status": "ok"}


def _visible_schedules(identity: usage.Identity) -> list[dict]:
    try:
        with _SCHED_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                rows = conn.execute(
                    "SELECT * FROM workflow_schedules"
                    " ORDER BY created_at").fetchall()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] 调度查询失败（%s）", type(e).__name__)
        return []
    out = []
    for r in rows:
        # 可见性同 4.1 口径：user 仅本人；approver/admin 全部
        if auth.enabled() and identity.role == "user" \
                and r["creator"] != identity.username:
            continue
        out.append(dict(r))
    return out


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts or "")


def _params_summary(params_json: str) -> str:
    try:
        data = json.loads(params_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return ";".join(f"{k}={data[k]}" for k in sorted(data))[:200]


def list_schedules() -> str:
    """列举当前身份可见的调度登记（触发方式、参数摘要、创建者、状态）。"""
    identity = _context_identity()
    rows = _visible_schedules(identity)
    if not rows:
        return "当前没有调度登记（可用 create_schedule 工具登记）。"
    lines = []
    for r in rows:
        kind = ""
        if r["interval_seconds"]:
            kind = (f"周期 {r['interval_seconds']}s，"
                    f"下次触发 {_fmt_ts(r['next_run'])}")
        if r["event_scope"]:
            extra = f"（过滤：{r['name_filter']}）" if r["name_filter"] else ""
            kind = f"{kind}；" if kind else ""
            kind += f"事件：文件上传至 {r['event_scope']}{extra}"
        lines.append(f"- {r['schedule_id']} {r['definition_name']}（{kind}）"
                     f" 参数：{_params_summary(r['params_json'])}"
                     f" 创建者：{r['creator']} 状态：{r['status']}")
    return "\n".join(lines)


def list_trigger_attempts(schedule_id: str = "", limit: int = 20) -> str:
    """查询触发历史：每次尝试（triggered/skipped/denied）+ 原因 + 时间。

    跳过/失效原因不只存在日志，可经工具查询（spec AC4）。
    """
    identity = _context_identity()
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    try:
        with _SCHED_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                sql = ("SELECT * FROM workflow_trigger_attempts"
                       " WHERE schedule_id LIKE ?")
                args: list = [f"%{schedule_id}" if schedule_id else "%"]
                if auth.enabled() and identity.role == "user":
                    sql += " AND creator = ?"
                    args.append(identity.username or "")
                sql += " ORDER BY created_at DESC LIMIT ?"
                args.append(limit)
                rows = conn.execute(sql, args).fetchall()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] 触发历史查询失败（%s）", type(e).__name__)
        return "触发历史查询失败，请稍后重试"
    if not rows:
        return "暂无触发历史。"
    lines = []
    for r in rows:
        line = (f"- {r['attempt_id']} [{r['result']}] {r['definition_name']}"
                f" 调度：{r['schedule_id']}"
                f" {_fmt_ts(r['created_at'])}")
        if r["outcome"]:
            line += f" 终态：{r['outcome']}"
        if r["reason"]:
            line += f" 原因：{r['reason']}"
        lines.append(line)
    return "\n".join(lines)


# ---- claim 核心（同步 claim-only）------------------------------------------


def _insert_attempt(conn: sqlite3.Connection, schedule: dict, trigger_id: str,
                    source: str, result: str, reason: str,
                    payload: dict, at: float) -> bool:
    """写触发尝试留痕；唯一键冲突（幂等重放）返回 False。"""
    attempt_id = "wft-" + secrets.token_hex(6)
    before = conn.total_changes
    conn.execute(
        "INSERT OR IGNORE INTO workflow_trigger_attempts (attempt_id,"
        " schedule_id, trigger_id, trigger_source, definition_name, creator,"
        " result, reason, payload_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, schedule["schedule_id"], trigger_id, source,
         schedule["definition_name"], schedule["creator"], result, reason,
         json.dumps(payload, ensure_ascii=False, default=str), at))
    inserted = conn.total_changes > before
    if inserted:
        _trim_attempts(conn)
    return inserted


def _trim_attempts(conn: sqlite3.Connection) -> None:
    """触发尝试超容量裁最旧（同事务内执行，保留 floor(CAPACITY*RATIO)）。"""
    (total,) = conn.execute(
        "SELECT COUNT(*) FROM workflow_trigger_attempts").fetchone()
    if total > ATTEMPT_CAPACITY:
        keep = int(ATTEMPT_CAPACITY * ATTEMPT_TRIM_RATIO)
        conn.execute(
            "DELETE FROM workflow_trigger_attempts WHERE rowid IN"
            " (SELECT rowid FROM workflow_trigger_attempts"
            " ORDER BY rowid ASC LIMIT ?)", (total - keep,))


def _claim_common(conn: sqlite3.Connection, schedule: dict, trigger_id: str,
                  source: str, at: float,
                  event_payload: dict | None = None) -> dict | None:
    """claim 事务内的公共路径：身份重解析 → 留痕 → 组装作业。

    留痕载荷仅事件白名单字段（定时触发留空载荷，计划时间在 created_at）；
    返回 None 表示拒绝/跳过/幂等重放（不执行）。
    """
    user, uerr = _resolve_creator(schedule["creator"])
    if user is None:
        _insert_attempt(conn, schedule, trigger_id, source, "denied",
                        uerr, event_payload or {}, at)
        return None
    loaded, derr = _load_for_claim(schedule, user)
    if loaded is None:
        _insert_attempt(conn, schedule, trigger_id, source, "skipped",
                        derr, event_payload or {}, at)
        return None
    ok, derr = _definition_dept_ok(loaded["definition"], user)
    if not ok:
        # 身份时效类拒绝（P1-2）：调岗后部门白名单不满足
        _insert_attempt(conn, schedule, trigger_id, source, "denied",
                        derr, event_payload or {}, at)
        return None
    if STATE.llm is None:
        # Mock 模式无真实模型：claim 前即跳过，留痕与执行结果一致
        _insert_attempt(conn, schedule, trigger_id, source, "skipped",
                        "需要真实模型支持（当前为 Mock 模式）",
                        event_payload or {}, at)
        return None
    if not _insert_attempt(conn, schedule, trigger_id, source, "triggered",
                           "", event_payload or {}, at):
        return None  # 幂等：该触发键已留痕（事件重复派发）
    return _job(schedule, loaded["params"], trigger_id, source, at,
                _event_vars(event_payload))


def process_due(now: float | None = None) -> list[dict]:
    """到点 claim（同步 claim-only）：返回待执行作业，不执行编排。

    单事务（BEGIN IMMEDIATE）完成到点判断、claim、next_run 推进与
    留痕；next_run 按周期推进到未来（长任务跨周期不追赶）。
    """
    if now is None:
        now = time.time()
    jobs: list[dict] = []
    with _SCHED_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM workflow_schedules WHERE status = 'active'"
                " AND interval_seconds > 0 AND next_run IS NOT NULL"
                " AND next_run <= ?", (now,)).fetchall()
            for row in rows:
                schedule = dict(row)
                planned = schedule["next_run"]
                nxt = planned
                while nxt <= now:  # 跨周期推进到未来，不追赶
                    nxt += schedule["interval_seconds"]
                trigger_id = f"timer-{schedule['schedule_id']}-{planned}"
                conn.execute(
                    "UPDATE workflow_schedules SET next_run = ?,"
                    " claimed_at = ? WHERE schedule_id = ?",
                    (nxt, now, schedule["schedule_id"]))
                job = _claim_common(conn, schedule, trigger_id, "scheduled",
                                    planned)
                if job is not None:
                    jobs.append(job)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return jobs


def on_file_uploaded(*, event_id: str, rel_path: str, uploader: str,
                     scope: str, chars: int,
                     occurred_at: float | None = None) -> list[dict]:
    """文件上传成功事件钩（同步 claim-only）。

    精确 scope 绑定 + 可选文件名过滤；（事件 ID × 调度）唯一触发键幂等；
    载荷白名单仅入留痕（无 preview/全文/绝对路径）。
    """
    if occurred_at is None:
        occurred_at = time.time()
    jobs: list[dict] = []
    with _SCHED_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM workflow_schedules WHERE status = 'active'"
                " AND event_scope != '' AND event_scope = ?",
                (scope,)).fetchall()
            fname = Path(rel_path).name
            for row in rows:
                schedule = dict(row)
                if schedule["name_filter"] and not fnmatch.fnmatch(
                        fname, schedule["name_filter"]):
                    continue
                payload = {"event_id": event_id, "rel_path": rel_path,
                           "uploader": uploader, "scope": scope,
                           "chars": chars, "occurred_at": occurred_at}
                job = _claim_common(conn, schedule, event_id, "event",
                                    occurred_at, payload)
                if job is not None:
                    jobs.append(job)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return jobs


# ---- 执行派发与生命周期 ----------------------------------------------------


def _task_done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[scheduler] 触发任务失败（%s）：%s",
                       type(exc).__name__, exc)


def _dispatch(job: dict) -> asyncio.Task:
    task = asyncio.create_task(execute_job(job))
    _TASKS.add(task)
    task.add_done_callback(_task_done)
    return task


def _acquire_run(schedule_id: str, definition_name: str, trigger_id: str,
                 now: float) -> bool:
    """领取定义级运行租约（P1-1）：同名定义同一时刻只允许一轮执行。

    租约落在调度行，按 definition_name 互斥——不同调度引用同一定义同样
    互斥；超过 STALE_RUNNING_GRACE 的租约视为进程遗留，可被接手。
    """
    with _SCHED_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT running_trigger_id FROM workflow_schedules"
                " WHERE definition_name = ? AND running_since IS NOT NULL"
                " AND running_since > ? LIMIT 1",
                (definition_name, now - STALE_RUNNING_GRACE)).fetchone()
            if row is not None and row["running_trigger_id"] != trigger_id:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE workflow_schedules SET running_since = ?,"
                " running_trigger_id = ? WHERE schedule_id = ?",
                (now, trigger_id, schedule_id))
            conn.commit()
            return True
        finally:
            conn.close()


def _release_run(schedule_id: str, trigger_id: str) -> None:
    """释放运行租约；仅当租约仍属本次触发时清空（防误清他轮）。"""
    try:
        with _SCHED_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute(
                    "UPDATE workflow_schedules SET running_since = NULL,"
                    " running_trigger_id = '' WHERE schedule_id = ?"
                    " AND running_trigger_id = ?", (schedule_id, trigger_id))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] 运行租约释放失败（%s）", type(e).__name__)


def _finish_attempt(schedule_id: str, trigger_id: str, outcome: str,
                    reason: str = "") -> None:
    """回写触发尝试终态（P2-3）：completed/failed/cancelled/skipped/denied。

    只回写尚未终结（outcome 为空）的尝试，避免覆盖启动清扫的 abandoned。
    """
    try:
        with _SCHED_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute(
                    "UPDATE workflow_trigger_attempts SET outcome = ?,"
                    " reason = ?, finished_at = ? WHERE schedule_id = ?"
                    " AND trigger_id = ? AND outcome = ''",
                    (outcome, reason, time.time(), schedule_id, trigger_id))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] 触发尝试终态回写失败（%s）", type(e).__name__)


async def execute_job(job: dict) -> str:
    """执行一个已 claim 的触发作业。

    顺序：身份重解析 → 定义级运行租约（互斥）→ 编排执行 → 尝试终态回写
    → finally 释放租约。停机取消时补偿标记实例 failed、回写 cancelled 后
    重新抛出，保持任务取消语义。
    """
    schedule_id = job.get("schedule_id", "")
    trigger_id = job.get("trigger_id", "")
    user, uerr = _resolve_creator(job.get("creator", ""))
    if user is None:
        logger.warning("[scheduler] 触发拒绝（%s）：%s", schedule_id, uerr)
        _finish_attempt(schedule_id, trigger_id, "denied", uerr)
        return f"自动触发未执行：{uerr}"
    if not _acquire_run(schedule_id, job.get("definition_name", ""),
                        trigger_id, time.time()):
        reason = "运行中互斥：同名流程上一轮仍在执行，本次跳过"
        logger.warning("[scheduler] 触发跳过（%s）：%s", schedule_id, reason)
        _finish_attempt(schedule_id, trigger_id, "skipped", reason)
        return "自动触发未执行：同名流程上一轮仍在执行（运行中互斥）"
    identity = usage.Identity(username=user["username"], role=user["role"],
                              department=user.get("department", ""),
                              auth_source="local")
    try:
        out = await orch.run_triggered(
            job["definition_name"], job.get("params") or {}, identity,
            trigger_source=job.get("trigger_source", "scheduled"),
            schedule_id=schedule_id, trigger_id=trigger_id,
            planned_at=job.get("planned_at"),
            event=job.get("event") or None)
        status = orch.instance_status_for_trigger(trigger_id)
        if status == "completed" or (not status and
                                     out.startswith("多角色编排完成")):
            _finish_attempt(schedule_id, trigger_id, "completed")
        else:
            _finish_attempt(schedule_id, trigger_id, "failed",
                            f"执行失败（实例状态：{status or '未知'}）")
        return out
    except asyncio.CancelledError:
        orch.abort_instance_for_trigger(trigger_id, "停机取消")
        _finish_attempt(schedule_id, trigger_id, "cancelled", "停机取消")
        raise
    except Exception as e:
        logger.error("[scheduler] 自动触发执行失败（%s）", type(e).__name__)
        _finish_attempt(schedule_id, trigger_id, "failed",
                        f"执行异常（{type(e).__name__}）")
        return f"自动触发执行失败（{type(e).__name__}）"
    finally:
        _release_run(schedule_id, trigger_id)


async def dispatch_upload_event(*, rel_path: str, uploader: str,
                                scope: str, chars: int) -> None:
    """上传端点事件挂钩入口：白名单载荷、自界异常隔离（旁路不阻塞）。"""
    event_id = "evt-" + secrets.token_hex(6)
    try:
        jobs = await asyncio.to_thread(
            on_file_uploaded, event_id=event_id, rel_path=rel_path,
            uploader=uploader, scope=scope, chars=chars,
            occurred_at=time.time())
    except Exception as e:
        logger.warning("[scheduler] 上传事件处理失败（%s），上传不受影响",
                       type(e).__name__)
        return
    for job in jobs:
        try:
            _dispatch(job)
        except Exception as e:
            # 逐作业隔离（P2-4）：单个派发失败不影响其余命中调度
            logger.warning("[scheduler] 触发作业派发失败（%s），其余继续",
                           type(e).__name__)


def sweep_stale_running(now: float | None = None,
                        grace: float = STALE_RUNNING_GRACE) -> int:
    """启动清扫：超龄 running 实例标 failed（进程重启中断）；清空运行租约，
    并把上一进程未回写终态的 triggered 尝试标 abandoned。"""
    if now is None:
        now = time.time()
    n = 0
    try:
        with orch._WF_LOCK:
            conn = orch._wf_connect()
            try:
                orch._wf_ensure_schema(conn)
                cur = conn.execute(
                    "UPDATE workflow_instances SET status = 'failed',"
                    " error = '进程重启中断', finished_at = ?"
                    " WHERE status = 'running' AND started_at <= ?",
                    (now, now - grace))
                n = cur.rowcount
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] stale 实例清扫失败（%s）", type(e).__name__)
    try:
        with _SCHED_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute(
                    "UPDATE workflow_schedules SET running_since = NULL,"
                    " running_trigger_id = '' WHERE running_since IS NOT NULL")
                conn.execute(
                    "UPDATE workflow_trigger_attempts SET outcome = 'abandoned',"
                    " reason = '进程重启中断，执行结果未知', finished_at = ?"
                    " WHERE result = 'triggered' AND outcome = ''", (now,))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.error("[scheduler] 启动清扫调度侧失败（%s）", type(e).__name__)
    return n


# ---- 调度循环（lifespan 宿主，沿 _approval_scan_loop 先例）------------------

_TASKS: set[asyncio.Task] = set()
_LOOP_TASK: asyncio.Task | None = None


def multi_worker() -> bool:
    """多 worker 部署检测（uvicorn --workers 传导 WEB_CONCURRENCY）。"""
    raw = os.environ.get("WEB_CONCURRENCY", "")
    try:
        return int(raw) > 1
    except (TypeError, ValueError):
        return False


async def scheduler_loop(interval: float = SCHED_LOOP_INTERVAL) -> None:
    """调度循环壳：to_thread claim + create_task 派发；单轮异常不死亡。"""
    while True:
        await asyncio.sleep(interval)
        try:
            jobs = await asyncio.to_thread(process_due, time.time())
            for job in jobs:
                _dispatch(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[scheduler] 单轮触发失败（%s，下轮重试）：%s",
                           type(e).__name__, e)


def start_loop(interval: float = SCHED_LOOP_INTERVAL) -> None:
    """启动调度循环（lifespan 调用；需在事件循环内）。

    fail-closed 守护：多 worker 拒启 + WARNING；同进程二次启动拒绝。
    进程内其他存活 app 实例（异事件循环，测试嵌套场景）已持有循环时，
    本实例跳过启动并 WARNING——进程内始终保持单个调度循环。
    """
    global _LOOP_TASK
    if multi_worker():
        logger.warning(
            "[scheduler] 检测到多 worker 部署（WEB_CONCURRENCY>1），调度循环"
            "拒绝启动——多进程会重复触发定时流程；请以单进程运行服务。")
        return
    if _LOOP_TASK is not None and not _LOOP_TASK.done():
        loop = _LOOP_TASK.get_loop()
        if loop.is_closed():
            pass  # 宿主循环已关的残留任务（上个 lifespan 未走 shutdown）视为过期
        elif loop is asyncio.get_running_loop():
            raise RuntimeError("调度循环已在本进程启动，拒绝二次启动")
        else:
            logger.warning("[scheduler] 进程内其他 app 实例已持有调度循环，"
                           "本实例跳过启动（进程内单循环约定）")
            return
    _LOOP_TASK = asyncio.create_task(scheduler_loop(interval))


async def stop_loop() -> None:
    """停机：取消并等待本事件循环的调度循环与在途任务（无未观察异常）。

    异循环持有的任务（嵌套实例场景）不动——由其宿主 lifespan 负责。
    """
    global _LOOP_TASK
    loop = asyncio.get_running_loop()
    task = _LOOP_TASK
    if task is not None and task.get_loop() is loop:
        _LOOP_TASK = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    mine = [t for t in list(_TASKS) if t.get_loop() is loop]
    for t in mine:
        t.cancel()
    if mine:
        await asyncio.gather(*mine, return_exceptions=True)
    for t in mine:
        _TASKS.discard(t)
