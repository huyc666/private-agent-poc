"""编排定义：解析、校验、内容哈希（v0.17.44，PLAN 5.1，spec §2.1/AC1）。

红测试口径：定义层任何非法输入都必须以结构化结果拒绝，不抛裸异常；
安全清单/危险工具/绝对路径在加载期即 fail-closed。
"""
import pytest

from server import config
from server import orchestration as orch


def _valid_definition() -> dict:
    return {
        "name": "research-demo",
        "version": "1",
        "description": "调研编排",
        "max_parallel": 2,
        "roles": [
            {"id": "planner", "name": "规划", "prompt": "拆解任务", "tools": []},
            {"id": "worker", "name": "调研", "prompt": "执行子任务",
             "tools": ["read_text_file"]},
            {"id": "aggregator", "name": "汇总", "prompt": "汇总结果", "tools": []},
        ],
        "steps": [
            {"id": "plan", "role": "planner", "mode": "single"},
            {"id": "research", "role": "worker", "mode": "parallel",
             "fanout": {"from": "plan", "limit": 2}, "inputs": ["plan"]},
        ],
        "aggregate": {"role": "aggregator", "inputs": ["research"]},
    }


def test_valid_mixed_modes_definition_passes_and_has_hash():
    result = orch.load_definition_text("name: research-demo\nversion: '1'\n"
                                       "roles: []\nsteps: []\n")
    # 空 roles/steps 应拒绝；换成完整定义走同一入口
    result = orch.load_definition_text(
        "name: research-demo\n"
        "version: '1'\n"
        "roles:\n"
        "  - id: planner\n"
        "    prompt: 拆解\n"
        "    tools: []\n"
        "steps:\n"
        "  - id: plan\n"
        "    role: planner\n"
        "    mode: single\n")
    assert result["status"] == "ok"
    assert result["definition"]["name"] == "research-demo"
    assert len(result["hash"]) >= 16


def test_hash_is_stable_and_changes_with_content():
    a = _valid_definition()
    b = dict(a)
    b["description"] = "另一份"
    assert orch.definition_hash(a) == orch.definition_hash(a)
    assert orch.definition_hash(a) != orch.definition_hash(b)


def test_load_definition_from_file_keeps_snapshot_and_hash(tmp_path):
    path = tmp_path / "demo.yaml"
    path.write_text(
        "name: demo\n"
        "version: '1'\n"
        "roles:\n"
        "  - id: only\n"
        "    prompt: 做一件事\n"
        "    tools: []\n"
        "steps:\n"
        "  - id: run\n"
        "    role: only\n"
        "    mode: single\n",
        encoding="utf-8")
    loaded = orch.load_definition(path)
    assert loaded["status"] == "ok"
    assert loaded["hash"] == orch.definition_hash(loaded["definition"])


def test_yaml_parse_error_is_structured():
    result = orch.load_definition_text("name: demo\nroles: [\n")
    assert result["status"] == "error"
    assert result["error"]
    assert "Traceback" not in result["error"]


def test_empty_roles_or_steps_rejected():
    base = _valid_definition()
    base["roles"] = []
    assert any("角色" in e for e in orch.validate_definition(base))
    base = _valid_definition()
    base["steps"] = []
    assert any("步骤" in e for e in orch.validate_definition(base))


def test_duplicate_role_id_rejected():
    data = _valid_definition()
    data["roles"].append({"id": "planner", "prompt": "重复", "tools": []})
    assert any("角色 id" in e for e in orch.validate_definition(data))


def test_empty_prompt_rejected():
    data = _valid_definition()
    data["roles"][0]["prompt"] = "   "
    assert any("prompt" in e for e in orch.validate_definition(data))


def test_unknown_role_reference_rejected():
    data = _valid_definition()
    data["steps"][0]["role"] = "missing"
    assert any("未定义角色" in e for e in orch.validate_definition(data))


def test_duplicate_step_id_rejected():
    data = _valid_definition()
    data["steps"].append({"id": "plan", "role": "planner", "mode": "single"})
    assert any("步骤 id" in e for e in orch.validate_definition(data))


def test_self_and_forward_inputs_rejected():
    data = _valid_definition()
    data["steps"][0]["inputs"] = ["plan"]
    assert any("inputs" in e for e in orch.validate_definition(data))
    data = _valid_definition()
    data["steps"][0]["inputs"] = ["research"]
    assert any("inputs" in e for e in orch.validate_definition(data))


def test_fanout_must_reference_previous_step():
    data = _valid_definition()
    data["steps"][1]["fanout"]["from"] = "research"
    assert any("fanout" in e for e in orch.validate_definition(data))


def test_max_parallel_over_global_ceiling_rejected():
    data = _valid_definition()
    data["max_parallel"] = orch.GLOBAL_MAX_PARALLEL + 1
    errors = orch.validate_definition(data)
    assert any(str(orch.GLOBAL_MAX_PARALLEL) in e for e in errors)


def test_too_many_roles_or_steps_rejected():
    data = _valid_definition()
    data["roles"] = [{"id": f"r{i}", "prompt": "p", "tools": []}
                     for i in range(orch.MAX_ROLES + 1)]
    data["steps"] = [{"id": f"s{i}", "role": "r0", "mode": "single"}
                     for i in range(orch.MAX_STEPS + 1)]
    errors = orch.validate_definition(data)
    assert any("角色数" in e for e in errors)
    assert any("步骤数" in e for e in errors)


def test_unknown_tool_rejected():
    data = _valid_definition()
    data["roles"][1]["tools"] = ["totally_unknown_tool"]
    assert any("totally_unknown_tool" in e for e in orch.validate_definition(data))


