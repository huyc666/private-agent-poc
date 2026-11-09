"""4.3 门控真实模型用例（Q4=A）：AC3/AC9 的人工留证辅助。

默认跳过：未设置 RUN_LLM_TESTS 时整文件不计入必过基线；运行需真实模型
（MOCK_LLM=false 且 STATE.llm 已装配）。运行记录回填 spec §6 / §7.1，
留证字段固定：命令、文档相对路径与哈希、draft_id、确认后实例 id。
"""
import asyncio
import hashlib
import inspect
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest
import yaml

from server import auth, config, orchestration as orch, usage

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LLM_TESTS"),
    reason="需 RUN_LLM_TESTS=1 与真实模型（Q4=A 门控用例，不计入必过基线）")

DRAFT_TOOL = "draft_orchestration_from_doc"
API = "/api/orchestration-drafts"


@pytest.fixture(scope="module", autouse=True)
def _real_llm():
    """门控运行：复用生产装配函数接真实模型（不是替身）。"""
    if not os.environ.get("RUN_LLM_TESTS"):
        yield
        return
    if orch.STATE.llm is None:
        from server import agent
        asyncio.run(agent.build_agent())
    yield


def _call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


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
    return p, f"uploads/{config.user_scope(username)}/{name}"


def _draft_fn():
    if orch.STATE.llm is None:
        pytest.skip("真实模型未装配（STATE.llm 为空），门控用例不适用")
    fn = getattr(orch, DRAFT_TOOL, None)
    if fn is None:
        pytest.fail(f"未实现：server.orchestration.{DRAFT_TOOL}")
    return fn


def _drafts_root():
    root = getattr(config, "ORCHESTRATION_DRAFTS_DIR", None)
    if root is None:
        root = Path(config.TOOL_WORKSPACE) / "orchestration_drafts"
    return Path(root)


def test_ac3_real_model_roles_differ_between_documents():
    _login("alice")
    docs = {
        "采购制度.md": "采购制度：需求申请由申请岗提交，部门负责人审批，采购岗执行。",
        "档案制度.md": "档案制度：资料由登记岗接收，档案管理员分类归档，审计岗抽查。",
    }
    role_sets = []
    for name, text in docs.items():
        p, rel = _write_upload("alice", name, text)
        result = _call(_draft_fn(), rel)
        assert "已生成流程草稿" in result, f"真实模型生成失败：{result}"
        draft_id = re.search(r"drf-[0-9a-f]{12}", result).group(0)
        meta_path = next(_drafts_root().rglob(f"{draft_id}.meta.json"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"[留证] {rel} sha256={hashlib.sha256(p.read_bytes()).hexdigest()} "
              f"draft_id={meta['draft_id']}")
        yaml_path = Path(meta_path).with_suffix("").with_suffix(".yaml")
        definition = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert orch.validate_definition(definition) == []
        role_sets.append(tuple(sorted(r["id"] for r in definition["roles"])))
    assert len(set(role_sets)) == 2, f"真实模型输出不得套固定角色模板：{role_sets}"


def test_ac9_real_model_end_to_end(client_factory):
    key = _login("alice")
    doc = ("业务说明：客服岗接收工单，技术岗处理问题，质检岗回访并汇总。"
           "每个角色只处理自己职责范围内的信息。")
    p, rel = _write_upload("alice", "业务说明.md", doc)
    result = _call(_draft_fn(), rel)
    found = re.search(r"drf-[0-9a-f]{12}", result)
    assert found, f"生成结果需含 draft_id：{result}"
    draft_id = found.group(0)
    meta_path = next(_drafts_root().rglob(f"{draft_id}.meta.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["draft_id"] = draft_id
    client = client_factory()
    if key:
        client.headers.update({"Authorization": f"Bearer {key}"})
    confirmed = client.post(f"{API}/{draft_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    # 4.1 起定义可声明必填参数：按定义补齐，避免测试自身漏参导致执行未开始
    yaml_path = Path(meta_path).with_suffix("").with_suffix(".yaml")
    definition = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    params = {p["name"]: (p.get("default") or "测试工单：系统登录失败")
              for p in definition.get("params") or []}
    run_text = _call(orch.run_orchestration, meta["definition_name"],
                     "执行一次", params)
    assert "多角色编排完成" in run_text, run_text
    found = re.search(r"wfi-[0-9a-f]{12}", run_text)
    assert found, f"执行结果需含实例 ID：{run_text}"
    conn = sqlite3.connect(str(config.STATE_DB))
    row = conn.execute(
        "SELECT status, error FROM workflow_instances WHERE instance_id=?",
        (found.group(0),)).fetchone()
    conn.close()
    assert row and row[0] == "completed", f"实例终态异常：{row}"
    print(f"[留证] doc={rel} sha256={hashlib.sha256(p.read_bytes()).hexdigest()} "
          f"draft_id={draft_id} definition={meta['definition_name']} "
          f"instance={found.group(0)} status={row[0]}")
