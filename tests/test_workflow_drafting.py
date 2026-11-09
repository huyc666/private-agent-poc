"""4.3 文档生成流程草稿：AC1/AC2/AC4/AC6/AC6b/AC7/AC8 红测试。

红测试口径（docs/specs/4.3-doc-to-workflow.md）：
- 只按 spec 契约写断言，不写实现；未实现的入口经 getattr 暴露为
  ``pytest.fail``（failed），不使用模块顶层 import（避免收集错误）。
- AC6 只测既有 ``validate_definition`` 的结构拦截能力，预计本文件内已绿；
  红的是「生成链路」（AC1/AC2/AC4/AC6b/AC7/AC8）。
- 守护用例 ``test_guard_*`` 与 AC 编号解耦：``SAFE_*`` 基线一致性在实现前
  即应绿，新增登记项（四项工具名进禁止清单、跨域目录收口）在实现前为红。
"""
import asyncio
import hashlib
import inspect
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage

from server import audit, auth, config, orchestration as orch, tools_admin, usage

DRAFT_TOOL = "draft_orchestration_from_doc"
DRAFT_ID_RE = re.compile(r"drf-[0-9a-f]{12}")
_FAIL_MARKERS = ("拒绝", "失败", "不允许", "不存在", "为空", "不可用", "只能",
                 "不具备", "无法")


# ---------------------------------------------------------------- 通用夹具/工具

