#!/usr/bin/env python
"""知识库建索引工具（v0.17.0）：文档目录 → BM25 检索索引（SQLite 单文件）。

用法（在 agent-poc 目录下）：
    python tools/build_kb.py kb/common     # 通用知识库（平台包用）
    python tools/build_kb.py kb/hr         # HR 领域知识库（kb-demo 包用）

切块规则：按 Markdown 标题分层，段落合并到 ~500 字以内；
分词：中文按二元组（bigram）、英文/数字按单词——中文制度类文档检索够用，
零第三方依赖，完全离线可解释。知识更新后重跑本脚本即可（不碰包签名）。

索引与查询两侧使用同一套分词；查询侧代码内嵌在各包的检索工具里
（包工具必须自包含，见 PACK_GUIDE.md）。
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

CHUNK_MAX = 500  # 每块目标最大字符数

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """中文 bigram（连续汉字段内两两组合）+ 英文/数字单词。
    索引与查询必须用同一个分词器。"""
    text = text.lower()
    terms = list(_WORD.findall(text))
    buf: list[str] = []

    def flush():
        if len(buf) == 1:
            terms.append(buf[0])
        else:
            for i in range(len(buf) - 1):
                terms.append(buf[i] + buf[i + 1])
        buf.clear()

    for ch in text:
        if _CJK.match(ch):
            buf.append(ch)
        else:
            flush()
    flush()
    return terms


def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """按标题切块，返回 (标题路径, 块内容)。小段落向标题块内合并。"""
    chunks: list[tuple[str, str]] = []
    heading = "（前言）"
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            chunks.append((heading, body))

    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            buf = []
            heading = m.group(2).strip()
        else:
            buf.append(line)
            if sum(len(x) for x in buf) > CHUNK_MAX:
                flush()
                buf = []
    flush()
    # 二次合并过小的块（同标题下）
    merged: list[tuple[str, str]] = []
    for h, c in chunks:
        if merged and merged[-1][0] == h and len(merged[-1][1]) + len(c) <= CHUNK_MAX:
            merged[-1] = (h, merged[-1][1] + "\n" + c)
        else:
            merged.append((h, c))
    return merged


def build(kb_dir: Path) -> None:
    docs = sorted(p for p in kb_dir.rglob("*") if p.suffix.lower() in (".md", ".txt"))
    if not docs:
        print(f"{kb_dir} 下没有 .md/.txt 文档")
        return
    db_path = kb_dir / "kb.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE chunks (doc TEXT, heading TEXT, content TEXT,"
                 " dl INTEGER, terms TEXT)")
    conn.execute("CREATE TABLE df (term TEXT PRIMARY KEY, n INTEGER)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    df: dict[str, int] = {}
    n_chunks = 0
    total_dl = 0
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for heading, content in chunk_markdown(text):
            terms = tokenize(content)
            if not terms:
                continue
            tf: dict[str, int] = {}
            for t in terms:
                tf[t] = tf.get(t, 0) + 1
            for t in tf:
                df[t] = df.get(t, 0) + 1
            conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?)",
                         (doc.name, heading, content, len(terms), json.dumps(tf)))
            n_chunks += 1
            total_dl += len(terms)
    for t, n in df.items():
        conn.execute("INSERT INTO df VALUES (?,?)", (t, n))
    conn.execute("INSERT INTO meta VALUES ('n_chunks', ?)", (str(n_chunks),))
    conn.execute("INSERT INTO meta VALUES ('avgdl', ?)", (str(total_dl / max(n_chunks, 1)),))
    conn.commit()
    conn.close()
    print(f"已建索引：{db_path}（{len(docs)} 份文档 → {n_chunks} 块，{len(df)} 个词项）")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    build(Path(sys.argv[1]))
