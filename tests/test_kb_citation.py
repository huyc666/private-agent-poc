"""PLAN 2.2 检索问答与出处标注测试（spec：docs/specs/2.2-kb-citation.md，AC1~AC11）。

- AC1 锚点生成：md/txt=line、pdf=page、docx=para、pptx=slide、xlsx=sheet
- AC2 工具输出：人读片段 + 尾部 <kb-citations> JSON 块，剥离后出处完整
- AC3 agent SSE 层：on_tool_end 解析块发 citations 事件、给人看文本剥离标签
- AC4 前端卡片：静态断言（事件处理/转义/target=_blank/历史回放）+ citations 持久化
- AC5 定位端点分流：line/sheet/slide 文本片段页、page 原始 pdf 流、para mammoth 页
- AC6 拒绝路径：401/404/防穿越/RBAC 跟随/开放模式放行/无绝对路径
- AC7 无锚点降级：anchor NULL → 条目无链接不报错
- AC8/AC9 提示词：多库协作规则 + 防编造约束文案断言
- AC10 pack.yaml knowledge_bases 声明与 kb_allowed 判定
- AC11 包工具自包含（AST 断言）
"""
import ast
import json
import os
import re
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PACK_TOOLS = {
    "common": PROJECT_ROOT / "packs/core-knowledge/tools/search_common_knowledge.py",
    "hr": PROJECT_ROOT / "packs/kb-demo/tools/search_hr_knowledge.py",
}

CITE_RE = re.compile(r"<kb-citations>(\[.*?\])</kb-citations>", re.S)


# ---------------------------------------------------------------- 工具函数


def _build_kb(tmp_path, files):
    """落盘文档并建索引（默认 env 引擎链路，与 2.1 套件同款但只测锚点不测向量：
    注入假向量避免触发 fastembed 加载）。"""
    import tools.build_kb as build_kb
    kb = tmp_path / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        p = kb / name
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")

    def _fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]
    build_kb.build(kb, embed_fn=_fake_embed)
    return kb


def _rows(kb):
    conn = sqlite3.connect(str(kb / "kb.db"))
    try:
        return conn.execute(
            "SELECT doc, heading, content, anchor FROM chunks ORDER BY rowid").fetchall()
    finally:
        conn.close()


def _anchors(kb):
    return [(d, h, json.loads(a) if a else None) for d, h, _, a in _rows(kb)]


def _update_anchor_null(kb, doc):
    conn = sqlite3.connect(str(kb / "kb.db"))
    try:
        conn.execute("UPDATE chunks SET anchor=NULL WHERE doc=?", (doc,))
        conn.commit()
    finally:
        conn.close()


