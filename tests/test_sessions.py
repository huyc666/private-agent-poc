"""sessions：对话记录 SQLite 持久化（重启不丢、按用户 ACL 过滤）。"""
from server import config, sessions


def _append_many(session_id, user_id, n, prefix="msg"):
    for i in range(n):
        sessions.append(session_id, user_id, "user", f"{prefix}-{i}")


def test_append_and_history_roundtrip():
    sessions.append("s1", "alice", "user", "你好")
    sessions.append("s1", "alice", "assistant", "收到")
    rows = sessions.history("s1")
    assert [r["role"] for r in rows] == ["user", "assistant"]   # 时间正序
    assert rows[0]["content"] == "你好"


def test_history_limit_returns_recent():
    _append_many("s2", "alice", 10)
    rows = sessions.history("s2", limit=3)
    assert len(rows) == 3 and rows[-1]["content"] == "msg-9"


def test_history_limit_fallback_to_default(monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_HISTORY", 2)
    _append_many("s3", "alice", 5)
    assert len(sessions.history("s3")) == 2


def test_append_truncates_oversized_content():
    sessions.append("s4", "alice", "user", "x" * 30_000)
    assert len(sessions.history("s4")[0]["content"]) == 20_000


def test_history_empty_session():
    assert sessions.history("no-such") == []


def test_list_sessions_grouped_and_ordered():
    _append_many("a-sess", "alice", 2)
    _append_many("b-sess", "bob", 1)
    rows = sessions.list_sessions()
    assert rows[0]["session_id"] == "b-sess"      # 最后活跃倒序
    assert rows[0]["first_message"] == "msg-0"     # 首条 user 消息做标题


def test_list_sessions_acl_filters_by_user():
    _append_many("alice-s", "alice", 1)
    _append_many("bob-s", "bob", 1)
    only_alice = sessions.list_sessions(user_id="alice")
    assert [r["session_id"] for r in only_alice] == ["alice-s"]


def test_session_users_distinct():
    sessions.append("mix", "alice", "user", "a")
    sessions.append("mix", "alice", "assistant", "b")
    sessions.append("mix", "bob", "user", "c")
    assert sessions.session_users("mix") == {"alice", "bob"}


def test_delete_session_returns_count():
    _append_many("del-s", "alice", 3)
    assert sessions.delete_session("del-s") == 3
    assert sessions.history("del-s") == []
    assert sessions.delete_session("del-s") == 0
