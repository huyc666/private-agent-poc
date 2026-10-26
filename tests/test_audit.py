"""PLAN 6.3 统一操作审计日志测试（spec：docs/specs/6.3-audit-log.md）。

- AC1 登录成功/失败均落审计（who/when/what + 来源 IP）
- AC2 六类管理动作（建用户/轮换/重置密码/设角色/设部门/吊销）全部可查
- AC3 审批决议落审计（决议人 + 结果）
- AC4 文件上传/删除/下载/技能包导入/工具删除落审计（相对路径，无绝对路径）
- AC5 查询端点权限拒绝路径 + 过滤参数 + CSV 导出
- AC6 容量裁剪；写入失败不阻塞主链路
- AC7 审计不含明文 Key / 服务器绝对路径
"""
import io
import zipfile

import pytest

from server import approval, audit, auth


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def admin_client(client_factory):
    key = auth.add_user("root", role="admin")
    assert key
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def _mk(username, role="user"):
    key = auth.add_user(username, role=role)
    assert key, f"用户 {username} 创建失败"
    return key


def _actions():
    return {r["action"] for r in audit.query(limit=1000)}


def _skill_zip():
    """最小合法技能包（SKILL.md frontmatter 含 name）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("demo-audit-skill/SKILL.md",
                   "---\nname: demo-audit-skill\ndescription: 审计测试用技能\n---\n\n正文")
    return buf.getvalue()


# ---------------------------------------------------------------- AC1 登录审计

def test_ac1_login_success_audited(client_factory):
    _mk("bob")
    assert auth.set_password("bob", "bob-password-1")
    c = client_factory()
    r = c.post("/api/auth/login", json={"username": "bob",
                                        "password": "bob-password-1"})
    assert r.status_code == 200
    recs = [x for x in audit.query(action="登录成功", limit=10)]
    assert any(x["actor"] == "bob" and x["detail"] for x in recs)  # detail 含来源 IP


def test_ac1_login_failure_audited(client_factory):
    _mk("bob")
    assert auth.set_password("bob", "bob-password-1")
    c = client_factory()
    r = c.post("/api/auth/login", json={"username": "bob",
                                        "password": "wrong-password"})
    assert r.status_code == 401
    recs = [x for x in audit.query(action="登录失败", limit=10)]
    assert any(x["actor"] == "bob" and x["detail"] for x in recs)


# ---------------------------------------------------------------- AC2 管理动作审计

def test_ac2_admin_actions_audited(admin_client):
    admin_client.post("/api/admin/users",
                      json={"username": "eve", "role": "user"})
    admin_client.post("/api/admin/users/eve/rotate", json={"days": 30})
    admin_client.post("/api/admin/users/eve/password",
                     json={"password": "eve-password-1"})
    admin_client.post("/api/admin/users/eve/role", json={"role": "approver"})
    admin_client.post("/api/admin/users/eve/department", json={"department": "财务部"})
    admin_client.post("/api/admin/users/eve/revoke", json={})
    got = _actions()
    assert {"创建用户", "轮换 Key", "重置密码", "设角色", "设部门", "吊销账号"} <= got
    # 记录者均为 admin root
    recs = [x for x in audit.query(limit=1000) if x["action"] == "吊销账号"]
    assert recs and recs[-1]["actor"] == "root" and recs[-1]["role"] == "admin"


# ---------------------------------------------------------------- AC3 审批决议审计

def test_ac3_approval_decision_audited(client_factory):
    key = _mk("carol", role="approver")
    aid = approval.create_approval("delete_workspace_file", {"path": "x.txt"})
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    c.post("/api/approve", json={"approval_id": aid, "approve": True})
    recs = [x for x in audit.query(action="审批通过", limit=10)]
    assert any(x["actor"] == "carol" and x["target"] == aid for x in recs)


# ---------------------------------------------------------------- AC4 文件操作审计

def test_ac4_file_ops_audited(admin_client):
    from server import config
    # 上传（raw body + ?name=，多用户分域由服务端按身份路由）
    r = admin_client.post("/api/uploads", params={"name": "报表.txt"},
                          content="季度数据".encode("utf-8"))
    assert r.status_code == 200
    recs = [x for x in audit.query(action="文件上传", limit=10)]
    assert recs and recs[-1]["target"] == "报表.txt"  # 相对路径（无盘符/反斜杠）
    # 删除
    r = admin_client.delete("/api/uploads/报表.txt")
    assert r.status_code == 200
    assert any(x["action"] == "文件删除" and x["target"] == "报表.txt"
               for x in audit.query(limit=20))
    # 下载（/api/files 仅开放 outputs 产出；admin 全局视图域为 shared）
    out_dir = config.OUTPUTS_DIR / "shared"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "结果.txt").write_text("产出内容", encoding="utf-8")
    r = admin_client.get("/api/files/结果.txt")
    assert r.status_code == 200
    # 单段名按身份路由（admin 回退 shared 域），target 记域内相对路径
    assert any(x["action"] == "文件下载" and x["target"].endswith("结果.txt")
               for x in audit.query(limit=20))
    # 技能包导入
    r = admin_client.post("/api/skillpacks", content=_skill_zip(),
                          headers={"Content-Type": "application/zip"})
    assert r.status_code == 200
    recs = [x for x in audit.query(action="技能包导入", limit=10)]
    assert recs and recs[-1]["target"] == "demo-audit-skill"


def test_ac4_tool_delete_audited():
    from server import config, tools_builtin
    up = config.TOOL_WORKSPACE / "uploads" / "shared"
    up.mkdir(parents=True, exist_ok=True)
    (up / "旧数据.txt").write_text("x", encoding="utf-8")
    out = tools_builtin.do_delete("uploads/shared/旧数据.txt")
    assert out.startswith("已删除文件")
    recs = [x for x in audit.query(action="工具删除文件", limit=10)]
    assert recs and recs[-1]["target"] == "uploads/shared/旧数据.txt"


def test_ac4_tool_save_audited():
    """spec A7「工具级文件写/删」的写半边（评审 C1：save_output_file 曾漏接）。
    直接调用无对话上下文 → 域回退 shared，actor 为空（spec 允许工具级空角色）。"""
    from server import tools_builtin
    out = tools_builtin.save_output_file("生成报表.txt", "季度产出内容")
    assert out.startswith("已保存")
    recs = [x for x in audit.query(action="工具保存文件", limit=10)]
    assert recs and recs[-1]["target"] == "outputs/shared/生成报表.txt"


# ---------------------------------------------------------------- AC5 查询端点与权限

def test_ac5_query_with_filters(admin_client):
    _mk("alice")
    admin_client.post("/api/admin/users/alice/role", json={"role": "approver"})
    r = admin_client.get("/api/audit", params={"actor": "root",
                                               "action": "设角色",
                                               "limit": 50})
    assert r.status_code == 200
    recs = r.json()["records"]
    assert recs and all(x["actor"] == "root" and x["action"] == "设角色"
                        for x in recs)
    for x in recs:
        for f in ("ts", "actor", "role", "action", "target", "detail"):
            assert f in x


def test_ac5_export_csv(admin_client):
    _mk("dave")
    # 导出非空路径：先造一条审计记录（v0.17.30 修复回归——空表时循环体
    # 不执行，曾漏掉 time 未导入的 NameError 真机 500）
    audit.log("probe", "admin", "登录成功", detail="127.0.0.1")
    r = admin_client.get("/api/audit/export")
    assert r.status_code == 200
    assert "csv" in r.headers.get("content-type", "")
    body = r.text
    assert "时间,操作人,角色,动作,目标,详情" in body
    assert "probe" in body and "登录成功" in body  # 数据行真实写出


def test_ac5_export_csv_no_line_boundary_split(admin_client):
    """评审 C2 回归防线：actor 含 Unicode 行边界字符（\u2028，可经登录失败
    注入）时，导出不得把一条记录裂成多条残行（曾因 splitlines+join 裂行）。"""
    import csv as _csv
    import io as _io
    audit.log("a\u2028b", "", "登录失败", detail="127.0.0.1")
    r = admin_client.get("/api/audit/export", params={"actor": "a\u2028b"})
    assert r.status_code == 200
    rows = list(_csv.reader(_io.StringIO(r.text.lstrip("\ufeff"))))
    assert rows[0][:2] == ["时间", "操作人"]
    actors = [x[1] for x in rows[1:]]
    assert "a\u2028b" in actors          # 完整保留在单条记录内
    assert "a" not in actors             # 未被裂成残行 "a" / "b"


def test_ac5_user_403(client_factory):
    key = _mk("alice")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    assert c.get("/api/audit").status_code == 403
    assert c.get("/api/audit/export").status_code == 403


def test_ac5_approver_403(client_factory):
    key = _mk("carol", role="approver")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    assert c.get("/api/audit").status_code == 403


def test_ac5_no_credentials_401(client_factory):
    assert client_factory().get("/api/audit").status_code == 401


def test_ac5_open_mode_400(client_factory, monkeypatch):
    from server import config
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    r = client_factory().get("/api/audit")
    assert r.status_code == 400 and "开放模式" in r.json()["error"]


# ---------------------------------------------------------------- AC6 容量与容错

def test_ac6_capacity_trim(admin_client, monkeypatch):
    monkeypatch.setattr(audit, "CAPACITY", 20)
    for i in range(30):
        audit.log("root", "admin", "登录成功", detail=f"压测#{i}")
    total = len(audit.query(limit=1000))
    assert total <= 20
    assert len(audit.query(limit=1000)) == total  # 裁剪后查询稳定
    # 最旧的压测记录已被裁掉
    recs = audit.query(limit=1000)
    assert all("压测#0" not in (x["detail"] or "") for x in recs)


def test_ac6_write_failure_does_not_block(client_factory, monkeypatch):
    _mk("bob")
    assert auth.set_password("bob", "bob-password-1")

    def boom(*a, **kw):
        raise RuntimeError("audit 表不可写（模拟）")

    monkeypatch.setattr(audit, "_execute", boom)
    c = client_factory()
    r = c.post("/api/auth/login", json={"username": "bob",
                                        "password": "bob-password-1"})
    assert r.status_code == 200  # 审计写入失败不阻塞登录主链路


# ---------------------------------------------------------------- AC7 敏感值不落审计

def test_ac7_no_plaintext_key_in_audit(admin_client):
    r = admin_client.post("/api/admin/users", json={"username": "eve"})
    new_key = r.json().get("key", "")
    assert new_key.startswith("pak-")
    all_text = " ".join(
        f'{x["actor"]}{x["role"]}{x["action"]}{x["target"]}{x["detail"]}'
        for x in audit.query(limit=1000))
    assert new_key not in all_text  # 明文 Key 绝不入审计


def test_ac7_no_absolute_path_in_audit(admin_client):
    r = admin_client.post("/api/uploads", params={"name": "绝密.txt"},
                          content="内容".encode("utf-8"))
    assert r.status_code == 200
    recs = [x for x in audit.query(action="文件上传", limit=10)]
    assert recs
    for x in recs:
        assert ":" not in x["target"] and "\\" not in x["target"]
