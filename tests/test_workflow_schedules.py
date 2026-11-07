"""4.2 定时/事件触发：调度登记、到点/事件触发、防重、身份时效、留痕
（spec docs/specs/4.2-scheduled-triggers.md AC1-AC12；AC13 为回归项）。

红测试口径：server/schedulers.py 为 v0.17.49 新增模块——实现前
import 报 ModuleNotFoundError（结构性红，惰性导入保证逐用例留痕）；
tools_admin 调度工具不存在（AttributeError）；FORBIDDEN_TOOL_NAMES
未补登记类工具名（断言红）；上传端点未挂事件钩（捕获断言红）。
全部用例注入时钟（create_schedule(now=...)/process_due(now)），零真实等待。
"""
import asyncio
import io
import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from server import audit, auth, config, orchestration as orch, state, usage

# ---------------------------------------------------------------- 基础助手


def _sched():
    """惰性导入：红态逐用例报 ModuleNotFoundError（结构性红）。"""
    from server import schedulers
    return schedulers


@pytest.fixture(autouse=True)
def _reset_loop_state():
    """复位调度循环模块级状态：防本文件用例间/向前泄漏（嵌套实例场景）。"""
    yield
    try:
        from server import schedulers
        schedulers._LOOP_TASK = None
        schedulers._TASKS.clear()
    except ImportError:
        pass  # 红态


def _identity(username, role="approver", department=""):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _as(username, role="approver", department=""):
    ident = _identity(username, role, department)
    usage.current_identity.set(ident)
    usage.current_user.set(username)
    return ident


