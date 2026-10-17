"""Token 计量：按 用户 / 任务（单次运行）/ 会话 三个维度记录与聚合。

采集原理：
- ChatOpenAI 开启 stream_usage=True 后，每次模型调用的结束事件携带
  usage_metadata（prompt_tokens / completion_tokens / total_tokens）
- UsageCallback 挂在 LLM 对象上，主 Agent 与多 Agent worker 的每次调用都会触发
- stream_reply 开头用 contextvars 设置 (user_id, session_id, run_id)，
  回调读取上下文完成归属（asyncio 任务创建时复制上下文，嵌套调用同样生效）

存储：SQLite（USAGE_DB，默认 data/usage.db 运行时目录），私有化部署零额外组件；
生产可替换为 Postgres（表结构见 _ensure_schema）。

金额折算：MODEL_PRICES 配置模型单价（元/百万 tokens，输入/输出分开），落账时按
当时单价折算 prompt_cost / completion_cost 并固化（价格调整只影响新记录）；
缺单价或配置非法的模型仅记 token（WARNING 提示），绝不因折算阻塞落账。

维度定义：
- 用户 user_id：请求方标识（API 认证落地前由调用方传入，缺省 anonymous）
- 任务 run_id：一次 /api/chat 请求的完整运行（可能含多轮模型调用与 worker 子调用）
- 会话 session_id：同一对话的多次运行累计
"""
import contextvars
import csv
import io
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# 请求上下文（stream_reply 设置，UsageCallback 读取）
current_user = contextvars.ContextVar("usage_user", default="anonymous")
current_session = contextvars.ContextVar("usage_session", default="")
current_run = contextvars.ContextVar("usage_run", default="")

_lock = threading.Lock()

# 时钟挂点（单次模型调用计时，monotonic 不受系统跳变影响；测试可替换）
_mono = time.monotonic


def parse_model_prices(raw: str) -> dict:
    """解析 MODEL_PRICES（JSON：{"模型名": {"input": 输入单价, "output": 输出单价}}，
    单位 元/百万 tokens）。空串 = 未配置；非法 JSON / 非对象 / 条目不合法时
    WARNING 并跳过——宁可不折算，不折错。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("[usage] MODEL_PRICES 不是合法 JSON，金额折算停用（token 照常记录）")
        return {}
    if not isinstance(data, dict):
        logger.warning("[usage] MODEL_PRICES 需为 {模型名: {input, output}} JSON 对象，"
                       "金额折算停用（token 照常记录）")
        return {}
    prices = {}
    for name, item in data.items():
        def _num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        if (not name.strip() or not isinstance(item, dict)
                or not _num(item.get("input")) or not _num(item.get("output"))
                or item["input"] < 0 or item["output"] < 0):
            logger.warning("[usage] MODEL_PRICES 条目非法（需非负 input/output 单价），"
                           "模型 %s 不折算（token 照常记录）", name)
            continue
        prices[name] = {"input": float(item["input"]), "output": float(item["output"])}
    return prices


# 进程内价格表：启动时解析固化，价格调整重启生效（历史账按落账时单价固化）
_PRICES: dict = parse_model_prices(config.MODEL_PRICES)


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
            total_tokens INTEGER NOT NULL,
            prompt_cost REAL NOT NULL DEFAULT 0,
            completion_cost REAL NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok'
        )""")
    # 老库迁移：无金额列则补齐（幂等；历史行按默认 0 计入，token 数据不动）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_records)")}
    if cols and "prompt_cost" not in cols:
        conn.execute("ALTER TABLE usage_records ADD COLUMN prompt_cost REAL NOT NULL DEFAULT 0")
    if cols and "completion_cost" not in cols:
        conn.execute("ALTER TABLE usage_records ADD COLUMN completion_cost REAL NOT NULL DEFAULT 0")
    # 老库迁移：无健康度列则补齐（v0.17.22 PLAN 6.6；历史行 0/'ok' 聚合不破）
    if cols and "duration_ms" not in cols:
        conn.execute("ALTER TABLE usage_records ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0")
    if cols and "status" not in cols:
        conn.execute("ALTER TABLE usage_records ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_run ON usage_records(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_records(date)")


