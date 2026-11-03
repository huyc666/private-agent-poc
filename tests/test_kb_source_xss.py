"""P0-2 知识库原文视图存储型 XSS 防护（spec：docs/specs/security-hardening-v0.17.37.md
AC6-AC10）。

审核报告 §4（P0）：docx 渲染页把 parse_document 文本直拼 <p>/title/逻辑路径
（未 _esc）、Mammoth 输出直写 innerHTML（无白名单消毒）、无 CSP——恶意 docx
可在原文页形成存储型 XSS，同源脚本可窃取浏览器凭据并访问同源 API。

修复边界（spec 4.1-4.4）：服务端拼接必转义、Mammoth 输出经服务端白名单消毒
（POST /api/kb/sanitize）、原文页严格 CSP + X-Frame-Options + frame-ancestors。
"""
import re

import pytest


def _mk_evil_docx(path):
    """恶意 docx：w:t 原样保留攻击载荷（标签字面量 / 引号 / 实体源文本）。"""
    from docx import Document
    d = Document()
    d.add_paragraph('<img src=x onerror=alert(1)>伪装成文本的标签')
    d.add_paragraph('引号"单引\'与符号&和<b>标签</b>混合')
    d.add_paragraph('&lt;script&gt;alert(2)&lt;/script&gt;')
    d.save(path)


@pytest.fixture()
def client(tmp_path, client_factory, make_user, monkeypatch):
    from server import kb_access
    monkeypatch.setattr(kb_access, "_kb_home", lambda: tmp_path)
    monkeypatch.setattr(kb_access, "_declared_kb_packs",
                        lambda: [{"name": "t", "knowledge_bases": ["kb"],
                                  "allowed_roles": []}])
    key = make_user("xss_admin", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


# ---------------------------------------------------------------- AC6 恶意正文转义


def test_ac6_malicious_docx_text_escaped(client, tmp_path):
    """AC6（报告验收 1）：恶意 docx 正文（标签字面量/引号/实体）在渲染页只能
    以转义文本呈现，不得形成可执行节点。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    _mk_evil_docx(kb / "evil.docx")
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "evil.docx", "kind": "para", "a": 1, "b": 3})
    assert r.status_code == 200
    # 可执行节点不得存在（正文文本必须按文本呈现）
    assert "<img src=x" not in r.text
    assert "<script>alert" not in r.text
    assert "<b>标签</b>" not in r.text
    # 转义形态在场：浏览器渲染为纯文本
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text
    assert "&quot;" in r.text                     # 引号已转义
    # 实体源文本（&lt;... 字面量）不得被浏览器二次解码为标签：& 须再转义
    assert "&amp;lt;script&amp;gt;" in r.text


def test_ac8_malicious_filename_contexts(client, tmp_path):
    """AC8：恶意文件名直投 kb 目录（Windows 磁盘拒绝 <>\" 字符，取其可落盘
    的属性/字符串破坏字符 ' & $）——title/path 元素上下文与 fetch URL 的
    JS 字符串上下文均不得突破。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    name = "evil'&$.docx"
    _mk_evil_docx(kb / name)
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": name, "kind": "para", "a": 1, "b": 1})
    assert r.status_code == 200
    # title / 逻辑路径（元素文本上下文）：转义后呈现
    assert "evil&#39;&amp;$.docx" in r.text
    assert "kb/kb/evil&#39;&amp;$.docx" in r.text
    # fetch URL（内联 JS 单引号字符串上下文）：quote 百分号编码，无裸引号闭合
    assert "%27" in r.text
    assert f"fetch('{name}" not in r.text
    # 文本片段页 title 同口径
    (kb / "frag'&.md").write_text("第一行内容\n", encoding="utf-8")
    r2 = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "frag'&.md", "kind": "line", "a": 1, "b": 1})
    assert r2.status_code == 200
    assert "frag&#39;&amp;.md" in r2.text


# ---------------------------------------------------------------- AC7 白名单消毒矩阵


def test_ac7_sanitize_matrix():
    """AC7（报告验收 2）：HTML 白名单消毒器矩阵——白名单标签/属性/URL 保留，
    主动内容（含内容）整体移除，事件属性与危险 scheme 移除，未知标签剥壳保文。"""
    from server.html_sanitize import sanitize_html
    # 白名单标签保留
    assert "<p>ok</p>" in sanitize_html("<p>ok</p>")
    # 白名单属性保留（表格 colspan / a href https / img data:image）
    out = sanitize_html("<table><tr><td colspan='2'>c</td></tr></table>")
    assert "colspan" in out and "c" in out
    out = sanitize_html('<a href="https://example.com" title="t">link</a>')
    assert 'href="https://example.com"' in out and 'title="t"' in out
    out = sanitize_html('<img src="data:image/png;base64,AAAA" alt="i">')
    assert "data:image/png;base64,AAAA" in out
    # 主动内容连同内容整体移除（script 文本不得泄露为正文文本）
    out = sanitize_html("<p>a</p><script>steal()</script>"
                        "<iframe src='x'></iframe><object data='y'></object>")
    assert "<p>a</p>" in out
    assert "steal" not in out and "iframe" not in out and "object" not in out
    # 事件属性移除（属性值一并丢弃）
    out = sanitize_html("<p onclick='steal()'>t</p>")
    assert "onclick" not in out and "steal" not in out and "<p>t</p>" in out
    # 危险 URL scheme 移除
    assert "javascript:" not in sanitize_html("<a href='javascript:alert(1)'>x</a>")
    assert "data:text/html" not in sanitize_html(
        "<a href='data:text/html;base64,PHNjcmlwdD4='>x</a>")
    assert "<img" not in sanitize_html('<img src="data:text/html;base64,x">')
    # 未知标签剥壳保留文本（内容不丢失）
    out = sanitize_html("<font size='3'>txt</font>")
    assert "txt" in out and "<font" not in out
    # 实体不再解码为标签（无二次注入）
    assert "<script" not in sanitize_html("&lt;script&gt;x&lt;/script&gt;")


