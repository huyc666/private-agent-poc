"""PLAN 8.3：记忆智能管理（spec：docs/specs/8.3-memory-smart-management.md）。

- AC1 相似记忆检测（关键词包含关系）
- AC2 记忆合并替换
- AC3 过期记忆启动清理
- AC4 注入分类加权排序
- AC5 合并审计日志
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


# ---------------------------------------------------------------- AC1 相似记忆检测

def test_ac1_similar_memory_detection():
    """alice 已有「我喜欢中文回复」，写入「我偏好中文回答」时检测到相似。"""
    memories.init()
    memories.create("alice", "我喜欢中文回复", "preference")

    similar = memories.find_similar("alice", "我偏好中文回答", "preference")
    assert len(similar) == 1
    assert similar[0]["content"] == "我喜欢中文回复"

    # 不同分类不视为相似
    similar = memories.find_similar("alice", "我偏好中文回答", "fact")
    assert len(similar) == 0

    # 无关键词重叠不视为相似
    similar = memories.find_similar("alice", "项目使用 PostgreSQL", "preference")
    assert len(similar) == 0


# ---------------------------------------------------------------- AC2 记忆合并替换

def test_ac2_memory_merge_replaces_old(client_factory):
    """alice 合并记忆 ID=1（旧）和 ID=2（新），旧记忆删除，新记忆保留。"""
    memories.init()
    id1 = memories.create("alice", "我喜欢中文回复", "preference")
    id2 = memories.create("alice", "我偏好中文回答", "preference")

    key = auth.add_user("alice", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})

    r = c.post("/api/memories/merge", json={
        "keep_id": id2,
        "merge_from_ids": [id1]
    })
    assert r.status_code == 200

    rows = _rows("SELECT * FROM user_memories WHERE id IN (?, ?)", (id1, id2))
    assert len(rows) == 1
    assert rows[0]["id"] == id2
    assert rows[0]["content"] == "我偏好中文回答"


# ---------------------------------------------------------------- AC3 过期记忆启动清理

def test_ac3_expired_memory_cleanup_on_startup(monkeypatch):
    """bob 有过期记忆 expires_at=2026-11-01，服务启动时被物理删除。"""
    memories.init()
    now = time.time()
    expired_time = now - 86400 * 21  # 21 天前（已过期）
    future_time = now + 86400 * 30   # 30 天后（未过期）

    id_expired = memories.create("bob", "过期记忆", "fact")
    id_active = memories.create("bob", "有效记忆", "fact")

    conn = sqlite3.connect(config.STATE_DB)
    conn.execute("UPDATE user_memories SET expires_at = ? WHERE id = ?",
                 (expired_time, id_expired))
    conn.execute("UPDATE user_memories SET expires_at = ? WHERE id = ?",
                 (future_time, id_active))
    conn.commit()
    conn.close()

    deleted = memories.cleanup_expired()
    assert deleted == 1

    rows = _rows("SELECT id FROM user_memories WHERE user_id = ?", ("bob",))
    assert len(rows) == 1
    assert rows[0]["id"] == id_active

    recs = audit.query(limit=10)
    expire_logs = [r for r in recs if r["action"] == "memory_expire"]
    assert len(expire_logs) == 1
    assert expire_logs[0]["actor"] == "system"


# ---------------------------------------------------------------- AC4 注入分类加权

def test_ac4_memory_injection_category_weighting(monkeypatch):
    """alice 有 preference/fact/context 三类记忆，注入时 preference 排最前。"""
    memories.init()
    memories.create("alice", "context 记忆", "context")
    memories.create("alice", "fact 记忆", "fact")
    memories.create("alice", "preference 记忆", "preference")

    from server import agent
    usage.current_identity.set(_identity("alice", "user"))

    messages = agent._dynamic_prompt({"messages": []})
    prompt = messages[0].content
    lines = [l for l in prompt.split("\n") if "记忆" in l or "preference" in l
             or "fact" in l or "context" in l]

    preference_idx = next(i for i, l in enumerate(lines)
                          if "preference" in l)
    fact_idx = next(i for i, l in enumerate(lines)
                    if "fact 记忆" in l)
    context_idx = next(i for i, l in enumerate(lines)
                       if "context 记忆" in l)

    assert preference_idx < fact_idx < context_idx


# ---------------------------------------------------------------- AC5 合并审计日志

def test_ac5_merge_audit_log(client_factory):
    """alice 合并记忆，审计日志记录 memory_merge（含新旧记忆 ID）。"""
    memories.init()
    audit.init()
    id1 = memories.create("alice", "旧记忆", "preference")
    id2 = memories.create("alice", "新记忆", "preference")

    key = auth.add_user("alice", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})

    r = c.post("/api/memories/merge", json={
        "keep_id": id2,
        "merge_from_ids": [id1]
    })
    assert r.status_code == 200

    recs = audit.query(limit=10)
    merge_logs = [r for r in recs if r["action"] == "memory_merge"]
    assert len(merge_logs) == 1
    assert merge_logs[0]["actor"] == "alice"
    assert merge_logs[0]["target"] == str(id2)
    assert f"merged_from={id1}" in merge_logs[0]["detail"]