def _load_tool(rel: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"cite_{rel.stem}", rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_tool(kb, monkeypatch, which="common"):
    monkeypatch.setenv("KB_COMMON_DIR" if which == "common" else "KB_HR_DIR", str(kb))
    return _load_tool(PACK_TOOLS[which])


def _split_output(out: str):
    """拆出人读文本与 citations JSON；无块时第二项为 None。"""
    m = CITE_RE.search(out)
    if not m:
        return out, None
    return out[:m.start()].rstrip(), json.loads(m.group(1))


def _mk_pdf(path: Path, pages_text: list[str]):
    from tests.test_kb_ingestion import _mk_pdf as mk
    mk(path, pages_text)


def _mk_docx(path: Path):
    from tests.test_kb_ingestion import _mk_docx as mk
    mk(path)


def _mk_xlsx(path: Path):
    from tests.test_kb_ingestion import _mk_xlsx as mk
    mk(path)


def _mk_pptx(path: Path):
    from tests.test_kb_ingestion import _mk_pptx as mk
    mk(path)


# ---------------------------------------------------------------- AC1 锚点


def test_ac1_md_line_anchor(tmp_path):
    body = "# 保密协议\n\n新员工入职一周内签署保密协议。\n\n# 考勤\n\n九点前打卡。"
    kb = _build_kb(tmp_path, {"信息安全.md": body})
    src_lines = body.splitlines()
    for doc, heading, anchor in _anchors(kb):
        assert doc == "信息安全.md" and anchor["kind"] == "line"
        got = src_lines[anchor["a"] - 1:anchor["b"]]
        assert got, f"锚点 {anchor} 未落在源文件行上"


def test_ac1_txt_line_anchor(tmp_path):
    body = "纪要第一行。\n纪要第二行，内容更长一些。"
    kb = _build_kb(tmp_path, {"纪要.txt": body})
    assert all(a["kind"] == "line" for _, _, a in _anchors(kb))


def test_ac1_pdf_page_anchor(tmp_path):
    probe = tmp_path / "probe.pdf"
    _mk_pdf(probe, ["Annual leave five paid days per year",
                    "Check in before nine"])
    kb = _build_kb(tmp_path, {"h.pdf": probe.read_bytes()})
    pages = {a["a"] for _, _, a in _anchors(kb)}
    assert all(a["kind"] == "page" for _, _, a in _anchors(kb))
    assert pages == {1, 2}


def test_ac1_docx_para_anchor(tmp_path):
    probe = tmp_path / "probe.docx"
    _mk_docx(probe)
    kb = _build_kb(tmp_path, {"制度.docx": probe.read_bytes()})
    anchors = _anchors(kb)
    assert all(a["kind"] == "para" for _, _, a in anchors)
    paras = sorted({a["a"] for _, _, a in anchors})
    assert paras and min(paras) >= 1


def test_ac1_pptx_slide_anchor(tmp_path):
    probe = tmp_path / "probe.pptx"
    _mk_pptx(probe)
    kb = _build_kb(tmp_path, {"培训.pptx": probe.read_bytes()})
    anchors = _anchors(kb)
    assert anchors and all(a["kind"] == "slide" for _, _, a in anchors)
    assert {a["a"] for _, _, a in anchors} == {1}


def test_ac1_xlsx_sheet_anchor(tmp_path):
    probe = tmp_path / "probe.xlsx"
    _mk_xlsx(probe)
    kb = _build_kb(tmp_path, {"额度.xlsx": probe.read_bytes()})
    anchors = _anchors(kb)
    assert all(a["kind"] == "sheet" for _, _, a in anchors)
    assert {a["sheet"] for _, _, a in anchors} == {"假期额度"}


# ---------------------------------------------------------------- AC2 工具输出


def _seed_common_kb(tmp_path):
    body = "# 保密协议\n\n新员工入职一周内签署保密协议，违者按制度处理。\n\n" \
           "# 设备管理\n\n长期离岗设备须登记回收。"
    return _build_kb(tmp_path, {"信息安全.md": body})


def test_ac2_output_format(tmp_path, monkeypatch):
    kb = _seed_common_kb(tmp_path)
    tool = _make_tool(kb, monkeypatch)
    out = tool.search_common_knowledge("保密协议")
    human, cites = _split_output(out)
    assert cites is not None and len(cites) >= 1
    item = cites[0]
    assert item["kb"] == "common" and item["doc"] == "信息安全.md"
    assert item["kind"] == "line" and item["a"] >= 1
    assert "保密协议" in human and human.index("【来源") < human.index("保密协议")
    # 出处行在前、JSON 块在尾，人读文本无标签残留
    assert "<kb-citations>" not in human


def test_ac2_no_hit_no_block(tmp_path, monkeypatch):
    kb = _seed_common_kb(tmp_path)
    tool = _make_tool(kb, monkeypatch)
    out = tool.search_common_knowledge("完全不存在的词组xyzq")
    assert "未找到" in out
    assert CITE_RE.search(out) is None


def test_ac2_stripped_length_bound(tmp_path, monkeypatch):
    """人读文本（剥离块后）必须 ≤2000，保证服务端截断不吞出处行。"""
    long_body = "# 长制度\n\n" + "。".join(f"条款第{i}项内容描述" for i in range(200)) + "。"
    kb = _build_kb(tmp_path, {"长.md": long_body})
    tool = _make_tool(kb, monkeypatch)
    out = tool.search_common_knowledge("条款")
    human, _ = _split_output(out)
    assert len(human) <= 2000


# ---------------------------------------------------------------- AC3 SSE 层


def test_ac3_parse_citations_helper():
    from server.agent import parse_citations
    text = "【来源：a.md · X】\n片段\n<kb-citations>" \
           '[{"kb":"common","doc":"a.md","heading":"X","kind":"line","a":1,"b":2,"label":"a.md · X"}]' \
           "</kb-citations>"
    clean, cites = parse_citations(text)
    assert "<kb-citations>" not in clean and "片段" in clean
    assert cites and cites[0]["doc"] == "a.md"
    clean2, cites2 = parse_citations("普通输出无块")
    assert clean2 == "普通输出无块" and cites2 is None


def test_ac3_stream_emits_citations_event():
    """静态断言：stream_reply 的 on_tool_end 分支调用 parse_citations 并 yield citations 事件。"""
    src = (PROJECT_ROOT / "server/agent.py").read_text(encoding="utf-8")
    assert 'yield {"type": "citations"' in src
    assert "parse_citations" in src


# ---------------------------------------------------------------- AC4 前端与持久化


def test_ac4_frontend_static():
    src = (PROJECT_ROOT / "web/index.html").read_text(encoding="utf-8")
    assert "'citations'" in src                      # SSE 事件分支
    assert "kb-card" in src                          # 卡片渲染
    assert "'_blank'" in src                         # 新标签打开（row.target 赋值）
    assert "renderCitations" in src                  # 渲染函数（历史回放复用）


def test_ac4_sessions_persist_citations(tmp_path, monkeypatch):
    from server import sessions
    monkeypatch.setattr(sessions, "_db_path", lambda: tmp_path / "state.db")
    extra = json.dumps([{"kb": "common", "doc": "a.md", "label": "a.md · X"}],
                       ensure_ascii=False)
    sessions.append("s1", "u1", "user", "问题")
    sessions.append("s1", "u1", "assistant", "答案", extra=extra)
    hist = sessions.history("s1")
    assist = [m for m in hist if m["role"] == "assistant"][0]
    assert isinstance(assist["citations"], list)
    assert assist["citations"][0]["doc"] == "a.md"
    # 老消息无 citations 字段也不报错
    user_msg = [m for m in hist if m["role"] == "user"][0]
    assert user_msg.get("citations") in (None, [])


def test_ac4_chat_loop_collects_citations():
    """静态断言：chat SSE 循环收集 citations 事件并随 assistant 消息落库。"""
    src = (PROJECT_ROOT / "server/main.py").read_text(encoding="utf-8")
    assert 'ev["type"] == "citations"' in src
    assert "extra=" in src


# ---------------------------------------------------------------- AC5 定位端点


@pytest.fixture()
def client(tmp_path, client_factory, make_user, monkeypatch):
    from server import main as server_main
    monkeypatch.setattr(server_main, "_kb_home", lambda: tmp_path)
    monkeypatch.setattr(server_main, "_declared_kb_packs",
                        lambda: [{"name": "t", "knowledge_bases": ["kb"],
                                  "allowed_roles": []}])
    key = make_user("cite_admin", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def test_ac6_unauthorized_401(tmp_path, client_factory, monkeypatch):
    """认证开启且未登录 → 401。"""
    from server import main as server_main
    _seed_line_kb(tmp_path)
    monkeypatch.setattr(server_main, "_kb_home", lambda: tmp_path)
    monkeypatch.setattr(server_main, "_declared_kb_packs",
                        lambda: [{"name": "t", "knowledge_bases": ["kb"],
                                  "allowed_roles": []}])
    r = client_factory().get("/api/kb/source", params={
        "kb": "kb", "doc": "sec.md", "kind": "line", "a": 1, "b": 3})
    assert r.status_code == 401


def _seed_line_kb(tmp_path):
    body = "# 保密协议\n\n新员工入职一周内签署保密协议。\n\n# 考勤\n\n九点前打卡。"
    return _build_kb(tmp_path, {"sec.md": body})


def test_ac5_line_view(client, tmp_path):
    kb = _seed_line_kb(tmp_path)
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "sec.md", "kind": "line", "a": 1, "b": 3})
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "签署保密协议" in r.text            # 命中行在内
    assert "kb-line hl" in r.text              # 命中行高亮标记
    assert "九点前打卡" in r.text or "保密协议" in r.text  # 上下文行