@pytest.mark.parametrize("tool_name", sorted(orch.FORBIDDEN_TOOL_NAMES))
def test_forbidden_tool_rejected(tool_name):
    data = _valid_definition()
    data["roles"][1]["tools"] = [tool_name]
    errors = orch.validate_definition(data)
    assert any(tool_name in e and "危险" in e for e in errors)


def test_absolute_path_in_prompt_rejected_without_echoing_path():
    data = _valid_definition()
    data["roles"][0]["prompt"] = "读取 C:\\Users\\alice\\secret.txt 并返回"
    errors = orch.validate_definition(data)
    assert any("绝对路径" in e for e in errors)
    assert not any("C:\\Users" in e for e in errors)


def test_absolute_unix_path_in_inputs_meta_rejected():
    data = _valid_definition()
    data["roles"][1]["prompt"] = "见 /etc/passwd"
    errors = orch.validate_definition(data)
    assert any("绝对路径" in e for e in errors)
    assert not any("/etc/passwd" in e for e in errors)


def test_unsupported_on_error_semantics_rejected():
    data = _valid_definition()
    data["on_error"] = "retry"
    errors = orch.validate_definition(data)
    assert any("on_error" in e for e in errors)


def test_bundled_examples_validate():
    for name in ("research-demo.yaml", "review-demo.yaml"):
        path = config.PROJECT_ROOT / "orchestrations" / name
        loaded = orch.load_definition(path)
        assert loaded["status"] == "ok", loaded["error"]


def test_unknown_top_level_key_rejected():
    data = _valid_definition()
    data["web_editor"] = True
    assert any("web_editor" in e for e in orch.validate_definition(data))


# ---- 校验边角（覆盖率与 fail-closed 口径） ----

def test_non_mapping_yaml_rejected():
    result = orch.load_definition_text("- 一项\n- 另一项\n")
    assert result["status"] == "error"
    assert "对象" in result["error"]


def test_non_object_role_and_step_rejected():
    data = _valid_definition()
    data["roles"].append("not-a-role")
    data["steps"].append("not-a-step")
    errors = orch.validate_definition(data)
    assert any("角色必须是对象" in e for e in errors)
    assert any("步骤必须是对象" in e for e in errors)


def test_missing_ids_and_bad_types_rejected():
    data = _valid_definition()
    data["version"] = ""
    data["max_parallel"] = "many"
    data["roles"][0].pop("id")
    data["roles"][1]["tools"] = "read_text_file"
    data["steps"][0].pop("id")
    data["steps"][1]["inputs"] = "plan"
    data["steps"][1]["fanout"]["limit"] = 0
    errors = orch.validate_definition(data)
    assert any("version" in e for e in errors)
    assert any("max_parallel" in e for e in errors)
    assert any("缺少 id" in e for e in errors)
    assert any("tools 必须是字符串列表" in e for e in errors)
    assert any("inputs 必须是字符串列表" in e for e in errors)
    assert any("fanout.limit" in e for e in errors)


def test_invalid_mode_rejected():
    data = _valid_definition()
    data["steps"][0]["mode"] = "map"
    assert any("mode" in e for e in orch.validate_definition(data))


def test_unknown_nested_fields_rejected():
    data = _valid_definition()
    data["roles"][0]["model"] = "gpt"
    data["steps"][0]["retry"] = 1
    data["aggregate"]["dedupe"] = True
    errors = orch.validate_definition(data)
    assert any("model" in e for e in errors)
    assert any("retry" in e for e in errors)
    assert any("dedupe" in e for e in errors)


def test_invalid_aggregate_shapes_rejected():
    data = _valid_definition()
    data["aggregate"] = "planner"
    assert any("aggregate 必须是对象" in e for e in orch.validate_definition(data))
    data["aggregate"] = {"role": "missing", "inputs": "research"}
    errors = orch.validate_definition(data)
    assert any("未定义角色" in e for e in errors)
    assert any("aggregate.inputs" in e for e in errors)
    data["aggregate"] = {"role": "aggregator", "inputs": ["nope"]}
    assert any("aggregate.inputs" in e for e in orch.validate_definition(data))


def test_serial_step_with_fanout_rejected():
    data = _valid_definition()
    data["steps"][0]["fanout"] = {"from": "plan"}
    assert any("不应声明 fanout" in e for e in orch.validate_definition(data))


def test_parse_fanout_json_lines_and_fallback():
    assert orch._parse_fanout('["甲","乙"]', 2, "任务") == ["甲", "乙"]
    assert orch._parse_fanout("x", 4, "任务") == ["任务"]
    lines = "1. 子任务甲乙\n2. 子任务乙丙\n- 子任务丙丁"
    assert orch._parse_fanout(lines, 3, "任务") == ["子任务甲乙", "子任务乙丙", "子任务丙丁"]
    # 非法 JSON 数组退回按行解析
    assert orch._parse_fanout('[oops] 子任务甲乙', 2, "任务") == ["[oops] 子任务甲乙"]


def test_content_of_handles_non_string():
    assert orch._content_of("纯文本") == "纯文本"
    assert orch._content_of(123) == "123"


def test_pack_meta_failure_falls_back_to_builtin_only(monkeypatch):
    from server import packs
    monkeypatch.setattr(packs, "enabled_packs",
                        lambda: (_ for _ in ()).throw(RuntimeError("bad")))
    names = set(orch.available_tools())
    assert set(orch.SAFE_BUILTIN_TOOL_NAMES).issubset(names)