def test_ac7_duplicate_attribute_first_wins():
    """N1（外部复验 2026-11-02-v0.17.39-8bba994-review §5）：重复同名属性——
    校验与输出同取首次出现（浏览器 first-wins 语义），「危险在前、掩护在后」
    不得绕过「输出即可信 HTML」契约。"""
    from server.html_sanitize import sanitize_html
    # 危险在前、掩护在后：按首次出现判定——容器剥壳保文，void 整标签丢弃
    out = sanitize_html(
        '<a href="javascript:alert(1)" href="https://safe.example">x</a>')
    assert "javascript:" not in out and "<a" not in out and "x" in out
    assert "<img" not in sanitize_html(
        '<img src="javascript:alert(1)" src="data:image/png;base64,AA">')
    # 安全在前、危险在后：首个（安全）保留，重复项不输出
    out = sanitize_html(
        '<a href="https://safe.example" href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out and out.count("href=") == 1 \
        and "https://safe.example" in out
    # 合法重复同名属性同样只发首次（输出永不含重复属性）
    out = sanitize_html('<a href="https://a.example" href="https://b.example">x</a>')
    assert out.count("href=") == 1 and "https://a.example" in out


# ---------------------------------------------------------------- AC9 CSP 与框架防护


def test_ac9_csp_headers_and_nonce(client, tmp_path):
    """AC9：docx 渲染页（含内联脚本）带严格 CSP（nonce 版）+ X-Frame-Options；
    内联脚本 nonce 与响应头一致；文本片段页带简版 CSP。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    _mk_evil_docx(kb / "c.docx")
    (kb / "t.md").write_text("正文内容\n", encoding="utf-8")
    r = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "c.docx", "kind": "para", "a": 1, "b": 1})
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "script-src" in csp and "nonce-" in csp
    assert "frame-ancestors 'self'" in csp
    assert r.headers.get("x-frame-options") == "SAMEORIGIN"
    m = re.search(r"nonce-([A-Za-z0-9_-]+)", csp)
    assert m, "CSP 应携带具体 nonce 值"
    # 内联脚本携带与头一致的 nonce（nonce 不匹配 = 脚本被 CSP 拦截）
    assert re.search(rf"<script nonce=['\"]{m.group(1)}['\"]", r.text), \
        "内联脚本应携带与 CSP 一致的 nonce"
    # 文本片段页：简版 CSP（无脚本页面）
    r2 = client.get("/api/kb/source", params={
        "kb": "kb", "doc": "t.md", "kind": "line", "a": 1, "b": 1})
    assert r2.status_code == 200
    assert "default-src 'none'" in r2.headers.get("content-security-policy", "")


# ---------------------------------------------------------------- AC10 消毒端点


def test_ac10_sanitize_auth_required(client_factory):
    """AC10：认证模式未登录访问消毒端点 → 401。"""
    r = client_factory().post("/api/kb/sanitize", json={"html": "<p>x</p>"})
    assert r.status_code == 401


def test_ac10_sanitize_sanitizes(client):
    """AC10（报告验收 2 端点面）：登录后提交 Mammoth 输出——回显消毒结果，
    主动内容移除、白名单保留。"""
    r = client.post("/api/kb/sanitize", json={
        "html": "<p onclick=x>ok</p><script>steal()</script>"
                "<a href='javascript:alert(1)'>x</a>"})
    assert r.status_code == 200
    out = r.json()["html"]
    assert "<p>ok</p>" in out
    assert "steal" not in out and "onclick" not in out and "javascript:" not in out


def test_ac10_sanitize_size_limit(client):
    """AC10：超长输入（>2MB）拒绝——消毒端点不做无界处理。"""
    r = client.post("/api/kb/sanitize", json={"html": "x" * (2 * 1024 * 1024 + 1)})
    assert r.status_code == 413


def test_ac10_sanitize_content_length_precheck(client):
    """评审 #1 修复回归：Content-Length 前置预检——超限头直接 413
    （不把 body 读进内存）；非法 Content-Length → 400。"""
    # 小 body + 伪造超限头：预检在读 body 前即拒
    r = client.post("/api/kb/sanitize", content=b'{"html": "x"}',
                    headers={"Content-Length": str(2 * 1024 * 1024 + 1)})
    assert r.status_code == 413
    # 非法头值：不进入解析
    r2 = client.post("/api/kb/sanitize", content=b'{"html": "x"}',
                     headers={"Content-Length": "abc"})
    assert r2.status_code == 400
    # 正常 Content-Length（未超限）：照常消毒
    r3 = client.post("/api/kb/sanitize", json={"html": "<p>ok</p>"})
    assert r3.status_code == 200 and "<p>ok</p>" in r3.json()["html"]


def test_ac10_sanitize_open_mode_allows(client_factory, monkeypatch):
    """AC10：开放模式与 source 端点同口径——无身份也可用（离线部署演示态）。"""
    from server import config as cfg
    monkeypatch.setattr(cfg, "AUTH_ENABLED", False)
    c = client_factory()
    r = c.post("/api/kb/sanitize", json={"html": "<b>ok</b><u>u</u>"})
    assert r.status_code == 200
    assert "<b>ok</b>" in r.json()["html"]
