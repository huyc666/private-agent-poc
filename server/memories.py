"""PLAN 8.1：用户记忆沉淀与召回（spec：docs/specs/8.1-user-memory.md）。

- 记忆存储：user_memories 表（SQLite/Postgres 双后端统一适配）
- 记忆写入：显式「记住…」指令（save_memory 工具）
- 记忆召回：新会话开场按 user_id 注入系统提示（_dynamic_prompt）
- 记忆管理：控制台页签 CRUD + 权限隔离（user 管本人，admin 管全量）
"""
import logging
import sqlite3
import threading
import time

from . import audit, config, db

logger = logging.getLogger(__name__)

# schema 初始化（每进程、每个库路径只跑一次）
_schema_lock = threading.Lock()
_schema_ready_path: str | None = None


def init() -> None:
    """启动时建表（幂等）+ 容量裁剪（失败不阻断启动）。"""
    global _schema_ready_path
    path = config.STATE_DB
    if _schema_ready_path == path:
        return
    with _schema_lock:
        if _schema_ready_path == path:
            return
        try:
            conn = db.connect("state")
            if isinstance(conn, sqlite3.Connection):
                try:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS user_memories (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id TEXT NOT NULL,
                            content TEXT NOT NULL,
                            category TEXT NOT NULL DEFAULT 'fact',
                            created REAL NOT NULL,
                            updated REAL NOT NULL,
                            expires_at REAL,
                            enabled INTEGER NOT NULL DEFAULT 1
                        )""")
                    conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_user_memories_user_enabled
                        ON user_memories(user_id, enabled)""")
                    conn.commit()
                finally:
                    conn.close()
            else:
                # Postgres：db.ensure_postgres_schema 已在 connect 时执行
                conn.close()
            _schema_ready_path = path
        except Exception as e:
            logger.warning(f"[memories] 初始化失败（{type(e).__name__}）"
                           "——首次写入时会重试建表")


def create(user_id: str, content: str, category: str = "fact",
           expires_days: int | None = 90) -> int:
    """创建记忆。返回 memory_id。"""
    init()
    now = time.time()
    expires_at = now + expires_days * 86400 if expires_days else None
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        try:
            cur = conn.execute(
                "INSERT INTO user_memories (user_id, content, category, created, "
                "updated, expires_at, enabled) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (user_id, content, category, now, now, expires_at))
            memory_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    else:
        try:
            cur = conn.execute(
                "INSERT INTO user_memories (user_id, content, category, created, "
                "updated, expires_at, enabled) VALUES (%s, %s, %s, %s, %s, %s, 1) "
                "RETURNING id",
                (user_id, content, category, now, now, expires_at))
            memory_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
    return memory_id


def get_active(user_id: str, limit_tokens: int = 2000) -> list[dict]:
    """召回启用记忆（按 updated 倒序，注入量控制 ≤limit_tokens）。"""
    init()
    now = time.time()
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, content, category, updated FROM user_memories "
                "WHERE user_id = ? AND enabled = 1 "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY updated DESC",
                (user_id, now)).fetchall()
        finally:
            conn.close()
    else:
        try:
            rows = conn.execute(
                "SELECT id, content, category, updated FROM user_memories "
                "WHERE user_id = %s AND enabled = 1 "
                "AND (expires_at IS NULL OR expires_at > %s) "
                "ORDER BY updated DESC",
                (user_id, now)).fetchall()
        finally:
            conn.close()
    # token 估算：1 中文字符 ≈ 1 token，1 英文单词 ≈ 1.3 tokens
    # 简化：按字符数估算（中文按 1，英文按 0.75）
    items = []
    total_chars = 0
    max_chars = limit_tokens * 4  # 2000 tokens ≈ 8000 字符（保守估计）
    for row in rows:
        content = row["content"] if isinstance(row, dict) else row[1]
        char_count = len(content)
        if total_chars + char_count > max_chars:
            break
        items.append({
            "id": row["id"] if isinstance(row, dict) else row[0],
            "content": content,
            "category": row["category"] if isinstance(row, dict) else row[2],
        })
        total_chars += char_count
    # 更新 updated 字段（LRU 语义）
    if items:
        ids = [item["id"] for item in items]
        conn = db.connect("state")
        if isinstance(conn, sqlite3.Connection):
            try:
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE user_memories SET updated = ? WHERE id IN ({placeholders})",
                    (now, *ids))
                conn.commit()
            finally:
                conn.close()
        else:
            try:
                conn.execute(
                    "UPDATE user_memories SET updated = %s WHERE id = ANY(%s)",
                    (now, ids))
                conn.commit()
            finally:
                conn.close()
    return items


