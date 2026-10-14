"""auth：用户生命周期 / PBKDF2 登录 / 限流锁定 / 凭据解析 / SSO 身份头来源校验。"""
import ipaddress
import sqlite3
import time

import pytest

from server import auth, config


# ---------------------------------------------------------------- 用户管理

def test_add_user_returns_one_time_key(make_user):
    key = make_user("alice")
    assert key.startswith("pak-") and len(key) == 36  # pak- + 32 hex


def test_add_user_duplicate_rejected(make_user):
    make_user("alice")
    assert auth.add_user("alice") is None


def test_add_user_invalid_role_rejected():
    assert auth.add_user("eve", role="superuser") is None


def test_add_user_reserved_scope_username_rejected():
    # 保留域 shared：会与开放模式遗留共享文件域合并，越权读取遗留内容
    assert auth.add_user("shared") is None
    # 全非法字符用户名清洗后同样落入 shared
    assert auth.add_user("###") is None


def test_add_user_name_rules():
    assert auth.add_user("") is None
    assert auth.add_user("x" * 65) is None
    assert auth.add_user(" padded ", role="user")  # strip 后合法


def test_add_user_with_department(make_user):
    auth.add_user("bob", department="财务部")
    assert auth.user_by_name("bob")["department"] == "财务部"


def test_set_department_and_clear(make_user):
    make_user("bob")
    assert auth.set_department("bob", "研发部") is True
    assert auth.user_by_name("bob")["department"] == "研发部"
    assert auth.set_department("bob", "") is True
    assert auth.user_by_name("bob")["department"] == ""


def test_rotate_user_invalidates_old_key(make_user):
    old = make_user("alice")
    new = auth.rotate_user("alice")
    assert new and new != old
    assert auth.resolve_key(old) is None
    assert auth.resolve_key(new)["username"] == "alice"


def test_revoke_user_blocks_key_and_tokens(make_user):
    key = make_user("alice")
    auth.set_password("alice", "password123")
    tok = auth.login("alice", "password123")
    assert auth.revoke_user("alice") is True
    assert auth.resolve_key(key) is None
    assert auth.resolve_token(tok["token"]) is None
    assert auth.user_by_name("alice") is None     # 吊销后不可见（保留审计行）
    assert auth.revoke_user("alice") is False     # 幂等：重复吊销无效果


def test_excluded_expired_key_via_direct_db(make_user):
    """过期 Key（expires_at 已过）不可解析。"""
    key = make_user("alice")
    conn = sqlite3.connect(config.STATE_DB)
    conn.execute("UPDATE api_users SET expires_at = ? WHERE username = 'alice'",
                 (time.time() - 10,))
    conn.commit()
    conn.close()
    assert auth.resolve_key(key) is None


def test_list_users_shape(make_user):
    make_user("alice")
    rows = auth.list_users()
    assert any(r["username"] == "alice" and r["revoked"] == 0 for r in rows)
    assert "password_hash" not in rows[0]           # 只回 has_password 布尔
    assert "key_hash" not in rows[0]                 # 散列绝不外泄


# ---------------------------------------------------------------- 密码与登录

def test_password_too_short_rejected(make_user):
    make_user("alice")
    assert auth.set_password("alice", "short") is False
    assert auth.login("alice", "short") is None


def test_password_stored_as_pbkdf2_hash(make_user):
    make_user("alice")
    auth.set_password("alice", "password123")
    conn = sqlite3.connect(config.STATE_DB)
    stored = conn.execute(
        "SELECT password_hash FROM api_users WHERE username='alice'"
    ).fetchone()[0]
    conn.close()
    assert stored.startswith("pbkdf2$200000$")
    assert "password123" not in stored


def test_login_success_issues_pat_token(make_user):
    make_user("alice")
    auth.set_password("alice", "password123")
    result = auth.login("alice", "password123")
    assert result["token"].startswith("pat-")
    assert result["identity"]["username"] == "alice"
    ident = auth.resolve_token(result["token"])
    assert ident["role"] == "user"
    # token 只存散列
    conn = sqlite3.connect(config.STATE_DB)
    n = conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0]
    conn.close()
    assert n == 1


def test_login_failure_is_ambiguous(make_user):
    make_user("alice")
    auth.set_password("alice", "password123")
    assert auth.login("alice", "wrong-pass") is None      # 密码错
    assert auth.login("ghost", "password123") is None      # 用户不存在（同响应）
    assert auth.login("alice", "") is None                # 空密码


def test_login_lockout_after_consecutive_fails(make_user):
    """连续失败 5 次锁定 30s：锁定期内即使密码正确也拒绝（防在线爆破）。"""
    make_user("alice")
    auth.set_password("alice", "password123")
    for _ in range(auth.LOGIN_MAX_FAILS):
        assert auth.login("alice", "wrong-password") is None
    # 已达阈值：正确密码也被拒（模糊 None，不泄露锁定状态）
    assert auth.login("alice", "password123") is None
    # 锁定记录已落 STATE_DB（跨进程共享计数）
    conn = sqlite3.connect(config.STATE_DB)
    row = conn.execute(
        "SELECT fails, lock_until FROM login_fails WHERE username='alice'"
    ).fetchone()
    conn.close()
    assert row is not None and row[1] > time.time()