class _FakeLLM:
    """按序回放的替身模型；调用次数即生成/修复预算证据。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, messages, config=None):
        self.calls.append({"messages": list(messages), "config": config})
        if not self.replies:
            raise AssertionError("模型调用次数超出预期（生成 + 修复最多 2 次）")
        text = self.replies.pop(0)
        if isinstance(text, BaseException):
            raise text
        return AIMessage(content=text)


@pytest.fixture
def draft_llm(monkeypatch):
    """把替身模型装到 orchestration.STATE.llm（与 run_orchestration 同口径）。"""
    def _install(replies):
        fake = _FakeLLM(replies)
        monkeypatch.setattr(orch.STATE, "llm", fake)
        return fake
    return _install


@pytest.fixture
def as_alice(login_as):
    login_as("alice", role="user")
    usage.current_identity.set(usage.Identity(
        username="alice", role="user", department="finance",
        auth_source="bearer"))
    return "alice"


def _login(username, role="user", department="finance"):
    auth.add_user(username, role=role, department=department)
    usage.current_user.set(username)
    usage.current_identity.set(usage.Identity(
        username=username, role=role, department=department,
        auth_source="bearer"))
    return username


def _call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


def _require(module, name):
    fn = getattr(module, name, None)
    if fn is None:
        pytest.fail(f"未实现：{module.__name__}.{name}（4.3 spec 契约）")
    return fn


def _draft_fn():
    return _require(orch, DRAFT_TOOL)


def _scope(username):
    return config.user_scope(username)


def _write_upload(username, name, text):
    d = Path(config.UPLOADS_DIR) / _scope(username)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def _uploads_rel(username, name):
    return f"uploads/{_scope(username)}/{name}"


def _drafts_root():
    root = getattr(config, "ORCHESTRATION_DRAFTS_DIR", None)
    if root is None:
        root = Path(config.TOOL_WORKSPACE) / "orchestration_drafts"
    return Path(root)


def _draft_files():
    root = _drafts_root()
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _one_meta():
    metas = list(_drafts_root().rglob("*.meta.json"))
    assert len(metas) == 1, f"应恰好一份草稿元数据，实际 {metas}"
    return json.loads(metas[0].read_text(encoding="utf-8"))


def _assert_rejected(text):
    assert isinstance(text, str), f"工具应返回文本，实际 {type(text)}"
    assert any(m in text for m in _FAIL_MARKERS), f"应拒绝/失败但返回：{text!r}"


def _assert_no_draft_assets():
    assert _draft_files() == [], f"失败路径不得落盘：{_draft_files()}"


def _definition(name="doc-flow", roles=None, steps=None, aggregate=True):
    roles = roles or [
        {"id": "intake", "name": "受理", "prompt": "阅读并整理业务要求", "tools": []},
        {"id": "reviewer", "name": "审核", "prompt": "按制度条款审核",
         "tools": ["read_text_file"]},
    ]
    steps = steps or [
        {"id": "intake", "role": "intake", "mode": "single"},
        {"id": "review", "role": "reviewer", "mode": "single", "inputs": ["intake"]},
    ]
    d = {"name": name, "version": "1", "description": "文档生成流程",
         "max_parallel": 2, "roles": roles, "steps": steps}
    if aggregate:
        d["aggregate"] = {"role": "reviewer", "inputs": ["review"]}
    return d


def _reply(definition, notes="生成说明：按文档角色生成"):
    return ("```yaml\n"
            + yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)
            + "```\n" + notes)


def _all_text(fake):
    parts = []
    for call in fake.calls:
        for m in call["messages"]:
            parts.append(str(getattr(m, "content", m)))
    return "\n".join(parts)


def _instance_rows():
    path = Path(config.STATE_DB)
    if not path.exists():
        return []
    conn = sqlite3.connect(str(path))
    try:
        try:
            return conn.execute(
                "SELECT definition_name, status FROM workflow_instances").fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def _seed_draft(scope, draft_id, status="pending", owner="alice"):
    root = _drafts_root() / scope
    root.mkdir(parents=True, exist_ok=True)
    definition = _definition()
    raw = yaml.safe_dump(definition, allow_unicode=True, sort_keys=False).encode()
    (root / f"{draft_id}.yaml").write_bytes(raw)
    meta = {
        "draft_id": draft_id, "owner": owner, "owner_role": "user",
        "scope": scope, "source_path": f"uploads/{scope}/seed.md",
        "source_sha256": "0" * 64, "source_chars": 4,
        "definition_name": definition["name"], "definition_version": "1",
        "definition_hash": hashlib.sha256(raw).hexdigest(),
        "status": status, "created_at": time.time(),
        "generation_notes": "", "validation": {"status": "ok", "errors": []},
    }
    (root / f"{draft_id}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- AC1 源文档读取与隔离

def test_ac1_own_document_is_accepted_and_sent_to_model(as_alice, draft_llm):
    text = "制度正文：受理后由审核岗在两个工作日内复核。"
    _write_upload("alice", "制度.md", text)
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "制度.md"))
    assert isinstance(result, str)
    assert text in _all_text(fake), "文档正文必须进入生成提示"
    assert len(_draft_files()) == 2, "成功时应有 .yaml + .meta.json 两个文件"


def test_ac1_cross_domain_document_rejected_without_model_call(as_alice, draft_llm):
    _write_upload("bob", "制度.md", "他人的制度文档")
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("bob", "制度.md"))
    _assert_rejected(result)
    assert fake.calls == [], "校验失败不得调用模型"
    _assert_no_draft_assets()


@pytest.mark.parametrize("shape", [
    "C:\\Windows\\system.ini",
    "/etc/passwd",
    "uploads/../etc/passwd",
    "../uploads/x.md",
    "etc/passwd",
    "uploads/only-one-segment.md",
])
def test_ac1_illegal_path_shapes_rejected(as_alice, draft_llm, shape):
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), shape)
    _assert_rejected(result)
    assert fake.calls == [], "路径非法时不得调用模型"
    _assert_no_draft_assets()


@pytest.mark.parametrize("name,text", [
    (".env", "SECRET=1"),
    ("state.db", "binary"),
    ("missing.md", None),
    ("empty.md", ""),
])
def test_ac1_sensitive_missing_and_empty_documents_rejected(as_alice, draft_llm,
                                                           name, text):
    if text is not None:
        _write_upload("alice", name, text)
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", name))
    _assert_rejected(result)
    assert fake.calls == []
    _assert_no_draft_assets()


def test_ac1_length_limit_exact_ok_and_one_over_rejected(as_alice, draft_llm):
    fake = draft_llm([_reply(_definition()), _reply(_definition())])
    ok_text = "甲" * 20_000
    _write_upload("alice", "limit-ok.md", ok_text)
    accepted = _call(_draft_fn(), _uploads_rel("alice", "limit-ok.md"))
    assert isinstance(accepted, str)
    assert not accepted.startswith("生成失败"), f"恰好 20000 字符应接受：{accepted}"
    assert len(_draft_files()) == 2

    _write_upload("alice", "limit-over.md", "乙" * 20_001)
    rejected = _call(_draft_fn(), _uploads_rel("alice", "limit-over.md"))
    _assert_rejected(rejected)
    assert "20001" not in rejected and "乙" * 100 not in rejected, "错误文案不回显正文"


def test_ac1_non_text_extension_rejected(as_alice, draft_llm):
    _write_upload("alice", "manual.pdf", "%PDF-1.4")
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "manual.pdf"))
    _assert_rejected(result)
    assert "docx" in result or "txt" in result or "md" in result, "应提示先转文本格式"
    assert fake.calls == []


def test_ac1_docx_path_normalizes_to_converted_md(as_alice, draft_llm):
    text = "规范正文：采购申请由部门负责人审批。"
    _write_upload("alice", "规范.docx.md", text)
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "规范.docx"))
    assert isinstance(result, str) and len(_draft_files()) == 2, \
        f"传 .docx 且存在 .docx.md 时应归一：{result}"
    assert text in _all_text(fake)


def test_ac1_prompt_masks_secret_values(as_alice, draft_llm):
    secret = "sk-live-abcdef123456"
    _write_upload("alice", "凭据.md", f"接口说明\napi_key: {secret}\n")
    fake = draft_llm([_reply(_definition())])
    _call(_draft_fn(), _uploads_rel("alice", "凭据.md"))
    text = _all_text(fake)
    assert secret not in text, "文档中的明文密钥不得进入模型"
    assert "已脱敏" in text or "***" in text


def test_guard_cross_scope_covers_drafts_and_personal_definitions(monkeypatch):
    """P1-1：草稿目录与存量个人定义目录一并纳入跨域拒绝清单。"""
    from server import tools_builtin
    monkeypatch.setattr(auth, "enabled", lambda: True)
    monkeypatch.setattr(auth, "current_role", lambda: "user")
    other = "other-scope"
    assert tools_builtin._cross_scope_denied(
        ("orchestration_drafts", other, "drf-000000000000.yaml")), \
        "user 读取他人草稿必须被拒"
    assert tools_builtin._cross_scope_denied(
        ("orchestrations_personal", other, "x.yaml")), \
        "user 读取他人个人定义必须被拒"
    own = tools_builtin._file_scope()
    assert tools_builtin._cross_scope_denied(
        ("orchestration_drafts", own, "x.yaml")) is None


# ---------------------------------------------------------------- AC2 生成与草稿落盘

def test_ac2_draft_assets_and_disk_byte_hashes(as_alice, draft_llm):
    text = "制度：受理岗登记，审核岗复核，汇总岗归档。"
    doc = _write_upload("alice", "流程.md", text)
    loaded = _definition()
    fake = draft_llm([_reply(loaded)])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    assert isinstance(result, str) and DRAFT_ID_RE.search(result), \
        f"成功应返回草稿 ID（drf-<12 hex>）：{result}"

    files = _draft_files()
    assert len(files) == 2
    yaml_path = next(p for p in files if p.suffix == ".yaml")
    meta = _one_meta()
    for key in ("draft_id", "owner", "owner_role", "scope", "source_path",
                "source_sha256", "source_chars", "definition_name",
                "definition_version", "definition_hash", "status",
                "created_at", "generation_notes", "validation"):
        assert key in meta, f"草稿元数据缺字段 {key}"
    assert DRAFT_ID_RE.fullmatch(meta["draft_id"])
    assert meta["owner"] == "alice"
    assert meta["scope"] == _scope("alice")
    assert meta["status"] == "pending"
    assert meta["source_path"] == _uploads_rel("alice", "流程.md")
    assert meta["source_sha256"] == hashlib.sha256(doc.read_bytes()).hexdigest()
    assert meta["source_chars"] == len(text)

    raw = yaml_path.read_bytes()
    expect = hashlib.sha256(raw).hexdigest()
    got = meta["definition_hash"]
    assert got == expect or (len(got) >= 16 and expect.startswith(got)), \
        "definition_hash 必须按草稿 YAML 磁盘字节计算"

    drafted = yaml.safe_load(raw)
    assert orch.validate_definition(drafted) == []
    assert "doc-flow" not in orch.list_orchestrations()
    assert list(Path(config.ORCHESTRATIONS_DIR).glob("*.yaml")) == []
    assert fake.calls and len(fake.calls) == 1


def test_ac2_invalid_yaml_triggers_exactly_one_repair(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    fake = draft_llm(["这不是 YAML：[[[", _reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    assert isinstance(result, str) and len(_draft_files()) == 2
    assert len(fake.calls) == 2, "非法 YAML 必须恰好触发一次修复调用"


def test_ac2_repair_failure_leaves_no_files(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    fake = draft_llm(["不是 YAML", "仍不是 YAML"])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    _assert_rejected(result)
    assert len(fake.calls) == 2, "总调用不得超过 2 次（1 生成 + 1 修复）"
    _assert_no_draft_assets()


def test_ac2_mock_mode_unavailable_and_zero_files(as_alice):
    _write_upload("alice", "流程.md", "制度正文")
    # conftest 默认 MOCK_LLM=true 且 STATE.llm 为空
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    assert "Mock" in result or "真实模型" in result, f"Mock 模式需明确提示：{result}"
    _assert_no_draft_assets()


def test_ac2_generation_notes_text_only_no_code(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    notes = "生成说明：工具缺口——需要天气查询能力（用途：…；输入输出建议：…）。"
    draft_llm([_reply(_definition(), notes)])
    _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    meta = _one_meta()
    assert "工具缺口" in meta["generation_notes"]
    assert "def " not in meta["generation_notes"]
    assert "import " not in meta["generation_notes"]
    assert not list(_drafts_root().rglob("*.py")), "草稿目录不得出现任何 .py"


# ---------------------------------------------------------------- AC4 草稿惰性

def test_ac4_draft_not_listed_not_executable_and_no_instance(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    fake = draft_llm([_reply(_definition())])
    _call(_draft_fn(), _uploads_rel("alice", "流程.md"))

    listing = orch.list_orchestrations()
    assert "doc-flow" not in listing, "草稿不得出现在可用定义列表"
    assert orch.resolve_definition("doc-flow", usage.current_identity.get())["status"] \
        != "ok"

    run_result = _call(orch.run_orchestration, "doc-flow", "执行一次")
    assert "不存在" in run_result or "无权限" in run_result, run_result
    assert _instance_rows() == [], "草稿不得产生实例行"
    assert orch._TOOL_CACHE == {}, "草稿不得进入工具缓存"
    assert len(fake.calls) == 1


def test_ac4_draft_directory_absent_from_upload_listing(as_alice):
    _write_upload("bob", "他人文档.md", "正文")
    other_scope = _scope("bob")
    root = _drafts_root() / other_scope
    root.mkdir(parents=True, exist_ok=True)
    (root / "drf-abcdefabcdef.yaml").write_text("name: x\n", encoding="utf-8")
    from server import tools_builtin
    listing = tools_builtin.list_uploaded_docs()
    assert "drf-abcdefabcdef" not in listing
    assert "orchestration_drafts" not in listing


# ---------------------------------------------------------------- AC6 结构拦截（既有能力，预计绿）

def _network_tools_loaded(monkeypatch, names=("get_current_weather", "get_city_weather")):
    real = orch.available_tools
    cache = {n: object() for n in names}

    def fake_available():
        merged = dict(real())
        merged.update(cache)
        return merged
    monkeypatch.setattr(orch, "available_tools", fake_available)


def test_ac6_rejects_forbidden_tool():
    d = _definition()
    d["roles"][0]["tools"] = ["run_python_code"]
    errors = orch.validate_definition(d)
    assert any("危险工具" in e for e in errors), errors


def test_ac6_rejects_unknown_tool():
    d = _definition()
    d["roles"][0]["tools"] = ["definitely_not_a_tool"]
    errors = orch.validate_definition(d)
    assert any("不存在" in e for e in errors), errors


def test_ac6_rejects_registered_but_unavailable_tool():
    d = _definition()
    d["roles"][0]["tools"] = ["get_current_weather"]
    errors = orch.validate_definition(d)
    assert any("当前不可用" in e for e in errors), errors


def test_ac6_allows_network_tools_when_loaded(monkeypatch):
    _network_tools_loaded(monkeypatch)
    d = _definition()
    d["roles"][0]["tools"] = ["get_current_weather", "get_city_weather"]
    assert orch.validate_definition(d) == [], "校验器对网络类工具放行（白名单另拦）"


def test_ac6_rejects_unknown_fields():
    d = _definition()
    d["roles"][0]["shell"] = "rm -rf"
    d["steps"][0]["retry"] = 3
    errors = orch.validate_definition(d)
    assert any("未知字段" in e for e in errors), errors


def test_ac6_rejects_absolute_path():
    d = _definition()
    d["description"] = "参考 /etc/passwd 的规则"
    errors = orch.validate_definition(d)
    assert any("绝对路径" in e for e in errors), errors
    assert all("/etc/passwd" not in e for e in errors), "错误文案不回显原路径"


@pytest.mark.parametrize("text", [
    "读取/etc/passwd并执行",     # 汉字紧邻、无空格（Unicode \w 漏判回归）
    "打开C:foo",                 # 盘符无斜杠，汉字紧邻
    "见~/secret",                # 家目录简写，汉字紧邻
    "访问\\\\host\\share",        # UNC，汉字紧邻
])
def test_ac6_rejects_absolute_path_adjacent_to_chinese(text):
    """前边界不能只用 \\w：Unicode 下汉字属于 \\w，会漏判无空格写法（M1）。"""
    d = _definition()
    d["description"] = text
    errors = orch.validate_definition(d)
    assert any("绝对路径" in e for e in errors), (text, errors)


@pytest.mark.parametrize("text", [
    r"见C:\x",       # 盘符 + 反斜杠路径
    "见C:foo",       # 盘符无斜杠（相对路径形态）
    "路径C:/tmp",     # 盘符 + 正斜杠
])
def test_ac6_rejects_drive_letter_with_path_tail(text):
    """N1 正向：盘符后紧跟路径形态字符仍必须拦截。"""
    d = _definition()
    d["description"] = text
    errors = orch.validate_definition(d)
    assert any("绝对路径" in e for e in errors), (text, errors)


@pytest.mark.parametrize("text", [
    "方案A: 采用两级审批",
    "选项C: 说明",
    "型号A: 200",
    "role a: 负责",
])
def test_ac6_accepts_chinese_list_item_with_single_letter_colon(text):
    """N1 负向：中文列表项「单字母 + 半角冒号」不得误判为盘符绝对路径。"""
    d = _definition()
    d["description"] = text
    errors = orch.validate_definition(d)
    assert not any("绝对路径" in e for e in errors), (text, errors)


def test_ac6_rejects_role_and_step_limits():
    roles = [{"id": f"r{i}", "prompt": "做事", "tools": []} for i in range(9)]
    d = _definition(roles=roles,
                    steps=[{"id": f"s{i}", "role": "r0", "mode": "single"}
                           for i in range(9)], aggregate=False)
    errors = orch.validate_definition(d)
    assert any("角色数超过上限" in e for e in errors), errors
    assert any("步骤数超过上限" in e for e in errors), errors


def test_ac6_rejects_blank_and_whitespace_prompt():
    d = _definition()
    d["roles"][0]["prompt"] = "   "
    errors = orch.validate_definition(d)
    assert any("prompt 不能为空" in e for e in errors), errors


def test_ac6_rejects_overlong_prompt():
    d = _definition()
    d["roles"][0]["prompt"] = "长" * 4001
    errors = orch.validate_definition(d)
    assert any("4000" in e for e in errors), errors


def test_ac6_rejects_undefined_step_role():
    d = _definition()
    d["steps"][1]["role"] = "ghost"
    errors = orch.validate_definition(d)
    assert any("未定义角色" in e for e in errors), errors


@pytest.mark.parametrize("ref_kind", ["missing", "self", "forward"])
def test_ac6_rejects_illegal_inputs_references(ref_kind):
    d = _definition()
    if ref_kind == "missing":
        d["steps"][1]["inputs"] = ["ghost"]
    elif ref_kind == "self":
        d["steps"][1]["inputs"] = ["review"]
    else:
        d["steps"][0]["inputs"] = ["review"]
    errors = orch.validate_definition(d)
    assert any("只能引用前序步骤" in e for e in errors), errors


def test_ac6_rejects_aggregate_inputs_missing_step():
    d = _definition()
    d["aggregate"]["inputs"] = ["ghost"]
    errors = orch.validate_definition(d)
    assert any("aggregate.inputs" in e for e in errors), errors


# ---------------------------------------------------------------- AC6b 白名单强制验收 + 执行期口径

def test_ac6b_network_tool_violation_triggers_exactly_one_repair(
        as_alice, draft_llm, monkeypatch):
    _network_tools_loaded(monkeypatch)
    _write_upload("alice", "流程.md", "制度正文")
    bad = _definition()
    bad["roles"][1]["tools"] = ["get_city_weather"]
    fake = draft_llm([_reply(bad), _reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    assert isinstance(result, str) and len(_draft_files()) == 2, result
    assert len(fake.calls) == 2, "越界必须触发恰好一次修复调用"
    yaml_path = next(p for p in _draft_files() if p.suffix == ".yaml")
    assert "get_city_weather" not in yaml_path.read_text(encoding="utf-8"), \
        "不得把越界工具静默裁剪后当作原定义保存（应以修复结果为准）"


def test_ac6b_network_tool_still_outside_fails_without_assets(
        as_alice, draft_llm, monkeypatch):
    _network_tools_loaded(monkeypatch)
    _write_upload("alice", "流程.md", "制度正文")
    bad = _definition()
    bad["roles"][1]["tools"] = ["get_city_weather"]
    fake = draft_llm([_reply(bad), _reply(bad)])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    _assert_rejected(result)
    assert len(fake.calls) == 2
    _assert_no_draft_assets()


def test_ac6b_empty_upstream_output_marked(as_alice):
    """上游空产出显式标记（依赖 PLAN 5.2 引擎改动；未实现前为红）。"""
    role = {"id": "reviewer", "prompt": "复核上游结果", "tools": []}
    messages = orch._step_messages(
        role, usage.current_identity.get(), "总任务",
        {"intake": ""}, ["intake"])
    text = "\n".join(str(getattr(m, "content", m)) for m in messages)
    assert "上游步骤 intake 输出为空" in text, \
        "空产出必须显式标记，不得静默消失（且不改变 ok 判定）"


def test_ac6b_no_python_and_no_custom_tool_call(as_alice, draft_llm, monkeypatch):
    """Q2=A：不生成任何 .py，也不调用 create_custom_tool。"""
    from server import tools_admin
    called = []
    monkeypatch.setattr(tools_admin, "create_custom_tool",
                        lambda *a, **k: called.append((a, k)) or "called")
    _write_upload("alice", "流程.md", "制度正文，需要天气能力")
    draft_llm([_reply(_definition(), "生成说明：工具缺口——天气查询（不含代码）")])
    _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    assert called == []
    assert not list(_drafts_root().rglob("*.py"))


# ---------------------------------------------------------------- AC7 失败与审计口径

def test_ac7_model_exception_fixed_text_without_path(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    draft_llm([RuntimeError("C:\\Windows\\secret\\x.txt")])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    _assert_rejected(result)
    assert "RuntimeError" in result, "固定文案应含异常类型名"
    assert "C:\\Windows" not in result and "secret" not in result
    _assert_no_draft_assets()


def test_ac7_empty_model_output_rejected(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    draft_llm(["", ""])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    _assert_rejected(result)
    _assert_no_draft_assets()


def test_ac7_yaml_parse_failure_rejected(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    draft_llm(["```yaml\nname: [unclosed\n```", "```yaml\n- not: a definition\n```"])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    _assert_rejected(result)
    _assert_no_draft_assets()


def test_ac7_audit_records_draft_creation(as_alice, draft_llm):
    secret = "sk-live-abcdef123456"
    _write_upload("alice", "流程.md", f"制度正文\napi_key: {secret}\n")
    draft_llm([_reply(_definition())])
    _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    meta = _one_meta()
    records = audit.query(actor="alice", limit=50)
    assert records, "草稿创建必须留审计"
    blob = json.dumps(records, ensure_ascii=False)
    assert meta["draft_id"] in blob, "审计应记录 draft_id"
    assert secret not in blob, "审计不得记录密钥值"
    assert "制度正文" not in blob, "审计不得记录文档正文"
    assert str(_drafts_root()) not in blob, "审计不得记录绝对路径"


# ---------------------------------------------------------------- AC8 资源上限

def test_ac8_quota_counts_pending_only(as_alice, draft_llm):
    scope = _scope("alice")
    for i in range(20):
        _seed_draft(scope, f"drf-{i:012x}", status="confirmed" if i % 2 else "discarded")
    _write_upload("alice", "流程.md", "制度正文")
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    assert isinstance(result, str) and not result.startswith("生成失败"), \
        f"confirmed/discarded 不计入配额：{result}"
    assert len(list(_drafts_root().rglob("*.meta.json"))) == 21
    assert len(fake.calls) == 1


def test_ac8_pending_quota_exhausted_rejected(as_alice, draft_llm):
    scope = _scope("alice")
    for i in range(20):
        _seed_draft(scope, f"drf-{i:012x}", status="pending")
    _write_upload("alice", "流程.md", "制度正文")
    fake = draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    _assert_rejected(result)
    assert "20" in result or "上限" in result
    assert len(list(_drafts_root().rglob("*.meta.json"))) == 20
    assert fake.calls == []


def test_ac8_discard_frees_quota(as_alice, draft_llm):
    scope = _scope("alice")
    for i in range(20):
        _seed_draft(scope, f"drf-{i:012x}", status="pending")
    discard = _require(orch, "discard_orchestration_draft")
    _call(discard, "drf-000000000000")
    _write_upload("alice", "流程.md", "制度正文")
    draft_llm([_reply(_definition())])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    assert isinstance(result, str) and not result.startswith("生成失败"), \
        f"丢弃后应释放名额：{result}"


@pytest.mark.parametrize("text", [
    "参考 /srv/app/config 的规则",
    "参考 /data/input 的规则",
    "参考 /mnt/disk 的规则",
    "参考 ~/secret.txt 的规则",
    "参考 C:foo 的规则",
    r"参考 \\host\share\x 的规则",
])
def test_ac6_rejects_absolute_path_shapes(text):
    """安全复审 L10：/srv /data /mnt、~/、盘符无斜杠与 UNC 都按绝对路径拒绝。"""
    d = _definition()
    d["description"] = text
    errors = orch.validate_definition(d)
    assert any("绝对路径" in e for e in errors), (text, errors)
    assert all("passwd" not in e for e in errors), "错误文案不回显原路径"


def test_guard_draft_prompt_only_lists_identity_visible_tools(monkeypatch):
    """安全复审 L8：系统提示展示「身份可见 ∩ 白名单」，不是完整白名单。"""
    ident = usage.Identity(username="alice", role="user", department="finance",
                           auth_source="bearer")
    monkeypatch.setattr(orch, "visible_tool_names", lambda _i: {"read_text_file"})
    messages = orch._draft_messages("正文", "", ident)
    system = str(messages[0].content)
    assert "read_text_file" in system
    assert "calculator" not in system, "不可用工具不得出现在生成提示里"


def test_guard_reserved_anonymous_username_rejected():
    """安全复审 L1：anonymous 为保留身份，不允许注册（避免草稿落 global 域）。"""
    assert auth.add_user("anonymous", role="user") is None
    assert auth.add_user("ANONYMOUS", role="approver") is None


def test_ac1_audit_records_source_path(as_alice, draft_llm):
    """安全复审 L9：创建草稿的审计必须带来源文档相对路径（spec §2.5）。"""
    _write_upload("alice", "制度.md", "制度正文")
    draft_llm([_reply(_definition())])
    _call(_draft_fn(), _uploads_rel("alice", "制度.md"))
    rows = audit.query(action="生成流程草稿")
    assert rows, "应写创建审计"
    assert "source=uploads/" in rows[0]["detail"]
    assert "制度.md" in rows[0]["detail"]


def test_guard_draft_source_path_rejects_bad_shapes():
    for bad in ("", "uploads/a/b/c.md", "a/b.md", "uploads/../b.md"):
        assert orch._draft_source_path(bad) is None


def test_guard_split_draft_output_empty_yaml_block():
    assert orch._split_draft_output("```yaml\n\n```") == ("", "", "模型输出为空")


def test_guard_whitelist_ignores_non_dict_role():
    ident = usage.Identity(username="alice", role="user", department="finance",
                           auth_source="bearer")
    definition = {"roles": ["不是对象", {"id": "intake", "tools": []}]}
    assert orch._draft_whitelist_violations(definition, ident) == []


def test_guard_invoke_draft_llm_without_model(monkeypatch):
    monkeypatch.setattr(orch.STATE, "llm", None)
    with pytest.raises(RuntimeError):
        asyncio.run(orch._invoke_draft_llm([]))


def test_guard_tools_admin_draft_wrappers_delegate(monkeypatch):
    """四个工具入口只是转发：覆盖 tools_admin 的薄封装分支。"""
    async def fake_draft(doc_path, requirements=""):
        return f"draft:{doc_path}:{requirements}"

    async def fake_confirm(draft_id):
        return f"confirm:{draft_id}"

    monkeypatch.setattr(orch, "draft_orchestration_from_doc", fake_draft)
    monkeypatch.setattr(orch, "confirm_orchestration_draft", fake_confirm)
    monkeypatch.setattr(orch, "list_orchestration_drafts", lambda: "list-ok")
    monkeypatch.setattr(orch, "discard_orchestration_draft",
                        lambda did: f"discard:{did}")
    assert asyncio.run(tools_admin.draft_orchestration_from_doc(
        "uploads/a/b.md", "补充")) == "draft:uploads/a/b.md:补充"
    assert tools_admin.list_orchestration_drafts() == "list-ok"
    assert asyncio.run(tools_admin.confirm_orchestration_draft(
        "drf-aaaaaaaaaaaa")) == "confirm:drf-aaaaaaaaaaaa"
    assert tools_admin.discard_orchestration_draft(
        "drf-bbbbbbbbbbbb") == "discard:drf-bbbbbbbbbbbb"


def test_guard_draft_limit_rechecked_inside_lock(as_alice, draft_llm, monkeypatch):
    """并发窗口：早检通过、拿锁后复检必须再次拒绝。"""
    _write_upload("alice", "制度.md", "制度正文")
    draft_llm([_reply(_definition())])
    calls = {"n": 0}

    def fake_count(scope):
        calls["n"] += 1
        return 0 if calls["n"] == 1 else orch.MAX_DRAFTS_PER_SCOPE

    monkeypatch.setattr(orch, "_pending_draft_count", fake_count)
    out = _call(_draft_fn(), _uploads_rel("alice", "制度.md"))
    assert "上限" in out, out


def test_guard_draft_write_failure_cleans_up(as_alice, draft_llm, monkeypatch):
    """落盘失败时清理半成品并返回固定文案。"""
    _write_upload("alice", "制度.md", "制度正文")
    draft_llm([_reply(_definition())])

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(orch.os, "replace", boom)
    out = _call(_draft_fn(), _uploads_rel("alice", "制度.md"))
    assert out.startswith("生成失败：草稿写入失败"), out


def test_guard_draft_read_failure_returns_fixed_text(as_alice, draft_llm, monkeypatch):
    """通过路径校验后读盘失败（文件被占用/权限变化）→ 固定失败文案。"""
    _write_upload("alice", "制度.md", "制度正文")
    draft_llm([_reply(_definition())])

    def boom(self, *args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr(Path, "read_bytes", boom)
    out = _call(_draft_fn(), _uploads_rel("alice", "制度.md"))
    assert out.startswith("生成失败：文档不存在或不可读"), out


def test_guard_draft_repair_call_failure_returns_fixed_text(as_alice, draft_llm):
    """首轮输出不合约触发修复；修复轮模型异常按类型名返回固定文案。"""
    _write_upload("alice", "制度.md", "制度正文")
    draft_llm(["没有 yaml 代码块", RuntimeError("boom")])
    out = _call(_draft_fn(), _uploads_rel("alice", "制度.md"))
    assert out.startswith("生成失败：模型调用异常（RuntimeError）"), out


def test_guard_visible_tool_names_excludes_forbidden(monkeypatch):
    """可见工具集合过滤禁止项（FORBIDDEN 与可用集合污染时也不放行）。"""
    monkeypatch.setattr(orch, "available_tools",
                        lambda: {"read_text_file": object(),
                                 "run_orchestration": object()})
    monkeypatch.setattr(orch, "_pack_tool_meta", lambda: {})
    ident = usage.Identity(username="alice", role="user", department="finance",
                           auth_source="bearer")
    names = orch.visible_tool_names(ident)
    assert "read_text_file" in names
    assert "run_orchestration" not in names


def test_ac8_oversize_yaml_rejected(as_alice, draft_llm):
    _write_upload("alice", "流程.md", "制度正文")
    big = _definition()
    big["description"] = "长" * 70_000
    draft_llm([_reply(big), _reply(big)])
    result = _call(_draft_fn(), _uploads_rel("alice", "流程.md"))
    _assert_rejected(result)
    _assert_no_draft_assets()


# ---------------------------------------------------------------- 守护断言

def test_guard_safe_tool_lists_unchanged():
    """SAFE_* 与基线逐字一致（生成层白名单不得放宽执行层安全清单）。"""
    assert orch.SAFE_BUILTIN_TOOL_NAMES == (
        "read_text_file", "list_uploaded_docs", "save_output_file")
    assert orch.SAFE_PACK_TOOL_NAMES == (
        "search_common_knowledge", "calculator", "get_current_time",
        "search_hr_knowledge", "search_finance_knowledge",
        "get_current_weather")
    assert orch.SAFE_MCP_TOOL_NAMES == ("get_city_weather",)


def test_guard_forbidden_tools_include_new_draft_tools():
    expected = {
        "delete_workspace_file", "create_skill", "delete_skill", "update_skill",
        "create_custom_tool", "delete_custom_tool", "update_custom_tool",
        "run_custom_tool", "enable_pack", "disable_pack", "create_pack",
        "delete_pack", "sign_pack", "run_python_code", "orchestrate_subtasks",
        "create_orchestration", "run_orchestration", "add_user", "delete_user",
        "list_users", "delete_uploaded_file", "create_schedule", "delete_schedule",
        "draft_orchestration_from_doc", "confirm_orchestration_draft",
        "list_orchestration_drafts", "discard_orchestration_draft",
    }
    assert set(orch.FORBIDDEN_TOOL_NAMES) == expected


def test_guard_visible_tool_names_hides_role_restricted_pack_tool(monkeypatch):
    """P1-8：按身份屏蔽工具名的唯一判定在 visible_tool_names。"""
    visible = _require(orch, "visible_tool_names")
    monkeypatch.setattr(orch, "available_tools",
                        lambda: {"restricted_tool": object()})
    monkeypatch.setattr(orch, "_pack_tool_meta", lambda: {
        "restricted_tool": {"name": "restricted-pack",
                            "allowed_roles": ["approver"],
                            "allowed_departments": []}})
    user_ident = usage.Identity(username="alice", role="user",
                                department="finance", auth_source="bearer")
    approver = usage.Identity(username="carol", role="approver",
                              department="finance", auth_source="bearer")
    assert "restricted_tool" not in visible(user_ident)
    assert "restricted_tool" in visible(approver)


# ------------------------------------------------- AC10 新增代码覆盖率补齐（工具入口/分支）
# spec AC10 要求新增代码覆盖率 ≥90%：以下用例覆盖模型工具入口、审批分支
# 与草稿元数据异常路径（此前的红测试聚焦生成链路与端点主路径）。

def test_tool_list_drafts_empty_then_own(as_alice):
    """list 工具：无草稿固定文案；有草稿列出本人条目。"""
    assert "当前没有流程草稿" in _call(orch.list_orchestration_drafts)
    _seed_draft(_scope("alice"), "drf-000000000001")
    text = _call(orch.list_orchestration_drafts)
    assert "drf-000000000001" in text and "doc-flow" in text


def test_tool_discard_personal_and_errors(as_alice):
    """discard 工具：personal 直通；已丢弃/不存在返回失败文案。"""
    _seed_draft(_scope("alice"), "drf-000000000002")
    assert "已丢弃" in _call(orch.discard_orchestration_draft, "drf-000000000002")
    assert "丢弃失败" in _call(orch.discard_orchestration_draft, "drf-000000000002")
    assert "不存在" in _call(orch.discard_orchestration_draft, "drf-00000000000f")


def test_tool_confirm_personal_returns_pending_text(as_alice):
    """confirm 工具在 personal 域只返回待确认提示，不代替本人确认。"""
    _seed_draft(_scope("alice"), "drf-000000000003")
    text = _call(orch.confirm_orchestration_draft, "drf-000000000003")
    assert "待确认" in text


def test_tool_confirm_global_goes_through_approval(monkeypatch):
    """global 域确认走审批门：审批回调执行后才落盘（此处来源缺失 → 失败文案）。"""
    from server import approval
    _login("carol", role="approver")
    _seed_draft("global", "drf-000000000004", owner="carol")
    seen = []

    def _fake_require(action, payload, title, callback):
        seen.append((action, payload))
        return callback()

    monkeypatch.setattr(approval, "require_approval", _fake_require)
    result = _call(orch.confirm_orchestration_draft, "drf-000000000004")
    assert seen and seen[0][0] == "confirm_orchestration_draft"
    assert "确认失败" in result, "来源缺失时审批回调内应返回失败，不静默成功"


def test_tool_discard_global_goes_through_approval(monkeypatch):
    """global 域丢弃同样走审批门，不在工具内直通。"""
    from server import approval
    _login("carol", role="approver")
    _seed_draft("global", "drf-000000000005", owner="carol")
    seen = []
    monkeypatch.setattr(approval, "require_approval",
                        lambda a, p, t, cb: seen.append(a) or cb())
    result = _call(orch.discard_orchestration_draft, "drf-000000000005")
    assert seen == ["discard_orchestration_draft"]
    assert "已丢弃全局流程草稿" in result


def test_draft_requirements_appended_to_prompt(as_alice, draft_llm):
    """requirements 非空时追加到生成提示（AC1 补充要求通道）。"""
    _write_upload("alice", "制度.md", "制度正文")
    fake = draft_llm([_reply(_definition())])
    _call(_draft_fn(), _uploads_rel("alice", "制度.md"), "必须包含审核角色")
    assert "必须包含审核角色" in _all_text(fake)


def test_draft_requirements_over_limit_rejected(as_alice, draft_llm):
    """requirements 超限显式拒绝（与文档 20k 同口径），不调用模型、不落盘。"""
    _write_upload("alice", "制度.md", "制度正文")
    fake = draft_llm([])
    out = _call(_draft_fn(), _uploads_rel("alice", "制度.md"),
                "x" * (orch.MAX_REQUIREMENTS_CHARS + 1))
    _assert_rejected(out)
    assert "上限" in out, f"超限需明确提示上限：{out}"
    assert fake.calls == [], "超限在模型调用前拒绝"
    _assert_no_draft_assets()


def test_draft_empty_and_missing_doc_path_rejected(as_alice, draft_llm):
    """空路径与不存在的文档在模型调用前拒绝。"""
    fake = draft_llm([_reply(_definition())])
    assert "不能为空" in _call(_draft_fn(), "")
    result = _call(_draft_fn(), f"uploads/{_scope('alice')}/不存在.md")
    _assert_rejected(result)
    assert fake.calls == [], "路径校验失败不得调用模型"
    _assert_no_draft_assets()


def test_draft_notes_truncated_to_limit(as_alice, draft_llm):
    """生成说明超长时按 MAX_GENERATION_NOTES 截断落库。"""
    _write_upload("alice", "制度.md", "制度正文")
    draft_llm([_reply(_definition(), "生成说明：" + "旧" * 1500)])
    _call(_draft_fn(), _uploads_rel("alice", "制度.md"))
    meta = _one_meta()
    assert len(meta["generation_notes"]) <= orch.MAX_GENERATION_NOTES


def test_confirm_invalid_status_rejected(as_alice):
    """元数据状态不在三态内 → 409 invalid_status。"""
    _seed_draft(_scope("alice"), "drf-000000000006", status="weird")
    result = orch.confirm_draft("drf-000000000006", usage.Identity(
        username="alice", role="user", department="finance",
        auth_source="bearer"))
    assert result["status"] == 409 and result["error"] == "invalid_status"


def test_confirm_and_detail_reject_missing_yaml(as_alice):
    """草稿 YAML 被外部删除 → 确认 409 draft_missing，详情 404。"""
    _seed_draft(_scope("alice"), "drf-000000000007")
    (_drafts_root() / _scope("alice") / "drf-000000000007.yaml").unlink()
    ident = usage.Identity(username="alice", role="user", department="finance",
                           auth_source="bearer")
    result = orch.confirm_draft("drf-000000000007", ident)
    assert result["status"] == 409 and result["error"] == "draft_missing"
    assert orch.get_draft_detail("drf-000000000007", ident)["status"] == 404


def test_confirm_source_missing_rejected(as_alice):
    """来源文档在确认前消失 → 409 source_missing，不写入可用定义。"""
    _seed_draft(_scope("alice"), "drf-000000000008")
    result = orch.confirm_draft("drf-000000000008", usage.Identity(
        username="alice", role="user", department="finance",
        auth_source="bearer"))
    assert result["status"] == 409 and result["error"] == "source_missing"
    assert not (Path(config.ORCHESTRATIONS_DIR) / "doc-flow.yaml").exists()
