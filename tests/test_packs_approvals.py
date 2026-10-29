"""PLAN 3.2 包与审批面板测试（spec：docs/specs/3.2-packs-approvals.md）。

单测覆盖 AC 的服务端可自动化部分；页面呈现为 s6 人工测试项（AC1 四页签
可见性、AC3 操作按钮显隐文案、AC5 按角色渲染页签/拒绝视图、AC9 懒加载）：

- AC1 GET /api/admin/packs：admin 可得全量包列表（平台包+领域包），字段完整
- AC2 端点启停即时生效（列表/挂载链）+ STATE_DB 持久化（重启模拟仍停用）
- AC3 服务端 fail-closed：平台包停用 400、status≠ok 启用 400、不存在 404
- AC4 停用包/启用包写审计（who/when/what）
- AC6 /api/approvals 可见性口径（user 仅自己发起、approver/admin 全部）+ 字段
- AC7 approver 决议链路：批准/拒绝 → 列表状态即时更新 + 审计可查
- AC8 拒绝路径：/api/admin/packs* 三端点未登录 401 / user·approver 403 /
  开放模式 400；user 调 POST /api/approve 403（口径沿用现状）
"""
import sqlite3

import pytest
import yaml

from server import approval, audit, auth, config, packs, usage

TOOL_SRC = 'def greet(name: str) -> str:\n    """问候"""\n    return f"hi {name}"\n'
SKILL_MD = "---\nname: demo\n---\n按需执行"


# ---------------------------------------------------------------- 夹具与工具

@pytest.fixture
def admin_client(client_factory):
    """admin 身份的 TestClient（带鉴权头）。"""
    key = auth.add_user("root", role="admin")
    assert key
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def _mk_pack(name: str, platform: bool = False, files: dict | None = None,
             **manifest_extra):
    """在隔离 PACKS_DIR 落一个包：manifest 补充项直入 pack.yaml，files 为
    相对路径→内容（工具/技能/提示词文件）。"""
    d = config.PACKS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "0.1.0",
                "description": f"{name} 测试领域包",
                "core_version": f">={packs.CORE_VERSION}"}
    if platform:
        manifest["platform"] = True
    manifest.update(manifest_extra)
    (d / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    for rel, text in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    packs.invalidate_scan_cache()
    return d


def _client_with(username: str, role: str, client_factory):
    key = auth.add_user(username, role=role)
    assert key
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def _approvals(c) -> dict:
    """当前客户端视角的审批单字典（aid → 记录）。"""
    return {x["aid"]: x for x in c.get("/api/approvals").json()["approvals"]}


# ---------------------------------------------------------------- AC1 包列表

def test_ac1_admin_packs_list_full_fields(admin_client):
    """admin 拉全量包列表：平台包与领域包都在，管理视图字段完整。"""
    _mk_pack("plat-core", platform=True)
    _mk_pack("domain-ops", tools=["tools/greet.py"], skills=["skills/demo"],
             prompt="prompt.md", allowed_roles=["user", "approver"],
             files={"tools/greet.py": TOOL_SRC, "skills/demo/SKILL.md": SKILL_MD,
                    "prompt.md": "领域提示词"})
    r = admin_client.get("/api/admin/packs")
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()["packs"]}
    assert {"plat-core", "domain-ops"} <= set(by_name)
    dom = by_name["domain-ops"]
    for f in ("name", "version", "description", "status", "enabled", "platform",
              "tools", "skills", "trust", "allowed_roles"):
        assert f in dom, f"管理视图缺字段 {f}"
    assert dom["version"] == "0.1.0" and dom["status"] == "ok"
    assert dom["enabled"] is True and dom["platform"] is False
    assert dom["tools"] == ["greet"] and dom["skills"] == ["demo"]
    assert set(dom["allowed_roles"]) == {"user", "approver"}  # 顺序由解析器定
    plat = by_name["plat-core"]
    assert plat["platform"] is True and plat["enabled"] is True   # 平台包恒启用


# ---------------------------------------------------------------- AC2 启停与持久化

def test_ac2_disable_enable_roundtrip(admin_client):
    """端点启停即时生效：列表 enabled 翻转 + 挂载链（提示词片段）随动。"""
    _mk_pack("p1", tools=["tools/greet.py"], prompt="prompt.md",
             files={"tools/greet.py": TOOL_SRC, "prompt.md": "领域提示词"})
    assert packs.prompt_fragments() == "领域提示词"
    r = admin_client.post("/api/admin/packs/p1/disable")
    assert r.status_code == 200 and r.json()["ok"] is True
    view = {p["name"]: p for p in
            admin_client.get("/api/admin/packs").json()["packs"]}
    assert view["p1"]["enabled"] is False            # 列表即时变「已停用」
    assert not packs.is_enabled("p1")
    assert packs.prompt_fragments() == ""            # 挂载链：片段即时移除
    r = admin_client.post("/api/admin/packs/p1/enable")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert packs.is_enabled("p1")
    assert packs.prompt_fragments() == "领域提示词"    # 再启用恢复