def test_ac5_pdf_raw_stream(client, tmp_path):
    probe = tmp_path / "probe.pdf"
    _mk_pdf(probe, ["Annual leave five days", "Check in"])
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "h.pdf").write_bytes(probe.read_bytes())
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "h.pdf", "kind": "page", "a": 1})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content.startswith(b"%PDF")


def test_ac5_docx_mammoth_page(client, tmp_path):
    probe = tmp_path / "probe.docx"
    _mk_docx(probe)
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "制度.docx").write_bytes(probe.read_bytes())
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "制度.docx", "kind": "para", "a": 2, "b": 2})
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "/vendor/mammoth" in r.text          # 本地 vendor 引用（离线）
    assert "fallback" in r.text.lower() or "降级" in r.text
    # raw 模式返回原始二进制
    r2 = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "制度.docx", "kind": "para", "a": 2, "raw": "1"})
    assert r2.status_code == 200
    assert r2.content[:2] == b"PK"              # docx = zip


def test_ac5_sheet_slide_view(client, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    _mk_xlsx(kb / "额度.xlsx")
    _mk_pptx(kb / "培训.pptx")
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "额度.xlsx", "kind": "sheet", "a": 1, "b": 3,
        "sheet": "假期额度"})
    assert r.status_code == 200
    assert "年假：5" in r.text and "病假：10" in r.text   # 数据行逐字一致
    assert "kb-line hl" in r.text                        # 命中行高亮
    r2 = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "培训.pptx", "kind": "slide", "a": 1})
    assert r2.status_code == 200 and "入职流程" in r2.text


