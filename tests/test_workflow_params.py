"""4.1 流程定义与固化：参数声明校验与触发参数校验/渲染（spec AC1/AC2）。

红测试口径：params/task_template 为 v0.17.46 新增字段——实现前要么被顶层字段
白名单拒绝（合法定义无法加载），要么工具签名不接受 params（TypeError）；
实现后参数声明/触发校验 fail-closed，模板渲染为单遍字面替换。
"""
import asyncio
import sqlite3
from pathlib import Path

import pytest
import yaml

from server import config, orchestration as orch, state, usage

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _base_definition(name="param-demo", params=None, task_template=None) -> dict:
    d = {
        "name": name,
        "version": "1",
        "roles": [{"id": "r", "prompt": "执行任务", "tools": []}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }
    if params is not None:
        d["params"] = params
    if task_template is not None:
        d["task_template"] = task_template
    return d


def _instance_count() -> int:
    conn = sqlite3.connect(config.STATE_DB)
    try:
        n = conn.execute("SELECT COUNT(*) FROM workflow_instances").fetchone()[0]
    except sqlite3.OperationalError:
        return 0  # 表尚未建立（红态）等价于无实例行
    finally:
        conn.close()
    return n


def _prepare(monkeypatch, definition):
    monkeypatch.setattr(state.STATE, "llm", object())
    usage.current_identity.set(_identity())
    usage.current_user.set("alice")
    orch.save_definition(definition, scope=None)


# ---------------------------------------------------------------- AC1 声明校验


def test_ac1_valid_params_definition_loads():
    d = _base_definition(
        params=[{"name": "week", "type": "string", "required": True,
                 "description": "周次"},
                {"name": "highlights", "type": "string", "default": "无"}],
        task_template="请写 {{week}} 的周报，重点：{{highlights}}")
    assert orch.validate_definition(d) == []
    assert orch.load_definition_text(
        yaml.safe_dump(d, allow_unicode=True))["status"] == "ok"


@pytest.mark.parametrize("params,keyword", [
    ([{"name": "week", "type": "string"},
      {"name": "week", "type": "string"}], "重复"),
    ([{"name": "Week", "type": "string"}], "参数名"),
    ([{"name": "_week", "type": "string"}], "参数名"),
    ([{"name": "week-name", "type": "string"}], "参数名"),
    ([{"name": "week", "type": "list"}], "类型"),
    ([{"name": "count", "type": "int", "default": "3"}], "default"),
    ([{"name": "flag", "type": "bool", "default": 0}], "default"),
    ([{"name": "week", "type": "string"}, "not-a-dict"], "参数"),
])
def test_ac1_param_declaration_rejected(params, keyword):
    d = _base_definition(params=params)
    errors = orch.validate_definition(d)
    assert errors, f"应拒绝：{params}"
    assert any(keyword in e for e in errors), errors


def test_ac1_template_reference_undeclared_param_rejected():
    d = _base_definition(params=[{"name": "week", "type": "string"}],
                         task_template="周报 {{week}} {{nope}}")
    errors = orch.validate_definition(d)
    assert errors and any("未声明" in e for e in errors), errors


def test_ac1_legacy_definitions_unchanged():
    # 5.1 既有的两个无参示例定义：校验结果不变（可加载）
    for name in ("research-demo", "review-demo"):
        loaded = orch.load_definition(PROJECT_ROOT / "orchestrations" / f"{name}.yaml")
        assert loaded["status"] == "ok", loaded["error"]


# ---------------------------------------------------------------- AC2 触发校验


@pytest.mark.parametrize("params,keyword", [
    ({}, "week"),                                  # 缺必填
    ({"week": "2026-W45", "nope": "x"}, "nope"),  # 未知参数
    ({"week": 2026}, "week"),                     # 类型不符（int 传给 string）
    ({"week": "x" * 2001}, "week"),               # 超过 2000 字符上限
])
def test_ac2_trigger_param_rejection_creates_no_instance(monkeypatch, params, keyword):
    d = _base_definition(params=[{"name": "week", "type": "string", "required": True}],
                         task_template="周报 {{week}}")
    _prepare(monkeypatch, d)
    out = asyncio.run(orch.run_orchestration("param-demo", "", params=params))
    assert "编排未执行" in out, out
    assert keyword in out, out
    assert _instance_count() == 0


def test_ac2_bool_param_strict_typing(monkeypatch):
    d = _base_definition(params=[{"name": "flag", "type": "bool", "required": True}],
                         task_template="标记 {{flag}}")
    _prepare(monkeypatch, d)
    out = asyncio.run(orch.run_orchestration("param-demo", "", params={"flag": 1}))
    assert "编排未执行" in out and "flag" in out
    assert _instance_count() == 0


def _capture_execute(monkeypatch):
    """桩掉 execute，只捕获渲染后的任务文本（渲染发生在执行之前）。"""
    captured = {}

    async def fake_execute(definition, task, **kwargs):
        captured["task"] = task
        return {"ok": True, "name": definition.get("name", ""), "version": "1",
                "hash": "", "outputs": {"s": "ok"}, "findings": [], "subtasks": [],
                "summary": "ok", "completed": True, "failed_step": "", "error": ""}

    monkeypatch.setattr(orch, "execute", fake_execute)
    return captured


def test_ac2_template_render_single_pass_literal(monkeypatch):
    d = _base_definition(params=[{"name": "week", "type": "string", "required": True}],
                         task_template="周报 {{week}} 完")
    _prepare(monkeypatch, d)
    captured = _capture_execute(monkeypatch)
    evil = "{{week}} {{nope}} __import__('os').system('id') {0.__class__}"
    asyncio.run(orch.run_orchestration("param-demo", "", params={"week": evil}))
    # 单遍字面替换：注入型参数值原样保留，无二次替换、无 eval
    assert captured["task"] == f"周报 {evil} 完"


def test_ac2_optional_default_rendered(monkeypatch):
    d = _base_definition(
        params=[{"name": "week", "type": "string", "required": True},
                {"name": "highlights", "type": "string", "default": "无重点"}],
        task_template="周报 {{week}} 重点 {{highlights}}")
    _prepare(monkeypatch, d)
    captured = _capture_execute(monkeypatch)
    asyncio.run(orch.run_orchestration("param-demo", "", params={"week": "2026-W45"}))
    assert captured["task"] == "周报 2026-W45 重点 无重点"


def test_ac2_explicit_task_wins_over_template(monkeypatch):
    d = _base_definition(params=[{"name": "week", "type": "string", "required": True}],
                         task_template="周报 {{week}}")
    _prepare(monkeypatch, d)
    captured = _capture_execute(monkeypatch)
    asyncio.run(orch.run_orchestration(
        "param-demo", "显式任务优先", params={"week": "2026-W45"}))
    assert captured["task"] == "显式任务优先"


def test_ac2_no_task_no_template_rejected(monkeypatch):
    d = _base_definition()  # 无 params 也无 task_template
    _prepare(monkeypatch, d)
    out = asyncio.run(orch.run_orchestration("param-demo", "", params={}))
    assert "编排未执行" in out, out
    assert _instance_count() == 0


# -------------------------------------------- 外部审核整改（v0.17.47，N2/N4）


def test_n2_oversized_string_default_rejected_at_declaration():
    # N2：声明的字符串 default 也受 2000 字符上限（此前只查类型）
    d = _base_definition(params=[{"name": "week", "type": "string",
                                  "default": "x" * 2001}])
    errors = orch.validate_definition(d)
    assert errors and any("上限" in e for e in errors), errors


def test_n2_oversized_default_rejected_at_resolve():
    # N2：触发层对最终值（显式值与默认值）统一做长度校验
    d = _base_definition(params=[{"name": "week", "type": "string",
                                  "default": "x" * 2001}])
    values, err = orch._resolve_trigger_params(d, None)
    assert values == {} and "上限" in err


def test_n2_oversized_default_no_instance(monkeypatch):
    # N2：省略参数时超长默认值 → 定义校验即拒绝，不创建实例行
    d = _base_definition(params=[{"name": "week", "type": "string",
                                  "default": "x" * 2001}],
                         task_template="周报 {{week}}")
    _prepare(monkeypatch, d)
    out = asyncio.run(orch.run_orchestration("param-demo", "", params=None))
    assert "编排未执行" in out, out
    assert _instance_count() == 0


@pytest.mark.parametrize("bad", [[], "", 0])
def test_n4_non_dict_params_rejected_direct(bad):
    # N4：falsy 非字典 params 不得被归一为 {}（直接函数契约，类型检查在前）
    d = _base_definition(params=[{"name": "week", "type": "string"}])
    values, err = orch._resolve_trigger_params(d, bad)
    assert values == {} and "对象" in err
