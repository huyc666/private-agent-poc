"""SEC-1（spec v0.17.32 AC1）：chat 写入口的会话归属校验。

历史/删除端点经 _can_access_session 拦截，chat 却缺失——登录用户可向他人
session_id 写消息并接续其 LangGraph thread_id 模型上下文（跨用户上下文
泄露 + 会话状态污染）。修复后语义（与 _can_access_session 不同，不复用）：
- 空会话（尚无消息）放行：首轮写入必须允许，新会话 session_id 由客户端生成；
- owners 非空且含本人与 anonymous 之外的身份 → 403；
- admin 全局视图；开放模式不校验（行为不变）。
"""
import pytest

from server import auth, config, sessions


@pytest.fixture
def api(client_factory):
    with client_factory() as c:          # with 语义触发 lifespan（mock 模式装配）
        yield c


def _mk_user(username, role="user", password="password123"):
    assert auth.add_user(username, role=role)
    assert auth.set_password(username, password)
    return username


def _login(api, username, password="password123"):
    r = api.post("/api/auth/login",
                 json={"username": username, "password": password})
    assert r.status_code == 200
    return r.json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def test_ac1_chat_other_user_session_403(api):
    """核心场景：user B 向 user A 已有消息的会话发 chat → 403，且不写入。"""
    _mk_user("alice")
    tok_b = _login(api, _mk_user("mallory"))
    sessions.append("s-alice", "alice", "user", "你好")
    r = api.post("/api/chat", headers=_hdr(tok_b),
                 json={"session_id": "s-alice", "message": "接续上下文"})
    assert r.status_code == 403
    assert "mallory" not in sessions.session_users("s-alice")


def test_ac1_chat_new_session_allowed(api):
    """新会话（owners 为空）首轮写入放行。"""
    tok = _login(api, _mk_user("bob"))
    r = api.post("/api/chat", headers=_hdr(tok),
                 json={"session_id": "s-new-bob", "message": "你好"})
    assert r.status_code == 200
    assert "bob" in sessions.session_users("s-new-bob")


def test_ac1_chat_own_session_allowed(api):
    """本人续聊自己已有消息的会话放行。"""
    tok = _login(api, _mk_user("alice"))
    sessions.append("s-alice", "alice", "user", "第一轮")
    r = api.post("/api/chat", headers=_hdr(tok),
                 json={"session_id": "s-alice", "message": "第二轮"})
    assert r.status_code == 200


def test_ac1_chat_admin_global_view_allowed(api):
    """admin 保留全局视图（管理职责），可向任意会话发消息。"""
    tok_ad = _login(api, _mk_user("root1", role="admin"))
    sessions.append("s-alice", "alice", "user", "你好")
    r = api.post("/api/chat", headers=_hdr(tok_ad),
                 json={"session_id": "s-alice", "message": "管理员接入"})
    assert r.status_code == 200


def test_ac1_chat_approver_not_owner_403(api):
    """approver 对他人会话同样 403（v0.17.29 隐私收紧口径：会话内容属用户隐私）。"""
    _mk_user("alice")
    tok_ap = _login(api, _mk_user("appr1", role="approver"))
    sessions.append("s-alice", "alice", "user", "你好")
    r = api.post("/api/chat", headers=_hdr(tok_ap),
                 json={"session_id": "s-alice", "message": "越权接入"})
    assert r.status_code == 403


def test_ac1_chat_open_mode_unchecked(api, monkeypatch):
    """开放模式行为不变：无身份概念，任意 session_id 可聊（演示语义）。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    sessions.append("s-anyone", "someone", "user", "旧消息")
    r = api.post("/api/chat",
                 json={"session_id": "s-anyone", "message": "hi",
                       "user_id": "guest"})
    assert r.status_code == 200


def test_ac1_chat_anonymous_legacy_session_allowed(api):
    """开放模式遗留会话（owners 仅含 anonymous）：认证用户可接续——
    anonymous 属于双方共同的开放态语义（与 _can_access_session 同口径）。"""
    tok = _login(api, _mk_user("alice"))
    sessions.append("s-legacy", "anonymous", "user", "旧消息")
    r = api.post("/api/chat", headers=_hdr(tok),
                 json={"session_id": "s-legacy", "message": "继续"})
    assert r.status_code == 200


# ---- v0.17.33 AC1：锁外检查后的并发窗口（复验实测）----

def test_ac1_chat_race_window_second_check_rejects(api, monkeypatch):
    """锁外检查放行后、进锁前归属被并发写入占据 → 锁内二次检查以 error
    事件拒绝（已开流无法改 HTTP 码），劫持者不写入该会话。"""
    from server import agent
    _mk_user("alice")
    tok_b = _login(api, _mk_user("mallory"))
    real_lock = agent.session_lock

    def _lock_with_injected_owner(sid):
        if sid == "s-race":
            # 模拟并发时序：mallory 锁外检查时会话为空（放行），进锁前
            # alice 的并发首轮已写入并持有归属
            sessions.append("s-race", "alice", "user", "并发首轮")
        return real_lock(sid)

    monkeypatch.setattr(agent, "session_lock", _lock_with_injected_owner)
    r = api.post("/api/chat", headers=_hdr(tok_b),
                 json={"session_id": "s-race", "message": "劫持"})
    assert r.status_code == 200                    # SSE 已开流
    assert '"type": "error"' in r.text             # 以 error 事件终止
    assert "mallory" not in sessions.session_users("s-race")
    assert "alice" in sessions.session_users("s-race")


def test_ac1_chat_race_window_admin_global_view_allowed(api, monkeypatch):
    """锁内二次检查复用 _chat_session_denied 全语义：admin 全局视图在并发
    窗口下不被误拒（管理职责，可接入任意会话）。"""
    from server import agent
    _mk_user("alice")
    tok_ad = _login(api, _mk_user("root2", role="admin"))
    real_lock = agent.session_lock

    def _lock_with_injected_owner(sid):
        if sid == "s-race-ad":
            sessions.append("s-race-ad", "alice", "user", "首轮")
        return real_lock(sid)

    monkeypatch.setattr(agent, "session_lock", _lock_with_injected_owner)
    r = api.post("/api/chat", headers=_hdr(tok_ad),
                 json={"session_id": "s-race-ad", "message": "管理员接入"})
    assert r.status_code == 200
    assert '"type": "error"' not in r.text
