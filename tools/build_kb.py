#!/usr/bin/env python
"""知识库建索引工具（v0.17.24）：文档目录 → BM25 + 向量混合检索索引（SQLite 单文件）。

用法（在 agent-poc 目录下）：
    python tools/build_kb.py kb/common     # 通用知识库（平台包用）
    python tools/build_kb.py kb/hr         # HR 领域知识库（kb-demo 包用）

支持格式：.md/.txt/.docx/.pdf/.xlsx/.pptx/.html/.htm（解析器见 tools/kb_parsers.py，
统一转伪 Markdown 后分块）；.doc/.xls/.ppt 老格式明确报错提示转存 OOXML；
扫描件 PDF（无文本层）跳过 + WARNING；单文档失败不阻塞整库索引。

切块规则：按 Markdown 标题分层，段落合并到 ~500 字以内；
分词：中文按二元组（bigram）、英文/数字按单词——中文制度类文档检索够用，
零第三方依赖，完全离线可解释。知识更新后重跑本脚本即可（不碰包签名）。

向量通道（EMBED_BACKEND 配置化，与包内检索工具同一套配置）：
    local  （默认）fastembed + BAAI/bge-small-zh-v1.5，进程内离线推理，
           模型文件本地缓存（EMBED_CACHE_DIR 可指向预置目录，全程不联网）
    openai 外部 OpenAI 兼容 /v1/embeddings 服务（EMBED_API_BASE/EMBED_API_KEY）
引擎不可用（未装 fastembed/缺模型文件）时自动退化为纯 BM25 索引 + WARNING。

索引与查询两侧使用同一套分词；查询侧代码内嵌在各包的检索工具里
（包工具必须自包含，见 docs/PACK_GUIDE.md）。
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

try:
    from tools import kb_parsers          # 作为模块导入（测试/被 import 场景）
except ImportError:                        # 脚本直跑：sys.path[0] = tools/
    import kb_parsers

CHUNK_MAX = 500  # 每块目标最大字符数

PARSE_EXTS = (".md", ".txt", ".docx", ".pdf", ".xlsx", ".pptx", ".html", ".htm")
_LEGACY_EXTS = (".doc", ".xls", ".ppt")  # 老二进制格式：明确报错提示转存
_EMBED_BATCH = 32

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[a-z0-9]+")

_UNSET = object()  # build() 的 embed_fn 缺省哨兵：区分「未指定（走 env）」与「显式关闭」

# ---------------------------------------------------------------- 向量引擎


_ENGINE = None


def _embed_model() -> str:
    return os.environ.get("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")


def _get_engine():
    """fastembed 引擎懒加载 + 进程内缓存（模型本地缓存目录，推理不联网）。"""
    global _ENGINE
    if _ENGINE is None:
        from fastembed import TextEmbedding
        _ENGINE = TextEmbedding(model_name=_embed_model(),
                                cache_dir=os.environ.get("EMBED_CACHE_DIR") or None)
    return _ENGINE


def _embed_local(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _get_engine().embed(list(texts), batch_size=_EMBED_BATCH)]


def _embed_openai(texts: list[str]) -> list[list[float]]:
    """外部 OpenAI 兼容 /v1/embeddings（内网可调外部服务时按需配置）。"""
    import urllib.request
    base = os.environ.get("EMBED_API_BASE", "").rstrip("/")
    if not base:
        raise ValueError("EMBED_BACKEND=openai 需配置 EMBED_API_BASE")
    key = os.environ.get("EMBED_API_KEY", "")
    out: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH):
        batch = texts[i:i + _EMBED_BATCH]
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            base + "/embeddings",
            data=json.dumps({"model": _embed_model(), "input": batch}).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out.extend(item["embedding"] for item in data["data"])
    return out


def _resolve_embed():
    if os.environ.get("EMBED_BACKEND", "local").strip().lower() == "openai":
        return _embed_openai
    return _embed_local


# ---------------------------------------------------------------- 分词与分块


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


def _agg_anchor(vals: list) -> dict | None:
    """把一块内的行锚点聚合为单一定位锚点（min~max 区间；xlsx 取首个 Sheet）。"""
    vals = [v for v in vals if v]
    if not vals:
        return None
    kind = vals[0]["kind"]
    if kind == "sheet":
        sheet = vals[0].get("sheet", "")
        vals = [v for v in vals if v.get("sheet") == sheet]
    nums = [n for v in vals for n in (v["a"], v.get("b", v["a"]))]
    agg = {"kind": kind, "a": min(nums), "b": max(nums)}
    if kind == "sheet":
        agg["sheet"] = vals[0].get("sheet", "")
    return agg


def chunk_markdown_anchored(text: str, anchors: list) -> list[tuple[str, str, dict | None]]:
    """锚点版分块：与 chunk_markdown 同算法（标题分层 + 二次合并），
    返回 (标题路径, 块内容, 聚合锚点 dict 或 None)。锚点由 chunk 内首末行的
    行锚点聚合而来（见 kb_parsers 锚点约定），无锚点行（空行/分隔行）忽略。"""
    lines = text.splitlines()
    groups: list[tuple[str, list[int]]] = []  # (标题, 块内行号列表)
    heading = "（前言）"
    buf: list[int] = []

    def flush():
        idxs = list(buf)
        buf.clear()
        if any(lines[i].strip() for i in idxs):
            groups.append((heading, idxs))

    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            heading = m.group(2).strip()
        else:
            buf.append(i)
            if sum(len(lines[j]) for j in buf) > CHUNK_MAX:
                flush()
    flush()
    # 二次合并过小的块（同标题下）——与 chunk_markdown 同规则
    merged: list[tuple[str, list[int]]] = []
    for h, idxs in groups:
        if merged and merged[-1][0] == h:
            prev = "\n".join(lines[j] for j in merged[-1][1]).strip()
            cur = "\n".join(lines[j] for j in idxs).strip()
            if len(prev) + len(cur) <= CHUNK_MAX:
                merged[-1] = (h, merged[-1][1] + idxs)
                continue
        merged.append((h, idxs))
    out: list[tuple[str, str, dict | None]] = []
    for h, idxs in merged:
        content = "\n".join(lines[j] for j in idxs).strip()
        anchor = _agg_anchor([anchors[j] for j in idxs if 0 <= j < len(anchors)])
        out.append((h, content, anchor))
    return out


# ---------------------------------------------------------------- 建索引


def build(kb_dir: Path, embed_fn=_UNSET) -> None:
    """扫描 kb_dir 全部支持格式的文档，建 BM25 +（可选）向量索引。

    embed_fn：texts -> 向量列表，注入用（测试假向量/自定义引擎）；
    未指定时按 EMBED_BACKEND 从 env 解析；显式传 None 则仅建 BM25。
    """
    docs = sorted(p for p in kb_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in PARSE_EXTS + _LEGACY_EXTS)
    if not docs:
        print(f"{kb_dir} 下没有可入库的文档（支持：{'/'.join(PARSE_EXTS + _LEGACY_EXTS)}）")
        return

    # ---- 解析：单文档失败跳过 + 记入清单，不阻塞整库 ----
    parsed: list[tuple[str, str, list]] = []  # (文档名, 伪md全文, 行锚点列表)
    failed: list[tuple[str, str]] = []
    limit = kb_parsers.MAX_FILE_MB * 1024 * 1024
    for p in docs:
        try:
            if p.stat().st_size > limit:
                raise kb_parsers.KbParseError(
                    f"超过单文件解析上限 {kb_parsers.MAX_FILE_MB}MB，已跳过")
            if p.suffix.lower() in (".md", ".txt"):
                text = p.read_text(encoding="utf-8")
                # md/txt 锚点=源文件行号（kind=line，1 起），与 splitlines 逐行对齐
                anchors = [{"kind": "line", "a": i + 1}
                           for i in range(len(text.splitlines()))]
            else:
                text, anchors = kb_parsers.parse_document(p)
            parsed.append((p.name, text, anchors))
        except Exception as e:
            msg = str(e).replace(str(kb_dir), "") or type(e).__name__  # 不回显绝对路径
            failed.append((p.name, msg))

    # ---- 分块（锚点聚合到块；锚点生成失败不阻塞，块仍可入库）----
    chunks_all: list[tuple[str, str, str, int, str, str | None]] = []
    df: dict[str, int] = {}
    total_dl = 0
    for doc_name, text, anchors in parsed:
        try:
            chunked = chunk_markdown_anchored(text, anchors)
        except Exception as e:
            print(f"WARNING：{doc_name} 锚点分块失败，降级无锚点入库"
                  f"（{type(e).__name__}）")
            chunked = [(h, c, None) for h, c in chunk_markdown(text)]
        for heading, content, anchor in chunked:
            terms = tokenize(content)
            if not terms:
                continue
            tf: dict[str, int] = {}
            for t in terms:
                tf[t] = tf.get(t, 0) + 1
            for t in tf:
                df[t] = df.get(t, 0) + 1
            chunks_all.append((doc_name, heading, content, len(terms),
                               json.dumps(tf, ensure_ascii=False),
                               json.dumps(anchor, ensure_ascii=False) if anchor else None))
            total_dl += len(terms)

    # ---- 向量化（失败退化为纯 BM25）----
    vecs: list[list[float]] | None = None
    dim = 0
    if embed_fn is _UNSET:
        embed_fn = _resolve_embed()
    if embed_fn is not None and chunks_all:
        try:
            vecs = embed_fn([c[2] for c in chunks_all])
            dim = len(vecs[0])
            if any(len(v) != dim for v in vecs):
                raise ValueError("返回向量维度不一致")
        except Exception as e:
            print(f"WARNING：向量引擎不可用，本次仅建 BM25 索引（{type(e).__name__}）")
            vecs = None

    # ---- 落库（SEC-7，v0.17.32：临时库构建 → 完整性校验 → 原子替换）----
    # 旧实现先 unlink 现役库再重建：构建中途异常旧索引即丢失，重建期间查询
    # 也会打到半成品库。现在全部写入 kb.db.tmp；PRAGMA quick_check 通过后，
    # 旧库转存 kb.db.bak（只留一版，可回滚；转存失败降级为不保留），最后
    # 原子替换为现役库。失败路径（评审修复 C4/C8）：连接强制关闭防句柄锁住
    # tmp（unlink 抛 PermissionError 会掩盖原始异常、残留毒化下次构建）；
    # 转存成功而替换失败时回滚 bak，兑现「任何失败现役库保持完好」。
    import time

    db_path = kb_dir / "kb.db"
    tmp_path = kb_dir / "kb.db.tmp"
    bak_path = kb_dir / "kb.db.bak"
    tmp_path.unlink(missing_ok=True)   # C4：清历史崩溃残留，防 CREATE TABLE 撞表
    moved_to_bak = False               # C8：替换失败时判定是否需要回滚
    try:
        conn = sqlite3.connect(str(tmp_path))
        try:
            conn.execute("CREATE TABLE chunks (doc TEXT, heading TEXT, content TEXT,"
                         " dl INTEGER, terms TEXT, vec BLOB, anchor TEXT)")
            conn.execute("CREATE TABLE df (term TEXT PRIMARY KEY, n INTEGER)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

            n_chunks = len(chunks_all)
            for i, (doc, heading, content, dl, terms_json, anchor_json) in enumerate(chunks_all):
                vec_blob = None
                if vecs is not None:
                    vec_blob = np.asarray(vecs[i], dtype="<f4").tobytes()
                conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)",
                             (doc, heading, content, dl, terms_json, vec_blob, anchor_json))
            for t, n in df.items():
                conn.execute("INSERT INTO df VALUES (?,?)", (t, n))
            backend = os.environ.get("EMBED_BACKEND", "local").strip().lower()
            conn.executemany("INSERT INTO meta VALUES (?,?)", [
                ("n_chunks", str(n_chunks)),
                ("avgdl", str(total_dl / max(n_chunks, 1))),
                ("has_vec", "1" if vecs is not None else "0"),
                ("dim", str(dim)),
                ("embed_backend", backend),
                ("embed_model", _embed_model()),
            ])
            conn.commit()
            check = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()   # C4：异常路径同样关闭——句柄未释放会锁住 tmp，外层
                            # finally 的 unlink 在 Windows 抛 PermissionError 掩盖原始异常
        if not check or check[0] != "ok":
            raise RuntimeError(f"新索引完整性校验失败: {check}")

        if db_path.exists():
            try:
                os.replace(db_path, bak_path)   # 只留一版（覆盖上一版 bak）
                moved_to_bak = True
            except OSError as e:
                print(f"WARNING：旧索引转存 kb.db.bak 失败（继续替换，旧版不保留）: {e}")
        try:
            for attempt in range(3):
                try:
                    os.replace(tmp_path, db_path)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(0.1)
        except OSError:
            # C8：现役库已转存 bak、新库替换失败 → 回滚保住上一版；转存未
            # 成功（旧库仍在原位）则不动，防把陈旧 bak 覆盖到完好的旧库上。
            if moved_to_bak:
                try:
                    os.replace(bak_path, db_path)
                    print(f"WARNING：新索引替换失败，已回滚保留上一版索引: {db_path}")
                except OSError as e:
                    print(f"WARNING：回滚失败（现役库缺失），请手动将 {bak_path} "
                          f"改名为 {db_path}: {e}")
            raise
    finally:
        tmp_path.unlink(missing_ok=True)   # 成功路径 tmp 已被替换；失败清残留

    vec_info = f"，向量 {dim} 维" if vecs is not None else "（未启用向量，仅 BM25）"
    print(f"已建索引：{db_path}（{len(parsed)} 份文档入库 → {n_chunks} 块，"
          f"{len(df)} 个词项{vec_info}）")
    if failed:
        print(f"WARNING：{len(failed)} 份文档未能入库：")
        for name, reason in failed:
            print(f"  - {name}：{reason}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    build(Path(sys.argv[1]))
