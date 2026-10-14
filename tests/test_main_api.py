"""FastAPI 端点：认证中间件 / 登录 / 健康检查 / 会话与审批 ACL / 文件上传下载分域 /
chat SSE 冒烟（mock 模式全链路）。"""
import json

import pytest

from server import approval, auth, config, sessions


@pytest.fixture
def api(client_factory):
    with client_factory() as c:          # with 语义触发 lifespan（mock 模式装配）
        yield c


def _mk_user(username, role="user", password="password123"):
    assert auth.add_user(username, role=role)
    assert auth.set_password(username, password)
    return username


def _login(api, username, password="password123"):
    r = api.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return r.json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 登录与中间件

def test_login_ok(api):
    _mk_user("u1")
    r = api.post("/api/auth/login", json={"username": "u1", "password": "password123"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("pat-")
    assert body["identity"]["username"] == "u1"
    assert body["identity"]["role"] == "user"


def test_login_wrong_password_fuzzy(api):
    _mk_user("u1")
    r = api.post("/api/auth/login", json={"username": "u1", "password": "wrongpass9"})
    assert r.status_code == 401
    r = api.post("/api/auth/login", json={"username": "ghost", "password": "whatever9"})
    assert r.status_code == 401          # 用户不存在与密码错误同路径（防枚举）


def test_logout_invalidates_pat(api):
    _mk_user("u1")
    tok = _login(api, "u1")
    assert api.get("/api/sessions", headers=_hdr(tok)).status_code == 200
    assert api.post("/api/auth/logout", headers=_hdr(tok)).json()["ok"] is True
    assert api.get("/api/sessions", headers=_hdr(tok)).status_code == 401


def test_middleware_401(api):
    assert api.get("/api/sessions").status_code == 401
    assert api.get("/api/sessions", headers=_hdr("pak-deadbeef")).status_code == 401


def test_health_unauth_minimal(api):
    """认证模式下未携带有效凭据：健康检查只回最小信息（不泄露内部状态）。"""
    r = api.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "auth_enabled": True}


def test_health_auth_full(api):
    _mk_user("u1")
    tok = _login(api, "u1")
    body = api.get("/api/health", headers=_hdr(tok)).json()
    assert body["mode"] == "mock"
    assert "tools" in body
    assert body["identity"]["username"] == "u1"


# ---------------------------------------------------------------- 会话 ACL

def test_sessions_acl(api):
    tok1 = _login(api, _mk_user("u1"))
    tok2 = _login(api, _mk_user("u2"))
    sessions.append("s1", "u1", "user", "你好")
    sessions.append("s1", "u1", "assistant", "收到")
    names1 = [s["session_id"] for s in api.get("/api/sessions", headers=_hdr(tok1)).json()["sessions"]]
    names2 = [s["session_id"] for s in api.get("/api/sessions", headers=_hdr(tok2)).json()["sessions"]]
    assert "s1" in names1
    assert "s1" not in names2                       # user 只见自己的会话


def test_history_forbidden_for_other_user(api):
    tok2 = _login(api, _mk_user("u2"))
    sessions.append("s1", "u1", "user", "你好")
    r = api.get("/api/sessions/s1/history", headers=_hdr(tok2))
    assert r.status_code == 403


def test_history_negative_limit_falls_back(api):
    tok1 = _login(api, _mk_user("u1"))
    sessions.append("s1", "u1", "user", "你好")
    r = api.get("/api/sessions/s1/history?limit=-3", headers=_hdr(tok1))
    assert r.status_code == 200                     # 负数回退默认，不炸 SQLite LIMIT


def test_approve_role_separation(api):
    """决议权限（v0.17.9）：user 不可决议任何审批（堵自审自批），approver 可以。"""
    aid = approval.create_approval("delete_workspace_file", {"path": "x.txt"})
    tok_user = _login(api, _mk_user("plain"))
    r = api.post("/api/approve", headers=_hdr(tok_user),
                 json={"approval_id": aid, "approve": True})
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    tok_ap = _login(api, _mk_user("boss", role="approver"))
    r = api.post("/api/approve", headers=_hdr(tok_ap),
                 json={"approval_id": aid, "approve": True})
    assert r.json() == {"ok": True}
    # 已决议的审批再提交 → not_found_or_decided
    r = api.post("/api/approve", headers=_hdr(tok_ap),
                 json={"approval_id": aid, "approve": True})
    assert r.json()["error"] == "not_found_or_decided"


# ---------------------------------------------------------------- 上传分域

def test_upload_and_domain_isolation(api):
    tok1 = _login(api, _mk_user("u1"))
    tok2 = _login(api, _mk_user("u2"))
    r = api.post("/api/uploads", params={"name": "doc.md"}, content=b"hello",
                 headers=_hdr(tok1))
    assert r.status_code == 200
    assert r.json()["name"] == "u1/doc.md"          # 落本人域
    # 清单分域：u2 看不到 u1 的文档
    names2 = [i["name"] for i in api.get("/api/uploads", headers=_hdr(tok2)).json()["uploads"]]
    assert names2 == []
    # u2 删 u1 的文档：user 强制本域 → 目标不存在 → 404（不可越域探测）
    r = api.delete("/api/uploads/u1/doc.md", headers=_hdr(tok2))
    assert r.status_code == 404
    # u1 删自己的文档
    r = api.delete("/api/uploads/u1/doc.md", headers=_hdr(tok1))
    assert r.status_code == 200
    assert r.json()["deleted"] == "u1/doc.md"


def test_upload_bad_names_rejected(api):
    tok1 = _login(api, _mk_user("u1"))
    for bad in ("bad.exe", "con.md", 'x:name.md', "  "):
        r = api.post("/api/uploads", params={"name": bad}, content=b"x",
                     headers=_hdr(tok1))
        assert r.status_code == 400, bad


def test_upload_size_precheck_413(api, monkeypatch):
    """Content-Length 预检：超限直接 413，不读完整请求体（防内存耗尽）。"""
    monkeypatch.setattr(config, "UPLOAD_MAX_MB", 0)
    tok1 = _login(api, _mk_user("u1"))
    r = api.post("/api/uploads", params={"name": "doc.md"}, content=b"x",
                 headers=_hdr(tok1))
    assert r.status_code == 413


# ---------------------------------------------------------------- 输出下载分域

def test_files_download_domain_rules(api):
    tok1 = _login(api, _mk_user("u1"))
    tok2 = _login(api, _mk_user("u2"))
    tok_ap = _login(api, _mk_user("boss", role="approver"))
    out = config.OUTPUTS_DIR
    (out / "u1").mkdir(parents=True)
    (out / "shared").mkdir(parents=True)
    (out / "u1" / "report.md").write_text("u1 的报告", encoding="utf-8")
    (out / "shared" / "legacy.md").write_text("旧版遗留", encoding="utf-8")
    # 单段名路由到本域
    r = api.get("/api/files/report.md", headers=_hdr(tok1))
    assert r.status_code == 200
    assert "u1 的报告" in r.text
    # user 强制本域：带 u1/ 前缀也拿不到别人域的文件（u1 域无 secret.md → 404）
    r = api.get("/api/files/u1/secret.md", headers=_hdr(tok2))
    assert r.status_code == 404
    # approver 全局视图：可带域前缀访问任意域
    r = api.get("/api/files/u1/report.md", headers=_hdr(tok_ap))
    assert r.status_code == 200
    # 全局视图回退 shared（旧版无域链接兼容）；user 角色不回退（保持隔离）
    r = api.get("/api/files/legacy.md", headers=_hdr(tok_ap))
    assert r.status_code == 200
    r = api.get("/api/files/legacy.md", headers=_hdr(tok2))
    assert r.status_code == 404


# ---------------------------------------------------------------- chat SSE 冒烟

def test_chat_sse_smoke(api):
    """mock 模式全链路：SSE 流含 session/token/done 事件，回复落库成对。"""
    tok1 = _login(api, _mk_user("u1"))
    events = []
    with api.stream("POST", "/api/chat",
                    json={"message": "你好"}, headers=_hdr(tok1)) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    types = [e["type"] for e in events]
    assert "session" in types
    assert "token" in types
    assert types[-1] == "done"
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "Mock" in answer
    # 对话记录落库且成对（user 消息 + assistant 回复）
    sid = next(e["session_id"] for e in events if e["type"] == "session")
    msgs = sessions.history(sid, 10)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
