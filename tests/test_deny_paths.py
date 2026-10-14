"""安全防线「拒绝路径」分支补测（v0.17.17 分支覆盖审查产出）。

此前离线用例全部打在各防线的放行侧；本文件逐个触发拒绝分支：
- 上传/技能包端点：空请求体 400、超限 413（Content-Length 预检 + 读后兜底）
- 下载/删除端点：路径穿越文件名 400、单段名本域路由、user 角色强制本域
- 技能包导入：zip 炸弹上限（条目数 / 解压总量）
- 登录限流表容量控制：清已解锁条目、超限全清兜底
- SSO 身份头来源校验：白名单解析 fail-closed、非受信来源忽略身份头
- 会话端点：user 越权 403 / approver 放行 / 开放模式不过滤
- 专属自定义工具：归属校验拒绝、沙箱 fail-closed
- 包签名：未启用 / 包缺失 / 签名失败；run_python_code 沙箱不可达
- 审批超时竞态：标记 expired 落败后以决议为准
- 凭据解析：非法哈希 scheme、非 pat 令牌、空用户名
"""
import asyncio
import importlib
import io
import time
import zipfile

import pytest

from server import approval, auth, config, custom_tools, packs, skills, tools_builtin


@pytest.fixture
def client(client_factory, make_user):
    """带认证身份的 TestClient（默认 admin，便于走通端点到防线分支）。"""
    key = make_user("root-acl", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


# ---------------------------------------------------------- 上传 / 技能包端点防线

def test_upload_empty_body_rejected(client):
    r = client.post("/api/uploads?name=a.txt", content=b"")
    assert r.status_code == 400 and "请求体为空" in r.json()["error"]


def test_upload_oversize_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_MAX_MB", 0.001)   # 上限≈1KB，免发大 body
    r = client.post("/api/uploads?name=a.txt", content=b"x" * 2000)
    assert r.status_code == 413


def test_upload_docx_oversize_xml_rejected(client, monkeypatch):
    """docx 解压炸弹防线：word/document.xml 解压后超上限 → 400。"""
    monkeypatch.setattr(config, "UPLOAD_MAX_MB", 10)
    monkeypatch.setattr("server.main._DOCX_XML_MAX_CHARS", 5)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:p>abcdefgh</w:p>")
    r = client.post("/api/uploads?name=a.docx", content=buf.getvalue())
    assert r.status_code == 400 and "过大" in r.json()["error"]


def test_skillpack_empty_and_oversize_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_MAX_MB", 0.001)
    assert client.post("/api/skillpacks", content=b"").status_code == 400
    assert client.post("/api/skillpacks", content=b"x" * 2000).status_code == 413


def test_skillpack_zip_entries_limit(monkeypatch):
    monkeypatch.setattr(skills, "_SKILLPACK_MAX_ENTRIES", 2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: t\n---\n# t")
        zf.writestr("a.md", "x")
        zf.writestr("b.md", "y")
    with pytest.raises(ValueError, match="文件数超过上限"):
        skills.import_skill_zip(buf.getvalue(), False, None)


def test_skillpack_zip_total_size_limit(monkeypatch):
    monkeypatch.setattr(skills, "_SKILLPACK_MAX_TOTAL", 10)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "x" * 50)
    with pytest.raises(ValueError, match="总大小超过上限"):
        skills.import_skill_zip(buf.getvalue(), False, None)


# ---------------------------------------------------------- 下载 / 删除端点防线

def test_download_traversal_rejected(client):
    r = client.get("/api/files/..%2F.env")
    assert r.status_code == 400 and r.json()["error"] == "非法文件名"


def test_delete_upload_traversal_rejected(client):
    r = client.delete("/api/uploads/..%2Fx.txt")
    assert r.status_code == 400 and r.json()["error"] == "非法文件名"


def test_delete_upload_single_segment_routes_own_scope(client, login_as):
    """单段文件名按请求身份路由到本域（user 角色不会落到他人域/共享域）。"""
    login_as("u-del", role="user")
    r = client.delete("/api/uploads/not-exist.txt")
    assert r.status_code == 404                      # 本域寻址后未命中


def test_delete_upload_two_segment_user_forced_own_scope(client, login_as):
    """user 角色带域前缀 → 强制重写为本域（越域删除不可达）。"""
    login_as("u-del2", role="user")
    r = client.delete("/api/uploads/other-domain/not-exist.txt")
    assert r.status_code == 404                      # 被改写为本域 not-exist


# ---------------------------------------------------------- 登录限流表容量控制

def _seed_fails(rows):
    conn = auth._connect()
    auth._ensure_schema(conn)
    conn.execute("DELETE FROM login_fails")
    conn.executemany("INSERT INTO login_fails VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def _fails_exists(username: str) -> bool:
    conn = auth._connect()
    n = conn.execute("SELECT COUNT(*) FROM login_fails WHERE username = ?",
                     (username,)).fetchone()[0]
    conn.close()
    return n > 0


def test_login_fails_capacity_clears_unlocked_only(monkeypatch):
    """表满且含已解锁条目：只清已解锁，保留锁定中的活跃条目。"""
    monkeypatch.setattr(auth, "_LOGIN_FAILS_MAX", 2)
    now = time.time()
    _seed_fails([("rf_a", 5, now - 10), ("rf_b", 5, now - 20), ("rf_c", 5, now + 999)])
    assert auth.login("probe-a", "wrong-password") is None
    # 失败尝试自身也会记账（probe-a）；已解锁的 rf_a/rf_b 被清，锁定中的 rf_c 保留
    assert not _fails_exists("rf_a") and not _fails_exists("rf_b")
    assert _fails_exists("rf_c")


def test_login_fails_capacity_full_clear(monkeypatch):
    """表满且全部锁定中：整体清空保底（限流临时降级，PBKDF2 仍兜底）。"""
    monkeypatch.setattr(auth, "_LOGIN_FAILS_MAX", 2)
    now = time.time()
    _seed_fails([("rf_d", 5, now + 999), ("rf_e", 5, now + 999)])
    assert auth.login("probe-b", "wrong-password") is None
    assert not _fails_exists("rf_d") and not _fails_exists("rf_e")


# ---------------------------------------------------------- SSO 身份头来源校验

def test_proxy_allowlist_parse_failclosed(monkeypatch):
    """白名单解析：合法网段入表；含非法 token 整体弃用（fail-closed）。"""
    orig = auth._allowed_proxy_nets
    try:
        monkeypatch.setattr(config, "AUTH_PROXY_ALLOWED_IPS", "10.0.0.0/8, not-an-ip")
        importlib.reload(auth)
        assert auth._allowed_proxy_nets == []        # 一个坏 token → 全部弃用
        monkeypatch.setattr(config, "AUTH_PROXY_ALLOWED_IPS", "192.168.0.0/16")
        importlib.reload(auth)
        assert len(auth._allowed_proxy_nets) == 1
    finally:
        auth._allowed_proxy_nets = orig


def test_proxy_ip_allowed_direct(monkeypatch):
    monkeypatch.setattr(auth, "_allowed_proxy_nets",
                        [__import__("ipaddress").ip_network("10.0.0.0/8")])
    assert auth.proxy_ip_allowed("10.1.2.3") is True
    assert auth.proxy_ip_allowed("11.1.2.3") is False
    assert auth.proxy_ip_allowed(None) is False
    assert auth.proxy_ip_allowed("testclient") is False   # 非法 IP 文本 → 拒绝


def test_sso_header_from_untrusted_source_ignored(client, monkeypatch, make_user):
    """未命中白名单来源携带身份头 → 忽略（防伪造），/api/* 返回 401。"""
    make_user("u-sso", role="user")
    monkeypatch.setattr(config, "AUTH_IDENTITY_HEADER", "X-User")
    c = client_factory_nocache()
    r = c.get("/api/sessions", headers={"X-User": "u-sso"})
    assert r.status_code == 401


def test_sso_header_from_trusted_source_resolves(client_factory, make_user, monkeypatch):
    """命中白名单来源：身份头映射本地用户，部门头同步透传。"""
    make_user("u-sso2", role="user")
    monkeypatch.setattr(config, "AUTH_IDENTITY_HEADER", "X-User")
    monkeypatch.setattr(config, "AUTH_IDENTITY_DEPARTMENT_HEADER", "X-Dept")
    monkeypatch.setattr(auth, "proxy_ip_allowed", lambda host: True)
    c = client_factory()
    r = c.get("/api/sessions", headers={"X-User": "u-sso2", "X-Dept": "sec"})
    assert r.status_code == 200
    ident = auth.resolve_identity("", "u-sso2", "sec")
    assert ident["department"] == "sec"


def client_factory_nocache():
    from fastapi.testclient import TestClient
    from server.main import app
    return TestClient(app)


# ---------------------------------------------------------- 会话资源级 ACL

def test_session_delete_user_forbidden(client_factory, make_user):
    """user 角色删除他人/无主会话 → 403（越权防线）。"""
    make_user("u-ses", role="user")
    key = auth.add_user("u-ses2", role="user")
    c = client_factory()
    r = c.delete("/api/sessions/some-other-user-session",
                 headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403


def test_session_delete_approver_allowed(client_factory, make_user):
    key = make_user("u-appr", role="approver")
    c = client_factory()
    r = c.delete("/api/sessions/any-session",
                 headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and not r.json()["deleted"]


def test_session_delete_open_mode_no_acl(client_factory, make_user, monkeypatch):
    make_user("u-om")
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    c = client_factory()
    r = c.delete("/api/sessions/any-session")
    assert r.status_code == 200 and not r.json()["deleted"]


# ---------------------------------------------------------- 专属自定义工具边界

def test_personal_tool_rejects_empty_scope(login_as, monkeypatch):
    """user 角色但专属域解析为空 → 拒绝（防越权兜底分支）。"""
    login_as("u-pt", role="user")
    monkeypatch.setattr(config, "scope_clean", lambda u: "")
    out = custom_tools._resolve_personal("some_tool")
    assert out == "专属自定义工具仅对登录用户可用"


def test_personal_tool_rejects_unknown_role(login_as, monkeypatch):
    """角色不在 user/approver/admin 白名单 → 拒绝（防御性分支）。"""
    login_as("u-pt2", role="user")
    monkeypatch.setattr(auth, "current_role", lambda: "ghost")
    out = custom_tools._resolve_personal("some_tool")
    assert out == "专属自定义工具仅对登录用户可用"


def test_personal_tool_sandbox_down_failclosed(login_as, monkeypatch):
    """CUSTOM_TOOLS_EXEC=sandbox 且沙箱不可达 → fail-closed 拒绝执行。"""
    login_as("u-pt3", role="user")
    scope = config.scope_clean("u-pt3")
    d = config.PERSONAL_CUSTOM_TOOLS_DIR / scope
    d.mkdir(parents=True, exist_ok=True)
    (d / "t1.py").write_text("def t1():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("CUSTOM_TOOLS_EXEC", "sandbox")
    monkeypatch.setattr(config, "SANDBOX_URL", "http://127.0.0.1:1")   # 必不可达
    monkeypatch.setattr(custom_tools, "_up_cache", {"t": 0.0, "v": False})
    out = asyncio.run(custom_tools.run_personal_tool("t1", {}))
    assert "fail-closed" in out and "沙箱不可达" in out


# ---------------------------------------------------------- 包签名 / 沙箱代码执行

def test_sign_pack_not_enforced():
    msg = packs.sign_pack("any-pack")
    assert "未配置 PACK_SIGNING_KEY" in msg


def test_sign_pack_missing_pack(monkeypatch):
    monkeypatch.setattr(packs.packsign, "is_enforced", lambda: True)
    assert "不存在或缺少 pack.yaml" in packs.sign_pack("no-such-pack")


def test_sign_pack_signing_failure(monkeypatch):
    monkeypatch.setattr(packs.packsign, "is_enforced", lambda: True)
    d = config.PACKS_DIR / "p-fail"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text("name: p-fail\n", encoding="utf-8")
    monkeypatch.setattr(packs.packsign, "sign_pack_files", lambda pd: "")
    assert "签名失败" in packs.sign_pack("p-fail")


def test_run_python_code_sandbox_unreachable(monkeypatch):
    """配置了沙箱但不可达 → 明确失败文案（不回退本地执行，fail-closed）。"""
    monkeypatch.setattr(config, "SANDBOX_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(config, "SANDBOX_AUTH_TOKEN", "tk")
    out = tools_builtin.run_python_code("print(1)")
    assert "沙箱调用失败" in out
    monkeypatch.setattr(config, "SANDBOX_AUTH_TOKEN", "")   # 无令牌分支
    assert "沙箱调用失败" in tools_builtin.run_python_code("print(1)")


# ---------------------------------------------------------- 审批超时竞态

def _race(monkeypatch, final_status):
    """构造超时竞态：标记 expired 的 UPDATE 落败（n==0），重查决议。"""
    monkeypatch.setattr(approval, "_execute", lambda *a, **k: 0)
    seq = [{"status": "pending"}, {"status": final_status}]
    monkeypatch.setattr(approval, "_query_one", lambda *a, **k: seq.pop(0))
    return asyncio.run(approval.wait_decision("aid-x", timeout=0))


def test_approval_timeout_race_approved_wins(monkeypatch):
    assert _race(monkeypatch, "approved") is True


def test_approval_timeout_race_rejected_wins(monkeypatch):
    assert _race(monkeypatch, "rejected") is False


def test_approval_timeout_race_still_pending(monkeypatch):
    monkeypatch.setattr(approval, "_execute", lambda *a, **k: 0)
    monkeypatch.setattr(approval, "_query_one", lambda *a, **k: {"status": "pending"})
    assert asyncio.run(approval.wait_decision("aid-x", timeout=0)) is None


# ---------------------------------------------------------- 凭据解析防线

def test_verify_password_rejects_other_scheme():
    assert auth._verify_password("x", "bcrypt$1234$abcd$efgh") is False


def test_resolve_token_rejects_non_pat():
    assert auth.resolve_token("") is None
    assert auth.resolve_token("pak-not-a-login-token") is None
    assert auth.resolve_token("junk") is None


def test_user_by_name_empty():
    assert auth.user_by_name("") is None
    assert auth.user_by_name("   ") is None