def test_ac2_disable_persisted_across_reload(admin_client):
    """Q2 核心决策：停用落 STATE_DB（disabled_packs 表），重启（重新加载）不丢。"""
    _mk_pack("p1")
    assert admin_client.post("/api/admin/packs/p1/disable").json()["ok"] is True
    conn = sqlite3.connect(config.STATE_DB)
    rows = conn.execute("SELECT name FROM disabled_packs").fetchall()
    conn.close()
    assert [x[0] for x in rows] == ["p1"]            # 禁用名单已写库
    # 模拟重启：内存禁用集合清空后按启动流程重载 → 仍停用
    packs._disabled.clear()
    packs.load_disabled_packs()
    assert not packs.is_enabled("p1")
    # 启用同样持久：重载后已启用
    assert admin_client.post("/api/admin/packs/p1/enable").json()["ok"] is True
    packs._disabled.clear()
    packs.load_disabled_packs()
    assert packs.is_enabled("p1")


# ---------------------------------------------------------------- AC3 fail-closed

def test_ac3_platform_pack_disable_refused_400(admin_client):
    """平台包停用被拒（前端隐藏入口只是体验层，服务端是防线）。"""
    _mk_pack("plat-core", platform=True)
    r = admin_client.post("/api/admin/packs/plat-core/disable")
    assert r.status_code == 400
    assert "不可禁用" in r.json()["error"]
    assert packs.is_enabled("plat-core")             # 平台包不受影响


def test_ac3_broken_pack_enable_refused_400(admin_client):
    """status≠ok 的包不可启用（挂载坏包 = 把故障引进运行时）。"""
    _mk_pack("broken", requires_env=["PA_TEST_MISSING_ENV_XYZ"])
    r = admin_client.post("/api/admin/packs/broken/enable")
    assert r.status_code == 400
    assert "不可启用" in r.json()["error"]


def test_ac3_unknown_pack_toggle_404(admin_client):
    """操作不存在的包：404（与 3.1 用户端点「目标不存在」口径一致）。"""
    assert admin_client.post("/api/admin/packs/ghost/disable").status_code == 404
    assert admin_client.post("/api/admin/packs/ghost/enable").status_code == 404


# ---------------------------------------------------------------- AC4 启停审计

def test_ac4_pack_toggle_audited(admin_client):
    """包启停写审计：who（root）/ what（停用包·启用包）/ target（包名）。"""
    _mk_pack("p1")
    admin_client.post("/api/admin/packs/p1/disable")
    admin_client.post("/api/admin/packs/p1/enable")
    off = [x for x in audit.query(action="停用包", limit=10)]
    on = [x for x in audit.query(action="启用包", limit=10)]
    assert any(x["actor"] == "root" and x["target"] == "p1" for x in off)
    assert any(x["actor"] == "root" and x["target"] == "p1" for x in on)


# ---------------------------------------------------------------- AC6 审批列表口径

def test_ac6_approvals_visibility_and_fields(client_factory):
    """面板数据口径：user 仅自己发起、approver/admin 全部；字段完整。"""
    usage.current_user.set("alice")
    a1 = approval.create_approval("delete_workspace_file", {"path": "a1.txt"})
    a2 = approval.create_approval("kb_rebuild", {})
    usage.current_user.set("bob")
    a3 = approval.create_approval("delete_workspace_file", {"path": "b.txt"})

    alice = _client_with("alice", "user", client_factory)
    aids = {x["aid"] for x in alice.get("/api/approvals").json()["approvals"]}
    assert {a1, a2} <= aids and a3 not in aids      # user 只见自己发起

    carol = _client_with("carol", "approver", client_factory)
    recs = _approvals(carol)
    assert {a1, a2, a3} <= set(recs)                # approver 全局池
    rec = recs[a1]
    for f in ("aid", "tool", "args", "status", "created", "user_id",
              "decided_by", "decided", "executed_at"):
        assert f in rec, f"审批列表缺字段 {f}"
    assert rec["status"] == "pending" and rec["user_id"] == "alice"
    assert rec["tool"] == "delete_workspace_file"


# ---------------------------------------------------------------- AC7 决议链路

