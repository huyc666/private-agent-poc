"""PLAN 2.3 多知识库与权限测试（spec：docs/specs/2.3-kb-permissions.md，AC1~AC12）。

- AC1~AC8：pack.yaml `allowed_departments` 部门白名单——解析 fail-closed、执行层
  拦截、提示层过滤、角色×部门 AND 语义、调岗即时生效
- AC9：定位端点纳入部门维度（部门 A 账号 404 部门 B 库，不暴露库清单）
- AC10：知识库重导入即生效（工具按调用打开索引，无需重启）
- AC11：admin / 开放模式跨部门放行（全局视图）
- AC12：通用库 + 部门库三库并存互不串库；包工具自包含（AST 断言）
"""
import ast
import importlib.util
from pathlib import Path

import pytest
import yaml

from server import auth, config, custom_tools, packs, usage

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PACK_TOOLS = {
    "common": PROJECT_ROOT / "packs/core-knowledge/tools/search_common_knowledge.py",
    "hr": PROJECT_ROOT / "packs/kb-demo/tools/search_hr_knowledge.py",
    "finance": PROJECT_ROOT / "packs/kb-finance-demo/tools/search_finance_knowledge.py",
}
KB_ENV = {"common": "KB_COMMON_DIR", "hr": "KB_HR_DIR", "finance": "KB_FINANCE_DIR"}

TOOL_SRC = ('def greet(name: str) -> str:\n'
            '    """问候"""\n'
            '    return f"hi {name}"\n')

TOOL_SRC2 = ('def greet2(name: str) -> str:\n'
             '    """问候2"""\n'
             '    return f"hi2 {name}"\n')


# ---------------------------------------------------------------- 工具函数


@pytest.fixture
def packs_dir():
    """隔离 PACKS_DIR（conftest isolated_env 已指向 tmp_path）。"""
    return config.PACKS_DIR


def _mk_pack(root, name, roles=None, departments=None, knowledge_bases=None,
             tools=("tools/greet.py",), files=None):
    """落一个带 prompt.md 的包；roles/departments/knowledge_bases=None 表示不配置。"""
    manifest = {"name": name, "version": "0.1.0", "description": f"{name} 包",
                "core_version": ">=0.1.0", "tools": list(tools),
                "prompt": "prompt.md"}
    if roles is not None:
        manifest["allowed_roles"] = roles
    if departments is not None:
        manifest["allowed_departments"] = departments
    if knowledge_bases is not None:
        manifest["knowledge_bases"] = knowledge_bases
    all_files = {"prompt.md": f"{name} 的领域提示词片段",
                 "tools/greet.py": TOOL_SRC}
    all_files.update(files or {})
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    for rel, text in all_files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    packs.invalidate_scan_cache()
    return d


def _login(username, role="user", department=""):
    """建用户（含部门）并切换对话上下文身份。"""
    key = auth.add_user(username, role=role, department=department)
    assert key, f"用户 {username} 创建失败"
    usage.current_user.set(username)
    return key


def _pack_tools() -> dict:
    tools = packs.load_pack_tools()
    assert tools, "包工具未挂载"
    return {t.name: t for t in tools}


def _build_kb(tmp_path, files):
    """落盘文档并建索引（注入假向量，避免触发 fastembed 加载）。"""
    from tests.test_kb_citation import _build_kb as build
    return build(tmp_path, files)