def record(model: str, usage: dict, source: str = "main",
           duration_ms: int = 0, status: str = "ok") -> None:
    """记录一次模型调用的 token 消耗、金额折算与健康度（归属取自 contextvars）。
    status='ok' 且 usage 为空/全零（提供方未返回 usage）时跳过；失败行（status='error'）
    强制落库（token/金额 0），否则失败无法计数；缺单价模型金额记 0。"""
    pt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    ct = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    tt = int(usage.get("total_tokens") or pt + ct)
    if tt <= 0 and status != "error":
        return
    now = time.time()
    resolved_model = model or config.MODEL_NAME
    price = _PRICES.get(resolved_model)
    # 单位 元/百万 tokens；落账时折算并固化，round 6 位防浮点噪声累积
    prompt_cost = round(pt / 1e6 * price["input"], 6) if price else 0.0
    completion_cost = round(ct / 1e6 * price["output"], 6) if price else 0.0
    row = (now, time.strftime("%Y-%m-%d", time.localtime(now)),
           current_user.get(), current_session.get(), current_run.get(),
           resolved_model, source, pt, ct, tt, prompt_cost, completion_cost,
           max(0, int(duration_ms or 0)), "error" if status == "error" else "ok")
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO usage_records (ts, date, user_id, session_id, run_id,"
                " model, source, prompt_tokens, completion_tokens, total_tokens,"
                " prompt_cost, completion_cost, duration_ms, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            conn.commit()
        finally:
            conn.close()


class UsageCallback:
    """LangChain 回调：每次 LLM 调用提取 usage、单次调用时长与成败状态落账。
    挂在 ChatOpenAI 上，主 Agent 与多 Agent worker（共用 _llm）全覆盖。
    计时按回调 run_id 配对 on_llm_start/on_llm_end（多 Agent worker 并行时
    contextvar 单值会互相覆盖，run_id 是每次调用的天然配对键）；
    任何回调内异常吞掉，计量失败绝不阻塞主链路。"""

    def __init__(self):
        from langchain_core.callbacks import BaseCallbackHandler

        starts: dict = {}  # run_id → monotonic 起始时刻（end/error 后弹出，无泄漏）

        class _Handler(BaseCallbackHandler):
            def on_llm_start(self, serialized, prompts, **kwargs):
                try:
                    rid = str(kwargs.get("run_id") or "")
                    if rid:
                        with _lock:
                            starts[rid] = _mono()
                except Exception:
                    pass  # 计时失败不影响落账与主链路

            def on_llm_end(self, response, **kwargs):
                try:
                    rid = str(kwargs.get("run_id") or "")
                    with _lock:
                        st = starts.pop(rid, None)
                    dur = int((_mono() - st) * 1000) if st is not None else 0
                    msg = response.generations[0][0].message
                    u = getattr(msg, "usage_metadata", None) or {}
                    model = (getattr(msg, "response_metadata", None) or {}).get(
                        "model_name") or config.MODEL_NAME
                    # 多 Agent worker 的调用带 ma_worker 标签时标记来源
                    source = "worker" if "ma_worker" in (kwargs.get("tags") or []) else "main"
                    record(model, u, source, duration_ms=dur)
                except Exception:
                    pass  # 计量失败绝不阻塞主链路

            def on_llm_error(self, error, **kwargs):
                try:
                    rid = str(kwargs.get("run_id") or "")
                    with _lock:
                        st = starts.pop(rid, None)
                    dur = int((_mono() - st) * 1000) if st is not None else 0
                    # 失败时拿不到响应元数据，模型名近似取当前配置
                    source = "worker" if "ma_worker" in (kwargs.get("tags") or []) else "main"
                    record("", {}, source, duration_ms=dur, status="error")
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


_COST_COLS = ("COALESCE(SUM(prompt_cost),0) AS prompt_cost,"
              " COALESCE(SUM(completion_cost),0) AS completion_cost")


def _attach_total_cost(row: dict) -> dict:
    """视图行补 total_cost（明细金额 SUM，round 6 位防浮点噪声）。"""
    row["total_cost"] = round(row.get("prompt_cost", 0.0)
                              + row.get("completion_cost", 0.0), 6)
    return row


def run_usage(run_id: str) -> dict:
    """单次任务（一次 /api/chat 运行）的消耗合计（含金额与健康度）。"""
    rows = _query(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens),0) AS total_tokens,"
        " COALESCE(SUM(duration_ms),0) AS duration_ms,"
        " COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) AS failed_calls,"
        f" {_COST_COLS}"
        " FROM usage_records WHERE run_id = ?", (run_id,))
    row = rows[0] if rows else {"calls": 0, "prompt_tokens": 0,
                                "completion_tokens": 0, "total_tokens": 0,
                                "duration_ms": 0, "failed_calls": 0,
                                "prompt_cost": 0.0, "completion_cost": 0.0}
    return _attach_total_cost(row)


def session_usage(session_id: str) -> dict:
    """一个会话的累计消耗（含金额）。"""
    rows = _query(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens),0) AS total_tokens,"
        f" {_COST_COLS}"
        " FROM usage_records WHERE session_id = ?", (session_id,))
    row = rows[0] if rows else {"calls": 0, "prompt_tokens": 0,
                                "completion_tokens": 0, "total_tokens": 0,
                                "prompt_cost": 0.0, "completion_cost": 0.0}
    return _attach_total_cost(row)