def test_ac7_approver_decide_updates_list_and_audit(client_factory):
    """PLAN 主验收的服务端链路：批准/拒绝 → 列表状态即时更新 + 审计可查。"""
    usage.current_user.set("alice")
    ok_aid = approval.create_approval("delete_workspace_file", {"path": "ok.txt"})
    no_aid = approval.create_approval("delete_workspace_file", {"path": "no.txt"})
    carol = _client_with("carol", "approver", client_factory)

    r = carol.post("/api/approve", json={"approval_id": ok_aid, "approve": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = carol.post("/api/approve", json={"approval_id": no_aid, "approve": False})
    assert r.status_code == 200 and r.json()["ok"] is True

    recs = _approvals(carol)
    assert recs[ok_aid]["status"] == "approved"     # 决议后状态即时更新
    assert recs[ok_aid]["decided_by"] == "carol" and recs[ok_aid]["decided"]
    assert recs[no_aid]["status"] == "rejected"
    # 审计与审批历史双通道（Q4）：who/when/what 可查
    assert any(x["actor"] == "carol" and x["target"] == ok_aid
               for x in audit.query(action="审批通过", limit=10))
    assert any(x["actor"] == "carol" and x["target"] == no_aid
               for x in audit.query(action="审批驳回", limit=10))


# ---------------------------------------------------------------- AC8 拒绝路径

def test_ac8_packs_endpoints_no_credentials_401(client_factory):
    """未登录调包管理三端点：401。"""
    c = client_factory()
    assert c.get("/api/admin/packs").status_code == 401
    assert c.post("/api/admin/packs/p1/disable").status_code == 401
    assert c.post("/api/admin/packs/p1/enable").status_code == 401


def test_ac8_packs_endpoints_user_approver_403(client_factory):
    """user/approver 调包管理三端点：403（前端页签隐藏只是体验层）。"""
    for role in ("user", "approver"):
        c = _client_with(f"someone-{role}", role, client_factory)
        assert c.get("/api/admin/packs").status_code == 403, role
        assert c.post("/api/admin/packs/p1/disable").status_code == 403, role
        assert c.post("/api/admin/packs/p1/enable").status_code == 403, role


def test_ac8_packs_endpoints_open_mode_400(client_factory, monkeypatch):
    """开放模式调包管理三端点：400（无用户体系，管理端点不可用）。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    c = client_factory()
    assert c.get("/api/admin/packs").status_code == 400
    assert c.post("/api/admin/packs/p1/disable").status_code == 400
    assert c.post("/api/admin/packs/p1/enable").status_code == 400
    assert "开放模式" in c.get("/api/admin/packs").json()["error"]


def test_ac8_user_approve_403(client_factory):
    """user 决议审批：403 forbidden（v0.17.9 口径沿用，堵自审自批）。"""
    usage.current_user.set("alice")
    aid = approval.create_approval("delete_workspace_file", {"path": "x.txt"})
    alice = _client_with("alice", "user", client_factory)
    r = alice.post("/api/approve", json={"approval_id": aid, "approve": True})
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert approval.pending_count() == 1            # 记录未被改动


# ------------------------------------------------- 评审修复回归（v0.17.31）

def test_review1_routing_by_structure_not_message(admin_client):
    """评审①回归：状态码来自结构化结果，不靠文案子串——目录名含「不存在」
    的 error 包停用应 200 且记审计（此前文案匹配误判 404 且漏审计）。"""
    bad = "broken-不存在-demo"   # NAME_PATTERN 不匹配 → error 包，name=目录名
    d = config.PACKS_DIR / bad
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text("not-a-valid: [yaml", encoding="utf-8")
    packs.invalidate_scan_cache()
    r = admin_client.post(f"/api/admin/packs/{bad}/disable")
    assert r.status_code == 200 and r.json()["ok"] is True   # 修复前：404
    assert not packs.is_enabled(bad)
    assert any(x["target"] == bad for x in audit.query(action="停用包", limit=5))
    # 既有路由不受影响：正常包名不存在 → 仍 404
    assert admin_client.post("/api/admin/packs/ghost/disable").status_code == 404


def test_review2_persist_failure_rolls_back_500(admin_client, monkeypatch):
    """评审②回归：写库失败 → 端点 500、内存态回滚（重启复活防线）、不记审计。"""
    monkeypatch.setattr(packs, "_persist_disabled",
                        lambda name, disabled: False)
    _mk_pack("p1")
    r = admin_client.post("/api/admin/packs/p1/disable")
    assert r.status_code == 500                      # 修复前：200 假成功
    assert "写库失败" in r.json()["error"]
    assert packs.is_enabled("p1")                    # 内存回滚：保持启用
    assert not [x for x in audit.query(action="停用包", limit=5)
                if x["target"] == "p1"]              # 未成功不记审计
    # enable 侧对称：先真实停用（恢复 _persist_disabled），再验证启用回滚
    monkeypatch.setattr(packs, "_persist_disabled",
                        lambda name, disabled: True)
    assert admin_client.post("/api/admin/packs/p1/disable").status_code == 200
    monkeypatch.setattr(packs, "_persist_disabled",
                        lambda name, disabled: False)
    assert admin_client.post("/api/admin/packs/p1/enable").status_code == 500
    assert not packs.is_enabled("p1")                # 内存回滚：保持停用
