"""4.1 流程定义与固化：实例生命周期、字段/可见性/截断、失败终局（spec AC3/AC4/AC7）。

红测试口径：workflow_instances 表与实例回写均为 v0.17.46 新增——实现前
查询无表（空行集）、run_orchestration 不接受 params（TypeError）、
weekly-report.yaml 不存在（FileNotFoundError）。
"""
import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from server import config, orchestration as orch, sessions, state, usage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_INSTANCE_ID_RE = re.compile(r"wfi-[0-9a-f]{12}")


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _instance_rows(initiator=None) -> list[dict]:
    conn = sqlite3.connect(config.STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        if initiator:
            rows = conn.execute(
                "SELECT * FROM workflow_instances WHERE initiator = ?"
                " ORDER BY started_at", (initiator,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workflow_instances ORDER BY started_at").fetchall()
    except sqlite3.OperationalError:
        return []  # 表尚未建立（红态）
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _instance_count() -> int:
    return len(_instance_rows())


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    async def ainvoke(self, messages, config=None):
        return AIMessage(content=self.replies.pop(0))


def _install_react(monkeypatch, *, answer="步骤结果", factory=None):
    import langgraph.prebuilt as prebuilt
    made = []

    class FakeAgent:
        def __init__(self, llm, tools):
            self.llm = llm
            self.tools = list(tools)
            made.append(self)

        async def ainvoke(self, payload, config=None):
            if factory is not None:
                return await factory(self, payload, config)
            return {"messages": [AIMessage(content=answer)]}

    monkeypatch.setattr(prebuilt, "create_react_agent", FakeAgent)
    return made


def _enable_core_utils(monkeypatch):
    """测试环境 PACKS_DIR 为空：按 5.1 白名单口径桩载 get_current_time。"""
    tool = StructuredTool.from_function(
        func=lambda: "2026-11-05 10:00", name="get_current_time",
        description="获取当前时间")
    monkeypatch.setattr(state.STATE, "pack_tools", {"get_current_time": tool})
    monkeypatch.setattr(orch, "_pack_tool_meta", lambda: {
        "get_current_time": {"name": "core-utils", "allowed_roles": [],
                             "allowed_departments": []}})


def _prepare(monkeypatch):
    monkeypatch.setattr(state.STATE, "llm", object())
    usage.current_session.set("sess-1")
    usage.current_run.set("run-1")


def _run_weekly(monkeypatch, week, highlights=None, username="alice"):
    identity = _identity(username)
    usage.current_identity.set(identity)
    usage.current_user.set(username)
    params = {"week": week}
    if highlights is not None:
        params["highlights"] = highlights
    return asyncio.run(orch.run_orchestration("weekly-report", "", params=params))


def _install_weekly_report(monkeypatch):
    _enable_core_utils(monkeypatch)
    text = (PROJECT_ROOT / "orchestrations" / "weekly-report.yaml").read_text(
        encoding="utf-8")
    loaded = orch.load_definition_text(text)
    assert loaded["status"] == "ok", loaded["error"]
    orch.save_definition(loaded["definition"], scope=None)
    return loaded["definition"]


# ---------------------------------------------------------------- AC3 重复触发


def test_ac3_weekly_report_repeatable_trigger(monkeypatch):
    _install_weekly_report(monkeypatch)
    _prepare(monkeypatch)
    saved = []

    async def factory(agent, payload, config):
        save = next((t for t in agent.tools if t.name == "save_output_file"), None)
        if save is not None:
            save.invoke({"name": "周报.md", "content": "周报正文"})
            saved.append(True)
        return {"messages": [AIMessage(content="步骤产出")]}

    _install_react(monkeypatch, factory=factory)

    out1 = _run_weekly(monkeypatch, "2026-W44")
    out2 = _run_weekly(monkeypatch, "2026-W45", highlights="项目甲落地")
    assert "多角色编排完成" in out1 and "多角色编排完成" in out2

    rows = _instance_rows("alice")
    assert len(rows) == 2
    assert len({r["instance_id"] for r in rows}) == 2
    for r in rows:
        assert r["status"] == "completed"
        assert r["definition_name"] == "weekly-report"
        assert r["outputs_json"] and r["summary"]
        assert r["finished_at"]
    assert sorted(json.loads(r["params_json"])["week"] for r in rows) == [
        "2026-W44", "2026-W45"]
    # 产出文件落发起者域
    assert saved, "writer 步骤应调用 save_output_file"
    scope = config.user_scope("alice")
    assert (config.OUTPUTS_DIR / scope / "周报.md").exists()


def test_ac3_outputs_truncated_to_budget(monkeypatch):
    _install_weekly_report(monkeypatch)
    _prepare(monkeypatch)
    _install_react(monkeypatch, answer="长" * 3000)  # 每步产出 6000 字符
    _run_weekly(monkeypatch, "2026-W44")
    rows = _instance_rows("alice")
    assert len(rows) == 1
    outputs = json.loads(rows[0]["outputs_json"])
    assert outputs and all(len(v) <= 2000 for v in outputs.values())
    assert len(rows[0]["summary"]) <= 2000


# ---------------------------------------------------------------- AC4 实体与可见性


def test_ac4_instance_fields_complete(monkeypatch):
    _install_weekly_report(monkeypatch)
    _prepare(monkeypatch)
    _install_react(monkeypatch)
    _run_weekly(monkeypatch, "2026-W44")
    rows = _instance_rows("alice")
    assert len(rows) == 1
    r = rows[0]
    assert _INSTANCE_ID_RE.fullmatch(r["instance_id"])
    for field in ("definition_name", "definition_version", "definition_hash",
                  "initiator", "department", "session_id", "run_id",
                  "params_json", "status", "failed_step", "error",
                  "started_at", "finished_at", "outputs_json", "summary"):
        assert field in r, f"缺字段 {field}"
    assert r["initiator"] == "alice" and r["department"] == "finance"
    assert r["session_id"] == "sess-1" and r["run_id"] == "run-1"
    assert r["definition_hash"]
    # 触发时未传的可选参数按声明 default 填充后一并留存
    assert json.loads(r["params_json"]) == {"week": "2026-W44", "highlights": "无"}


def _simple_definition(name="inst-demo") -> dict:
    return {
        "name": name, "version": "1",
        "roles": [{"id": "r", "prompt": "执行", "tools": ["read_text_file"]}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }


def test_ac4_visibility_user_vs_approver(monkeypatch):
    _prepare(monkeypatch)
    orch.save_definition(_simple_definition(), scope=None)
    _install_react(monkeypatch)

    usage.current_identity.set(_identity("alice"))
    usage.current_user.set("alice")
    out_a = asyncio.run(orch.run_orchestration("inst-demo", "任务甲"))
    id_a = _INSTANCE_ID_RE.search(out_a)
    assert id_a, out_a

    usage.current_identity.set(_identity("bob", department="hr"))
    usage.current_user.set("bob")
    out_b = asyncio.run(orch.run_orchestration("inst-demo", "任务乙"))
    id_b = _INSTANCE_ID_RE.search(out_b)
    assert id_b and id_b.group(0) != id_a.group(0)

    # user 仅本人实例（列举工具只读同步，与 list_custom_tools 同口径）
    usage.current_identity.set(_identity("alice"))
    listing = orch.list_workflow_instances()
    assert id_a.group(0) in listing and id_b.group(0) not in listing

    usage.current_identity.set(_identity("bob", department="hr"))
    listing = orch.list_workflow_instances()
    assert id_b.group(0) in listing and id_a.group(0) not in listing

    # approver / admin 全部可见
    for role in ("approver", "admin"):
        usage.current_identity.set(_identity("carol", role=role))
        listing = orch.list_workflow_instances()
        assert id_a.group(0) in listing and id_b.group(0) in listing, role


def test_ac4_listing_order_limit_and_filter(monkeypatch):
    usage.current_identity.set(_identity("carol"))
    usage.current_user.set("carol")
    orch.list_workflow_instances()  # 触发模块建表（schema 归 orchestration 管理）
    # 造 105 行数据（空表循环体不执行先例：必须覆盖非空路径与上限截断）
    conn = sqlite3.connect(config.STATE_DB)
    try:
        for i in range(105):
            conn.execute(
                "INSERT INTO workflow_instances (instance_id, definition_name,"
                " definition_version, definition_hash, initiator, department,"
                " session_id, run_id, params_json, status, failed_step, error,"
                " started_at, finished_at, outputs_json, summary)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"wfi-{i:012x}", "inst-demo", "1", "h", "carol", "finance",
                 "s", "r", "{}", "completed", "", "", 1000.0 + i, 1001.0 + i,
                 "{}", "摘要"))
        conn.commit()
    finally:
        conn.close()

    listing = orch.list_workflow_instances(limit=200)
    ids = _INSTANCE_ID_RE.findall(listing)
    assert len(ids) == 100  # limit 上限 100
    assert ids[0] == f"wfi-{104:012x}" and ids[-1] == f"wfi-{5:012x}"  # 倒序

    listing20 = orch.list_workflow_instances(limit=20)
    assert len(_INSTANCE_ID_RE.findall(listing20)) == 20

    filtered = orch.list_workflow_instances(definition_name="other-demo", limit=200)
    assert not _INSTANCE_ID_RE.findall(filtered)


# ---------------------------------------------------------------- AC7 失败与终局


def test_ac7_execution_exception_marks_failed(monkeypatch):
    _prepare(monkeypatch)
    orch.save_definition(_simple_definition("fail-demo"), scope=None)

    async def boom(agent, payload, config=None):
        raise RuntimeError("boom C:\\secret\\trace")

    _install_react(monkeypatch, factory=boom)
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    out = asyncio.run(orch.run_orchestration("fail-demo", "任务"))
    assert "执行失败" in out
    rows = _instance_rows("alice")
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "failed"
    assert r["failed_step"] == "s"
    assert r["error"] and "RuntimeError" in r["error"]
    assert "C:\\secret" not in r["error"]
    assert r["finished_at"]


def test_ac7_fence_abort_marks_aborted(monkeypatch):
    _prepare(monkeypatch)
    orch.save_definition(_simple_definition("abort-demo"), scope=None)
    _install_react(monkeypatch)
    monkeypatch.setattr(sessions, "fence_ok", lambda sid, gen: False)
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    usage.current_generation.set(3)
    asyncio.run(orch.run_orchestration("abort-demo", "任务"))
    rows = _instance_rows("alice")
    assert len(rows) == 1
    assert rows[0]["status"] == "aborted"
    assert rows[0]["finished_at"]
    assert json.loads(rows[0]["outputs_json"] or "{}") == {}


def test_ac7_budget_exceeded_marks_failed_with_step(monkeypatch):
    _prepare(monkeypatch)
    # 预算用例经 _GuardedLLM 真实调用模型 → 需要可应答的 FakeLLM（非 object()）
    monkeypatch.setattr(state.STATE, "llm", _FakeLLM(["ok", "ok"]))
    definition = {
        "name": "budget-wf", "version": "1",
        "roles": [{"id": "r", "prompt": "执行", "tools": ["read_text_file"]}],
        "steps": [{"id": "burn", "role": "r", "mode": "single"}],
    }
    orch.save_definition(definition, scope=None)
    monkeypatch.setattr(orch, "MAX_MODEL_CALLS", 1)

    async def factory(agent, payload, config):
        await agent.llm.ainvoke(payload["messages"])
        await agent.llm.ainvoke(payload["messages"])
        return {"messages": [AIMessage(content="ok")]}

    _install_react(monkeypatch, factory=factory)
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    asyncio.run(orch.run_orchestration("budget-wf", "任务"))
    rows = _instance_rows("alice")
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["failed_step"] == "burn"
    assert "上限" in rows[0]["error"]


def test_ac7_mock_mode_no_phantom_instance(monkeypatch):
    monkeypatch.setattr(state.STATE, "llm", None)
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    out = asyncio.run(orch.run_orchestration(
        "weekly-report", "任务", params={"week": "2026-W44"}))
    assert "Mock" in out
    assert _instance_count() == 0


def test_ac7_instance_write_failure_does_not_block(monkeypatch):
    # 实例落库失败仅记日志：执行照常完成（实例是记录，不是执行前提）
    _prepare(monkeypatch)
    orch.save_definition(_simple_definition("dbfail-demo"), scope=None)
    _install_react(monkeypatch)

    def bad_connect():
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(orch, "_wf_connect", bad_connect)
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    out = asyncio.run(orch.run_orchestration("dbfail-demo", "任务"))
    assert "多角色编排完成" in out, out