class _CtxLLM:
    """直连模式假模型：记录调用时的身份/会话上下文（计量归属断言用）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen_messages = []
        self.ctx = []

    async def ainvoke(self, messages, config=None):
        self.seen_messages.append(messages)
        self.ctx.append((usage.current_user.get(), usage.current_session.get()))
        return AIMessage(content=self.replies.pop(0))


def _prepare_llm(monkeypatch, replies=("步骤产出",)):
    llm = _CtxLLM(replies)
    monkeypatch.setattr(state.STATE, "llm", llm)
    return llm


def _rows(table, order="created_at"):
    conn = sqlite3.connect(config.STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order}").fetchall()
    except sqlite3.OperationalError:
        return []  # 表尚未建立（红态）
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _schedule_rows():
    return _rows("workflow_schedules")


def _attempt_rows():
    return _rows("workflow_trigger_attempts")


def _instance_rows():
    return _rows("workflow_instances", order="started_at")


def _def_dict(name="sched-demo", tools=None, template="处理主题 {{topic}}"):
    return {
        "name": name, "version": "1", "description": "调度测试定义",
        "params": [
            {"name": "topic", "type": "string", "description": "主题",
             "required": True, "default": ""},
            {"name": "note", "type": "string", "description": "备注",
             "required": False, "default": ""},
        ],
        "task_template": template,
        "roles": [{"id": "r", "prompt": "执行者", "tools": tools or []}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }


def _install_def(name="sched-demo", tools=None, template="处理主题 {{topic}}"):
    orch.save_definition(_def_dict(name, tools, template), scope=None)


def _register_timer(monkeypatch, *, username="carol", role="approver",
                    interval=60, params=None, name="sched-demo",
                    now=1000.0, tools=None, template="处理主题 {{topic}}"):
    """登记一个 interval 调度（创建者须为真实 auth 用户：触发时重查身份）。"""
    auth.add_user(username, role=role)
    _install_def(name, tools, template)
    _as(username, role)
    out = _sched().create_schedule(
        name, interval_seconds=interval,
        params={"topic": "周报"} if params is None else params, now=now)
    assert out["status"] == "ok", out.get("error")
    return out["schedule_id"]


def _register_event(monkeypatch, *, username="root", role="admin",
                    event_scope=None, name_filter="", name="sched-demo"):
    auth.add_user(username, role=role)
    _install_def(name)
    _as(username, role)
    out = _sched().create_schedule(
        name, interval_seconds=0, params={"topic": "周报"},
        event_scope=event_scope or config.user_scope(username),
        name_filter=name_filter, now=1000.0)
    assert out["status"] == "ok", out.get("error")
    return out["schedule_id"]


def _file_event(scope, fname="报告.docx.md", event_id="evt-1", chars=5):
    return {"event_id": event_id, "rel_path": f"{scope}/{fname}",
            "uploader": "root", "scope": scope, "chars": chars,
            "occurred_at": 1234.5}


def _run_job(job):
    return asyncio.run(_sched().execute_job(job))


# ---------------------------------------------------------------- AC1 登记


def test_ac1_schedule_create_success_and_fields(monkeypatch):
    _register_timer(monkeypatch)
    rows = _schedule_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["schedule_id"].startswith("wfs-")
    assert r["definition_name"] == "sched-demo"
    assert r["interval_seconds"] == 60
    assert r["event_scope"] == ""
    assert r["status"] == "active"
    assert r["creator"] == "carol"
    assert r["next_run"] == 1060.0  # 首次到点 = 登记 + interval
    assert json.loads(r["params_json"]) == {"topic": "周报"}
    # 登记记审计（who/what）
    recs = [x for x in audit.query(action="登记调度", limit=10)]
    assert any(x["actor"] == "carol" for x in recs)
    # 列举可见：id/定义/周期/参数摘要/创建者
    _as("carol", "approver")
    listing = _sched().list_schedules()
    assert r["schedule_id"] in listing and "sched-demo" in listing
    assert "60" in listing and "周报" in listing and "carol" in listing


def test_ac1_schedule_create_rejections(monkeypatch):
    auth.add_user("carol", role="approver")
    _install_def("sched-demo")
    _as("carol", "approver")
    S = _sched()

    # interval 生产下限 60（注入时钟不影响下限）
    out = S.create_schedule("sched-demo", interval_seconds=30,
                            params={"topic": "x"}, now=1000.0)
    assert out["status"] == "error" and "60" in out["error"]
    # 非法类型
    out = S.create_schedule("sched-demo", interval_seconds="60",
                            params={"topic": "x"}, now=1000.0)
    assert out["status"] == "error"
    # 定时与事件绑定均缺失
    out = S.create_schedule("sched-demo", interval_seconds=0,
                            params={"topic": "x"}, now=1000.0)
    assert out["status"] == "error" and ("interval" in out["error"]
                                         or "scope" in out["error"])
    # 事件绑定的 scope 不合法
    out = S.create_schedule("sched-demo", interval_seconds=0,
                            params={"topic": "x"}, event_scope="../etc",
                            now=1000.0)
    assert out["status"] == "error"
    # 定义不存在
    out = S.create_schedule("no-such-def", interval_seconds=60,
                            params={"topic": "x"}, now=1000.0)
    assert out["status"] == "error" and "不存在" in out["error"]
    # 定义缺少 task_template（自动触发无对话任务，必须有模板）
    d = _def_dict("no-tmpl")
    d.pop("task_template")
    orch.save_definition(d, scope=None)
    out = S.create_schedule("no-tmpl", interval_seconds=60,
                            params={"topic": "x"}, now=1000.0)
    assert out["status"] == "error" and "task_template" in out["error"]
    # 登记参数不合法（_resolve_trigger_params 口径：缺必填/未知/类型）
    out = S.create_schedule("sched-demo", interval_seconds=60,
                            params={"unknown": 1}, now=1000.0)
    assert out["status"] == "error"
    out = S.create_schedule("sched-demo", interval_seconds=60,
                            params={}, now=1000.0)
    assert out["status"] == "error" and "topic" in out["error"]
    # 全部拒绝都不落表
    assert _schedule_rows() == []


def test_ac1_delete_stops_future_audits_and_inflight_survives(monkeypatch):
    sid = _register_timer(monkeypatch)
    _as("carol", "approver")
    _prepare_llm(monkeypatch)
    S = _sched()
    jobs = S.process_due(1060.0)
    assert len(jobs) == 1  # 删除前到点已 claim（在途）
    out = S.delete_schedule(sid)
    assert out["status"] == "ok", out.get("error")
    # 删除后未来到点不再触发
    assert S.process_due(1120.0) == []
    recs = [x for x in audit.query(action="删除调度", limit=10)]
    assert any(x["actor"] == "carol" and x["target"] == sid for x in recs)
    # 契约：删除只停未来触发，在途实例自然结束
    result = _run_job(jobs[0])
    assert "多角色编排完成" in result
    assert any(r["status"] == "completed" for r in _instance_rows())


# ---------------------------------------------------------------- AC2 定时触发


def test_ac2_timer_fires_at_first_due_and_instance_fields(monkeypatch):
    sid = _register_timer(monkeypatch)
    llm = _prepare_llm(monkeypatch)
    S = _sched()
    assert S.process_due(1000.0) == []          # 登记时刻不到点
    assert S.process_due(1059.9) == []           # 未到点不触发
    jobs = S.process_due(1060.0)                 # 首次到点 = 登记 + interval
    assert len(jobs) == 1
    job = jobs[0]
    assert job["schedule_id"] == sid
    assert job["trigger_source"] == "scheduled"
    assert job["planned_at"] == 1060.0
    assert job["trigger_id"] and job["creator"] == "carol"
    # 模板以登记参数渲染
    result = _run_job(job)
    assert "多角色编排完成" in result
    human = llm.seen_messages[0][1].content
    assert "处理主题 周报" in human
    # 实例行带触发字段
    rows = _instance_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "completed"
    assert r["trigger_source"] == "scheduled"
    assert r["schedule_id"] == sid
    assert r["trigger_id"] == job["trigger_id"]
    assert r["planned_at"] == 1060.0
    assert r["initiator"] == "carol"
    assert json.loads(r["params_json"]) == {"topic": "周报", "note": ""}


def test_ac2_timer_not_due_no_action(monkeypatch):
    _register_timer(monkeypatch)
    S = _sched()
    assert S.process_due(1059.0) == []
    rows = _schedule_rows()
    assert rows[0]["next_run"] == 1060.0        # 未动
    assert _attempt_rows() == []                 # 未到点无触发尝试留痕


# ---------------------------------------------------------------- AC3 事件触发


def test_ac3_event_dispatch_matching_and_payload_whitelist(monkeypatch):
    scope = config.user_scope("root")
    sid = _register_event(monkeypatch, event_scope=scope)
    _prepare_llm(monkeypatch)
    S = _sched()
    # 列举输出事件绑定（含过滤展示）
    _as("root", "admin")
    assert "事件：文件上传至" in S.list_schedules()
    jobs = S.on_file_uploaded(**_file_event(scope))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["schedule_id"] == sid
    assert job["trigger_source"] == "event"
    assert job["creator"] == "root"
    # 载荷白名单：不含 preview/全文/绝对路径
    attempts = _attempt_rows()
    assert len(attempts) == 1
    payload = json.loads(attempts[0]["payload_json"])
    assert set(payload) == {"event_id", "rel_path", "uploader", "scope",
                            "chars", "occurred_at"}
    assert payload["rel_path"] == f"{scope}/报告.docx.md"
    assert str(config.UPLOADS_DIR) not in attempts[0]["payload_json"]
    # 事件触发的执行同样落实例（trigger_source=event）
    result = _run_job(job)
    assert "多角色编排完成" in result
    r = _instance_rows()[0]
    assert r["trigger_source"] == "event" and r["schedule_id"] == sid


def test_ac3_event_no_registration_scope_or_filter_mismatch(monkeypatch):
    scope = config.user_scope("root")
    other = config.user_scope("bob")
    S = _sched()
    # 无登记 → 不触发
    assert S.on_file_uploaded(**_file_event(scope)) == []
    _register_event(monkeypatch, event_scope=scope, name_filter="*.txt")
    # 上传者域不匹配登记 scope → 不执行
    assert S.on_file_uploaded(**_file_event(other)) == []
    # 文件名过滤不中（登记 *.txt，上传 .docx.md）→ 不执行
    assert S.on_file_uploaded(**_file_event(scope)) == []
    assert _attempt_rows() == []


def test_ac3_upload_docx_success_invokes_hook_whitelist(monkeypatch,
                                                         client_factory):
    scope = config.user_scope("root")
    S = _sched()
    captured = []
    monkeypatch.setattr(S, "on_file_uploaded",
                        lambda **kw: captured.append(kw) or [])
    key = auth.add_user("root", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:body><w:p>hello</w:p></w:body>")
    r = c.post("/api/uploads", params={"name": "报告.docx"},
               content=buf.getvalue())
    assert r.status_code == 200
    # 转换成功后挂钩：docx 转换名 .docx.md 入载荷
    assert len(captured) == 1
    kw = captured[0]
    assert set(kw) == {"event_id", "rel_path", "uploader", "scope",
                       "chars", "occurred_at"}      # 白名单，无 preview/全文
    assert kw["rel_path"] == f"{scope}/报告.docx.md"
    assert kw["uploader"] == "root" and kw["chars"] == 5
    assert "preview" not in kw
    assert str(config.UPLOADS_DIR) not in json.dumps(kw, default=str)


def test_ac3_upload_invalid_docx_no_hook(monkeypatch, client_factory):
    S = _sched()
    captured = []
    monkeypatch.setattr(S, "on_file_uploaded",
                        lambda **kw: captured.append(kw) or [])
    key = auth.add_user("root", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    r = c.post("/api/uploads", params={"name": "坏.docx"},
               content=b"not-a-zip")
    assert r.status_code == 400                 # 转换失败 → 不触发
    assert captured == []


def test_ac3_event_dispatch_idempotent(monkeypatch):
    scope = config.user_scope("root")
    _register_event(monkeypatch, event_scope=scope)
    _prepare_llm(monkeypatch)
    S = _sched()
    first = S.on_file_uploaded(**_file_event(scope))
    second = S.on_file_uploaded(**_file_event(scope))  # 同一事件重复派发
    assert len(first) == 1 and second == []
    assert len(_attempt_rows()) == 1           # 事件 × 调度唯一触发键


# ---------------------------------------------------------------- AC4 历史可查


def test_ac4_auto_instances_visible_in_listing(monkeypatch):
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    jobs = S.process_due(1060.0)
    result = _run_job(jobs[0])
    import re
    m = re.search(r"wfi-[0-9a-f]{12}", result)
    assert m, result
    # 创建者（approver）可见，且列举输出带触发来源（D8）
    _as("carol", "approver")
    listing = orch.list_workflow_instances(limit=50)
    assert m.group(0) in listing and "触发：scheduled" in listing
    # 可见性同 4.1：user 仅本人
    _as("bob", "user")
    assert m.group(0) not in orch.list_workflow_instances(limit=50)


def test_ac4_trigger_attempts_queryable(monkeypatch):
    sid = _register_timer(monkeypatch)
    auth.revoke_user("carol")
    S = _sched()
    assert S.process_due(1060.0) == []          # 拒绝执行
    attempts = _attempt_rows()
    assert len(attempts) == 1 and attempts[0]["result"] == "denied"
    # 跳过/拒绝不只存在日志：经工具可查（结果 + 原因 + 时间）
    _as("carol", "approver")
    listing = S.list_trigger_attempts(limit=10)
    assert "denied" in listing and "吊销" in listing
    # user 角色看不到他人调度的触发历史（可见性同 4.1 口径）
    _as("bob", "user")
    assert sid not in S.list_trigger_attempts(limit=10)


# ---------------------------------------------------------------- AC5 原子防重


def test_ac5_concurrent_claim_single_execution(monkeypatch):
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    results = []

    def _worker():
        results.append(S.process_due(1060.0))

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    jobs = [j for r in results for j in r]
    assert len(jobs) == 1                        # 同一到点并发 claim 只执行一次
    assert len(_attempt_rows()) == 1


def test_ac5_event_and_timer_share_schedule_no_duplicate(monkeypatch):
    scope = config.user_scope("root")
    auth.add_user("root", role="admin")
    _install_def("sched-demo")
    _as("root", "admin")
    _prepare_llm(monkeypatch)
    S = _sched()
    out = S.create_schedule("sched-demo", interval_seconds=60,
                            params={"topic": "周报"}, event_scope=scope,
                            now=1000.0)
    assert out["status"] == "ok"
    jobs_timer = S.process_due(1060.0)
    jobs_event = S.on_file_uploaded(**_file_event(scope))
    # 缺省 occurred_at 分支 + 重复事件不再派发（事件 × 调度唯一触发键）
    again = S.on_file_uploaded(event_id="evt-1",
                               rel_path=f"{scope}/报告.docx.md",
                               uploader="root", scope=scope, chars=5)
    # 各触发键恰执行一次：定时一次 + 事件一次，重复事件不再派发
    assert len(jobs_timer) == 1 and len(jobs_event) == 1 and again == []
    assert len(_attempt_rows()) == 2


def test_ac5_instance_write_failure_keeps_claim_mutex(monkeypatch):
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    jobs = S.process_due(1060.0)
    assert len(jobs) == 1
    # 实例写库失败不破坏互斥：锁在调度行（claim 已推进 next_run），不在实例行
    monkeypatch.setattr(orch, "create_instance_row",
                        lambda *a, **k: None)
    result = _run_job(jobs[0])
    assert isinstance(result, str)              # 执行不崩溃
    assert S.process_due(1060.0) == []          # 同一到点不再重复 claim
    assert len(_attempt_rows()) == 1


# ---------------------------------------------------------------- AC6 身份时效


def test_ac6_revoked_creator_denied_next_round(monkeypatch):
    _register_timer(monkeypatch)
    auth.revoke_user("carol")
    S = _sched()
    assert S.process_due(1060.0) == []
    attempts = _attempt_rows()
    assert len(attempts) == 1 and attempts[0]["result"] == "denied"
    assert "吊销" in attempts[0]["reason"] or "失效" in attempts[0]["reason"]


def test_ac6_role_downgrade_denied_restore_recovers(monkeypatch):
    _register_timer(monkeypatch)
    S = _sched()
    auth.set_role("carol", "user")              # 降级后不满足 Q3
    assert S.process_due(1060.0) == []
    attempts = _attempt_rows()
    assert attempts[-1]["result"] == "denied" and "角色" in attempts[-1]["reason"]
    # 恢复有效身份后按下一轮到点恢复执行（不追赶：1060 已 claim，下一轮 1120）
    auth.set_role("carol", "approver")
    _prepare_llm(monkeypatch)
    jobs = S.process_due(1120.0)
    assert len(jobs) == 1
    assert "多角色编排完成" in _run_job(jobs[0])


def test_ac6_department_change_denies_execution(monkeypatch):
    from langchain_core.tools import StructuredTool
    tool = StructuredTool.from_function(
        func=lambda query="": "OK", name="search_hr_knowledge",
        description="HR 知识检索")
    monkeypatch.setattr(state.STATE, "pack_tools",
                        {"search_hr_knowledge": tool})
    monkeypatch.setattr(orch, "_pack_tool_meta", lambda: {
        "search_hr_knowledge": {"name": "hr-pack", "allowed_roles": [],
                               "allowed_departments": ["hr"]}})
    _register_timer(monkeypatch, tools=["search_hr_knowledge"])
    auth.set_department("carol", "finance")
    _prepare_llm(monkeypatch)
    S = _sched()
    # 调岗后部门权限不满足 → 触发拒绝并留痕
    assert S.process_due(1060.0) == []
    attempts = _attempt_rows()
    assert attempts[-1]["result"] == "denied" and "部门" in attempts[-1]["reason"]
    # 部门回到白名单 → 下一轮恢复执行
    auth.set_department("carol", "hr")
    assert len(S.process_due(1120.0)) == 1


def test_ac6_metering_and_instance_attributed_to_creator(monkeypatch):
    _register_timer(monkeypatch)
    llm = _prepare_llm(monkeypatch)
    S = _sched()
    jobs = S.process_due(1060.0)
    _run_job(jobs[0])
    # 执行期身份上下文 = 创建者（触发时重解析），计量归属创建者
    assert llm.ctx and llm.ctx[0][0] == "carol"
    assert _instance_rows()[0]["initiator"] == "carol"


# ---------------------------------------------------------------- AC7 执行边界


def test_ac7_auto_instance_unbound_from_session(monkeypatch):
    _register_timer(monkeypatch)
    llm = _prepare_llm(monkeypatch)
    S = _sched()
    jobs = S.process_due(1060.0)
    result = _run_job(jobs[0])
    assert "多角色编排完成" in result
    # session_id=""、generation=None：自动实例不绑会话删除生命周期（无栅栏 aborted）
    assert llm.ctx[0][1] == ""
    r = _instance_rows()[0]
    assert r["session_id"] == "" and r["status"] == "completed"
    # 缺省 now（真实时钟）分支：下一周期到点即 claim（零真实等待）
    assert len(S.process_due()) == 1


def test_ac7_registration_tools_forbidden_in_definitions():
    # 登记类工具名进 FORBIDDEN_TOOL_NAMES（编排定义引用被拒，防递归建调度）
    assert "create_schedule" in orch.FORBIDDEN_TOOL_NAMES
    assert "delete_schedule" in orch.FORBIDDEN_TOOL_NAMES
    bad = _def_dict("bad-def", tools=["create_schedule"])
    loaded = orch.load_definition_text(
        __import__("yaml").safe_dump(bad, allow_unicode=True))
    assert loaded["status"] == "error"
    assert any(k in loaded["error"] for k in ("安全清单", "危险", "拒绝"))
    # 只读列举类工具不得进入编排安全清单（默认 fail-closed）
    assert not orch._is_safe_tool_name("create_schedule")
    assert not orch._is_safe_tool_name("list_schedules")


def test_ac7_stale_definition_skips_with_trace(monkeypatch):
    _register_timer(monkeypatch)
    # 登记后定义被替换为失效定义（改参/引用登记类工具）→ 留痕跳过不崩溃
    orch.save_definition(_def_dict("sched-demo", tools=["create_schedule"]),
                         scope=None)
    S = _sched()
    jobs = S.process_due(1060.0)
    assert jobs == []
    attempts = _attempt_rows()
    assert len(attempts) == 1 and attempts[0]["result"] == "skipped"
    assert "create_schedule" in attempts[0]["reason"]
    # 定义彻底删除 → 同样留痕跳过
    (Path(config.ORCHESTRATIONS_DIR) / "sched-demo.yaml").unlink()
    assert S.process_due(1120.0) == []
    assert _attempt_rows()[-1]["result"] == "skipped"


def test_ac7_loop_survives_cycle_exception(monkeypatch):
    S = _sched()
    calls = []

    def flaky(now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("claim 崩了")
        return []

    monkeypatch.setattr(S, "process_due", flaky)
    sleeps = {"n": 0}

    async def fake_sleep(sec):
        sleeps["n"] += 1
        if sleeps["n"] > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(S.scheduler_loop(interval=0.01))
    assert len(calls) == 2  # 单轮异常后循环继续运行


# ---------------------------------------------------------------- AC8 重启恢复


def test_ac8_registration_persists_no_catchup_after_downtime(monkeypatch):
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    # 停机期间错过多个到点：恢复后不补跑——仅一次触发（最早错过的到点）
    jobs = S.process_due(5000.0)
    assert len(jobs) == 1 and jobs[0]["planned_at"] == 1060.0
    rows = _schedule_rows()
    assert rows[0]["next_run"] > 5000.0
    assert len(_attempt_rows()) == 1
    # 登记持久化：新连接（新会话）重读仍在
    S2 = _sched()
    assert S2.process_due(5000.0) == []  # next_run 已推进，无重复


def test_ac8_stale_running_swept_on_startup(monkeypatch):
    import time as _time
    _register_timer(monkeypatch)
    # 触发模块建表（schema 归 orchestration 管理，沿 4.1 测试先例）
    _as("carol", "approver")
    orch.list_workflow_instances()
    now = _time.time()
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute(
            "INSERT INTO workflow_instances (instance_id, definition_name,"
            " definition_version, definition_hash, initiator, department,"
            " session_id, run_id, params_json, status, failed_step, error,"
            " started_at, outputs_json, summary)"
            " VALUES ('wfi-stale','sched-demo','1','h','carol','','','','{}',"
            " 'running','','',?, '{}','')", (now - 7200,))
        conn.execute(
            "INSERT INTO workflow_instances (instance_id, definition_name,"
            " definition_version, definition_hash, initiator, department,"
            " session_id, run_id, params_json, status, failed_step, error,"
            " started_at, outputs_json, summary)"
            " VALUES ('wfi-live','sched-demo','1','h','carol','','','','{}',"
            " 'running','','',?, '{}','')", (now - 10,))
        conn.commit()
    finally:
        conn.close()
    S = _sched()
    n = S.sweep_stale_running(now=now)
    assert n == 1
    by_id = {r["instance_id"]: r for r in _instance_rows()}
    assert by_id["wfi-stale"]["status"] == "failed"
    assert by_id["wfi-stale"]["error"] == "进程重启中断"
    assert by_id["wfi-live"]["status"] == "running"   # 未超龄不动


# ---------------------------------------------------------------- AC9 权限


def test_ac9_user_role_cannot_register_audited(monkeypatch):
    auth.add_user("frank", role="user")
    _install_def("sched-demo")
    from server import tools_admin
    _as("frank", "user")
    out = tools_admin.create_schedule("sched-demo", interval_seconds=60,
                                      params={"topic": "x"})
    assert "仅 approver/admin" in out          # Q3：入口收口
    assert "调度登记失败" in out
    assert _schedule_rows() == []
    recs = [x for x in audit.query(action="调度拒绝", limit=10)]
    assert any(x["actor"] == "frank" for x in recs)   # 拒绝记审计


def test_ac9_delete_permissions_creator_or_admin(monkeypatch):
    from server import tools_admin
    sid = _register_timer(monkeypatch, username="carol")
    # approver 不能删他人登记
    auth.add_user("dave", role="approver")
    _as("dave", "approver")
    out = tools_admin.delete_schedule(sid)
    assert "仅创建者本人或 admin" in out and "调度删除失败" in out
    assert any(x["actor"] == "dave" for x in
               audit.query(action="调度拒绝", limit=10))
    # 创建者本人可删
    _as("carol", "approver")
    assert "调度已删除" in tools_admin.delete_schedule(sid)
    # admin 可删任何登记
    sid2 = _register_timer(monkeypatch, username="dave")
    _as("erin", "admin")
    assert "调度已删除" in tools_admin.delete_schedule(sid2)
    assert any(x["actor"] == "erin" and x["target"] == sid2 for x in
               audit.query(action="删除调度", limit=10))


def test_ac9_schedule_tools_exposed_and_forbidden_listed():
    from server import tools_admin
    names = {getattr(f, "__name__", "") for f in tools_admin.BUILTIN_TOOLS}
    assert {"create_schedule", "list_schedules", "delete_schedule",
            "list_trigger_attempts"} <= names   # 工具实际装配
    assert {"create_schedule", "delete_schedule"} <= orch.FORBIDDEN_TOOL_NAMES


# ---------------------------------------------------------------- AC10 推进与幂等


def test_ac10_interval_floor_and_type_rejections(monkeypatch):
    auth.add_user("carol", role="approver")
    _install_def("sched-demo")
    _as("carol", "approver")
    S = _sched()
    for bad in (30, 0.5, -60, True):
        out = S.create_schedule("sched-demo", interval_seconds=bad,
                                params={"topic": "x"}, now=1000.0)
        assert out["status"] == "error", bad
    assert "60" in S.create_schedule("sched-demo", interval_seconds=30,
                                      params={"topic": "x"},
                                      now=1000.0)["error"]


def test_ac10_cross_period_advance_no_catchup_rollback_idle(monkeypatch):
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    # 长任务跨多个周期：一次到点只触发一次，next_run 按周期推进到未来（不追赶）
    jobs = S.process_due(1180.0)
    assert len(jobs) == 1 and jobs[0]["planned_at"] == 1060.0
    assert _schedule_rows()[0]["next_run"] == 1240.0
    # 时钟回拨（now < next_run）无动作
    assert S.process_due(900.0) == []
    assert _schedule_rows()[0]["next_run"] == 1240.0
    assert len(_attempt_rows()) == 1


# ---------------------------------------------------------------- AC11 旁路与生命周期


def test_ac11_hook_failure_upload_still_succeeds(monkeypatch, client_factory):
    S = _sched()

    def boom(**kw):
        raise ValueError("挂钩自爆")

    monkeypatch.setattr(S, "on_file_uploaded", boom)
    key = auth.add_user("root", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    r = c.post("/api/uploads", params={"name": "报表.txt"},
               content="季度数据".encode("utf-8"))
    assert r.status_code == 200               # 挂钩同步异常不影响上传


def test_ac11_dispatch_failure_upload_still_succeeds(monkeypatch,
                                                      client_factory):
    # 创建者 carol（approver）登记绑定 hank 上传域的事件；上传者 hank 与
    # 创建者分离（add_user 首次创建返回 Key）
    scope = config.user_scope("hank")
    auth.add_user("carol", role="approver")
    key = auth.add_user("hank", role="admin")
    _install_def("sched-demo")
    _as("carol", "approver")
    _prepare_llm(monkeypatch)
    S = _sched()
    out = S.create_schedule("sched-demo", interval_seconds=0,
                            params={"topic": "周报"}, event_scope=scope,
                            now=1000.0)
    assert out["status"] == "ok", out.get("error")

    async def boom(job):
        raise RuntimeError("派发自爆")

    monkeypatch.setattr(S, "execute_job", boom)
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    r = c.post("/api/uploads", params={"name": "报告.txt"},
               content="正文内容".encode("utf-8"))
    assert r.status_code == 200               # 派发异步异常不影响上传
    # 触发尝试已留痕（claim 同步完成）
    assert _attempt_rows() and _attempt_rows()[0]["result"] == "triggered"


def test_ac11_stop_loop_cancels_and_drains_tasks(monkeypatch):
    S = _sched()
    started = asyncio.Event()

    async def slow(job):
        started.set()
        await asyncio.Event().wait()          # 模拟在途长任务

    monkeypatch.setattr(S, "execute_job", slow)

    async def main():
        S.start_loop()
        task = S._dispatch({"schedule_id": "wfs-x"})
        await started.wait()
        await S.stop_loop()                    # 取消并等待在途任务
        return task

    task = asyncio.run(main())
    assert task.cancelled() or task.done()    # 在途任务被取消
    assert not S._TASKS                        # 任务集合清空


def test_ac11_done_callback_observes_task_exception(monkeypatch, caplog):
    import logging
    S = _sched()

    async def boom(job):
        raise RuntimeError("任务异常可观察")

    monkeypatch.setattr(S, "execute_job", boom)
    with caplog.at_level(logging.WARNING):

        async def main():
            S._dispatch({"schedule_id": "wfs-x"})
            await asyncio.sleep(0.05)

        asyncio.run(main())
    assert "RuntimeError" in caplog.text       # done 回调观察并记录异常
    assert not S._TASKS


# ---------------------------------------------------------------- AC12 部署守护


def test_ac12_second_loop_start_rejected(monkeypatch):
    S = _sched()

    async def main():
        S.start_loop()
        try:
            with pytest.raises(RuntimeError):
                S.start_loop()                 # 同进程二次启动循环拒绝
        finally:
            await S.stop_loop()

    asyncio.run(main())
    assert S._LOOP_TASK is None


def test_ac12_multi_worker_refuses_loop_with_warning(monkeypatch, caplog):
    import logging
    S = _sched()
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with caplog.at_level(logging.WARNING):
        S.start_loop()
    assert S._LOOP_TASK is None                # 多 worker 拒启调度循环
    assert "多 worker" in caplog.text           # 启动明确 WARNING（fail-closed）


# ------------------------------------------------- 边界与异常路径（AC13 覆盖）


def test_ac13_db_error_paths_are_fail_safe(monkeypatch):
    """调度库不可用：登记/删除/列举/历史/清扫均降级为明确结果，不抛异常。"""
    auth.add_user("carol", role="approver")
    _install_def("sched-demo")
    _as("carol", "approver")
    S = _sched()

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(S, "_connect", boom)
    monkeypatch.setattr(orch, "_wf_connect", boom)
    out = S.create_schedule("sched-demo", interval_seconds=60,
                            params={"topic": "x"})
    assert out["status"] == "error" and "稍后重试" in out["error"]
    out = S.delete_schedule("wfs-none")
    assert out["status"] == "error" and "稍后重试" in out["error"]
    assert S.list_schedules() == "当前没有调度登记（可用 create_schedule 工具登记）。"
    assert S.list_trigger_attempts() == "触发历史查询失败，请稍后重试"
    assert S.sweep_stale_running() == 0


def test_ac13_invalid_inputs_and_visibility_edges(monkeypatch):
    S = _sched()
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    _as("carol", "approver")
    # 删除不存在的调度
    out = S.delete_schedule("wfs-nope")
    assert out["status"] == "error" and out["error"] == "调度不存在"
    # user 角色看不到他人登记；非法 limit 回退默认 20
    _as("bob", "user")
    assert S.list_schedules() == "当前没有调度登记（可用 create_schedule 工具登记）。"
    assert S.list_trigger_attempts(limit="x") == "暂无触发历史。"
    # 引用内置工具的定义：claim 期部门检查走 meta 未命中分支，正常登记触发
    auth.add_user("erin", role="approver")
    _install_def("builtin-demo", tools=["read_text_file"])
    _as("erin", "approver")
    out = S.create_schedule("builtin-demo", interval_seconds=60,
                            params={"topic": "x"}, now=1100.0)
    assert out["status"] == "ok", out.get("error")
    # 注意：carol 的 sched-demo（登记于 1000）在 1160 同样到点，故只断言目标在列
    due = S.process_due(1160.0)
    assert [j["definition_name"] for j in due] == ["sched-demo", "builtin-demo"]
    # roles 中混入非 dict 条目：部门检查逐条跳过，不影响判定（损坏行防御）
    ok, _ = S._definition_dept_ok({"roles": ["bad", {"role": "user",
                                                     "tools": ["read_text_file"]}]},
                                   {"role": "approver", "department": ""})
    assert ok
    # resolve 通过但定义文件加载失败：登记期 load 兜底文案
    monkeypatch.setattr(orch, "load_definition",
                        lambda p: {"status": "error", "error": "bad yaml"})
    out = S.create_schedule("builtin-demo", interval_seconds=60,
                            params={"topic": "x"}, now=1100.0)
    assert out["status"] == "error" and out["error"] == "定义校验失败：bad yaml"


def test_ac13_corrupt_rows_degrade_gracefully(monkeypatch):
    _register_timer(monkeypatch)
    S = _sched()
    sid = _schedule_rows()[0]["schedule_id"]
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute("UPDATE workflow_schedules SET params_json = '坏-json'"
                     " WHERE schedule_id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()
    # claim：损坏参数按空参重解析 → 参数不合法留痕跳过（不崩溃）
    assert S.process_due(1060.0) == []
    attempts = _attempt_rows()
    assert attempts[-1]["result"] == "skipped"
    assert "参数不合法" in attempts[-1]["reason"]

    # 列举侧损坏：摘要清空、时间降级为原文，仍可读
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute("UPDATE workflow_schedules SET params_json = '坏-json',"
                     " next_run = 'abc' WHERE schedule_id = ?", (sid,))
        conn.execute(
            "INSERT INTO workflow_trigger_attempts (attempt_id, schedule_id,"
            " trigger_id, trigger_source, definition_name, creator, result,"
            " reason, payload_json, created_at)"
            " VALUES ('wft-bad', ?, 't1', 'scheduled', 'sched-demo', 'carol',"
            " 'triggered', '', '{}', 'abc')", (sid,))
        conn.commit()
    finally:
        conn.close()
    _as("carol", "approver")
    assert sid in S.list_schedules()            # 摘要/时间清洗降级
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute("UPDATE workflow_schedules SET params_json = '[1]'"
                     " WHERE schedule_id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()
    assert sid in S.list_schedules()             # 非对象参数摘要降级
    assert "abc" in S.list_trigger_attempts(limit=10)  # 损坏时间不崩


def test_ac13_claim_rollback_on_attempt_failure(monkeypatch):
    _register_timer(monkeypatch)
    S = _sched()

    def boom(*a, **k):
        raise RuntimeError("留痕失败")

    monkeypatch.setattr(S, "_insert_attempt", boom)
    with pytest.raises(RuntimeError):
        S.process_due(1060.0)
    # 事务回滚：next_run 未推进、无半态
    assert _schedule_rows()[0]["next_run"] == 1060.0
    assert _attempt_rows() == []
    # 事件路径同样回滚
    scope = config.user_scope("root")
    auth.add_user("root", role="admin")
    _install_def("event-demo")
    _as("root", "admin")
    out = S.create_schedule("event-demo", interval_seconds=0,
                            params={"topic": "x"}, event_scope=scope,
                            now=1000.0)
    assert out["status"] == "ok", out.get("error")
    with pytest.raises(RuntimeError):
        S.on_file_uploaded(event_id="evt-9",
                           rel_path=f"{scope}/报告.md", uploader="root",
                           scope=scope, chars=1, occurred_at=1.0)


def test_ac13_execute_job_error_paths(monkeypatch):
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    job = {"schedule_id": "wfs-x", "definition_name": "sched-demo",
           "params": {}, "creator": "ghost", "trigger_source": "scheduled",
           "trigger_id": "t", "planned_at": 1.0}
    # claim 后创建者失效的竞态兜底：拒绝执行且不崩
    out = asyncio.run(S.execute_job(job))
    assert "自动触发未执行" in out
    # 编排内部异常：自吞并返回明确文案（循环不死亡）——用真实 claim 的
    # 作业（手工 job 无调度行，租约 CAS 会按不存在行 fail-closed 拒绝）
    real_job = S.process_due(1060.0)[0]

    async def boom(*a, **k):
        raise RuntimeError("编排爆炸")

    monkeypatch.setattr(orch, "run_triggered", boom)
    out = asyncio.run(S.execute_job(real_job))
    assert "自动触发执行失败（RuntimeError）" in out


def test_ac13_open_mode_creator_fallback(monkeypatch):
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    monkeypatch.setattr(auth, "enabled", lambda: False)
    assert len(S.process_due(1060.0)) == 1  # 开放模式按 admin 口径放行


def test_ac13_loop_dispatches_jobs(monkeypatch):
    S = _sched()
    ran = []

    def fake_due(now):
        return [{"schedule_id": "wfs-x"}]

    async def noop(job):
        ran.append(job["schedule_id"])

    monkeypatch.setattr(S, "process_due", fake_due)
    monkeypatch.setattr(S, "execute_job", noop)
    real_sleep = asyncio.sleep
    sleeps = {"n": 0}

    async def fake_sleep(sec):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            await real_sleep(0)   # 给派发任务一个调度点
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(S.scheduler_loop(interval=0.01))
    assert ran == ["wfs-x"]        # 循环壳正确派发已 claim 作业


def test_ac13_loop_cancel_during_cycle_propagates(monkeypatch):
    S = _sched()

    def fake_due(now):
        raise asyncio.CancelledError

    monkeypatch.setattr(S, "process_due", fake_due)

    async def fake_sleep(sec):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    # claim 环节被取消：循环让取消语义传播（不吞成普通异常）
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(S.scheduler_loop(interval=0.01))


def test_ac13_start_loop_replaces_stale_closed_loop_task():
    S = _sched()
    closed = asyncio.new_event_loop()
    closed.close()

    class _StaleTask:               # 上个 lifespan 遗留、宿主循环已关的任务
        def done(self) -> bool:
            return False

        def get_loop(self):
            return closed

    S._LOOP_TASK = _StaleTask()

    async def main():
        S.start_loop()              # 不抛「二次启动拒绝」，换新任务
        await S.stop_loop()

    asyncio.run(main())
    assert S._LOOP_TASK is None

    # 异事件循环的存活实例已持有循环：本实例跳过启动，不替换他人任务
    other = asyncio.new_event_loop()
    holder = _StaleTask()
    holder.get_loop = lambda: other       # 未关闭且非当前循环
    holder_ref = S._LOOP_TASK = holder

    async def second():
        S.start_loop()
        assert S._LOOP_TASK is holder_ref

    try:
        asyncio.run(second())
    finally:
        other.close()


# ------------------------------------------------- 评审修复补测（P1-1/P2-1/P3-1）


def test_ac13_attempt_capacity_trimmed(monkeypatch):
    """触发尝试表超容量裁最旧（对齐 audit 10 万条先例）。"""
    _register_timer(monkeypatch)
    S = _sched()
    monkeypatch.setattr(S, "ATTEMPT_CAPACITY", 5)
    monkeypatch.setattr(S, "ATTEMPT_TRIM_RATIO", 0.6)
    schedule = {"schedule_id": "wfs-cap", "definition_name": "sched-demo",
                "creator": "carol"}
    conn = S._connect()
    try:
        S._ensure_schema(conn)
        for i in range(6):
            S._insert_attempt(conn, schedule, f"t{i}", "scheduled",
                              "triggered", "", {}, 1000.0 + i)
        conn.commit()
        kept = [r["trigger_id"] for r in conn.execute(
            "SELECT trigger_id FROM workflow_trigger_attempts"
            " ORDER BY rowid")]
    finally:
        conn.close()
    assert kept == ["t3", "t4", "t5"]      # 6 > 5 → 保留 5*0.6=3 条最新


def test_ac13_create_schedule_message_pure_event(monkeypatch):
    """纯事件登记的文案不再出现「下次到点 None」。"""
    from server import tools_admin
    scope = config.user_scope("root")
    auth.add_user("root", role="admin")
    _install_def("sched-demo")
    _as("root", "admin")
    out = tools_admin.create_schedule("sched-demo", params={"topic": "周报"},
                                      event_scope=scope)
    assert "调度已登记" in out and "事件触发" in out
    assert "下次到点" not in out and "None" not in out


def test_ac13_mock_mode_claim_skipped(monkeypatch):
    """Mock 模式无真实模型：claim 即跳过留痕，不记 triggered、不建实例。"""
    _register_timer(monkeypatch)
    monkeypatch.setattr(state.STATE, "llm", None)
    S = _sched()
    assert S.process_due(1060.0) == []
    attempts = _attempt_rows()
    assert attempts[-1]["result"] == "skipped"
    assert "真实模型" in attempts[-1]["reason"]
    assert _instance_rows() == []


# ------------------------------------------------ AC14 评审整改（v0.17.49 补丁）


def _set_lease(sid, running_since, trigger_id="other-trigger"):
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute("UPDATE workflow_schedules SET running_since = ?,"
                     " running_trigger_id = ? WHERE schedule_id = ?",
                     (running_since, trigger_id, sid))
        conn.commit()
    finally:
        conn.close()


def test_ac14_definition_mutex_skips_overlapping_execution(monkeypatch):
    """同名流程上一轮仍在执行：新作业不执行、留痕 skipped、不建实例。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    sid = _schedule_rows()[0]["schedule_id"]
    job = S.process_due(1060.0)[0]
    _set_lease(sid, time.time())
    out = _run_job(job)
    assert "上一轮仍在执行" in out
    att = _attempt_rows()[-1]
    assert att["outcome"] == "skipped" and "运行中互斥" in att["reason"]
    assert _instance_rows() == []
    # 别的租约未被误释放
    assert _schedule_rows()[0]["running_trigger_id"] == "other-trigger"