def list_all(user_id: str | None = None, enabled: int | None = None,
             category: str | None = None) -> list[dict]:
    """列出记忆（user 只看本人，admin 看全量）。"""
    init()
    where_clauses = []
    params = []
    if user_id:
        where_clauses.append("user_id = ?")
        params.append(user_id)
    if enabled is not None:
        where_clauses.append("enabled = ?")
        params.append(enabled)
    if category:
        where_clauses.append("category = ?")
        params.append(category)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT id, user_id, content, category, created, updated, "
                f"expires_at, enabled FROM user_memories WHERE {where_sql} "
                f"ORDER BY updated DESC",
                params).fetchall()
        finally:
            conn.close()
    else:
        try:
            rows = conn.execute(
                f"SELECT id, user_id, content, category, created, updated, "
                f"expires_at, enabled FROM user_memories WHERE {where_sql} "
                f"ORDER BY updated DESC",
                params).fetchall()
        finally:
            conn.close()
    return [dict(r) if isinstance(r, sqlite3.Row) else {
        "id": r[0], "user_id": r[1], "content": r[2], "category": r[3],
        "created": r[4], "updated": r[5], "expires_at": r[6], "enabled": r[7]
    } for r in rows]


def update(memory_id: int, user_id: str, **fields) -> bool:
    """编辑记忆（content/category/enabled/expires_at）。返回是否成功。"""
    init()
    allowed = {"content", "category", "enabled", "expires_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = list(updates.values()) + [memory_id, user_id]
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        try:
            cur = conn.execute(
                f"UPDATE user_memories SET {set_clause} "
                f"WHERE id = ? AND user_id = ?",
                params)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    else:
        try:
            cur = conn.execute(
                f"UPDATE user_memories SET {set_clause} "
                f"WHERE id = %s AND user_id = %s",
                params)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete(memory_id: int, user_id: str) -> bool:
    """删除记忆（硬删除）。返回是否成功。"""
    init()
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        try:
            cur = conn.execute(
                "DELETE FROM user_memories WHERE id = ? AND user_id = ?",
                (memory_id, user_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    else:
        try:
            cur = conn.execute(
                "DELETE FROM user_memories WHERE id = %s AND user_id = %s",
                (memory_id, user_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ============ PLAN 8.3：记忆智能管理 ============

def _extract_keywords(content: str) -> set[str]:
    """提取关键词（简单分词：中文按字+词组，英文按空格切分，去停用词）。"""
    keywords = set()
    import re
    # 中文：滑动窗口提取 2-4 字子串
    for match in re.finditer(r'[一-龥]+', content):
        chinese_text = match.group()
        for i in range(len(chinese_text)):
            for length in (2, 3, 4):
                if i + length <= len(chinese_text):
                    keywords.add(chinese_text[i:i+length])
    # 英文/数字：按空格切分
    for word in re.findall(r'[a-zA-Z0-9]+', content.lower()):
        if len(word) >= 2:
            keywords.add(word)
    return keywords


def find_similar(user_id: str, content: str, category: str) -> list[dict]:
    """查找同用户同分类下关键词重叠的记忆（轻量规则，非向量检索）。"""
    keywords = _extract_keywords(content)
    if not keywords:
        return []
    all_memories = list_all(user_id, category=category)
    similar = []
    for mem in all_memories:
        mem_keywords = _extract_keywords(mem["content"])
        # 关键词重叠检测
        if keywords & mem_keywords:
            similar.append(mem)
    return similar


def cleanup_expired() -> int:
    """删除已过期记忆（expires_at < now）。返回删除条数。"""
    init()
    now = time.time()
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        try:
            cur = conn.execute(
                "DELETE FROM user_memories "
                "WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,))
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()
    else:
        try:
            cur = conn.execute(
                "DELETE FROM user_memories "
                "WHERE expires_at IS NOT NULL AND expires_at < %s",
                (now,))
            conn.commit()
            deleted = cur.rowcount
        finally:
            conn.close()
    if deleted > 0:
        audit.log("system", "", "memory_expire", detail=f"deleted={deleted}")
    return deleted


def merge(keep_id: int, merge_from_ids: list[int], user_id: str) -> bool:
    """合并记忆：保留 keep_id，删除 merge_from_ids。返回是否成功。"""
    init()
    conn = db.connect("state")
    if isinstance(conn, sqlite3.Connection):
        try:
            for mid in merge_from_ids:
                conn.execute(
                    "DELETE FROM user_memories WHERE id = ? AND user_id = ?",
                    (mid, user_id))
            conn.commit()
            return True
        finally:
            conn.close()
    else:
        try:
            for mid in merge_from_ids:
                conn.execute(
                    "DELETE FROM user_memories WHERE id = %s AND user_id = %s",
                    (mid, user_id))
            conn.commit()
            return True
        finally:
            conn.close()