# ---------------------------------------------------------------- AC6 拒绝路径


def test_ac6_traversal_rejected(client, tmp_path):
    _seed_line_kb(tmp_path)
    for bad in ("../x.md", "a/b.md", "a\\b.md", "..", "\x01.md", "。md"):
        r = client.get("/api/kb/source", params={
            "kb": "kb", "doc": bad, "kind": "line", "a": 1, "b": 2})
        assert r.status_code == 404, f"{bad!r} 应 404"
    r = client.get("/api/kb/source", params={
        "kb": "../etc", "doc": "x.md", "kind": "line", "a": 1, "b": 2})
    assert r.status_code == 404                  # kb 名白名单
    # 错误信息不含绝对路径
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "不存在.md", "kind": "line", "a": 1, "b": 2})
    assert r.status_code == 404 and str(tmp_path) not in r.text


def test_ac6_kb_not_declared(client, tmp_path, monkeypatch):
    """kb 未被启用包 knowledge_bases 声明 → 404（fail-closed，不暴露库清单）。"""
    from server import main as server_main
    _seed_line_kb(tmp_path)
    monkeypatch.setattr(server_main, "_declared_kb_packs",
                        lambda: [{"name": "p", "knowledge_bases": ["other"],
                                  "allowed_roles": []}])
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "sec.md", "kind": "line", "a": 1, "b": 3})
    assert r.status_code == 404


def test_ac6_rbac_follow(monkeypatch):
    """kb_allowed 纯函数：包 allowed_roles 跟随（user 被拒、approver 放行、
    未配置默认放行、kb 未声明拒绝）。"""
    from server.main import kb_allowed
    packs = [{"name": "p1", "knowledge_bases": ["hr"], "allowed_roles": ["approver"]},
             {"name": "p2", "knowledge_bases": ["common"], "allowed_roles": []}]
    assert kb_allowed("hr", "approver", packs) is True
    assert kb_allowed("hr", "user", packs) is False
    assert kb_allowed("common", "user", packs) is True
    assert kb_allowed("unknown", "admin", packs) is False
    assert kb_allowed("hr", None, packs) is True          # 开放模式放行


# ---------------------------------------------------------------- AC7 无锚点降级


def test_ac7_null_anchor_degrade(tmp_path, monkeypatch):
    kb = _seed_common_kb(tmp_path)
    _update_anchor_null(kb, "信息安全.md")
    tool = _make_tool(kb, monkeypatch)
    out = tool.search_common_knowledge("保密协议")
    human, cites = _split_output(out)
    assert cites is not None
    assert all("a" not in c or c.get("a") is None for c in cites)
    assert "无锚点" in human or all(not c.get("a") for c in cites)


# ---------------------------------------------------------------- AC8/AC9 提示词


def test_ac8_ac9_prompt_text():
    from server import config
    prompt = config.KB_QA_PROMPT
    assert "多个知识库" in prompt or "各库" in prompt          # 多库协作规则
    assert "合并" in prompt                                    # 出处合并
    assert "如实说明" in prompt                                # 防编造：无命中
    assert "不得" in prompt and ("编造" in prompt or "虚构" in prompt)
    assert "kb-citations" in prompt                            # 不复述标签块


def test_ac9_prompt_assembled():
    """静态断言：系统提示词装配包含 KB_QA_PROMPT。"""
    src = (PROJECT_ROOT / "server/agent.py").read_text(encoding="utf-8")
    assert "config.KB_QA_PROMPT" in src