def test_ac14_lease_released_and_attempt_outcome(monkeypatch):
    """正常执行后释放租约，尝试记录回写终态 completed / finished_at。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    job = S.process_due(1060.0)[0]
    out = _run_job(job)
    assert "多角色编排完成" in out
    row = _schedule_rows()[0]
    assert row["running_since"] is None and row["running_trigger_id"] == ""
    att = _attempt_rows()[-1]
    assert att["result"] == "triggered"
    assert att["outcome"] == "completed" and att["finished_at"]


def test_ac14_cancel_marks_failed_and_releases_lease(monkeypatch):
    """停机取消：实例标 failed、尝试终态 cancelled、租约释放。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()

    async def fake_run(name, params, identity, **kw):
        orch.create_instance_row(
            _def_dict(), identity, params, "h",
            trigger_source=kw.get("trigger_source", "scheduled"),
            schedule_id=kw.get("schedule_id", ""),
            trigger_id=kw.get("trigger_id", ""),
            planned_at=kw.get("planned_at"),
            session_id="", run_id=kw.get("run_id") or "wfr-fake")
        await asyncio.sleep(3600)

    monkeypatch.setattr(orch, "run_triggered", fake_run)

    async def scenario():
        job = S.process_due(1060.0)[0]
        task = asyncio.create_task(S.execute_job(job))
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    inst = _instance_rows()[-1]
    assert inst["status"] == "failed" and "停机取消" in (inst["error"] or "")
    att = _attempt_rows()[-1]
    assert att["outcome"] == "cancelled" and "停机取消" in att["reason"]
    assert _schedule_rows()[0]["running_since"] is None


