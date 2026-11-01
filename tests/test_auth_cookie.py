"""P0-2 凭据 HttpOnly Cookie 迁移与 CSRF 纵深（spec 4.5，AC12-AC14）。

审核报告 §4：浏览器登录态（pat- Bearer）长期落 localStorage——同源脚本一旦
执行即可窃取凭据。修复语义（用户裁决本轮完整迁移）：
- 登录签发 pap_token=HttpOnly SameSite=Lax Cookie（AUTH_COOKIE_SECURE=true
  时追加 Secure），响应体 token 字段保留（程序化 curl 兼容）；
- 凭据优先级 SSO 头 > Bearer > Cookie，Bearer（pak-/pat-）路径全量保留；
- CSRF 纵深：cookie 身份的非 GET 请求必须带 X-Requested-With，否则 403
  （login/logout 豁免；Bearer/SSO 头路径免校验）；
- 登出按 Bearer 或 cookie 令牌注销 + Set-Cookie Max-Age=0 清除浏览器侧。
"""
import sqlite3

import pytest

from server import auth, config


@pytest.fixture
def api(client_factory):
    with client_factory() as c:
        yield c


def _mk_password_user(username, role="user"):
    assert auth.add_user(username, role=role)
    assert auth.set_password(username, "password123")
    return username


def _login(api, username):
    r = api.post("/api/auth/login",
                 json={"username": username, "password": "password123"})
    assert r.status_code == 200
    return r.json()["token"]


def _cookie(token):
    return {"Cookie": f"pap_token={token}"}


def _set_cookies(resp):
    return "; ".join(resp.headers.get_list("set-cookie"))


# ---------------------------------------------------------------- AC12 Cookie 签发


def test_ac12_login_sets_httponly_cookie(api):
    """AC12：登录响应 Set-Cookie pap_token（HttpOnly/SameSite=Lax/Max-Age 对齐
    令牌 TTL；默认无 Secure）；cookie 可访问受保护 API；响应体 token 保留。"""
    _mk_password_user("cu")
    r = api.post("/api/auth/login",
                 json={"username": "cu", "password": "password123"})
    assert r.status_code == 200
    tok = r.json()["token"]            # 响应体 token 保留（程序化 curl 兼容）
    sc = _set_cookies(r)
    assert f"pap_token={tok}" in sc
    assert "httponly" in sc.lower()
    assert "samesite=lax" in sc.lower()
    assert f"max-age={int(config.AUTH_TOKEN_TTL_HOURS * 3600)}" in sc.lower()
    assert "secure" not in sc.lower()   # 默认 AUTH_COOKIE_SECURE=false（本地 http）
    # cookie 访问受保护 API（无 Authorization 头）
    r2 = api.get("/api/sessions", headers=_cookie(tok))
    assert r2.status_code == 200


def test_ac12_cookie_secure_flag_when_configured(api, monkeypatch):
    """AC12：AUTH_COOKIE_SECURE=true（生产 HTTPS 部署）→ Cookie 追加 Secure。"""
    monkeypatch.setattr(config, "AUTH_COOKIE_SECURE", True)
    _mk_password_user("cu2")
    r = api.post("/api/auth/login",
                 json={"username": "cu2", "password": "password123"})
    assert r.status_code == 200
    assert "secure" in _set_cookies(r).lower()


def test_ac12_bearer_path_regression(api):
    """AC12：Bearer 路径全量保留——pat- 令牌走 Authorization 头照常可用。"""
    _mk_password_user("cu3")
    tok = _login(api, "cu3")
    r = api.get("/api/sessions", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_ac12_bearer_precedence_over_cookie(api):
    """AC12：凭据优先级 Bearer > Cookie——两者同在时身份取 Bearer 一方。"""
    key_a = auth.add_user("pa")
    assert key_a
    _mk_password_user("cb")
    tok_b = _login(api, "cb")
    r = api.get("/api/health", headers={
        "Authorization": f"Bearer {key_a}", **_cookie(tok_b)})
    ident = (r.json() or {}).get("identity") or {}
    assert ident.get("username") == "pa"


# ---------------------------------------------------------------- AC13 CSRF 纵深


def test_ac13_cookie_post_without_xrw_403(api):
    """AC13：cookie 身份的非 GET 请求缺 X-Requested-With → 403（CSRF 纵深）。"""
    _mk_password_user("cu4")
    tok = _login(api, "cu4")
    r = api.post("/api/chat", headers=_cookie(tok),
                 json={"session_id": "s-csrf", "message": "hi"})
    assert r.status_code == 403


def test_ac13_cookie_post_with_xrw_passes(api):
    """AC13：cookie 身份 + X-Requested-With → 正常进入业务（chat SSE 开流）。"""
    _mk_password_user("cu5")
    tok = _login(api, "cu5")
    r = api.post("/api/chat", headers={
        **_cookie(tok), "X-Requested-With": "XMLHttpRequest"},
        json={"session_id": "s-csrf-ok", "message": "hi"})
    assert r.status_code == 200


def test_ac13_cookie_get_without_xrw_allowed(api):
    """AC13：GET 请求不触发 CSRF 校验（无状态变更，SameSite=Lax 主防线）。"""
    _mk_password_user("cu6")
    tok = _login(api, "cu6")
    r = api.get("/api/sessions", headers=_cookie(tok))
    assert r.status_code == 200


def test_ac13_bearer_post_no_xrw_needed(api):
    """AC13：Bearer 路径免 XRW 校验（凭据不自动附带，无 CSRF 面）——不回归。"""
    key = auth.add_user("pk2")
    assert key
    r = api.post("/api/chat", headers={"Authorization": f"Bearer {key}"},
                 json={"session_id": "s-bearer", "message": "hi"})
    assert r.status_code == 200


# ---------------------------------------------------------------- AC14 登出清 cookie


def test_ac14_logout_clears_cookie_and_token(api):
    """AC14：登出（cookie 身份，logout 豁免 XRW）→ 注销令牌 + Set-Cookie
    Max-Age=0 清除浏览器侧；auth_tokens 行删除，cookie 立即失效。"""
    _mk_password_user("cu7")
    tok = _login(api, "cu7")
    # cookie 会话有效
    assert api.get("/api/sessions", headers=_cookie(tok)).status_code == 200
    r = api.post("/api/auth/logout", headers=_cookie(tok))
    assert r.status_code == 200 and r.json() == {"ok": True}
    sc = _set_cookies(r)
    assert "pap_token=" in sc and "max-age=0" in sc.lower()
    # 令牌已失效：cookie 访问受保护 API → 401
    assert api.get("/api/sessions", headers=_cookie(tok)).status_code == 401
    # auth_tokens 行已删
    conn = sqlite3.connect(config.STATE_DB)
    try:
        n = conn.execute("SELECT COUNT(*) FROM auth_tokens").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_ac14_bearer_logout_regression(api):
    """AC14：Bearer 登出路径保留——Authorization 头令牌注销照常生效。"""
    _mk_password_user("cu8")
    tok = _login(api, "cu8")
    r = api.post("/api/auth/logout",
                 headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert api.get("/api/sessions",
                   headers={"Authorization": f"Bearer {tok}"}).status_code == 401