def summary(user_id: str = "", session_id: str = "", days: int = 0) -> dict:
    """聚合视图：总量 + 按用户 / 按会话 / 按任务（run）分组（含金额字段）。
    days>0 时只统计最近 N 天。total_cost 只含已折算部分（缺单价模型 cost=0）。"""
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
        " COALESCE(SUM(total_tokens),0) AS total_tokens,"
        f" {_COST_COLS}"
        f" FROM usage_records{cond}", tuple(params))
    by_user = _query(
        "SELECT user_id, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens,"
        " SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,"
        f" {_COST_COLS}"
        f" FROM usage_records{cond} GROUP BY user_id ORDER BY total_tokens DESC LIMIT 50",
        tuple(params))
    by_session = _query(
        "SELECT session_id, user_id, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens,"
        f" {_COST_COLS}"
        f" FROM usage_records{cond} GROUP BY session_id ORDER BY total_tokens DESC LIMIT 50",
        tuple(params))
    by_run = _query(
        "SELECT run_id, session_id, user_id, date, COUNT(*) AS calls,"
        " SUM(total_tokens) AS total_tokens,"
        " SUM(CASE WHEN source='worker' THEN total_tokens ELSE 0 END) AS worker_tokens,"
        " SUM(duration_ms) AS duration_ms,"
        " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed_calls,"
        f" {_COST_COLS}"
        f" FROM usage_records{cond} GROUP BY run_id ORDER BY MAX(ts) DESC LIMIT 50",
        tuple(params))
    return {"total": _attach_total_cost(total[0]) if total else {},
            "by_user": [_attach_total_cost(r) for r in by_user],
            "by_session": [_attach_total_cost(r) for r in by_session],
            "by_run": [_attach_total_cost(r) for r in by_run]}


# 未设置部门/映射外用户的聚合分组名（v0.17.21 部门报表）
_UNASSIGNED = "未分配"


def department_usage(days: int = 0, department_map: dict = None) -> dict:
    """按部门聚合用量（含金额，v0.17.21 PLAN 6.2）。管理视角视图。

    department_map 为 {user_id: 部门} 映射，由调用方（main.py）从 auth 层注入，
    usage 模块不反向依赖 auth；跨库（usage.db × state.db）无法 SQL join，
    Python 侧按当前映射聚合——调岗即时反映，usage_records 结构零改动。
    无部门/映射外用户计入「未分配」，不丢数据。
    """
    where, params = [], []
    if days > 0:
        where.append("ts >= ?")
        params.append(time.time() - days * 86400)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    rows = _query(
        "SELECT user_id, COUNT(*) AS calls,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens),0) AS total_tokens,"
        f" {_COST_COLS}"
        f" FROM usage_records{cond} GROUP BY user_id", tuple(params))
    mapping = department_map or {}
    grouped = {}
    for r in rows:
        dept = mapping.get(r["user_id"]) or _UNASSIGNED
        d = grouped.setdefault(dept, {"department": dept, "calls": 0,
                                      "prompt_tokens": 0, "completion_tokens": 0,
                                      "total_tokens": 0,
                                      "prompt_cost": 0.0, "completion_cost": 0.0})
        for k in ("calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            d[k] += r[k]
        d["prompt_cost"] += r["prompt_cost"]
        d["completion_cost"] += r["completion_cost"]
    out = [_attach_total_cost(d) for d in grouped.values()]
    out.sort(key=lambda x: x["total_cost"], reverse=True)
    return {"by_department": out}


_CSV_COLUMNS = [("department", "部门"), ("calls", "调用次数"),
                ("prompt_tokens", "输入 tokens"), ("completion_tokens", "输出 tokens"),
                ("total_tokens", "总 tokens"), ("prompt_cost", "输入金额（元）"),
                ("completion_cost", "输出金额（元）"), ("total_cost", "总金额（元）")]


def department_csv(days: int = 0, department_map: dict = None) -> tuple:
    """部门报表 CSV 导出（v0.17.21）：返回 (csv 文本, 建议文件名)。
    UTF-8 带 BOM（Excel 直开中文不乱码）；文件名含生成日期，服务端固定模式
    生成（不含用户输入，无路径注入面）。只读查询，不影响计量落账。"""
    view = department_usage(days=days, department_map=department_map)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in _CSV_COLUMNS])
    for row in view["by_department"]:
        writer.writerow([row[key] for key, _ in _CSV_COLUMNS])
    date_tag = time.strftime("%Y%m%d")
    return "\ufeff" + buf.getvalue(), f"usage-departments-{date_tag}.csv"