@pytest.mark.skipif(os.environ.get("RUN_LLM_TESTS") != "1",
                    reason="真实 LLM 集成测试：设 RUN_LLM_TESTS=1 且 .env 配置可用的模型密钥后运行")
def test_ac8_multi_kb_merge_e2e(tmp_path, make_user, monkeypatch):
    """端到端实测（真实 LLM，模式同 test_integration_llm.py）：跨库问题
    （HR 事项 + 设备事项）应触发两个库的检索工具，citations 事件合并多库出处，
    且模型回复不含 <kb-citations> 标签（防编造约束生效）。
    前置：python tools/build_kb.py kb/hr 与 kb/common 已重建（v0.17.25 带锚点）。"""
    import asyncio
    import uuid

    from server import agent, config, packs
    from server.state import STATE
    make_user("cite_e2e_admin", role="admin")
    if not config.LLM_API_KEY:
        pytest.skip("config.LLM_API_KEY 为空：.env 未配置模型密钥")
    # conftest 把包目录隔离到临时目录；端到端需要真实领域包挂载知识库检索工具
    monkeypatch.setattr(config, "PACKS_DIR", PROJECT_ROOT / "packs")
    packs.invalidate_scan_cache()
    config.MOCK_LLM = False
    asyncio.run(agent.build_agent())
    assert STATE.agent is not None
    try:
        sid = f"cite-e2e-{uuid.uuid4().hex[:8]}"

        async def _run():
            return [ev async for ev in agent.stream_reply(
                [{"role": "user",
                  "content": "入职多久能休年假？另外长期离岗的设备要怎么处理？"}],
                session_id=sid, user_id="cite_e2e_admin")]
        events = asyncio.run(_run())
    finally:
        # 还原 mock 模式与空 STATE（与离线用例默认假设一致，不污染同进程后续用例）
        config.MOCK_LLM = True
        STATE.llm = STATE.agent = None
        STATE.checkpointer = None
        STATE.checkpointer_cm = None
        STATE.pack_tools = {}
        if hasattr(STATE, "system_prompt_static"):
            delattr(STATE, "system_prompt_static")

    tools_seen = [e.get("name", "") for e in events
                  if e["type"] == "tool" and e.get("status") == "end"]
    cites = [c for e in events if e["type"] == "citations"
             for c in (e.get("citations") or [])]
    text = "".join(e["content"] for e in events if e["type"] == "token")
    kb_search = [t for t in tools_seen if "search_" in t and "knowledge" in t]
    assert kb_search, f"未观察到知识库检索工具调用（tools={tools_seen}）"
    assert "error" not in [e["type"] for e in events]
    assert len({c.get("kb") for c in cites}) >= 2, \
        f"出处应来自两个库（实际 {[(c.get('kb'), c.get('doc')) for c in cites]}）"
    assert "<kb-citations>" not in text, "模型回复不得复述机器可读出处块"


# ---------------------------------------------------------------- AC10 声明


def test_ac10_pack_yaml_knowledge_bases(tmp_path, monkeypatch):
    from server import packs
    pk = tmp_path / "p1"
    (pk / "tools").mkdir(parents=True)
    (pk / "pack.yaml").write_text(
        "name: p1\ndescription: t\n"
        f"core_version: '>={packs.CORE_VERSION}'\n"
        "knowledge_bases: [hr, legal]\nallowed_roles: [approver]\n",
        encoding="utf-8")
    monkeypatch.setattr(packs, "_packs_dir", lambda: tmp_path)
    info = packs._parse_pack(pk)
    assert info.get("knowledge_bases") == ["hr", "legal"], info.get("error")
    listed = [p for p in packs.list_packs() if p["name"] == "p1"][0]
    assert listed["knowledge_bases"] == ["hr", "legal"]


def test_ac10_real_packs_declared():
    for rel, bases in (("packs/core-knowledge/pack.yaml", ["common"]),
                       ("packs/kb-demo/pack.yaml", ["hr"])):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "knowledge_bases:" in text
        for b in bases:
            assert b in text


# ---------------------------------------------------------------- AC11 自包含


def test_ac11_pack_tools_self_contained():
    for which, rel in PACK_TOOLS.items():
        tree = ast.parse(rel.read_text(encoding="utf-8"))
        banned = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in ("server", "tools"):
                        banned.append(a.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in ("server", "tools"):
                    banned.append(node.module)
        assert not banned, f"{which} 包工具不得 import server/tools：{banned}"
