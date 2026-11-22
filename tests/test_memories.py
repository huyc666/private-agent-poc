"""PLAN 8.1：用户记忆沉淀与召回（spec：docs/specs/8.1-user-memory.md）。

- AC1 user_memories 表 schema（SQLite/Postgres 双后端幂等建表 + 索引）
- AC2 显式 save_memory 工具写入
- AC3 _dynamic_prompt 召回注入
- AC4 注入量控制（token 上限）
- AC5 权限隔离（user vs admin）
- AC6 CRUD API 端点
- AC7 审计集成
- AC8 过期过滤
"""
import os
import sqlite3
import time

import pytest

from server import audit, auth, config, db, memories, usage


PG_URL = os.environ.get(
    "PG_URL", "postgresql://postgres:postgres@127.0.0.1:5432/agent_poc")


def _with_schema(url: str, schema_name: str) -> str:
    return f"{url}{('&' if '?' in url else '?')}options=-c%20search_path%3D{schema_name}"


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _rows(sql, params=()):
    conn = sqlite3.connect(config.STATE_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _old_state_db_without_memories():
    """构造无 user_memories 表的老库（模拟既有部署）。"""
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute("DROP TABLE IF EXISTS user_memories")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- AC1 schema

def test_ac1_user_memories_schema_sqlite_and_postgres(monkeypatch):
    """老库启动时幂等建 user_memories 表 + 索引；Postgres DDL 同样幂等。"""
    _old_state_db_without_memories()
    memories.init()
    cols = {r[1] for r in sqlite3.connect(config.STATE_DB).execute(
        "PRAGMA table_info(user_memories)").fetchall()}
    indexes = {r[1] for r in sqlite3.connect(config.STATE_DB).execute(
        "PRAGMA index_list(user_memories)").fetchall()}
    assert {"id", "user_id", "content", "category", "created", "updated",
            "expires_at", "enabled"} <= cols
    assert "idx_user_memories_user_enabled" in indexes

    # Postgres 契约测（真实 PG 门控）
    if os.environ.get("RUN_PG_TESTS") != "1":
        pytest.skip("RUN_PG_TESTS=1 未设置，跳过真实 PG 契约测")
    schema = "test_memories_ac1"
    monkeypatch.setenv("STATE_BACKEND", "postgres")
    monkeypatch.setenv("STATE_DATABASE_URL", _with_schema(PG_URL, schema))
    db.ensure_postgres_schema()
    conn = db.connect_state()
    try:
        cols_pg = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'user_memories'").fetchall()}
        assert {"id", "user_id", "content", "category", "created", "updated",
                "expires_at", "enabled"} <= cols_pg
    finally:
        conn.close()


# ---------------------------------------------------------------- AC2 显式写入

def test_ac2_explicit_save_memory_tool_creates_record(monkeypatch):
    """用户 alice 调 save_memory 工具，user_memories 表新增一行。"""
    memories.init()
    usage.current_identity.set(_identity("alice", "user"))
    from server.tools_builtin import save_memory
    result = save_memory("我喜欢中文回复", category="preference")
    assert "已记住" in result
    rows = _rows("SELECT * FROM user_memories WHERE user_id = 'alice'")
    assert len(rows) == 1
    assert rows[0]["content"] == "我喜欢中文回复"
    assert rows[0]["category"] == "preference"
    assert rows[0]["enabled"] == 1


# ---------------------------------------------------------------- AC3 召回注入

def test_ac3_dynamic_prompt_injects_active_memories(monkeypatch):
    """alice 有 2 条启用记忆，新会话开场系统提示含「用户记忆」段落。"""
    memories.init()
    memories.create("alice", "我喜欢中文回复", "preference")
    memories.create("alice", "我在做数据分析项目", "project")
    usage.current_identity.set(_identity("alice", "user"))
    from server import agent
    prompt = agent._dynamic_prompt({"messages": []})
    system_msg = prompt[0].content
    assert "用户记忆（2 条）" in system_msg
    assert "我喜欢中文回复" in system_msg
    assert "我在做数据分析项目" in system_msg


# ---------------------------------------------------------------- AC4 注入量控制

def test_ac4_memory_injection_respects_token_limit(monkeypatch):
    """alice 有 50 条记忆（总 token 超上限），只注入最近更新的若干条。"""
    memories.init()
    for i in range(50):
        memories.create("alice", f"记忆 {i}" * 100, "fact")  # 每条约 200 tokens
    monkeypatch.setenv("MEMORY_MAX_TOKENS", "2000")
    usage.current_identity.set(_identity("alice", "user"))
    from server import agent
    prompt = agent._dynamic_prompt({"messages": []})
    system_msg = prompt[0].content
    assert "用户记忆" in system_msg
    assert "仅显示最近" in system_msg
    # 粗粒度断言：注入段落长度有限
    memory_section = system_msg.split("用户记忆")[1]
    assert len(memory_section) < 10000  # 2000 tokens ≈ 8000 字符（中文）


# ---------------------------------------------------------------- AC5 权限隔离

def test_ac5_memory_isolation_user_vs_admin(client_factory):
    """alice 和 bob 各有记忆；alice 调 API 只见本人，admin 见全量。"""
    memories.init()
    memories.create("alice", "alice 的偏好", "preference")
    memories.create("bob", "bob 的事实", "fact")

    key_alice = auth.add_user("alice", role="user")
    key_admin = auth.add_user("root", role="admin")

    c_alice = client_factory()
    c_alice.headers.update({"Authorization": f"Bearer {key_alice}"})
    r = c_alice.get("/api/memories")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["content"] == "alice 的偏好"

    c_admin = client_factory()
    c_admin.headers.update({"Authorization": f"Bearer {key_admin}"})
    r = c_admin.get("/api/memories")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2


def test_ac5b_admin_can_edit_and_delete_other_user_memory(client_factory):
    """admin 可编辑/删除任意用户的记忆（v0.20.1 修复：反查目标归属）。"""
    memories.init()
    memories.create("alice", "alice 的原始内容", "fact")
    key_admin = auth.add_user("root", role="admin")

    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key_admin}"})
    r = c.get("/api/memories")
    alice_mem = next(m for m in r.json() if m["user_id"] == "alice")

    # admin 编辑 alice 的记忆
    r = c.patch(f"/api/memories/{alice_mem['id']}",
                json={"content": "admin 修改后", "enabled": False})
    assert r.status_code == 200
    rows = _rows("SELECT content, enabled FROM user_memories WHERE id = ?",
                 (alice_mem["id"],))
    assert rows[0]["content"] == "admin 修改后"
    assert rows[0]["enabled"] == 0

    # admin 删除 alice 的记忆
    r = c.delete(f"/api/memories/{alice_mem['id']}")
    assert r.status_code == 204
    rows = _rows("SELECT * FROM user_memories WHERE id = ?", (alice_mem["id"],))
    assert len(rows) == 0


