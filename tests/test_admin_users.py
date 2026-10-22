"""PLAN 3.1 用户与 Key 管理测试（spec：docs/specs/3.1-user-console.md）。

- AC2 用户列表：全字段返回（用户名/角色/部门/状态/密码/Key 过期/创建时间）
- AC3 建用户：一次性 Key 可用；同名/保留名 shared/非法角色/空名拒绝
- AC4 轮换 Key：新 Key 可用、旧 Key 立即失效
- AC5 重置密码：≥8 位生效可登录；<8 位拒绝
- AC6 设角色：立即生效；非法角色拒绝；admin 不能把自己降为非 admin
- AC7 设部门：设置与清空立即生效
- AC8 吊销：pak- Key 与 pat- 令牌立即失效；不可恢复（对已吊销用户轮换被拒，
  防 rotate 复活口子）；admin 不能吊销自己
- AC9 权限拒绝路径：user/approver → 403，未认证 → 401，开放模式 → 400
- AC10 审计日志：管理动作产生 [admin] 结构化日志，且不含明文 Key
- AC12 端点契约回归（404 目标不存在等）
"""
import pytest

from server import auth, config


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def admin_client(client_factory):
    """admin 身份的 TestClient（带鉴权头）。"""
    key = auth.add_user("root", role="admin")
    assert key
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def _mk(username, role="user", days=0, department=""):
    key = auth.add_user(username, role=role, days=days, department=department)
    assert key, f"用户 {username} 创建失败"
    return key


# ---------------------------------------------------------------- AC9 权限拒绝路径

def test_ac9_user_role_403(client_factory):
    key = _mk("alice")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    assert c.get("/api/admin/users").status_code == 403


def test_ac9_approver_role_403(client_factory):
    key = _mk("carol", role="approver")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    assert c.post("/api/admin/users/alice/role",
                  json={"role": "user"}).status_code == 403


def test_ac9_no_credentials_401(client_factory):
    assert client_factory().get("/api/admin/users").status_code == 401


