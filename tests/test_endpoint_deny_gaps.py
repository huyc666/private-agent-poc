"""S3 映射表缺口补齐：9 个纯缺口 + 2 个半覆盖端点的拒绝用例。

覆盖（对照 docs/reviews/endpoint-deny-mapping.md）：
- 管理端五个写端点（建用户/轮换/改密/改部门/吊销）：401 / 403 / 开放模式 400，
  并断言被拒时**无副作用**（不新建、不改库、不吊销、旧 Key 仍有效）
- GET /vendor/{name}：真实穿越路径 → 404，且响应不泄露目录/源码内容
- GET /api/approvals、GET /api/uploads、GET /api/usage：未认证 → 401
- POST /api/auth/logout：未认证直打 → 401
- GET /api/audit/export：未认证 → 401、approver → 403
"""
import pytest

from server import auth, config


def _client(client_factory, key: str):
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def _assert_401(r) -> None:
    """未认证拒绝的行为断言：统一错误体（不泄露端点内部信息）。"""
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def _assert_admin_403(r) -> None:
    assert r.status_code == 403
    assert r.json() == {"error": "需要管理员角色"}


def _password_hash(username: str) -> str | None:
    """直接读库取密码散列（None = 未设置密码）。"""
    conn = auth._connect()
    try:
        auth._ensure_schema(conn)
        row = conn.execute(
            "SELECT password_hash FROM api_users WHERE username = ?",
            (username,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------- 管理端五个写端点

ADMIN_WRITE_CASES = [
    # (名称, 路径, 请求体)
    ("create", "/api/admin/users",
     {"username": "gap-newbie", "role": "user"}),
    ("rotate", "/api/admin/users/gap-victim/rotate", {"days": 0}),
    ("password", "/api/admin/users/gap-victim/password",
     {"password": "longenough"}),
    ("department", "/api/admin/users/gap-victim/department",
     {"department": "hr"}),
    ("revoke", "/api/admin/users/gap-victim/revoke", None),
]
IDS = [c[0] for c in ADMIN_WRITE_CASES]


def _call(c, path: str, body):
    return c.post(path) if body is None else c.post(path, json=body)


@pytest.mark.parametrize("name,path,body", ADMIN_WRITE_CASES, ids=IDS)
def test_admin_write_endpoint_no_credentials_401(client_factory, make_user,
                                                 name, path, body):
    """五个写端点各自未认证直打 → 401（不拿共享守卫的 403 分支顶替）。"""
    if name != "create":
        make_user("gap-victim", role="user")
    r = _call(client_factory(), path, body)
    _assert_401(r)
    # 行为断言：被拒请求不得产生副作用
    if name == "create":
        assert auth.user_by_name("gap-newbie") is None
    else:
        assert auth.user_by_name("gap-victim") is not None


@pytest.mark.parametrize("role", ["user", "approver"])
@pytest.mark.parametrize("name,path,body", ADMIN_WRITE_CASES, ids=IDS)
def test_admin_write_endpoint_non_admin_403(client_factory, make_user,
                                            name, path, body, role):
    """五个写端点各自对 user / approver → 403，且不改动目标用户。"""
    victim_key = ""
    if name != "create":
        victim_key = make_user("gap-victim", role="user")
        auth.set_department("gap-victim", "ops")
    c = _client(client_factory, make_user(f"gap-actor-{role}-{name}", role=role))
    r = _call(c, path, body)
    _assert_admin_403(r)

    if name == "create":
        assert auth.user_by_name("gap-newbie") is None
    elif name == "rotate":
        assert auth.resolve_identity(victim_key, "", "") is not None
    elif name == "password":
        # 直接查库：密码散列仍为空（不是"碰巧登录失败"）
        assert _password_hash("gap-victim") is None
    elif name == "department":
        assert auth.user_by_name("gap-victim")["department"] == "ops"
    elif name == "revoke":
        assert auth.resolve_identity(victim_key, "", "") is not None


@pytest.mark.parametrize("name,path,body", ADMIN_WRITE_CASES, ids=IDS)
def test_admin_write_endpoint_open_mode_400(client_factory, monkeypatch,
                                            name, path, body):
    """开放模式（无用户体系）五个写端点 → 400，不落任何写操作。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    r = _call(client_factory(), path, body)
    assert r.status_code == 400
    assert r.json() == {"error": "开放模式无用户体系，管理端点不可用"}
    if name == "create":
        assert auth.user_by_name("gap-newbie") is None


def test_admin_rotate_rejected_keeps_old_key_valid(client_factory, make_user):
    """轮换被拒的强行为断言：旧 Key 仍可认证（若守卫漏放行旧 Key 会失效）。"""
    victim_key = make_user("gap-victim-rot", role="user")
    c = _client(client_factory, make_user("gap-actor-rot", role="user"))
    r = c.post("/api/admin/users/gap-victim-rot/rotate", json={"days": 0})
    _assert_admin_403(r)
    assert auth.resolve_identity(victim_key, "", "") is not None


def test_admin_revoke_rejected_keeps_user_active(client_factory, make_user):
    victim_key = make_user("gap-victim-rev", role="user")
    c = _client(client_factory, make_user("gap-actor-rev", role="user"))
    r = c.post("/api/admin/users/gap-victim-rev/revoke")
    _assert_admin_403(r)
    assert auth.resolve_identity(victim_key, "", "") is not None
    # 端点后续仍可正常访问（用户未被吊销）
    assert _client(client_factory, victim_key).get("/api/sessions").status_code == 200


# ---------------------------------------------------------- GET /vendor/{name}

@pytest.mark.parametrize("probe", [
    "%2E%2E",                         # 编码上级目录（raw_path 保留，真达端点）
    "..%5C.env",                      # Windows 反斜杠穿越
    "%2E%2E%5Cserver%5Cmain.py",      # 反斜杠进入源码树
])
def test_vendor_traversal_rejected_404(client_factory, probe):
    """真实构造穿越路径 → 404，响应体只有错误文案、不含目录/源码内容。"""
    r = client_factory().get(f"/vendor/{probe}")
    assert r.status_code == 404
    body = r.text
    assert r.json() == {"error": "非法资源名"}
    # 不泄露：不得反射请求路径，不得返回任何文件内容特征
    assert "FastAPI" not in body
    assert "private-agent" not in body
    assert "PROJECT_ROOT" not in body
    assert ".env" not in body


def test_vendor_legit_file_still_served(client_factory):
    """反向对照：合法 vendored 文件正常返回（守卫不是一刀切 404）。"""
    r = client_factory().get("/vendor/mammoth.browser.min.js")
    assert r.status_code == 200
    assert "application/javascript" in r.headers["content-type"]


# ---------------------------------------------------------- 列表/查询端点 401

@pytest.mark.parametrize("path", ["/api/approvals", "/api/uploads", "/api/usage"])
def test_list_endpoints_no_credentials_401(client_factory, path):
    _assert_401(client_factory().get(path))


# ---------------------------------------------------------- 登出 / 审计导出

def test_logout_no_credentials_401(client_factory):
    """未认证直打登出 → 401（不落空 token 注销动作）。"""
    _assert_401(client_factory().post("/api/auth/logout"))


def test_audit_export_no_credentials_401(client_factory):
    _assert_401(client_factory().get("/api/audit/export"))


def test_audit_export_approver_403(client_factory, make_user):
    r = _client(client_factory, make_user("gap-approver", role="approver")) \
        .get("/api/audit/export")
    _assert_admin_403(r)