# ---------------------------------------------------------------- AC6 CRUD API

def test_ac6_memory_crud_api_endpoints(client_factory):
    """alice 登录，创建/编辑/删除记忆 API 全流程。"""
    memories.init()
    key = auth.add_user("alice", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})

    # 创建
    r = c.post("/api/memories", json={"content": "新记忆", "category": "fact"})
    assert r.status_code == 201
    memory_id = r.json()["id"]

    # 编辑
    r = c.patch(f"/api/memories/{memory_id}", json={"content": "更新后"})
    assert r.status_code == 200
    rows = _rows("SELECT content FROM user_memories WHERE id = ?", (memory_id,))
    assert rows[0]["content"] == "更新后"

    # 删除
    r = c.delete(f"/api/memories/{memory_id}")
    assert r.status_code == 204
    rows = _rows("SELECT * FROM user_memories WHERE id = ?", (memory_id,))
    assert len(rows) == 0


# ---------------------------------------------------------------- AC7 审计集成

def test_ac7_memory_audit_log_integration(client_factory):
    """alice 创建/编辑/删除记忆，audit_log 表各有对应记录。"""
    memories.init()
    audit.init()
    key = auth.add_user("alice", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})

    r = c.post("/api/memories", json={"content": "测试记忆", "category": "fact"})
    memory_id = r.json()["id"]
    c.patch(f"/api/memories/{memory_id}", json={"enabled": False})
    c.delete(f"/api/memories/{memory_id}")

    recs = audit.query(limit=100)
    creates = [r for r in recs if r["action"] == "memory_create"]
    updates = [r for r in recs if r["action"] == "memory_update"]
    deletes = [r for r in recs if r["action"] == "memory_delete"]
    assert any(r["actor"] == "alice" and r["target"] == str(memory_id)
               for r in creates)
    assert any(r["actor"] == "alice" and r["target"] == str(memory_id)
               for r in updates)
    assert any(r["actor"] == "alice" and r["target"] == str(memory_id)
               for r in deletes)
    # 审计不记内容
    assert not any("测试记忆" in r.get("detail", "") for r in creates)


# ---------------------------------------------------------------- AC8 过期过滤

def test_ac8_memory_expiration_filtering(monkeypatch):
    """alice 有 3 条记忆（1 条已过期），召回时只注入未过期的。"""
    memories.init()
    memories.create("alice", "未过期 1", "fact")
    memories.create("alice", "未过期 2", "fact")
    expired_id = memories.create("alice", "已过期", "context")
    # 手动设置过期时间为过去
    conn = sqlite3.connect(config.STATE_DB)
    try:
        conn.execute("UPDATE user_memories SET expires_at = ? WHERE id = ?",
                     (time.time() - 3600, expired_id))
        conn.commit()
    finally:
        conn.close()

    usage.current_identity.set(_identity("alice", "user"))
    from server import agent
    prompt = agent._dynamic_prompt({"messages": []})
    system_msg = prompt[0].content
    assert "用户记忆（2 条）" in system_msg
    assert "未过期 1" in system_msg
    assert "未过期 2" in system_msg
    assert "已过期" not in system_msg