def test_ac14_run_id_recorded_per_execution(monkeypatch):
    """每次自动执行生成独立 run_id，写入实例行（计量/关联依据）。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    _run_job(S.process_due(1060.0)[0])
    _run_job(S.process_due(1120.0)[0])
    runs = [r["run_id"] for r in _instance_rows()]
    assert len(runs) == 2
    assert all(r.startswith("wfr-") for r in runs)
    assert len(set(runs)) == 2


def test_ac14_event_vars_rendered_from_whitelist(monkeypatch):
    """事件白名单变量可进模板；不进参数/实例参数（历史可复现）。"""
    scope = config.user_scope("root")
    auth.add_user("root", role="admin")
    _install_def("sched-demo",
                 template="来源 {{event_path}}；事件 {{event_id}}；主题 {{topic}}")
    _as("root", "admin")
    S = _sched()
    out = S.create_schedule("sched-demo", interval_seconds=0,
                            params={"topic": "周报"}, event_scope=scope,
                            now=1000.0)
    assert out["status"] == "ok", out.get("error")
    llm = _prepare_llm(monkeypatch)
    jobs = S.on_file_uploaded(**_file_event(scope))
    assert len(jobs) == 1
    _run_job(jobs[0])
    human = llm.seen_messages[0][1].content
    assert f"来源 uploads/{scope}/报告.docx.md" in human
    assert "事件 evt-1" in human and "主题 周报" in human
    assert json.loads(_instance_rows()[-1]["params_json"]) \
        == {"topic": "周报", "note": ""}


def test_ac14_expired_creator_denied(monkeypatch):
    """账号过期（Bearer Key 已失效）的创建者不再触发执行。"""
    _register_timer(monkeypatch)
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute("UPDATE api_users SET expires_at = ?"
                     " WHERE username = 'carol'", (time.time() - 10,))
        conn.commit()
    finally:
        conn.close()
    _prepare_llm(monkeypatch)
    S = _sched()
    assert S.process_due(1060.0) == []
    att = _attempt_rows()[-1]
    assert att["result"] == "denied" and "过期" in att["reason"]


def test_ac14_dispatch_failure_isolated_per_job(monkeypatch):
    """单个作业派发失败只记日志，后续作业照常派发（上传不受影响）。"""
    scope = config.user_scope("root")
    _register_event(monkeypatch, event_scope=scope)
    _register_event(monkeypatch, event_scope=scope)
    _prepare_llm(monkeypatch)
    S = _sched()
    calls = []

    def fake_dispatch(job):
        calls.append(job["trigger_id"])
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(S, "_dispatch", fake_dispatch)
    asyncio.run(S.dispatch_upload_event(rel_path=f"{scope}/报告.docx.md",
                                        uploader="root", scope=scope, chars=5))
    assert len(calls) == 2


def test_ac14_startup_sweep_clears_leases_and_pending_attempts(monkeypatch):
    """启动清扫：清空运行租约，未执行完的尝试记 abandoned（终态可查）。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    sid = _schedule_rows()[0]["schedule_id"]
    _set_lease(sid, time.time())
    assert S.process_due(1060.0)[0]["trigger_id"]
    S.sweep_stale_running(now=time.time(), grace=3600)
    row = _schedule_rows()[0]
    assert row["running_since"] is None and row["running_trigger_id"] == ""
    assert _attempt_rows()[-1]["outcome"] == "abandoned"


