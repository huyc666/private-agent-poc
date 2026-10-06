"""core-knowledge 平台包工具：search_common_knowledge。

检索公司通用知识库（kb/common，BM25 索引），返回带来源标注的相关条文。
索引由 tools/build_kb.py 生成；知识更新后重跑建索引即可（数据在包外，不碰签名）。

本文件是「知识库检索工具模板」：复制到其他领域包时只需改三处——
工具名、KB_DIR 环境变量名/默认目录、docstring 中的库说明。
包工具必须完全自包含：不 import server 任何模块（见 PACK_GUIDE.md）。
"""
import json
import math
import os
import re
import sqlite3
from pathlib import Path

# 数据目录：环境变量优先，默认 <项目根>/kb/common（本文件位于 packs/<包>/tools/ 下）
_KB_DIR = Path(os.environ.get(
    "KB_COMMON_DIR",
    str(Path(__file__).resolve().parents[3] / "kb" / "common")))
_TOP_K = 3
_SNIPPET_MAX = 400

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """与 tools/build_kb.py 相同的分词器（中文 bigram + 英文/数字单词）。"""
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


def search_common_knowledge(query: str) -> str:
    """检索公司通用制度知识库（信息安全、行政流程等全员适用的规定）。
    query 为自然语言问题或关键词，例如「长期离岗设备怎么处理」。
    返回带来源标注（文档名/章节）的相关条文，未检索到时会明确说明。"""
    db = _KB_DIR / "kb.db"
    if not db.is_file():
        return (f"通用知识库索引不存在：{db}。"
                "请先用 tools/build_kb.py 建立索引（python tools/build_kb.py kb/common）")
    terms = list(dict.fromkeys(_tokenize(query)))
    if not terms:
        return "查询词无法分词，请换用更具体的表述"

    conn = sqlite3.connect(str(db))
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        n_chunks = int(meta["n_chunks"])
        avgdl = float(meta["avgdl"])
        df = dict(conn.execute(
            f"SELECT term, n FROM df WHERE term IN ({','.join('?' * len(terms))})",
            terms))
        if not df:
            return "知识库中未找到相关规定（没有词项命中），可换用更口语化的关键词重试"
        rows = conn.execute(
            f"SELECT doc, heading, content, dl, terms FROM chunks"
            f" WHERE EXISTS (SELECT 1 FROM json_each(chunks.terms)"
            f" WHERE json_each.key IN ({','.join('?' * len(terms))}))",
            terms).fetchall()
    finally:
        conn.close()

    # BM25 打分（k1=1.5, b=0.75）
    k1, b = 1.5, 0.75
    scored = []
    for doc, heading, content, dl, terms_json in rows:
        tf = json.loads(terms_json)
        score = 0.0
        for t in terms:
            if t not in tf or t not in df:
                continue
            idf = math.log((n_chunks - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
            score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scored.append((score, doc, heading, content))
    if not scored:
        return "知识库中未找到相关规定，可换用更口语化的关键词重试"
    scored.sort(key=lambda x: -x[0])

    out = []
    for score, doc, heading, content in scored[:_TOP_K]:
        snippet = content if len(content) <= _SNIPPET_MAX else content[:_SNIPPET_MAX] + "……"
        out.append(f"【来源：{doc} · {heading}】\n{snippet}")
    return "\n\n".join(out)
