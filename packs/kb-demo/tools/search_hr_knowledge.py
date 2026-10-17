"""kb-demo 领域包工具：search_hr_knowledge。

检索 HR 知识库（kb/hr），BM25 + 向量双通道混合排序（RRF 融合），返回带
来源标注的制度条文。本文件复制自 core-knowledge 包的检索工具模板，仅改
三处：工具名、KB_DIR 环境变量名/默认目录、docstring 中的库说明。

混合检索：BM25（中文 bigram，k1=1.5/b=0.75）与语义向量（EMBED_BACKEND：
local=fastembed 进程内离线推理 / openai=外部 OpenAI 兼容 /v1/embeddings）
双通道各取 top20，RRF(k=60) 融合；单通道无结果自动退化另一通道；向量
通道不可用时退化为纯 BM25。
包工具必须完全自包含：不 import server 任何模块（见 PACK_GUIDE.md）。
"""
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

# 数据目录：环境变量优先，默认 <项目根>/kb/hr（本文件位于 packs/<包>/tools/ 下）
_KB_DIR = Path(os.environ.get(
    "KB_HR_DIR",
    str(Path(__file__).resolve().parents[3] / "kb" / "hr")))
_KB_NAME = "hr"  # 库名（pack.yaml knowledge_bases 声明一致；出处逻辑路径用）
_TOP_K = 3
_SNIPPET_MAX = 400
_TOPN = 20        # 每通道参与 RRF 融合的条数
_RRF_K = 60       # RRF 平滑常数

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


# ---- 向量引擎（与 tools/build_kb.py 同一套 EMBED_* 配置；进程内缓存）----

_ENGINE = None
_WARNED = False


def _embed_model() -> str:
    return os.environ.get("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")


def _get_engine():
    global _ENGINE
    if _ENGINE is None:
        from fastembed import TextEmbedding
        _ENGINE = TextEmbedding(model_name=_embed_model(),
                                cache_dir=os.environ.get("EMBED_CACHE_DIR") or None)
    return _ENGINE