def test_ac14_attempt_listing_shows_outcome(monkeypatch):
    """触发历史可查终态（completed/cancelled 等），不只 claim 结果。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    _run_job(S.process_due(1060.0)[0])
    listing = S.list_trigger_attempts(limit=10)
    assert "终态：completed" in listing


def test_ac14_schema_migrates_existing_tables():
    """存量库升级：旧表自动补运行租约/终态列（幂等，不丢历史）。"""
    S = _sched()
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute(
            "CREATE TABLE workflow_schedules (schedule_id TEXT PRIMARY KEY,"
            " definition_name TEXT NOT NULL, params_json TEXT NOT NULL"
            " DEFAULT '{}', interval_seconds INTEGER NOT NULL DEFAULT 0,"
            " event_scope TEXT NOT NULL DEFAULT '', name_filter TEXT NOT NULL"
            " DEFAULT '', status TEXT NOT NULL DEFAULT 'active', creator TEXT"
            " NOT NULL, creator_role TEXT NOT NULL DEFAULT '',"
            " creator_department TEXT NOT NULL DEFAULT '', next_run REAL,"
            " claimed_at REAL, created_at REAL NOT NULL)")
        conn.execute(
            "CREATE TABLE workflow_trigger_attempts (attempt_id TEXT PRIMARY KEY,"
            " schedule_id TEXT NOT NULL, trigger_id TEXT NOT NULL,"
            " trigger_source TEXT NOT NULL, definition_name TEXT NOT NULL"
            " DEFAULT '', creator TEXT NOT NULL DEFAULT '', result TEXT NOT NULL,"
            " reason TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL"
            " DEFAULT '{}', created_at REAL NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    conn = S._connect()
    try:
        S._ensure_schema(conn)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(workflow_schedules)")}
        acols = {r[1] for r in conn.execute(
            "PRAGMA table_info(workflow_trigger_attempts)")}
    finally:
        conn.close()
    assert {"running_since", "running_trigger_id"} <= cols
    assert {"outcome", "finished_at", "run_id"} <= acols


def test_ac14_bookkeeping_failure_does_not_raise(monkeypatch):
    """租约释放/终态回写尽力而为：库异常只记日志，不外抛。"""
    S = _sched()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(S, "_connect", boom)
    S._release_run("wfs-x", "trg-x")
    S._finish_attempt("wfs-x", "trg-x", "completed")


# ------------------------------------------------ AC15 复核整改（第三轮评审）


def test_ac15_same_trigger_reentry_rejected(monkeypatch):
    """同一触发重复派发不允许重入续租（P1-1）：第二次领取同样拒绝。

    旧实现按 running_since 时长 + trigger_id 判定，同 trigger 可刷新
    自己的租约并重入执行，同一作业可生成两个实例。
    """
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    sid = _schedule_rows()[0]["schedule_id"]
    job = S.process_due(1060.0)[0]
    now = time.time()
    assert S._acquire_run(sid, "sched-demo", job["trigger_id"], now) is True
    # 本人重入：不允许（普通执行无续租语义）
    assert S._acquire_run(sid, "sched-demo", job["trigger_id"], now) is False
    # 释放后可再次领取
    S._release_run(sid, job["trigger_id"])
    assert S._acquire_run(sid, "sched-demo", job["trigger_id"], now) is True
    S._release_run(sid, job["trigger_id"])


def test_ac15_active_long_task_not_taken_over(monkeypatch):
    """超龄但未释放的租约运行期不按时长接管（P1-2 方案 1）：新触发仍
    skipped；只有启动清扫清空租约后才能接手。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    sid = _schedule_rows()[0]["schedule_id"]
    job = S.process_due(1060.0)[0]
    _set_lease(sid, time.time() - 7200)  # 超过 grace，但任务可能仍活跃
    out = _run_job(job)
    assert "上一轮仍在执行" in out
    att = _attempt_rows()[-1]
    assert att["outcome"] == "skipped" and "运行中互斥" in att["reason"]
    # 模拟进程重启：启动清扫清空遗留租约后，下一轮触发可正常执行
    S.sweep_stale_running(now=time.time(), grace=3600)
    out2 = _run_job(S.process_due(1120.0)[0])
    assert "多角色编排完成" in out2