def test_ac9_open_mode_400(client_factory, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    r = client_factory().get("/api/admin/users")
    assert r.status_code == 400 and "开放模式" in r.json()["error"]


# ---------------------------------------------------------------- AC2 用户列表

def test_ac2_list_users_fields(admin_client):
    _mk("alice", department="hr")
    _mk("bob", role="approver", days=30)
    users = admin_client.get("/api/admin/users").json()["users"]
    names = {u["username"] for u in users}
    assert {"root", "alice", "bob"} <= names
    bob = next(u for u in users if u["username"] == "bob")
    for field in ("username", "role", "department", "created", "revoked",
                  "expires_at", "has_password"):
        assert field in bob
    assert bob["role"] == "approver" and bob["has_password"] == 0
    assert bob["expires_at"] is not None


# ---------------------------------------------------------------- AC3 建用户

def test_ac3_create_user_one_time_key(admin_client):
    r = admin_client.post("/api/admin/users", json={
        "username": "alice", "role": "user", "days": 30, "department": "hr"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("pak-")
    ident = auth.resolve_key(key)
    assert ident and ident["username"] == "alice" \
        and ident["role"] == "user" and ident["department"] == "hr"


def test_ac3_create_duplicate_400(admin_client):
    _mk("alice")
    r = admin_client.post("/api/admin/users",
                          json={"username": "alice", "role": "user"})
    assert r.status_code == 400


def test_ac3_create_reserved_shared_400(admin_client):
    r = admin_client.post("/api/admin/users",
                          json={"username": "shared", "role": "user"})
    assert r.status_code == 400


def test_ac3_create_bad_role_400(admin_client):
    r = admin_client.post("/api/admin/users",
                          json={"username": "eve", "role": "superman"})
    assert r.status_code == 400


def test_ac3_create_empty_name_400(admin_client):
    r = admin_client.post("/api/admin/users",
                          json={"username": "  ", "role": "user"})
    assert r.status_code == 400


# ---------------------------------------------------------------- AC4 轮换 Key

def test_ac4_rotate_new_key_valid_old_invalid(admin_client):
    old = _mk("alice")
    r = admin_client.post("/api/admin/users/alice/rotate", json={"days": 0})
    assert r.status_code == 200
    new = r.json()["key"]
    assert auth.resolve_key(new)["username"] == "alice"
    assert auth.resolve_key(old) is None


def test_ac4_rotate_unknown_user_404(admin_client):
    assert admin_client.post("/api/admin/users/ghost/rotate",
                             json={}).status_code == 404


# ---------------------------------------------------------------- AC5 重置密码

def test_ac5_reset_password_login_ok(admin_client):
    _mk("alice")
    r = admin_client.post("/api/admin/users/alice/password",
                          json={"password": "s3cretpass"})
    assert r.status_code == 200
    assert auth.login("alice", "s3cretpass") is not None


def test_ac5_password_too_short_400(admin_client):
    _mk("alice")
    r = admin_client.post("/api/admin/users/alice/password",
                          json={"password": "short"})
    assert r.status_code == 400


def test_ac5_password_unknown_user_404(admin_client):
    assert admin_client.post("/api/admin/users/ghost/password",
                             json={"password": "s3cretpass"}).status_code == 404


# ---------------------------------------------------------------- AC6 设角色

def test_ac6_set_role_immediate(admin_client):
    key = _mk("alice")
    r = admin_client.post("/api/admin/users/alice/role",
                          json={"role": "approver"})
    assert r.status_code == 200
    assert auth.resolve_key(key)["role"] == "approver"


def test_ac6_invalid_role_400(admin_client):
    _mk("alice")
    r = admin_client.post("/api/admin/users/alice/role",
                          json={"role": "owner"})
    assert r.status_code == 400


def test_ac6_admin_cannot_demote_self(admin_client):
    r = admin_client.post("/api/admin/users/root/role",
                          json={"role": "user"})
    assert r.status_code == 400
    assert auth.user_by_name("root")["role"] == "admin"


def test_ac6_self_demote_blocked_with_space_variant(admin_client):
    """评审问题1：带空格的用户名变体（%20root）不得绕过「不能操作自己」——
    端点须先归一化路径参数再比较（auth 层 set_role 会 strip）。"""
    r = admin_client.post("/api/admin/users/%20root/role",
                          json={"role": "user"})
    assert r.status_code == 400
    assert auth.user_by_name("root")["role"] == "admin"


def test_ac8_self_revoke_blocked_with_space_variant(admin_client):
    """评审问题1（revoke 同构）：空格变体同样不得绕过「不能吊销自己」。"""
    r = admin_client.post("/api/admin/users/%20root/revoke", json={})
    assert r.status_code == 400
    root = next(u for u in auth.list_users() if u["username"] == "root")
    assert root["revoked"] == 0


def test_ac12_days_over_limit_rejected(admin_client):
    """评审问题3：days 超上界（>3650 天）被 422 拒绝，不写异常 expires_at。"""
    r = admin_client.post("/api/admin/users",
                          json={"username": "eve2", "days": 3651})
    assert r.status_code == 422


# ---------------------------------------------------------------- AC7 设部门

def test_ac7_set_and_clear_department(admin_client):
    key = _mk("alice")
    assert admin_client.post("/api/admin/users/alice/department",
                             json={"department": "财务部"}).status_code == 200
    assert auth.resolve_key(key)["department"] == "财务部"
    assert admin_client.post("/api/admin/users/alice/department",
                             json={"department": "  "}).status_code == 200
    assert auth.resolve_key(key)["department"] == ""


def test_ac7_department_revoked_user_404(admin_client):
    _mk("alice")
    auth.revoke_user("alice")
    assert admin_client.post("/api/admin/users/alice/department",
                             json={"department": "hr"}).status_code == 404


# ---------------------------------------------------------------- AC8 吊销

def test_ac8_revoke_key_and_token_immediate(admin_client, client_factory):
    key = _mk("alice")
    assert auth.set_password("alice", "s3cretpass")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    login = c.post("/api/auth/login",
                   json={"username": "alice", "password": "s3cretpass"}).json()
    token = login["token"]

    assert admin_client.post("/api/admin/users/alice/revoke").status_code == 200
    assert auth.resolve_key(key) is None
    assert auth.resolve_token(token) is None
    users = admin_client.get("/api/admin/users").json()["users"]
    assert next(u for u in users if u["username"] == "alice")["revoked"] == 1


def test_ac8_no_recovery_via_rotate(admin_client):
    """吊销不可恢复：对已吊销用户轮换 Key 被拒（rotate 会清吊销位，须封口）。"""
    _mk("alice")
    assert auth.revoke_user("alice")
    r = admin_client.post("/api/admin/users/alice/rotate", json={})
    assert r.status_code == 404
    alice = next(u for u in auth.list_users() if u["username"] == "alice")
    assert alice["revoked"] == 1


def test_ac8_admin_cannot_revoke_self(admin_client):
    r = admin_client.post("/api/admin/users/root/revoke", json={})
    assert r.status_code == 400
    assert auth.user_by_name("root") is not None


# ---------------------------------------------------------------- AC10 审计日志

def test_ac10_admin_actions_logged_without_plain_key(admin_client, capsys):
    admin_client.post("/api/admin/users",
                      json={"username": "alice", "role": "user"})
    admin_client.post("/api/admin/users/alice/revoke", json={})
    out = capsys.readouterr().out
    assert "[admin]" in out and "alice" in out and "root" in out
    # 明文 Key 不得出现在日志
    key = auth.list_users()  # 无法反查明文，改为确认日志不含 pak- 前缀串
    assert "pak-" not in out