def _embed_openai(texts: list[str]) -> list[list[float]]:
    import urllib.request
    base = os.environ.get("EMBED_API_BASE", "").rstrip("/")
    if not base:
        raise ValueError("EMBED_BACKEND=openai 需配置 EMBED_API_BASE")
    key = os.environ.get("EMBED_API_KEY", "")
    out: list[list[float]] = []
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for i in range(0, len(texts), 32):
        req = urllib.request.Request(
            base + "/embeddings",
            data=json.dumps({"model": _embed_model(),
                             "input": texts[i:i + 32]}).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out.extend(item["embedding"] for item in data["data"])
    return out


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """按 EMBED_BACKEND 向量化；任何失败返回 None（调用方退化为 BM25）。"""
    global _WARNED
    backend = os.environ.get("EMBED_BACKEND", "local").strip().lower()
    try:
        if backend == "openai":
            return _embed_openai(texts)
        return [v.tolist() for v in _get_engine().embed(list(texts), batch_size=32)]
    except Exception as e:
        if not _WARNED:
            print(f"WARNING：向量通道不可用，检索退化为 BM25（{type(e).__name__}）",
                  file=sys.stderr)
            _WARNED = True
        return None


def search_hr_knowledge(query: str) -> str:
    """检索 HR 制度知识库（请假、考勤、加班调休等人事规定）。
    query 为自然语言问题或关键词，例如「年假有几天」「请假超过一周要交接吗」。
    返回带来源标注（文档名/章节）的相关条文，未检索到时会明确说明。"""
    db = _KB_DIR / "kb.db"
    if not db.is_file():
        return (f"HR 知识库索引不存在：{db}。"
                "请先用 tools/build_kb.py 建立索引（python tools/build_kb.py kb/hr）")
    terms = list(dict.fromkeys(_tokenize(query)))
    if not terms:
        return "查询词无法分词，请换用更具体的表述"

    conn = sqlite3.connect(str(db))
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        has_vec = meta.get("has_vec") == "1"

        # ---- BM25 通道（k1=1.5, b=0.75）----
        bm25_top: list[tuple[float, int, str, str, str, str | None]] = []
        if terms:
            n_chunks = int(meta["n_chunks"])
            avgdl = float(meta["avgdl"])
            df = dict(conn.execute(
                f"SELECT term, n FROM df WHERE term IN ({','.join('?' * len(terms))})",
                terms))
            if df:
                rows = conn.execute(
                    f"SELECT rowid, doc, heading, content, dl, terms, anchor FROM chunks"
                    f" WHERE EXISTS (SELECT 1 FROM json_each(chunks.terms)"
                    f" WHERE json_each.key IN ({','.join('?' * len(terms))}))",
                    terms).fetchall()
                k1, b = 1.5, 0.75
                for rowid, doc, heading, content, dl, terms_json, anchor in rows:
                    tf = json.loads(terms_json)
                    score = 0.0
                    for t in terms:
                        if t not in tf or t not in df:
                            continue
                        idf = math.log((n_chunks - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
                        score += idf * tf[t] * (k1 + 1) / (
                            tf[t] + k1 * (1 - b + b * dl / avgdl))
                    if score > 0:
                        bm25_top.append((score, rowid, doc, heading, content, anchor))
        bm25_top.sort(key=lambda x: -x[0])
        bm25_top = bm25_top[:_TOPN]

        # ---- 向量通道（点积暴力检索）----
        vec_top: list[tuple[float, int, str, str, str, str | None]] = []
        if has_vec:
            qv = _embed_texts([query])
            if qv:
                vrows = conn.execute(
                    "SELECT rowid, doc, heading, content, vec, anchor FROM chunks"
                    " WHERE vec IS NOT NULL").fetchall()
                if vrows:
                    mat = np.frombuffer(b"".join(r[4] for r in vrows),
                                        dtype="<f4").reshape(len(vrows), -1)
                    q = np.asarray(qv[0], dtype="<f4")
                    if mat.shape[1] == len(q):
                        dots = mat @ q
                        order = np.argsort(-dots, kind="stable")[:_TOPN]
                        vec_top = [(float(dots[i]), vrows[i][0], vrows[i][1],
                                    vrows[i][2], vrows[i][3], vrows[i][5])
                                   for i in order]
    finally:
        conn.close()

    # ---- RRF 融合（k=60）：单通道空 → 退化另一通道 ----
    def _rrf(channels):
        fused: dict[int, tuple[float, str, str, str, str | None]] = {}
        for ch in channels:
            for rank, entry in enumerate(ch, 1):
                _, rowid, doc, heading, content, anchor = entry
                s = fused[rowid][0] if rowid in fused else 0.0
                fused[rowid] = (s + 1.0 / (_RRF_K + rank), doc, heading, content, anchor)
        ordered = sorted(fused.values(), key=lambda x: -x[0])
        return [(doc, heading, content, anchor)
                for _, doc, heading, content, anchor in ordered]

    if bm25_top and vec_top:
        hits = _rrf([bm25_top, vec_top])
    elif bm25_top:
        hits = [(d, h, c, a) for _, _, d, h, c, a in bm25_top]
    elif vec_top:
        hits = [(d, h, c, a) for _, _, d, h, c, a in vec_top]
    else:
        return "知识库中未找到相关规定，可换用更口语化的关键词重试"

    # ---- 输出：人读条文（来源行在前，格式与 v0.17.0 一致）
    #      + 尾部机器可读出处块（v0.17.25：服务端解析后直推前端出处卡片，
    #      数据不经模型转写；人读文本不含该块，SSE 侧会剥离）----
    human_parts: list[str] = []
    cites: list[dict] = []
    for doc, heading, content, anchor_json in hits[:_TOP_K]:
        snippet = content if len(content) <= _SNIPPET_MAX else content[:_SNIPPET_MAX] + "……"
        item = {"kb": _KB_NAME, "doc": doc, "heading": heading,
                "label": f"{doc} · {heading}"}
        suffix = ""
        if anchor_json:
            try:
                a = json.loads(anchor_json)
            except Exception:
                a = None
            if isinstance(a, dict) and a.get("kind"):
                item["kind"] = a["kind"]
                item["a"] = a.get("a")
                if a.get("b") is not None:
                    item["b"] = a.get("b")
                if a.get("sheet"):
                    item["sheet"] = a["sheet"]
            else:
                suffix = "（无锚点）"
        else:
            suffix = "（无锚点）"
        human_parts.append(f"【来源：{doc} · {heading}】{suffix}\n{snippet}")
        cites.append(item)
    block = "<kb-citations>" + json.dumps(cites, ensure_ascii=False) + "</kb-citations>"
    return "\n\n".join(human_parts) + "\n\n" + block