def test_ac15_run_scoped_status_and_abort():
    """终态查询/取消补偿按 run_id 精确执行（P2-1）：同 trigger_id 的
    其他运行实例不被误伤。"""
    ident = _identity("carol")
    for rid in ("wfr-a", "wfr-b"):
        orch.create_instance_row(
            _def_dict(), ident, {"topic": "x", "note": ""}, "h",
            trigger_source="scheduled", schedule_id="s1",
            trigger_id="same-trigger", planned_at=None,
            session_id="", run_id=rid)
    assert orch.instance_status_for_run("wfr-b") == "running"
    assert orch.instance_status_for_run("wfr-none") == ""
    n = orch.abort_instance_for_run("wfr-a", "停机取消")
    assert n == 1
    assert orch.instance_status_for_run("wfr-a") == "failed"
    # 旧任务的取消补偿不得影响同 trigger 下的新运行实例
    assert orch.instance_status_for_run("wfr-b") == "running"


def test_ac15_takeover_old_task_cancel_never_touches_new_run(monkeypatch):
    """端到端（P1-1/P1-2/P2-1 组合）：旧任务被启动清扫接替后，其取消
    补偿只命中自己 run_id 的实例，新执行照常完成且不受影响。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    job1 = S.process_due(1060.0)[0]

    async def fake_run(name, params, identity, **kw):
        iid = orch.create_instance_row(
            _def_dict(), identity, params, "h",
            trigger_source=kw.get("trigger_source", "scheduled"),
            schedule_id=kw.get("schedule_id", ""),
            trigger_id=kw.get("trigger_id", ""),
            planned_at=kw.get("planned_at"),
            session_id="", run_id=kw.get("run_id") or "wfr-fake")
        if kw.get("trigger_id") == job1["trigger_id"]:
            await asyncio.sleep(3600)  # 第一轮长任务迟迟不结束
        else:
            orch.finish_instance_row(iid, status="completed", summary="完成")
        return "多角色编排完成"

    monkeypatch.setattr(orch, "run_triggered", fake_run)
    seen = {}

    async def scenario():
        task1 = asyncio.create_task(S.execute_job(job1))
        for _ in range(3):
            await asyncio.sleep(0)
        # 模拟进程重启：清扫清空租约（实例 1 在 grace 内不被标 failed）
        S.sweep_stale_running(now=time.time(), grace=3600)
        job2 = S.process_due(1120.0)[0]
        seen["trigger2"] = job2["trigger_id"]
        out2 = await S.execute_job(job2)
        assert "多角色编排完成" in out2
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1

    asyncio.run(scenario())
    rows = _instance_rows()
    assert len(rows) == 2
    old_inst, new_inst = rows[0], rows[1]
    assert old_inst["status"] == "failed"
    assert "停机取消" in (old_inst["error"] or "")
    # 新执行不被旧任务的取消补偿误伤，且尝试回写关联本次 run_id
    assert new_inst["status"] == "completed"
    assert new_inst["run_id"] != old_inst["run_id"]
    atts = {a["trigger_id"]: a for a in _attempt_rows()}
    assert atts[seen["trigger2"]]["outcome"] == "completed"
    assert atts[seen["trigger2"]]["run_id"] == new_inst["run_id"]


def test_ac15_orphan_job_lease_rejected(monkeypatch):
    """调度行不存在的手工作业：租约 CAS 条件更新命中 0 行 fail-closed
    拒绝（不执行、不建实例、留痕 skipped）。"""
    _register_timer(monkeypatch)
    _prepare_llm(monkeypatch)
    S = _sched()
    job = {"schedule_id": "wfs-none", "definition_name": "sched-demo",
           "params": {}, "creator": "carol", "trigger_source": "scheduled",
           "trigger_id": "t-orphan", "planned_at": 1.0}
    out = asyncio.run(S.execute_job(job))
    assert "上一轮仍在执行" in out
    assert _instance_rows() == []
    assert _attempt_rows() == []  # 未经 claim，无留痕可回写
