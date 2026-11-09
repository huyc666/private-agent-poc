"""4.3 端到端：AC3（角色由文档决定）与 AC9（生成→确认→执行）红测试。

只按 spec 契约写断言；生成入口未实现时以 pytest.fail 暴露为 failed。
AC3 的口径：通过校验只证明结构与工具边界合法，两份输出不同只证明未套固定
模板；角色语义是否真的来自文档由门控真实模型 + 人工留证判断（见
tests/test_workflow_draft_llm_gated.py）。
"""
import asyncio
import hashlib
import inspect
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage

from server import auth, config, orchestration as orch, usage

DRAFT_TOOL = "draft_orchestration_from_doc"
API = "/api/orchestration-drafts"


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append({"messages": list(messages), "config": config})
        if not self.replies:
            raise AssertionError("模型调用次数超出预期")
        return AIMessage(content=self.replies.pop(0))


def _call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


def _draft_fn():
    fn = getattr(orch, DRAFT_TOOL, None)
    if fn is None:
        pytest.fail(f"未实现：server.orchestration.{DRAFT_TOOL}")
    return fn


def _login(username, role="user"):
    key = auth.add_user(username, role=role, department="finance")
    usage.current_user.set(username)
    usage.current_identity.set(usage.Identity(
        username=username, role=role, department="finance",
        auth_source="bearer"))
    return key


def _write_upload(username, name, text):
    d = Path(config.UPLOADS_DIR) / config.user_scope(username)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return f"uploads/{config.user_scope(username)}/{name}"


def _drafts_root():
    root = getattr(config, "ORCHESTRATION_DRAFTS_DIR", None)
    if root is None:
        root = Path(config.TOOL_WORKSPACE) / "orchestration_drafts"
    return Path(root)


def _reply(definition):
    return ("```yaml\n"
            + yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)
            + "```\n生成说明：按文档角色生成")


def _definition(name, role_ids):
    roles = [{"id": rid, "name": rid, "prompt": f"承担 {rid} 职责", "tools": []}
             for rid in role_ids]
    steps = [{"id": "s1", "role": role_ids[0], "mode": "single"},
             {"id": "s2", "role": role_ids[1], "mode": "single",
              "inputs": ["s1"]}]
    return {"name": name, "version": "1", "description": "文档生成流程",
            "max_parallel": 2, "roles": roles, "steps": steps,
            "aggregate": {"role": role_ids[1], "inputs": ["s2"]}}


def _all_text(fake):
    parts = []
    for call in fake.calls:
        for m in call["messages"]:
            parts.append(str(getattr(m, "content", m)))
    return "\n".join(parts)


def _instance_status(name):
    path = Path(config.STATE_DB)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        try:
            row = conn.execute(
                "SELECT status FROM workflow_instances WHERE definition_name = ?"
                " ORDER BY started_at DESC LIMIT 1", (name,)).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()


# ---------------------------------------------------------------- AC3 角色由文档决定

def test_ac3_prompt_contains_document_and_data_constraint(monkeypatch):
    _login("alice")
    text = "采购制度：申请岗提交，审批岗复核，归档岗留存。"
    path = _write_upload("alice", "采购制度.md", text)
    fake = _FakeLLM([_reply(_definition("doc-flow", ["applicant", "approver"]))])
    monkeypatch.setattr(orch.STATE, "llm", fake)
    _call(_draft_fn(), path)
    prompt = _all_text(fake)
    assert text in prompt, "生成提示必须包含文档正文"
    assert any(k in prompt for k in ("不可信", "数据", "不要执行", "不得执行")), \
        "生成提示必须声明文档是不可信数据"


def test_ac3_two_documents_produce_different_role_sets(monkeypatch):
    _login("alice")
    doc_a = "采购制度：申请岗与审批岗。"
    doc_b = "档案制度：登记岗与归档岗。"
    path_a = _write_upload("alice", "采购.md", doc_a)
    path_b = _write_upload("alice", "档案.md", doc_b)
    fake = _FakeLLM([
        _reply(_definition("flow-a", ["applicant", "approver"])),
        _reply(_definition("flow-b", ["registrar", "archivist"])),
    ])
    monkeypatch.setattr(orch.STATE, "llm", fake)
    _call(_draft_fn(), path_a)
    _call(_draft_fn(), path_b)

    metas = [json.loads(p.read_text(encoding="utf-8"))
             for p in _drafts_root().rglob("*.meta.json")]
    assert len(metas) == 2
    yamls = [yaml.safe_load(p.read_text(encoding="utf-8"))
             for p in _drafts_root().rglob("*.yaml")]
    role_sets = {tuple(sorted(r["id"] for r in d["roles"])) for d in yamls}
    assert len(role_sets) == 2, "两份文档不得生成同一套固定角色"
    assert all(orch.validate_definition(d) == [] for d in yamls)


# ---------------------------------------------------------------- AC9 可运行端到端

def test_ac9_generate_confirm_run_completed(monkeypatch, client_factory):
    key = _login("alice")
    doc = "业务说明：受理岗登记需求，复核岗审核并汇总。"
    path = _write_upload("alice", "业务说明.md", doc)
    definition = _definition("doc-flow", ["intake", "reviewer"])
    fake = _FakeLLM([
        _reply(definition),      # 生成
        "受理结果",               # step 1
        "复核结果",               # step 2
        "最终汇总",               # aggregate
    ])
    monkeypatch.setattr(orch.STATE, "llm", fake)

    result = _call(_draft_fn(), path)
    assert isinstance(result, str)
    metas = list(_drafts_root().rglob("*.meta.json"))
    assert len(metas) == 1
    draft_id = json.loads(metas[0].read_text(encoding="utf-8"))["draft_id"]

    client = client_factory()
    if key:
        client.headers.update({"Authorization": f"Bearer {key}"})
    confirmed = client.post(f"{API}/{draft_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text

    run_text = _call(orch.run_orchestration, "doc-flow", "执行一次")
    assert "多角色编排完成" in run_text, run_text
    assert _instance_status("doc-flow") == "completed"
    assert len(fake.calls) == 4, "生成 1 次 + 三步执行 3 次"