def _load_tool(rel: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_kb_tool(kb, monkeypatch, which):
    monkeypatch.setenv(KB_ENV[which], str(kb))
    return _load_tool(PACK_TOOLS[which], f"kbperm_{which}")


# ---------------------------------------------------------------- AC1~AC3 执行层

def test_ac1_unset_departments_all_allowed(packs_dir):
    _mk_pack(packs_dir, "open-pack")
    _login("alice", role="user", department="hr")
    assert _pack_tools()["greet"].invoke({"name": "bob"}) == "hi bob"
    _login("bob", role="user", department="")     # 未分配部门也不受限制
    assert _pack_tools()["greet"].invoke({"name": "bob"}) == "hi bob"


def test_ac2_dept_in_whitelist_allowed(packs_dir):
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    _login("alice", role="user", department="hr")
    assert _pack_tools()["greet"].invoke({"name": "bob"}) == "hi bob"


def test_ac3_other_dept_call_denied(packs_dir):
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    _login("frank", role="user", department="finance")
    out = _pack_tools()["greet"].invoke({"name": "bob"})
    assert "部门" in out and "hr" in out


# ---------------------------------------------------------------- AC4 提示层

def test_ac4_prompt_fragment_filtered_by_department(packs_dir):
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    _mk_pack(packs_dir, "open-pack")
    hr_view = packs.prompt_fragments(role="user", department="hr")
    assert "hr-pack 的领域提示词片段" in hr_view
    assert "open-pack 的领域提示词片段" in hr_view
    fin_view = packs.prompt_fragments(role="user", department="finance")
    assert "hr-pack 的领域提示词片段" not in fin_view
    assert "open-pack 的领域提示词片段" in fin_view


# ---------------------------------------------------------------- AC5/AC6 边界与 fail-closed

def test_ac5_unassigned_department_denied(packs_dir):
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    _login("nobody", role="user", department="")
    out = _pack_tools()["greet"].invoke({"name": "bob"})
    assert "部门" in out


def test_ac6_departments_empty_list_fail_closed(packs_dir):
    _mk_pack(packs_dir, "bad-pack", departments=[])
    p = packs.scan_packs()[0]
    assert p["status"] == "error" and "部门" in p["error"]


def test_ac6_departments_not_list_fail_closed(packs_dir):
    _mk_pack(packs_dir, "bad-pack", departments="hr")
    p = packs.scan_packs()[0]
    assert p["status"] == "error"


def test_ac6_departments_blank_or_overlong_fail_closed(packs_dir):
    _mk_pack(packs_dir, "bad-pack", departments=["hr", "  "])
    p = packs.scan_packs()[0]
    assert p["status"] == "error"
    _mk_pack(packs_dir, "long-pack", departments=["x" * 65])
    p = packs.scan_packs()[0]
    assert p["status"] == "error"


def test_ac6_departments_control_char_fail_closed(packs_dir):
    _mk_pack(packs_dir, "ctl-pack", departments=["hr\t1"])
    p = packs.scan_packs()[0]
    assert p["status"] == "error"


# ---------------------------------------------------------------- AC7 组合语义 AND

def test_ac7_roles_and_departments_both_required(packs_dir):
    _mk_pack(packs_dir, "both-pack", roles=["approver"], departments=["hr"])
    _login("u1", role="user", department="hr")          # 部门满足、角色不满足
    assert "仅限" in _pack_tools()["greet"].invoke({"name": "b"})
    _login("u2", role="approver", department="finance")  # 角色满足、部门不满足
    assert "部门" in _pack_tools()["greet"].invoke({"name": "b"})
    _login("u3", role="approver", department="hr")       # 两者都满足
    assert _pack_tools()["greet"].invoke({"name": "b"}) == "hi b"


def test_ac7_scope_denied_helper_semantics(packs_dir):
    _login("alice", role="user", department="hr")
    assert custom_tools.pack_scope_denied("p", ["approver"], ["hr"]) is not None
    assert custom_tools.pack_scope_denied("p", [], ["finance"]) is not None
    assert custom_tools.pack_scope_denied("p", [], ["hr"]) is None
    assert custom_tools.pack_scope_denied("p", [], []) is None       # 未配置不限制
    assert custom_tools.pack_scope_denied("p", [], ["HR"]) is None   # 归一化大小写
    assert custom_tools.pack_scope_denied("p", [], [" hr "]) is None  # 归一化空格


def test_ac7_department_value_normalized(packs_dir):
    _mk_pack(packs_dir, "hr-pack", departments=["HR"])
    _login("alice", role="user", department=" hr ")
    assert _pack_tools()["greet"].invoke({"name": "b"}) == "hi b"


# ---------------------------------------------------------------- AC8 调岗即时生效

def test_ac8_department_change_takes_effect_next_round(packs_dir):
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    _mk_pack(packs_dir, "fin-pack", departments=["finance"],
             tools=("tools/greet2.py",), files={"tools/greet2.py": TOOL_SRC2})
    _login("alice", role="user", department="hr")
    assert "hr-pack 的领域提示词片段" in packs.prompt_fragments(
        role="user", department=auth.current_department())
    assert auth.set_department("alice", "finance") is True
    # 不重登、不重启：同一会话下一轮即按新部门判定
    dept = auth.current_department()
    assert dept == "finance"
    view = packs.prompt_fragments(role="user", department=dept)
    assert "hr-pack 的领域提示词片段" not in view
    assert "fin-pack 的领域提示词片段" in view
    # 两包工具名不同（greet/greet2），端到端 invoke 断言无同名合并歧义：
    # 调岗后人事包工具被拒、财务包工具放行
    tools = _pack_tools()
    assert "部门" in tools["greet"].invoke({"name": "b"})
    assert tools["greet2"].invoke({"name": "b"}) == "hi2 b"
    # 直接判定：当前部门下 hr-pack 被拒、fin-pack 放行
    assert custom_tools.pack_scope_denied("hr-pack", [], ["hr"]) is not None
    assert custom_tools.pack_scope_denied("fin-pack", [], ["finance"]) is None


def test_ac8_current_department_helper(packs_dir):
    _login("alice", role="user", department="hr")
    assert auth.current_department() == "hr"
    _login("anon", role="user", department="")
    assert auth.current_department() == ""


# ---------------------------------------------------------------- AC9 定位端点

@pytest.fixture()
def kb_root(tmp_path):
    root = tmp_path / "kb"
    (root / "hr").mkdir(parents=True)
    (root / "finance").mkdir(parents=True)
    (root / "hr" / "考勤制度.md").write_text("# 考勤\n\n九点前打卡。", encoding="utf-8")
    (root / "finance" / "报销制度.md").write_text("# 报销\n\n差旅据实报销。",
                                          encoding="utf-8")
    return root


@pytest.fixture()
def kb_client(tmp_path, kb_root, client_factory, monkeypatch):
    """按 (角色, 部门) 建用户并返回带鉴权头的 TestClient。"""
    from server import main as server_main
    monkeypatch.setattr(server_main, "_kb_home", lambda: kb_root)
    monkeypatch.setattr(server_main, "_declared_kb_packs", lambda: [
        {"name": "kb-demo", "knowledge_bases": ["hr"],
         "allowed_roles": [], "allowed_departments": ["hr"]},
        {"name": "kb-finance-demo", "knowledge_bases": ["finance"],
         "allowed_roles": [], "allowed_departments": ["finance"]},
    ])

    def _mk(username, role="user", department=""):
        key = auth.add_user(username, role=role, department=department)
        assert key
        c = client_factory()
        c.headers.update({"Authorization": f"Bearer {key}"})
        return c
    return _mk


def test_ac9_other_department_kb_source_404(kb_client):
    c = kb_client("frank", role="user", department="finance")
    r = c.get("/api/kb/source", params={"kb": "finance", "doc": "报销制度.md",
                                        "kind": "line", "a": 1})
    assert r.status_code == 200 and "差旅据实报销" in r.text
    r2 = c.get("/api/kb/source", params={"kb": "hr", "doc": "考勤制度.md",
                                         "kind": "line", "a": 1})
    assert r2.status_code == 404 and "考勤" not in r2.text


def test_ac9_own_department_kb_source_ok(kb_client):
    c = kb_client("alice", role="user", department="hr")
    r = c.get("/api/kb/source", params={"kb": "hr", "doc": "考勤制度.md",
                                        "kind": "line", "a": 1})
    assert r.status_code == 200 and "九点前打卡" in r.text


def test_ac9_unassigned_department_404(kb_client):
    c = kb_client("nobody", role="user", department="")
    r = c.get("/api/kb/source", params={"kb": "hr", "doc": "考勤制度.md",
                                        "kind": "line", "a": 1})
    assert r.status_code == 404


def test_ac11_admin_endpoint_cross_department(kb_client):
    c = kb_client("root1", role="admin", department="")
    for kb, doc in (("hr", "考勤制度.md"), ("finance", "报销制度.md")):
        r = c.get("/api/kb/source", params={"kb": kb, "doc": doc,
                                            "kind": "line", "a": 1})
        assert r.status_code == 200, f"admin 应跨部门放行 {kb}"


# ---------------------------------------------------------------- AC10 重导入即生效

def test_ac10_reimport_takes_effect_without_restart(tmp_path, monkeypatch):
    kb = _build_kb(tmp_path, {"甲制度.md": "# 甲\n\n初始条款内容。"})
    tool = _make_kb_tool(kb, monkeypatch, "hr")
    assert "未找到" in tool.search_hr_knowledge("季度考核")
    # 运行中重导入（build 会重建 kb.db）：同一进程、已加载模块不需重启即生效
    _build_kb(tmp_path, {"乙制度.md": "# 乙\n\n季度考核办法说明。"})
    out = tool.search_hr_knowledge("季度考核")
    assert "未找到" not in out and "季度考核" in out


# ---------------------------------------------------------------- AC11 全局视图

def test_ac11_admin_cross_department_allowed(packs_dir):
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    _login("root1", role="admin", department="finance")
    assert _pack_tools()["greet"].invoke({"name": "b"}) == "hi b"
    assert custom_tools.pack_scope_denied("hr-pack", [], ["hr"]) is None


def test_ac11_open_mode_cross_department_allowed(packs_dir, monkeypatch):
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    monkeypatch.setattr(auth, "enabled", lambda: False)
    usage.current_user.set("anonymous")
    assert _pack_tools()["greet"].invoke({"name": "b"}) == "hi b"
    assert "hr-pack 的领域提示词片段" in packs.prompt_fragments(role="admin")


# ---------------------------------------------------------------- AC12 三库并存与自包含

def test_ac12_three_kbs_coexist_and_isolated(packs_dir):
    _mk_pack(packs_dir, "common-pack")                       # 通用库：不限部门
    _mk_pack(packs_dir, "hr-pack", departments=["hr"])
    _mk_pack(packs_dir, "fin-pack", departments=["finance"])

    hr_view = packs.prompt_fragments(role="user", department="hr")
    assert "common-pack 的领域提示词片段" in hr_view
    assert "hr-pack 的领域提示词片段" in hr_view
    assert "fin-pack 的领域提示词片段" not in hr_view

    fin_view = packs.prompt_fragments(role="user", department="finance")
    assert "common-pack 的领域提示词片段" in fin_view
    assert "fin-pack 的领域提示词片段" in fin_view
    assert "hr-pack 的领域提示词片段" not in fin_view

    info = {p["name"]: p for p in packs.list_packs()}
    assert info["hr-pack"]["allowed_departments"] == ["hr"]
    assert info["common-pack"]["allowed_departments"] == []


def test_ac12_pack_tools_self_contained():
    for which, rel in PACK_TOOLS.items():
        if not rel.exists():
            continue
        tree = ast.parse(rel.read_text(encoding="utf-8"))
        banned = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in ("server", "tools"):
                        banned.append(a.name)
            elif (isinstance(node, ast.ImportFrom) and node.level == 0
                  and node.module and node.module.split(".")[0] in ("server", "tools")):
                banned.append(node.module)
        assert not banned, f"{which} 包工具不得 import server/tools：{banned}"


def test_ac12_finance_pack_declares_department():
    """演示资产：财务部门包声明 knowledge_bases 与 allowed_departments（与实际盘面一致）。"""
    manifest = yaml.safe_load(
        (PROJECT_ROOT / "packs/kb-finance-demo/pack.yaml").read_text(encoding="utf-8"))
    assert manifest["knowledge_bases"] == ["finance"]
    assert manifest["allowed_departments"] == ["finance"]


# ---------------------------------------- PLAN 2.4 一库多包共享（spec：docs/specs/2.4-kb-multi-pack.md）

def test_24_ac7_shared_kb_prompt_tool_per_pack_isolation(packs_dir):
    """AC7：两包共享库 policy——提示片段与工具调用仍按各自包隔离，不受 OR 合并影响。"""
    _mk_pack(packs_dir, "hr-pack", departments=["hr"], knowledge_bases=["policy"])
    _mk_pack(packs_dir, "fin-pack", departments=["finance"], knowledge_bases=["policy"],
             tools=("tools/greet2.py",), files={"tools/greet2.py": TOOL_SRC2})
    view_hr = packs.prompt_fragments(role="user", department="hr")
    assert "hr-pack 的领域提示词片段" in view_hr
    assert "fin-pack 的领域提示词片段" not in view_hr
    view_fin = packs.prompt_fragments(role="user", department="finance")
    assert "fin-pack 的领域提示词片段" in view_fin
    assert "hr-pack 的领域提示词片段" not in view_fin
    _login("alice", role="user", department="hr")
    tools = _pack_tools()
    assert tools["greet"].invoke({"name": "b"}) == "hi b"      # 本包检索工具放行
    assert "部门" in tools["greet2"].invoke({"name": "b"})      # 他包工具执行层拒绝
    _login("carl", role="user", department="finance")
    tools = _pack_tools()
    assert tools["greet2"].invoke({"name": "b"}) == "hi2 b"
    assert "部门" in tools["greet"].invoke({"name": "b"})


def test_24_ac8_shared_kb_endpoint_both_departments(tmp_path, kb_root,
                                                    client_factory, monkeypatch):
    """AC8：定位端点两包同声明库——hr/finance 身份均 200，未分配部门 404。"""
    from server import main as server_main
    (kb_root / "policy").mkdir(exist_ok=True)
    (kb_root / "policy" / "差旅制度.md").write_text("# 差旅\n\n标准 800 元。",
                                                   encoding="utf-8")
    monkeypatch.setattr(server_main, "_kb_home", lambda: kb_root)
    monkeypatch.setattr(server_main, "_declared_kb_packs", lambda: [
        {"name": "hr-pack", "knowledge_bases": ["policy"], "allowed_roles": [],
         "allowed_departments": ["hr"]},
        {"name": "fin-pack", "knowledge_bases": ["policy"], "allowed_roles": [],
         "allowed_departments": ["finance"]}])

    def _mk(username, department=""):
        key = auth.add_user(username, role="user", department=department)
        assert key
        c = client_factory()
        c.headers.update({"Authorization": f"Bearer {key}"})
        return c

    params = {"kb": "policy", "doc": "差旅制度.md", "kind": "line", "a": 1}
    r1 = _mk("p8hr", "hr").get("/api/kb/source", params=params)
    assert r1.status_code == 200 and "标准 800 元" in r1.text
    r2 = _mk("p8fin", "finance").get("/api/kb/source", params=params)
    assert r2.status_code == 200
    r3 = _mk("p8none", "").get("/api/kb/source", params=params)
    assert r3.status_code == 404


def test_24_ac10_shared_kb_same_tool_name_scan_error(packs_dir):
    """AC10：共享库 + 同名工具 → 两包整包 error（fail-closed，错误信息点名三方）；
    消除冲突（检索工具改名）后两包恢复 ok。"""
    _mk_pack(packs_dir, "hr-pack", departments=["hr"], knowledge_bases=["policy"])
    _mk_pack(packs_dir, "fin-pack", departments=["finance"], knowledge_bases=["policy"])
    info = {p["name"]: p for p in packs.list_packs()}
    assert info["hr-pack"]["status"] == "error"
    assert info["fin-pack"]["status"] == "error"
    for name, other in (("hr-pack", "fin-pack"), ("fin-pack", "hr-pack")):
        err = info[name]["error"]
        assert "同名工具" in err and "greet" in err and "policy" in err
        assert other in err  # 错误信息点名对方包
    import shutil
    shutil.rmtree(packs_dir / "fin-pack")
    _mk_pack(packs_dir, "fin-pack", departments=["finance"], knowledge_bases=["policy"],
             tools=("tools/greet2.py",), files={"tools/greet2.py": TOOL_SRC2})
    info = {p["name"]: p for p in packs.list_packs()}
    assert info["hr-pack"]["status"] == "ok"
    assert info["fin-pack"]["status"] == "ok"


def test_24_ac10_no_shared_kb_same_tool_name_warn_only(packs_dir):
    """AC10 反向：无共享库的同名工具维持既有「后加载跳过 + 告警」，不判 error。"""
    _mk_pack(packs_dir, "pack-a", knowledge_bases=["kba"])
    _mk_pack(packs_dir, "pack-b", knowledge_bases=["kbb"])
    info = {p["name"]: p for p in packs.list_packs()}
    assert info["pack-a"]["status"] == "ok"
    assert info["pack-b"]["status"] == "ok"


def test_24_ac10_pack_internal_duplicate_tool_no_false_conflict(packs_dir):
    """AC10 评审回归：单包 tools 清单重复列同一文件 + 共享库他包不同工具名
    → 去重后不误报冲突（修复前会产出「与包「」…」的破损错误信息）。"""
    _mk_pack(packs_dir, "pack-a", knowledge_bases=["policy"],
             tools=("tools/greet.py", "tools/greet.py"))  # 清单重复条目
    _mk_pack(packs_dir, "pack-b", knowledge_bases=["policy"],
             tools=("tools/greet2.py",), files={"tools/greet2.py": TOOL_SRC2})
    info = {p["name"]: p for p in packs.list_packs()}
    assert info["pack-a"]["status"] == "ok"
    assert info["pack-a"]["tools"] == ["greet"]  # 去重后单条
    assert info["pack-b"]["status"] == "ok"