def test_login_lockout_expires_and_recovers(make_user):
    make_user("alice")
    auth.set_password("alice", "password123")
    for _ in range(auth.LOGIN_MAX_FAILS):
        auth.login("alice", "wrong-password")
    # 模拟锁定期结束
    conn = sqlite3.connect(config.STATE_DB)
    conn.execute("UPDATE login_fails SET lock_until = 1 WHERE username='alice'")
    conn.commit()
    conn.close()
    assert auth.login("alice", "password123") is not None


def test_login_success_clears_fail_counter(make_user):
    make_user("alice")
    auth.set_password("alice", "password123")
    for _ in range(3):
        auth.login("alice", "wrong-password")
    assert auth.login("alice", "password123") is not None
    conn = sqlite3.connect(config.STATE_DB)
    n = conn.execute(
        "SELECT COUNT(*) FROM login_fails WHERE username='alice'").fetchone()[0]
    conn.close()
    assert n == 0


def test_logout_invalidates_pat_token(make_user):
    make_user("alice")
    auth.set_password("alice", "password123")
    tok = auth.login("alice", "password123")["token"]
    assert auth.logout(tok) is True
    assert auth.resolve_token(tok) is None
    assert auth.logout(tok) is False            # 幂等
    assert auth.logout("pak-xxx") is False      # 对 API Key 无效


# ---------------------------------------------------------------- 凭据解析

def test_resolve_credential_dispatch_by_prefix(make_user):
    key = make_user("alice")
    assert auth.resolve_credential(key)["username"] == "alice"
    assert auth.resolve_credential("pat-xxx") is None
    assert auth.resolve_credential("xxx") is None


def test_resolve_key_rejects_bad_prefix():
    assert auth.resolve_key("pat-abc") is None
    assert auth.resolve_key("") is None


# ---------------------------------------------------------------- 角色上下文

def test_current_role_open_mode_is_admin(monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    from server import usage
    usage.current_user.set("anyone")
    assert auth.current_role() == "admin"       # 开放模式=旧行为全局开放


def test_current_role_missing_user_is_least_privilege():
    from server import usage
    usage.current_user.set("ghost-not-exist")
    assert auth.current_role() == "user"         # 身份缺失 fail-closed 最小权限


def test_current_role_anonymous_is_admin(monkeypatch):
    from server import usage
    usage.current_user.set("anonymous")
    assert auth.current_role() == "admin"


def test_current_role_resolves_from_user_table(make_user):
    from server import usage
    make_user("approver1", role="approver")
    usage.current_user.set("approver1")
    assert auth.current_role() == "approver"


# ---------------------------------------------------------------- SSO 身份头

def test_proxy_ip_allowed_requires_whitelist(monkeypatch):
    monkeypatch.setattr(auth, "_allowed_proxy_nets", [])
    assert auth.proxy_ip_allowed("10.0.0.1") is False   # fail-closed 默认


def test_proxy_ip_allowed_matches_cidr(monkeypatch):
    monkeypatch.setattr(auth, "_allowed_proxy_nets",
                        [ipaddress.ip_network("10.0.0.0/8")])
    assert auth.proxy_ip_allowed("10.1.2.3") is True
    assert auth.proxy_ip_allowed("192.168.1.1") is False
    assert auth.proxy_ip_allowed("not-an-ip") is False
    assert auth.proxy_ip_allowed(None) is False


def test_resolve_identity_header_takes_priority(make_user):
    make_user("sso_user", role="approver")
    ident = auth.resolve_identity("", "sso_user")
    assert ident["role"] == "approver"


def test_resolve_identity_unknown_header_user_fail_closed():
    assert auth.resolve_identity("", "ghost") is None


def test_resolve_identity_department_header_overrides_local(make_user):
    make_user("sso_user")
    ident = auth.resolve_identity("", "sso_user", "市场部")
    assert ident["department"] == "市场部"       # 网关值优先（IdP 权威源）


def test_resolve_identity_falls_back_to_bearer(make_user):
    key = make_user("alice")
    assert auth.resolve_identity(key, "")["username"] == "alice"
    assert auth.resolve_identity("", "") is None


# ---------------------------------------------------------------- 引导管理员

def test_bootstrap_admin_creates_first_admin_once(monkeypatch):
    monkeypatch.setattr(config, "AUTH_BOOTSTRAP_ADMIN", "root-admin")
    auth.bootstrap_admin()
    assert auth.user_by_name("root-admin")["role"] == "admin"
    key1 = auth.user_by_name("root-admin") and True
    auth.bootstrap_admin()                       # 表非空 → 不再创建
    users = [u["username"] for u in auth.list_users()]
    assert users.count("root-admin") == 1
    assert key1


def test_bootstrap_admin_noop_without_config(monkeypatch):
    monkeypatch.setattr(config, "AUTH_BOOTSTRAP_ADMIN", "")
    auth.bootstrap_admin()
    assert auth.list_users() == []
