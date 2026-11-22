"""PLAN 8.5：记忆 LLM 判重（spec：docs/specs/8.5-memory-llm-dedup.md）。

- AC1 缩写自动合并（LLM 裁决 duplicate → 自动合并）
- AC2 语义相反区分（LLM 裁决 distinct → 不合并不提示）
- AC3 无关键词重叠不调 LLM（前置过滤降成本）
- AC4 LLM 失败按 uncertain（fail-safe，交人判断）
- AC5 控制台端点仅返回 uncertain 对（用户隔离）
- AC6 对话确认后 merge_memory 工具合并

LLM 客户端一律 mock，不发起真实调用。
"""
import sqlite3

import pytest

from server import audit, auth, config, memories, usage


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


class _MockLLM:
    """按调用顺序返回预设 verdict 的假 LLM 客户端。"""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    def judge(self, content_a, content_b):
        self.calls += 1
        return self.verdicts.pop(0) if self.verdicts else "distinct"


# ---------------------------------------------------------------- AC1 缩写自动合并

def test_ac1_abbreviation_auto_merge(monkeypatch):
    """已有「项目数据库是 PostgreSQL」，存「项目用 PG」：
    mock LLM 裁决 duplicate → 旧条目自动删除、新条目保留，审计 actor=system。"""
    memories.init()
    memories.create("alice", "项目数据库是 PostgreSQL", "project")

    mock_llm = _MockLLM(["duplicate"])
    monkeypatch.setattr(memories, "llm_judge_pair", mock_llm.judge)

    usage.current_identity.set(_identity("alice", "user"))
    from server import tools_builtin
    result = tools_builtin.save_memory("项目用 PG", "project")

    assert "合并" in result

    rows = _rows("SELECT * FROM user_memories WHERE user_id = ?", ("alice",))
    assert len(rows) == 1
    assert rows[0]["content"] == "项目用 PG"

    recs = audit.query(limit=20)
    merge_logs = [r for r in recs if r["action"] == "memory_merge"]
    assert len(merge_logs) == 1
    assert merge_logs[0]["actor"] == "system"


# ---------------------------------------------------------------- AC2 语义相反区分

def test_ac2_opposite_meaning_distinct(monkeypatch):
    """「我喜欢中文」vs「我讨厌中文」：mock LLM 裁决 distinct → 不合并不提示。"""
    memories.init()
    memories.create("alice", "我喜欢中文", "preference")

    mock_llm = _MockLLM(["distinct"])
    monkeypatch.setattr(memories, "llm_judge_pair", mock_llm.judge)

    usage.current_identity.set(_identity("alice", "user"))
    from server import tools_builtin
    result = tools_builtin.save_memory("我讨厌中文", "preference")

    assert "合并" not in result
    assert "重复" not in result

    rows = _rows("SELECT * FROM user_memories WHERE user_id = ?", ("alice",))
    assert len(rows) == 2


# ---------------------------------------------------------------- AC3 无重叠不调 LLM

def test_ac3_no_keyword_overlap_skips_llm(monkeypatch):
    """「用 Python」vs「公司成立 2010 年」关键词无重叠 → LLM 零调用。"""
    memories.init()
    memories.create("alice", "公司成立 2010 年", "fact")

    mock_llm = _MockLLM(["duplicate"])
    monkeypatch.setattr(memories, "llm_judge_pair", mock_llm.judge)

    usage.current_identity.set(_identity("alice", "user"))
    from server import tools_builtin
    result = tools_builtin.save_memory("用 Python", "fact")

    assert mock_llm.calls == 0
    assert "合并" not in result


# ---------------------------------------------------------------- AC4 LLM 失败按 uncertain

def test_ac4_llm_failure_treated_as_uncertain(monkeypatch):
    """LLM 调用抛异常 → 不自动合并；返回消息附合并提示。"""
    memories.init()
    memories.create("alice", "我偏好简洁回答", "preference")

    def _failing_judge(content_a, content_b):
        raise RuntimeError("LLM 超时")

    monkeypatch.setattr(memories, "llm_judge_pair", _failing_judge)

    usage.current_identity.set(_identity("alice", "user"))
    from server import tools_builtin
    result = tools_builtin.save_memory("我偏好简短回复", "preference")

    rows = _rows("SELECT * FROM user_memories WHERE user_id = ?", ("alice",))
    assert len(rows) == 2
    assert "重复" in result

    recs = audit.query(limit=20)
    merge_logs = [r for r in recs if r["action"] == "memory_merge"]
    assert len(merge_logs) == 0


# ---------------------------------------------------------------- AC5 控制台端点仅 uncertain

def test_ac5_similar_endpoint_uncertain_only(client_factory, monkeypatch):
    """GET /api/memories/similar 返回 uncertain 对；user 只见本人；duplicate 对不出现。"""
    memories.init()
    memories.create("alice", "我偏好简洁回答", "preference")
    memories.create("alice", "回复请尽量简短", "preference")
    memories.create("bob", "bob 的记忆甲", "fact")
    memories.create("bob", "bob 的记忆乙", "fact")

    def _judge(content_a, content_b):
        if "bob" in content_a:
            return "duplicate"
        return "uncertain"

    monkeypatch.setattr(memories, "llm_judge_pair", _judge)

    key = auth.add_user("alice", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})

    r = c.get("/api/memories/similar")
    assert r.status_code == 200
    pairs = r.json()
    assert len(pairs) == 1
    assert {pairs[0]["keep"]["user_id"], pairs[0]["drop"]["user_id"]} == {"alice"}


# ---------------------------------------------------------------- AC6 对话确认后工具合并

def test_ac6_merge_memory_tool_after_confirm():
    """用户在对话确认合并后，模型调用 merge_memory 工具完成合并（审计 actor=当前用户）。"""
    memories.init()
    old_id = memories.create("alice", "我偏好简洁回答", "preference")
    new_id = memories.create("alice", "回复请尽量简短", "preference")

    usage.current_identity.set(_identity("alice", "user"))
    from server import tools_builtin
    result = tools_builtin.merge_memory(new_id, [old_id])

    assert "已合并" in result or "成功" in result

    rows = _rows("SELECT * FROM user_memories WHERE user_id = ?", ("alice",))
    assert len(rows) == 1
    assert rows[0]["id"] == new_id

    recs = audit.query(limit=20)
    merge_logs = [r for r in recs if r["action"] == "memory_merge"]
    assert len(merge_logs) == 1
    assert merge_logs[0]["actor"] == "alice"


def test_ac6b_merge_memory_rejects_other_user():
    """merge_memory 不能合并他人记忆（隔离保护）。"""
    memories.init()
    bob_id = memories.create("bob", "bob 的记忆", "fact")

    usage.current_identity.set(_identity("alice", "user"))
    from server import tools_builtin
    result = tools_builtin.merge_memory(bob_id, [])

    assert "失败" in result or "无权" in result or "不存在" in result
